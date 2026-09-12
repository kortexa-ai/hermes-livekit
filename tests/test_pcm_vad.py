"""Audio batching must not hide speech or change the room-calibration window."""

import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_livekit.media import pcm_rms
from hermes_livekit.vad import AdaptiveRmsGate


def frame(level):
    return struct.pack("<2h", level, -level) * 480


@pytest.mark.parametrize("packet_bytes", [320, 960, 1920, 5760, 19200, 38400])
@pytest.mark.parametrize("noise", [50, 150, 650, 1200])
def test_pcm_calibration_and_short_speech_survive_batching(packet_bytes, noise):
    quiet, speech = frame(noise), frame(max(800, noise * 4))
    initial = quiet * 5 + speech * 10 + quiet * 5
    gate = AdaptiveRmsGate()
    assert not gate.calibrate_pcm(b"")
    for offset in range(0, len(initial), packet_bytes):
        packet = initial[offset:offset + packet_bytes]
        ready = gate.calibrate_pcm(packet)
        assert ready is (offset + len(packet) >= len(initial))
    assert gate.noise_rms == noise
    assert gate.is_speech(pcm_rms(initial), pcm=initial, speaking=False,
                          frame_seconds=0.4)
    assert not gate.is_speech(noise, pcm=quiet * 30, speaking=False,
                              frame_seconds=0.6)
    # A short signal followed by quiet must not disappear inside a poll batch.
    batch = speech + quiet * 29
    assert gate.is_speech(pcm_rms(batch), pcm=batch, speaking=False,
                          frame_seconds=0.6)
    assert gate.noise_rms == noise


@pytest.mark.asyncio
@pytest.mark.parametrize("transport,packet_frames", [
    ("direct", 1), ("direct", 3), ("direct", 10), ("direct", 20),
    ("conference", 10), ("conference", 30),
])
@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("has_speech", [False, True])
async def test_transport_preserves_early_speech_and_rejects_room_noise(
        transport, packet_frames, warm, has_speech, monkeypatch, tmp_path):
    from pathlib import Path

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from gateway.config import PlatformConfig
    from gateway.platform_registry import PlatformEntry, platform_registry
    from hermes_livekit.adapter import LiveKitAdapter
    from hermes_livekit.realtime_webrtc import QueuedAudioTrack, RealtimeCall
    import hermes_livekit.adapter as conference
    import hermes_livekit.realtime_webrtc as realtime

    quiet, speech = frame(50), frame(800)
    frames = ([quiet] * 20 if warm else []) + [quiet] * 5
    frames += [speech if has_speech else quiet] * 10 + [quiet] * 95
    packets = [b"".join(frames[i:i + packet_frames])
               for i in range(0, len(frames), packet_frames)]
    clock = [0.0]

    if transport == "direct":
        adapter = SimpleNamespace(process_voice=AsyncMock())
        call = RealtimeCall(adapter, "fixture", AsyncMock(), QueuedAudioTrack(),
                            AsyncMock(), silence_duration=0.7)
        monkeypatch.setattr(realtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        try:
            for packet in packets:
                clock[0] += len(packet) / 96000
                await call.accept_pcm(packet)
            await asyncio.gather(*call.tasks)
            sink = adapter.process_voice
        finally:
            call.output_track.stop()
    else:
        platform_registry.register(PlatformEntry(name="livekit", label="LiveKit",
            adapter_factory=LiveKitAdapter, check_fn=lambda: True))
        try:
            # Energy-gate batching contract; the synthetic square wave is not speech to Silero.
            adapter = LiveKitAdapter(PlatformConfig(extra={"silence_duration": 0.7, "vad_backend": "rms"}))
            identity = "synthetic-speaker"
            adapter._audio_buffers[identity] = bytearray()
            adapter._process_voice_input = AsyncMock()
            adapter._publish_agent_event = AsyncMock()
            adapter._publish_typed = AsyncMock()
            batches = iter(packets)

            async def poll(_):
                packet = next(batches, None)
                if packet is None:
                    adapter._running = False
                    return
                clock[0] += len(packet) / 96000
                adapter._audio_buffers[identity].extend(packet)

            monkeypatch.setattr(conference, "time", SimpleNamespace(monotonic=lambda: clock[0]))
            monkeypatch.setattr(conference, "asyncio", SimpleNamespace(
                sleep=poll, create_task=asyncio.create_task,
                CancelledError=asyncio.CancelledError))
            adapter._running = True
            await adapter._check_silence_loop()
            await asyncio.sleep(0)
            sink = adapter._process_voice_input
        finally:
            platform_registry.unregister("livekit")

    assert sink.await_count == int(has_speech)
    if has_speech:
        captured = sink.call_args.args[1]
        assert speech * 10 in captured
