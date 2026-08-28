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
from hermes_livekit.tool_safety import ToolAuditLog, ToolPolicy
from tools.registry import registry


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
    adapter._tool_policy = ToolPolicy.parse(
        json.dumps(
            {
                "tools": [
                    {
                        "participant_identity": identity,
                        "tool_name": "desktop_notify",
                        "tier": 1,
                    }
                ]
            }
        )
    )
    adapter._tool_audit = ToolAuditLog(clock=lambda: 100.0)
    return adapter


def adapter_for_registration(tool_name: str = "desktop_notify") -> LiveKitAdapter:
    adapter = object.__new__(LiveKitAdapter)
    adapter.platform = SimpleNamespace(value="livekit")
    adapter._room = SimpleNamespace(
        remote_participants={"client-a": object(), "client-b": object()}
    )
    adapter._room_generation = 1
    adapter._client_tools = {}
    adapter._tool_owners = {}
    adapter._tool_methods = {}
    adapter._publish_typed = AsyncMock()
    adapter._tool_policy = ToolPolicy.parse(
        json.dumps(
            {
                "tools": [
                    {
                        "participant_identity": identity,
                        "tool_name": tool_name,
                        "tier": 1,
                    }
                    for identity in ("client-a", "client-b")
                ]
            }
        )
    )
    adapter._tool_audit = ToolAuditLog(clock=lambda: 100.0)
    return adapter


def advertised_tool(name: str = "desktop_notify") -> dict[str, object]:
    return {
        "name": name,
        "description": "Show a notification.",
        "input_schema": {"type": "object", "properties": {}},
    }


def conference_tools(*names: str) -> dict[str, object]:
    return {
        "type": "conference.tools.register",
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "Show a notification.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in names
        ],
    }


@pytest.mark.asyncio
async def test_portable_catalog_registers_and_acknowledges_on_shared_topic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = adapter_for_registration()
    registered: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(registry, "get_entry", lambda name: registered.get(name))
    monkeypatch.setattr(
        registry,
        "register",
        lambda **kwargs: registered.__setitem__(str(kwargs["name"]), kwargs),
    )

    await adapter._register_client_tools(
        conference_tools("desktop_notify"), "client-a"
    )

    assert len(registered) == 1
    adapter._publish_typed.assert_awaited_once_with(
        {"type": "conference.tools.registered"},
        identity="client-a",
        topic="conference.tools",
    )


@pytest.mark.asyncio
async def test_invalid_portable_catalog_is_rejected_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = adapter_for_registration()
    register = AsyncMock()
    monkeypatch.setattr(registry, "register", register)

    await adapter._register_client_tools(
        {"type": "conference.tools.register", "tools": [{"type": "function"}]},
        "client-a",
    )

    register.assert_not_called()
    adapter._publish_typed.assert_awaited_once_with(
        {"type": "conference.tools.rejected"},
        identity="client-a",
        topic="conference.tools",
    )


@pytest.mark.asyncio
async def test_same_advertised_name_gets_distinct_scoped_slots_and_routes_to_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = adapter_for_registration()
    registered: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(registry, "get_entry", lambda name: registered.get(name))
    monkeypatch.setattr(
        registry,
        "register",
        lambda **kwargs: registered.__setitem__(str(kwargs["name"]), kwargs),
    )
    participant = FakeLocalParticipant()
    adapter._room = SimpleNamespace(
        local_participant=participant,
        remote_participants={"client-a": object(), "client-b": object()},
    )
    adapter._tool_call_timeout = 12.5

    await adapter._register_client_tool(advertised_tool(), "client-a")
    await adapter._register_client_tool(advertised_tool(), "client-b")

    assert len(registered) == 2
    assert set(adapter._tool_owners.values()) == {"client-a", "client-b"}
    for scoped_name, registration in registered.items():
        assert len(scoped_name) <= 64
        assert registration["schema"]["name"] == scoped_name
        owner = adapter._tool_owners[scoped_name]
        await registration["handler"]({"owner": owner})
    assert [call["destination_identity"] for call in participant.calls] == [
        adapter._tool_owners[name] for name in registered
    ]
    assert {call["method"] for call in participant.calls} == {"desktop_notify"}


