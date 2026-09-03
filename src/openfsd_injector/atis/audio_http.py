"""Serve cached ATIS WAV/OGG over HTTP for opsaero-main / a radio client.

This is the first-party Linux/Docker path: TTS already writes
``audio/cache/{icao}.wav``. Pilots (or opsaero-main) fetch the file from
this process. It does not scrape LiveATC and does not transmit on SRS.

Default bind is ``0.0.0.0:8091`` inside compose (the compose file does
not publish the port). On the host, bind ``127.0.0.1`` unless
``AUDIO_HTTP_PUBLISH=1`` or ``AUDIO_HTTP_HOST`` is set.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from ..config import StationConfig
from .voice import OGG_SUFFIX, WAV_SUFFIX, cached_letter_path, cached_wav_path

log = logging.getLogger(__name__)

INDEX_NAME = "index.json"


@dataclass
class AudioStationRecord:
    icao: str
    callsign: str
    frequency: float
    letter: str = ""
    has_wav: bool = False
    has_ogg: bool = False


class AudioCatalog:
    """In-memory ATIS audio index, refreshed when a letter/wav changes."""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._stations: dict[str, AudioStationRecord] = {}

    def register(self, station: StationConfig) -> None:
        key = station.icao.upper()
        existing = self._stations.get(key)
        self._stations[key] = AudioStationRecord(
            icao=key,
            callsign=station.callsign,
            frequency=station.frequency,
            letter=existing.letter if existing else "",
            has_wav=existing.has_wav if existing else False,
            has_ogg=existing.has_ogg if existing else False,
        )
        self.refresh_station(station)

    def refresh_station(self, station: StationConfig, letter: str | None = None) -> None:
        key = station.icao.upper()
        rec = self._stations.get(key)
        if rec is None:
            rec = AudioStationRecord(
                icao=key, callsign=station.callsign, frequency=station.frequency
            )
            self._stations[key] = rec
        rec.callsign = station.callsign
        rec.frequency = station.frequency
        wav = cached_wav_path(self.cache_dir, key)
        ogg = wav.with_suffix(OGG_SUFFIX)
        rec.has_wav = wav.is_file()
        rec.has_ogg = ogg.is_file()
        if letter and letter.strip():
            rec.letter = letter.strip().upper()[:1]
        else:
            sidecar = cached_letter_path(self.cache_dir, key)
            rec.letter = sidecar.read_text(encoding="utf-8").strip().upper()[:1] if sidecar.is_file() else rec.letter
        self.write_index_file()

    def records(self) -> list[AudioStationRecord]:
        return [self._stations[k] for k in sorted(self._stations)]

    def index_payload(self, public_base: str = "") -> dict[str, Any]:
        base = public_base.rstrip("/")
        stations = []
        for rec in self.records():
            icao_l = rec.icao.lower()
            audio_url = None
            ogg_url = None
            if rec.has_wav:
                audio_url = f"{base}/atis/{icao_l}{WAV_SUFFIX}"
            if rec.has_ogg:
                ogg_url = f"{base}/atis/{icao_l}{OGG_SUFFIX}"
            stations.append(
                {
                    "icao": rec.icao,
                    "callsign": rec.callsign,
                    "frequency": rec.frequency,
                    "letter": rec.letter,
                    "audio_url": audio_url,
                    "ogg_url": ogg_url,
                }
            )
        return {"stations": stations}

    def write_index_file(self) -> None:
        path = self.cache_dir / INDEX_NAME
        path.write_text(
            json.dumps(self.index_payload(""), indent=2) + "\n",
            encoding="utf-8",
        )


@dataclass
class AudioHttpServer:
    """Tiny stdlib asyncio HTTP/1.1 server. GET-only, no auth."""

    catalog: AudioCatalog
    host: str
    port: int
    _server: asyncio.AbstractServer | None = field(default=None, init=False, repr=False)
    bound_port: int = field(default=0, init=False)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        sockets = self._server.sockets or []
        if sockets:
            self.bound_port = int(sockets[0].getsockname()[1])
        else:
            self.bound_port = self.port
        log.info(
            "ATIS audio HTTP on %s:%s (index /atis/index.json)",
            self.host,
            self.bound_port,
        )

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                return
            headers = await _read_headers(reader)
            try:
                method, target, _version = request_line.decode("latin-1").split()
            except ValueError:
                await _send(writer, 400, b"text/plain", b"bad request\n")
                return
            host = headers.get("host", f"{self.host}:{self.bound_port}")
            path = unquote(target.split("?", 1)[0])
            if method not in {"GET", "HEAD"}:
                await _send(writer, 405, b"text/plain", b"method not allowed\n")
                return
            await self._route(writer, method, path, host)
        except Exception:
            log.exception("audio HTTP request failed")
            try:
                await _send(writer, 500, b"text/plain", b"internal error\n")
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _route(
        self, writer: asyncio.StreamWriter, method: str, path: str, host: str
    ) -> None:
        if path == "/atis/index.json":
            scheme = "http"
            public_base = f"{scheme}://{host}"
            body = json.dumps(self.catalog.index_payload(public_base)).encode("utf-8")
            await _send(writer, 200, b"application/json", body, head=method == "HEAD")
            return
        match = re.fullmatch(r"/atis/([A-Za-z]{4})\.(wav|ogg)", path)
        if not match:
            await _send(writer, 404, b"text/plain", b"not found\n")
            return
        icao, ext = match.group(1).upper(), match.group(2)
        dest = cached_wav_path(self.catalog.cache_dir, icao)
        if ext == "ogg":
            dest = dest.with_suffix(OGG_SUFFIX)
        if not dest.is_file():
            await _send(writer, 404, b"text/plain", b"not found\n")
            return
        ctype = b"audio/wav" if ext == "wav" else b"audio/ogg"
        await _send_file(writer, dest, ctype, head=method == "HEAD")


async def _read_headers(reader: asyncio.StreamReader) -> dict[str, str]:
    headers: dict[str, str] = {}
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=10)
        if not line or line in (b"\r\n", b"\n"):
            break
        raw = line.decode("latin-1", errors="replace").rstrip("\r\n")
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


async def _send(
    writer: asyncio.StreamWriter,
    status: int,
    content_type: bytes,
    body: bytes,
    *,
    head: bool = False,
) -> None:
    reason = {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
    }.get(status, "OK")
    header = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type.decode('ascii')}\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n"
        "Cache-Control: no-store\r\n"
        "\r\n"
    ).encode("ascii")
    writer.write(header)
    if not head:
        writer.write(body)
    await writer.drain()


async def _send_file(
    writer: asyncio.StreamWriter,
    path: Path,
    content_type: bytes,
    *,
    head: bool = False,
) -> None:
    data = path.read_bytes()
    await _send(writer, 200, content_type, data, head=head)
