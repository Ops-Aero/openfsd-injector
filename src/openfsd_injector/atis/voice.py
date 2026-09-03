"""Voice ATIS hooks.

openFSD is the FSD *data* server. It does not include Audio for VATSIM (AFV).
Pilots hear voice ATIS only if a separate radio sidecar plays the cached file
on frequency (TrackAudio/AFV-compatible, SRS, etc.). This module generates
that file; it does not transmit it.

Backends:
  - ``none`` : no audio
  - ``file`` : use a looping local file per station (you drop files in cache_dir)
  - ``tts``  : synthesise looping WAV/OGG from the current text ATIS

TTS prefers ``edge-tts`` (cloud, no GPU). ``piper`` is an optional local
fallback. Cache path is ``{cache_dir}/{icao}.wav``; the file is rewritten only
when the information letter changes. LiveATC and other scraped feeds are never
fetched — ``scrape_url`` is ignored.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Protocol

from ..config import StationConfig, VoiceConfig

log = logging.getLogger(__name__)

LETTER_SUFFIX = ".letter"
WAV_SUFFIX = ".wav"
OGG_SUFFIX = ".ogg"


class Synthesizer(Protocol):
    """Write ``dest_wav`` as a PCM WAV. Tests inject a fake; CI never calls a TTS API."""

    async def synthesize(self, text: str, dest_wav: Path) -> None: ...


def atis_speech_text(text_lines: list[str]) -> str:
    """Turn ATIS lines into a single utterance for a TTS engine."""
    parts = [line.strip().rstrip(".") for line in text_lines if line.strip()]
    if not parts:
        return ""
    return ". ".join(parts) + "."


def information_letter(text_lines: list[str], letter: str | None = None) -> str:
    """Prefer the explicit ATIS letter; otherwise the last token of the first line."""
    if letter and letter.strip():
        token = letter.strip().upper()
        return token[0] if token[0].isalpha() else token
    if text_lines:
        tokens = text_lines[0].split()
        if tokens and len(tokens[-1]) == 1 and tokens[-1].isalpha():
            return tokens[-1].upper()
    return ""


def cached_wav_path(cache_dir: Path, icao: str) -> Path:
    return cache_dir / f"{icao.lower()}{WAV_SUFFIX}"


def cached_letter_path(cache_dir: Path, icao: str) -> Path:
    return cache_dir / f"{icao.lower()}{LETTER_SUFFIX}"


def read_cached_letter(cache_dir: Path, icao: str) -> str:
    path = cached_letter_path(cache_dir, icao)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip().upper()[:1]


def write_cached_letter(cache_dir: Path, icao: str, letter: str) -> None:
    cached_letter_path(cache_dir, icao).write_text(letter.upper()[:1], encoding="utf-8")


def write_pcm_wav(
    path: Path,
    frames: bytes,
    *,
    rate: int = 8000,
    channels: int = 1,
    sample_width: int = 2,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(sample_width)
        wav.setframerate(rate)
        wav.writeframes(frames)


def append_silence(path: Path, seconds: float) -> None:
    """Pad a WAV so a radio sidecar can loop it without an abrupt restart."""
    if seconds <= 0:
        return
    with wave.open(str(path), "rb") as reader:
        channels = reader.getnchannels()
        sample_width = reader.getsampwidth()
        rate = reader.getframerate()
        frames = reader.readframes(reader.getnframes())
        comptype = reader.getcomptype()
        compname = reader.getcompname()
    extra = b"\x00" * int(seconds * rate * sample_width * channels)
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(rate)
        writer.setcomptype(comptype, compname)
        writer.writeframes(frames + extra)


def run_ffmpeg(src: Path, dest: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise FileNotFoundError("ffmpeg is not on PATH")
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(src),
            str(dest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        raise RuntimeError(f"ffmpeg failed ({result.returncode}): {detail}")


class EdgeTtsSynthesizer:
    """Microsoft Edge cloud TTS. Needs network; no GPU."""

    def __init__(self, voice: str) -> None:
        self.voice = voice

    async def synthesize(self, text: str, dest_wav: Path) -> None:
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError(
                "edge-tts is not installed; pip install 'openfsd-injector[tts]'"
            ) from exc
        dest_wav.parent.mkdir(parents=True, exist_ok=True)
        tmp_mp3 = dest_wav.with_suffix(".mp3.tmp")
        try:
            communicate = edge_tts.Communicate(text, self.voice)
            await communicate.save(str(tmp_mp3))
            await asyncio.to_thread(run_ffmpeg, tmp_mp3, dest_wav)
        finally:
            tmp_mp3.unlink(missing_ok=True)


class PiperSynthesizer:
    """Optional local ONNX TTS (CPU). Requires a voice model path."""

    def __init__(self, model_path: str) -> None:
        self.model_path = model_path

    async def synthesize(self, text: str, dest_wav: Path) -> None:
        await asyncio.to_thread(self._synthesize_sync, text, dest_wav)

    def _synthesize_sync(self, text: str, dest_wav: Path) -> None:
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise RuntimeError(
                "piper is not installed; pip install piper-tts and set voice.piper_model"
            ) from exc
        if not self.model_path:
            raise RuntimeError("voice.piper_model is required for the piper engine")
        dest_wav.parent.mkdir(parents=True, exist_ok=True)
        voice = PiperVoice.load(self.model_path)
        with wave.open(str(dest_wav), "wb") as wav_file:
            synth = getattr(voice, "synthesize_wav", None) or voice.synthesize
            synth(text, wav_file)


class AutoSynthesizer:
    """Prefer edge-tts; fall back to piper when configured."""

    def __init__(self, cfg: VoiceConfig) -> None:
        self.cfg = cfg

    async def synthesize(self, text: str, dest_wav: Path) -> None:
        engine = (self.cfg.engine or "auto").strip().lower()
        if engine == "piper":
            await PiperSynthesizer(self.cfg.piper_model).synthesize(text, dest_wav)
            return
        errors: list[str] = []
        if engine in {"", "auto", "edge-tts"}:
            try:
                await EdgeTtsSynthesizer(self.cfg.voice).synthesize(text, dest_wav)
                return
            except Exception as exc:
                errors.append(f"edge-tts: {exc}")
                if engine == "edge-tts":
                    raise
                log.warning("edge-tts failed (%s); trying piper if configured", exc)
        if self.cfg.piper_model:
            try:
                await PiperSynthesizer(self.cfg.piper_model).synthesize(text, dest_wav)
                return
            except Exception as exc:
                errors.append(f"piper: {exc}")
                raise
        detail = "; ".join(errors) or "no TTS engine available"
        raise RuntimeError(
            f"TTS synthesis failed ({detail}). Install edge-tts "
            "(pip install 'openfsd-injector[tts]') or set voice.piper_model"
        )


class VoiceBackend:
    def __init__(
        self,
        cfg: VoiceConfig,
        synthesizer: Synthesizer | None = None,
        transcode=None,
    ) -> None:
        self.cfg = cfg
        self.cache = Path(cfg.cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self._synthesizer = synthesizer
        self._transcode = transcode or run_ffmpeg
        self._warned_scrape = False

    def _warn_if_scrape_configured(self) -> None:
        if self._warned_scrape or not self.cfg.scrape_url.strip():
            return
        self._warned_scrape = True
        log.warning(
            "voice.scrape_url is ignored — this injector never scrapes LiveATC "
            "or other audio feeds; TTS generates speech from our own ATIS text"
        )

    def _synthesizer_for_tts(self) -> Synthesizer:
        if self._synthesizer is not None:
            return self._synthesizer
        return AutoSynthesizer(self.cfg)

    async def refresh(
        self,
        station: StationConfig,
        text_lines: list[str],
        letter: str | None = None,
    ) -> Path | None:
        self._warn_if_scrape_configured()
        if not self.cfg.enabled or self.cfg.backend in {"", "none"}:
            return None
        if self.cfg.backend == "file":
            path = cached_wav_path(self.cache, station.icao)
            if path.exists():
                log.info("voice file present for %s: %s", station.icao, path)
                return path
            log.warning("voice.backend=file but %s is missing", path)
            return None
        if self.cfg.backend == "tts":
            return await self._refresh_tts(station, text_lines, letter)
        log.warning("unknown voice backend %r", self.cfg.backend)
        return None

    async def _refresh_tts(
        self,
        station: StationConfig,
        text_lines: list[str],
        letter: str | None,
    ) -> Path | None:
        current = information_letter(text_lines, letter)
        dest = cached_wav_path(self.cache, station.icao)
        cached = read_cached_letter(self.cache, station.icao)
        if current and cached == current and dest.is_file():
            log.info(
                "voice cache hit for %s information %s: %s",
                station.icao,
                current,
                dest,
            )
            return dest

        speech = atis_speech_text(text_lines)
        if not speech:
            log.warning("TTS skipped for %s — no ATIS text", station.icao)
            return None

        tmp = dest.with_suffix(".wav.tmp")
        try:
            await self._synthesizer_for_tts().synthesize(speech, tmp)
            if self.cfg.loop_silence_seconds > 0:
                await asyncio.to_thread(append_silence, tmp, self.cfg.loop_silence_seconds)
            tmp.replace(dest)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

        if current:
            write_cached_letter(self.cache, station.icao, current)

        ogg = dest.with_suffix(OGG_SUFFIX)
        try:
            await asyncio.to_thread(self._transcode, dest, ogg)
        except Exception as exc:
            log.info("ogg companion not written for %s: %s", station.icao, exc)

        log.info(
            "TTS wrote %s information %s -> %s",
            station.icao,
            current or "?",
            dest,
        )
        return dest