@pytest.mark.asyncio
async def test_camera_snapshot_registers_routes_and_unregisters_for_two_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = adapter_for_registration("camera.snapshot")
    registered: dict[str, dict[str, object]] = {}
    removed: list[str] = []
    monkeypatch.setattr(registry, "get_entry", lambda name: registered.get(name))
    monkeypatch.setattr(
        registry,
        "register",
        lambda **kwargs: registered.__setitem__(str(kwargs["name"]), kwargs),
    )
    monkeypatch.setattr(registry, "deregister", removed.append)
    participant = FakeLocalParticipant()
    adapter._room = SimpleNamespace(
        local_participant=participant,
        remote_participants={
            "client-a": object(),
            "client-b": object(),
            "client-c": object(),
        },
    )
    adapter._tool_call_timeout = 12.5
    message = advertised_tool("camera.snapshot")

    await adapter._register_client_tool(message, "client-a")
    await adapter._register_client_tool(message, "client-b")
    accepted = await adapter._register_client_tool(message, "client-c")
    assert len(registered) == 2
    assert accepted is False
    assert all("." not in scoped_name for scoped_name in registered)

    for scoped_name, registration in registered.items():
        await registration["handler"]({"request": scoped_name})
    assert {call["destination_identity"] for call in participant.calls} == {
        "client-a",
        "client-b",
    }
    assert {call["method"] for call in participant.calls} == {"camera.snapshot"}

    client_a_slot = next(iter(adapter._client_tools["client-a"]))
    client_b_slot = next(iter(adapter._client_tools["client-b"]))
    await adapter._register_client_tools(conference_tools(), "client-a")
    assert removed == [client_a_slot]
    assert client_b_slot in adapter._tool_owners


@pytest.mark.asyncio
async def test_empty_registration_and_disconnect_are_scoped_to_exact_participant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = adapter_for_registration()
    registered: dict[str, dict[str, object]] = {}
    removed: list[str] = []
    monkeypatch.setattr(registry, "get_entry", lambda name: registered.get(name))
    monkeypatch.setattr(
        registry,
        "register",
        lambda **kwargs: registered.__setitem__(str(kwargs["name"]), kwargs),
    )
    monkeypatch.setattr(registry, "deregister", removed.append)
    await adapter._register_client_tool(advertised_tool(), "client-a")
    await adapter._register_client_tool(advertised_tool(), "client-b")
    client_a_slot = next(iter(adapter._client_tools["client-a"]))
    client_b_slot = next(iter(adapter._client_tools["client-b"]))

    await adapter._register_client_tools(conference_tools(), "client-a")

    assert removed == [client_a_slot]
    assert client_b_slot in adapter._tool_owners
    adapter._cleanup_client_tools("client-b")
    assert removed == [client_a_slot, client_b_slot]
    assert adapter._client_tools == {}
    assert adapter._tool_owners == {}
    assert adapter._tool_methods == {}


@pytest.mark.asyncio
async def test_reconnect_reregisters_only_the_same_owner_method_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = adapter_for_registration()
    registered: dict[str, dict[str, object]] = {}
    calls: list[str] = []
    monkeypatch.setattr(registry, "get_entry", lambda name: registered.get(name))

    def register(**kwargs: object) -> None:
        name = str(kwargs["name"])
        calls.append(name)
        registered[name] = kwargs

    monkeypatch.setattr(registry, "register", register)
    monkeypatch.setattr(registry, "deregister", lambda name: registered.pop(name))
    await adapter._register_client_tool(advertised_tool(), "client-a")
    await adapter._register_client_tool(advertised_tool(), "client-a")
    adapter._cleanup_client_tools("client-a")
    await adapter._register_client_tool(advertised_tool(), "client-a")

    assert len(set(calls)) == 1
    assert len(calls) == 3
    assert adapter._client_tools == {"client-a": {calls[0]}}


@pytest.mark.asyncio
async def test_scoped_name_collision_fails_before_registry_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = adapter_for_registration()
    adapter._tool_owners = {"lk_collision": "client-a"}
    adapter._tool_methods = {"lk_collision": "desktop_notify"}
    adapter._client_tools = {"client-a": {"lk_collision"}}
    register = AsyncMock()
    monkeypatch.setattr(adapter, "_scoped_tool_name", lambda identity, name: "lk_collision")
    monkeypatch.setattr(registry, "get_entry", lambda name: object())
    monkeypatch.setattr(registry, "register", register)

    accepted = await adapter._register_client_tool(advertised_tool(), "client-b")

    register.assert_not_called()
    assert accepted is False
    assert adapter._tool_owners == {"lk_collision": "client-a"}


