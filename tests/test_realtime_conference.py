from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from hermes_livekit.adapter import LiveKitAdapter


class LocalParticipant:
    def __init__(self) -> None:
        self.messages: list[tuple[dict, dict]] = []

    async def publish_data(self, data: bytes, **options) -> None:
        self.messages.append((json.loads(data), options))


def conference_adapter() -> tuple[LiveKitAdapter, LocalParticipant]:
    local = LocalParticipant()
    adapter = object.__new__(LiveKitAdapter)
    adapter.platform = SimpleNamespace(value="livekit")
    adapter._room_name = "room-a"
    adapter._room = SimpleNamespace(
        local_participant=local,
        remote_participants={"client-a": object()},
    )
    adapter._audio_source = None
    adapter._paused = False
    adapter._realtime_protocol = adapter._new_realtime_protocol()
    return adapter, local


@pytest.mark.asyncio
async def test_lifecycle_uses_shared_conference_event_topic() -> None:
    adapter, local = conference_adapter()
    protocol = adapter._realtime_protocol

    await protocol.client_connected("client-a")
    await adapter._publish_agent_event("agent:listening-start", {"identity": "client-a"})
    await adapter._publish_agent_event("agent:listening-stop", {"identity": "client-a"})
    await adapter._publish_agent_event(
        "agent:user-transcript",
        {"identity": "client-a", "transcript": "hello", "final": True},
    )
    await adapter._publish_agent_event("agent:thinking-start")
    await adapter.send("room-a", "hi")
    await adapter._publish_agent_event("agent:speaking-start")
    await adapter._publish_agent_event("agent:speaking-stop")

    assert [message["type"] for message, _ in local.messages] == [
        "session.created",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "conversation.item.created",
        "conversation.item.input_audio_transcription.completed",
        "response.created",
        "response.output_audio_transcript.done",
        "output_audio_buffer.started",
        "response.done",
        "output_audio_buffer.stopped",
    ]
    assert all(
        options["topic"] == LiveKitAdapter.DATA_CHANNEL_EVENTS_TOPIC
        for _, options in local.messages
    )
    assert local.messages[0][1]["destination_identities"] == ["client-a"]
    assert all(
        options["destination_identities"] == []
        for _, options in local.messages[1:]
    )


@pytest.mark.asyncio
async def test_inbound_event_uses_authoritative_participant_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, local = conference_adapter()
    scheduled: list[object] = []
    monkeypatch.setattr(asyncio, "create_task", lambda coroutine: scheduled.append(coroutine))
    packet = SimpleNamespace(
        topic=LiveKitAdapter.DATA_CHANNEL_EVENTS_TOPIC,
        participant=SimpleNamespace(identity="client-a"),
        data=json.dumps({"type": "unknown", "event_id": "event-1"}).encode(),
    )

    adapter._on_data_received(packet)
    await scheduled.pop()

    message, options = local.messages[-1]
    assert message["type"] == "error"
    assert message["error"]["event_id"] == "event-1"
    assert options["destination_identities"] == ["client-a"]


@pytest.mark.asyncio
async def test_extension_events_stay_off_conversation_topic() -> None:
    adapter, local = conference_adapter()

    await adapter._publish_agent_event(
        "agent:frame-captured",
        {"identity": "client-a", "width": 1, "height": 1},
    )

    message, options = local.messages[-1]
    assert message["type"] == "agent:frame-captured"
    assert options["topic"] == LiveKitAdapter.DATA_CHANNEL_CONTROL_TOPIC
