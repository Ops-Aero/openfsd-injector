import pytest

from openfsd_injector.airports import AIRPORTS, lookup_airport, parse_atis_icaos
from openfsd_injector.protocol import encode_frequency


def test_parse_atis_icaos_strips_and_dedupes():
    assert parse_atis_icaos("egll, EGKK, egll,") == ["EGLL", "EGKK"]
    assert parse_atis_icaos("") == []
    assert parse_atis_icaos("  ") == []


def test_lookup_known_and_unknown():
    assert lookup_airport("egll").lat == 51.4775
    assert "EGKK" in AIRPORTS
    with pytest.raises(KeyError, match="ZZZZ"):
        lookup_airport("ZZZZ")


def test_table_coordinates_and_frequencies_are_sane():
    for icao, info in AIRPORTS.items():
        assert info.icao == icao
        assert -90.0 <= info.lat <= 90.0
        assert -180.0 <= info.lon <= 180.0
        encode_frequency(info.frequency)
