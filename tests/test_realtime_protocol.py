from __future__ import annotations

import json
import asyncio

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
        "conversation.item.added",
        "conversation.item.input_audio_transcription.completed",
        "conversation.item.done",
        "response.created",
        "response.output_item.added",
        "conversation.item.added",
        "response.content_part.added",
        "output_audio_buffer.started",
        "response.output_audio_transcript.done",
        "response.content_part.done",
        "conversation.item.done",
        "response.output_item.done",
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
    assert inputs == []
    assert [event["type"] for event, _ in sent[:2]] == [
        "conversation.item.added",
        "conversation.item.done",
    ]
    assert all(recipient == "client-a" for _, recipient in sent[:2])
    await protocol.handle_client_message(
        json.dumps({"type": "response.create", "event_id": "response-1"}),
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
async def test_response_create_requires_a_new_queued_input() -> None:
    inputs: list[tuple[str, str]] = []

    async def on_text(text: str, identity: str) -> None:
        inputs.append((text, identity))

    protocol, sent = protocol_fixture(on_text_input=on_text)
    item = {
        "type": "conversation.item.create",
        "item": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "one turn"}],
        },
    }
    await protocol.handle_client_message(json.dumps(item), "client-a")
    await protocol.handle_client_message(json.dumps({"type": "response.create"}), "client-a")
    await protocol.handle_client_message(json.dumps({"type": "response.create"}), "client-a")

    assert inputs == [("one turn", "client-a")]
    assert sent[-1][0]["type"] == "error"
    assert sent[-1][0]["error"]["code"] == "response_create_unsupported"


@pytest.mark.asyncio
async def test_response_create_without_new_input_uses_request_callback() -> None:
    requested: list[str] = []

    async def on_response(identity: str) -> None:
        requested.append(identity)

    protocol, sent = protocol_fixture(on_response_requested=on_response)
    await protocol.handle_client_message(
        json.dumps({"type": "response.create"}),
        "client-a",
    )

    assert requested == ["client-a"]
    assert sent == []


@pytest.mark.asyncio
async def test_does_not_replace_a_pending_typed_input() -> None:
    protocol, sent = protocol_fixture()

    def item(text: str) -> str:
        return json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        })

    await protocol.handle_client_message(item("first"), "client-a")
    await protocol.handle_client_message(item("second"), "client-a")

    assert sent[-1][0]["type"] == "error"
    assert sent[-1][0]["error"]["code"] == "conversation_item_pending"


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


@pytest.mark.asyncio
async def test_function_call_waits_for_output_and_response_create() -> None:
    protocol, sent = protocol_fixture()
    await protocol.client_connected("client-a")
    await protocol.response_started()

    result = asyncio.create_task(
        protocol.request_client_tool("fixture_echo", {"value": "ready"})
    )
    await asyncio.sleep(0)
    argument_event = next(
        event for event, _ in sent
        if event["type"] == "response.function_call_arguments.done"
    )
    call_id = argument_event["call_id"]

    assert argument_event["name"] == "fixture_echo"
    assert json.loads(argument_event["arguments"]) == {"value": "ready"}
    assert [
        event["response"]["status"] for event, _ in sent
        if event["type"] == "response.done"
    ] == ["completed"]

    await protocol.handle_client_message(
        json.dumps({
            "type": "conversation.item.create",
            "event_id": "tool-output",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": "client-result",
            },
        }),
        "client-a",
    )
    assert result.done() is False
    assert any(
        event["type"] == "conversation.item.done"
        and event["item"]["type"] == "function_call_output"
        for event, _ in sent
    )

    await protocol.handle_client_message(
        json.dumps({"type": "response.create", "event_id": "tool-continue"}),
        "client-a",
    )

    assert await result == "client-result"
    assert sent[-1][0]["type"] == "response.created"


@pytest.mark.asyncio
async def test_function_output_rejects_unknown_call_ids() -> None:
    protocol, sent = protocol_fixture()
    await protocol.handle_client_message(
        json.dumps({
            "type": "conversation.item.create",
            "event_id": "unknown-output",
            "item": {
                "type": "function_call_output",
                "call_id": "call_unknown",
                "output": "nope",
            },
        }),
        "client-a",
    )

    assert sent[-1][0]["error"]["code"] == "unknown_tool_call"
    assert sent[-1][0]["error"]["event_id"] == "unknown-output"
