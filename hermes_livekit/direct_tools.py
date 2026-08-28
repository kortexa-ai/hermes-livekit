"""Session-owned client function tools for the direct Realtime transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .tool_safety import valid_tool_name


DIRECT_TOOLSET_NAME = "realtime-client-tools"
DIRECT_TOOL_PREFIX = "rt_"
MAX_DIRECT_TOOLS = 16
MAX_TOOL_DESCRIPTION_BYTES = 4 * 1024
MAX_TOOL_SCHEMA_BYTES = 64 * 1024


class DirectToolError(ValueError):
    """The OpenAI Realtime tool setup is invalid or unsupported."""


@dataclass(frozen=True)
class DirectToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]


def parse_direct_tools(session: dict[str, Any]) -> tuple[list[DirectToolDefinition], str]:
    """Validate the portable OpenAI function-tool subset used by Hermes Direct."""
    choice = session.get("tool_choice", "auto")
    if choice not in {"auto", "none", "required"}:
        raise DirectToolError("tool_choice must be auto, none, or required")
    if choice == "required":
        raise DirectToolError("tool_choice required is not supported by Hermes Direct")

    raw_tools = session.get("tools", [])
    if not isinstance(raw_tools, list):
        raise DirectToolError("tools must be an array")
    if len(raw_tools) > MAX_DIRECT_TOOLS:
        raise DirectToolError("too many tools")
    try:
        encoded_tools = json.dumps(raw_tools, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise DirectToolError("tools must contain valid JSON") from exc
    if len(encoded_tools) > MAX_TOOL_SCHEMA_BYTES:
        raise DirectToolError("tool schemas are too large")

    parsed: list[DirectToolDefinition] = []
    names: set[str] = set()
    allowed_keys = {"type", "name", "description", "parameters"}
    for index, value in enumerate(raw_tools):
        label = f"tools[{index}]"
        if not isinstance(value, dict):
            raise DirectToolError(f"{label} must be an object")
        unknown = set(value) - allowed_keys
        if unknown:
            raise DirectToolError(f"unsupported {label} field: {sorted(unknown)[0]}")
        if value.get("type") != "function":
            raise DirectToolError(f"{label}.type must be function")
        name = value.get("name")
        # Hermes's public registry contract supports canonical names up to 64
        # characters. Reject OpenAI's wider name subset instead of silently
        # rewriting the public function identity.
        if not valid_tool_name(name) or "." in str(name):
            raise DirectToolError(f"{label}.name is invalid")
        if name in names:
            raise DirectToolError(f"duplicate tool: {name}")
        names.add(name)
        description = value.get("description", "")
        if not isinstance(description, str):
            raise DirectToolError(f"{label}.description must be a string")
        if len(description.encode("utf-8")) > MAX_TOOL_DESCRIPTION_BYTES:
            raise DirectToolError(f"{label}.description is too large")
        parameters = value.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise DirectToolError(f"{label}.parameters must be an object schema")
        parsed.append(DirectToolDefinition(name, description, parameters))

    return ([] if choice == "none" else parsed), choice


def install_direct_toolsets() -> None:
    """Declare the direct platform bundle without requiring a Hermes patch."""
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    TOOLSETS.setdefault(
        DIRECT_TOOLSET_NAME,
        {
            "description": "Tools offered by the active OpenAI Realtime client",
            "tools": [],
            "includes": [],
        },
    )
    # Hermes defers non-core plugin tools behind tool_search by default. A
    # client-provided Realtime function is an active-session surface, like a
    # desktop UI callback, and must stay in the model's native tools array to
    # preserve OpenAI tool semantics. Extend the existing runtime category;
    # no hermes-agent source change is required.
    try:
        from tools import tool_search

        tool_search._DIRECT_SURFACE_TOOLSETS = frozenset({
            *tool_search._DIRECT_SURFACE_TOOLSETS,
            DIRECT_TOOLSET_NAME,
        })
    except Exception:
        pass
    TOOLSETS.setdefault(
        "hermes-realtime",
        {
            "description": "Direct Realtime voice toolset",
            "tools": list(_HERMES_CORE_TOOLS),
            "includes": [DIRECT_TOOLSET_NAME],
        },
    )


class DirectToolBridge:
    """Register one call's tools and proxy invocations over its data channel."""

    def __init__(self, *, session_id: str, protocol: Any) -> None:
        self.session_id = session_id
        self.protocol = protocol
        self._registered: dict[str, tuple[str, Any, str | None]] = {}
        self._definitions: dict[str, DirectToolDefinition] = {}
        self._call_lock = asyncio.Lock()
        try:
            self._owner_loop: asyncio.AbstractEventLoop | None = (
                asyncio.get_running_loop()
            )
        except RuntimeError:
            self._owner_loop = None

    def register(self, tools: Iterable[DirectToolDefinition]) -> None:
        from tools.registry import registry
        from toolsets import TOOLSETS

        install_direct_toolsets()
        static_names = TOOLSETS[DIRECT_TOOLSET_NAME]["tools"]
        try:
            for tool in tools:
                registry_name = self._scoped_name(self.session_id, tool.name)
                handler = self._handler(tool.name)
                scope = registry.plugin_scope_for_callable(handler)
                if registry.get_entry(registry_name, scope=scope) is not None:
                    raise DirectToolError(f"tool registry collision: {tool.name}")
                if registry_name not in static_names:
                    static_names.append(registry_name)
                registry.register(
                    name=registry_name,
                    toolset=DIRECT_TOOLSET_NAME,
                    schema={
                        "name": registry_name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                    handler=handler,
                    is_async=True,
                    description=tool.description,
                    scope=scope,
                )
                entry = registry.get_entry(registry_name, scope=scope)
                if entry is None:
                    raise DirectToolError(f"failed to register tool: {tool.name}")
                self._registered[registry_name] = (tool.name, entry, scope)
                self._definitions[registry_name] = tool
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        from tools.registry import registry
        from toolsets import TOOLSETS

        static_names = TOOLSETS.get(DIRECT_TOOLSET_NAME, {}).get("tools", [])
        for registry_name, (_advertised_name, entry, scope) in tuple(
            self._registered.items()
        ):
            try:
                registry.restore_registration(
                    registry_name, entry, None, scope=scope
                )
            finally:
                while registry_name in static_names:
                    static_names.remove(registry_name)
        self._registered.clear()
        self._definitions.clear()

    def prompt_hint(self) -> str:
        """Describe deferred client tools in an ephemeral per-turn prompt."""
        tools = [
            {
                "client_name": definition.name,
                "internal_name": registry_name,
                "description": definition.description,
                "parameters": definition.parameters,
            }
            for registry_name, definition in self._definitions.items()
        ]
        if not tools:
            return ""
        catalog = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
        return (
            "This Realtime call has client-provided function tools. Their exact "
            "schemas and call-scoped internal names are in the JSON below. When "
            "a function is appropriate, invoke the function with its exact "
            "internal_name and matching arguments. If the user explicitly asks "
            "to use a supplied function, you must invoke it before answering. "
            "In a phrase of the form 'with <parameter-name> <value>', the "
            "word after 'with' names the argument and the remaining text is "
            "its value; for example, 'with value ready' means "
            "{\"value\":\"ready\"}. Do not ask for clarification when all "
            "required arguments are present in the request. "
            "Never reveal the internal name.\n"
            f"{catalog}"
        )

    def _handler(self, advertised_name: str):
        async def proxy(args: dict[str, Any] | None = None, **kwargs: Any) -> str:
            invocation_session = kwargs.get("session_id")
            if not self._owns_invocation(invocation_session):
                raise RuntimeError("realtime tool invoked outside its owning session")
            arguments = dict(args or {})
            running_loop = asyncio.get_running_loop()
            owner_loop = self._owner_loop
            if owner_loop is None:
                self._owner_loop = running_loop
                owner_loop = running_loop
            if owner_loop is running_loop:
                return await self._request(advertised_name, arguments)
            result = asyncio.run_coroutine_threadsafe(
                self._request(advertised_name, arguments), owner_loop
            )
            return await asyncio.wrap_future(result)

        return proxy

    def _owns_invocation(self, session_id: Any) -> bool:
        if session_id == self.session_id:
            return True
        if not isinstance(session_id, str) or not session_id:
            return False
        return self._lookup_session_key(session_id) == self.session_id

    @staticmethod
    def _lookup_session_key(session_id: str) -> str | None:
        """Resolve Hermes's persisted session ID to its gateway routing key."""
        try:
            from hermes_state import SessionDB

            database = SessionDB(read_only=True)
            try:
                session = database.get_session(session_id)
            finally:
                database.close()
        except Exception:
            return None
        key = session.get("session_key") if isinstance(session, dict) else None
        return key if isinstance(key, str) and key else None

    async def _request(self, name: str, arguments: dict[str, Any]) -> str:
        async with self._call_lock:
            return await self.protocol.request_client_tool(name, arguments)

    @staticmethod
    def _scoped_name(session_id: str, advertised_name: str) -> str:
        digest = hashlib.sha256(
            session_id.encode("utf-8") + b"\0" + advertised_name.encode("ascii")
        ).hexdigest()[:16]
        readable_limit = 64 - len(DIRECT_TOOL_PREFIX) - len(digest) - 1
        return f"{DIRECT_TOOL_PREFIX}{digest}_{advertised_name[:readable_limit]}"
