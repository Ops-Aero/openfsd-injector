"""Supervise SRS presence + optional WAV→Opus TX without taking FSD down.

Empty ``SRS_HOST`` never constructs this. A refused/down SRS server is
retried in the background. ``SRS_TX=0`` or missing libopus keeps HTTP
audio and TCP radio presence (when connected) but does not claim TX.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ...config import AppConfig, StationConfig
from ...protocol import frequency_hz
from ..audio_http import AudioCatalog
from ..voice import cached_wav_path
from .client import PING_INTERVAL_SECONDS, SrsClient
from .opus import (
    SRS_FRAME_MS,
    OpusEncoder,
    OpusUnavailable,
    encode_pcm_frames,
    open_encoder,
    opus_available,
)
from .pcm import load_wav_pcm16_mono_16k

log = logging.getLogger(__name__)

RECONNECT_SECONDS = 5.0
MAX_RECONNECT_SECONDS = 60.0
CATALOG_POLL_SECONDS = 2.0
FRAME_SECONDS = SRS_FRAME_MS / 1000.0


@dataclass
class SrsTxStatus:
    """Honest TX report — tests must read this instead of trusting logs."""

    host: str
    port: int
    connected: bool
    tx_enabled: bool
    opus_available: bool
    transmitting: bool
    radios: int
    reason: str

    @property
    def audio_path_live(self) -> bool:
        """True only when UDP Opus frames are actually being sent."""
        return self.connected and self.tx_enabled and self.opus_available and self.transmitting


@dataclass
class _StationTx:
    icao: str
    callsign: str
    frequency_mhz: float
    frequency_hz: int
    wav_path: Path
    wav_mtime: float
    frames: list[bytes] = field(default_factory=list)
    task: asyncio.Task | None = None


class SrsBridge:
    """One SRS client, many ATIS radios. Never required for injector uptime."""

    def __init__(
        self,
        app: AppConfig,
        catalog: AudioCatalog,
        *,
        encoder_factory=None,
        client_factory=None,
        reconnect_seconds: float = RECONNECT_SECONDS,
    ) -> None:
        self.app = app
        self.catalog = catalog
        audio = app.atis.audio_http
        self.host = audio.srs_host
        self.port = audio.srs_port
        self.tx_enabled = audio.srs_tx
        self.name = audio.srs_name
        self.coalition = audio.srs_coalition
        self.eam_password = audio.srs_eam_password
        self.cache_dir = Path(app.atis.voice.cache_dir)
        self._encoder_factory = encoder_factory
        self._client_factory = client_factory or SrsClient
        self._reconnect_seconds = reconnect_seconds
        self.client: SrsClient | None = None
        self._stations: dict[str, _StationTx] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self.frames_sent = 0
        self.sessions = 0
        self.last_error = ""
        self._connected = False

    def status(self) -> SrsTxStatus:
        opus_ok = self._opus_ready()
        transmitting = self.frames_sent > 0 and any(
            st.frames and st.task is not None and not st.task.done()
            for st in self._stations.values()
        )
        if not self.host:
            reason = "SRS_HOST empty — HTTP audio only"
        elif not self._connected:
            reason = self.last_error or "SRS not connected"
        elif not self.tx_enabled:
            reason = "SRS_TX=0 — TCP radios only; HTTP audio remains"
        elif not opus_ok:
            reason = "libopus unavailable — TCP radios only; HTTP audio remains"
        elif not any(st.frames for st in self._stations.values()):
            reason = "connected; waiting for cached WAVs"
        elif transmitting:
            reason = "transmitting Opus on station frequencies"
        else:
            reason = "connected; TX starting"
        return SrsTxStatus(
            host=self.host,
            port=self.port,
            connected=self._connected,
            tx_enabled=self.tx_enabled,
            opus_available=opus_ok,
            transmitting=transmitting,
            radios=len(self._stations),
            reason=reason,
        )

    def _opus_ready(self) -> bool:
        if not self.tx_enabled:
            return False
        if self._encoder_factory is not None:
            return True
        return opus_available()

    def _make_encoder(self) -> OpusEncoder:
        if self._encoder_factory is not None:
            return self._encoder_factory()
        return open_encoder()

    def _make_client(self) -> SrsClient:
        return self._client_factory(
            self.host,
            self.port,
            name=self.name,
            coalition=self.coalition,
            eam_password=self.eam_password,
        )

    def snapshot_radios(self) -> list[tuple[str, float, int]]:
        """``(callsign, mhz, hz)`` for stations that currently have a WAV."""
        out: list[tuple[str, float, int]] = []
        for st in self.app.atis.stations:
            path = cached_wav_path(self.cache_dir, st.icao)
            if not path.is_file():
                continue
            out.append((st.callsign, st.frequency, frequency_hz(st.frequency)))
        return out

    async def run(self) -> None:
        if not self.host:
            log.info("SRS_HOST empty — not connecting (HTTP audio only)")
            await self._stop.wait()
            return
        delay = self._reconnect_seconds
        while not self._stop.is_set():
            try:
                await self._session()
                delay = self._reconnect_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                self._connected = False
                log.warning(
                    "SRS session failed (%s) — injector stays up; HTTP audio remains. retry in %.1fs",
                    exc,
                    delay,
                )
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                delay = min(delay * 2, MAX_RECONNECT_SECONDS)

    async def stop(self) -> None:
        self._stop.set()
        await self._teardown_session()

    async def _session(self) -> None:
        client = self._make_client()
        self.client = client
        await client.connect()
        self.sessions += 1
        self._connected = True
        self.last_error = ""
        radios = self._radio_payloads()
        await client.sync(radios)
        if self.eam_password:
            await client.send_eam_password(radios)
            await client.radio_update(radios)
        await client.ping()
        log.info(
            "SRS SYNC %s on %s:%s (%d radio(s)); %s",
            self.name,
            self.host,
            self.port,
            len(radios),
            self.status().reason,
        )
        if self.tx_enabled and not self._opus_ready():
            log.error(
                "SRS_TX is on but libopus is not available — not transmitting. "
                "Install libopus0 or set SRS_TX=0. HTTP audio on :8091 still works."
            )
        tasks = [
            asyncio.create_task(client.read_loop(), name="srs-tcp-read"),
            asyncio.create_task(client.ping_loop(PING_INTERVAL_SECONDS), name="srs-ping"),
            asyncio.create_task(self._catalog_loop(), name="srs-catalog"),
        ]
        try:
            await self._reconcile()
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                exc = task.exception()
                if exc is not None:
                    raise exc
            raise ConnectionError("SRS background task exited")
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._teardown_session()

    async def _teardown_session(self) -> None:
        async with self._lock:
            for st in self._stations.values():
                if st.task is not None:
                    st.task.cancel()
            await asyncio.gather(
                *(st.task for st in self._stations.values() if st.task is not None),
                return_exceptions=True,
            )
            for st in self._stations.values():
                st.task = None
        client, self.client = self.client, None
        self._connected = False
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    def _stations_with_wav(self) -> list[StationConfig]:
        found: list[StationConfig] = []
        for st in self.app.atis.stations:
            if cached_wav_path(self.cache_dir, st.icao).is_file():
                found.append(st)
        return found

    def _radio_payloads(self) -> list[dict]:
        if self.client is None:
            return []
        return self.client.radios_from_stations(
            [(st.callsign, st.frequency) for st in self._stations_with_wav()]
        )

    async def _catalog_loop(self) -> None:
        while not self._stop.is_set():
            await self._reconcile()
            await asyncio.sleep(CATALOG_POLL_SECONDS)

    async def _reconcile(self) -> None:
        async with self._lock:
            wanted: dict[str, StationConfig] = {
                st.icao.upper(): st for st in self._stations_with_wav()
            }
            current = set(self._stations)
            added = set(wanted) - current
            removed = current - set(wanted)
            radio_set_changed = bool(added or removed)

            for icao in removed:
                st = self._stations.pop(icao)
                if st.task is not None:
                    st.task.cancel()

            for icao, station in wanted.items():
                path = cached_wav_path(self.cache_dir, icao)
                mtime = path.stat().st_mtime
                existing = self._stations.get(icao)
                need_load = existing is None or existing.wav_mtime != mtime
                if existing is None:
                    existing = _StationTx(
                        icao=icao,
                        callsign=station.callsign,
                        frequency_mhz=station.frequency,
                        frequency_hz=frequency_hz(station.frequency),
                        wav_path=path,
                        wav_mtime=mtime,
                    )
                    self._stations[icao] = existing
                if need_load:
                    existing.wav_path = path
                    existing.wav_mtime = mtime
                    existing.frequency_mhz = station.frequency
                    existing.frequency_hz = frequency_hz(station.frequency)
                    existing.callsign = station.callsign
                    existing.frames = self._encode_wav(path, icao)
                    if existing.task is not None:
                        existing.task.cancel()
                        existing.task = None
                if (
                    self.tx_enabled
                    and existing.frames
                    and self.client is not None
                    and (existing.task is None or existing.task.done())
                ):
                    existing.task = asyncio.create_task(
                        self._tx_loop(existing),
                        name=f"srs-tx-{icao}",
                    )

            if radio_set_changed and self.client is not None and self.client.connected.is_set():
                await self.client.radio_update(self._radio_payloads())
                log.info(
                    "SRS RADIO_UPDATE %d station(s) on-freq",
                    len(self._stations),
                )

    def _encode_wav(self, path: Path, icao: str) -> list[bytes]:
        if not self.tx_enabled:
            return []
        if not self._opus_ready():
            return []
        try:
            pcm = load_wav_pcm16_mono_16k(path)
        except Exception:
            log.exception("SRS could not read %s for %s", path, icao)
            return []
        encoder: OpusEncoder | None = None
        try:
            encoder = self._make_encoder()
            frames = encode_pcm_frames(pcm, encoder)
        except OpusUnavailable as exc:
            log.error("SRS Opus unavailable for %s: %s", icao, exc)
            return []
        except Exception:
            log.exception("SRS Opus encode failed for %s", icao)
            return []
        finally:
            if encoder is not None:
                encoder.close()
        log.info("SRS encoded %s (%d frames) for TX on %s", icao, len(frames), path.name)
        return frames

    async def _tx_loop(self, station: _StationTx) -> None:
        index = 0
        while not self._stop.is_set():
            client = self.client
            if client is None or not station.frames:
                return
            try:
                await client.send_voice(station.frames[index], station.frequency_hz)
                self.frames_sent += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("SRS UDP TX failed for %s", station.icao)
                raise
            index = (index + 1) % len(station.frames)
            await asyncio.sleep(FRAME_SECONDS)
