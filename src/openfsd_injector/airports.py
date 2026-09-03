"""Built-in ICAO table for env-only station expansion.

``ATIS_ICAOS`` (see ``DEFAULT_ATIS_ICAOS``) builds stations from this table
when ``plugins.atis.stations`` is empty. An explicit list in ``config.yaml``
always wins. Unknown ICAOs fail fast — add a station block to the config
instead of guessing coordinates.

Frequencies are published VHF COM ATIS (8.33 channel names in Europe;
FAA D-ATIS COM in the US). Navaid overlays below 118 MHz are omitted
because FSD ``encode_frequency`` only accepts 118–137 MHz.
Coordinates are aerodrome reference points.
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


# One container / one CID covers this majors set when ATIS_ICAOS is unset
# in a copied .env.example. Order matches the documented UK / EU / US lists.
DEFAULT_ATIS_ICAOS: tuple[str, ...] = (
    # UK
    "EGLL",
    "EGKK",
    "EGSS",
    "EGLC",
    "EGGW",
    "EGCC",
    "EGGP",
    "EGBB",
    "EGNX",
    "EGPH",
    "EGPF",
    "EGPD",
    "EGAA",
    "EGHH",
    "EGGD",
    "EGNT",
    # EU
    "EHAM",
    "LFPG",
    "LFPO",
    "EDDF",
    "EDDM",
    "LEMD",
    "LEBL",
    "LIRF",
    "LSZH",
    "EBBR",
    "EKCH",
    "ENGM",
    "EIDW",
    # US
    "KATL",
    "KORD",
    "KLAX",
    "KDFW",
    "KDEN",
    "KJFK",
    "KSFO",
    "KSEA",
    "KLAS",
    "KMIA",
    "KPHX",
    "KBOS",
    "KIAD",
    "KEWR",
)

DEFAULT_ATIS_ICAOS_CSV = ",".join(DEFAULT_ATIS_ICAOS)


# Published ARP + typical/combined ATIS COM. Anything else belongs in config.yaml.
AIRPORTS: dict[str, AirportInfo] = {
    # UK — NATS/UK AIP 8.33 channel names (VATSIM-UK sector file).
    "EGLL": AirportInfo("EGLL", "Heathrow Information", 51.4775, -0.4614, 128.080),
    "EGKK": AirportInfo("EGKK", "Gatwick Information", 51.1481, -0.1903, 136.525),
    "EGSS": AirportInfo("EGSS", "Stansted Information", 51.8850, 0.2350, 127.180),
    "EGLC": AirportInfo("EGLC", "City Information", 51.5053, 0.0553, 136.355),
    "EGGW": AirportInfo("EGGW", "Luton Information", 51.8747, -0.3683, 120.575),
    "EGCC": AirportInfo("EGCC", "Manchester Information", 53.3537, -2.2750, 128.175),
    "EGGP": AirportInfo("EGGP", "Liverpool Information", 53.3349, -2.8496, 124.330),
    "EGBB": AirportInfo("EGBB", "Birmingham Information", 52.4539, -1.7480, 136.030),
    "EGNX": AirportInfo("EGNX", "East Midlands Information", 52.8311, -1.3281, 122.680),
    "EGPH": AirportInfo("EGPH", "Edinburgh Information", 55.9500, -3.3725, 131.355),
    "EGPF": AirportInfo("EGPF", "Glasgow Information", 55.8719, -4.4331, 129.575),
    "EGPD": AirportInfo("EGPD", "Aberdeen Information", 57.2019, -2.1978, 121.855),
    "EGAA": AirportInfo("EGAA", "Aldergrove Information", 54.6575, -6.2158, 126.130),
    "EGHH": AirportInfo("EGHH", "Bournemouth Information", 50.7805, -1.8396, 133.730),
    "EGGD": AirportInfo("EGGD", "Bristol Information", 51.3823, -2.7165, 126.030),
    "EGNT": AirportInfo("EGNT", "Newcastle Information", 55.0380, -1.6896, 118.380),
    # EU — national AIP / published COM ATIS (arrival or combined).
    "EHAM": AirportInfo("EHAM", "Schiphol Information", 52.3086, 4.7639, 132.975),
    "LFPG": AirportInfo("LFPG", "Charles de Gaulle Information", 49.0097, 2.5479, 128.200),
    "LFPO": AirportInfo("LFPO", "Orly Information", 48.7295, 2.3590, 131.350),
    "EDDF": AirportInfo("EDDF", "Frankfurt Information", 50.0379, 8.5622, 118.025),
    "EDDM": AirportInfo("EDDM", "Munich Information", 48.3538, 11.7861, 123.130),
    "LEMD": AirportInfo("LEMD", "Madrid Information", 40.4934, -3.5722, 118.250),
    "LEBL": AirportInfo("LEBL", "Barcelona Information", 41.2971, 2.0785, 118.650),
    "LIRF": AirportInfo("LIRF", "Fiumicino Information", 41.8045, 12.2520, 121.850),
    "LSZH": AirportInfo("LSZH", "Zurich Information", 47.4581, 8.5481, 125.725),
    "EBBR": AirportInfo("EBBR", "Brussels Information", 50.9014, 4.4844, 132.475),
    "EKCH": AirportInfo("EKCH", "Kastrup Information", 55.6179, 12.6560, 122.750),
    "ENGM": AirportInfo("ENGM", "Gardermoen Information", 60.1939, 11.1004, 126.125),
    "EIDW": AirportInfo("EIDW", "Dublin Information", 53.4213, -6.2700, 124.525),
    # US — FAA D-ATIS VHF COM (not navaid overlays such as 113.7 / 115.7).
    "KATL": AirportInfo("KATL", "Atlanta Information", 33.6367, -84.4281, 119.650),
    "KORD": AirportInfo("KORD", "O'Hare Information", 41.9786, -87.9048, 135.400),
    "KLAX": AirportInfo("KLAX", "Los Angeles Information", 33.9425, -118.4081, 133.800),
    "KDFW": AirportInfo("KDFW", "Dallas-Fort Worth Information", 32.8968, -97.0380, 123.775),
    "KDEN": AirportInfo("KDEN", "Denver Information", 39.8600, -104.6738, 125.600),
    "KJFK": AirportInfo("KJFK", "Kennedy Information", 40.6399, -73.7787, 128.725),
    "KSFO": AirportInfo("KSFO", "San Francisco Information", 37.6198, -122.3748, 118.850),
    "KSEA": AirportInfo("KSEA", "Seattle-Tacoma Information", 47.4479, -122.3103, 118.000),
    "KLAS": AirportInfo("KLAS", "Harry Reid Information", 36.0834, -115.1518, 132.400),
    "KMIA": AirportInfo("KMIA", "Miami Information", 25.7960, -80.2898, 119.150),
    "KPHX": AirportInfo("KPHX", "Sky Harbor Information", 33.4353, -112.0059, 127.575),
    "KBOS": AirportInfo("KBOS", "Logan Information", 42.3620, -71.0079, 135.000),
    "KIAD": AirportInfo("KIAD", "Dulles Information", 38.9445, -77.4558, 134.850),
    "KEWR": AirportInfo("KEWR", "Newark Information", 40.6894, -74.1705, 134.825),
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
