"""libopus encoder for SRS (16 kHz mono, 40 ms VoIP frames).

SRS 2.x voice is raw Opus frames, not Ogg. This wraps the system
``libopus`` via ctypes so the image stays Linux/Docker-first — no
Windows-only ExternalAudio.exe.

If libopus cannot be loaded, :func:`opus_available` is false and the
bridge must not claim UDP TX works.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from typing import Protocol

log = logging.getLogger(__name__)

OPUS_OK = 0
OPUS_APPLICATION_VOIP = 2048
SRS_OPUS_RATE = 16_000
SRS_OPUS_CHANNELS = 1
SRS_FRAME_MS = 40
SRS_FRAME_SAMPLES = SRS_OPUS_RATE * SRS_FRAME_MS // 1000  # 640
SRS_FRAME_BYTES = SRS_FRAME_SAMPLES * 2  # s16le
_MAX_PACKET = 4000


class OpusUnavailable(RuntimeError):
    """libopus is not loaded; UDP TX must be marked unavailable."""


class OpusEncoder(Protocol):
    def encode_frame(self, pcm_s16le: bytes) -> bytes: ...

    def close(self) -> None: ...


_LIB: ctypes.CDLL | None | bool = False


def _load_libopus() -> ctypes.CDLL | None:
    global _LIB
    if _LIB is not False:
        return _LIB if _LIB is not None else None
    candidates: list[str] = []
    found = ctypes.util.find_library("opus")
    if found:
        candidates.append(found)
    candidates.extend(["libopus.so.0", "libopus.so", "opus"])
    for name in candidates:
        try:
            lib = ctypes.CDLL(name)
        except OSError:
            continue
        lib.opus_encoder_create.restype = ctypes.c_void_p
        lib.opus_encoder_create.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        lib.opus_encode.restype = ctypes.c_int
        lib.opus_encode.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_int32,
        ]
        lib.opus_encoder_destroy.restype = None
        lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
        _LIB = lib
        return lib
    _LIB = None
    return None


def opus_available() -> bool:
    return _load_libopus() is not None


class LibOpusEncoder:
    """Real libopus encoder. Create only when :func:`opus_available` is true."""

    def __init__(
        self,
        *,
        rate: int = SRS_OPUS_RATE,
        channels: int = SRS_OPUS_CHANNELS,
        application: int = OPUS_APPLICATION_VOIP,
    ) -> None:
        lib = _load_libopus()
        if lib is None:
            raise OpusUnavailable("libopus is not installed (need libopus0 on Debian/Ubuntu)")
        err = ctypes.c_int(0)
        handle = lib.opus_encoder_create(rate, channels, application, ctypes.byref(err))
        if not handle or err.value != OPUS_OK:
            raise OpusUnavailable(f"opus_encoder_create failed ({err.value})")
        self._lib = lib
        self._handle = handle
        self._rate = rate
        self._channels = channels

    def encode_frame(self, pcm_s16le: bytes) -> bytes:
        if len(pcm_s16le) != SRS_FRAME_BYTES:
            raise ValueError(f"Opus frame must be {SRS_FRAME_BYTES} bytes s16le, got {len(pcm_s16le)}")
        if not self._handle:
            raise OpusUnavailable("encoder has been closed")
        pcm = ctypes.create_string_buffer(pcm_s16le)
        out = ctypes.create_string_buffer(_MAX_PACKET)
        n = self._lib.opus_encode(
            self._handle,
            pcm,
            SRS_FRAME_SAMPLES,
            out,
            _MAX_PACKET,
        )
        if n < 0:
            raise RuntimeError(f"opus_encode failed ({n})")
        return out.raw[:n]

    def close(self) -> None:
        if self._handle:
            self._lib.opus_encoder_destroy(self._handle)
            self._handle = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


class FakeOpusEncoder:
    """Deterministic stand-in for tests. Not a substitute for on-air TX."""

    def encode_frame(self, pcm_s16le: bytes) -> bytes:
        if len(pcm_s16le) != SRS_FRAME_BYTES:
            raise ValueError(f"Opus frame must be {SRS_FRAME_BYTES} bytes s16le, got {len(pcm_s16le)}")
        # Distinct from silence so tests can see a payload moved.
        digest = sum(pcm_s16le) & 0xFFFF
        return b"FAKEOPUS" + digest.to_bytes(2, "little") + pcm_s16le[:8]

    def close(self) -> None:
        return None


def open_encoder() -> OpusEncoder:
    return LibOpusEncoder()


def encode_pcm_frames(pcm_s16le: bytes, encoder: OpusEncoder) -> list[bytes]:
    """Split 16 kHz s16le mono PCM into 40 ms Opus frames (last frame zero-padded)."""
    if not pcm_s16le:
        return []
    frames: list[bytes] = []
    offset = 0
    while offset < len(pcm_s16le):
        chunk = pcm_s16le[offset : offset + SRS_FRAME_BYTES]
        if len(chunk) < SRS_FRAME_BYTES:
            chunk = chunk + b"\x00" * (SRS_FRAME_BYTES - len(chunk))
        frames.append(encoder.encode_frame(chunk))
        offset += SRS_FRAME_BYTES
    return frames
