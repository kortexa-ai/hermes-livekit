"""Direct WebRTC transport contract and real aiortc negotiation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, FormData, web
from aiortc import RTCPeerConnection, RTCSessionDescription

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from hermes_livekit.realtime_webrtc import (
    CLIENT_IDENTITY,
    PROXY_CALL_HEADER,
    PROXY_PRINCIPAL_HEADER,
    PROXY_SIGNATURE_HEADER,
    PROXY_TIMESTAMP_HEADER,
    RealtimeWebRTCAdapter,
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
        assert len(adapter._calls) == 1
        active_call = next(iter(adapter._calls.values()))
        assert active_call.tool_bridge is not None
        registered_name = next(iter(active_call.tool_bridge._registered))
        assert registry.get_entry(registered_name) is not None
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
async def test_listener_rejects_required_tool_choice_explicitly(
    realtime_platform: None,
) -> None:
    adapter = RealtimeWebRTCAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "127.0.0.1", "port": 0, "api_key": "test-token"},
        )
    )
    try:
        assert await adapter.connect() is True
        port = adapter._site._server.sockets[0].getsockname()[1]
        form = FormData(default_to_multipart=True)
        form.add_field("sdp", "v=0\r\n")
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
                assert response.status == 400
                assert "required is not supported" in await response.text()
    finally:
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
