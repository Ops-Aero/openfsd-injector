"""Startup validation: bad or missing config must fail fast with clear errors."""

import copy
from pathlib import Path

import pytest
import yaml

from openfsd_injector.config import ConfigError, MissingCredentialError, load_config

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
    "OPENFSD_CONFIG",
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
