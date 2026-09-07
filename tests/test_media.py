"""Synthetic media worker coverage; no transcription service is called."""

import asyncio
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import wave

import pytest

from hermes_livekit.media import transcribe_pcm


@pytest.mark.parametrize("fail", [False, True, "empty"])
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


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["quiet", "speech", "different_turn", "different_prefix", "trimmed_quiet"])
async def test_early_transcription_reuses_only_the_same_speech(change):
    from hermes_livekit.media import EarlyTranscription

    calls = []
    speech = b"\x00\x10" * 960
    quiet = b"\x10\x00" * 960
    prefetch = EarlyTranscription(lambda pcm: calls.append(pcm) or "hello", 48_000, 1)
    snapshot = speech + quiet
    mutable = bytearray(snapshot)
    assert prefetch.start(mutable, 1.0)
    mutable.clear()
    await prefetch._task
    assert not prefetch.start(snapshot, 1.0)
    final = {"quiet": snapshot + quiet, "speech": snapshot + speech,
             "different_turn": snapshot, "different_prefix": quiet + speech,
             "trimmed_quiet": speech}[change]
    candidate = prefetch.take(final, 2.0 if change == "different_turn" else 1.0, 220)
    if change in {"quiet", "trimmed_quiet"}:
        assert await candidate == "hello"
    else:
        assert candidate is None
    assert prefetch.take(final, 1.0, 220) is None
    assert calls == [snapshot]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_early_worker_is_bounded_and_outlives_cancelled_waiter(fail):
    from hermes_livekit.media import EarlyTranscription, transcribe_with_prefetch

    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def worker(pcm):
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(timeout=5)
            if fail is True:
                raise RuntimeError("synthetic failure")
            if fail == "empty":
                return ""
            return "first"
        finally:
            loop.call_soon_threadsafe(finished.set)

    prefetch = EarlyTranscription(worker, 48_000, 1)
    pcm = b"\x00\x10" * 960
    prefetch.start(pcm, 1.0)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert not prefetch.start(pcm, 2.0)
        candidate = prefetch.take(pcm, 1.0, 220)
        waiter = asyncio.create_task(transcribe_with_prefetch(
            pcm, 48_000, 1, prefetched=candidate, transcribe=lambda *args: "fallback",
        ))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert not candidate.done()
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 2)
    result = await transcribe_with_prefetch(
        pcm, 48_000, 1, prefetched=candidate, transcribe=lambda *args: "fallback",
    )
    assert result == (("fallback", False) if fail else ("first", True))


@pytest.mark.parametrize("value,valid", [
    (0, True), (0.35, True), (False, False), (float("nan"), False),
    (float("inf"), False), (0.05, False), (0.7, False), ("0.35", False),
])
def test_prefetch_config_is_opt_in_and_precedes_endpoint(value, valid):
    from hermes_livekit.media import configured_asr_prefetch
    assert configured_asr_prefetch({}, 0.7) == 0
    if valid:
        assert configured_asr_prefetch({"asr_prefetch_silence": value}, 0.7) == value
    else:
        with pytest.raises(ValueError):
            configured_asr_prefetch({"asr_prefetch_silence": value}, 0.7)
