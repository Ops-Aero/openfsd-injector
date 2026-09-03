"""Voice ATIS hooks.

openFSD is the FSD *data* server. It does not include Audio for VATSIM (AFV).
Pilots hear voice ATIS only if you also run a radio stack they can tune
(TrackAudio/AFV-compatible, SRS, etc.). This module is the place that stack
will plug into.

v0.1 implements:
  - `none`  : no audio
  - `file`  : keep a looping local file per station (you drop files in cache_dir)
  - scrape/tts placeholders so the hourly job has a single call site
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import StationConfig, VoiceConfig

log = logging.getLogger(__name__)


class VoiceBackend:
    def __init__(self, cfg: VoiceConfig) -> None:
        self.cfg = cfg
        self.cache = Path(cfg.cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)

    async def refresh(self, station: StationConfig, text_lines: list[str]) -> Path | None:
        if not self.cfg.enabled or self.cfg.backend in {"", "none"}:
            return None
        if self.cfg.backend == "file":
            path = self.cache / f"{station.icao.lower()}.wav"
            if path.exists():
                log.info("voice file present for %s: %s", station.icao, path)
                return path
            log.warning("voice.backend=file but %s is missing", path)
            return None
        if self.cfg.backend == "tts":
            log.warning("TTS backend is not wired yet — text ATIS still updates")
            return None
        log.warning("unknown voice backend %r", self.cfg.backend)
        return None
