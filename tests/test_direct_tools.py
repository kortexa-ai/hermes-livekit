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


def test_parses_openai_flat_function_tools_and_rejects_unenforceable_required() -> None:
    tools, choice = parse_direct_tools(direct_session())

    assert choice == "auto"
    assert tools[0].name == "fixture_echo"
    assert tools[0].parameters["type"] == "object"

    with pytest.raises(DirectToolError, match="required is not supported"):
        parse_direct_tools(direct_session(choice="required"))


@pytest.mark.asyncio
async def test_scoped_handler_checks_hermes_session_before_proxying(monkeypatch) -> None:
    protocol = AsyncMock()
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
