"""Blocking media work shared by the two realtime transports."""

from __future__ import annotations

import asyncio
import math
import os
import struct
import tempfile
import wave


def pcm_rms(pcm: bytes) -> float:
    """RMS of little-endian signed PCM16, independent of host byte order."""
    count = len(pcm) // 2
    if not count:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[:count * 2])
    return math.sqrt(sum(value * value for value in samples) / count)


def configured_asr_prefetch(extra: dict, silence_duration: float) -> float:
    """Zero disables speculative ASR; otherwise it must precede the endpoint."""
    value = extra.get("asr_prefetch_silence", 0.0)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not (value == 0 or 0.1 <= value < silence_duration)):
        raise ValueError("asr_prefetch_silence must be 0 or at least 0.1 and below silence_duration")
    return float(value)


class EarlyTranscription:
    """One speculative worker per input; candidates never dispatch an agent turn.

    The speech marker rejects resumption detected by VAD. Checking the actual
    PCM tail also covers explicit mute/end-of-turn before a polling VAD sees
    the newly received speech. Discarding a candidate never cancels its worker.
    """

    def __init__(self, transcribe, sample_rate: int, channels: int):
        self._transcribe = transcribe
        self._frame_bytes = max(2, sample_rate * channels * 2 // 50)
        self._task = None
        self._marker = None
        self._pcm = b""

    def start(self, pcm: bytes | bytearray, speech_marker: float) -> bool:
        if self._task is not None and (not self._task.done() or self._marker == speech_marker):
            return False
        # Copy only when starting a worker, not on every quiet input frame.
        self._pcm, self._marker = bytes(pcm), speech_marker
        self._task = asyncio.create_task(asyncio.to_thread(self._transcribe, self._pcm))
        self._task.add_done_callback(self._observe)
        return True

    @staticmethod
    def _observe(task):
        # A resumed/disconnected turn may never await this candidate.
        if not task.cancelled():
            task.exception()

    def discard(self) -> None:
        self._marker, self._pcm = None, b""

    def take(self, pcm: bytes, speech_marker: float | None, quiet_threshold: float):
        candidate, snapshot, marker = self._task, self._pcm, self._marker
        self.discard()
        if candidate is None or marker is None or speech_marker != marker:
            return None
        shared = min(len(snapshot), len(pcm))
        if snapshot[:shared] != pcm[:shared]:
            return None
        tail = snapshot[shared:] if len(snapshot) > len(pcm) else pcm[shared:]
        if any(pcm_rms(tail[i:i + self._frame_bytes]) > quiet_threshold
               for i in range(0, len(tail), self._frame_bytes)):
            return None
        return candidate


async def transcribe_with_prefetch(pcm, sample_rate, channels, *, prefetched=None, transcribe):
    """Await a valid early result, or preserve the existing batch fallback."""
    if prefetched is not None:
        try:
            # A cancelled gateway waiter must not close the in-flight request
            # or release the worker-owned temporary input early.
            transcript = await asyncio.shield(prefetched)
            if transcript:
                return transcript, True
        except Exception:
            pass  # Speculation failed; the ordinary final transcription gets its turn.
    return await asyncio.to_thread(transcribe, pcm, sample_rate, channels), False


def transcribe_pcm(pcm: bytes, sample_rate: int, channels: int) -> str:
    """Own the temporary input for the entire synchronous transcription.

    Run this in a worker. Cancelling its asyncio waiter must not delete the
    input while the transcription thread is still reading it.
    """
    from tools.transcription_tools import transcribe_audio

    directory = os.path.join(tempfile.gettempdir(), "hermes_livekit")
    os.makedirs(directory, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        suffix=".wav", prefix="utterance_", dir=directory, delete=False
    ) as file:
        path = file.name
    try:
        with wave.open(path, "wb") as file:
            file.setnchannels(channels)
            file.setsampwidth(2)
            file.setframerate(sample_rate)
            file.writeframes(pcm)
        result = transcribe_audio(path)
        if not isinstance(result, dict):
            return ""
        transcript = result.get("transcript") or result.get("text") or ""
        return transcript.strip() if isinstance(transcript, str) else ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
