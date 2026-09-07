"""State isolation and framing, without model downloads or provider calls."""

import numpy as np
import pytest

from hermes_livekit.speech_detector import SileroRmsGate, load_silero_model
from hermes_livekit.vad import AdaptiveRmsGate, configured_vad_factory


class RecordingSession:
    def __init__(self):
        self.inputs = []

    def run(self, _, inputs):
        self.inputs.append({k: v.copy() for k, v in inputs.items()})
        return np.array([[0.8]], dtype=np.float32), inputs["state"] + 1


def test_classifier_preserves_sample_sequence_across_frames_and_isolates_inputs():
    audio = np.arange(9600, dtype=np.int16).tobytes()
    sessions = []
    for size in (960 * 2, 9600 * 2):
        session = RecordingSession()
        gate = SileroRmsGate(session, 0.5, noise_rms=150)
        for offset in range(0, len(audio), size):
            gate.is_speech(150, speaking=False, pcm=audio[offset:offset + size])
        sessions.append(session)
        assert gate.noise_rms == 150  # positive classifier results never become room noise
        assert len(gate._pending) < 512
    assert len(sessions[0].inputs) == len(sessions[1].inputs)
    for left, right in zip(sessions[0].inputs, sessions[1].inputs):
        np.testing.assert_array_equal(left["input"], right["input"])
        np.testing.assert_array_equal(left["state"], right["state"])
    shared = sessions[0]
    fresh = SileroRmsGate(shared, 0.5)
    fresh.is_speech(150, speaking=False, pcm=audio[:1920 * 2])
    assert not shared.inputs[-1]["state"].any()
    assert not shared.inputs[-1]["input"][:, :64].any()


@pytest.mark.parametrize("extra", [
    {"vad_backend": "unknown"}, {"vad_backend": None},
    {"vad_backend": "silero"},
    *({"vad_backend": "silero", "vad_model_path": "/fixture/model", "vad_threshold": v}
      for v in (True, "0.5", 0, 1, float("nan"))),
])
def test_invalid_detector_settings_fail_before_accepting_audio(extra):
    with pytest.raises(ValueError):
        configured_vad_factory(extra)


def test_default_needs_no_model_and_configured_factories_keep_state_separate(monkeypatch):
    assert isinstance(configured_vad_factory({})(), AdaptiveRmsGate)
    session = RecordingSession()
    paths = []
    monkeypatch.setattr("hermes_livekit.speech_detector.load_silero_model",
                        lambda path: paths.append(path) or session)
    factory = configured_vad_factory({"vad_backend": "silero", "vad_model_path": "/fixture/model"})
    first, second = factory(), factory()
    first._state.fill(3)
    assert not second._state.any()
    assert paths == ["/fixture/model"]


def test_bad_model_is_rejected_before_runtime_construction(tmp_path):
    model = tmp_path / "model.onnx"
    model.write_bytes(b"not a model")
    # The optional runtime is never constructed for unverified bytes.
    with pytest.raises(ValueError, match="size"):
        load_silero_model(str(model))


def test_model_checksum_is_checked_even_when_size_matches(tmp_path, monkeypatch):
    import hermes_livekit.speech_detector as detector

    model = tmp_path / "model.onnx"
    model.write_bytes(b"wrong")
    monkeypatch.setattr(detector, "SILERO_MODEL_BYTES", 5)
    with pytest.raises(ValueError, match="checksum"):
        load_silero_model(str(model))


def test_classifier_holds_decision_between_windows_and_learns_only_noise():
    session = RecordingSession()
    gate = SileroRmsGate(session, 0.5, noise_rms=150)
    pcm = np.zeros(1920, dtype=np.int16).tobytes()
    assert gate.is_speech(650, speaking=False, pcm=pcm)
    count = len(session.inputs)
    assert gate.is_speech(650, speaking=True, pcm=pcm[:20])
    assert len(session.inputs) == count  # fewer than 512 new 16-kHz samples
    assert gate.noise_rms == 150
    session.run = lambda _, inputs: (np.array([[0.01]], dtype=np.float32), inputs["state"])
    assert not gate.is_speech(650, speaking=True, pcm=pcm)
    assert gate.noise_rms > 150
    for invalid in (None, b"x"):
        with pytest.raises(ValueError, match="PCM16"):
            gate.is_speech(150, speaking=False, pcm=invalid)


