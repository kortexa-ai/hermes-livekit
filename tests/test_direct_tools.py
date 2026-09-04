"""Direct Realtime client tools remain call-owned at the Hermes registry edge."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from hermes_livekit.direct_tools import (
    DIRECT_TOOLSET_NAME,
    DirectToolBridge,
    DirectToolError,
    parse_direct_tools,
)
from tools.registry import registry


def direct_session(*, choice: str = "auto") -> dict:
    return {
        "type": "realtime",
        "tools": [{
            "type": "function",
            "name": "fixture_echo",
            "description": "Return a value.",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        }],
        "tool_choice": choice,
    }


def test_parses_openai_flat_function_tools_and_supported_choices() -> None:
    tools, choice = parse_direct_tools(direct_session())

    assert choice == "auto"
    assert tools[0].name == "fixture_echo"
    assert tools[0].parameters["type"] == "object"

    required_tools, required = parse_direct_tools(direct_session(choice="required"))
    assert required == "required"
    assert required_tools == tools

    disabled_tools, disabled = parse_direct_tools(direct_session(choice="none"))
    assert disabled == "none"
    assert disabled_tools == tools

    named_session = direct_session()
    named_session["tool_choice"] = {"type": "function", "name": "fixture_echo"}
    _, named = parse_direct_tools(named_session)
    assert named == {"type": "function", "name": "fixture_echo"}


@pytest.mark.parametrize(
    ("tools", "message"),
    [
        ("bad", "tools must be an array"),
        ([{"type": "function"}], "name is invalid"),
        ([{
            "type": "function",
            "name": "fixture_echo",
            "parameters": {"type": "string"},
        }], "object schema"),
        ([{
            "type": "function",
            "name": "fixture_echo",
            "parameters": {"type": "object", "default": float("nan")},
        }], "valid JSON"),
        ([direct_session()["tools"][0]] * 2, "duplicate tool"),
    ],
)
def test_rejects_malformed_and_duplicate_direct_tools(tools, message: str) -> None:
    session = direct_session()
    session["tools"] = tools

    with pytest.raises(DirectToolError, match=message):
        parse_direct_tools(session)


def test_bounds_direct_tool_catalog_and_choice() -> None:
    session = direct_session()
    session["tools"][0]["description"] = "x" * (4 * 1024 + 1)
    with pytest.raises(DirectToolError, match="description is too large"):
        parse_direct_tools(session)

    session = direct_session()
    session["tools"] = [
        {**session["tools"][0], "name": f"tool_{index}"}
        for index in range(17)
    ]
    with pytest.raises(DirectToolError, match="too many tools"):
        parse_direct_tools(session)

    session = direct_session()
    session["tools"][0]["parameters"]["properties"] = {
        "value": {"type": "string", "description": "x" * (64 * 1024)}
    }
    with pytest.raises(DirectToolError, match="schemas are too large"):
        parse_direct_tools(session)

    with pytest.raises(DirectToolError, match="needs at least one tool"):
        parse_direct_tools({"tools": [], "tool_choice": "required"})
    session = direct_session()
    session["tool_choice"] = {"type": "function", "name": "missing"}
    with pytest.raises(DirectToolError, match="declared function"):
        parse_direct_tools(session)


@pytest.mark.asyncio
async def test_scoped_handler_checks_hermes_session_before_proxying(monkeypatch) -> None:
    protocol = AsyncMock()
    protocol.tool_choice = "auto"
    protocol.request_client_tool.return_value = "client-result"
    tools, _choice = parse_direct_tools(direct_session())
    bridge = DirectToolBridge(session_id="agent:main:realtime:dm:call-a", protocol=protocol)
    bridge.register(tools)
    registry_name = next(iter(bridge._registered))
    entry = registry.get_entry(registry_name)
    assert entry is not None
    assert "fixture_echo" in bridge.prompt_hint()
    assert registry_name in bridge.prompt_hint()

    try:
        with pytest.raises(RuntimeError, match="outside its owning session"):
            await entry.handler(
                {"value": "wrong"},
                session_id="agent:main:realtime:dm:call-b",
            )
        monkeypatch.setattr(
            bridge,
            "_lookup_session_key",
            lambda session_id: (
                "agent:main:realtime:dm:call-a"
                if session_id == "20260827_185359_e806d4ac"
                else None
            ),
        )
        assert await asyncio.to_thread(
            lambda: asyncio.run(entry.handler(
                {"value": "ready"},
                session_id="20260827_185359_e806d4ac",
            ))
        ) == "client-result"
        protocol.request_client_tool.assert_awaited_once_with(
            "fixture_echo", {"value": "ready"}
        )
    finally:
        bridge.close()

    assert registry.get_entry(registry_name) is None


@pytest.mark.asyncio
async def test_parallel_direct_invocations_are_serialized_deterministically() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def request(name: str, _arguments: dict) -> str:
        calls.append(name)
        if name == "first":
            first_started.set()
            await release_first.wait()
        return name

    protocol = AsyncMock()
    protocol.tool_choice = "auto"
    protocol.request_client_tool.side_effect = request
    session = direct_session()
    session["tools"] = [
        {**session["tools"][0], "name": "first"},
        {**session["tools"][0], "name": "second"},
    ]
    tools, _ = parse_direct_tools(session)
    bridge = DirectToolBridge(session_id="owned", protocol=protocol)
    bridge.register(tools)
    entries = {
        advertised: registry.get_entry(registry_name)
        for registry_name, (advertised, _entry, _scope) in bridge._registered.items()
    }
    try:
        first = asyncio.create_task(entries["first"].handler({}, session_id="owned"))
        await first_started.wait()
        second = asyncio.create_task(entries["second"].handler({}, session_id="owned"))
        await asyncio.sleep(0)
        assert calls == ["first"]
        release_first.set()
        assert await asyncio.gather(first, second) == ["first", "second"]
        assert calls == ["first", "second"]
    finally:
        bridge.close()


def test_required_and_named_choices_are_injected_into_the_turn_prompt() -> None:
    protocol = AsyncMock()
    protocol.tool_choice = "auto"
    tools, _ = parse_direct_tools(direct_session())
    bridge = DirectToolBridge(session_id="owned", protocol=protocol)
    bridge.register(tools)
    try:
        assert "must invoke at least one" in bridge.prompt_hint("required")
        named = bridge.prompt_hint({"type": "function", "name": "fixture_echo"})
        assert "must invoke the function" in named
        assert next(iter(bridge._registered)) in named
    finally:
        bridge.close()


def test_tool_choice_limits_the_exposed_hermes_registry_surface() -> None:
    protocol = AsyncMock()
    session = direct_session()
    session["tools"] = [
        session["tools"][0],
        {**session["tools"][0], "name": "other"},
    ]
    tools, _ = parse_direct_tools(session)
    bridge = DirectToolBridge(session_id="owned", protocol=protocol)
    bridge.register(tools)
    try:
        from toolsets import TOOLSETS

        static_names = TOOLSETS[DIRECT_TOOLSET_NAME]["tools"]
        bridge.set_tool_choice("none")
        assert not set(bridge._registered).intersection(static_names)
        bridge.set_tool_choice({"type": "function", "name": "fixture_echo"})
        exposed = set(bridge._registered).intersection(static_names)
        assert len(exposed) == 1
        assert bridge._definitions[next(iter(exposed))].name == "fixture_echo"
        bridge.set_tool_choice("required")
        assert set(bridge._registered).issubset(static_names)
    finally:
        bridge.close()


@pytest.mark.asyncio
async def test_handler_enforces_none_and_named_function_choices() -> None:
    protocol = AsyncMock()
    session = direct_session()
    session["tools"] = [
        session["tools"][0],
        {**session["tools"][0], "name": "other"},
    ]
    tools, _ = parse_direct_tools(session)
    bridge = DirectToolBridge(session_id="owned", protocol=protocol)
    bridge.register(tools)
    entries = {
        advertised: registry.get_entry(registry_name)
        for registry_name, (advertised, _entry, _scope) in bridge._registered.items()
    }
    try:
        protocol.tool_choice = "none"
        with pytest.raises(RuntimeError, match="disabled"):
            await entries["fixture_echo"].handler({}, session_id="owned")
        protocol.tool_choice = {"type": "function", "name": "fixture_echo"}
        with pytest.raises(RuntimeError, match="does not match"):
            await entries["other"].handler({}, session_id="owned")
    finally:
        bridge.close()


def test_registered_direct_toolset_is_selected_for_realtime_platform() -> None:
    from hermes_cli.tools_config import _get_platform_tools

    protocol = AsyncMock()
    tools, _choice = parse_direct_tools(direct_session())
    bridge = DirectToolBridge(session_id="agent:main:realtime:dm:call-a", protocol=protocol)
    bridge.register(tools)
    try:
        assert DIRECT_TOOLSET_NAME in _get_platform_tools({}, "realtime")
    finally:
        bridge.close()
