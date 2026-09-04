"""Direct WebRTC transport contract and real aiortc negotiation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientSession, FormData, web
from aiortc import RTCPeerConnection, RTCSessionDescription

from gateway.config import PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platform_registry import PlatformEntry, platform_registry
from hermes_livekit.realtime_webrtc import (
    AdaptiveRmsGate,
    CLIENT_IDENTITY,
    OUTPUT_ECHO_GUARD_SECONDS,
    PROXY_CALL_HEADER,
    PROXY_PRINCIPAL_HEADER,
    PROXY_SIGNATURE_HEADER,
    PROXY_TIMESTAMP_HEADER,
    QueuedAudioTrack,
    RealtimeCall,
    RealtimeWebRTCAdapter,
    _gateway_profile_name,
    _parse_ice_servers,
    _proxy_signature_payload,
    check_realtime_requirements,
)
from tools.registry import registry


@pytest.fixture
def realtime_platform() -> None:
    entry = PlatformEntry(
        name="realtime",
        label="Realtime",
        adapter_factory=lambda config: RealtimeWebRTCAdapter(config),
        check_fn=check_realtime_requirements,
    )
    platform_registry.register(entry)
    try:
        yield
    finally:
        platform_registry.unregister("realtime")


@pytest.mark.asyncio
async def test_direct_listener_negotiates_and_sends_session_created(
    realtime_platform: None,
) -> None:
    adapter = RealtimeWebRTCAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "api_key": "test-token"},
        )
    )
    peer = RTCPeerConnection()
    created = asyncio.Event()
    updated = asyncio.Event()
    received: list[dict[str, object]] = []
    channel = peer.createDataChannel("oai-events")
    peer.addTransceiver("audio", direction="recvonly")
    adapter.process_text = AsyncMock()
    registered_name: str | None = None

    @channel.on("message")
    def on_message(raw: str) -> None:
        event = json.loads(raw)
        received.append(event)
        if event.get("type") == "session.created":
            created.set()
        if event.get("type") == "session.updated":
            updated.set()

    try:
        assert await adapter.connect() is True
        port = adapter._site._server.sockets[0].getsockname()[1]
        await peer.setLocalDescription(await peer.createOffer())
        form = FormData(default_to_multipart=True)
        form.add_field("sdp", peer.localDescription.sdp)
        form.add_field(
            "session",
            json.dumps({
                "type": "realtime",
                "instructions": "Initial instructions.",
                "tools": [{
                    "type": "function",
                    "name": "fixture_echo",
                    "description": "Return a value.",
                    "parameters": {"type": "object", "properties": {}},
                }],
                "tool_choice": "auto",
            }),
        )
        async with ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{port}/v1/realtime/calls",
                data=form,
                headers={
                    "Authorization": "Bearer test-token",
                },
            ) as response:
                assert response.status == 201
                assert response.content_type == "application/sdp"
                assert response.headers["Location"].startswith("/v1/realtime/calls/call_")
                answer = await response.text()
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        await asyncio.wait_for(created.wait(), timeout=10)

        assert received[0]["type"] == "session.created"
        assert received[0]["session"]["model"] == "hermes"
        assert received[0]["session"]["tool_choice"] == "auto"
        assert received[0]["session"]["instructions"] == "Initial instructions."
        assert len(adapter._calls) == 1
        active_call = next(iter(adapter._calls.values()))
        assert active_call.tool_bridge is not None
        registered_name = next(iter(active_call.tool_bridge._registered))
        assert registry.get_entry(registered_name) is not None
        channel.send(json.dumps({
            "type": "session.update",
            "event_id": "fixture-session-update",
            "session": {
                "type": "realtime",
                "instructions": "Reply briefly.",
            },
        }))
        await asyncio.wait_for(updated.wait(), timeout=5)
        assert active_call.protocol.instructions == "Reply briefly."
        channel.send(json.dumps({"type": "response.create"}))
        await asyncio.wait_for(
            _wait_until(lambda: adapter.process_text.await_count == 1),
            timeout=5,
        )
        requested_call, prompt = adapter.process_text.await_args.args
        assert requested_call.call_id.startswith("call_")
        assert prompt == "Follow the session instructions and respond now."
    finally:
        await peer.close()
        await adapter.disconnect()
    if registered_name is not None:
        assert registry.get_entry(registered_name) is None


async def _wait_until(predicate, interval: float = 0.01) -> None:
    while not predicate():
        await asyncio.sleep(interval)


def test_gateway_profile_name_is_derived_from_process_home(tmp_path) -> None:
    assert _gateway_profile_name(tmp_path / ".hermes") == "default"
    assert _gateway_profile_name(tmp_path / ".hermes" / "profiles" / "mira") == "mira"
    assert _gateway_profile_name(tmp_path / ".hermes" / "profiles" / "Mira") == "default"


@pytest.mark.asyncio
async def test_direct_listener_exposes_authenticated_fixed_profile_discovery(
    realtime_platform: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes" / "profiles" / "mira"))
    adapter = RealtimeWebRTCAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "api_key": "test-token"},
        )
    )
    try:
        assert await adapter.connect() is True
        port = adapter._site._server.sockets[0].getsockname()[1]
        async with ClientSession() as client:
            async with client.get(
                f"http://127.0.0.1:{port}/v1/realtime/discovery",
            ) as response:
                assert response.status == 401
            async with client.get(
                f"http://127.0.0.1:{port}/v1/realtime/discovery",
                headers={"Authorization": "Bearer test-token"},
            ) as response:
                assert response.status == 200
                assert response.headers["Cache-Control"] == "no-store"
                assert await response.json() == {
                    "version": 1,
                    "profile": "mira",
                    "realtime_path": "/v1/realtime/calls",
                }
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_queued_audio_track_paces_pcm_in_realtime() -> None:
    track = QueuedAudioTrack()
    samples_per_frame = 48_000 // 50
    frame_bytes = b"\x00\x00" * samples_per_frame
    await track.enqueue_pcm(frame_bytes * 3)

    started_at = asyncio.get_running_loop().time()
    frames = [await track.recv() for _ in range(3)]
    elapsed = asyncio.get_running_loop().time() - started_at

    # The first frame is ready immediately; the next two are spaced 20 ms
    # apart instead of being returned as a single RTP burst.
    assert elapsed >= 0.035
    assert [frame.pts for frame in frames] == [
        0,
        samples_per_frame,
        samples_per_frame * 2,
    ]
    assert all(frame.sample_rate == 48_000 for frame in frames)


def test_adaptive_rms_gate_learns_fan_noise_without_calling_it_speech() -> None:
    gate = AdaptiveRmsGate()

    # The Pi fan measures around RMS 150, well above the legacy fixed gate of
    # 50. Include loud samples to model the user starting to talk while the
    # initial 400 ms calibration window is still open.
    for rms in [150.0] * 12 + [900.0] * 8:
        gate.calibrate(rms)

    assert gate.ready is True
    assert gate.noise_rms == 150.0
    assert gate.is_speech(175.0, speaking=False) is False
    assert gate.is_speech(900.0, speaking=False) is True
    assert gate.is_speech(175.0, speaking=True) is False


def test_adaptive_rms_gate_keeps_safe_floor_after_muted_track_calibration() -> None:
    gate = AdaptiveRmsGate()
    for _ in range(20):
        gate.calibrate(0.0)

    # WPE sends digital silence before replaceTrack installs the real mic.
    # The measured Pi fan (RMS ~150, short-window peaks below 200) must not
    # become speech merely because calibration happened while muted.
    assert gate.noise_rms == 1.0
    assert gate.start_threshold == 300.0
    assert gate.stop_threshold == 220.0
    assert gate.is_speech(200.0, speaking=False) is False
    assert gate.is_speech(700.0, speaking=False) is True


def test_adaptive_rms_gate_tracks_gradual_idle_noise_changes() -> None:
    gate = AdaptiveRmsGate()
    for _ in range(20):
        gate.calibrate(100.0)
    initial_floor = gate.noise_rms

    for _ in range(100):
        assert gate.is_speech(175.0, speaking=False) is False

    assert gate.noise_rms is not None
    assert initial_floor is not None
    assert gate.noise_rms > initial_floor
    assert gate.noise_rms < 175.0


def _proxy_headers(
    *,
    principal: str,
    call_id: str,
    sdp: str,
    session_json: str,
    secret: str,
    timestamp: str | None = None,
) -> dict[str, str]:
    encoded_principal = base64.urlsafe_b64encode(principal.encode()).decode().rstrip("=")
    signed_at = timestamp or str(int(time.time()))
    signature = hmac.new(
        secret.encode(),
        _proxy_signature_payload(
            signed_at,
            call_id,
            encoded_principal,
            sdp,
            session_json,
        ),
        hashlib.sha256,
    ).hexdigest()
    return {
        PROXY_PRINCIPAL_HEADER: encoded_principal,
        PROXY_CALL_HEADER: call_id,
        PROXY_TIMESTAMP_HEADER: signed_at,
        PROXY_SIGNATURE_HEADER: f"v1={signature}",
    }


def test_internal_proxy_signature_matches_the_api_cross_language_fixture() -> None:
    signature = hmac.new(
        b"fixture-secret",
        _proxy_signature_payload(
            "1787900000",
            "rt_12345678",
            "dXNlci1h",
            "v=0\r\n",
            '{"type":"realtime"}',
        ),
        hashlib.sha256,
    ).hexdigest()

    assert signature == "c1cdd493ff293612f1140d867040dccdcd9b17478c300be6108ec7ee754da6e1"


def test_internal_proxy_metadata_is_body_bound_scoped_and_replay_safe() -> None:
    adapter = RealtimeWebRTCAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "api_key": "service-token",
                "proxy_signing_key": "signing-secret",
            },
        )
    )
    headers = _proxy_headers(
        principal="user-a",
        call_id="rt_12345678",
        sdp="v=0\r\n",
        session_json='{"type":"realtime"}',
        secret="signing-secret",
    )
    request = SimpleNamespace(headers=headers)

    identity = adapter._client_identity(
        request,
        "v=0\r\n",
        '{"type":"realtime"}',
    )

    assert identity.startswith("kortexa-")
    assert identity != "user-a"
    with pytest.raises(web.HTTPConflict):
        adapter._client_identity(request, "v=0\r\n", '{"type":"realtime"}')

    altered = RealtimeWebRTCAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "api_key": "service-token",
                "proxy_signing_key": "signing-secret",
            },
        )
    )
    with pytest.raises(web.HTTPUnauthorized):
        altered._client_identity(request, "v=1\r\n", '{"type":"realtime"}')


def test_unsigned_direct_requests_keep_the_legacy_opaque_identity() -> None:
    adapter = RealtimeWebRTCAdapter(
        PlatformConfig(enabled=True, extra={"api_key": "service-token"})
    )

    assert adapter._client_identity(SimpleNamespace(headers={}), "v=0\r\n", "{}") == CLIENT_IDENTITY


def test_direct_ice_servers_are_bounded_and_scheme_checked() -> None:
    servers = _parse_ice_servers(json.dumps([
        {"urls": "stun:stun.example.test:3478"},
        {
            "urls": ["turn:turn.example.test:3478?transport=udp"],
            "username": "user",
            "credential": "secret",
        },
    ]))

    assert len(servers) == 2
    assert servers[1].username == "user"
    with pytest.raises(ValueError, match="urls is invalid"):
        _parse_ice_servers('[{"urls":"https://example.test"}]')


@pytest.mark.asyncio
async def test_listener_requires_api_key(realtime_platform: None) -> None:
    adapter = RealtimeWebRTCAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0})
    )

    assert await adapter.connect() is False


@pytest.mark.asyncio
async def test_listener_accepts_required_tool_choice(
    realtime_platform: None,
) -> None:
    adapter = RealtimeWebRTCAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "api_key": "test-token"},
        )
    )
    peer = RTCPeerConnection()
    created = asyncio.Event()
    channel = peer.createDataChannel("oai-events")
    peer.addTransceiver("audio", direction="recvonly")

    @channel.on("message")
    def on_message(raw: str) -> None:
        if json.loads(raw).get("type") == "session.created":
            created.set()

    try:
        assert await adapter.connect() is True
        port = adapter._site._server.sockets[0].getsockname()[1]
        await peer.setLocalDescription(await peer.createOffer())
        form = FormData(default_to_multipart=True)
        form.add_field("sdp", peer.localDescription.sdp)
        form.add_field(
            "session",
            json.dumps({
                "type": "realtime",
                "tools": [{
                    "type": "function",
                    "name": "fixture_echo",
                    "parameters": {"type": "object", "properties": {}},
                }],
                "tool_choice": "required",
            }),
        )
        async with ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{port}/v1/realtime/calls",
                data=form,
                headers={"Authorization": "Bearer test-token"},
            ) as response:
                assert response.status == 201
                answer = await response.text()
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        await asyncio.wait_for(created.wait(), timeout=10)
        call = next(iter(adapter._calls.values()))
        assert call.protocol.tool_choice == "required"
    finally:
        await peer.close()
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_direct_send_voice_uses_native_audio_track() -> None:
    adapter = object.__new__(RealtimeWebRTCAdapter)
    adapter.play_tts = AsyncMock(return_value="delivered")

    result = await adapter.send_voice(
        chat_id="call",
        audio_path="reply.wav",
        caption="caption",
        reply_to="message",
        metadata={"turn": 1},
    )

    assert result == "delivered"
    adapter.play_tts.assert_awaited_once_with(
        chat_id="call",
        audio_path="reply.wav",
        caption="caption",
        reply_to="message",
        metadata={"turn": 1},
    )


@pytest.mark.asyncio
async def test_direct_play_tts_keeps_capture_paused_for_output_echo_tail() -> None:
    protocol = AsyncMock()
    output_track = SimpleNamespace(enqueue_pcm=AsyncMock(), drained=AsyncMock())
    call = SimpleNamespace(
        protocol=protocol,
        output_track=output_track,
        paused=False,
        tts_completed=False,
    )
    adapter = object.__new__(RealtimeWebRTCAdapter)
    adapter._calls = {"call": call}

    async def guarded_sleep(delay: float) -> None:
        assert delay == OUTPUT_ECHO_GUARD_SECONDS
        assert call.paused is True
        protocol.output_stopped.assert_not_awaited()

    with (
        patch(
            "hermes_livekit.adapter.LiveKitAdapter._decode_audio_to_pcm",
            return_value=b"\x00\x00" * 320,
        ),
        patch(
            "hermes_livekit.realtime_webrtc.asyncio.sleep",
            side_effect=guarded_sleep,
        ) as sleep,
    ):
        result = await adapter.play_tts("call", "reply.wav")

    assert result.success is True
    sleep.assert_awaited_once_with(OUTPUT_ECHO_GUARD_SECONDS)
    output_track.enqueue_pcm.assert_awaited_once()
    output_track.drained.assert_awaited_once_with()
    protocol.output_started.assert_awaited_once_with()
    protocol.output_stopped.assert_awaited_once_with()
    assert call.tts_completed is True
    assert call.paused is False


@pytest.mark.asyncio
async def test_direct_send_completes_transcript_response() -> None:
    protocol = AsyncMock()
    adapter = object.__new__(RealtimeWebRTCAdapter)
    call = SimpleNamespace(protocol=protocol, tts_completed=True)
    adapter._calls = {"call": call}

    result = await adapter.send(chat_id="call", content="hello")

    assert result.success is True
    protocol.assistant_transcript.assert_awaited_once_with("hello")
    protocol.output_stopped.assert_awaited_once_with()
    assert call.tts_completed is False


def test_direct_adapter_supports_async_completion_delivery() -> None:
    assert RealtimeWebRTCAdapter.supports_async_delivery is True


@pytest.mark.asyncio
async def test_direct_internal_wake_waits_for_silence() -> None:
    adapter = object.__new__(RealtimeWebRTCAdapter)
    call = SimpleNamespace(
        call_id="call",
        speaking=True,
        closed=False,
        tasks=set(),
    )

    def spawn(coroutine: object) -> None:
        task = asyncio.create_task(coroutine)
        call.tasks.add(task)
        task.add_done_callback(call.tasks.discard)

    call.spawn = spawn
    adapter._calls = {"call": call}
    event = SimpleNamespace(internal=True, source=SimpleNamespace(chat_id="call"))

    with patch.object(BasePlatformAdapter, "handle_message", new=AsyncMock()) as dispatch:
        await adapter.handle_message(event)
        await asyncio.sleep(0)
        dispatch.assert_not_awaited()

        call.speaking = False
        await asyncio.gather(*call.tasks)
        dispatch.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_direct_mute_finalizes_and_unmute_resets_vad() -> None:
    protocol = AsyncMock()
    adapter = SimpleNamespace(process_voice=AsyncMock())
    call = RealtimeCall(
        adapter=adapter,
        call_id="call-a",
        peer=AsyncMock(),
        output_track=QueuedAudioTrack(),
        protocol=protocol,
    )
    call.speaking = True
    call.audio_buffer.extend(b"\x00\x00" * 48_000)
    old_vad = call.vad

    await call.set_input_audio_state(True)
    await asyncio.sleep(0)
    assert call.input_muted is True
    assert call.audio_buffer == b""
    protocol.speech_stopped.assert_awaited_once_with("webrtc-client")
    adapter.process_voice.assert_awaited_once()

    await call.set_input_audio_state(False)
    assert call.input_muted is False
    assert call.vad is not old_vad
    assert call.vad.ready is False

    for task in list(call.tasks):
        task.cancel()
