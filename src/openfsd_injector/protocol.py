"""VATSIM-style FSD packet helpers as implemented by openFSD."""

from __future__ import annotations


def frequency_khz(mhz: float) -> int:
    """Integer kHz, rounded the same way FSD ``%`` packets encode the COM."""
    khz = int(round(mhz * 1000))
    if not 100_000 <= khz <= 199_999:
        raise ValueError(f"frequency out of aviation VHF range: {mhz}")
    return khz


def encode_frequency(mhz: float) -> str:
    """Encode 128.080 MHz as the 5-digit FSD form `28080` (drop the leading 1)."""
    return f"{frequency_khz(mhz) - 100_000:05d}"


def frequency_hz(mhz: float) -> int:
    """Integer Hz for SRS / TrackAudio. 128.080 MHz → ``128080000``.

    Same rounding as :func:`encode_frequency`, so the radio matches the FSD
    ``%`` advertisement.
    """
    return frequency_khz(mhz) * 1000


def decode_frequency(encoded: str) -> float:
    return (100_000 + int(encoded.split("&", 1)[0])) / 1000.0


def freq_to_radio_target(mhz: float) -> str:
    """Text-radio address used in `#TM` (`@28080` for 128.080)."""
    return f"@{encode_frequency(mhz)}"


def split_packet(line: str) -> list[str]:
    return line.rstrip("\r\n").split(":")


def packet_type(line: str) -> str:
    raw = line.rstrip("\r\n")
    if not raw:
        return ""
    if raw[0] in {"$", "#", "%", "@", "^"}:
        return raw[:3] if len(raw) >= 3 and raw[1].isalpha() and raw[2].isalpha() else raw[0]
    return raw[:3]


def build_id(
    callsign: str,
    client_id: str,
    client_name: str,
    major: int,
    minor: int,
    cid: int,
    system_uid: int = 0,
) -> str:
    # 8 fields — omit the 9th (VatsimAuth challenge) so openFSD skips auth challenges.
    return (
        f"$ID{callsign}:SERVER:{client_id}:{client_name}:"
        f"{major}:{minor}:{cid}:{system_uid}"
    )


def build_add_atc(
    callsign: str,
    real_name: str,
    cid: int,
    token: str,
    rating: int,
    proto: int,
) -> str:
    return f"#AA{callsign}:SERVER:{real_name}:{cid}:{token}:{rating}:{proto}"


def build_delete_atc(callsign: str, cid: int) -> str:
    return f"#DA{callsign}:SERVER:{cid}"


def build_atc_position(
    callsign: str,
    frequency_mhz: float,
    facility_type: int,
    vis_range_nm: int,
    rating: int,
    lat: float,
    lon: float,
) -> str:
    return (
        f"%{callsign}:{encode_frequency(frequency_mhz)}:{facility_type}:"
        f"{int(vis_range_nm)}:{rating}:{lat:.5f}:{lon:.5f}:0"
    )


def build_new_atis(callsign: str, letter: str) -> str:
    """Notify in-range ATC that this station's information letter changed."""
    return f"$CQ{callsign}:@94835:NEWATIS:{letter}"


def build_query_response(from_cs: str, to_cs: str, qtype: str, *payload: str) -> str:
    tail = ":".join(payload)
    if tail:
        return f"$CR{from_cs}:{to_cs}:{qtype}:{tail}"
    return f"$CR{from_cs}:{to_cs}:{qtype}"


def build_text(from_cs: str, to_cs: str, message: str) -> str:
    return f"#TM{from_cs}:{to_cs}:{message}"


def parse_query(line: str) -> tuple[str, str, str, list[str]] | None:
    """Return (from, to, type, payload_fields) for $CQ / $CR, else None."""
    raw = line.rstrip("\r\n")
    if not raw.startswith("$CQ") and not raw.startswith("$CR"):
        return None
    fields = raw.split(":")
    if len(fields) < 3:
        return None
    origin = fields[0][3:]
    dest = fields[1]
    qtype = fields[2]
    payload = fields[3:]
    return origin, dest, qtype, payload
