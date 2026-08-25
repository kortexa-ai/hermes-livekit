"""Closed protocol contract for bounded LiveKit byte-stream tool results."""

from __future__ import annotations

import base64
import json
import math
import re
from dataclasses import dataclass
from typing import Any


REFERENCE_TYPE = "livekit-byte-stream"
REFERENCE_VERSION = 1
TOPIC_PREFIX = "hermes-tool-result/"
MAX_RESULT_BYTES = 12 * 1024 * 1024
MAX_SUMMARY_CHARS = 1024
MAX_TRANSFER_TIMEOUT_SEC = 120.0
MIN_TRANSFER_RATE_BYTES_SEC = 256 * 1024
TRANSFER_SETUP_SEC = 15.0
DRAIN_TIMEOUT_SEC = 5.0
IMAGE_MIME_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)
LIFECYCLE_FAILURE_CODES = frozenset(
    {
        "transfer_timeout",
        "transfer_incomplete",
        "owner_disconnected",
        "transfer_cancelled",
        "room_replaced",
    }
)
STREAM_READY_TYPE = "agent:tool-result-stream-ready"
STREAM_CANCEL_TYPE = "agent:tool-result-stream-cancel"

_STREAM_ID = re.compile(r"^[0-9a-f]{32}$")
_MIME_TYPE = re.compile(
    r"^[A-Za-z0-9!#$&^_.+-]{1,63}/[A-Za-z0-9!#$&^_.+-]{1,63}$"
)
_REFERENCE_FIELDS = frozenset(
    {
        "type",
        "version",
        "owner_identity",
        "stream_id",
        "topic",
        "mime_type",
        "expected_size",
        "text_summary",
    }
)


