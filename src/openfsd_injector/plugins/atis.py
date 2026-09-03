from __future__ import annotations

import asyncio
import logging

from ..atis.builder import AtisState, build_lines, next_letter
from ..atis.metar import fetch_metar
from ..atis.voice import VoiceBackend
from ..auth import resolve_token
from ..client import FsdClient
from ..config import AppConfig, StationConfig
from ..protocol import build_query_response, parse_query
from .base import Plugin

log = logging.getLogger(__name__)


class StationRuntime:
    def __init__(self, app: AppConfig, station: StationConfig, token: str) -> None:
        self.app = app
        self.station = station
        self.token = token
        self.state = AtisState()
        self.client = FsdClient(app.server.host, app.server.port, app.auth)
        self.client.on_packet(self._on_packet)
        self.voice = VoiceBackend(app.atis.voice)
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.refresh_atis(initial=True)
        await self.client.connect_atc(self.station.callsign, self.token)
        await self.client.send_position(
            self.station.frequency,
            self.station.facility_type,
            self.station.vis_range_nm,
            self.station.lat,
            self.station.lon,
        )
        self._tasks = [
            asyncio.create_task(self.client.read_loop(), name=f"{self.station.callsign}-read"),
            asyncio.create_task(self._position_loop(), name=f"{self.station.callsign}-pos"),
            asyncio.create_task(self._refresh_loop(), name=f"{self.station.callsign}-atis"),
        ]
        log.info(
            "ATIS online %s on %.3f (%s)",
            self.station.callsign,
            self.station.frequency,
            self.station.icao,
        )

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.disconnect()

    async def refresh_atis(self, initial: bool = False) -> None:
        try:
            metar = await fetch_metar(self.app.atis.metar_url, self.station.icao)
        except Exception:
            log.exception("METAR fetch failed for %s", self.station.icao)
            if not self.state.metar:
                metar = f"{self.station.icao} METAR UNAVAILABLE"
            else:
                return
        if not initial and metar == self.state.metar:
            log.info("%s METAR unchanged — keeping information %s", self.station.icao, self.state.letter)
            return
        if not initial:
            self.state.letter = next_letter(self.state.letter)
        self.state.metar = metar
        self.state.lines = build_lines(self.station, self.state.letter, metar)
        log.info("%s information %s (%d lines)", self.station.icao, self.state.letter, len(self.state.lines))
        try:
            await self.voice.refresh(self.station, self.state.lines)
        except Exception:
            log.exception("voice refresh failed for %s", self.station.icao)

    async def _position_loop(self) -> None:
        interval = self.app.injector.position_interval_seconds
        while True:
            await asyncio.sleep(interval)
            await self.client.send_position(
                self.station.frequency,
                self.station.facility_type,
                self.station.vis_range_nm,
                self.station.lat,
                self.station.lon,
            )

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.app.atis.refresh_seconds)
            await self.refresh_atis()

    async def _on_packet(self, line: str) -> None:
        parsed = parse_query(line)
        if not parsed:
            return
        origin, dest, qtype, _payload = parsed
        if dest != self.station.callsign:
            return
        if qtype == "ATIS":
            await self._reply_atis(origin)
        elif qtype == "RN":
            await self.client.send(
                build_query_response(self.station.callsign, origin, "RN", self.app.auth.real_name, "", str(self.app.auth.rating))
            )
        elif qtype == "CAPS":
            await self.client.send(
                build_query_response(
                    self.station.callsign,
                    origin,
                    "CAPS",
                    "VERSION=1",
                    "ATCINFO=1",
                )
            )

    async def _reply_atis(self, requester: str) -> None:
        cs = self.station.callsign
        await self.client.send(build_query_response(cs, requester, "ATIS", "V", "voice.local"))
        for line in self.state.lines:
            await self.client.send(build_query_response(cs, requester, "ATIS", "T", line))
        total = len(self.state.lines) + 2
        await self.client.send(build_query_response(cs, requester, "ATIS", "E", str(total)))
        log.info("served ATIS %s %s -> %s", cs, self.state.letter, requester)


class AtisPlugin(Plugin):
    name = "atis"

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self._runtimes: list[StationRuntime] = []

    async def start(self) -> None:
        if not self.cfg.atis.enabled:
            log.info("ATIS plugin disabled")
            return
        if not self.cfg.atis.stations:
            log.warning("ATIS plugin enabled but no stations configured")
            return
        token = await resolve_token(self.cfg)
        self._runtimes = [StationRuntime(self.cfg, st, token) for st in self.cfg.atis.stations]
        await asyncio.gather(*(rt.start() for rt in self._runtimes))

    async def stop(self) -> None:
        await asyncio.gather(*(rt.stop() for rt in self._runtimes), return_exceptions=True)
