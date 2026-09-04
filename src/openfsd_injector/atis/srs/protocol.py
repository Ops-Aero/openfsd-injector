"""Ciribob SRS 2.x TCP JSON + UDP voice framing (2.4 packet layout).

TCP: one JSON object per line. UDP: ``UDPVoicePacket`` as in
ciribob/DCS-SimpleRadioStandalone ``Common/Models/UDPVoicePacket.cs``
(header 6 + Opus + 10 bytes/freq + 57-byte fixed segment).

This is a minimal correct subset — not a reimplementation of the full client.
UDP radio encryption (``enc=1``) and a server connection password are not
implemented; the opsaero-main default image has neither.
"""

from __future__ import annotations

import json
import math
import secrets
import struct
from dataclasses import dataclass, field
from typing import Any

from ...protocol import frequency_hz

SRS_CLIENT_VERSION = "2.4.0.0"
GUID_LENGTH = 22
HEADER_LENGTH = 6
FREQUENCY_SEGMENT_LENGTH = 10
FIXED_SEGMENT_LENGTH = 57  # uint32 + uint64 + hop + 2×22-byte GUID
UNIT_ID_DEFAULT = 1_000_000_01
MODULATION_AM = 0

MSG_UPDATE = 0
MSG_PING = 1
MSG_SYNC = 2
MSG_RADIO_UPDATE = 3
MSG_SERVER_SETTINGS = 4
MSG_CLIENT_DISCONNECT = 5
MSG_VERSION_MISMATCH = 6
MSG_EAM_PASSWORD = 7
MSG_EAM_DISCONNECT = 8


