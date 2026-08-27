"""Transport-neutral OpenAI-compatible Realtime session state."""

from __future__ import annotations

import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Optional


MAX_EVENT_BYTES = 256 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_EVENT_ID_BYTES = 128

Publish = Callable[[dict[str, Any], Optional[str]], Awaitable[bool]]
ClientCallback = Callable[[str], Awaitable[None] | None]
TextCallback = Callable[[str, str], Awaitable[None] | None]


async def _call(callback: Callable[..., Any] | None, *args: Any) -> bool:
    if callback is None:
        return False
    result = callback(*args)
    if inspect.isawaitable(result):
        await result
    return True


class RealtimeProtocol:
    """Own one realtime conversation independently of WebRTC or LiveKit."""

    def __init__(
        self,
        *,
        session_id: str,
        model: str,
        voice: str,
        publish: Publish,
        on_text_input: TextCallback | None = None,
        on_response_requested: ClientCallback | None = None,
        on_response_cancelled: ClientCallback | None = None,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.voice = voice
        self._publish = publish
        self._on_text_input = on_text_input
        self._on_response_requested = on_response_requested
        self._on_response_cancelled = on_response_cancelled
        self._started_at = time.monotonic()
        self._event_sequence = 0
        self._input_sequence = 0
        self._response_sequence = 0
        self._input_items: dict[str, str] = {}
        self._active_response_id: str | None = None
        self._active_output_item_id: str | None = None
        self._active_transcript: str | None = None
        self._speaking = False
        self._closed = False

    @property
    def active_response_id(self) -> str | None:
        return self._active_response_id

    async def client_connected(self, identity: str) -> None:
        if self._closed or not identity:
            return
        await self._emit(
            {
                "type": "session.created",
                "session": {
                    "id": self.session_id,
                    "type": "realtime",
                    "model": self.model,
                    "audio": {"output": {"voice": self.voice}},
                },
            },
            recipient=identity,
        )

    async def handle_client_message(self, raw: bytes | str, identity: str) -> None:
        if self._closed:
            return
        if isinstance(raw, bytes):
            if len(raw) > MAX_EVENT_BYTES:
                await self._error("event_too_large", "Client event is too large", identity, param="event")
                return
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                await self._error("invalid_event_json", "Client event must be UTF-8 JSON", identity, param="event")
                return
        else:
            text = raw
            if len(text.encode("utf-8")) > MAX_EVENT_BYTES:
                await self._error("event_too_large", "Client event is too large", identity, param="event")
                return
        try:
            event = json.loads(text)
        except (TypeError, ValueError):
            event = None
        if not isinstance(event, dict):
            await self._error("invalid_event_json", "Client event must be a JSON object", identity, param="event")
            return

        event_id = event.get("event_id")
        if not isinstance(event_id, str) or len(event_id.encode("utf-8")) > MAX_EVENT_ID_BYTES:
            event_id = None
        event_type = event.get("type")
        if event_type == "conversation.item.create":
            await self._accept_conversation_item(event, identity, event_id)
        elif event_type == "response.create":
            await self._accept_response_create(identity, event_id)
        elif event_type == "response.cancel":
            await self._accept_response_cancel(identity, event_id)
        else:
            label = event_type if isinstance(event_type, str) else "unknown"
            await self._error(
                "unsupported_client_event",
                f"Unsupported client event: {label}",
                identity,
                param="type",
                triggering_event_id=event_id,
            )

    async def speech_started(self, identity: str) -> None:
        item_id = self._new_input_item(identity)
        await self._emit(
            {
                "type": "input_audio_buffer.speech_started",
                "audio_start_ms": self._elapsed_ms(),
                "item_id": item_id,
            }
        )

    async def speech_stopped(self, identity: str) -> None:
        item_id = self._input_items.get(identity) or self._new_input_item(identity)
        await self._emit(
            {
                "type": "input_audio_buffer.speech_stopped",
                "audio_end_ms": self._elapsed_ms(),
                "item_id": item_id,
            }
        )

    async def user_transcript(self, transcript: str, identity: str) -> None:
        item_id = self._input_items.pop(identity, None) or self._new_item_id("input")
        await self._emit(
            {
                "type": "conversation.item.created",
                "previous_item_id": None,
                "item": {
                    "id": item_id,
                    "type": "message",
                    "status": "completed",
                    "role": "user",
                    "content": [{"type": "input_audio", "transcript": transcript}],
                },
            }
        )
        await self._emit(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "item_id": item_id,
                "content_index": 0,
                "transcript": transcript,
                "usage": {
                    "type": "tokens",
                    "total_tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "input_token_details": {"audio_tokens": 0, "text_tokens": 0},
                },
            }
        )

    async def response_started(self) -> None:
        if self._closed or self._active_response_id:
            return
        self._response_sequence += 1
        self._active_response_id = f"resp_{self.session_id}_{self._response_sequence}"
        self._active_output_item_id = None
        self._active_transcript = None
        await self._emit(
            {
                "type": "response.created",
                "response": {"id": self._active_response_id, "status": "in_progress", "output": []},
            }
        )

    async def output_started(self) -> None:
        await self.response_started()
        self._speaking = True
        if self._active_response_id:
            self._active_output_item_id = self._active_output_item_id or f"item_{self._active_response_id}_audio"
        await self._emit({"type": "output_audio_buffer.started", "response_id": self._active_response_id})

    async def assistant_transcript(self, transcript: str) -> None:
        await self.response_started()
        if not self._active_response_id:
            return
        self._active_output_item_id = self._active_output_item_id or f"item_{self._active_response_id}_audio"
        self._active_transcript = transcript
        await self._emit(
            {
                "type": "response.output_audio_transcript.done",
                "response_id": self._active_response_id,
                "item_id": self._active_output_item_id,
                "output_index": 0,
                "content_index": 0,
                "transcript": transcript,
            }
        )

    async def output_stopped(self) -> None:
        response_id = self._active_response_id
        await self._complete_response("completed")
        if self._speaking:
            self._speaking = False
            await self._emit({"type": "output_audio_buffer.stopped", "response_id": response_id})

    async def response_failed(self) -> None:
        await self._complete_response("failed")

    async def close(self) -> None:
        self._closed = True
        self._input_items.clear()
        self._active_response_id = None
        self._active_output_item_id = None
        self._active_transcript = None
        self._speaking = False

    async def _accept_conversation_item(
        self,
        event: dict[str, Any],
        identity: str,
        event_id: str | None,
    ) -> None:
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "user":
            await self._error(
                "unsupported_conversation_item",
                "Only user message input is supported",
                identity,
                param="item",
                triggering_event_id=event_id,
            )
            return
        content = item.get("content")
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict):
            await self._error("invalid_conversation_item", "item.content must contain one input", identity, param="item.content", triggering_event_id=event_id)
            return
        part = content[0]
        text = part.get("text") if part.get("type") == "input_text" else None
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-8")) > MAX_TEXT_BYTES:
            await self._error("invalid_conversation_item", "input_text is invalid", identity, param="item.content", triggering_event_id=event_id)
            return
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id or len(item_id.encode("utf-8")) > MAX_EVENT_ID_BYTES:
            item_id = self._new_item_id("input")
        normalized = {
            "id": item_id,
            "type": "message",
            "status": "completed",
            "role": "user",
            "content": [{"type": "input_text", "text": text.strip()}],
        }
        await self._emit({"type": "conversation.item.created", "previous_item_id": None, "item": normalized})
        if not await _call(self._on_text_input, text.strip(), identity):
            await self._error("text_input_unavailable", "Text input is unavailable", identity, triggering_event_id=event_id)

    async def _accept_response_create(self, identity: str, event_id: str | None) -> None:
        if self._active_response_id:
            await self._error("conversation_already_has_active_response", "The conversation already has an active response", identity, param="response", triggering_event_id=event_id)
            return
        if not await _call(self._on_response_requested, identity):
            await self._error("response_create_unsupported", "response.create requires conversation input", identity, param="response", triggering_event_id=event_id)

    async def _accept_response_cancel(self, identity: str, event_id: str | None) -> None:
        if not self._active_response_id:
            await self._error("no_active_response", "There is no active response to cancel", identity, param="response", triggering_event_id=event_id)
            return
        response_id = self._active_response_id
        await _call(self._on_response_cancelled, identity)
        await self._complete_response("cancelled")
        if self._speaking:
            self._speaking = False
            await self._emit({"type": "output_audio_buffer.cleared", "response_id": response_id})

    async def _complete_response(self, status: str) -> None:
        response_id = self._active_response_id
        if not response_id:
            return
        output: list[dict[str, Any]] = []
        if status == "completed" and self._active_transcript:
            output.append(
                {
                    "id": self._active_output_item_id or f"item_{response_id}_audio",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_audio", "transcript": self._active_transcript}],
                }
            )
        self._active_response_id = None
        self._active_output_item_id = None
        self._active_transcript = None
        await self._emit({"type": "response.done", "response": {"id": response_id, "status": status, "output": output}})

    async def _error(
        self,
        code: str,
        message: str,
        recipient: str,
        *,
        param: str | None = None,
        triggering_event_id: str | None = None,
    ) -> None:
        await self._emit(
            {
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "code": code,
                    "message": message[:500],
                    "param": param,
                    "event_id": triggering_event_id,
                },
            },
            recipient=recipient,
        )

    async def _emit(self, event: dict[str, Any], recipient: str | None = None) -> None:
        if self._closed:
            return
        self._event_sequence += 1
        payload = {
            "event_id": f"evt_{self.session_id}_{self._event_sequence}_{uuid.uuid4().hex[:8]}",
            **event,
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            self._closed = True
            raise RuntimeError("Realtime event exceeded the data message limit")
        await self._publish(payload, recipient)

    def _elapsed_ms(self) -> int:
        return max(0, int((time.monotonic() - self._started_at) * 1000))

    def _new_item_id(self, label: str) -> str:
        self._input_sequence += 1
        return f"item_{self.session_id}_{label}_{self._input_sequence}"

    def _new_input_item(self, identity: str) -> str:
        item_id = self._new_item_id("input")
        self._input_items[identity] = item_id
        return item_id
