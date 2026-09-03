"""Small built-in ICAO table for env-only station expansion.

``ATIS_ICAOS=EGLL,EGKK`` builds stations from this table when
``plugins.atis.stations`` is empty. An explicit list in ``config.yaml``
always wins. Unknown ICAOs fail fast — add a station block to the config
instead of guessing coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AirportInfo:
    icao: str
    name: str
    lat: float
    lon: float
    frequency: float
    vis_range_nm: int = 60
    facility_type: int = 4


# Published aerodrome reference points and typical ATIS frequencies.
# Enough for a fresh sidecar; anything else belongs in config.yaml.
AIRPORTS: dict[str, AirportInfo] = {
    "EGLL": AirportInfo("EGLL", "Heathrow Information", 51.4775, -0.4614, 128.080),
    "EGKK": AirportInfo("EGKK", "Gatwick Information", 51.1481, -0.1903, 136.525),
    "EGSS": AirportInfo("EGSS", "Stansted Information", 51.8850, 0.2350, 127.180),
    "EGGW": AirportInfo("EGGW", "Luton Information", 51.8747, -0.3683, 120.575),
    "EGCC": AirportInfo("EGCC", "Manchester Information", 53.3537, -2.2750, 128.175),
    "EGPH": AirportInfo("EGPH", "Edinburgh Information", 55.9500, -3.3725, 131.355),
    "EGPF": AirportInfo("EGPF", "Glasgow Information", 55.8719, -4.4331, 129.575),
    "EGBB": AirportInfo("EGBB", "Birmingham Information", 52.4539, -1.7480, 136.030),
    "EGAA": AirportInfo("EGAA", "Aldergrove Information", 54.6575, -6.2158, 126.130),
    "EIDW": AirportInfo("EIDW", "Dublin Information", 53.4213, -6.2700, 124.525),
    "LFPG": AirportInfo("LFPG", "Charles de Gaulle Information", 49.0097, 2.5479, 128.200),
    "EHAM": AirportInfo("EHAM", "Schiphol Information", 52.3086, 4.7639, 132.975),
    "EDDF": AirportInfo("EDDF", "Frankfurt Information", 50.0379, 8.5622, 118.025),
    "KJFK": AirportInfo("KJFK", "Kennedy Information", 40.6399, -73.7787, 128.725),
    "KLAX": AirportInfo("KLAX", "Los Angeles Information", 33.9425, -118.4081, 133.800),
}


def parse_atis_icaos(value: str) -> list[str]:
    """Split ``EGLL,EGKK`` (spaces optional) into unique uppercase ICAOs."""
    seen: set[str] = set()
    icaos: list[str] = []
    for part in value.split(","):
        icao = part.strip().upper()
        if not icao or icao in seen:
            continue
        seen.add(icao)
        icaos.append(icao)
    return icaos


def lookup_airport(icao: str) -> AirportInfo:
    key = icao.strip().upper()
    try:
        return AIRPORTS[key]
    except KeyError:
        raise KeyError(key) from None
