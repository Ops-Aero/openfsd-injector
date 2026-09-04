"""SRS 2.x ATIS TX: freq mapping, TCP presence, UDP Opus path. No LiveATC, no AFV."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path

import pytest
import yaml

from openfsd_injector.airports import AIRPORTS
from openfsd_injector.atis.srs.bridge import SrsBridge
from openfsd_injector.atis.srs.client import SrsClient
from openfsd_injector.atis.srs.opus import (
    SRS_FRAME_BYTES,
    FakeOpusEncoder,
    encode_pcm_frames,
    opus_available,
)
from openfsd_injector.atis.srs.pcm import load_wav_pcm16_mono_16k
from openfsd_injector.atis.srs.protocol import (
    GUID_LENGTH,
    MSG_EAM_PASSWORD,
    MSG_PING,
    MSG_RADIO_UPDATE,
    MSG_SYNC,
    SRS_CLIENT_VERSION,
    VoiceFrequency,
    VoicePacket,
    build_sync_message,
    client_payload,
    decode_tcp_message,
    decode_voice_packet,
    encode_tcp_message,
    encode_voice_packet,
    hz_bits_match,
    new_guid,
    radio_payload,
)
from openfsd_injector.atis.voice import write_pcm_wav
from openfsd_injector.config import AppConfig, AtisPluginConfig, AudioHttpConfig, StationConfig, VoiceConfig, load_config
from openfsd_injector.plugins.atis import AtisPlugin
from openfsd_injector.protocol import encode_frequency, frequency_hz

REPO_ROOT = Path(__file__).resolve().parents[1]

# Ciribob UDPVoicePacketTests.EncodeInitialVoicePacket (2.4 layout, 79 bytes).
CIRIBOB_GUID = b"ufYS_WlLVkmFPjqCgxz6GA"
CIRIBOB_ENCODED = bytes(
    [
        79, 0,
        6, 0,
        10, 0,
        0, 1, 2, 3, 4, 5,
        0, 0, 0, 0, 0, 0, 89, 64,
        4,
        0,
        1, 0, 0, 0,
        1, 0, 0, 0, 0, 0, 0, 0,
        4,
        *CIRIBOB_GUID,
        *CIRIBOB_GUID,
    ]
)

ENV_VARS = (
    "SRS_HOST",
    "SRS_PORT",
    "SRS_TX",
    "SRS_EAM_PASSWORD",
    "SRS_NAME",
    "SRS_COALITION",
    "AUDIO_HTTP",
    "AUDIO_HTTP_HOST",
    "AUDIO_HTTP_PORT",
    "ATIS_ICAOS",
    "VOICE_BACKEND",
    "OPENFSD_CID",
    "OPENFSD_PASSWORD",
    "OPENFSD_TOKEN",
    "OPENFSD_CONFIG",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def _station(icao: str = "EGLL", frequency: float = 128.080) -> StationConfig:
    return StationConfig(
        icao=icao,
        name=f"{icao} Information",
        callsign=f"{icao}_ATIS",
        frequency=frequency,
        lat=51.4775,
        lon=-0.4614,
    )


def _app(tmp_path: Path, *, srs_host: str, srs_tx: bool = True, stations=None) -> AppConfig:
    cache = tmp_path / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    return AppConfig(
        atis=AtisPluginConfig(
            voice=VoiceConfig(cache_dir=str(cache)),
            audio_http=AudioHttpConfig(
                enabled=False,
                srs_host=srs_host,
                srs_port=5002,
                srs_tx=srs_tx,
                srs_name="OPSAERO_ATIS",
            ),
            stations=stations or [_station()],
        )
    )


class FakeSrsPeer:
    """In-process ciribob-shaped TCP+UDP peer. No real SRS server in CI."""

    def __init__(self) -> None:
        self.tcp_messages: list[dict] = []
        self.udp_packets: list[bytes] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None
        self._udp: socket.socket | None = None
        self._got_sync = asyncio.Event()
        self._got_voice = asyncio.Event()

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_tcp, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp.bind(("127.0.0.1", self.port))
        udp.setblocking(False)
        self._udp = udp
        asyncio.get_running_loop().add_reader(udp, self._on_udp)

    async def stop(self) -> None:
        if self._udp is not None:
            asyncio.get_running_loop().remove_reader(self._udp)
            self._udp.close()
            self._udp = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _on_udp(self) -> None:
        assert self._udp is not None
        try:
            data, _addr = self._udp.recvfrom(4096)
        except BlockingIOError:
            return
        self.udp_packets.append(data)
        if len(data) > GUID_LENGTH:
            self._got_voice.set()

    async def _handle_tcp(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            hello = {
                "Version": SRS_CLIENT_VERSION,
                "MsgType": 4,
                "ServerSettings": {
                    "EXTERNAL_AWACS_MODE": "false",
                    "COALITION_AUDIO_SECURITY": "false",
                },
            }
            writer.write(encode_tcp_message(hello))
            await writer.drain()
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = decode_tcp_message(line)
                self.tcp_messages.append(msg)
                if msg.get("MsgType") == MSG_SYNC:
                    self._got_sync.set()
                    writer.write(
                        encode_tcp_message(
                            {"Version": SRS_CLIENT_VERSION, "MsgType": MSG_SYNC, "Clients": []}
                        )
                    )
                    await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def test_frequency_hz_matches_every_airport_fsd_advert():
    for icao, info in AIRPORTS.items():
        hz = frequency_hz(info.frequency)
        assert hz == int(round(info.frequency * 1000)) * 1000
        encoded = encode_frequency(info.frequency)
        assert (100_000 + int(encoded)) * 1000 == hz, icao


def test_radio_payload_uses_integer_hz():
    radio = radio_payload(frequency_mhz=128.080, name="EGLL_ATIS")
    assert radio["freq"] == 128_080_000.0
    assert radio["modulation"] == 0
    assert radio["enc"] is False
    assert radio["name"] == "EGLL_ATIS"


def test_ciribob_24_voice_packet_golden():
    packet = VoicePacket(
        audio=bytes([0, 1, 2, 3, 4, 5]),
        frequencies=[VoiceFrequency(frequency_hz=100.0, modulation=4, encryption=0)],
        unit_id=1,
        packet_id=1,
        hops=4,
        relay_guid=CIRIBOB_GUID,
        origin_guid=CIRIBOB_GUID,
    )
    encoded = encode_voice_packet(packet)
    assert encoded == CIRIBOB_ENCODED
    decoded = decode_voice_packet(encoded)
    assert decoded.audio == bytes([0, 1, 2, 3, 4, 5])
    assert decoded.frequencies[0].frequency_hz == 100.0
    assert decoded.unit_id == 1
    assert decoded.packet_id == 1
    assert decoded.hops == 4


def test_voice_packet_roundtrip_atis_hz():
    guid = new_guid().encode("ascii")
    packet = VoicePacket(
        audio=b"\x01\x02\x03\x04",
        frequencies=[VoiceFrequency(frequency_hz=float(frequency_hz(128.080)))],
        unit_id=100000001,
        packet_id=9,
        hops=0,
        relay_guid=guid,
        origin_guid=guid,
    )
    decoded = decode_voice_packet(encode_voice_packet(packet))
    assert decoded.audio == packet.audio
    assert hz_bits_match(decoded.frequencies[0].frequency_hz, 128_080_000)
    assert decoded.packet_id == 9


def test_decode_rejects_malformed_voice_packets():
    guid = b"A" * GUID_LENGTH
    valid = encode_voice_packet(
        VoicePacket(
            audio=b"\x01\x02",
            frequencies=[VoiceFrequency(frequency_hz=128_080_000.0)],
            unit_id=1,
            packet_id=1,
            hops=0,
            relay_guid=guid,
            origin_guid=guid,
        )
    )
    with pytest.raises(ValueError):
        decode_voice_packet(valid[:20])
    damaged = bytearray(valid)
    damaged[0] = (len(valid) - 8) & 0xFF
    with pytest.raises(ValueError):
        decode_voice_packet(bytes(damaged))


def test_sync_message_omits_empty_eam_password():
    client = client_payload(guid="A" * 22, name="OPSAERO_ATIS", coalition=0, radios=[])
    raw = encode_tcp_message(build_sync_message(client))
    payload = json.loads(raw)
    assert payload["MsgType"] == MSG_SYNC
    assert payload["Version"] == SRS_CLIENT_VERSION
    assert "ExternalAWACSModePassword" not in payload
    assert payload["Client"]["Name"] == "OPSAERO_ATIS"


def test_fake_opus_encoder_is_not_claimed_as_libopus():
    """A test double must not make opus_available() lie."""
    encoder = FakeOpusEncoder()
    frames = encode_pcm_frames(b"\x00\x10" * (SRS_FRAME_BYTES // 2), encoder)
    assert frames
    assert frames[0].startswith(b"FAKEOPUS")
    # Availability is the real ctypes load, independent of the fake.
    assert isinstance(opus_available(), bool)


@pytest.mark.skipif(not opus_available(), reason="libopus not installed")
def test_libopus_encodes_a_real_frame():
    from openfsd_injector.atis.srs.opus import LibOpusEncoder

    enc = LibOpusEncoder()
    try:
        out = enc.encode_frame(b"\x00\x00" * (SRS_FRAME_BYTES // 2))
    finally:
        enc.close()
    assert out
    assert not out.startswith(b"FAKEOPUS")


def test_pcm_resample_8k_to_16k(tmp_path):
    path = tmp_path / "egll.wav"
    write_pcm_wav(path, b"\x00\x10" * 8000, rate=8000)
    pcm = load_wav_pcm16_mono_16k(path)
    assert len(pcm) == 16000 * 2


@pytest.mark.asyncio
async def test_empty_srs_host_does_not_connect(tmp_path):
    from openfsd_injector.atis.audio_http import AudioCatalog

    app = _app(tmp_path, srs_host="")
    bridge = SrsBridge(app, AudioCatalog(Path(app.atis.voice.cache_dir)))
    task = asyncio.create_task(bridge.run())
    await asyncio.sleep(0.05)
    assert bridge.client is None
    assert not bridge.status().connected
    assert "HTTP audio only" in bridge.status().reason
    await bridge.stop()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_bridge_survives_missing_srs_server(tmp_path):
    from openfsd_injector.atis.audio_http import AudioCatalog

    app = _app(tmp_path, srs_host="127.0.0.1")
    app.atis.audio_http.srs_port = 1  # nothing listens; connect must fail
    bridge = SrsBridge(
        app,
        AudioCatalog(Path(app.atis.voice.cache_dir)),
        reconnect_seconds=0.05,
    )
    task = asyncio.create_task(bridge.run())
    try:
        await asyncio.sleep(0.2)
        assert not bridge.status().connected
        assert not bridge.status().audio_path_live
        assert bridge.sessions == 0
    finally:
        await bridge.stop()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_plugin_starts_when_srs_is_down(tmp_path, monkeypatch):
    """SRS must never be required for the injector to stay up."""
    from openfsd_injector.plugins import atis as atis_module

    class IdleRuntime:
        def __init__(self, app, station, catalog=None, **_kwargs):
            self.station = station

        async def start(self):
            return None

        async def wait(self):
            await asyncio.Event().wait()

        async def stop(self):
            return None

    monkeypatch.setattr(atis_module, "StationRuntime", IdleRuntime)
    app = _app(tmp_path, srs_host="127.0.0.1")
    app.atis.audio_http.srs_port = 1
    app.auth.cid = 999999
    app.auth.password = "fake-password-not-a-real-credential"
    plugin = AtisPlugin(app)
    await plugin.start()
    try:
        await asyncio.sleep(0.1)
        assert plugin._srs is not None
        assert not plugin._srs.status().connected
        assert plugin._supervisors
    finally:
        await plugin.stop()


@pytest.mark.asyncio
async def test_tcp_sync_and_udp_tx_against_fake_peer(tmp_path):
    from openfsd_injector.atis.audio_http import AudioCatalog

    peer = FakeSrsPeer()
    await peer.start()
    try:
        cache = tmp_path / "cache"
        write_pcm_wav(cache / "egll.wav", b"\x10\x00" * 3200, rate=8000)
        write_pcm_wav(cache / "egkk.wav", b"\x20\x00" * 3200, rate=8000)
        app = _app(
            tmp_path,
            srs_host="127.0.0.1",
            stations=[_station("EGLL", 128.080), _station("EGKK", 136.525)],
        )
        app.atis.audio_http.srs_port = peer.port
        app.atis.voice.cache_dir = str(cache)
        catalog = AudioCatalog(cache)
        catalog.register(app.atis.stations[0])
        catalog.register(app.atis.stations[1])
        bridge = SrsBridge(
            app,
            catalog,
            encoder_factory=FakeOpusEncoder,
            reconnect_seconds=0.05,
        )
        task = asyncio.create_task(bridge.run())
        try:
            async with asyncio.timeout(5):
                await peer._got_sync.wait()
                await peer._got_voice.wait()
            syncs = [m for m in peer.tcp_messages if m.get("MsgType") == MSG_SYNC]
            assert syncs
            radios = syncs[0]["Client"]["RadioInfo"]["radios"]
            freqs = {int(r["freq"]) for r in radios}
            assert freqs == {128_080_000, 136_525_000}
            names = {r["name"] for r in radios}
            assert names == {"EGLL_ATIS", "EGKK_ATIS"}
            assert syncs[0]["Client"]["Name"] == "OPSAERO_ATIS"
            assert syncs[0]["Version"] == SRS_CLIENT_VERSION

            voice = [p for p in peer.udp_packets if len(p) > GUID_LENGTH]
            assert voice, "UDP Opus path must send framed voice, not just pings"
            decoded = decode_voice_packet(voice[0])
            assert decoded.audio.startswith(b"FAKEOPUS")
            hz = int(decoded.frequencies[0].frequency_hz)
            assert hz in {128_080_000, 136_525_000}
            status = bridge.status()
            assert status.connected
            assert status.tx_enabled
            assert status.opus_available
            assert status.audio_path_live
            assert "transmitting" in status.reason
        finally:
            await bridge.stop()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_srs_tx_off_is_tcp_only(tmp_path):
    from openfsd_injector.atis.audio_http import AudioCatalog

    peer = FakeSrsPeer()
    await peer.start()
    try:
        cache = tmp_path / "cache"
        write_pcm_wav(cache / "egll.wav", b"\x10\x00" * 1600, rate=8000)
        app = _app(tmp_path, srs_host="127.0.0.1", srs_tx=False)
        app.atis.audio_http.srs_port = peer.port
        app.atis.voice.cache_dir = str(cache)
        bridge = SrsBridge(
            app,
            AudioCatalog(cache),
            encoder_factory=FakeOpusEncoder,
            reconnect_seconds=0.05,
        )
        task = asyncio.create_task(bridge.run())
        try:
            async with asyncio.timeout(5):
                await peer._got_sync.wait()
            await asyncio.sleep(0.15)
            voice = [p for p in peer.udp_packets if len(p) > GUID_LENGTH]
            assert voice == []
            assert any(m.get("MsgType") == MSG_SYNC for m in peer.tcp_messages)
            radios = peer.tcp_messages[0]["Client"]["RadioInfo"]["radios"]
            assert radios[0]["freq"] == 128_080_000.0
            status = bridge.status()
            assert status.connected
            assert status.tx_enabled is False
            assert status.audio_path_live is False
            assert "SRS_TX=0" in status.reason
        finally:
            await bridge.stop()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_wav_appearing_registers_radio(tmp_path):
    from openfsd_injector.atis.audio_http import AudioCatalog

    peer = FakeSrsPeer()
    await peer.start()
    try:
        cache = tmp_path / "cache"
        cache.mkdir()
        app = _app(tmp_path, srs_host="127.0.0.1")
        app.atis.audio_http.srs_port = peer.port
        app.atis.voice.cache_dir = str(cache)
        bridge = SrsBridge(
            app,
            AudioCatalog(cache),
            encoder_factory=FakeOpusEncoder,
            reconnect_seconds=0.05,
        )
        task = asyncio.create_task(bridge.run())
        try:
            async with asyncio.timeout(5):
                await peer._got_sync.wait()
            syncs = [m for m in peer.tcp_messages if m.get("MsgType") == MSG_SYNC]
            assert syncs[0]["Client"]["RadioInfo"]["radios"] == []
            write_pcm_wav(cache / "egll.wav", b"\x10\x00" * 1600, rate=8000)
            async with asyncio.timeout(5):
                while not any(m.get("MsgType") == MSG_RADIO_UPDATE for m in peer.tcp_messages):
                    await asyncio.sleep(0.05)
            updates = [m for m in peer.tcp_messages if m.get("MsgType") == MSG_RADIO_UPDATE]
            assert updates[-1]["Client"]["RadioInfo"]["radios"][0]["freq"] == 128_080_000.0
        finally:
            await bridge.stop()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_eam_password_is_sent_when_configured(tmp_path, monkeypatch):
    from openfsd_injector.atis.audio_http import AudioCatalog

    peer = FakeSrsPeer()
    await peer.start()
    try:
        app = _app(tmp_path, srs_host="127.0.0.1")
        app.atis.audio_http.srs_port = peer.port
        app.atis.audio_http.srs_eam_password = "not-a-real-eam-secret"
        app.atis.audio_http.srs_tx = False
        bridge = SrsBridge(app, AudioCatalog(Path(app.atis.voice.cache_dir)))
        task = asyncio.create_task(bridge.run())
        try:
            async with asyncio.timeout(5):
                await peer._got_sync.wait()
            async with asyncio.timeout(2):
                while not any(m.get("MsgType") == MSG_EAM_PASSWORD for m in peer.tcp_messages):
                    await asyncio.sleep(0.02)
            eam = [m for m in peer.tcp_messages if m.get("MsgType") == MSG_EAM_PASSWORD][0]
            assert eam["ExternalAWACSModePassword"] == "not-a-real-eam-secret"
        finally:
            await bridge.stop()
            await asyncio.gather(task, return_exceptions=True)
    finally:
        await peer.stop()


@pytest.mark.asyncio
async def test_client_ping_is_guid_on_udp(tmp_path):
    peer = FakeSrsPeer()
    await peer.start()
    try:
        client = SrsClient("127.0.0.1", peer.port, guid="B" * 22)
        await client.connect()
        try:
            await client.sync([])
            await client.ping()
            async with asyncio.timeout(2):
                while not any(len(p) == GUID_LENGTH for p in peer.udp_packets):
                    await asyncio.sleep(0.02)
            assert b"B" * 22 in peer.udp_packets
            assert any(m.get("MsgType") == MSG_PING for m in peer.tcp_messages)
        finally:
            await client.close()
    finally:
        await peer.stop()


def test_status_marks_opus_unavailable(tmp_path, monkeypatch):
    from openfsd_injector.atis.audio_http import AudioCatalog

    monkeypatch.setattr("openfsd_injector.atis.srs.bridge.opus_available", lambda: False)
    app = _app(tmp_path, srs_host="srs")
    bridge = SrsBridge(app, AudioCatalog(Path(app.atis.voice.cache_dir)))
    # Pretend TCP came up so the reason is about TX, not connect.
    bridge._connected = True
    status = bridge.status()
    assert status.tx_enabled is True
    assert status.opus_available is False
    assert status.audio_path_live is False
    assert "libopus unavailable" in status.reason


def test_load_config_srs_env(tmp_path, monkeypatch):
    config = {
        "server": {"host": "127.0.0.1", "port": 6809},
        "auth": {"cid": 999999, "password": "fake-password-not-a-real-credential"},
        "plugins": {
            "atis": {
                "stations": [
                    {"icao": "EGLL", "frequency": 128.080, "lat": 51.4775, "lon": -0.4614}
                ]
            }
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    cfg = load_config(path)
    assert cfg.atis.audio_http.srs_host == ""
    assert cfg.atis.audio_http.srs_port == 5002
    assert cfg.atis.audio_http.srs_tx is True
    assert cfg.atis.audio_http.srs_name == "OPSAERO_ATIS"
    assert cfg.atis.audio_http.srs_coalition == 0
    assert cfg.atis.audio_http.srs_eam_password == ""

    monkeypatch.setenv("SRS_HOST", "srs")
    monkeypatch.setenv("SRS_PORT", "5002")
    monkeypatch.setenv("SRS_TX", "0")
    monkeypatch.setenv("SRS_NAME", "OPSAERO_ATIS")
    monkeypatch.setenv("SRS_COALITION", "0")
    monkeypatch.setenv("SRS_EAM_PASSWORD", "from-env-only")
    cfg = load_config(path)
    assert cfg.atis.audio_http.srs_host == "srs"
    assert cfg.atis.audio_http.srs_port == 5002
    assert cfg.atis.audio_http.srs_tx is False
    assert cfg.atis.audio_http.srs_eam_password == "from-env-only"


def test_eam_password_is_not_read_from_yaml(tmp_path):
    config = {
        "server": {"host": "127.0.0.1", "port": 6809},
        "auth": {"cid": 999999, "password": "fake-password-not-a-real-credential"},
        "plugins": {
            "atis": {
                "audio_http": {"srs_eam_password": "must-not-load-from-yaml"},
                "stations": [
                    {"icao": "EGLL", "frequency": 128.080, "lat": 51.4775, "lon": -0.4614}
                ],
            }
        },
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    cfg = load_config(path)
    assert cfg.atis.audio_http.srs_eam_password == ""


def test_readme_and_env_document_srs():
    readme = (REPO_ROOT / "README.md").read_text()
    env = (REPO_ROOT / ".env.example").read_text()
    assert "SRS_HOST" in readme
    assert "SRS_PORT" in readme
    assert "SRS_HOST=srs" in readme
    assert "flisher/dcs-srs-server:ciribob-2.4.0.0" in readme
    assert "OPSAERO_ATIS" in readme
    assert "SRS_TX=0" in readme
    assert "DCS-SR-ExternalAudio.exe" in readme
    assert "LiveATC" in readme
    assert "AFV" in readme
    assert "SRS_HOST=" in env
    assert "SRS_PORT=5002" in env
    assert "SRS_EAM_PASSWORD=" in env
    # Example must not ship a filled-in EAM password.
    eam_line = next(line for line in env.splitlines() if "SRS_EAM_PASSWORD=" in line)
    assert eam_line.lstrip().startswith("#")
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "SRS_HOST" in compose
