"""Async TCP FSD client used by injected stations."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from . import protocol as proto
from .config import AuthConfig

log = logging.getLogger(__name__)

PacketHandler = Callable[[str], Awaitable[None]]


class FsdClient:
    def __init__(self, host: str, port: int, auth: AuthConfig) -> None:
        self.host = host
        self.port = port
        self.auth = auth
        self.callsign = ""
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._handlers: list[PacketHandler] = []
        self.connected = asyncio.Event()

    def on_packet(self, handler: PacketHandler) -> None:
        self._handlers.append(handler)

    async def connect_atc(self, callsign: str, token: str) -> None:
        self.callsign = callsign
        log.info("connecting %s -> %s:%s", callsign, self.host, self.port)
        self._reader, self._writer = await asyncio.open_connection(self.host, self.port)

        ident = await self._readline()
        if not ident.startswith("$DI"):
            raise RuntimeError(f"expected $DI from server, got {ident!r}")
        log.debug("server ident: %s", ident)

        a = self.auth
        await self.send(proto.build_id(
            callsign, a.client_id, a.client_name, a.client_major, a.client_minor, a.cid
        ))
        await self.send(proto.build_add_atc(
            callsign, a.real_name, a.cid, token, a.rating, a.protocol_revision
        ))
        self.connected.set()
        log.info("%s logged on as ATC", callsign)

    async def send(self, packet: str) -> None:
        if not self._writer:
            raise RuntimeError("not connected")
        line = packet if packet.endswith("\r\n") else packet + "\r\n"
        self._writer.write(line.encode("utf-8"))
        await self._writer.drain()
        log.debug(">> %s", packet.rstrip())

    async def send_position(
        self,
        frequency_mhz: float,
        facility_type: int,
        vis_range_nm: int,
        lat: float,
        lon: float,
    ) -> None:
        await self.send(
            proto.build_atc_position(
                self.callsign,
                frequency_mhz,
                facility_type,
                vis_range_nm,
                self.auth.rating,
                lat,
                lon,
            )
        )

    async def disconnect(self) -> None:
        if self._writer and self.callsign:
            try:
                await self.send(proto.build_delete_atc(self.callsign, self.auth.cid))
            except Exception:
                pass
        self.connected.clear()
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._reader = None
        self._writer = None

    async def read_loop(self) -> None:
        assert self._reader
        while True:
            line = await self._readline()
            if line is None:
                raise ConnectionError("server closed connection")
            log.debug("<< %s", line)
            if line.startswith("$ER"):
                log.error("server error: %s", line)
            for handler in list(self._handlers):
                try:
                    await handler(line)
                except Exception:
                    log.exception("packet handler failed for %s", line)

    async def _readline(self) -> str:
        assert self._reader
        raw = await self._reader.readline()
        if not raw:
            raise ConnectionError("server closed connection")
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")