class BinaryResultProtocolError(ValueError):
    """A closed protocol failure safe to map to a generic tool error."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BinaryResultReference:
    owner_identity: str
    stream_id: str
    topic: str
    mime_type: str
    expected_size: int
    text_summary: str
    transfer_timeout_sec: float


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BinaryResultProtocolError("invalid_reference")
        result[key] = value
    return result


def _valid_identity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 128
        and value == value.strip()
        and all(ord(char) >= 0x20 and char != "\x7f" for char in value)
    )


def _valid_summary(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= MAX_SUMMARY_CHARS
        and bool(value.strip())
        and all(ord(char) >= 0x20 or char in "\t\n\r" for char in value)
    )


def parse_reference(
    payload: str,
    *,
    rpc_owner_identity: str,
    configured_timeout_sec: float,
) -> BinaryResultReference:
    """Parse an RPC result and bind it to its registered participant owner."""
    if not isinstance(payload, str):
        raise BinaryResultProtocolError("invalid_reference")
    try:
        if len(payload.encode("utf-8")) > 4096:
            raise BinaryResultProtocolError("invalid_reference")
    except UnicodeEncodeError:
        raise BinaryResultProtocolError("invalid_reference") from None
    if not _valid_identity(rpc_owner_identity):
        raise BinaryResultProtocolError("invalid_owner")
    if (
        isinstance(configured_timeout_sec, bool)
        or not isinstance(configured_timeout_sec, (int, float))
        or not math.isfinite(configured_timeout_sec)
        or configured_timeout_sec <= 0
        or configured_timeout_sec > MAX_TRANSFER_TIMEOUT_SEC
    ):
        raise BinaryResultProtocolError("invalid_timeout")

    try:
        raw = json.loads(payload, object_pairs_hook=_object_without_duplicates)
    except BinaryResultProtocolError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise BinaryResultProtocolError("invalid_reference") from None

    if not isinstance(raw, dict) or frozenset(raw) != _REFERENCE_FIELDS:
        raise BinaryResultProtocolError("invalid_reference")
    if (
        raw["type"] != REFERENCE_TYPE
        or isinstance(raw["version"], bool)
        or not isinstance(raw["version"], int)
        or raw["version"] != REFERENCE_VERSION
    ):
        raise BinaryResultProtocolError("invalid_reference")
    if raw["owner_identity"] != rpc_owner_identity:
        raise BinaryResultProtocolError("owner_mismatch")
    if not _STREAM_ID.fullmatch(raw["stream_id"] if isinstance(raw["stream_id"], str) else ""):
        raise BinaryResultProtocolError("invalid_reference")
    expected_topic = TOPIC_PREFIX + raw["stream_id"]
    if raw["topic"] != expected_topic:
        raise BinaryResultProtocolError("invalid_reference")
    if not isinstance(raw["mime_type"], str) or not _MIME_TYPE.fullmatch(raw["mime_type"]):
        raise BinaryResultProtocolError("invalid_reference")
    if raw["mime_type"].startswith("image/") and raw["mime_type"] not in IMAGE_MIME_TYPES:
        raise BinaryResultProtocolError("unsupported_image_type")
    size = raw["expected_size"]
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= MAX_RESULT_BYTES:
        raise BinaryResultProtocolError("invalid_reference")
    if not _valid_summary(raw["text_summary"]):
        raise BinaryResultProtocolError("invalid_reference")

    scaled_timeout = TRANSFER_SETUP_SEC + size / MIN_TRANSFER_RATE_BYTES_SEC
    timeout = min(MAX_TRANSFER_TIMEOUT_SEC, max(float(configured_timeout_sec), scaled_timeout))
    return BinaryResultReference(
        owner_identity=raw["owner_identity"],
        stream_id=raw["stream_id"],
        topic=raw["topic"],
        mime_type=raw["mime_type"],
        expected_size=size,
        text_summary=raw["text_summary"],
        transfer_timeout_sec=timeout,
    )


def validate_stream_header(
    reference: BinaryResultReference,
    *,
    sender_identity: str,
    stream_id: str,
    topic: str,
    mime_type: str,
    total_size: int | None,
) -> None:
    """Require every advertised header field to match before reading bytes."""
    if sender_identity != reference.owner_identity:
        raise BinaryResultProtocolError("owner_mismatch")
    if (
        stream_id != reference.stream_id
        or topic != reference.topic
        or mime_type != reference.mime_type
        or total_size != reference.expected_size
    ):
        raise BinaryResultProtocolError("header_mismatch")


def reserve_stream(reference: BinaryResultReference, outstanding_topics: set[str]) -> str:
    """Reserve one globally unique topic before a LiveKit handler is installed."""
    if reference.topic in outstanding_topics:
        raise BinaryResultProtocolError("stream_collision")
    outstanding_topics.add(reference.topic)
    return reference.topic


def release_stream(reference: BinaryResultReference, outstanding_topics: set[str]) -> None:
    """Release a topic on every terminal path."""
    outstanding_topics.discard(reference.topic)


def validate_completed_size(reference: BinaryResultReference, received_size: int) -> None:
    """Reject short and overlong transfers; partial bytes are never a result."""
    if isinstance(received_size, bool) or received_size != reference.expected_size:
        raise BinaryResultProtocolError("transfer_incomplete")


def bounded_next_size(
    reference: BinaryResultReference, received_size: int, chunk_size: int
) -> int:
    """Account for a chunk before buffering it, rejecting any overrun."""
    if (
        isinstance(received_size, bool)
        or isinstance(chunk_size, bool)
        or not isinstance(received_size, int)
        or not isinstance(chunk_size, int)
        or received_size < 0
        or chunk_size < 0
        or received_size + chunk_size > reference.expected_size
    ):
        raise BinaryResultProtocolError("transfer_incomplete")
    return received_size + chunk_size


def terminal_chunk_action() -> str:
    """Chunks received after local termination are drained, never buffered."""
    return "discard"


def drain_deadline_action(
    *,
    trailer_received: bool,
    deadline_generation: int,
    current_generation: int,
    replacement_started: bool,
) -> str:
    """Coalesce pinned-SDK escalation and ignore stale generation tasks."""
    if deadline_generation != current_generation or replacement_started:
        return "no_op"
    return "release" if trailer_received else "replace_room_generation"


def stream_ready_message(reference: BinaryResultReference) -> dict[str, str]:
    """Build the targeted control message that permits the sender to start."""
    return {
        "type": STREAM_READY_TYPE,
        "stream_id": reference.stream_id,
        "topic": reference.topic,
    }


def stream_cancel_message(reference: BinaryResultReference) -> dict[str, str]:
    """Build the best-effort targeted cancellation message for the sender."""
    return {
        "type": STREAM_CANCEL_TYPE,
        "stream_id": reference.stream_id,
        "topic": reference.topic,
    }


def format_completed_result(reference: BinaryResultReference, payload: bytes) -> dict[str, Any]:
    """Map a verified payload to the Hermes result shape or metadata fallback."""
    if not isinstance(payload, bytes) or len(payload) != reference.expected_size:
        raise BinaryResultProtocolError("transfer_incomplete")
    if reference.mime_type in IMAGE_MIME_TYPES:
        encoded = base64.b64encode(payload).decode("ascii")
        return {
            "_multimodal": True,
            "content": [
                {"type": "text", "text": reference.text_summary},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{reference.mime_type};base64,{encoded}"
                    },
                },
            ],
            "text_summary": reference.text_summary,
        }
    return {
        "binary_result": {
            "mime_type": reference.mime_type,
            "size": reference.expected_size,
            "available_to_model": False,
        },
        "text_summary": reference.text_summary,
    }
