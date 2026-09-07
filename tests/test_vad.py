"""Room-noise adaptation and configurable direct voice endpointing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_livekit.vad import AdaptiveRmsGate, configured_silence_duration
from hermes_livekit.realtime_webrtc import QueuedAudioTrack, RealtimeCall
import hermes_livekit.realtime_webrtc as realtime
import hermes_livekit.media as media


def calibrated(noise):
    gate = AdaptiveRmsGate()
    for _ in range(20):
        gate.calibrate(noise)
    return gate


@pytest.mark.parametrize("noise", [30, 150, 650, 1200])
def test_room_floor_scales_without_learning_speech(noise):
    gate = calibrated(noise)
    assert gate.noise_rms == noise
    assert not gate.is_speech(noise * 1.1, speaking=False)
    before = gate.noise_rms
    for _ in range(100):
        assert gate.is_speech(max(800, noise * 3), speaking=True)
    assert gate.noise_rms == before
    assert gate.is_speech(max(800, noise * 3), speaking=False)


def test_noise_tracking_is_time_based_for_both_transport_frame_sizes():
    gates = []
    for seconds in [0.02, 0.2]:
        gate = calibrated(150)
        for _ in range(round(2 / seconds)):
            assert not gate.is_speech(220, speaking=False, frame_seconds=seconds)
        assert 150 < gate.noise_rms < 220
        gates.append(gate)
    assert gates[0].noise_rms == pytest.approx(gates[1].noise_rms)
    for gate, seconds in zip(gates, [0.02, 0.2]):
        for _ in range(round(1 / seconds)):
            assert not gate.is_speech(80, speaking=True, frame_seconds=seconds)
        assert gate.noise_rms < 100
    assert gates[0].noise_rms == pytest.approx(gates[1].noise_rms)


def test_pauses_track_room_noise_without_learning_near_threshold_phonemes():
    gate = calibrated(150)
    for _ in range(100):
        assert not gate.is_speech(160, speaking=True)
    assert 150 < gate.noise_rms < 160
    before = gate.noise_rms
    for _ in range(100):
        assert not gate.is_speech(gate.stop_threshold - 1, speaking=True)
    assert gate.noise_rms == before


def test_gradual_fan_ramp_stays_silent_above_old_noise_ceiling():
    gate = calibrated(150)
    for step in range(1000):
        noise = 150 + 550 * step / 999
        assert not gate.is_speech(noise, speaking=False)
    assert 600 < gate.noise_rms < 700
    assert gate.is_speech(2200, speaking=False)


@pytest.mark.parametrize("value", [0.2, 0.7, 1.5, 5])
def test_silence_duration_accepts_seconds(value):
    assert configured_silence_duration({"silence_duration": value}) == value


@pytest.mark.parametrize("value", [True, False, None, "0.7", 0, -1, 0.19, 5.1, float("nan"), float("inf")])
def test_invalid_silence_duration_is_rejected(value):
    with pytest.raises(ValueError, match="silence_duration"):
        configured_silence_duration({"silence_duration": value})


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [0.3, 0.7, 1.5])
async def test_direct_endpoint_respects_timeout_and_mid_sentence_pause(duration, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(realtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    adapter = SimpleNamespace(process_voice=AsyncMock())
    protocol = AsyncMock()
    call = RealtimeCall(adapter, "fixture", AsyncMock(), QueuedAudioTrack(), protocol,
                        silence_duration=duration)
    call.vad = calibrated(150)
    speech = (800).to_bytes(2, "little", signed=True) * 960
    room = (150).to_bytes(2, "little", signed=True) * 960

    async def speak():
        for _ in range(20):
            clock[0] += 0.02
            await call.accept_pcm(speech)

    await speak()
    clock[0] += duration - 0.02
    await call.accept_pcm(room)
    assert call.speaking
    protocol.speech_stopped.assert_not_awaited()
    # More speech resets the endpoint deadline and stays in the same turn.
    await speak()
    clock[0] += duration - 0.02
    await call.accept_pcm(room)
    assert call.speaking
    clock[0] += 0.03
    await call.accept_pcm(room)
    await asyncio.gather(*call.tasks)
    assert not call.speaking and not call.audio_buffer
    protocol.speech_started.assert_awaited_once()
    protocol.speech_stopped.assert_awaited_once()
    adapter.process_voice.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_overflow_discards_command_then_recovers_with_preroll(monkeypatch):
    from hermes_livekit.realtime_protocol import RealtimeProtocol

    clock = [0.0]
    monkeypatch.setattr(realtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(media, "MAX_CAPTURE_BYTES", 192000)
    events = []

    async def publish(event, recipient):
        events.append(event)
        return True

    protocol = RealtimeProtocol(session_id="bounded", model="fixture", voice="fixture", publish=publish)
    adapter = SimpleNamespace(process_voice=AsyncMock())
    call = RealtimeCall(adapter, "bounded", AsyncMock(), QueuedAudioTrack(), protocol,
                        silence_duration=0.7, vad=calibrated(150))
    speech, quiet = b"\x20\x03" * 960, b"\x96\x00" * 960

    async def feed(pcm, count):
        for _ in range(count):
            clock[0] += 0.02
            await call.accept_pcm(pcm)
            assert len(call.audio_buffer) <= media.MAX_CAPTURE_BYTES

    await feed(speech, 130)
    assert call.input_overflowed and not call.speaking and not call.audio_buffer
    assert not protocol._input_items
    assert [e["error"]["code"] for e in events if e["type"] == "error"] == ["input_audio_too_long"]
    adapter.process_voice.assert_not_awaited()
    await feed(quiet, 70)
    assert not call.input_overflowed
    await feed(speech, 20)
    await feed(quiet, 36)
    await asyncio.gather(*call.tasks)
    adapter.process_voice.assert_awaited_once()
    captured = adapter.process_voice.call_args.args[1]
    assert captured.startswith(quiet * (media.PREROLL_BYTES // len(quiet)) + speech * 20)
    assert not call.audio_buffer
    await call.set_input_audio_state(True)
    await feed(speech, 5)
    assert not call.audio_buffer and not call.preroll
    await call.set_input_audio_state(False)
    assert not call.vad.ready
    call.output_track.stop()


@pytest.mark.asyncio
async def test_conference_bounds_capture_and_only_classifies_new_pcm(monkeypatch, request):
    from gateway.config import PlatformConfig
    from gateway.platform_registry import PlatformEntry, platform_registry
    from hermes_livekit.adapter import LiveKitAdapter
    import hermes_livekit.adapter as conference

    platform_registry.register(PlatformEntry(name="livekit", label="LiveKit",
        adapter_factory=LiveKitAdapter, check_fn=lambda: True))
    request.addfinalizer(lambda: platform_registry.unregister("livekit"))

    clock = [0.0]
    monkeypatch.setattr(conference, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(media, "MAX_CAPTURE_BYTES", 144000)
    adapter = LiveKitAdapter(PlatformConfig(extra={"silence_duration": 0.7}))
    identity = "speaker"
    adapter._audio_buffers[identity] = bytearray()
    adapter._audio_gates[identity] = calibrated(150)
    adapter._speaking_participants.add(identity)
    adapter._publish_agent_event = AsyncMock()
    adapter._publish_typed = AsyncMock()
    adapter._process_voice_input = AsyncMock()
    speech, quiet = b"\x20\x03" * 960, b"\x96\x00" * 960

    class Stream:
        closed = False

        async def __aiter__(self):
            for _ in range(100):
                clock[0] += 0.02
                yield SimpleNamespace(frame=SimpleNamespace(data=SimpleNamespace(tobytes=lambda: speech)))

        async def aclose(self):
            self.closed = True

    stream = Stream()
    await adapter._audio_receive_loop(stream, identity)
    assert stream.closed and identity in adapter._audio_overflowed
    assert len(adapter._audio_buffers[identity]) <= media.PREROLL_BYTES * 2
    assert not adapter._flush_utterance(identity, len(adapter._audio_buffers[identity]))
    adapter._publish_typed.assert_awaited_once()
    adapter._process_voice_input.assert_not_awaited()
    adapter._last_audio_time[identity] = clock[0]
    # Poll once without a new chunk after each real batch. A recurrent model
    # must not see those old samples for a second time.
    batches = iter([quiet * 10] * 5 + [speech * 10] * 2 + [quiet * 10] * 5)
    real_sleep = asyncio.sleep
    decisions = []
    gate = adapter._audio_gates[identity]
    original = gate.is_speech

    def classify(rms, **kwargs):
        decisions.append(kwargs["pcm"])
        return original(rms, **kwargs)

    gate.is_speech = classify
    repeat = [False]

    async def poll(_):
        if repeat[0]:
            repeat[0] = False
            return
        pcm = next(batches, None)
        if pcm is None:
            adapter._running = False
            return
        repeat[0] = True
        clock[0] += 0.2
        adapter._audio_buffers[identity].extend(pcm)

    monkeypatch.setattr(conference, "asyncio", SimpleNamespace(
        sleep=poll, create_task=asyncio.create_task, CancelledError=asyncio.CancelledError))
    adapter._running = True
    await adapter._check_silence_loop()
    await real_sleep(0)
    assert identity not in adapter._audio_overflowed
    adapter._process_voice_input.assert_awaited_once()
    assert len(decisions) == 12
    adapter._cleanup_participant(identity)
    assert identity not in adapter._audio_processed and identity not in adapter._audio_gates
