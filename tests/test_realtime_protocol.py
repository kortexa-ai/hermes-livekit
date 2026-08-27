from __future__ import annotations

import json

import pytest

from hermes_livekit.realtime_protocol import RealtimeProtocol


def protocol_fixture(**callbacks):
    sent: list[tuple[dict, str | None]] = []

    async def publish(event: dict, recipient: str | None) -> bool:
        sent.append((event, recipient))
        return True

    protocol = RealtimeProtocol(
        session_id="room-a",
        model="local-model",
        voice="local-voice",
        publish=publish,
        **callbacks,
    )
    return protocol, sent


@pytest.mark.asyncio
async def test_targets_session_snapshot_and_correlated_client_error() -> None:
    protocol, sent = protocol_fixture()

    await protocol.client_connected("client-a")
    await protocol.handle_client_message(
        json.dumps({"type": "nope", "event_id": "client-event-1"}),
        "client-b",
    )

    assert sent[0][0]["type"] == "session.created"
    assert sent[0][1] == "client-a"
    assert sent[1][0]["error"]["event_id"] == "client-event-1"
    assert sent[1][1] == "client-b"


@pytest.mark.asyncio
async def test_maps_audio_and_response_lifecycle_to_openai_events() -> None:
    protocol, sent = protocol_fixture()

    await protocol.speech_started("client-a")
    await protocol.speech_stopped("client-a")
    await protocol.user_transcript("hello", "client-a")
    await protocol.response_started()
    await protocol.output_started()
    await protocol.assistant_transcript("hi there")
    await protocol.output_stopped()

    assert [event["type"] for event, _ in sent] == [
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "conversation.item.created",
        "conversation.item.input_audio_transcription.completed",
        "response.created",
        "output_audio_buffer.started",
        "response.output_audio_transcript.done",
        "response.done",
        "output_audio_buffer.stopped",
    ]
    response = sent[-2][0]["response"]
    assert response["status"] == "completed"
    assert response["output"][0]["content"] == [
        {"type": "output_audio", "transcript": "hi there"}
    ]
    assert all(event["event_id"].startswith("evt_room-a_") for event, _ in sent)


@pytest.mark.asyncio
async def test_routes_typed_input_and_cancellation_to_transport_callbacks() -> None:
    inputs: list[tuple[str, str]] = []
    cancelled: list[str] = []

    async def on_text(text: str, identity: str) -> None:
        inputs.append((text, identity))

    async def on_cancel(identity: str) -> None:
        cancelled.append(identity)

    protocol, sent = protocol_fixture(
        on_text_input=on_text,
        on_response_cancelled=on_cancel,
    )
    await protocol.handle_client_message(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": " hello "}],
                },
            }
        ),
        "client-a",
    )
    await protocol.response_started()
    await protocol.output_started()
    await protocol.handle_client_message(
        json.dumps({"type": "response.cancel", "event_id": "cancel-1"}),
        "client-a",
    )

    assert inputs == [("hello", "client-a")]
    assert cancelled == ["client-a"]
    assert [event["type"] for event, _ in sent[-2:]] == [
        "response.done",
        "output_audio_buffer.cleared",
    ]
    assert sent[-2][0]["response"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_rejects_oversized_and_non_object_client_events() -> None:
    protocol, sent = protocol_fixture()

    await protocol.handle_client_message(b"x" * (256 * 1024 + 1), "client-a")
    await protocol.handle_client_message("[]", "client-a")

    assert [event["error"]["code"] for event, _ in sent] == [
        "event_too_large",
        "invalid_event_json",
    ]
    assert all(recipient == "client-a" for _, recipient in sent)
