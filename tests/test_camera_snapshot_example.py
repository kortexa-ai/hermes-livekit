"""camera.snapshot example contract without a camera or LiveKit room."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


EXAMPLE_PATH = Path(__file__).parents[1] / "examples" / "test_client.py"
SPEC = importlib.util.spec_from_file_location("camera_snapshot_example", EXAMPLE_PATH)
assert SPEC is not None and SPEC.loader is not None
example = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(example)

STREAM_ID = "0123456789abcdef0123456789abcdef"
TOPIC = f"hermes-tool-result/{STREAM_ID}"


class FakeWriter:
    def __init__(
        self, *, block_write: bool = False, block_close: bool = False
    ) -> None:
        self.block_write = block_write
        self.block_close = block_close
        self.written: list[bytes] = []
        self.write_started = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_gate = asyncio.Event()
        self.closed = asyncio.Event()
        self.close_reason: str | None = None
        self.close_calls = 0

    async def write(self, data: bytes) -> None:
        self.write_started.set()
        if self.block_write:
            await asyncio.Future()
        self.written.append(data)

    async def aclose(self, *, reason: str = "") -> None:
        self.close_calls += 1
        self.close_reason = reason
        self.close_started.set()
        if self.block_close:
            await self.close_gate.wait()
        self.closed.set()


class FakeLocalParticipant:
    def __init__(self, writer: FakeWriter | None = None) -> None:
        self.writer = writer or FakeWriter()
        self.rpc_methods: dict[str, Any] = {}
        self.stream_calls: list[dict[str, Any]] = []

    def register_rpc_method(self, name: str, handler: Any) -> None:
        self.rpc_methods[name] = handler

    def unregister_rpc_method(self, name: str) -> None:
        self.rpc_methods.pop(name, None)

    async def stream_bytes(self, name: str, **kwargs: Any) -> FakeWriter:
        self.stream_calls.append({"name": name, **kwargs})
        return self.writer


class FakeRoom:
    def __init__(self, writer: FakeWriter | None = None) -> None:
        self.local_participant = FakeLocalParticipant(writer)
        self.remote_participants = {}
        self.disconnect_calls = 0

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


def make_client(writer: FakeWriter | None = None):
    client = example.TestClient("test-room", "camera-client")
    client.room = FakeRoom(writer)
    client._loop = asyncio.get_running_loop()
    return client


def invocation(*, caller: str = "hermes-avery", payload: str = "{}"):
    return SimpleNamespace(
        request_id="rpc-1",
        caller_identity=caller,
        payload=payload,
        response_timeout=30.0,
    )


def packet(message: dict[str, Any], *, sender: str = "hermes-avery"):
    return SimpleNamespace(
        data=json.dumps(message).encode(),
        topic=example.HERMES_CONTROL_TOPIC,
        participant=SimpleNamespace(identity=sender),
    )


def ready_message() -> dict[str, str]:
    return {
        "type": "agent:tool-result-stream-ready",
        "stream_id": STREAM_ID,
        "topic": TOPIC,
    }


def cancel_message() -> dict[str, str]:
    return {
        "type": "agent:tool-result-stream-cancel",
        "stream_id": STREAM_ID,
        "topic": TOPIC,
    }


@pytest.mark.asyncio
async def test_registration_uses_existing_discovery_and_rpc_path() -> None:
    client = make_client()
    published: list[tuple[dict[str, Any], str]] = []

    async def publish(message: dict[str, Any], topic: str = "") -> None:
        published.append((message, topic))

    client.publish = publish
    await client.register_tool()

    assert set(client.room.local_participant.rpc_methods) == {
        example.TOOL_NAME,
        example.CAMERA_TOOL_NAME,
    }
    camera = [message for message, _topic in published if message.get("name") == "camera.snapshot"]
    assert camera == [
        {
            "type": "client:tool-register",
            "name": "camera.snapshot",
            "description": example.CAMERA_TOOL_DESCRIPTION,
            "input_schema": example.CAMERA_TOOL_SCHEMA,
        }
    ]
    assert all(topic == example.HERMES_CONTROL_TOPIC for _message, topic in published)


@pytest.mark.asyncio
async def test_snapshot_success_targets_only_invoking_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(example.uuid, "uuid4", lambda: SimpleNamespace(hex=STREAM_ID))
    client = make_client()
    result = json.loads(await client._handle_camera_snapshot(invocation()))

    assert result == {
        "type": "livekit-byte-stream",
        "version": 1,
        "owner_identity": "camera-client",
        "stream_id": STREAM_ID,
        "topic": TOPIC,
        "mime_type": "image/png",
        "expected_size": len(example.CAMERA_FIXTURE_BYTES),
        "text_summary": "Example camera snapshot.",
    }
    client._on_data(packet(ready_message(), sender="another-agent"))
    await asyncio.sleep(0)
    assert client.room.local_participant.stream_calls == []

    client._on_data(packet(ready_message()))
    for _ in range(10):
        await asyncio.sleep(0)
        if STREAM_ID not in client._pending_snapshots:
            break

    assert client.room.local_participant.stream_calls == [
        {
            "name": "camera.snapshot.png",
            "total_size": len(example.CAMERA_FIXTURE_BYTES),
            "mime_type": "image/png",
            "stream_id": STREAM_ID,
            "destination_identities": ["hermes-avery"],
            "topic": TOPIC,
        }
    ]
    writer = client.room.local_participant.writer
    assert writer.written == [example.CAMERA_FIXTURE_BYTES]
    assert writer.close_reason == ""
    assert client._pending_snapshots == {}


@pytest.mark.asyncio
async def test_snapshot_cancel_closes_writer_and_clears_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(example.uuid, "uuid4", lambda: SimpleNamespace(hex=STREAM_ID))
    writer = FakeWriter(block_write=True)
    client = make_client(writer)
    await client._handle_camera_snapshot(invocation())
    pending = client._pending_snapshots[STREAM_ID]
    client._on_data(packet(ready_message()))
    await writer.write_started.wait()

    client._on_data(packet(cancel_message()))
    await asyncio.wait_for(writer.closed.wait(), timeout=1)

    assert writer.close_reason == "transfer_cancelled"
    assert pending.payload == bytearray()
    assert client._pending_snapshots == {}


@pytest.mark.asyncio
async def test_snapshot_transfer_timeout_closes_writer_and_cleans_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(example.uuid, "uuid4", lambda: SimpleNamespace(hex=STREAM_ID))
    monkeypatch.setattr(example, "CAMERA_TRANSFER_TIMEOUT_SEC", 0.01)
    writer = FakeWriter(block_write=True)
    client = make_client(writer)
    await client._handle_camera_snapshot(invocation())
    pending = client._pending_snapshots[STREAM_ID]

    client._on_data(packet(ready_message()))
    await asyncio.wait_for(writer.closed.wait(), timeout=1)
    await pending.send_task

    assert writer.close_reason == "transfer_timeout"
    assert pending.payload == bytearray()
    assert client._pending_snapshots == {}


@pytest.mark.asyncio
async def test_timeout_during_close_awaits_the_same_trailer_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(example.uuid, "uuid4", lambda: SimpleNamespace(hex=STREAM_ID))
    monkeypatch.setattr(example, "CAMERA_TRANSFER_TIMEOUT_SEC", 0.01)
    writer = FakeWriter(block_close=True)
    client = make_client(writer)
    await client._handle_camera_snapshot(invocation())
    pending = client._pending_snapshots[STREAM_ID]

    client._on_data(packet(ready_message()))
    await writer.close_started.wait()
    await asyncio.sleep(0.02)
    assert writer.close_calls == 1
    assert not writer.closed.is_set()
    writer.close_gate.set()
    await asyncio.wait_for(writer.closed.wait(), timeout=1)
    await pending.send_task

    assert writer.close_calls == 1
    assert writer.close_reason == ""
    assert client._pending_snapshots == {}


@pytest.mark.asyncio
async def test_cancel_during_close_awaits_the_same_trailer_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(example.uuid, "uuid4", lambda: SimpleNamespace(hex=STREAM_ID))
    writer = FakeWriter(block_close=True)
    client = make_client(writer)
    await client._handle_camera_snapshot(invocation())
    pending = client._pending_snapshots[STREAM_ID]
    client._on_data(packet(ready_message()))
    await writer.close_started.wait()

    client._on_data(packet(cancel_message()))
    await asyncio.sleep(0)
    assert writer.close_calls == 1
    writer.close_gate.set()
    await asyncio.wait_for(writer.closed.wait(), timeout=1)
    with pytest.raises(asyncio.CancelledError):
        await pending.send_task

    assert writer.close_calls == 1
    assert writer.close_reason == ""
    assert client._pending_snapshots == {}


@pytest.mark.asyncio
async def test_never_completing_close_is_detached_after_bounded_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(example.uuid, "uuid4", lambda: SimpleNamespace(hex=STREAM_ID))
    monkeypatch.setattr(example, "CAMERA_CLOSE_TIMEOUT_SEC", 0.01)
    writer = FakeWriter(block_close=True)
    client = make_client(writer)
    await client._handle_camera_snapshot(invocation())
    pending = client._pending_snapshots[STREAM_ID]

    client._on_data(packet(ready_message()))
    await asyncio.wait_for(pending.send_task, timeout=1)

    assert writer.close_calls == 1
    assert not pending.close_task.done()
    assert pending.payload == bytearray()
    assert client._pending_snapshots == {}
    assert client.room.disconnect_calls == 1
    await asyncio.wait_for(client.unregister_tool(), timeout=1)

    pending.close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending.close_task


@pytest.mark.asyncio
async def test_oversize_snapshot_is_rejected_before_pending_or_stream_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(example, "MAX_RESULT_BYTES", 2)
    client = make_client()
    client._snapshot_bytes = b"png"

    with pytest.raises(example.rtc.RpcError, match="camera snapshot unavailable"):
        await client._handle_camera_snapshot(invocation())
    assert client._pending_snapshots == {}
    assert client.room.local_participant.stream_calls == []


@pytest.mark.asyncio
async def test_unclaimed_reference_expires_and_clears_fixture_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(example.uuid, "uuid4", lambda: SimpleNamespace(hex=STREAM_ID))
    monkeypatch.setattr(example, "CAMERA_READY_TIMEOUT_SEC", 0.01)
    client = make_client()
    await client._handle_camera_snapshot(invocation())
    pending = client._pending_snapshots[STREAM_ID]

    await asyncio.sleep(0.02)

    assert pending.payload == bytearray()
    assert client._pending_snapshots == {}
    assert client.room.local_participant.stream_calls == []