@pytest.mark.parametrize("download", [b"valid", b"wrong", b"valid-extra"])
def test_model_installer_checks_download_and_never_overwrites(tmp_path, monkeypatch, download):
    import hashlib
    import importlib.util
    import io
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("prepare_vad", Path(__file__).parents[1] / "tools/prepare_vad.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    requests = []

    def retrieve(url, timeout):
        requests.append((url, timeout))
        return io.BytesIO(download)

    monkeypatch.setattr(installer, "urlopen", retrieve)
    destination = tmp_path / "model.onnx"
    digest = hashlib.sha256(b"valid").hexdigest()
    if download == b"valid":
        installer.install_verified(destination, "https://fixture.invalid/model", 5, digest)
        assert destination.read_bytes() == b"valid"
        installer.install_verified(destination, "https://fixture.invalid/model", 5, digest)
        assert len(requests) == 1  # verified installed files need no network
    else:
        with pytest.raises(ValueError, match="verification"):
            installer.install_verified(destination, "https://fixture.invalid/model", 5, digest)
        assert not destination.exists()
    assert not list(tmp_path.glob(".vad-*"))
    destination.write_bytes(b"existing")
    with pytest.raises(ValueError, match="exists"):
        installer.install_verified(destination, "https://fixture.invalid/model", 5, digest)
    assert destination.read_bytes() == b"existing"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_adapter_config_reaches_negotiated_call_and_unmute(monkeypatch, request):
    from aiohttp import ClientSession
    from aiortc import RTCPeerConnection
    from gateway.config import PlatformConfig
    from gateway.platform_registry import PlatformEntry, platform_registry
    from hermes_livekit.realtime_webrtc import RealtimeWebRTCAdapter
    from hermes_livekit.adapter import LiveKitAdapter

    session = RecordingSession()
    monkeypatch.setattr("hermes_livekit.speech_detector.load_silero_model", lambda _: session)
    for name, factory in (("realtime", RealtimeWebRTCAdapter), ("livekit", LiveKitAdapter)):
        platform_registry.register(PlatformEntry(name=name, label=name, adapter_factory=factory, check_fn=lambda: True))
        request.addfinalizer(lambda key=name: platform_registry.unregister(key))
    extra = {"host": "127.0.0.1", "port": 0, "api_key": "fixture", "vad_backend": "silero",
             "vad_model_path": "/fixture/model", "vad_threshold": 0.6}
    adapter = RealtimeWebRTCAdapter(PlatformConfig(enabled=True, extra=extra.copy()))
    peer = RTCPeerConnection()
    peer.addTransceiver("audio", direction="sendrecv")
    peer.createDataChannel("oai-events")
    try:
        assert await adapter.connect()
        port = adapter._site._server.sockets[0].getsockname()[1]
        await peer.setLocalDescription(await peer.createOffer())
        async with ClientSession() as client:
            async with client.post(f"http://127.0.0.1:{port}/v1/realtime/calls",
                data=peer.localDescription.sdp,
                headers={"Authorization": "Bearer fixture", "Content-Type": "application/sdp"}) as response:
                assert response.status == 201
                await response.read()
        call = next(iter(adapter._calls.values()))
        assert isinstance(call.vad, SileroRmsGate) and call.vad._threshold == 0.6
        call.vad._state.fill(7)
        await call.set_input_audio_state(True)
        await call.set_input_audio_state(False)
        assert isinstance(call.vad, SileroRmsGate) and not call.vad._state.any()
        assert call.vad._threshold == 0.6
        conference = LiveKitAdapter(PlatformConfig(extra=extra.copy()))
        await conference._set_input_audio_state("speaker", False)
        other = conference._audio_gates["speaker"]
        assert isinstance(other, SileroRmsGate) and other._threshold == 0.6
        assert other._session is call.vad._session
        call.vad._state.fill(7)
        assert not other._state.any()
    finally:
        await peer.close()
        await adapter.disconnect()
