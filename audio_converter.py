from __future__ import annotations

import numpy as np

_ENC_EXP_LUT: np.ndarray = np.array(
    [
        0, 0, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 3, 3, 3, 3,
        4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4,
        5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
        5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6, 6,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
    ],
    dtype=np.int32,
)

_DEC_EXP_LUT: np.ndarray = np.array(
    [0, 132, 396, 924, 1980, 4092, 8316, 16764], dtype=np.int32
)

_ULAW_BIAS: np.int32 = np.int32(132)
_ULAW_CLIP: np.int32 = np.int32(32635)
_ULAW_SILENCE = b"\x7f"


def pcm16_to_ulaw(samples: np.ndarray) -> bytes:
    s = samples.astype(np.int32)
    sign = np.where(s < 0, np.int32(0x80), np.int32(0x00))
    s = np.abs(s)
    s = np.minimum(s, _ULAW_CLIP) + _ULAW_BIAS
    exp = _ENC_EXP_LUT[s >> 7]
    mantissa = (s >> (exp + 3)) & np.int32(0x0F)
    ulaw = sign | (exp << 4) | mantissa
    return (~ulaw & 0xFF).astype(np.uint8).tobytes()


def ulaw_to_pcm16(ulaw_bytes: bytes) -> np.ndarray:
    u = (~np.frombuffer(ulaw_bytes, dtype=np.uint8)).astype(np.int32) & 0xFF
    sign = u & 0x80
    exp = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = _DEC_EXP_LUT[exp] + (mantissa << (exp + 3))
    samples = np.where(sign, -magnitude, magnitude)
    return np.clip(samples, -32768, 32767).astype(np.int16)


def _parse_format(fmt: str) -> tuple[str, int]:
    fmt = fmt.lower().strip()
    if "_" in fmt:
        enc, _, rate = fmt.partition("_")
        return enc, int(rate)
    return fmt, 16000


def to_ulaw_8000(audio_bytes: bytes, source_format: str) -> bytes:
    enc, _ = _parse_format(source_format)
    if enc == "ulaw":
        return audio_bytes
    if enc == "pcm":
        samples = np.frombuffer(audio_bytes, dtype=np.int16)
        return pcm16_to_ulaw(samples)
    raise ValueError(f"Unsupported source audio format: '{source_format}'")


def from_ulaw_8000(ulaw_bytes: bytes, target_format: str) -> bytes:
    enc, _ = _parse_format(target_format)
    if enc == "ulaw":
        return ulaw_bytes
    if enc == "pcm":
        return ulaw_to_pcm16(ulaw_bytes).tobytes()
    raise ValueError(f"Unsupported target audio format: '{target_format}'")


def pad_to_multiple(data: bytes, multiple: int = 160) -> bytes:
    remainder = len(data) % multiple
    if remainder:
        data += _ULAW_SILENCE * (multiple - remainder)
    return data


class AudioOutputBuffer:
    def __init__(self, min_chunk: int = 160) -> None:
        self._buf = bytearray()
        self._min_chunk = min_chunk

    def push(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        sendable = (len(self._buf) // self._min_chunk) * self._min_chunk
        if sendable == 0:
            return []
        chunk = bytes(self._buf[:sendable])
        del self._buf[:sendable]
        return [chunk]

    def flush(self) -> bytes | None:
        if not self._buf:
            return None
        data = pad_to_multiple(bytes(self._buf), self._min_chunk)
        self._buf.clear()
        return data

    def clear(self) -> None:
        self._buf.clear()
