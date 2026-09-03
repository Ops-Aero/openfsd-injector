from openfsd_injector.atis.builder import build_lines, next_letter
from openfsd_injector.config import StationConfig


def test_letter_wraps():
    assert next_letter("A") == "B"
    assert next_letter("Z") == "A"


def test_build_lines_contains_letter_and_metar():
    st = StationConfig(
        icao="EGLL",
        name="Heathrow Information",
        callsign="EGLL_ATIS",
        frequency=128.08,
        runways_dep="27L, 27R",
        runways_arr="27L",
        approach="ILS",
        extra_lines=["birds reported"],
    )
    lines = build_lines(st, "C", "EGLL 031250Z 27008KT 9999 FEW030 18/10 Q1018")
    assert lines[0] == "HEATHROW INFORMATION C"
    assert lines[1].startswith("EGLL ")
    assert any("INFORMATION C" in line for line in lines)
    assert any("BIRDS REPORTED" == line for line in lines)
