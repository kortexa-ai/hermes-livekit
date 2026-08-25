"""Remote client tools use LiveKit RPC for invocation and correlation."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_livekit.adapter import LiveKitAdapter


_EXAMPLE_PATH = Path(__file__).parents[1] / "examples" / "test_client.py"
_EXAMPLE_SPEC = importlib.util.spec_from_file_location(
    "hermes_livekit_test_client", _EXAMPLE_PATH
)
assert _EXAMPLE_SPEC is not None and _EXAMPLE_SPEC.loader is not None
example_client = importlib.util.module_from_spec(_EXAMPLE_SPEC)
_EXAMPLE_SPEC.loader.exec_module(example_client)


class FakeLocalParticipant:
    def __init__(self, result: str = '{"ok":true}') -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def perform_rpc(self, **kwargs: object) -> str:
        self.calls.append(kwargs)
        return self.result


def adapter_with_client(
    local_participant: FakeLocalParticipant, identity: str = "client-1"
) -> LiveKitAdapter:
    adapter = object.__new__(LiveKitAdapter)
    adapter._room = SimpleNamespace(
        local_participant=local_participant,
        remote_participants={identity: object()},
    )
    adapter._tool_call_timeout = 12.5
    return adapter


@pytest.mark.asyncio
async def test_remote_tool_handler_performs_targeted_rpc_and_decodes_result() -> None:
    participant = FakeLocalParticipant('{"shown":true,"count":2}')
    adapter = adapter_with_client(participant)

    result = await adapter._build_tool_handler("client-1", "desktop_notify")(
        {"title": "Hello"}, ignored_framework_value=True
    )

    assert result == {"shown": True, "count": 2}
    assert participant.calls == [
        {
            "destination_identity": "client-1",
            "method": "desktop_notify",
            "payload": json.dumps({"title": "Hello"}),
            "response_timeout": 12.5,
        }
    ]


@pytest.mark.asyncio
async def test_remote_tool_handler_rejects_a_disconnected_owner() -> None:
    participant = FakeLocalParticipant()
    adapter = adapter_with_client(participant)
    adapter._room.remote_participants.clear()

    with pytest.raises(RuntimeError, match="is not connected"):
        await adapter._build_tool_handler("client-1", "desktop_notify")({})

    assert participant.calls == []


@pytest.mark.asyncio
async def test_remote_tool_handler_propagates_native_rpc_failures() -> None:
    expected = RuntimeError("native RPC failed")

    class FailingParticipant(FakeLocalParticipant):
        async def perform_rpc(self, **kwargs: object) -> str:
            self.calls.append(kwargs)
            raise expected

    participant = FailingParticipant()
    adapter = adapter_with_client(participant)

    with pytest.raises(RuntimeError) as caught:
        await adapter._build_tool_handler("client-1", "desktop_notify")({})

    assert caught.value is expected


@pytest.mark.asyncio
async def test_remote_tool_handler_rejects_a_non_json_rpc_result() -> None:
    participant = FakeLocalParticipant("not-json")
    adapter = adapter_with_client(participant)

    with pytest.raises(json.JSONDecodeError):
        await adapter._build_tool_handler("client-1", "desktop_notify")({})


@pytest.mark.asyncio
async def test_remote_tool_handler_cancellation_abandons_native_rpc_wait() -> None:
    started = asyncio.Event()

    class BlockingParticipant(FakeLocalParticipant):
        async def perform_rpc(self, **kwargs: object) -> str:
            self.calls.append(kwargs)
            started.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

    adapter = adapter_with_client(BlockingParticipant())
    task = asyncio.create_task(
        adapter._build_tool_handler("client-1", "desktop_notify")({})
    )
    await started.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_hand_rolled_call_result_state_is_gone() -> None:
    adapter = object.__new__(LiveKitAdapter)

    assert not hasattr(adapter, "_pending_tool_calls")
    assert not hasattr(adapter, "_pending_tool_owners")
    assert not hasattr(adapter, "_handle_tool_result")
    assert not hasattr(adapter, "cancel_pending_tool_calls_for_session_reset")


@pytest.mark.asyncio
async def test_example_client_registers_rpc_before_advertising_tool() -> None:
    events: list[tuple[str, object]] = []

    class LocalParticipant:
        def register_rpc_method(self, name: str, handler: object) -> None:
            events.append(("rpc", (name, handler)))

    client = example_client.TestClient("room", "client-1")
    client.room = SimpleNamespace(local_participant=LocalParticipant())

    async def publish(payload: dict[str, object], topic: str = "") -> None:
        events.append(("publish", (payload, topic)))

    client.publish = AsyncMock(side_effect=publish)

    await client.register_tool()

    assert events[0] == (
        "rpc",
        (example_client.TOOL_NAME, client._handle_tool_call),
    )
    assert events[1][0] == "publish"
    assert events[1][1][0]["type"] == "client:tool-register"


@pytest.mark.asyncio
async def test_example_client_rpc_handler_returns_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(example_client, "macos_notify", lambda title, body: None)
    client = example_client.TestClient("room", "client-1")
    invocation = SimpleNamespace(
        payload=json.dumps({"title": "Hello", "body": "World"}),
        request_id="rpc-1",
    )

    result = await client._handle_tool_call(invocation)

    assert json.loads(result) == {"shown": True}


@pytest.mark.asyncio
async def test_example_client_rpc_handler_rejects_malformed_arguments() -> None:
    client = example_client.TestClient("room", "client-1")
    invocation = SimpleNamespace(payload="[]", request_id="rpc-1")

    with pytest.raises(example_client.rtc.RpcError) as caught:
        await client._handle_tool_call(invocation)

    assert caught.value.code == 1500
