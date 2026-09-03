"""TTS voice backend: cache by information letter; never call a real TTS API."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import yaml

from openfsd_injector.atis.voice import (
    VoiceBackend,
    atis_speech_text,
    cached_letter_path,
    cached_wav_path,
    information_letter,
    write_pcm_wav,
)
from openfsd_injector.config import StationConfig, VoiceConfig

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeSynthesizer:
    """Writes a tiny silent WAV. CI must not reach edge-tts or piper."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    async def synthesize(self, text: str, dest_wav: Path) -> None:
        self.calls.append((text, dest_wav))
        write_pcm_wav(dest_wav, b"\x00\x00" * 160)


def station(icao: str = "EGLL") -> StationConfig:
    return StationConfig(
        icao=icao,
        name="Heathrow Information",
        callsign=f"{icao}_ATIS",
        frequency=128.080,
        lat=51.4775,
        lon=-0.4614,
    )


def backend(
    tmp_path: Path,
    synth: FakeSynthesizer | None = None,
    *,
    enabled: bool = True,
    backend_name: str = "tts",
    scrape_url: str = "",
    loop_silence_seconds: float = 0.0,
    transcode=None,
) -> tuple[VoiceBackend, FakeSynthesizer]:
    fake = synth or FakeSynthesizer()

    def _noop_transcode(src: Path, dest: Path) -> None:
        dest.write_bytes(src.read_bytes())

    voice = VoiceBackend(
        VoiceConfig(
            enabled=enabled,
            backend=backend_name,
            scrape_url=scrape_url,
            cache_dir=str(tmp_path / "cache"),
            loop_silence_seconds=loop_silence_seconds,
        ),
        synthesizer=fake,
        transcode=transcode or _noop_transcode,
    )
    return voice, fake


LINES_C = ["HEATHROW INFORMATION C", "EGLL 031250Z 27008KT 9999 FEW030"]
LINES_D = ["HEATHROW INFORMATION D", "EGLL 031350Z 27010KT 9999 SCT025"]


def test_atis_speech_text_joins_lines():
    assert atis_speech_text(LINES_C) == (
        "HEATHROW INFORMATION C. EGLL 031250Z 27008KT 9999 FEW030."
    )


def test_information_letter_prefers_explicit():
    assert information_letter(LINES_C, "d") == "D"
    assert information_letter(LINES_C) == "C"
    assert information_letter([]) == ""


@pytest.mark.asyncio
async def test_tts_writes_wav_and_letter_sidecar(tmp_path):
    voice, fake = backend(tmp_path)
    path = await voice.refresh(station(), LINES_C, "C")
    assert path is not None
    assert path.name == "egll.wav"
    assert path.is_file()
    assert cached_letter_path(voice.cache, "EGLL").read_text() == "C"
    assert path.with_suffix(".ogg").is_file()
    assert len(fake.calls) == 1
    assert fake.calls[0][0].startswith("HEATHROW INFORMATION C")


@pytest.mark.asyncio
async def test_tts_skips_resynthesis_when_letter_unchanged(tmp_path):
    voice, fake = backend(tmp_path)
    first = await voice.refresh(station(), LINES_C, "C")
    second = await voice.refresh(station(), LINES_C, "C")
    assert first == second
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_tts_resynthesizes_when_letter_changes(tmp_path):
    voice, fake = backend(tmp_path)
    await voice.refresh(station(), LINES_C, "C")
    await voice.refresh(station(), LINES_D, "D")
    assert len(fake.calls) == 2
    assert cached_letter_path(voice.cache, "EGLL").read_text() == "D"
    assert fake.calls[1][0].startswith("HEATHROW INFORMATION D")


@pytest.mark.asyncio
async def test_tts_regenerates_if_wav_is_missing(tmp_path):
    voice, fake = backend(tmp_path)
    await voice.refresh(station(), LINES_C, "C")
    cached_wav_path(voice.cache, "EGLL").unlink()
    await voice.refresh(station(), LINES_C, "C")
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_tts_does_not_scrape_liveatc(tmp_path, monkeypatch):
    class Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("TTS must not make HTTP calls")

    monkeypatch.setattr(httpx, "AsyncClient", Boom)
    voice, fake = backend(
        tmp_path,
        scrape_url="https://www.liveatc.net/search/?icao=EGLL",
    )
    path = await voice.refresh(station(), LINES_C, "C")
    assert path is not None
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_disabled_or_none_backend_is_silent(tmp_path):
    voice, fake = backend(tmp_path, enabled=False)
    assert await voice.refresh(station(), LINES_C, "C") is None
    voice, fake = backend(tmp_path, backend_name="none")
    assert await voice.refresh(station(), LINES_C, "C") is None
    assert fake.calls == []


@pytest.mark.asyncio
async def test_file_backend_uses_dropped_wav(tmp_path):
    voice, fake = backend(tmp_path, backend_name="file")
    dest = cached_wav_path(voice.cache, "EGLL")
    write_pcm_wav(dest, b"\x00\x00" * 80)
    assert await voice.refresh(station(), LINES_C, "C") == dest
    assert fake.calls == []


@pytest.mark.asyncio
async def test_empty_atis_text_skips_tts(tmp_path):
    voice, fake = backend(tmp_path)
    assert await voice.refresh(station(), [], "C") is None
    assert fake.calls == []


@pytest.mark.asyncio
async def test_ogg_failure_still_returns_wav(tmp_path):
    def boom(_src: Path, _dest: Path) -> None:
        raise RuntimeError("ffmpeg missing")

    voice, fake = backend(tmp_path, transcode=boom)
    path = await voice.refresh(station(), LINES_C, "C")
    assert path is not None and path.is_file()
    assert not path.with_suffix(".ogg").exists()
    assert len(fake.calls) == 1


def test_example_config_does_not_enable_liveatc_scrape():
    example = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text())
    voice = example["plugins"]["atis"]["voice"]
    assert voice["scrape_url"] == ""
    assert voice["backend"] in {"none", "file", "tts"}
    text = (REPO_ROOT / "config.example.yaml").read_text().lower()
    assert "liveatc.net" not in text
    assert "http" not in voice["scrape_url"]
