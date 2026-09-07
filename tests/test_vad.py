"""Room-noise adaptation and configurable direct voice endpointing."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_livekit.vad import AdaptiveRmsGate, configured_silence_duration
from hermes_livekit.realtime_webrtc import QueuedAudioTrack, RealtimeCall
import hermes_livekit.realtime_webrtc as realtime


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
