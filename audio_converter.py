from __future__ import annotations

_ULAW_SILENCE = b"\x7f"


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
