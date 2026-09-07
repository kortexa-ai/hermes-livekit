"""Real PCM conversion and Hermes consumer integration, with fake output devices."""

from __future__ import annotations

import asyncio
import math
import os
import struct
import time
from types import SimpleNamespace

import pytest

from gateway.platforms.base import AudioFormat
from gateway.streaming_tts_consumer import StreamingTTSConsumer
from hermes_livekit.adapter import LiveKitAdapter
from hermes_livekit.realtime_protocol import RealtimeProtocol
from hermes_livekit.realtime_webrtc import QueuedAudioTrack, RealtimeWebRTCAdapter
from hermes_livekit.streaming_tts import FRAME_BYTES, PcmFramer


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def tone(rate=24000, seconds=0.2):
    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                    for i in range(int(rate * seconds)))


class Sink:
    def __init__(self):
        self.frames = []
        self.first = asyncio.Event()
        self.played = asyncio.Event()
        self.played.set()
        self.clears = 0

    async def enqueue_pcm(self, pcm):
        self.frames.append(pcm)
        self.first.set()

    async def capture_frame(self, frame):
        await self.enqueue_pcm(bytes(frame.data))

    async def drained(self):
        await self.played.wait()

    async def wait_for_playout(self):
        await self.drained()

    def clear(self):
        self.clears += 1
        self.played.set()

    clear_queue = clear


def voice_adapter(kind):
    events = []

    async def publish(event, recipient):
        events.append(event)
        return True

    protocol = RealtimeProtocol(session_id="fixture", model="test", voice="test", publish=publish)
    sink = Sink()
    if kind == "realtime":
        adapter = object.__new__(RealtimeWebRTCAdapter)
        owner = SimpleNamespace(protocol=protocol, output_track=sink, closed=False, paused=False)
        adapter._calls = {"chat": owner}
    else:
        adapter = object.__new__(LiveKitAdapter)
        adapter._room = owner = object()
        adapter._room_name = "chat"
        adapter._audio_source = sink
        adapter._realtime_protocol = protocol
        adapter._paused = False
    adapter._tts_streams = {}
    adapter._tts_lifecycle_lock = asyncio.Lock()
    adapter._tts_echo_guard = 0
    return adapter, owner, protocol, sink, events


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_provider_pcm_time_precedes_onset_gating_and_excludes_stale_handles(kind, caplog):
    caplog.set_level("INFO", logger="gateway.platforms.livekit.streaming_tts")
    adapter, owner, protocol, sink, events = voice_adapter(kind)
    adapter._tts_trim_leading_silence = True
    handle = await adapter.begin_streaming_tts("chat", AudioFormat(sample_rate=48000))
    await adapter.write_streaming_tts(handle, b"")
    assert handle.first_input_at is None
    await adapter.write_streaming_tts(handle, b"\0" * FRAME_BYTES)
    assert handle.first_input_at >= handle.opened_at
    assert not handle.audible and not sink.frames
    first = handle.first_input_at
    await adapter.write_streaming_tts(handle, b"\0" * FRAME_BYTES)
    assert handle.first_input_at == first
    assert caplog.text.count("streaming TTS first provider PCM") == 1
    replacement = await adapter.begin_streaming_tts("chat", AudioFormat(sample_rate=48000))
    await adapter.write_streaming_tts(handle, tone(rate=48000))
    assert replacement.first_input_at is None
    await adapter.abort_streaming_tts(replacement)


