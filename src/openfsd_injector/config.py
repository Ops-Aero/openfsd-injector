from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .protocol import encode_frequency


class ConfigError(ValueError):
    """Configuration is missing, malformed, or out of range."""


class MissingCredentialError(ConfigError):
    """No injector credential was configured and there is no built-in default."""


MISSING_CREDENTIAL_MESSAGE = (
    "no injector credential is configured: set auth.password or auth.token "
    "(or the OPENFSD_PASSWORD / OPENFSD_TOKEN environment variables). "
    "openfsd-injector ships no default credential"
)


REQUIRED_STATION_KEYS = ("icao", "frequency", "lat", "lon")
SUPPORTED_PROTOCOL_REVISIONS = (100, 101)
# openFSD/VATSIM facility types: 0=OBS 1=FSS 2=DEL 3=GND 4=TWR 5=APP 6=CTR
MAX_FACILITY_TYPE = 6
# 1=OBS 2=S1 3=S2 4=S3 5=C1 6=C2 7=C3 8=I1 9=I2 10=I3 11=SUP 12=ADM
MIN_RATING = 1
MAX_RATING = 12
MAX_VIS_RANGE_NM = 1500


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 6809
    api_base: str = ""


@dataclass
class AuthConfig:
    cid: int = 0
    password: str = ""
    token: str = ""
    real_name: str = "ATIS Bot"
    rating: int = 5
    protocol_revision: int = 100
    client_id: str = "0f5d"
    client_name: str = "openfsd-injector"
    client_major: int = 0
    client_minor: int = 1


@dataclass
class InjectorConfig:
    reconnect_seconds: float = 5.0
    position_interval_seconds: float = 5.0


@dataclass
class VoiceConfig:
    enabled: bool = False
    backend: str = "none"
    scrape_url: str = ""
    cache_dir: str = "audio/cache"


@dataclass
class StationConfig:
    icao: str = ""
    name: str = ""
    callsign: str = ""
    frequency: float = 0.0
    lat: float = 0.0
    lon: float = 0.0
    vis_range_nm: int = 50
    facility_type: int = 4
    runways_dep: str = ""
    runways_arr: str = ""
    approach: str = "ILS"
    extra_lines: list[str] = field(default_factory=list)


@dataclass
class AtisPluginConfig:
    enabled: bool = True
    refresh_seconds: int = 3600
    metar_url: str = "https://aviationweather.gov/api/data/metar?ids={icao}&format=raw"
    # Cap on $CQ ATIS replies served to one requester per window, so a single
    # peer cannot use the station as an outbound amplifier.
    reply_rate_limit: int = 6
    reply_rate_window_seconds: float = 60.0
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    stations: list[StationConfig] = field(default_factory=list)


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    injector: InjectorConfig = field(default_factory=InjectorConfig)
    atis: AtisPluginConfig = field(default_factory=AtisPluginConfig)
    raw: dict[str, Any] = field(default_factory=dict)


def _merge(dst: dict, src: dict) -> dict:
    out = dict(dst)
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping, got {type(value).__name__}")
    return value


def _as_int(value: Any, where: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} must be an integer, got {value!r}") from None


def _as_float(value: Any, where: str) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{where} must be a number, got {value!r}") from None


def _station_from_row(row: Any, index: int) -> StationConfig:
    where = f"plugins.atis.stations[{index}]"
    if not isinstance(row, dict):
        raise ConfigError(f"{where} must be a mapping, got {type(row).__name__}")
    missing = [key for key in REQUIRED_STATION_KEYS if row.get(key) in (None, "")]
    if missing:
        raise ConfigError(f"{where} is missing required key(s): {', '.join(missing)}")
    icao = str(row["icao"]).upper()
    return StationConfig(
        icao=icao,
        name=str(row.get("name") or f"{icao} Information"),
        callsign=str(row.get("callsign") or f"{icao}_ATIS"),
        frequency=_as_float(row["frequency"], f"{where}.frequency"),
        lat=_as_float(row["lat"], f"{where}.lat"),
        lon=_as_float(row["lon"], f"{where}.lon"),
        vis_range_nm=_as_int(row.get("vis_range_nm", 50), f"{where}.vis_range_nm"),
        facility_type=_as_int(row.get("facility_type", 4), f"{where}.facility_type"),
        runways_dep=str(row.get("runways_dep", "")),
        runways_arr=str(row.get("runways_arr", "")),
        approach=str(row.get("approach", "ILS")),
        extra_lines=[str(line) for line in (row.get("extra_lines") or [])],
    )


