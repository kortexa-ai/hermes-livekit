"""Synthetic media worker coverage; no transcription service is called."""

import asyncio
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import wave

import pytest

from hermes_livekit.media import transcribe_pcm


@pytest.mark.parametrize("fail", [False, True])
def test_transcription_worker_owns_and_removes_wav(monkeypatch, tmp_path, fail):
    paths = []

    def transcribe(path):
        paths.append(Path(path))
        with wave.open(path, "rb") as file:
            assert file.getframerate() == 48_000
            assert file.getnchannels() == 1
            assert file.readframes(2) == b"\x01\x00\x02\x00"
        if fail:
            raise RuntimeError("synthetic transcription failure")
        return {"text": " hello "}

    monkeypatch.setattr("hermes_livekit.media.tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", SimpleNamespace(transcribe_audio=transcribe))
    if fail:
        with pytest.raises(RuntimeError, match="synthetic"):
            transcribe_pcm(b"\x01\x00\x02\x00", 48_000, 1)
    else:
        assert transcribe_pcm(b"\x01\x00\x02\x00", 48_000, 1) == "hello"
    assert len(paths) == 1
    assert not paths[0].exists()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_delete_a_running_workers_input(monkeypatch, tmp_path):
    entered = asyncio.Event()
    release = threading.Event()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    paths = []
    errors = []

    def transcribe(path):
        paths.append(Path(path))
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=5)
        assert Path(path).exists()
        return {"text": "hello"}

    def worker():
        try:
            return transcribe_pcm(b"\x00\x00", 48_000, 1)
        except BaseException as exc:
            errors.append(exc)
            raise
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr("hermes_livekit.media.tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "tools.transcription_tools", SimpleNamespace(transcribe_audio=transcribe))
    task = asyncio.create_task(asyncio.to_thread(worker))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert paths[0].exists()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), timeout=2)
    assert not paths[0].exists()
    assert errors == []
