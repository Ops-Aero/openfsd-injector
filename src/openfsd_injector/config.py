from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv


@dataclass
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 6809
    api_base: str = ""


@dataclass
class AuthConfig:
    cid: int = 100000
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


def load_config(path: str | Path | None = None) -> AppConfig:
    load_dotenv()
    data: dict[str, Any] = {}
    cfg_path = Path(path or os.environ.get("OPENFSD_CONFIG", "config.yaml"))
    if cfg_path.exists():
        loaded = yaml.safe_load(cfg_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError("config root must be a mapping")
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

    server = data.get("server") or {}
    auth = data.get("auth") or {}
    inj = data.get("injector") or {}
    plugins = data.get("plugins") or {}
    atis_raw = plugins.get("atis") or {}
    voice_raw = atis_raw.get("voice") or {}

    stations = []
    for row in atis_raw.get("stations") or []:
        stations.append(
            StationConfig(
                icao=str(row["icao"]).upper(),
                name=row.get("name") or f"{row['icao']} Information",
                callsign=row.get("callsign") or f"{str(row['icao']).upper()}_ATIS",
                frequency=float(row["frequency"]),
                lat=float(row["lat"]),
                lon=float(row["lon"]),
                vis_range_nm=int(row.get("vis_range_nm", 50)),
                facility_type=int(row.get("facility_type", 4)),
                runways_dep=str(row.get("runways_dep", "")),
                runways_arr=str(row.get("runways_arr", "")),
                approach=str(row.get("approach", "ILS")),
                extra_lines=list(row.get("extra_lines") or []),
            )
        )

    cfg = AppConfig(
        server=ServerConfig(
            host=str(server.get("host", "127.0.0.1")),
            port=int(server.get("port", 6809)),
            api_base=str(server.get("api_base", "")).rstrip("/"),
        ),
        auth=AuthConfig(
            cid=int(auth.get("cid", 100000)),
            password=str(auth.get("password", "")),
            token=str(auth.get("token", "")),
            real_name=str(auth.get("real_name", "ATIS Bot")),
            rating=int(auth.get("rating", 5)),
            protocol_revision=int(auth.get("protocol_revision", 100)),
            client_id=str(auth.get("client_id", "0f5d")),
            client_name=str(auth.get("client_name", "openfsd-injector")),
            client_major=int(auth.get("client_major", 0)),
            client_minor=int(auth.get("client_minor", 1)),
        ),
        injector=InjectorConfig(
            reconnect_seconds=float(inj.get("reconnect_seconds", 5)),
            position_interval_seconds=float(inj.get("position_interval_seconds", 5)),
        ),
        atis=AtisPluginConfig(
            enabled=bool(atis_raw.get("enabled", True)),
            refresh_seconds=int(atis_raw.get("refresh_seconds", 3600)),
            metar_url=str(
                atis_raw.get(
                    "metar_url",
                    "https://aviationweather.gov/api/data/metar?ids={icao}&format=raw",
                )
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
    return cfg