def validate_config(cfg: AppConfig) -> None:
    """Fail fast on unusable configuration, listing every problem at once."""
    problems: list[str] = []

    if not cfg.server.host.strip():
        problems.append("server.host must not be empty")
    if not 1 <= cfg.server.port <= 65535:
        problems.append(f"server.port must be between 1 and 65535, got {cfg.server.port}")
    if cfg.server.api_base and not cfg.server.api_base.startswith(("http://", "https://")):
        problems.append(f"server.api_base must be an http(s) URL, got {cfg.server.api_base!r}")

    missing_credential = not cfg.auth.token and not cfg.auth.password
    if missing_credential:
        problems.append(MISSING_CREDENTIAL_MESSAGE)
    if cfg.auth.cid <= 0:
        problems.append(
            f"auth.cid must be the positive CID of your own openFSD account, got {cfg.auth.cid}"
        )
    if not MIN_RATING <= cfg.auth.rating <= MAX_RATING:
        problems.append(
            f"auth.rating must be between {MIN_RATING} and {MAX_RATING}, got {cfg.auth.rating}"
        )
    if cfg.auth.protocol_revision not in SUPPORTED_PROTOCOL_REVISIONS:
        problems.append(
            "auth.protocol_revision must be one of "
            f"{', '.join(str(r) for r in SUPPORTED_PROTOCOL_REVISIONS)}, "
            f"got {cfg.auth.protocol_revision}"
        )
    if not cfg.auth.real_name.strip():
        problems.append("auth.real_name must not be empty")

    if cfg.injector.reconnect_seconds <= 0:
        problems.append(
            f"injector.reconnect_seconds must be > 0, got {cfg.injector.reconnect_seconds}"
        )
    if cfg.injector.position_interval_seconds <= 0:
        problems.append(
            "injector.position_interval_seconds must be > 0, got "
            f"{cfg.injector.position_interval_seconds}"
        )

    if cfg.atis.enabled:
        if cfg.atis.refresh_seconds <= 0:
            problems.append(
                f"plugins.atis.refresh_seconds must be > 0, got {cfg.atis.refresh_seconds}"
            )
        if "{icao}" not in cfg.atis.metar_url:
            problems.append("plugins.atis.metar_url must contain the '{icao}' placeholder")
        if cfg.atis.reply_rate_limit <= 0:
            problems.append(
                f"plugins.atis.reply_rate_limit must be > 0, got {cfg.atis.reply_rate_limit}"
            )
        if cfg.atis.reply_rate_window_seconds <= 0:
            problems.append(
                "plugins.atis.reply_rate_window_seconds must be > 0, got "
                f"{cfg.atis.reply_rate_window_seconds}"
            )
        if cfg.atis.voice.backend not in {"", "none", "file", "tts"}:
            problems.append(
                f"plugins.atis.voice.backend must be none|file|tts, got {cfg.atis.voice.backend!r}"
            )

        seen: dict[str, int] = {}
        for index, st in enumerate(cfg.atis.stations):
            where = f"plugins.atis.stations[{index}]"
            if len(st.icao) != 4 or not st.icao.isalpha():
                problems.append(f"{where}.icao must be a 4-letter ICAO code, got {st.icao!r}")
            if not -90.0 <= st.lat <= 90.0:
                problems.append(f"{where}.lat must be between -90 and 90, got {st.lat}")
            if not -180.0 <= st.lon <= 180.0:
                problems.append(f"{where}.lon must be between -180 and 180, got {st.lon}")
            try:
                encode_frequency(st.frequency)
            except ValueError as exc:
                problems.append(f"{where}.frequency is unusable: {exc}")
            if not 0 <= st.facility_type <= MAX_FACILITY_TYPE:
                problems.append(
                    f"{where}.facility_type must be between 0 and {MAX_FACILITY_TYPE}, "
                    f"got {st.facility_type}"
                )
            if not 0 < st.vis_range_nm <= MAX_VIS_RANGE_NM:
                problems.append(
                    f"{where}.vis_range_nm must be between 1 and {MAX_VIS_RANGE_NM}, "
                    f"got {st.vis_range_nm}"
                )
            if not st.callsign.strip():
                problems.append(f"{where}.callsign must not be empty")
            key = st.callsign.strip().upper()
            if key in seen:
                problems.append(
                    f"{where}.callsign {st.callsign!r} duplicates "
                    f"plugins.atis.stations[{seen[key]}]"
                )
            else:
                seen[key] = index

    if problems:
        error = MissingCredentialError if missing_credential else ConfigError
        raise error("invalid configuration:\n  - " + "\n  - ".join(problems))


