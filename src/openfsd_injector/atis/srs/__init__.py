"""Ciribob SRS 2.x ATIS transmitter (Linux/Docker, not AFV, not ExternalAudio.exe).

One process, one TCP+UDP session, many radios — the layout SRS 2.x accepts
for an External Audio / EAM-style client. Expected server image:

``flisher/dcs-srs-server:ciribob-2.4.0.0`` (opsaero-main ``srs`` on 5002).

Empty ``SRS_HOST`` leaves this package unused. A down or missing SRS server
never takes the injector with it. ``SRS_TX=0`` keeps TCP radio presence and
HTTP audio, and does not pretend UDP voice works.
"""

from .bridge import SrsBridge, SrsTxStatus
from .opus import opus_available
from .protocol import (
    GUID_LENGTH,
    SRS_CLIENT_VERSION,
    decode_voice_packet,
    encode_voice_packet,
    new_guid,
)

__all__ = [
    "GUID_LENGTH",
    "SRS_CLIENT_VERSION",
    "SrsBridge",
    "SrsTxStatus",
    "decode_voice_packet",
    "encode_voice_packet",
    "new_guid",
    "opus_available",
]
