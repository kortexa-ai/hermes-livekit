"""Direct WebRTC transport contract and real aiortc negotiation."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession
from aiortc import RTCPeerConnection, RTCSessionDescription

from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from hermes_livekit.realtime_webrtc import (
    RealtimeWebRTCAdapter,
    check_realtime_requirements,
)


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
        async with ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{port}/v1/realtime/calls",
                data=peer.localDescription.sdp,
                headers={
                    "Authorization": "Bearer test-token",
                    "Content-Type": "application/sdp",
                },
            ) as response:
                assert response.status == 200
                assert response.content_type == "application/sdp"
                answer = await response.text()
        await peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        await asyncio.wait_for(created.wait(), timeout=10)

        assert received[0]["type"] == "session.created"
        assert received[0]["session"]["model"] == "hermes"
        assert len(adapter._calls) == 1
    finally:
        await peer.close()
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_listener_requires_api_key(realtime_platform: None) -> None:
    adapter = RealtimeWebRTCAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0})
    )

    assert await adapter.connect() is False


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
