from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

from ..atis.audio_http import AudioCatalog, AudioHttpServer
from ..atis.builder import AtisState, build_lines, next_letter
from ..atis.metar import fetch_metar
from ..atis.srs import SrsBridge
from ..atis.voice import VoiceBackend
from ..auth import require_credential, resolve_token
from ..client import FsdClient
from ..config import AppConfig, ConfigError, StationConfig
from ..protocol import build_new_atis, build_query_response, parse_query
from .base import Plugin

log = logging.getLogger(__name__)

# A station that stayed up this long is considered healthy, so its restart
# backoff is reset to the configured reconnect delay.
STABLE_UPTIME_SECONDS = 60.0
# Backoff ceiling, as a multiple of injector.reconnect_seconds.
MAX_BACKOFF_MULTIPLIER = 12
# Upper bound on tracked requesters, so the limiter cannot be used to grow memory.
MAX_TRACKED_REQUESTERS = 512


class ReplyRateLimiter:
    """Per-requester sliding-window limit on outbound replies.

    One ``$CQ ATIS`` request costs several packets, so an unlimited responder is
    an outbound amplifier. Requesters over the limit are dropped for the rest of
    the window.
    """

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
        max_tracked: int = MAX_TRACKED_REQUESTERS,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window = max(0.001, float(window_seconds))
        self.max_tracked = max(1, int(max_tracked))
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, requester: str) -> bool:
        now = self._clock()
        key = requester.strip().upper()
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        allowed = len(hits) < self.limit
        if allowed:
            hits.append(now)
        self._evict(now)
        return allowed

    def _evict(self, now: float) -> None:
        for key in [k for k, v in self._hits.items() if not v or now - v[-1] >= self.window]:
            del self._hits[key]
        overflow = len(self._hits) - self.max_tracked
        if overflow > 0:
            oldest = sorted(self._hits, key=lambda k: self._hits[k][-1])[:overflow]
            for key in oldest:
                del self._hits[key]


class StationRuntime:
    def __init__(
        self,
        app: AppConfig,
        station: StationConfig,
        catalog: AudioCatalog | None = None,
    ) -> None:
        self.app = app
        self.station = station
        self.catalog = catalog
        self.token = ""
        self.state = AtisState()
        self.client = FsdClient(app.server.host, app.server.port, app.auth)
        self.client.on_packet(self._on_packet)
        self.voice = VoiceBackend(app.atis.voice)
        self.limiter = ReplyRateLimiter(
            app.atis.reply_rate_limit, app.atis.reply_rate_window_seconds
        )
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        await self.refresh_atis(initial=True)
        # Mint immediately before #AA — OpsAero FSD JWTs expire in five minutes.
        self.token = await resolve_token(self.app)
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

    async def wait(self) -> None:
        """Raise as soon as one of this station's background tasks dies."""
        if not self._tasks:
            raise RuntimeError(f"{self.station.callsign} was not started")
        done, _pending = await asyncio.wait(self._tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
        finished = ", ".join(task.get_name() for task in done)
        raise ConnectionError(f"station task(s) exited unexpectedly: {finished}")

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
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
        if not initial and self.client.connected.is_set():
            try:
                await self.client.send(build_new_atis(self.station.callsign, self.state.letter))
            except Exception:
                log.exception("NEWATIS broadcast failed for %s", self.station.callsign)
        try:
            await self.voice.refresh(self.station, self.state.lines, self.state.letter)
        except Exception:
            log.exception("voice refresh failed for %s", self.station.icao)
        self._publish_audio_index()

    def _publish_audio_index(self) -> None:
        if self.catalog is None:
            return
        self.catalog.refresh_station(self.station, self.state.letter)

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
        if not self.limiter.allow(requester):
            log.warning("rate-limited ATIS request for %s from %s", cs, requester)
            return
        await self.client.send(build_query_response(cs, requester, "ATIS", "V", "voice.local"))
        for line in self.state.lines:
            await self.client.send(build_query_response(cs, requester, "ATIS", "T", line))
        total = len(self.state.lines) + 2
        await self.client.send(build_query_response(cs, requester, "ATIS", "E", str(total)))
        log.info("served ATIS %s %s -> %s", cs, self.state.letter, requester)


class StationSupervisor:
    """Keeps one station connected, restarting it with backoff when it dies."""

    def __init__(
        self,
        app: AppConfig,
        station: StationConfig,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        catalog: AudioCatalog | None = None,
    ) -> None:
        self.app = app
        self.station = station
        self.catalog = catalog
        self.runtime: StationRuntime | None = None
        self.restarts = 0
        self._sleep = sleep
        self._task: asyncio.Task | None = None

    @property
    def task(self) -> asyncio.Task | None:
        return self._task

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run(), name=f"{self.station.callsign}-supervisor"
        )

    async def wait(self) -> None:
        if self._task is None:
            raise RuntimeError(f"{self.station.callsign} supervisor was not started")
        await self._task

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        runtime, self.runtime = self.runtime, None
        if runtime is not None:
            with contextlib.suppress(Exception):
                await runtime.stop()

    async def _run(self) -> None:
        base = self.app.injector.reconnect_seconds
        delay = base
        loop = asyncio.get_running_loop()
        while True:
            runtime = StationRuntime(self.app, self.station, catalog=self.catalog)
            self.runtime = runtime
            started = loop.time()
            try:
                await runtime.start()
                await runtime.wait()
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await runtime.stop()
                raise
            except ConfigError:
                # Missing/invalid credentials will not fix themselves: surface it.
                with contextlib.suppress(Exception):
                    await runtime.stop()
                raise
            except Exception:
                log.exception("station %s dropped — will restart", self.station.callsign)
                with contextlib.suppress(Exception):
                    await runtime.stop()
            finally:
                self.runtime = None

            self.restarts += 1
            if loop.time() - started >= STABLE_UPTIME_SECONDS:
                delay = base
            log.info("restarting %s in %.1fs", self.station.callsign, delay)
            await self._sleep(delay)
            delay = min(delay * 2, base * MAX_BACKOFF_MULTIPLIER)


