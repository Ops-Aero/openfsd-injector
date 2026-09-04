from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .airports import lookup_airport, parse_atis_icaos
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
ADMINISTRATOR_RATING = 12
# Tower ATIS (facility_type 4) needs S2 (3); C1 (5) is the recommended ceiling.
RECOMMENDED_MAX_RATING = 5
MAX_VIS_RANGE_NM = 1500
SUPPORTED_VOICE_BACKENDS = ("", "none", "file", "tts")
SUPPORTED_VOICE_ENGINES = ("", "auto", "edge-tts", "piper")
DEFAULT_AUDIO_HTTP_PORT = 8091
DEFAULT_SRS_PORT = 5002
DEFAULT_SRS_NAME = "OPSAERO_ATIS"
ADMINISTRATOR_RATING_MESSAGE = (
    "auth.rating 12 (Administrator) is refused: create a dedicated "
    "least-privilege openFSD user for the injector (S2/3 for tower ATIS, "
    "at most C1/5). Set auth.allow_administrator or "
    "OPENFSD_ALLOW_ADMINISTRATOR=1 only if you intentionally accept this risk"
)


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
    # S2 — enough for tower ATIS (facility_type 4); never default to ADM (12).
    rating: int = 3
    protocol_revision: int = 100
    client_id: str = "0f5d"
    client_name: str = "openfsd-injector"
    client_major: int = 0
    client_minor: int = 1
    allow_administrator: bool = False


@dataclass
class InjectorConfig:
    reconnect_seconds: float = 5.0
    position_interval_seconds: float = 5.0


@dataclass
class VoiceConfig:
    enabled: bool = False
    backend: str = "none"
    # Ignored. Never used to fetch LiveATC or any other audio feed.
    scrape_url: str = ""
    cache_dir: str = "audio/cache"
    # auto = edge-tts, then piper if voice.piper_model is set.
    engine: str = "auto"
    voice: str = "en-GB-SoniaNeural"
    piper_model: str = ""
    loop_silence_seconds: float = 2.0


@dataclass
class AudioHttpConfig:
    """Serve cached WAV/OGG on an internal port for opsaero-main/client.

    ``srs_host`` empty = HTTP only (current default). When set, the injector
    also registers ATIS radios on a self-hosted ciribob SRS 2.x server
    (opsaero-main ``flisher/dcs-srs-server:ciribob-2.4.0.0`` on 5002).
    SRS is never required for the process to stay up.
    """

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = DEFAULT_AUDIO_HTTP_PORT
    # Empty = off. opsaero-main should set SRS_HOST=srs on the atis service.
    srs_host: str = ""
    srs_port: int = DEFAULT_SRS_PORT
    # False = TCP radio presence only; HTTP audio stays. Do not pretend TX.
    srs_tx: bool = True
    srs_name: str = DEFAULT_SRS_NAME
    # 0=spectator 1=red 2=blue. Spectator is correct for a civil ATIS bot.
    srs_coalition: int = 0
    # Env-only (SRS_EAM_PASSWORD). Never read from yaml / never commit.
    srs_eam_password: str = ""


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
    audio_http: AudioHttpConfig = field(default_factory=AudioHttpConfig)
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


