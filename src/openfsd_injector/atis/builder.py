"""Build VATSIM-style text ATIS lines from METAR + station config."""

from __future__ import annotations

import string
from dataclasses import dataclass, field

from ..config import StationConfig

LETTERS = string.ascii_uppercase


@dataclass
class AtisState:
    letter: str = "A"
    metar: str = ""
    lines: list[str] = field(default_factory=list)

    def advance(self) -> None:
        idx = LETTERS.index(self.letter)
        self.letter = LETTERS[(idx + 1) % len(LETTERS)]


def next_letter(current: str) -> str:
    idx = LETTERS.index(current.upper())
    return LETTERS[(idx + 1) % len(LETTERS)]


def build_lines(station: StationConfig, letter: str, metar: str) -> list[str]:
    icao = station.icao
    name = station.name.upper().removesuffix(" INFORMATION").strip()
    wx = metar.strip() or f"{icao} METAR UNAVAILABLE"
    lines = [
        f"{name} INFORMATION {letter}",
        wx,
    ]
    if station.runways_arr:
        lines.append(f"EXPECT {station.approach} APPROACH RUNWAY {station.runways_arr}")
    if station.runways_dep:
        lines.append(f"DEPARTURE RUNWAY {station.runways_dep}")
    lines.extend(line.upper() for line in station.extra_lines if line.strip())
    lines.append(f"ADVISE ON INITIAL CONTACT YOU HAVE INFORMATION {letter}")
    return lines
