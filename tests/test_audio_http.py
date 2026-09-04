"""Audio HTTP index: fake wav + fake synthesizer. No LiveATC, no admin login."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from openfsd_injector.airports import DEFAULT_ATIS_ICAOS
from openfsd_injector.atis.audio_http import AudioCatalog, AudioHttpServer
from openfsd_injector.atis.voice import VoiceBackend, cached_letter_path, write_pcm_wav
from openfsd_injector.config import StationConfig, VoiceConfig, load_config

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeSynthesizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    async def synthesize(self, text: str, dest_wav: Path) -> None:
        self.calls.append((text, dest_wav))
        write_pcm_wav(dest_wav, b"\x00\x00" * 160)


def _station(icao: str = "EGLL", frequency: float = 128.080) -> StationConfig:
    return StationConfig(
        icao=icao,
        name=f"{icao} Information",
        callsign=f"{icao}_ATIS",
        frequency=frequency,
        lat=51.4775,
        lon=-0.4614,
    )


LINES_C = ["HEATHROW INFORMATION C", "EGLL 031250Z 27008KT 9999 FEW030"]
LINES_D = ["HEATHROW INFORMATION D", "EGLL 031350Z 27010KT 9999 SCT025"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for var in (
        "AUDIO_HTTP",
        "AUDIO_HTTP_HOST",
        "AUDIO_HTTP_PORT",
        "AUDIO_HTTP_PUBLISH",
        "SRS_HOST",
        "SRS_PORT",
        "SRS_TX",
        "SRS_EAM_PASSWORD",
        "SRS_NAME",
        "SRS_COALITION",
        "ATIS_ICAOS",
        "VOICE_BACKEND",
        "OPENFSD_CID",
        "OPENFSD_PASSWORD",
        "OPENFSD_TOKEN",
        "OPENFSD_ALLOW_ADMINISTRATOR",
        "OPENFSD_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def _backend(tmp_path: Path) -> tuple[VoiceBackend, FakeSynthesizer]:
    fake = FakeSynthesizer()

    def _copy_ogg(src: Path, dest: Path) -> None:
        dest.write_bytes(src.read_bytes())

    voice = VoiceBackend(
        VoiceConfig(
            enabled=True,
            backend="tts",
            cache_dir=str(tmp_path / "cache"),
            loop_silence_seconds=0.0,
        ),
        synthesizer=fake,
        transcode=_copy_ogg,
    )
    return voice, fake


@pytest.mark.asyncio
async def test_index_and_wav_served_from_fake_cache(tmp_path):
    voice, fake = _backend(tmp_path)
    station = _station()
    catalog = AudioCatalog(voice.cache)
    catalog.register(station)

    path = await voice.refresh(station, LINES_C, "C")
    assert path is not None
    catalog.refresh_station(station, "C")

    server = AudioHttpServer(catalog, "127.0.0.1", 0)
    await server.start()
    try:
        base = f"http://127.0.0.1:{server.bound_port}"
        async with httpx.AsyncClient() as client:
            index = await client.get(f"{base}/atis/index.json")
            assert index.status_code == 200
            payload = index.json()
            assert payload["stations"][0]["icao"] == "EGLL"
            assert payload["stations"][0]["callsign"] == "EGLL_ATIS"
            assert payload["stations"][0]["frequency"] == 128.080
            assert payload["stations"][0]["letter"] == "C"
            assert payload["stations"][0]["audio_url"] == f"{base}/atis/egll.wav"
            assert payload["stations"][0]["ogg_url"] == f"{base}/atis/egll.ogg"

            wav = await client.get(f"{base}/atis/egll.wav")
            assert wav.status_code == 200
            assert wav.headers["content-type"].startswith("audio/wav")
            assert wav.content == path.read_bytes()

            ogg = await client.get(f"{base}/atis/EGLL.ogg")
            assert ogg.status_code == 200
            assert ogg.headers["content-type"].startswith("audio/ogg")

            missing = await client.get(f"{base}/atis/egkk.wav")
            assert missing.status_code == 404
            traversal = await client.get(f"{base}/atis/../egll.wav")
            assert traversal.status_code == 404
    finally:
        await server.stop()
    assert len(fake.calls) == 1
    assert (voice.cache / "index.json").is_file()


@pytest.mark.asyncio
async def test_index_refreshes_when_letter_changes(tmp_path):
    voice, fake = _backend(tmp_path)
    station = _station()
    catalog = AudioCatalog(voice.cache)
    catalog.register(station)
    await voice.refresh(station, LINES_C, "C")
    catalog.refresh_station(station, "C")
    assert catalog.index_payload("")["stations"][0]["letter"] == "C"

    await voice.refresh(station, LINES_D, "D")
    catalog.refresh_station(station, "D")
    payload = catalog.index_payload("")
    assert payload["stations"][0]["letter"] == "D"
    assert cached_letter_path(voice.cache, "EGLL").read_text() == "D"
    written = json.loads((voice.cache / "index.json").read_text())
    assert written["stations"][0]["letter"] == "D"
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_audio_http_does_not_scrape_liveatc_or_login(tmp_path, monkeypatch):
    class Boom:
        def __init__(self, *args, **kwargs):
            raise AssertionError("audio HTTP must not make outbound HTTP calls")

    monkeypatch.setattr(httpx, "AsyncClient", Boom)
    voice, fake = _backend(tmp_path)
    station = _station()
    catalog = AudioCatalog(voice.cache)
    catalog.register(station)
    await voice.refresh(station, LINES_C, "C")
    catalog.refresh_station(station, "C")

    dest = voice.cache / "egll.wav"
    write_pcm_wav(dest, b"\x00\x00" * 40)
    server = AudioHttpServer(catalog, "127.0.0.1", 0)
    await server.start()
    try:
        # Serve from the in-process server only — no LiveATC, no admin API.
        import asyncio
        from urllib.request import urlopen

        def _get(url: str) -> bytes:
            with urlopen(url, timeout=2) as resp:  # noqa: S310 - local test server
                return resp.read()

        body = await asyncio.to_thread(
            _get, f"http://127.0.0.1:{server.bound_port}/atis/index.json"
        )
        assert b"EGLL" in body
        assert b"liveatc" not in body.lower()
    finally:
        await server.stop()
    assert len(fake.calls) == 1


def test_load_config_audio_http_defaults_and_env(tmp_path, monkeypatch):
    config = {
        "server": {"host": "127.0.0.1", "port": 6809},
        "auth": {"cid": 999999, "password": "fake-password-not-a-real-credential"},
        "plugins": {
            "atis": {
                "stations": [
                    {
                        "icao": "EGLL",
                        "frequency": 128.080,
                        "lat": 51.4775,
                        "lon": -0.4614,
                    }
                ]
            }
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr("openfsd_injector.config.running_in_docker", lambda: False)
    cfg = load_config(path)
    assert cfg.atis.audio_http.enabled is True
    assert cfg.atis.audio_http.host == "127.0.0.1"
    assert cfg.atis.audio_http.port == 8091
    assert cfg.atis.audio_http.srs_host == ""
    assert cfg.atis.audio_http.srs_port == 5002
    assert cfg.atis.audio_http.srs_tx is True

    monkeypatch.setenv("AUDIO_HTTP_PUBLISH", "1")
    monkeypatch.setenv("AUDIO_HTTP_PORT", "8099")
    monkeypatch.setenv("SRS_HOST", "")
    cfg = load_config(path)
    assert cfg.atis.audio_http.host == "0.0.0.0"
    assert cfg.atis.audio_http.port == 8099


def test_docker_bind_is_all_interfaces_without_host_publish(tmp_path, monkeypatch):
    config = {
        "server": {"host": "127.0.0.1", "port": 6809},
        "auth": {"cid": 999999, "password": "fake-password-not-a-real-credential"},
        "plugins": {
            "atis": {
                "stations": [
                    {
                        "icao": "EGLL",
                        "frequency": 128.080,
                        "lat": 51.4775,
                        "lon": -0.4614,
                    }
                ]
            }
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setattr("openfsd_injector.config.running_in_docker", lambda: True)
    cfg = load_config(path)
    assert cfg.atis.audio_http.host == "0.0.0.0"
    assert cfg.atis.audio_http.enabled is True


def test_audio_http_can_be_disabled(tmp_path, monkeypatch):
    config = {
        "server": {"host": "127.0.0.1", "port": 6809},
        "auth": {"cid": 999999, "password": "fake-password-not-a-real-credential"},
        "plugins": {
            "atis": {
                "stations": [
                    {
                        "icao": "EGLL",
                        "frequency": 128.080,
                        "lat": 51.4775,
                        "lon": -0.4614,
                    }
                ]
            }
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    monkeypatch.setenv("AUDIO_HTTP", "0")
    cfg = load_config(path)
    assert cfg.atis.audio_http.enabled is False


def test_readme_documents_majors_list_and_audio_index():
    text = (REPO_ROOT / "README.md").read_text()
    assert "http://injector:8091/atis/index.json" in text
    assert DEFAULT_ATIS_ICAOS[0] == "EGLL"
    assert "KEWR" in text
    assert "LiveATC" in text
    assert "Administrator" in text
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert '127.0.0.1:8091:8091' in compose
    published = [
        line
        for line in compose.splitlines()
        if "8091:8091" in line and not line.lstrip().startswith("#")
    ]
    assert published == []