def load_config(path: str | Path | None = None, validate: bool = True) -> AppConfig:
    load_dotenv()
    data: dict[str, Any] = {}
    cfg_path = Path(path or os.environ.get("OPENFSD_CONFIG", "config.yaml"))
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ConfigError("config root must be a mapping")
        data = loaded

    env_overlay = {
        "server": {
            k: os.environ[env]
            for k, env in (
                ("host", "OPENFSD_HOST"),
                ("port", "OPENFSD_PORT"),
                ("api_base", "OPENFSD_API_BASE"),
            )
            if env in os.environ
        },
        "auth": {
            k: os.environ[env]
            for k, env in (
                ("cid", "OPENFSD_CID"),
                ("password", "OPENFSD_PASSWORD"),
                ("token", "OPENFSD_TOKEN"),
            )
            if env in os.environ
        },
    }
    data = _merge(data, {k: v for k, v in env_overlay.items() if v})

    server = _section(data, "server")
    auth = _section(data, "auth")
    inj = _section(data, "injector")
    plugins = _section(data, "plugins")
    atis_raw = plugins.get("atis") or {}
    if not isinstance(atis_raw, dict):
        raise ConfigError("plugins.atis must be a mapping")
    voice_raw = atis_raw.get("voice") or {}
    if not isinstance(voice_raw, dict):
        raise ConfigError("plugins.atis.voice must be a mapping")

    stations_raw = atis_raw.get("stations") or []
    if not isinstance(stations_raw, list):
        raise ConfigError("plugins.atis.stations must be a list")
    stations = [_station_from_row(row, i) for i, row in enumerate(stations_raw)]

    cfg = AppConfig(
        server=ServerConfig(
            host=str(server.get("host", "127.0.0.1")),
            port=_as_int(server.get("port", 6809), "server.port"),
            api_base=str(server.get("api_base", "")).rstrip("/"),
        ),
        auth=AuthConfig(
            cid=_as_int(auth.get("cid", 0), "auth.cid"),
            password=str(auth.get("password", "")),
            token=str(auth.get("token", "")),
            real_name=str(auth.get("real_name", "ATIS Bot")),
            rating=_as_int(auth.get("rating", 5), "auth.rating"),
            protocol_revision=_as_int(
                auth.get("protocol_revision", 100), "auth.protocol_revision"
            ),
            client_id=str(auth.get("client_id", "0f5d")),
            client_name=str(auth.get("client_name", "openfsd-injector")),
            client_major=_as_int(auth.get("client_major", 0), "auth.client_major"),
            client_minor=_as_int(auth.get("client_minor", 1), "auth.client_minor"),
        ),
        injector=InjectorConfig(
            reconnect_seconds=_as_float(
                inj.get("reconnect_seconds", 5), "injector.reconnect_seconds"
            ),
            position_interval_seconds=_as_float(
                inj.get("position_interval_seconds", 5), "injector.position_interval_seconds"
            ),
        ),
        atis=AtisPluginConfig(
            enabled=bool(atis_raw.get("enabled", True)),
            refresh_seconds=_as_int(
                atis_raw.get("refresh_seconds", 3600), "plugins.atis.refresh_seconds"
            ),
            metar_url=str(
                atis_raw.get(
                    "metar_url",
                    "https://aviationweather.gov/api/data/metar?ids={icao}&format=raw",
                )
            ),
            reply_rate_limit=_as_int(
                atis_raw.get("reply_rate_limit", 6), "plugins.atis.reply_rate_limit"
            ),
            reply_rate_window_seconds=_as_float(
                atis_raw.get("reply_rate_window_seconds", 60.0),
                "plugins.atis.reply_rate_window_seconds",
            ),
            voice=VoiceConfig(
                enabled=bool(voice_raw.get("enabled", False)),
                backend=str(voice_raw.get("backend", "none")),
                scrape_url=str(voice_raw.get("scrape_url", "")),
                cache_dir=str(voice_raw.get("cache_dir", "audio/cache")),
            ),
            stations=stations,
        ),
        raw=data,
    )
    if validate:
        validate_config(cfg)
    return cfg