@pytest.mark.parametrize("adapter_type", [RealtimeWebRTCAdapter, LiveKitAdapter])
def test_onset_setting_is_opt_in_and_requires_a_boolean(adapter_type):
    from gateway.config import PlatformConfig
    from gateway.platform_registry import PlatformEntry, platform_registry

    name = "realtime" if adapter_type is RealtimeWebRTCAdapter else "livekit"
    platform_registry.register(PlatformEntry(
        # Register a test-owned factory, not the plugin class: earlier plugin
        # discovery tests can bind that class to a different profile scope.
        name=name, label=name, adapter_factory=lambda config: adapter_type(config),
        check_fn=lambda: True,
    ))
    try:
        assert platform_registry.is_registered(name)
        assert not adapter_type(PlatformConfig())._tts_trim_leading_silence
        for value in (False, True):
            adapter = adapter_type(PlatformConfig(extra={"tts_trim_leading_silence": value}))
            assert adapter._tts_trim_leading_silence is value
        for value in ("true", "false", 1, 0, None, 0.5):
            with pytest.raises(ValueError, match="tts_trim_leading_silence"):
                adapter_type(PlatformConfig(extra={"tts_trim_leading_silence": value}))
    finally:
        platform_registry.unregister(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
@pytest.mark.parametrize("enabled", [False, True])
async def test_optional_onset_trim_preserves_pcm_across_provider_chunks(kind, enabled):
    adapter, owner, protocol, sink, events = voice_adapter(kind)
    adapter._tts_trim_leading_silence = enabled
    handle = await adapter.begin_streaming_tts("chat", AudioFormat(sample_rate=48000))
    source = b"\0" * (FRAME_BYTES * 30) + tone(rate=48000) + b"\0" * (FRAME_BYTES * 10)
    for offset in range(0, len(source), 137):
        await adapter.write_streaming_tts(handle, source[offset:offset + 137])
    await adapter.finish_streaming_tts(handle)
    removed = 26 * FRAME_BYTES if enabled else 0
    assert b"".join(sink.frames) == source[removed:]
    assert handle.audible and handle.finished
    assert sum(e["type"] == "output_audio_buffer.started" for e in events) == 1
    assert not adapter._tts_streams


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_quiet_prefix_failure_still_allows_whole_file_fallback(kind, monkeypatch):
    adapter, owner, protocol, sink, events = voice_adapter(kind)
    adapter._tts_trim_leading_silence = True

    class Provider:
        sample_rate, channels, sample_width = 48000, 1, 2

        def stream(self, text):
            yield b"\0" * FRAME_BYTES * 20
            raise RuntimeError("failed before speech")

    monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda config: Provider())
    consumer = StreamingTTSConsumer(adapter, "chat", {}, asyncio.get_running_loop())
    task = consumer.start()
    consumer.on_delta("The quiet prefix is not playable speech. ")
    consumer.finish()
    await asyncio.wait_for(task, 5)
    assert not consumer.audible and not consumer.suppress_whole_file
    assert not sink.frames and not events
    assert not adapter._tts_streams
    assert not (owner.paused if kind == "realtime" else adapter._paused)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_cancelled_prefix_does_not_leak_into_replacement(kind):
    adapter, owner, protocol, sink, events = voice_adapter(kind)
    adapter._tts_trim_leading_silence = True
    old = await adapter.begin_streaming_tts("chat", AudioFormat(sample_rate=48000))
    await adapter.write_streaming_tts(old, b"\0" * FRAME_BYTES * 20)
    assert not events and not old.audible
    await adapter.abort_streaming_tts(old)
    assert not list(old.leading_silence.finish())
    new = await adapter.begin_streaming_tts("chat", AudioFormat(sample_rate=48000))
    await adapter.finish_streaming_tts(old)
    speech = tone(rate=48000)
    await adapter.write_streaming_tts(new, speech)
    await adapter.finish_streaming_tts(new)
    assert b"".join(sink.frames) == speech


@pytest.mark.parametrize("rate", [8000, 16000, 24000, 44100, 48000])
def test_resampling_is_independent_of_http_chunk_boundaries(rate):
    pcm = tone(rate)
    whole, split = PcmFramer(AudioFormat(sample_rate=rate)), PcmFramer(AudioFormat(sample_rate=rate))
    expected = list(whole.feed(pcm)) + list(whole.finish())
    actual = []
    for offset in range(0, len(pcm), 137):
        actual.extend(split.feed(pcm[offset:offset + 137]))
    actual.extend(split.finish())
    assert b"".join(actual) == b"".join(expected)
    assert all(len(frame) == FRAME_BYTES for frame in actual)
    assert len(b"".join(actual)) == 48000 * 2 // 5


