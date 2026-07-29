"""Streaming PCM should reach LiveKit without whole-reply buffering."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import ClassVar

import pytest

from hermes_livekit import adapter as adapter_module
from hermes_livekit.adapter import LiveKitAdapter, LiveKitStreamingTTSHandle

AudioFormat = adapter_module.AudioFormat
_REAL_RTC = adapter_module.rtc


class _FakeAudioFrame:
    def __init__(self, data, sample_rate, num_channels, samples_per_channel):
        self.data = memoryview(bytes(data))
        self.sample_rate = sample_rate
        self.num_channels = num_channels
        self.samples_per_channel = samples_per_channel


class _FakeResampler:
    instances: ClassVar[list] = []

    def __init__(self, input_rate, output_rate, *, num_channels):
        self.input_rate = input_rate
        self.output_rate = output_rate
        self.num_channels = num_channels
        self.pushed = []
        self.flushed = False
        self.__class__.instances.append(self)

    def push(self, data):
        raw = bytes(data)
        self.pushed.append(raw)
        # The fake only models the rate relationship, not sample interpolation.
        output = raw * (self.output_rate // self.input_rate)
        return [
            _FakeAudioFrame(
                output,
                self.output_rate,
                self.num_channels,
                len(output) // (self.num_channels * 2),
            )
        ]

    def flush(self):
        self.flushed = True
        return []


_FakeRTC = SimpleNamespace(
    AudioFrame=_FakeAudioFrame,
    AudioResampler=_FakeResampler,
)


class _FakeAudioSource:
    def __init__(self):
        self.frames = []
        self.wait_count = 0
        self.clear_count = 0

    async def capture_frame(self, frame):
        self.frames.append(frame)

    async def wait_for_playout(self):
        self.wait_count += 1

    def clear_queue(self):
        self.clear_count += 1


class _BlockingAudioSource(_FakeAudioSource):
    def __init__(self):
        super().__init__()
        self.capture_started = asyncio.Event()
        self.release_capture = asyncio.Event()

    async def capture_frame(self, frame):
        self.frames.append(frame)
        self.capture_started.set()
        await self.release_capture.wait()

    def clear_queue(self):
        super().clear_queue()
        self.release_capture.set()


class _BlockingPlayoutSource(_FakeAudioSource):
    def __init__(self):
        super().__init__()
        self.wait_started = asyncio.Event()
        self.release_wait = asyncio.Event()

    async def wait_for_playout(self):
        self.wait_count += 1
        self.wait_started.set()
        await self.release_wait.wait()


class _Adapter(LiveKitAdapter):
    name = "livekit-test"

    async def _publish_agent_event(self, event_type, payload=None):
        self.events.append(event_type)


def _adapter() -> _Adapter:
    instance = object.__new__(_Adapter)
    instance._room = object()
    instance._audio_source = _FakeAudioSource()
    instance._paused = False
    instance._reconnecting = False
    instance._streaming_tts_available = True
    instance._streaming_tts_lock = asyncio.Lock()
    instance._streaming_tts_handle = None
    instance.events = []
    return instance


@pytest.fixture(autouse=True)
def _patch_rtc(monkeypatch):
    _FakeResampler.instances.clear()
    monkeypatch.setattr(adapter_module, "rtc", _FakeRTC)
    monkeypatch.setattr(adapter_module, "STREAMING_TTS_CONTRACT_AVAILABLE", True)
    monkeypatch.setattr(adapter_module, "TTS_ECHO_GUARD_SECONDS", 0)


def test_streaming_support_requires_connection_and_mono_int16():
    adapter = _adapter()

    assert adapter.supports_streaming_tts("room", AudioFormat())
    assert not adapter.supports_streaming_tts(
        "room", AudioFormat(sample_rate=24000, channels=2, sample_width=2)
    )
    assert not adapter.supports_streaming_tts(
        "room", AudioFormat(sample_rate=24000, channels=1, sample_width=1)
    )

    adapter._audio_source = None
    assert not adapter.supports_streaming_tts("room", AudioFormat())


def test_writes_48khz_pcm_immediately_in_frames_up_to_20ms():
    async def run():
        adapter = _adapter()
        handle = await adapter.begin_streaming_tts(
            "room", AudioFormat(sample_rate=48000, channels=1, sample_width=2)
        )
        assert isinstance(handle, LiveKitStreamingTTSHandle)

        # 50 ms becomes two complete 20 ms frames and one unpadded 10 ms frame.
        await adapter.write_streaming_tts(handle, b"\x01\x00" * 2400)

        source = adapter._audio_source
        assert [frame.samples_per_channel for frame in source.frames] == [960, 960, 480]
        assert handle.audible
        assert adapter._paused
        assert adapter.events == ["agent:speaking-start"]

        await adapter.finish_streaming_tts(handle)

        assert handle.finished
        assert not adapter._paused
        assert source.wait_count == 1
        assert adapter.events == ["agent:speaking-start", "agent:speaking-stop"]

    asyncio.run(run())


def test_24khz_pcm_uses_stateful_livekit_resampler():
    async def run():
        adapter = _adapter()
        handle = await adapter.begin_streaming_tts("room", AudioFormat())
        assert isinstance(handle, LiveKitStreamingTTSHandle)
        assert len(_FakeResampler.instances) == 1

        await adapter.write_streaming_tts(handle, b"\x02\x00" * 1200)
        await adapter.finish_streaming_tts(handle)

        resampler = _FakeResampler.instances[0]
        assert (resampler.input_rate, resampler.output_rate, resampler.num_channels) == (
            24000,
            48000,
            1,
        )
        assert resampler.flushed
        assert [frame.samples_per_channel for frame in adapter._audio_source.frames] == [
            960,
            960,
            480,
        ]

    asyncio.run(run())


def test_split_int16_sample_is_reassembled_without_buffering_reply():
    async def run():
        adapter = _adapter()
        handle = await adapter.begin_streaming_tts(
            "room", AudioFormat(sample_rate=48000, channels=1, sample_width=2)
        )
        assert isinstance(handle, LiveKitStreamingTTSHandle)

        await adapter.write_streaming_tts(handle, b"\x34")
        assert adapter._audio_source.frames == []
        assert not adapter._paused
        handle.audible = True  # Mirrors Hermes's optimistic post-write assignment.
        assert not handle.audible

        await adapter.write_streaming_tts(handle, b"\x12")
        assert len(adapter._audio_source.frames) == 1
        assert adapter._audio_source.frames[0].data.tobytes() == b"\x34\x12"
        assert handle.audible

    asyncio.run(run())


def test_abort_clears_queued_audio_and_is_idempotent():
    async def run():
        adapter = _adapter()
        handle = await adapter.begin_streaming_tts(
            "room", AudioFormat(sample_rate=48000, channels=1, sample_width=2)
        )
        assert isinstance(handle, LiveKitStreamingTTSHandle)
        await adapter.write_streaming_tts(handle, b"\x00\x00" * 960)
        frame_count = len(adapter._audio_source.frames)

        await adapter.abort_streaming_tts(handle, "barge-in")
        await adapter.abort_streaming_tts(handle, "late duplicate")
        await adapter.write_streaming_tts(handle, b"\x00\x00" * 960)

        assert handle.aborted
        assert adapter._streaming_tts_handle is None
        assert not adapter._paused
        assert adapter._audio_source.clear_count == 1
        assert len(adapter._audio_source.frames) == frame_count
        assert adapter.events == ["agent:speaking-start", "agent:speaking-stop"]

    asyncio.run(run())


def test_overlapping_reply_is_declined_and_stale_abort_is_isolated():
    async def run():
        adapter = _adapter()
        first = await adapter.begin_streaming_tts("room", AudioFormat())
        assert isinstance(first, LiveKitStreamingTTSHandle)
        assert await adapter.begin_streaming_tts("room", AudioFormat()) is None

        await adapter.finish_streaming_tts(first)
        second = await adapter.begin_streaming_tts("room", AudioFormat())
        assert second is not None

        await adapter.abort_streaming_tts(first, "late abort")
        assert adapter._streaming_tts_handle is second
        assert adapter._audio_source.clear_count == 0

    asyncio.run(run())


def test_interrupted_finish_uses_abort_semantics():
    async def run():
        adapter = _adapter()
        handle = await adapter.begin_streaming_tts(
            "room", AudioFormat(sample_rate=48000, channels=1, sample_width=2)
        )
        assert isinstance(handle, LiveKitStreamingTTSHandle)
        await adapter.write_streaming_tts(handle, b"\x00\x00" * 960)

        await adapter.finish_streaming_tts(handle, interrupted=True)

        assert handle.aborted
        assert adapter._audio_source.clear_count == 1
        assert adapter._audio_source.wait_count == 0

    asyncio.run(run())


def test_abort_is_not_blocked_by_audio_source_backpressure():
    async def run():
        adapter = _adapter()
        source = _BlockingAudioSource()
        adapter._audio_source = source
        handle = await adapter.begin_streaming_tts(
            "room", AudioFormat(sample_rate=48000, channels=1, sample_width=2)
        )
        assert isinstance(handle, LiveKitStreamingTTSHandle)

        write_task = asyncio.create_task(
            adapter.write_streaming_tts(handle, b"\x00\x00" * 960)
        )
        await asyncio.wait_for(source.capture_started.wait(), timeout=0.5)

        # Barge-in must clear the queue even while capture_frame is waiting for
        # LiveKit's queue to drain.
        await asyncio.wait_for(
            adapter.abort_streaming_tts(handle, "barge-in"), timeout=0.5
        )
        with pytest.raises(RuntimeError, match="aborted during LiveKit frame capture"):
            await asyncio.wait_for(write_task, timeout=0.5)

        assert handle.aborted
        assert not handle.audible
        assert source.clear_count == 1

    asyncio.run(run())


def test_real_livekit_resampler_feeds_audio_source(monkeypatch):
    """Exercise the installed LiveKit SDK resampler, not only the fake."""
    monkeypatch.setattr(adapter_module, "rtc", _REAL_RTC)

    async def run():
        adapter = _adapter()
        handle = await adapter.begin_streaming_tts("room", AudioFormat())
        assert isinstance(handle, LiveKitStreamingTTSHandle)

        # 100 ms at 24 kHz should remain 100 ms after conversion to 48 kHz.
        await adapter.write_streaming_tts(handle, b"\x01\x00" * 2400)
        await adapter.finish_streaming_tts(handle)

        assert handle.audible
        assert sum(
            frame.samples_per_channel for frame in adapter._audio_source.frames
        ) == 4800

    asyncio.run(run())


def test_finishing_reply_keeps_track_ownership_until_playout_ends():
    async def run():
        adapter = _adapter()
        source = _BlockingPlayoutSource()
        adapter._audio_source = source
        first = await adapter.begin_streaming_tts(
            "room", AudioFormat(sample_rate=48000, channels=1, sample_width=2)
        )
        assert isinstance(first, LiveKitStreamingTTSHandle)
        await adapter.write_streaming_tts(first, b"\x00\x00" * 960)

        finish_task = asyncio.create_task(adapter.finish_streaming_tts(first))
        await asyncio.wait_for(source.wait_started.wait(), timeout=0.5)
        assert first.finishing
        assert await adapter.begin_streaming_tts("room", AudioFormat()) is None

        source.release_wait.set()
        await asyncio.wait_for(finish_task, timeout=0.5)
        assert first.finished
        assert not first.finishing
        assert await adapter.begin_streaming_tts("room", AudioFormat()) is not None

    asyncio.run(run())


def test_teardown_recheck_declines_begin_already_waiting_on_lock():
    async def run():
        adapter = _adapter()
        await adapter._streaming_tts_lock.acquire()
        begin_task = asyncio.create_task(
            adapter.begin_streaming_tts("room", AudioFormat())
        )
        await asyncio.sleep(0)

        adapter._streaming_tts_available = False
        adapter._streaming_tts_lock.release()

        assert await asyncio.wait_for(begin_task, timeout=0.5) is None
        assert adapter._streaming_tts_handle is None

    asyncio.run(run())


def test_unexpected_disconnect_aborts_stream_before_reconnect():
    async def run():
        adapter = _adapter()
        adapter._running = True
        adapter._graceful_leave = False
        reconnected = []

        async def reconnect():
            assert adapter._reconnecting
            assert await adapter.begin_streaming_tts("room", AudioFormat()) is None
            reconnected.append(True)
            adapter._reconnecting = False

        adapter._reconnect_loop = reconnect
        handle = await adapter.begin_streaming_tts("room", AudioFormat())
        assert isinstance(handle, LiveKitStreamingTTSHandle)

        adapter._on_disconnected("network lost")
        assert adapter._reconnecting
        assert await adapter.begin_streaming_tts("room", AudioFormat()) is None
        await adapter._connect_task

        assert handle.aborted
        assert adapter._streaming_tts_handle is None
        assert adapter._audio_source.clear_count == 1
        assert not adapter._reconnecting
        assert reconnected == [True]

    asyncio.run(run())


def test_streaming_restores_client_pause_intent():
    async def run():
        adapter = _adapter()
        adapter._paused = True
        first = await adapter.begin_streaming_tts(
            "room", AudioFormat(sample_rate=48000, channels=1, sample_width=2)
        )
        assert isinstance(first, LiveKitStreamingTTSHandle)
        await adapter.write_streaming_tts(first, b"\x00\x00" * 960)
        await adapter._handle_client_control({"action": "resume"}, "client")
        assert adapter._paused  # TTS still owns the capture pause.
        await adapter.finish_streaming_tts(first)
        assert not adapter._paused

        second = await adapter.begin_streaming_tts(
            "room", AudioFormat(sample_rate=48000, channels=1, sample_width=2)
        )
        assert isinstance(second, LiveKitStreamingTTSHandle)
        await adapter.write_streaming_tts(second, b"\x00\x00" * 960)
        await adapter._handle_client_control({"action": "pause"}, "client")
        await adapter.finish_streaming_tts(second)
        assert adapter._paused

    asyncio.run(run())


def test_hermes_consumer_keeps_pre_audible_fallback_after_abort(monkeypatch):
    try:
        from gateway import streaming_tts_consumer as consumer_module
        from tools import tts_streaming
    except ImportError:
        pytest.skip("Hermes streaming-TTS consumer is not available in this release")

    class _Streamer:
        sample_rate = 48000
        channels = 1
        sample_width = 2

        def stream(self, text):
            del text
            yield b"\x00\x00" * 960

    monkeypatch.setattr(
        tts_streaming, "resolve_streaming_provider", lambda config: _Streamer()
    )

    async def run():
        adapter = _adapter()
        source = _BlockingAudioSource()
        adapter._audio_source = source
        consumer = consumer_module.StreamingTTSConsumer(
            adapter=adapter,
            chat_id="room",
            tts_config={},
            loop=asyncio.get_running_loop(),
        )

        task = consumer.start()
        consumer.on_delta("A complete sentence.")
        consumer.finish()
        await asyncio.wait_for(source.capture_started.wait(), timeout=0.5)
        handle = adapter._streaming_tts_handle
        consumer.abort("interrupted")
        await asyncio.wait_for(task, timeout=0.5)

        assert handle is not None
        assert handle.aborted
        assert not consumer.audible

    asyncio.run(run())


def test_hermes_consumer_keeps_fallback_for_remainder_only_failure(monkeypatch):
    try:
        from gateway import streaming_tts_consumer as consumer_module
        from tools import tts_streaming
    except ImportError:
        pytest.skip("Hermes streaming-TTS consumer is not available in this release")

    class _OddByteStreamer:
        sample_rate = 48000
        channels = 1
        sample_width = 2

        def stream(self, text):
            del text
            yield b"\x34"
            raise RuntimeError("provider failed after an incomplete sample")

    monkeypatch.setattr(
        tts_streaming,
        "resolve_streaming_provider",
        lambda config: _OddByteStreamer(),
    )

    async def run():
        adapter = _adapter()
        consumer = consumer_module.StreamingTTSConsumer(
            adapter=adapter,
            chat_id="room",
            tts_config={},
            loop=asyncio.get_running_loop(),
        )

        task = consumer.start()
        consumer.on_delta("A complete sentence.")
        consumer.finish()
        await asyncio.wait_for(task, timeout=0.5)

        assert adapter._audio_source.frames == []
        assert not consumer.audible
        assert not consumer.suppress_whole_file

    asyncio.run(run())