def new_guid() -> str:
    """22-byte ASCII client GUID (ciribob ShortGuid length)."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(secrets.choice(alphabet) for _ in range(GUID_LENGTH))


def _omit_empty(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if item is None:
                continue
            if item == "" and key in {"ExternalAWACSModePassword"}:
                continue
            out[key] = _omit_empty(item)
        return out
    if isinstance(value, list):
        return [_omit_empty(item) for item in value]
    return value


def encode_tcp_message(message: dict[str, Any]) -> bytes:
    """Serialize one SRS control message. No trailing spaces; newline-terminated."""
    return (json.dumps(_omit_empty(message), separators=(",", ":")) + "\n").encode("utf-8")


def decode_tcp_message(line: bytes | str) -> dict[str, Any]:
    if isinstance(line, bytes):
        text = line.decode("utf-8").strip()
    else:
        text = line.strip()
    if not text:
        raise ValueError("empty SRS TCP line")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("SRS TCP message must be a JSON object")
    return payload


def radio_payload(
    *,
    frequency_mhz: float,
    name: str,
    modulation: int = MODULATION_AM,
) -> dict[str, Any]:
    """One SRS radio on the station COM, integer Hz matching FSD."""
    return {
        "freq": float(frequency_hz(frequency_mhz)),
        "modulation": int(modulation),
        "enc": False,
        "encKey": 0,
        "secFreq": 0.0,
        "retransmit": False,
        "name": name,
    }


def client_payload(
    *,
    guid: str,
    name: str,
    coalition: int,
    radios: list[dict[str, Any]],
    unit: str = "External Audio",
    unit_id: int = UNIT_ID_DEFAULT,
    lat: float = 0.0,
    lon: float = 0.0,
    allow_record: bool = True,
) -> dict[str, Any]:
    return {
        "ClientGuid": guid,
        "Name": name,
        "Seat": 0,
        "Coalition": int(coalition),
        "AllowRecord": allow_record,
        "RadioInfo": {
            "radios": radios,
            "unit": unit,
            "unitId": int(unit_id),
            "iff": {
                "control": 2,
                "status": 0,
                "mode1": -1,
                "mode2": -1,
                "mode3": -1,
                "mode4": False,
                "mic": -1,
            },
            "ambient": {"vol": 1.0, "abType": ""},
        },
        "LatLngPosition": {"lat": lat, "lng": lon, "alt": 0.0},
    }


def build_sync_message(
    client: dict[str, Any],
    *,
    version: str = SRS_CLIENT_VERSION,
) -> dict[str, Any]:
    return {"Version": version, "Client": client, "MsgType": MSG_SYNC}


def build_radio_update_message(
    client: dict[str, Any],
    *,
    version: str = SRS_CLIENT_VERSION,
) -> dict[str, Any]:
    return {"Version": version, "Client": client, "MsgType": MSG_RADIO_UPDATE}


def build_ping_message(*, version: str = SRS_CLIENT_VERSION) -> dict[str, Any]:
    return {"Version": version, "MsgType": MSG_PING}


def build_eam_password_message(
    client: dict[str, Any],
    password: str,
    *,
    version: str = SRS_CLIENT_VERSION,
) -> dict[str, Any]:
    return {
        "Version": version,
        "Client": client,
        "ExternalAWACSModePassword": password,
        "MsgType": MSG_EAM_PASSWORD,
    }


@dataclass(frozen=True)
class VoiceFrequency:
    frequency_hz: float
    modulation: int = MODULATION_AM
    encryption: int = 0


@dataclass
class VoicePacket:
    audio: bytes
    frequencies: list[VoiceFrequency]
    unit_id: int
    packet_id: int
    hops: int
    relay_guid: bytes
    origin_guid: bytes
    packet_length: int = field(init=False)

    def __post_init__(self) -> None:
        if len(self.relay_guid) != GUID_LENGTH or len(self.origin_guid) != GUID_LENGTH:
            raise ValueError(f"SRS GUIDs must be {GUID_LENGTH} bytes")
        audio_len = len(self.audio)
        freq_len = len(self.frequencies) * FREQUENCY_SEGMENT_LENGTH
        self.packet_length = HEADER_LENGTH + audio_len + freq_len + FIXED_SEGMENT_LENGTH


def encode_voice_packet(packet: VoicePacket) -> bytes:
    """Encode a ciribob 2.4 ``UDPVoicePacket`` (little-endian)."""
    audio_len = len(packet.audio)
    freq_len = len(packet.frequencies) * FREQUENCY_SEGMENT_LENGTH
    total = HEADER_LENGTH + audio_len + freq_len + FIXED_SEGMENT_LENGTH
    buf = bytearray(total)
    struct.pack_into("<HHH", buf, 0, total, audio_len, freq_len)
    buf[HEADER_LENGTH : HEADER_LENGTH + audio_len] = packet.audio
    offset = HEADER_LENGTH + audio_len
    for freq in packet.frequencies:
        struct.pack_into("<d", buf, offset, float(freq.frequency_hz))
        buf[offset + 8] = int(freq.modulation) & 0xFF
        buf[offset + 9] = int(freq.encryption) & 0xFF
        offset += FREQUENCY_SEGMENT_LENGTH
    struct.pack_into("<IQ", buf, offset, int(packet.unit_id), int(packet.packet_id))
    buf[offset + 12] = int(packet.hops) & 0xFF
    buf[offset + 13 : offset + 13 + GUID_LENGTH] = packet.relay_guid
    buf[offset + 13 + GUID_LENGTH : total] = packet.origin_guid
    return bytes(buf)


def decode_voice_packet(data: bytes) -> VoicePacket:
    """Decode a ciribob 2.4 ``UDPVoicePacket``. Raises ``ValueError`` if malformed."""
    if len(data) < HEADER_LENGTH + FIXED_SEGMENT_LENGTH:
        raise ValueError(f"UDP voice packet too short: {len(data)} bytes")
    total, audio_len, freq_len = struct.unpack_from("<HHH", data, 0)
    if total != len(data):
        raise ValueError(f"packet length header {total} does not match datagram {len(data)}")
    expected = HEADER_LENGTH + audio_len + freq_len + FIXED_SEGMENT_LENGTH
    if expected != total:
        raise ValueError(f"segment lengths {expected} do not match packet length {total}")
    if freq_len % FREQUENCY_SEGMENT_LENGTH != 0:
        raise ValueError(f"frequency segment length {freq_len} is not a multiple of 10")
    audio = bytes(data[HEADER_LENGTH : HEADER_LENGTH + audio_len])
    frequencies: list[VoiceFrequency] = []
    cursor = HEADER_LENGTH + audio_len
    for _ in range(freq_len // FREQUENCY_SEGMENT_LENGTH):
        (hz,) = struct.unpack_from("<d", data, cursor)
        frequencies.append(
            VoiceFrequency(
                frequency_hz=hz,
                modulation=data[cursor + 8],
                encryption=data[cursor + 9],
            )
        )
        cursor += FREQUENCY_SEGMENT_LENGTH
    unit_id, packet_id = struct.unpack_from("<IQ", data, cursor)
    hops = data[cursor + 12]
    relay = bytes(data[cursor + 13 : cursor + 13 + GUID_LENGTH])
    origin = bytes(data[cursor + 13 + GUID_LENGTH : cursor + 13 + 2 * GUID_LENGTH])
    packet = VoicePacket(
        audio=audio,
        frequencies=frequencies,
        unit_id=unit_id,
        packet_id=packet_id,
        hops=hops,
        relay_guid=relay,
        origin_guid=origin,
    )
    if packet.packet_length != total:
        raise ValueError("decoded packet length mismatch")
    return packet


def hz_bits_match(encoded: float, expected_hz: int, *, tol_hz: float = 0.5) -> bool:
    return math.fabs(float(encoded) - float(expected_hz)) <= tol_hz