def test_truncated_pcm_sample_is_not_silently_padded():
    framer = PcmFramer(AudioFormat())
    assert list(framer.feed(b"\x01")) == []
    with pytest.raises(ValueError, match="incomplete PCM16"):
        list(framer.finish())


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_stream_starts_before_text_finishes_and_completes_one_response(kind, monkeypatch):
    adapter, owner, protocol, sink, events = voice_adapter(kind)

    class Provider:
        sample_rate, channels, sample_width = 24000, 1, 2

        def stream(self, text):
            pcm = tone()
            for offset in range(0, len(pcm), 137):
                yield pcm[offset:offset + 137]

    monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda config: Provider())
    consumer = StreamingTTSConsumer(adapter, "chat", {}, asyncio.get_running_loop())
    task = consumer.start()
    try:
        consumer.on_delta("This first sentence can play now. ")
        await asyncio.wait_for(sink.first.wait(), 5)
        assert not consumer.done
        assert protocol.active_response_id is not None
        assert owner.paused if kind == "realtime" else adapter._paused
        consumer.on_delta("This second sentence follows.")
        consumer.finish()
        assert await consumer.wait_complete(timeout=5)
        assert consumer.suppress_whole_file
        assert not adapter._tts_streams
        assert not (owner.paused if kind == "realtime" else adapter._paused)
        assert not any(event["type"] == "response.done" for event in events)
        await protocol.assistant_transcript("Both sentences.")
        await protocol.output_stopped()
        assert sum(e["type"] == "response.created" for e in events) == 1
        assert sum(e["type"] == "response.done" for e in events) == 1
        assert sum(e["type"] == "output_audio_buffer.started" for e in events) == 1
        assert sum(e["type"] == "output_audio_buffer.stopped" for e in events) == 1
        assert all(len(frame) == FRAME_BYTES for frame in sink.frames)
    finally:
        consumer.abort()
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
@pytest.mark.parametrize("partial", [False, True])
async def test_provider_failure_falls_back_only_before_pcm_delivery(kind, partial, monkeypatch):
    adapter, owner, protocol, sink, events = voice_adapter(kind)

    class Provider:
        sample_rate, channels, sample_width = 24000, 1, 2

        def stream(self, text):
            if partial:
                yield tone()
            raise RuntimeError("synthetic provider failure")

    monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda config: Provider())
    consumer = StreamingTTSConsumer(adapter, "chat", {}, asyncio.get_running_loop())
    task = consumer.start()
    consumer.on_delta("A sentence with an injected failure. ")
    consumer.finish()
    await asyncio.wait_for(task, 5)
    assert not consumer.completed
    assert consumer.suppress_whole_file is partial
    assert consumer.partial is partial
    assert not adapter._tts_streams
    assert not (owner.paused if kind == "realtime" else adapter._paused)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_abort_is_idempotent_and_late_old_chunks_cannot_touch_new_stream(kind):
    adapter, owner, protocol, sink, events = voice_adapter(kind)
    old = await adapter.begin_streaming_tts("chat", AudioFormat())
    await adapter.write_streaming_tts(old, tone())
    await adapter.abort_streaming_tts(old)
    await protocol.response_cancelled()
    new = await adapter.begin_streaming_tts("chat", AudioFormat())
    await adapter.write_streaming_tts(new, tone())
    clears, frames = sink.clears, len(sink.frames)
    await adapter.abort_streaming_tts(old)
    await adapter.write_streaming_tts(old, tone())
    await adapter.finish_streaming_tts(old)
    assert sink.clears == clears and len(sink.frames) == frames
    assert owner.paused if kind == "realtime" else adapter._paused
    assert adapter._tts_streams["chat"] is new
    await adapter.finish_streaming_tts(new)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_abort_racing_output_started_does_not_leave_playback_active(kind):
    adapter, owner, protocol, sink, events = voice_adapter(kind)
    handle = await adapter.begin_streaming_tts("chat", AudioFormat())
    starting, release = asyncio.Event(), asyncio.Event()
    original_start = protocol.output_started

    async def start():
        await original_start()
        starting.set()
        await release.wait()

    protocol.output_started = start
    writing = asyncio.create_task(adapter.write_streaming_tts(handle, tone()))
    await asyncio.wait_for(starting.wait(), 5)
    aborting = asyncio.create_task(adapter.abort_streaming_tts(handle))
    # Let cancellation race the in-flight start notification.
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(asyncio.gather(writing, aborting, return_exceptions=True), 5)
    assert handle.aborted and handle.finished
    assert not adapter._tts_streams
    assert not (owner.paused if kind == "realtime" else adapter._paused)
    assert sum(e["type"] == "output_audio_buffer.started" for e in events) == 1
    assert sum(e["type"] == "output_audio_buffer.stopped" for e in events) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_finish_waits_for_playout_and_abort_releases_waiter(kind):
    adapter, owner, protocol, sink, events = voice_adapter(kind)
    handle = await adapter.begin_streaming_tts("chat", AudioFormat())
    await adapter.write_streaming_tts(handle, tone())
    sink.played.clear()
    waiting = asyncio.Event()
    original_drain = adapter._tts_drain

    async def drain(h):
        waiting.set()
        await original_drain(h)

    adapter._tts_drain = drain
    task = asyncio.create_task(adapter.finish_streaming_tts(handle))
    await asyncio.wait_for(waiting.wait(), 5)
    assert owner.paused if kind == "realtime" else adapter._paused
    assert not task.done()
    await adapter.abort_streaming_tts(handle)
    await asyncio.wait_for(task, 5)
    assert handle.aborted and handle.finished
    assert not adapter._tts_streams


