"""Startup validation: bad or missing config must fail fast with clear errors."""

import copy
from pathlib import Path

import pytest
import yaml

from openfsd_injector.airports import DEFAULT_ATIS_ICAOS, DEFAULT_ATIS_ICAOS_CSV
from openfsd_injector.config import (
    ADMINISTRATOR_RATING,
    ADMINISTRATOR_RATING_MESSAGE,
    ConfigError,
    MissingCredentialError,
    load_config,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Obviously fake placeholder. Never put a real credential in a fixture.
FAKE_PASSWORD = "fake-password-not-a-real-credential"

BASE_CONFIG = {
    "server": {"host": "127.0.0.1", "port": 6809, "api_base": "http://127.0.0.1:8010"},
    "auth": {"cid": 999999, "password": FAKE_PASSWORD, "rating": 3},
    "injector": {"reconnect_seconds": 5, "position_interval_seconds": 5},
    "plugins": {
        "atis": {
            "enabled": True,
            "refresh_seconds": 3600,
            "stations": [
                {
                    "icao": "EGLL",
                    "callsign": "EGLL_ATIS",
                    "frequency": 128.080,
                    "lat": 51.4775,
                    "lon": -0.4614,
                }
            ],
        }
    },
}

ENV_VARS = (
    "OPENFSD_HOST",
    "OPENFSD_PORT",
    "OPENFSD_API_BASE",
    "OPENFSD_CID",
    "OPENFSD_PASSWORD",
    "OPENFSD_TOKEN",
    "OPENFSD_ALLOW_ADMINISTRATOR",
    "OPENFSD_CONFIG",
    "ATIS_ICAOS",
    "VOICE_BACKEND",
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
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """No ambient OPENFSD_* env and no developer .env leaking into a test."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)


def write_config(tmp_path: Path, mutate=None) -> Path:
    data = copy.deepcopy(BASE_CONFIG)
    if mutate is not None:
        mutate(data)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


def station(data: dict) -> dict:
    return data["plugins"]["atis"]["stations"][0]


def test_valid_config_loads(tmp_path):
    cfg = load_config(write_config(tmp_path))
    assert cfg.server.port == 6809
    assert cfg.auth.password == FAKE_PASSWORD
    assert len(cfg.atis.stations) == 1
    assert cfg.atis.stations[0].callsign == "EGLL_ATIS"
    assert cfg.atis.reply_rate_limit > 0


def test_missing_credential_fails_fast(tmp_path):
    def mutate(data):
        data["auth"].pop("password")

    with pytest.raises(ConfigError, match="no injector credential"):
        load_config(write_config(tmp_path, mutate))


def test_missing_credential_error_names_the_env_vars(tmp_path):
    def mutate(data):
        data["auth"]["password"] = ""

    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, mutate))
    assert "OPENFSD_PASSWORD" in str(exc.value)
    assert "OPENFSD_TOKEN" in str(exc.value)


def test_env_credential_satisfies_requirement(tmp_path, monkeypatch):
    def mutate(data):
        data["auth"].pop("password")

    path = write_config(tmp_path, mutate)
    monkeypatch.setenv("OPENFSD_PASSWORD", FAKE_PASSWORD)
    cfg = load_config(path)
    assert cfg.auth.password == FAKE_PASSWORD


def test_env_token_satisfies_requirement(tmp_path, monkeypatch):
    def mutate(data):
        data["auth"].pop("password")

    path = write_config(tmp_path, mutate)
    monkeypatch.setenv("OPENFSD_TOKEN", "fake.jwt.value")
    assert load_config(path).auth.token == "fake.jwt.value"


def test_missing_station_key_is_reported(tmp_path):
    def mutate(data):
        station(data).pop("lat")

    with pytest.raises(ConfigError, match=r"stations\[0\] is missing required key\(s\): lat"):
        load_config(write_config(tmp_path, mutate))


def test_non_numeric_station_value_is_reported(tmp_path):
    def mutate(data):
        station(data)["lat"] = "fifty-one"

    with pytest.raises(ConfigError, match=r"stations\[0\]\.lat must be a number"):
        load_config(write_config(tmp_path, mutate))


def test_non_integer_cid_placeholder_is_reported(tmp_path):
    def mutate(data):
        data["auth"]["cid"] = "replace-with-your-own-cid"

    with pytest.raises(ConfigError, match="auth.cid must be an integer"):
        load_config(write_config(tmp_path, mutate))


@pytest.mark.parametrize("lat", [90.5, -91.0])
def test_latitude_range(tmp_path, lat):
    def mutate(data):
        station(data)["lat"] = lat

    with pytest.raises(ConfigError, match=r"lat must be between -90 and 90"):
        load_config(write_config(tmp_path, mutate))


@pytest.mark.parametrize("lon", [180.1, -180.5])
def test_longitude_range(tmp_path, lon):
    def mutate(data):
        station(data)["lon"] = lon

    with pytest.raises(ConfigError, match=r"lon must be between -180 and 180"):
        load_config(write_config(tmp_path, mutate))


@pytest.mark.parametrize("freq", [99.5, 428.5, 0.0])
def test_frequency_must_be_encodable(tmp_path, freq):
    def mutate(data):
        station(data)["frequency"] = freq

    with pytest.raises(ConfigError, match="frequency is unusable"):
        load_config(write_config(tmp_path, mutate))


def test_duplicate_callsigns_rejected(tmp_path):
    def mutate(data):
        stations = data["plugins"]["atis"]["stations"]
        second = copy.deepcopy(stations[0])
        second["icao"] = "EGKK"
        second["callsign"] = "egll_atis"
        stations.append(second)

    with pytest.raises(ConfigError, match="duplicates"):
        load_config(write_config(tmp_path, mutate))


@pytest.mark.parametrize(
    "section,key",
    [
        ("injector", "reconnect_seconds"),
        ("injector", "position_interval_seconds"),
    ],
)
def test_timers_must_be_positive(tmp_path, section, key):
    def mutate(data):
        data[section][key] = 0

    with pytest.raises(ConfigError, match=f"{section}.{key} must be > 0"):
        load_config(write_config(tmp_path, mutate))


def test_refresh_and_rate_limit_must_be_positive(tmp_path):
    def mutate(data):
        data["plugins"]["atis"]["refresh_seconds"] = 0
        data["plugins"]["atis"]["reply_rate_limit"] = 0
        data["plugins"]["atis"]["reply_rate_window_seconds"] = 0

    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, mutate))
    message = str(exc.value)
    assert "refresh_seconds must be > 0" in message
    assert "reply_rate_limit must be > 0" in message
    assert "reply_rate_window_seconds must be > 0" in message


def test_facility_type_and_rating_sanity(tmp_path):
    def mutate(data):
        data["auth"]["rating"] = 99
        station(data)["facility_type"] = 9

    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, mutate))
    message = str(exc.value)
    assert "auth.rating must be between 1 and 12" in message
    assert "facility_type must be between 0 and 6" in message


def test_bad_port_and_icao_reported_together(tmp_path):
    def mutate(data):
        data["server"]["port"] = 0
        station(data)["icao"] = "EG"

    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, mutate))
    message = str(exc.value)
    assert "server.port must be between 1 and 65535" in message
    assert "icao must be a 4-letter ICAO code" in message


def test_shipped_example_config_carries_no_credential(tmp_path):
    """config.example.yaml must never ship a usable account."""
    example = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text())
    assert example["auth"]["password"] == ""
    assert example["auth"]["token"] == ""
    assert example["auth"]["cid"] == 0

    with pytest.raises(MissingCredentialError):
        load_config(REPO_ROOT / "config.example.yaml")


def test_shipped_example_uses_dedicated_atis_identity_not_admin():
    """Examples must recommend S2/C1 ATIS identity, never CID 1 / Administrator."""
    example = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text())
    assert example["auth"]["cid"] == 0
    assert example["auth"]["rating"] == 3
    assert example["auth"]["rating"] <= 5
    assert example["auth"]["rating"] != ADMINISTRATOR_RATING
    text = (REPO_ROOT / "config.example.yaml").read_text()
    assert "cid: 1" not in text
    assert "CID 1" in text  # mentioned only as something to avoid
    env = (REPO_ROOT / ".env.example").read_text()
    assert "OPENFSD_CID=1" not in env
    assert env.split("OPENFSD_CID=", 1)[1].splitlines()[0].startswith("replace-with-your-own")


def test_default_rating_is_s2_not_administrator(tmp_path):
    def mutate(data):
        data["auth"].pop("rating", None)

    cfg = load_config(write_config(tmp_path, mutate))
    assert cfg.auth.rating == 3
    assert cfg.auth.allow_administrator is False


def test_administrator_rating_fails_fast(tmp_path):
    def mutate(data):
        data["auth"]["rating"] = ADMINISTRATOR_RATING

    with pytest.raises(ConfigError, match="Administrator") as exc:
        load_config(write_config(tmp_path, mutate))
    assert "OPENFSD_ALLOW_ADMINISTRATOR" in str(exc.value)
    assert ADMINISTRATOR_RATING_MESSAGE in str(exc.value)


def test_administrator_rating_allowed_with_config_flag(tmp_path):
    def mutate(data):
        data["auth"]["rating"] = ADMINISTRATOR_RATING
        data["auth"]["allow_administrator"] = True

    cfg = load_config(write_config(tmp_path, mutate))
    assert cfg.auth.rating == ADMINISTRATOR_RATING
    assert cfg.auth.allow_administrator is True


def test_administrator_rating_allowed_with_env_flag(tmp_path, monkeypatch):
    def mutate(data):
        data["auth"]["rating"] = ADMINISTRATOR_RATING

    path = write_config(tmp_path, mutate)
    monkeypatch.setenv("OPENFSD_ALLOW_ADMINISTRATOR", "1")
    cfg = load_config(path)
    assert cfg.auth.allow_administrator is True


def test_piper_engine_requires_model(tmp_path):
    def mutate(data):
        data["plugins"]["atis"]["voice"] = {
            "enabled": True,
            "backend": "tts",
            "engine": "piper",
        }

    with pytest.raises(ConfigError, match="piper_model"):
        load_config(write_config(tmp_path, mutate))


def test_c1_rating_is_allowed_without_override(tmp_path):
    def mutate(data):
        data["auth"]["rating"] = 5

    assert load_config(write_config(tmp_path, mutate)).auth.rating == 5


def test_atis_icaos_expands_full_default_list(tmp_path, monkeypatch):
    def mutate(data):
        data["plugins"]["atis"]["stations"] = []

    path = write_config(tmp_path, mutate)
    monkeypatch.setenv("ATIS_ICAOS", DEFAULT_ATIS_ICAOS_CSV)
    cfg = load_config(path)
    assert [st.icao for st in cfg.atis.stations] == list(DEFAULT_ATIS_ICAOS)
    assert all(st.callsign == f"{st.icao}_ATIS" for st in cfg.atis.stations)
    assert len(cfg.atis.stations) == len(DEFAULT_ATIS_ICAOS)
    assert cfg.auth.rating != ADMINISTRATOR_RATING


def test_atis_icaos_expands_when_stations_missing(tmp_path, monkeypatch):
    def mutate(data):
        data["plugins"]["atis"]["stations"] = []

    path = write_config(tmp_path, mutate)
    monkeypatch.setenv("ATIS_ICAOS", "EGLL, EGKK")
    cfg = load_config(path)
    assert [st.icao for st in cfg.atis.stations] == ["EGLL", "EGKK"]
    assert cfg.atis.stations[0].callsign == "EGLL_ATIS"
    assert cfg.atis.stations[0].lat == 51.4775
    assert cfg.atis.stations[0].lon == -0.4614
    assert cfg.atis.stations[1].callsign == "EGKK_ATIS"
    assert cfg.atis.stations[1].lat == 51.1481
    assert cfg.atis.stations[1].frequency == 136.525


def test_config_yaml_stations_override_atis_icaos(tmp_path, monkeypatch):
    monkeypatch.setenv("ATIS_ICAOS", "EGKK")
    cfg = load_config(write_config(tmp_path))
    assert [st.icao for st in cfg.atis.stations] == ["EGLL"]
    assert cfg.atis.stations[0].callsign == "EGLL_ATIS"


def test_unknown_icao_in_atis_icaos_fails(tmp_path, monkeypatch):
    def mutate(data):
        data["plugins"]["atis"].pop("stations")

    path = write_config(tmp_path, mutate)
    monkeypatch.setenv("ATIS_ICAOS", "EGLL,ZZZZ")
    with pytest.raises(ConfigError, match="ZZZZ"):
        load_config(path)


def test_no_stations_and_no_atis_icaos_fails(tmp_path):
    def mutate(data):
        data["plugins"]["atis"]["stations"] = []

    with pytest.raises(ConfigError, match="ATIS_ICAOS"):
        load_config(write_config(tmp_path, mutate))


def test_env_only_bringup_without_config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFSD_CID", "999999")
    monkeypatch.setenv("OPENFSD_PASSWORD", FAKE_PASSWORD)
    monkeypatch.setenv("ATIS_ICAOS", "EGLL,EGKK")
    cfg = load_config(tmp_path / "does-not-exist.yaml")
    assert [st.icao for st in cfg.atis.stations] == ["EGLL", "EGKK"]
    assert cfg.auth.rating == 3
    assert cfg.auth.allow_administrator is False
    assert cfg.atis.voice.backend == "none"
    assert cfg.atis.voice.enabled is False


def test_voice_backend_tts_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("VOICE_BACKEND", "tts")
    cfg = load_config(write_config(tmp_path))
    assert cfg.atis.voice.backend == "tts"
    assert cfg.atis.voice.enabled is True


def test_voice_backend_defaults_to_tts_in_docker_when_unset(tmp_path, monkeypatch):
    def mutate(data):
        data["plugins"]["atis"].pop("voice", None)

    monkeypatch.setattr("openfsd_injector.config.running_in_docker", lambda: True)
    cfg = load_config(write_config(tmp_path, mutate))
    assert cfg.atis.voice.backend == "tts"
    assert cfg.atis.voice.enabled is True


def test_voice_backend_stays_none_outside_docker_when_unset(tmp_path, monkeypatch):
    def mutate(data):
        data["plugins"]["atis"].pop("voice", None)

    monkeypatch.setattr("openfsd_injector.config.running_in_docker", lambda: False)
    cfg = load_config(write_config(tmp_path, mutate))
    assert cfg.atis.voice.backend == "none"
    assert cfg.atis.voice.enabled is False


def test_voice_backend_yaml_overrides_docker_default(tmp_path, monkeypatch):
    def mutate(data):
        data["plugins"]["atis"]["voice"] = {"backend": "none", "enabled": False}

    monkeypatch.setattr("openfsd_injector.config.running_in_docker", lambda: True)
    cfg = load_config(write_config(tmp_path, mutate))
    assert cfg.atis.voice.backend == "none"
    assert cfg.atis.voice.enabled is False


def test_voice_backend_env_none_in_docker(tmp_path, monkeypatch):
    monkeypatch.setattr("openfsd_injector.config.running_in_docker", lambda: True)
    monkeypatch.setenv("VOICE_BACKEND", "none")
    cfg = load_config(write_config(tmp_path))
    assert cfg.atis.voice.backend == "none"
    assert cfg.atis.voice.enabled is False


def test_administrator_still_fails_on_env_only_path(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENFSD_CID", "999999")
    monkeypatch.setenv("OPENFSD_PASSWORD", FAKE_PASSWORD)
    monkeypatch.setenv("ATIS_ICAOS", "EGLL")
    path = tmp_path / "env-only.yaml"
    path.write_text("auth:\n  rating: 12\n")
    with pytest.raises(ConfigError, match="Administrator") as exc:
        load_config(path)
    assert "OPENFSD_ALLOW_ADMINISTRATOR" in str(exc.value)
    assert ADMINISTRATOR_RATING_MESSAGE in str(exc.value)


def test_shipped_example_uses_empty_stations_for_env_expansion():
    example = yaml.safe_load((REPO_ROOT / "config.example.yaml").read_text())
    assert example["plugins"]["atis"]["stations"] == []
    env = (REPO_ROOT / ".env.example").read_text()
    assert f"ATIS_ICAOS={DEFAULT_ATIS_ICAOS_CSV}" in env
    assert parse_env_atis_icaos(env) == list(DEFAULT_ATIS_ICAOS)
    assert "VOICE_BACKEND" in env
    assert "AUDIO_HTTP_PORT=8091" in env
    assert "injector:8091/atis/index.json" in env or "8091" in env


def parse_env_atis_icaos(text: str) -> list[str]:
    for line in text.splitlines():
        if line.startswith("ATIS_ICAOS="):
            return [part.strip() for part in line.split("=", 1)[1].split(",") if part.strip()]
    raise AssertionError("ATIS_ICAOS missing from .env.example")


def test_shipped_env_example_carries_no_credential():
    """.env.example must only contain replace-me placeholders."""
    text = (REPO_ROOT / ".env.example").read_text()
    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1].strip()
        for line in text.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert values["OPENFSD_CID"].startswith("replace-with-your-own")
    assert values["OPENFSD_PASSWORD"].startswith("replace-with-your-own")
    assert not (REPO_ROOT / "docker.env").exists()
    compose = (REPO_ROOT / "docker-compose.yml").read_text()
    assert "env_file:" in compose
    assert "- docker.env" not in compose
    assert "- .env" in compose
    assert "host.docker.internal:host-gateway" in compose
    assert "name: opsaero" in compose
    assert "OPENFSD_HOST=fsd" in compose
    assert "http://fsdweb:8010" in compose
    assert "SRS_HOST" in compose


def test_srs_port_must_be_in_range(tmp_path):
    def mutate(data):
        data["plugins"]["atis"]["audio_http"] = {"srs_port": 0}

    with pytest.raises(ConfigError, match="srs_port"):
        load_config(write_config(tmp_path, mutate))


def test_srs_coalition_must_be_0_1_or_2(tmp_path):
    def mutate(data):
        data["plugins"]["atis"]["audio_http"] = {"srs_coalition": 4}

    with pytest.raises(ConfigError, match="srs_coalition"):
        load_config(write_config(tmp_path, mutate))