class AtisPlugin(Plugin):
    name = "atis"

    def __init__(self, cfg: AppConfig) -> None:
        super().__init__(cfg)
        self._supervisors: list[StationSupervisor] = []
        self._catalog: AudioCatalog | None = None
        self._audio_http: AudioHttpServer | None = None
        self._srs: SrsBridge | None = None
        self._srs_task: asyncio.Task | None = None

    async def start(self) -> None:
        if not self.cfg.atis.enabled:
            log.info("ATIS plugin disabled")
            return
        if not self.cfg.atis.stations:
            log.warning("ATIS plugin enabled but no stations configured")
            return
        # Fail before spawning anything if no credential is configured. Each
        # station still mints its own token immediately before #AA.
        require_credential(self.cfg)
        self._catalog = AudioCatalog(Path(self.cfg.atis.voice.cache_dir))
        for st in self.cfg.atis.stations:
            self._catalog.register(st)
        if self.cfg.atis.audio_http.enabled:
            self._audio_http = AudioHttpServer(
                self._catalog,
                self.cfg.atis.audio_http.host,
                self.cfg.atis.audio_http.port,
            )
            try:
                await self._audio_http.start()
            except OSError as exc:
                raise ConfigError(
                    f"ATIS audio HTTP could not bind {self.cfg.atis.audio_http.host}:"
                    f"{self.cfg.atis.audio_http.port}: {exc}"
                ) from exc
            log.info(
                "ATIS audio index at http://%s:%s/atis/index.json",
                self.cfg.atis.audio_http.host,
                self._audio_http.bound_port,
            )
        if self.cfg.atis.audio_http.srs_host:
            self._srs = SrsBridge(self.cfg, self._catalog)
            self._srs_task = asyncio.create_task(self._run_srs(), name="srs-bridge")
            log.info(
                "SRS_HOST=%s SRS_PORT=%s SRS_TX=%s — one process, many radios "
                "(name %s); HTTP audio stays on even if SRS is down",
                self.cfg.atis.audio_http.srs_host,
                self.cfg.atis.audio_http.srs_port,
                int(self.cfg.atis.audio_http.srs_tx),
                self.cfg.atis.audio_http.srs_name,
            )
        self._supervisors = [
            StationSupervisor(self.cfg, st, catalog=self._catalog)
            for st in self.cfg.atis.stations
        ]
        for supervisor in self._supervisors:
            supervisor.start()

    async def wait(self) -> None:
        tasks = [s.task for s in self._supervisors if s.task is not None]
        if not tasks:
            await super().wait()
            return
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None:
                raise exc
        finished = ", ".join(task.get_name() for task in done)
        raise ConnectionError(f"station supervisor(s) exited unexpectedly: {finished}")

    async def stop(self) -> None:
        supervisors, self._supervisors = self._supervisors, []
        await asyncio.gather(
            *(s.stop() for s in supervisors), return_exceptions=True
        )
        srs_task, self._srs_task = self._srs_task, None
        srs, self._srs = self._srs, None
        if srs is not None:
            with contextlib.suppress(Exception):
                await srs.stop()
        if srs_task is not None:
            srs_task.cancel()
            await asyncio.gather(srs_task, return_exceptions=True)
        server, self._audio_http = self._audio_http, None
        if server is not None:
            with contextlib.suppress(Exception):
                await server.stop()
        self._catalog = None

    async def _run_srs(self) -> None:
        """SRS failures stay here — they must not fail :meth:`wait` / FSD."""
        if self._srs is None:
            return
        try:
            await self._srs.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "SRS bridge crashed — injector stays up; HTTP audio remains"
            )
