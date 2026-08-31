"""Transport-neutral OpenAI-compatible Realtime session state."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Optional


MAX_EVENT_BYTES = 256 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_EVENT_ID_BYTES = 128
MAX_INSTRUCTIONS_BYTES = 64 * 1024
MAX_TOOL_ARGUMENT_BYTES = 64 * 1024
MAX_TOOL_OUTPUT_BYTES = 64 * 1024
DEFAULT_TOOL_TIMEOUT_SECONDS = 30.0

Publish = Callable[[dict[str, Any], Optional[str]], Awaitable[bool]]
ClientCallback = Callable[[str], Awaitable[None] | None]
TextCallback = Callable[[str, str], Awaitable[None] | None]
InputAudioStateCallback = Callable[[bool, str], Awaitable[None] | None]
HERMES_INPUT_AUDIO_STATE = "hermes.input_audio.state"
HERMES_INPUT_AUDIO_STATE_UPDATED = "hermes.input_audio.state_updated"


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
        on_input_audio_state: InputAudioStateCallback | None = None,
        instructions: str = "",
        tool_choice: str = "auto",
        tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
    ) -> None:
        self.session_id = session_id
        self.model = model
        self.voice = voice
        self._publish = publish
        self._on_text_input = on_text_input
        self._on_response_requested = on_response_requested
        self._on_response_cancelled = on_response_cancelled
        self._on_input_audio_state = on_input_audio_state
        self.instructions = instructions
        self.tool_choice = tool_choice
        self._tool_timeout_seconds = tool_timeout_seconds
        self._started_at = time.monotonic()
        self._event_sequence = 0
        self._input_sequence = 0
        self._response_sequence = 0
        self._input_items: dict[str, str] = {}
        self._pending_text_inputs: dict[str, str] = {}
        self._clients: set[str] = set()
        self._active_response_id: str | None = None
        self._active_output_item_id: str | None = None
        self._active_transcript: str | None = None
        self._output_item_announced = False
        self._speaking = False
        self._pending_tool: dict[str, Any] | None = None
        self._closed = False

    @property
    def active_response_id(self) -> str | None:
        return self._active_response_id

    async def client_connected(self, identity: str) -> None:
        if self._closed or not identity or identity in self._clients:
            return
        self._clients.add(identity)
        await self._emit(
            {
                "type": "session.created",
                "session": self._session_snapshot(),
            },
            recipient=identity,
        )

    def client_disconnected(self, identity: str) -> None:
        self._clients.discard(identity)
        self._input_items.pop(identity, None)
        self._pending_text_inputs.pop(identity, None)

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
        if event_type == "session.update":
            await self._accept_session_update(event, identity, event_id)
        elif event_type == "conversation.item.create":
            await self._accept_conversation_item(event, identity, event_id)
        elif event_type == "response.create":
            await self._accept_response_create(identity, event_id)
        elif event_type == "response.cancel":
            await self._accept_response_cancel(identity, event_id)
        elif event_type == HERMES_INPUT_AUDIO_STATE:
            await self._accept_input_audio_state(event, identity, event_id)
        else:
            label = event_type if isinstance(event_type, str) else "unknown"
            await self._error(
                "unsupported_client_event",
                f"Unsupported client event: {label}",
                identity,
                param="type",
                triggering_event_id=event_id,
            )

    async def _accept_input_audio_state(
        self,
        event: dict[str, Any],
        identity: str,
        event_id: str | None,
    ) -> None:
        muted = event.get("muted")
        if not isinstance(muted, bool):
            await self._error(
                "invalid_input_audio_state",
                "muted must be a boolean",
                identity,
                param="muted",
                triggering_event_id=event_id,
            )
            return
        if not await _call(self._on_input_audio_state, muted, identity):
            await self._error(
                "unsupported_client_event",
                "Input audio state is not supported by this transport",
                identity,
                param="type",
                triggering_event_id=event_id,
            )
            return
        await self._emit(
            {
                "type": HERMES_INPUT_AUDIO_STATE_UPDATED,
                "muted": muted,
            },
            recipient=identity,
        )

    def _session_snapshot(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "type": "realtime",
            "model": self.model,
            "instructions": self.instructions,
            "tool_choice": self.tool_choice,
            "audio": {"output": {"voice": self.voice}},
        }

    async def _accept_session_update(
        self,
        event: dict[str, Any],
        identity: str,
        event_id: str | None,
    ) -> None:
        session = event.get("session")
        if not isinstance(session, dict):
            await self._error(
                "invalid_session",
                "session must be an object",
                identity,
                param="session",
                triggering_event_id=event_id,
            )
            return
        allowed = {"type", "instructions", "tool_choice"}
        unknown = next((key for key in session if key not in allowed), None)
        if unknown is not None:
            await self._error(
                "unsupported_session_field",
                f"Unsupported session field: {unknown}",
                identity,
                param=f"session.{unknown}",
                triggering_event_id=event_id,
            )
            return
        if session.get("type") != "realtime":
            await self._error(
                "unsupported_session_type",
                "Only realtime sessions are supported",
                identity,
                param="session.type",
                triggering_event_id=event_id,
            )
            return
        instructions = session.get("instructions", self.instructions)
        if not isinstance(instructions, str) or len(instructions.encode("utf-8")) > MAX_INSTRUCTIONS_BYTES:
            await self._error(
                "invalid_session",
                "session.instructions is invalid",
                identity,
                param="session.instructions",
                triggering_event_id=event_id,
            )
            return
        tool_choice = session.get("tool_choice", self.tool_choice)
        if tool_choice not in {"auto", "none"}:
            await self._error(
                "unsupported_tool_choice",
                "Unsupported tool choice",
                identity,
                param="session.tool_choice",
                triggering_event_id=event_id,
            )
            return
        self.instructions = instructions
        self.tool_choice = tool_choice
        await self._emit(
            {"type": "session.updated", "session": self._session_snapshot()},
            recipient=identity,
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
        item = {
            "id": item_id,
            "type": "message",
            "status": "completed",
            "role": "user",
            "content": [{"type": "input_audio", "transcript": transcript}],
        }
        await self._emit(
            {
                "type": "conversation.item.added",
                "previous_item_id": None,
                "item": item,
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
        await self._emit(
            {
                "type": "conversation.item.done",
                "previous_item_id": None,
                "item": item,
            }
        )

    async def response_started(self) -> None:
        if self._closed or self._active_response_id:
            return
        self._response_sequence += 1
        self._active_response_id = f"resp_{self.session_id}_{self._response_sequence}"
        self._active_output_item_id = None
        self._active_transcript = None
        self._output_item_announced = False
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
            await self._announce_audio_output_item()
        await self._emit({"type": "output_audio_buffer.started", "response_id": self._active_response_id})

    async def assistant_transcript(self, transcript: str) -> None:
        await self.response_started()
        if not self._active_response_id:
            return
        self._active_output_item_id = self._active_output_item_id or f"item_{self._active_response_id}_audio"
        self._active_transcript = transcript
        await self._announce_audio_output_item()
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
        await self._finish_audio_output_item("completed")
        await self._complete_response("completed")
        if self._speaking:
            self._speaking = False
            await self._emit({"type": "output_audio_buffer.stopped", "response_id": response_id})

    async def response_failed(self) -> None:
        await self._finish_audio_output_item("incomplete")
        await self._complete_response("failed")

    async def request_client_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Emit one OpenAI function call and await its client-owned result."""
        if self._closed:
            raise RuntimeError("Realtime session is closed")
        if self._pending_tool is not None:
            raise RuntimeError("A client tool call is already pending")
        try:
            encoded_arguments = json.dumps(
                arguments, ensure_ascii=False, separators=(",", ":")
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Tool arguments are not valid JSON") from exc
        if len(encoded_arguments.encode("utf-8")) > MAX_TOOL_ARGUMENT_BYTES:
            raise RuntimeError("Tool arguments are too large")

        await self.response_started()
        response_id = self._active_response_id
        if not response_id:
            raise RuntimeError("Could not start a function-call response")
        wire_call_id = f"call_{uuid.uuid4().hex}"
        item_id = f"item_{wire_call_id}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending = {
            "call_id": wire_call_id,
            "name": name,
            "future": future,
            "output": None,
        }
        self._pending_tool = pending
        item = {
            "id": item_id,
            "type": "function_call",
            "status": "in_progress",
            "call_id": wire_call_id,
            "name": name,
            "arguments": "",
        }
        await self._emit({
            "type": "response.output_item.added",
            "response_id": response_id,
            "output_index": 0,
            "item": item,
        })
        await self._emit({"type": "conversation.item.added", "previous_item_id": None, "item": item})
        await self._emit({
            "type": "response.function_call_arguments.done",
            "response_id": response_id,
            "item_id": item_id,
            "output_index": 0,
            "call_id": wire_call_id,
            "name": name,
            "arguments": encoded_arguments,
        })
        done_item = {**item, "status": "completed", "arguments": encoded_arguments}
        await self._emit({"type": "conversation.item.done", "previous_item_id": None, "item": done_item})
        await self._emit({
            "type": "response.output_item.done",
            "response_id": response_id,
            "output_index": 0,
            "item": done_item,
        })
        await self._complete_response(
            "completed",
            explicit_output=[{
                "id": item_id,
                "type": "function_call",
                "call_id": wire_call_id,
                "name": name,
                "arguments": encoded_arguments,
            }],
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(future), timeout=self._tool_timeout_seconds
            )
        except asyncio.TimeoutError:
            if self._pending_tool is pending:
                self._pending_tool = None
            if not future.done():
                future.cancel()
            await self._error(
                "tool_timeout",
                "Timed out while waiting for client tool result",
                next(iter(self._clients), ""),
                param="item.output",
            )
            raise RuntimeError("Client tool result timeout") from None

    async def close(self) -> None:
        self._closed = True
        pending, self._pending_tool = self._pending_tool, None
        if pending is not None:
            future = pending["future"]
            if not future.done():
                future.cancel()
        self._clients.clear()
        self._input_items.clear()
        self._pending_text_inputs.clear()
        self._active_response_id = None
        self._active_output_item_id = None
        self._active_transcript = None
        self._output_item_announced = False
        self._speaking = False

    async def _accept_conversation_item(
        self,
        event: dict[str, Any],
        identity: str,
        event_id: str | None,
    ) -> None:
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            await self._accept_tool_output(item, identity, event_id)
            return
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "user":
            await self._error(
                "unsupported_conversation_item",
                "Only user messages and function_call_output are supported",
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
        if identity in self._pending_text_inputs:
            await self._error(
                "conversation_item_pending",
                "Create a response for the pending input before adding another",
                identity,
                param="item",
                triggering_event_id=event_id,
            )
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
        await self._emit_conversation_item(normalized, recipient=identity)
        self._pending_text_inputs[identity] = text.strip()

    async def _accept_response_create(self, identity: str, event_id: str | None) -> None:
        if self._active_response_id:
            await self._error("conversation_already_has_active_response", "The conversation already has an active response", identity, param="response", triggering_event_id=event_id)
            return
        pending_tool = self._pending_tool
        if pending_tool is not None:
            if pending_tool["output"] is None:
                await self._error("tool_outputs_pending", "The tool output is required before response.create", identity, param="response", triggering_event_id=event_id)
                return
            await self.response_started()
            self._pending_tool = None
            future = pending_tool["future"]
            if not future.done():
                future.set_result(pending_tool["output"])
            return
        text = self._pending_text_inputs.pop(identity, None)
        if text is not None:
            if not await _call(self._on_text_input, text, identity):
                await self._error("text_input_unavailable", "Text input is unavailable", identity, triggering_event_id=event_id)
            return
        if not await _call(self._on_response_requested, identity):
            await self._error("response_create_unsupported", "response.create is unavailable", identity, param="response", triggering_event_id=event_id)

    async def _accept_tool_output(
        self,
        item: dict[str, Any],
        identity: str,
        event_id: str | None,
    ) -> None:
        call_id = item.get("call_id")
        output = item.get("output")
        if (
            not isinstance(call_id, str)
            or not call_id
            or len(call_id.encode("utf-8")) > MAX_EVENT_ID_BYTES
            or not isinstance(output, str)
            or len(output.encode("utf-8")) > MAX_TOOL_OUTPUT_BYTES
        ):
            await self._error("invalid_tool_output", "call_id or output is invalid", identity, param="item", triggering_event_id=event_id)
            return
        pending = self._pending_tool
        if pending is None or pending["call_id"] != call_id:
            await self._error("unknown_tool_call", "Tool call is unknown or no longer pending", identity, param="item.call_id", triggering_event_id=event_id)
            return
        if pending["output"] is not None:
            await self._error("duplicate_tool_output", "Tool output was already received", identity, param="item.call_id", triggering_event_id=event_id)
            return
        pending["output"] = output
        await self._emit_conversation_item(
            {
                "id": f"item_{call_id}_output",
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            },
            recipient=identity,
        )

    async def _accept_response_cancel(self, identity: str, event_id: str | None) -> None:
        if not self._active_response_id:
            await self._error("no_active_response", "There is no active response to cancel", identity, param="response", triggering_event_id=event_id)
            return
        response_id = self._active_response_id
        await _call(self._on_response_cancelled, identity)
        await self._finish_audio_output_item("incomplete")
        await self._complete_response("cancelled")
        if self._speaking:
            self._speaking = False
            await self._emit({"type": "output_audio_buffer.cleared", "response_id": response_id})

    async def _complete_response(
        self,
        status: str,
        *,
        explicit_output: list[dict[str, Any]] | None = None,
    ) -> None:
        response_id = self._active_response_id
        if not response_id:
            return
        output: list[dict[str, Any]] = list(explicit_output or [])
        if explicit_output is None and status == "completed" and self._active_transcript:
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
        self._output_item_announced = False
        await self._emit({"type": "response.done", "response": {"id": response_id, "status": status, "output": output}})

    async def _emit_conversation_item(
        self,
        item: dict[str, Any],
        *,
        recipient: str | None = None,
    ) -> None:
        await self._emit(
            {"type": "conversation.item.added", "previous_item_id": None, "item": item},
            recipient=recipient,
        )
        await self._emit(
            {"type": "conversation.item.done", "previous_item_id": None, "item": item},
            recipient=recipient,
        )

    def _audio_output_item(self, status: str) -> dict[str, Any] | None:
        if not self._active_response_id or not self._active_output_item_id:
            return None
        return {
            "id": self._active_output_item_id,
            "type": "message",
            "status": status,
            "role": "assistant",
            "content": [{"type": "output_audio", "transcript": self._active_transcript or ""}],
        }

    async def _announce_audio_output_item(self) -> None:
        if self._output_item_announced or not self._active_response_id:
            return
        item = self._audio_output_item("in_progress")
        if item is None:
            return
        self._output_item_announced = True
        await self._emit({
            "type": "response.output_item.added",
            "response_id": self._active_response_id,
            "output_index": 0,
            "item": item,
        })
        await self._emit({"type": "conversation.item.added", "previous_item_id": None, "item": item})
        await self._emit({
            "type": "response.content_part.added",
            "response_id": self._active_response_id,
            "item_id": self._active_output_item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_audio", "transcript": ""},
        })

    async def _finish_audio_output_item(self, status: str) -> None:
        if not self._output_item_announced or not self._active_response_id or not self._active_output_item_id:
            return
        item = self._audio_output_item(status)
        if item is None:
            return
        await self._emit({
            "type": "response.content_part.done",
            "response_id": self._active_response_id,
            "item_id": self._active_output_item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_audio", "transcript": self._active_transcript or ""},
        })
        await self._emit({"type": "conversation.item.done", "previous_item_id": None, "item": item})
        await self._emit({
            "type": "response.output_item.done",
            "response_id": self._active_response_id,
            "output_index": 0,
            "item": item,
        })
        self._output_item_announced = False

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
