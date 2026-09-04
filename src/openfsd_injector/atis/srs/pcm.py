"""Load a cached ATIS WAV as 16 kHz s16le mono for SRS Opus."""

from __future__ import annotations

import audioop
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
    if width < 1:
        raise ValueError(f"{path} has an invalid sample width")
    if width != 2:
        frames = audioop.lin2lin(frames, width, 2)
        width = 2
    if channels == 2:
        frames = audioop.tomono(frames, width, 0.5, 0.5)
    elif channels != 1:
        raise ValueError(f"{path} has {channels} channels; only mono/stereo WAV is supported")
    if rate != SRS_OPUS_RATE:
        frames, _state = audioop.ratecv(frames, width, 1, rate, SRS_OPUS_RATE, None)
    return frames
