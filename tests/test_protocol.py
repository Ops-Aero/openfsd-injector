from openfsd_injector.protocol import (
    build_add_atc,
    build_atc_position,
    build_new_atis,
    encode_frequency,
    frequency_hz,
    parse_query,
)


def test_encode_frequency():
    assert encode_frequency(122.800) == "22800"
    assert encode_frequency(128.080) == "28080"
    assert encode_frequency(118.100) == "18100"


def test_frequency_hz_matches_fsd_rounding():
    """SRS integer Hz is the same COM FSD advertises in ``%`` packets."""
    assert frequency_hz(128.080) == 128_080_000
    assert frequency_hz(122.800) == 122_800_000
    assert frequency_hz(136.355) == 136_355_000
    assert frequency_hz(118.100) == 118_100_000


def test_add_atc_field_count():
    pkt = build_add_atc("EGLL_ATIS", "ATIS Bot", 100000, "token", 5, 100)
    assert pkt.startswith("#AAEGLL_ATIS:")
    assert pkt.split(":") == [
        "#AAEGLL_ATIS",
        "SERVER",
        "ATIS Bot",
        "100000",
        "token",
        "5",
        "100",
    ]


def test_atc_position():
    pkt = build_atc_position("EGLL_ATIS", 128.080, 4, 60, 5, 51.4775, -0.4614)
    fields = pkt.split(":")
    assert fields[0] == "%EGLL_ATIS"
    assert fields[1] == "28080"
    assert fields[2] == "4"
    assert fields[3] == "60"
    assert fields[5] == "51.47750"
    assert fields[6] == "-0.46140"


def test_parse_atis_query():
    parsed = parse_query("$CQBAW123:EGLL_ATIS:ATIS")
    assert parsed == ("BAW123", "EGLL_ATIS", "ATIS", [])


def test_new_atis_broadcast():
    pkt = build_new_atis("EGLL_ATIS", "C")
    assert pkt == "$CQEGLL_ATIS:@94835:NEWATIS:C"