def _as_bool(value: Any, where: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    raise ConfigError(f"{where} must be a boolean, got {value!r}")


def running_in_docker() -> bool:
    """True when the process is inside a container (``/.dockerenv``)."""
    return Path("/.dockerenv").exists()


def resolve_voice_backend(voice_raw: dict[str, Any]) -> str:
    """``VOICE_BACKEND`` wins; else yaml; else ``tts`` in Docker, ``none`` on the host."""
    env_backend = os.environ.get("VOICE_BACKEND")
    if env_backend is not None and env_backend.strip() != "":
        return env_backend.strip().lower()
    raw = voice_raw.get("backend")
    if raw not in (None, ""):
        return str(raw).strip().lower()
    if running_in_docker():
        return "tts"
    return "none"


def resolve_voice_enabled(voice_raw: dict[str, Any], backend: str) -> bool:
    """Yaml ``enabled`` is the override; otherwise TTS is on when the backend is ``tts``."""
    if "enabled" in voice_raw:
        return _as_bool(voice_raw["enabled"], "plugins.atis.voice.enabled")
    return backend == "tts"


def resolve_audio_http_enabled(audio_raw: dict[str, Any]) -> bool:
    """``AUDIO_HTTP`` wins; else yaml; else on (the first-party audio path)."""
    env = os.environ.get("AUDIO_HTTP")
    if env is not None and env.strip() != "":
        return _as_bool(env, "AUDIO_HTTP")
    if "enabled" in audio_raw:
        return _as_bool(audio_raw["enabled"], "plugins.atis.audio_http.enabled")
    return True


def resolve_audio_http_host(audio_raw: dict[str, Any]) -> str:
    """Compose binds ``0.0.0.0``. Host Python binds loopback unless publish is set."""
    env_host = os.environ.get("AUDIO_HTTP_HOST")
    if env_host is not None and env_host.strip() != "":
        return env_host.strip()
    raw = audio_raw.get("host")
    if raw not in (None, ""):
        return str(raw).strip()
    publish = os.environ.get("AUDIO_HTTP_PUBLISH", "")
    if publish.strip() != "" and _as_bool(publish, "AUDIO_HTTP_PUBLISH"):
        return "0.0.0.0"
    if running_in_docker():
        return "0.0.0.0"
    return "127.0.0.1"


def resolve_audio_http_port(audio_raw: dict[str, Any]) -> int:
    env_port = os.environ.get("AUDIO_HTTP_PORT")
    if env_port is not None and env_port.strip() != "":
        return _as_int(env_port, "AUDIO_HTTP_PORT")
    return _as_int(audio_raw.get("port", DEFAULT_AUDIO_HTTP_PORT), "plugins.atis.audio_http.port")


def resolve_srs_host(audio_raw: dict[str, Any]) -> str:
    """Optional. Empty keeps the SRS loop off — HTTP is the first-party path."""
    env = os.environ.get("SRS_HOST")
    if env is not None:
        return env.strip()
    return str(audio_raw.get("srs_host", "")).strip()


def resolve_srs_port(audio_raw: dict[str, Any]) -> int:
    env = os.environ.get("SRS_PORT")
    if env is not None and env.strip() != "":
        return _as_int(env, "SRS_PORT")
    return _as_int(audio_raw.get("srs_port", DEFAULT_SRS_PORT), "plugins.atis.audio_http.srs_port")


def resolve_srs_tx(audio_raw: dict[str, Any]) -> bool:
    """``SRS_TX`` wins; else yaml; else on (only used when ``SRS_HOST`` is set)."""
    env = os.environ.get("SRS_TX")
    if env is not None and env.strip() != "":
        return _as_bool(env, "SRS_TX")
    if "srs_tx" in audio_raw:
        return _as_bool(audio_raw["srs_tx"], "plugins.atis.audio_http.srs_tx")
    return True


def resolve_srs_name(audio_raw: dict[str, Any]) -> str:
    env = os.environ.get("SRS_NAME")
    if env is not None and env.strip() != "":
        return env.strip()
    raw = audio_raw.get("srs_name")
    if raw not in (None, ""):
        return str(raw).strip()
    return DEFAULT_SRS_NAME


def resolve_srs_coalition(audio_raw: dict[str, Any]) -> int:
    env = os.environ.get("SRS_COALITION")
    if env is not None and env.strip() != "":
        return _as_int(env, "SRS_COALITION")
    return _as_int(audio_raw.get("srs_coalition", 0), "plugins.atis.audio_http.srs_coalition")


def resolve_srs_eam_password() -> str:
    """EAM password is env-only so it cannot be committed in config.yaml."""
    return os.environ.get("SRS_EAM_PASSWORD", "").strip()


def stations_from_atis_icaos(value: str) -> list[StationConfig]:
    """Build stations from ``ATIS_ICAOS`` using the built-in airport table."""
    stations: list[StationConfig] = []
    unknown: list[str] = []
    for icao in parse_atis_icaos(value):
        try:
            info = lookup_airport(icao)
        except KeyError:
            unknown.append(icao)
            continue
        stations.append(
            StationConfig(
                icao=info.icao,
                name=info.name,
                callsign=f"{info.icao}_ATIS",
                frequency=info.frequency,
                lat=info.lat,
                lon=info.lon,
                vis_range_nm=info.vis_range_nm,
                facility_type=info.facility_type,
            )
        )
    if unknown:
        listed = ", ".join(repr(code) for code in unknown)
        raise ConfigError(
            f"ATIS_ICAOS ICAO(s) {listed} not in the built-in airport table; "
            "add a station entry to config.yaml (lat/lon/frequency) instead"
        )
    return stations


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
    if cfg.auth.rating == ADMINISTRATOR_RATING and not cfg.auth.allow_administrator:
        problems.append(ADMINISTRATOR_RATING_MESSAGE)
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
        if cfg.atis.voice.backend not in SUPPORTED_VOICE_BACKENDS:
            problems.append(
                f"plugins.atis.voice.backend must be none|file|tts, got {cfg.atis.voice.backend!r}"
            )
        if cfg.atis.voice.engine not in SUPPORTED_VOICE_ENGINES:
            problems.append(
                "plugins.atis.voice.engine must be auto|edge-tts|piper, got "
                f"{cfg.atis.voice.engine!r}"
            )
        if not cfg.atis.stations:
            problems.append(
                "no ATIS stations: set plugins.atis.stations in config.yaml "
                "or ATIS_ICAOS (see .env.example majors list)"
            )
        if cfg.atis.voice.loop_silence_seconds < 0:
            problems.append(
                "plugins.atis.voice.loop_silence_seconds must be >= 0, got "
                f"{cfg.atis.voice.loop_silence_seconds}"
            )
        if (
            cfg.atis.voice.backend == "tts"
            and cfg.atis.voice.engine == "piper"
            and not cfg.atis.voice.piper_model.strip()
        ):
            problems.append(
                "plugins.atis.voice.piper_model is required when voice.engine is piper"
            )
        if cfg.atis.audio_http.enabled:
            if not cfg.atis.audio_http.host.strip():
                problems.append("plugins.atis.audio_http.host must not be empty")
            if not 0 <= cfg.atis.audio_http.port <= 65535:
                problems.append(
                    "plugins.atis.audio_http.port must be between 0 and 65535, "
                    f"got {cfg.atis.audio_http.port}"
                )
        if not 1 <= cfg.atis.audio_http.srs_port <= 65535:
            problems.append(
                "plugins.atis.audio_http.srs_port must be between 1 and 65535, "
                f"got {cfg.atis.audio_http.srs_port}"
            )
        if cfg.atis.audio_http.srs_coalition not in (0, 1, 2):
            problems.append(
                "plugins.atis.audio_http.srs_coalition must be 0 (spectator), "
                f"1 (red) or 2 (blue), got {cfg.atis.audio_http.srs_coalition}"
            )
        if cfg.atis.audio_http.srs_host and not cfg.atis.audio_http.srs_name.strip():
            problems.append("plugins.atis.audio_http.srs_name must not be empty when SRS_HOST is set")

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
                ("allow_administrator", "OPENFSD_ALLOW_ADMINISTRATOR"),
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
    audio_http_raw = atis_raw.get("audio_http") or {}
    if not isinstance(audio_http_raw, dict):
        raise ConfigError("plugins.atis.audio_http must be a mapping")

    stations_raw = atis_raw.get("stations")
    if stations_raw in (None, []):
        stations = stations_from_atis_icaos(os.environ.get("ATIS_ICAOS", ""))
    elif not isinstance(stations_raw, list):
        raise ConfigError("plugins.atis.stations must be a list")
    else:
        stations = [_station_from_row(row, i) for i, row in enumerate(stations_raw)]

    voice_backend = resolve_voice_backend(voice_raw)
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
            rating=_as_int(auth.get("rating", 3), "auth.rating"),
            protocol_revision=_as_int(
                auth.get("protocol_revision", 100), "auth.protocol_revision"
            ),
            client_id=str(auth.get("client_id", "0f5d")),
            client_name=str(auth.get("client_name", "openfsd-injector")),
            client_major=_as_int(auth.get("client_major", 0), "auth.client_major"),
            client_minor=_as_int(auth.get("client_minor", 1), "auth.client_minor"),
            allow_administrator=_as_bool(
                auth.get("allow_administrator", False), "auth.allow_administrator"
            ),
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
                enabled=resolve_voice_enabled(voice_raw, voice_backend),
                backend=voice_backend,
                scrape_url=str(voice_raw.get("scrape_url", "")),
                cache_dir=str(voice_raw.get("cache_dir", "audio/cache")),
                engine=str(voice_raw.get("engine", "auto")),
                voice=str(voice_raw.get("voice", "en-GB-SoniaNeural")),
                piper_model=str(voice_raw.get("piper_model", "")),
                loop_silence_seconds=_as_float(
                    voice_raw.get("loop_silence_seconds", 2.0),
                    "plugins.atis.voice.loop_silence_seconds",
                ),
            ),
            audio_http=AudioHttpConfig(
                enabled=resolve_audio_http_enabled(audio_http_raw),
                host=resolve_audio_http_host(audio_http_raw),
                port=resolve_audio_http_port(audio_http_raw),
                srs_host=resolve_srs_host(audio_http_raw),
                srs_port=resolve_srs_port(audio_http_raw),
                srs_tx=resolve_srs_tx(audio_http_raw),
                srs_name=resolve_srs_name(audio_http_raw),
                srs_coalition=resolve_srs_coalition(audio_http_raw),
                srs_eam_password=resolve_srs_eam_password(),
            ),
            stations=stations,
        ),
        raw=data,
    )
    if validate:
        validate_config(cfg)
    return cfg