def tool_packet(identity: str = "client-a") -> SimpleNamespace:
    return SimpleNamespace(
        topic=LiveKitAdapter.DATA_CHANNEL_TOOLS_TOPIC,
        participant=SimpleNamespace(identity=identity),
        data=json.dumps(conference_tools("desktop_notify")).encode(),
    )


@pytest.mark.asyncio
async def test_queued_registration_after_disconnect_cannot_install_orphan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = adapter_for_registration()
    scheduled: list[object] = []
    register = AsyncMock()
    monkeypatch.setattr(asyncio, "create_task", lambda coroutine: scheduled.append(coroutine))
    monkeypatch.setattr(registry, "register", register)

    adapter._on_data_received(tool_packet())
    adapter._room.remote_participants.clear()
    await scheduled.pop()

    register.assert_not_called()
    assert adapter._client_tools == {}


@pytest.mark.asyncio
async def test_old_generation_registration_cannot_mutate_replacement_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = adapter_for_registration()
    scheduled: list[object] = []
    registered: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(asyncio, "create_task", lambda coroutine: scheduled.append(coroutine))
    monkeypatch.setattr(registry, "get_entry", lambda name: registered.get(name))
    monkeypatch.setattr(
        registry,
        "register",
        lambda **kwargs: registered.__setitem__(str(kwargs["name"]), kwargs),
    )

    old_room = adapter._room
    old_generation = adapter._room_generation
    adapter._room = SimpleNamespace(remote_participants={"client-a": object()})
    adapter._room_generation = 2
    adapter._on_data_received(
        tool_packet(),
        receiving_room=old_room,
        receiving_generation=old_generation,
    )
    await scheduled.pop()
    assert registered == {}

    adapter._on_data_received(tool_packet())
    await scheduled.pop()
    assert len(registered) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    [" client", "client ", "client\n", "client\u0085", "client\u200b", "\ud800", "", "é" * 65],
)
async def test_invalid_or_ambiguous_owner_identity_never_mutates_registry(
    identity: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = adapter_for_registration()
    register = AsyncMock()
    monkeypatch.setattr(registry, "register", register)

    await adapter._register_client_tool(advertised_tool(), identity)

    register.assert_not_called()
    assert not LiveKitAdapter._valid_tool_owner_identity(identity)
    assert adapter._client_tools == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name",
    [
        " desktop_notify",
        "desktop_notify ",
        "desktop_notify\n",
        ".camera",
        "camera.",
        "camera..snapshot",
        "camera/snapshot",
        "camera\\snapshot",
        "camera.\u200bsnapshot",
        "cámara.snapshot",
        "a" * 65,
        7,
    ],
)
async def test_invalid_or_ambiguous_tool_name_never_mutates_registry(
    name: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = adapter_for_registration()
    register = AsyncMock()
    monkeypatch.setattr(registry, "register", register)

    await adapter._register_client_tool(advertised_tool(name), "client-a")

    register.assert_not_called()
    assert adapter._client_tools == {}


def test_scoped_name_is_deterministic_bounded_and_pair_specific() -> None:
    longest_name = "a" * 64
    first = LiveKitAdapter._scoped_tool_name("participant-a", longest_name)

    assert first == LiveKitAdapter._scoped_tool_name("participant-a", longest_name)
    assert len(first) == 64
    assert first != LiveKitAdapter._scoped_tool_name("participant-b", longest_name)
    assert first != LiveKitAdapter._scoped_tool_name("participant-a", "a" * 63 + "b")
    dotted = LiveKitAdapter._scoped_tool_name("participant-a", "camera.snapshot")
    underscored = LiveKitAdapter._scoped_tool_name("participant-a", "camera_snapshot")
    assert "." not in dotted
    assert dotted != underscored


@pytest.mark.asyncio
async def test_remote_tool_handler_performs_targeted_rpc_and_returns_json_text() -> None:
    participant = FakeLocalParticipant('{"shown":true,"count":2}')
    adapter = adapter_with_client(participant)

    result = await adapter._build_tool_handler("client-1", "desktop_notify")(
        {"title": "Hello"}, ignored_framework_value=True
    )

    assert result == '{"shown":true,"count":2}'
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
    assert events[1][0] == "rpc"
    assert events[2][0] == "publish"
    assert events[2][1][0]["type"] == "conference.tools.register"
    assert events[2][1][1] == "conference.tools"


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
