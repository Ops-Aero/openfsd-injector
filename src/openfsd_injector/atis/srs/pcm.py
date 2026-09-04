"""Load a cached ATIS WAV as 16 kHz s16le mono for SRS Opus."""

from __future__ import annotations

import array
import wave
from pathlib import Path

from .opus import SRS_OPUS_RATE


def load_wav_pcm16_mono_16k(path: Path) -> bytes:
    """Read a PCM WAV and resample to 16 kHz mono s16le.

    Compressed (non-PCM) WAVs are rejected — TTS writes uncompressed PCM.
    """
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
        comptype = wav.getcomptype()
    if comptype != "NONE":
        raise ValueError(f"{path} is a compressed WAV ({comptype}); SRS needs PCM")
    if channels < 1:
        raise ValueError(f"{path} has no audio channels")
    frames = _to_s16le(frames, width)
    frames = _to_mono_s16(frames, channels)
    if rate != SRS_OPUS_RATE:
        frames = _resample_s16_mono(frames, rate, SRS_OPUS_RATE)
    return frames


def _to_s16le(frames: bytes, width: int) -> bytes:
    if width == 2:
        return frames
    if width == 1:
        out = array.array("h")
        for byte in frames:
            out.append((byte - 128) * 256)
        return out.tobytes()
    if width == 4:
        src = array.array("i")
        src.frombytes(frames)
        out = array.array("h")
        for sample in src:
            clamped = max(-32768, min(32767, sample >> 16))
            out.append(clamped)
        return out.tobytes()
    raise ValueError(f"unsupported PCM sample width {width}")


def _to_mono_s16(frames: bytes, channels: int) -> bytes:
    if channels == 1:
        return frames
    if channels < 1:
        raise ValueError(f"unsupported channel count {channels}")
    samples = array.array("h")
    samples.frombytes(frames)
    out = array.array("h")
    for i in range(0, len(samples), channels):
        chunk = samples[i : i + channels]
        if len(chunk) < channels:
            break
        out.append(int(sum(chunk) / channels))
    return out.tobytes()


def _resample_s16_mono(frames: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate:
        return frames
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError("sample rates must be positive")
    src = array.array("h")
    src.frombytes(frames)
    if not src:
        return b""
    n_src = len(src)
    n_dst = max(1, int(round(n_src * dst_rate / src_rate)))
    out = array.array("h")
    if n_src == 1:
        out.extend([src[0]] * n_dst)
        return out.tobytes()
    last = n_src - 1
    for i in range(n_dst):
        pos = i * last / (n_dst - 1)
        lo = int(pos)
        hi = min(lo + 1, last)
        frac = pos - lo
        sample = src[lo] + (src[hi] - src[lo]) * frac
        out.append(int(round(sample)))
    return out.tobytes()
