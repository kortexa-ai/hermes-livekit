"""Blocking media work shared by the two realtime transports."""

from __future__ import annotations

import os
import tempfile
import wave


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
