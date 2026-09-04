"""Async SRS 2.x TCP control + UDP voice client.

One connection carries every ATIS radio (External Audio / EAM-style).
The injector FSD CID is unchanged — this is a separate radio identity.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Callable
from typing import Any

from .protocol import (
    GUID_LENGTH,
    MSG_EAM_PASSWORD,
    SRS_CLIENT_VERSION,
    UNIT_ID_DEFAULT,
    VoiceFrequency,
    VoicePacket,
    build_eam_password_message,
    build_ping_message,
    build_radio_update_message,
    build_sync_message,
    client_payload,
    decode_tcp_message,
    encode_tcp_message,
    encode_voice_packet,
    new_guid,
    radio_payload,
)

log = logging.getLogger(__name__)

PING_INTERVAL_SECONDS = 15.0
CONNECT_TIMEOUT_SECONDS = 5.0


class SrsClient:
    """One TCP writer + one UDP socket. Safe to close more than once."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        name: str = "OPSAERO_ATIS",
        coalition: int = 0,
        eam_password: str = "",
        version: str = SRS_CLIENT_VERSION,
        guid: str | None = None,
        unit_id: int = UNIT_ID_DEFAULT,
        lat: float = 0.0,
        lon: float = 0.0,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.name = name
        self.coalition = int(coalition)
        self.eam_password = eam_password
        self.version = version
        self.guid = guid or new_guid()
        self.unit_id = int(unit_id)
        self.lat = lat
        self.lon = lon
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._udp: socket.socket | None = None
        self._packet_number = 1
        self.tcp_sent: list[dict[str, Any]] = []
        self.udp_sent = 0
        self.connected = asyncio.Event()
        self._on_udp: Callable[[bytes], None] | None = None

    @property
    def guid_bytes(self) -> bytes:
        raw = self.guid.encode("ascii", errors="replace")
        if len(raw) < GUID_LENGTH:
            raw = raw + b"X" * (GUID_LENGTH - len(raw))
        return raw[:GUID_LENGTH]

    def client_dict(self, radios: list[dict[str, Any]]) -> dict[str, Any]:
        return client_payload(
            guid=self.guid,
            name=self.name,
            coalition=self.coalition,
            radios=radios,
            unit_id=self.unit_id,
            lat=self.lat,
            lon=self.lon,
        )

    def radios_from_stations(
        self, stations: list[tuple[str, float]]
    ) -> list[dict[str, Any]]:
        """``(callsign, frequency_mhz)`` → SRS radio list at integer Hz."""
        return [
            radio_payload(frequency_mhz=mhz, name=callsign) for callsign, mhz in stations
        ]

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=CONNECT_TIMEOUT_SECONDS,
        )
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setblocking(False)
        udp.connect((self.host, self.port))
        self._udp = udp
        self.connected.set()
        log.info("SRS TCP/UDP connected to %s:%s as %s", self.host, self.port, self.name)

    async def close(self) -> None:
        self.connected.clear()
        writer, self._writer = self._writer, None
        self._reader = None
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        udp, self._udp = self._udp, None
        if udp is not None:
            try:
                udp.close()
            except Exception:
                pass

    async def send_message(self, message: dict[str, Any]) -> None:
        if self._writer is None:
            raise ConnectionError("SRS TCP is not connected")
        self.tcp_sent.append(message)
        self._writer.write(encode_tcp_message(message))
        await self._writer.drain()

    async def sync(self, radios: list[dict[str, Any]]) -> None:
        await self.send_message(build_sync_message(self.client_dict(radios), version=self.version))

    async def radio_update(self, radios: list[dict[str, Any]]) -> None:
        await self.send_message(
            build_radio_update_message(self.client_dict(radios), version=self.version)
        )

    async def ping(self) -> None:
        await self.send_message(build_ping_message(version=self.version))
        await self.send_udp(self.guid_bytes)

    async def send_eam_password(self, radios: list[dict[str, Any]] | None = None) -> None:
        if not self.eam_password:
            return
        await self.send_message(
            build_eam_password_message(
                self.client_dict(radios or []),
                self.eam_password,
                version=self.version,
            )
        )

    async def send_udp(self, payload: bytes) -> None:
        if self._udp is None:
            raise ConnectionError("SRS UDP is not connected")
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(self._udp, payload)
        self.udp_sent += 1
        if self._on_udp is not None:
            self._on_udp(payload)

    async def send_voice(
        self,
        opus_frame: bytes,
        frequency_hz: int,
        *,
        modulation: int = 0,
    ) -> None:
        guid = self.guid_bytes
        packet = VoicePacket(
            audio=opus_frame,
            frequencies=[VoiceFrequency(frequency_hz=float(frequency_hz), modulation=modulation)],
            unit_id=self.unit_id,
            packet_id=self._packet_number,
            hops=0,
            relay_guid=guid,
            origin_guid=guid,
        )
        self._packet_number += 1
        await self.send_udp(encode_voice_packet(packet))

    async def read_loop(self) -> None:
        """Drain TCP so the server can push SYNC / settings. Lines are ignored."""
        reader = self._reader
        if reader is None:
            raise ConnectionError("SRS TCP is not connected")
        while True:
            line = await reader.readline()
            if not line:
                raise ConnectionError("SRS TCP closed")
            try:
                decode_tcp_message(line)
            except ValueError:
                log.debug("ignoring unreadable SRS TCP line (%d bytes)", len(line))

    async def ping_loop(self, interval: float = PING_INTERVAL_SECONDS) -> None:
        while True:
            await self.ping()
            await asyncio.sleep(interval)