@pytest.mark.asyncio
async def test_direct_queue_bounds_backpressure_and_clear_releases_old_producer():
    track = QueuedAudioTrack()
    task = asyncio.create_task(track.enqueue_pcm(tone(48000, 2)))

    async def full():
        while not track._queue.full():
            await asyncio.sleep(0)

    await asyncio.wait_for(full(), 5)
    assert not task.done()
    assert track._queue.qsize() * FRAME_BYTES <= 48000
    track.clear()
    await asyncio.wait_for(task, 5)
    await asyncio.wait_for(track.drained(), 5)
    assert track._queue.empty()


@pytest.mark.asyncio
async def test_disconnect_aborts_blocked_pcm_writer():
    adapter, owner, protocol, sink, events = voice_adapter("realtime")
    owner.output_track = QueuedAudioTrack()
    handle = await adapter.begin_streaming_tts("chat", AudioFormat())
    task = asyncio.create_task(adapter.write_streaming_tts(handle, tone(seconds=2)))

    async def full():
        while not owner.output_track._queue.full():
            await asyncio.sleep(0)

    await asyncio.wait_for(full(), 5)
    owner.closed = True
    await adapter._abort_tts_for_chat("chat")
    await asyncio.gather(task, return_exceptions=True)
    assert handle.aborted and handle.finished
    assert owner.output_track._queue.empty()
    assert not owner.paused and not adapter._tts_streams


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_unsupported_format_or_destination_declines_before_output(kind):
    adapter, owner, protocol, sink, events = voice_adapter(kind)
    for audio_format in (AudioFormat(channels=2), AudioFormat(sample_width=4), AudioFormat(sample_rate=0)):
        assert await adapter.begin_streaming_tts("chat", audio_format) is None
    assert await adapter.begin_streaming_tts("missing", AudioFormat()) is None
    assert not sink.frames and not events and not adapter._tts_streams


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_incomplete_first_sample_does_not_suppress_fallback(kind, monkeypatch):
    adapter, owner, protocol, sink, events = voice_adapter(kind)

    class Provider:
        sample_rate, channels, sample_width = 24000, 1, 2

        def stream(self, text):
            yield b"\x01"
            raise RuntimeError("failed before a playable sample")

    monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda config: Provider())
    consumer = StreamingTTSConsumer(adapter, "chat", {}, asyncio.get_running_loop())
    task = consumer.start()
    consumer.on_delta("This reply should still get a fallback. ")
    consumer.finish()
    await asyncio.wait_for(task, 5)
    assert not sink.frames
    assert not consumer.audible
    assert not consumer.suppress_whole_file


