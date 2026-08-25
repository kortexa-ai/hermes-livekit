"""Binary tool-result receiver contract without a LiveKit room."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest

import hermes_livekit.adapter as adapter_module
from hermes_livekit.adapter import (
    MAX_IGNORED_BINARY_DRAINS,
    MAX_PENDING_BINARY_RESULTS,
    LiveKitAdapter,
)


STREAM_ID = "0123456789abcdef0123456789abcdef"
TOPIC = f"hermes-tool-result/{STREAM_ID}"


def reference_payload(
    *, owner: str = "client-1", size: int = 3, mime_type: str = "image/jpeg"
) -> str:
    return json.dumps(
        {
            "type": "livekit-byte-stream",
            "version": 1,
            "owner_identity": owner,
            "stream_id": STREAM_ID,
            "topic": TOPIC,
            "mime_type": mime_type,
            "expected_size": size,
            "text_summary": "Camera snapshot.",
        }
    )


class FakeReader:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        owner: str = "client-1",
        stream_id: str = STREAM_ID,
        topic: str = TOPIC,
        mime_type: str = "image/jpeg",
        size: int = 3,
        hang: bool = False,
    ) -> None:
        self.owner = owner
        self.info = SimpleNamespace(
            stream_id=stream_id,
            topic=topic,
            mime_type=mime_type,
            size=size,
        )
        self.chunks = list(chunks)
        self.hang = hang
        self.waiter: asyncio.Future[None] | None = None

    def __aiter__(self) -> "FakeReader":
        return self

    async def __anext__(self) -> bytes:
        if self.chunks:
            return self.chunks.pop(0)
        if self.hang:
            self.waiter = asyncio.get_running_loop().create_future()
            await self.waiter
        raise StopAsyncIteration


class FakeLocalParticipant:
    def __init__(self, result: str) -> None:
        self.result = result
        self.rpc_calls: list[dict[str, Any]] = []
        self.published: list[dict[str, Any]] = []

    async def perform_rpc(self, **kwargs: Any) -> str:
        self.rpc_calls.append(kwargs)
        return self.result

    async def publish_data(self, data: bytes, **kwargs: Any) -> None:
        self.published.append({"message": json.loads(data), **kwargs})


class FakeRoom:
    def __init__(self, result: str) -> None:
        self.local_participant = FakeLocalParticipant(result)
        self.remote_participants: dict[str, object] = {
            "client-1": object(),
            "observer": object(),
        }
        self.handlers: dict[str, Any] = {}
        self.disconnect_called = False

    def register_byte_stream_handler(self, topic: str, handler: Any) -> None:
        if topic in self.handlers:
            raise ValueError("duplicate handler")
        self.handlers[topic] = handler

    def unregister_byte_stream_handler(self, topic: str) -> None:
        self.handlers.pop(topic, None)

    async def disconnect(self) -> None:
        self.disconnect_called = True

    def deliver(self, reader: FakeReader, identity: str = "client-1") -> None:
        self.handlers[reader.info.topic](reader, identity)


class _TestAdapter(LiveKitAdapter):
    name = "livekit-test"


def make_adapter(result: str) -> tuple[LiveKitAdapter, FakeRoom]:
    adapter = object.__new__(_TestAdapter)
    room = FakeRoom(result)
    adapter._room = room
    adapter._room_generation = 1
    adapter._room_replacement_started = False
    adapter._binary_topics = set()
    adapter._binary_transfers = {}
    adapter._binary_ignored_drains = set()
    adapter._binary_replacement_scheduled = False
    adapter._client_tools = {"client-1": {"camera_snapshot"}}
    adapter._tool_owners = {"camera_snapshot": "client-1"}
    adapter._tool_call_timeout = 30.0
    adapter._running = False
    adapter._graceful_leave = False
    adapter._connect_task = None
    adapter._audio_buffers = {}
    adapter._last_audio_time = {}
    adapter._audio_streams = {}
    adapter._speaking_participants = set()
    adapter._video_streams = {}
    return adapter, room


async def start_call(adapter: LiveKitAdapter) -> asyncio.Task[Any]:
    task = asyncio.create_task(
        adapter._build_tool_handler("client-1", "camera_snapshot")({})
    )
    for _ in range(10):
        await asyncio.sleep(0)
        if TOPIC in adapter._room.handlers:
            return task
    raise AssertionError("binary handler was not registered")


@pytest.mark.asyncio
async def test_success_maps_verified_image_and_releases_state() -> None:
    adapter, room = make_adapter(reference_payload())
    task = await start_call(adapter)
    room.deliver(FakeReader([b"j", b"pg"]))

    assert await task == {
        "_multimodal": True,
        "content": [
            {"type": "text", "text": "Camera snapshot."},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64,anBn"},
            },
        ],
        "text_summary": "Camera snapshot.",
    }
    assert room.local_participant.published[0]["message"]["type"] == (
        "agent:tool-result-stream-ready"
    )
    assert room.local_participant.published[0]["destination_identities"] == [
        "client-1"
    ]
    assert room.handlers == {}
    assert adapter._binary_topics == set()
    assert adapter._binary_transfers == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reader,identity,code",
    [
        (FakeReader([], mime_type="image/png"), "client-1", "header_mismatch"),
        (FakeReader([b"four"]), "client-1", "transfer_incomplete"),
        (FakeReader([b"x"]), "client-1", "transfer_incomplete"),
    ],
)
async def test_spoof_mismatch_overrun_and_incomplete_fail_closed(
    reader: FakeReader, identity: str, code: str
) -> None:
    adapter, room = make_adapter(reference_payload())
    task = await start_call(adapter)
    room.deliver(reader, identity)

    with pytest.raises(RuntimeError) as error:
        await task
    assert str(error.value) == f"binary tool result failed: {code}"
    await asyncio.sleep(0)
    assert adapter._binary_transfers == {}


@pytest.mark.asyncio
async def test_spoof_is_drained_without_claiming_the_owners_transfer() -> None:
    adapter, room = make_adapter(reference_payload())
    task = await start_call(adapter)
    room.deliver(FakeReader([]), "spoof")
    await asyncio.sleep(0)

    assert not task.done()
    assert adapter._binary_transfers[TOPIC].reader_task is None
    room.deliver(FakeReader([b"jpg"]), "client-1")
    result = await task
    assert result["_multimodal"] is True


@pytest.mark.asyncio
async def test_spoof_flood_caps_drains_and_coalesces_room_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter_module, "DRAIN_TIMEOUT_SEC", 30.0)
    adapter, room = make_adapter(reference_payload())
    task = await start_call(adapter)

    for index in range(MAX_IGNORED_BINARY_DRAINS + 20):
        room.deliver(
            FakeReader([], stream_id=f"{index:032x}", hang=True), "spoof"
        )

    assert len(adapter._binary_ignored_drains) == MAX_IGNORED_BINARY_DRAINS
    assert adapter._binary_replacement_scheduled
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="binary tool result failed: room_replaced"):
        await task
    assert room.disconnect_called
    assert adapter._binary_ignored_drains == set()


@pytest.mark.asyncio
async def test_timeout_without_a_stream_removes_handler_and_waiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_parse = adapter_module.parse_reference

    def short_parse(*args: Any, **kwargs: Any):
        return replace(real_parse(*args, **kwargs), transfer_timeout_sec=0.01)

    monkeypatch.setattr(adapter_module, "parse_reference", short_parse)
    adapter, room = make_adapter(reference_payload())

    with pytest.raises(RuntimeError, match="binary tool result failed: transfer_timeout"):
        await adapter._build_tool_handler("client-1", "camera_snapshot")({})
    assert room.handlers == {}
    assert adapter._binary_transfers == {}
    assert adapter._binary_topics == set()


@pytest.mark.asyncio
async def test_cancellation_clears_partial_bytes_and_sends_targeted_cancel() -> None:
    adapter, room = make_adapter(reference_payload())
    task = await start_call(adapter)
    reader = FakeReader([b"j"], hang=True)
    room.deliver(reader)
    await asyncio.sleep(0)

    transfer = adapter._binary_transfers[TOPIC]
    assert bytes(transfer.buffer) == b"j"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert transfer.terminal
    assert transfer.buffer == bytearray()
    assert transfer.result.cancelled()
    assert room.local_participant.published[-1]["message"]["type"] == (
        "agent:tool-result-stream-cancel"
    )
    adapter._fail_binary_generation(1, "room_replaced")


@pytest.mark.asyncio
async def test_owner_disconnect_fails_waiter_and_clears_partial_buffer() -> None:
    adapter, room = make_adapter(reference_payload())
    task = await start_call(adapter)
    reader = FakeReader([b"j"], hang=True)
    room.deliver(reader)
    await asyncio.sleep(0)
    transfer = adapter._binary_transfers[TOPIC]

    adapter._on_participant_disconnected(SimpleNamespace(identity="client-1"))

    with pytest.raises(RuntimeError, match="binary tool result failed: owner_disconnected"):
        await task
    assert transfer.buffer == bytearray()
    adapter._fail_binary_generation(1, "room_replaced")


@pytest.mark.asyncio
async def test_ignored_cancel_replaces_generation_once_and_clears_registrations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(adapter_module, "DRAIN_TIMEOUT_SEC", 0.01)
    real_parse = adapter_module.parse_reference

    def short_parse(*args: Any, **kwargs: Any):
        return replace(real_parse(*args, **kwargs), transfer_timeout_sec=0.01)

    monkeypatch.setattr(adapter_module, "parse_reference", short_parse)
    adapter, room = make_adapter(reference_payload())
    task = await start_call(adapter)
    reader = FakeReader([b"j"], hang=True)
    room.deliver(reader)

    with pytest.raises(RuntimeError, match="binary tool result failed: transfer_timeout"):
        await task
    await asyncio.sleep(0.03)

    assert room.disconnect_called
    assert adapter._room is None
    assert adapter._room_replacement_started
    assert adapter._binary_transfers == {}
    assert adapter._binary_topics == set()
    assert adapter._client_tools == {}
    assert adapter._tool_owners == {}
    await adapter._replace_binary_room_generation(1)
    assert room.disconnect_called


@pytest.mark.asyncio
async def test_room_disconnect_fails_generation_before_reconnect() -> None:
    adapter, room = make_adapter(reference_payload())
    task = await start_call(adapter)
    reader = FakeReader([b"j"], hang=True)
    room.deliver(reader)
    await asyncio.sleep(0)
    adapter._running = True

    async def no_reconnect() -> None:
        return None

    adapter._reconnect_loop = no_reconnect
    adapter._on_disconnected("network")

    with pytest.raises(RuntimeError, match="binary tool result failed: room_replaced"):
        await task
    await asyncio.sleep(0)
    assert adapter._binary_transfers == {}
    assert adapter._binary_topics == set()
    assert adapter._client_tools == {}


@pytest.mark.asyncio
async def test_pending_binary_results_have_a_fixed_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert MAX_PENDING_BINARY_RESULTS == 8
    monkeypatch.setattr(adapter_module, "MAX_PENDING_BINARY_RESULTS", 0)
    adapter, room = make_adapter(reference_payload())

    with pytest.raises(RuntimeError, match="binary tool result failed: too_many_pending"):
        await adapter._build_tool_handler("client-1", "camera_snapshot")({})
    assert room.handlers == {}
    assert adapter._binary_topics == set()