@pytest.mark.asyncio
async def test_gateway_finalizer_allows_a_long_reply_to_finish_playing(monkeypatch):
    from gateway.run_turn import GatewayTurnMixin

    adapter, owner, protocol, sink, events = voice_adapter("realtime")
    owner.output_track = QueuedAudioTrack()

    class Provider:
        sample_rate, channels, sample_width = 24000, 1, 2

        def stream(self, text):
            yield tone(seconds=12)

    monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda config: Provider())
    consumer = StreamingTTSConsumer(adapter, "chat", {}, asyncio.get_running_loop())
    context = SimpleNamespace(
        streaming_tts_consumer_holder=[consumer], session_key="chat", run_generation=1,
    )
    samples = 0

    async def play():
        nonlocal samples
        while True:
            frame = await owner.output_track.recv()
            # Count the test tone, not the track's new idle keepalive frames.
            if any(bytes(frame.planes[0])):
                samples += frame.samples

    playing = asyncio.create_task(play())
    task = consumer.start()
    try:
        consumer.on_delta("A longer spoken answer must not be cut off while playback is progressing. ")
        await asyncio.wait_for(
            GatewayTurnMixin()._run_agent_finalize_streaming_tts(context, adapter), 30,
        )
        assert consumer.completed
        assert samples == 12 * 48000
        assert consumer.suppress_whole_file and not adapter._tts_streams
    finally:
        consumer.abort()
        await adapter._abort_tts_for_chat("chat")
        playing.cancel()
        task.cancel()
        await asyncio.gather(playing, task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("trim_onset", [False, True], ids=["raw-onset", "trimmed-onset"])
@pytest.mark.parametrize("production", [False, True, True], ids=[
    "synthetic", "smarty-opt-in-first", "smarty-opt-in-repeat",
])
async def test_pcm_reaches_real_rtp_receiver_before_text_finishes(production, trim_onset, monkeypatch):
    """Opt in with HERMES_TTS_CANARY_CONFIG=/path/to/profile/config.yaml.

    The production variant uses the configured provider without logging secrets
    or playing audio on a device. Both peers are private, in-process test peers.
    """
    from aiortc import RTCConfiguration, RTCPeerConnection

    config = {}
    if production:
        path = os.environ.get("HERMES_TTS_CANARY_CONFIG")
        if not path:
            pytest.skip("production TTS canary is explicitly opt-in")
        import yaml

        with open(path) as file:
            config = yaml.safe_load(file)["tts"]
        # Provider availability consults the profile loader separately from
        # the supplied config. Keep HERMES_HOME isolated and secrets in memory.
        monkeypatch.setattr("tools.tts_streaming._load_tts_config", lambda: config)
    else:
        class Provider:
            sample_rate, channels, sample_width = 24000, 1, 2

            def stream(self, text):
                yield b"\0" * 28800 + tone(seconds=0.4)  # 600 ms generated quiet.

        monkeypatch.setattr("tools.tts_streaming.resolve_streaming_provider", lambda config: Provider())

    adapter, owner, protocol, sink, events = voice_adapter("realtime")
    adapter._tts_trim_leading_silence = trim_onset
    owner.output_track = QueuedAudioTrack()
    sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    sender.addTrack(owner.output_track)
    first_pcm, first_rtp = asyncio.Event(), asyncio.Event()
    timing = {}
    tasks = []
    consumer = None
    original_write = adapter._tts_write_frame

    async def write(handle, pcm):
        await original_write(handle, pcm)
        if not first_pcm.is_set():
            timing["first_pcm_s"] = time.monotonic() - started
            first_pcm.set()

    adapter._tts_write_frame = write

    @receiver.on("track")
    def receive(track):
        async def drain():
            while True:
                frame = await track.recv()
                pcm = frame.to_ndarray()
                if not first_rtp.is_set() and ((pcm > 40) | (pcm < -40)).any():
                    timing["first_rtp_s"] = time.monotonic() - started
                    first_rtp.set()

        tasks.append(asyncio.create_task(drain()))

    try:
        await sender.setLocalDescription(await sender.createOffer())
        await receiver.setRemoteDescription(sender.localDescription)
        await receiver.setLocalDescription(await receiver.createAnswer())
        await sender.setRemoteDescription(receiver.localDescription)

        async def connected():
            while sender.connectionState != "connected" or receiver.connectionState != "connected":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(connected(), 10)
        consumer = StreamingTTSConsumer(adapter, "chat", config, asyncio.get_running_loop())
        assert consumer.active
        tasks.append(consumer.start())
        started = time.monotonic()
        consumer.on_delta("Streaming audio should reach the receiver before the rest of this reply is ready. ")
        await asyncio.wait_for(first_rtp.wait(), 30)
        assert first_pcm.is_set() and not consumer.done
        handle = adapter._tts_streams["chat"]
        consumer.finish()
        assert await consumer.wait_complete(timeout=60)
        timing["complete_s"] = time.monotonic() - started
        timing["audio_s"] = handle.pcm_bytes / 96000
        timing["trimmed_ms"] = handle.leading_silence.trimmed_frames * 20 if trim_onset else 0
        if not production:
            # The 24->48 kHz resampler's pre-ringing starts in the preceding
            # frame. Keep that frame as well as the 80 ms lead-in.
            assert timing["trimmed_ms"] == (500 if trim_onset else 0)
            assert timing["audio_s"] == pytest.approx(0.5 if trim_onset else 1.0)
        assert consumer.suppress_whole_file and not adapter._tts_streams
        print({"streaming_tts_rtp": "production" if production else "synthetic",
               "trim_onset": trim_onset, **timing})
    finally:
        if consumer is not None:
            consumer.abort()
        await adapter._abort_tts_for_chat("chat")
        await sender.close()
        await receiver.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_rtp_preserves_short_onsets_after_idle_and_sentence_gaps():
    import numpy as np
    from aiortc import RTCConfiguration, RTCPeerConnection
    from av import AudioResampler

    source = QueuedAudioTrack()
    sender = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    receiver = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    sender.addTrack(source)
    received, tasks = [], []

    @receiver.on("track")
    def receive(track):
        async def drain():
            mono = AudioResampler(format="s16", layout="mono", rate=48000)
            while True:
                frame = await track.recv()
                for converted in mono.resample(frame):
                    samples = converted.to_ndarray().reshape(-1)
                    frequency = 0
                    if np.max(np.abs(samples.astype(np.int32))) > 500:
                        spectrum = np.abs(np.fft.rfft(samples))
                        frequency = int(np.argmax(spectrum) * 48000 / len(samples))
                    received.append((converted.pts, frequency))
        tasks.append(asyncio.create_task(drain()))

    async def received_count(count):
        while len(received) < count:
            await asyncio.sleep(0.01)

    def marked_burst():
        # Three distinct 60ms segments: losing the initial 50–100ms cannot
        # hide behind a test which merely detects that some audio arrived.
        return b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / 48000)))
            for hz in (400, 1000, 1800) for i in range(2880)
        )

    try:
        await sender.setLocalDescription(await sender.createOffer())
        await receiver.setRemoteDescription(sender.localDescription)
        await receiver.setLocalDescription(await receiver.createAnswer())
        await sender.setRemoteDescription(receiver.localDescription)
        await asyncio.wait_for(received_count(15), 10)
        assert all(frequency == 0 for _, frequency in received)
        for _ in range(3):
            start = len(received)
            await source.enqueue_pcm(marked_burst())
            await asyncio.wait_for(source.drained(), 2)
            await asyncio.wait_for(received_count(start + 35), 2)
            frequencies = [frequency for _, frequency in received[start:] if frequency]
            for hz in (400, 1000, 1800):
                assert sum(abs(frequency - hz) < 100 for frequency in frequencies) >= 2
            assert abs(frequencies[0] - 400) < 100
            assert abs(frequencies[-1] - 1800) < 100
        timestamps = [pts for pts, _ in received]
        assert all(b - a == 960 for a, b in zip(timestamps, timestamps[1:]))
    finally:
        await sender.close()
        await receiver.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
