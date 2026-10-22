"""Shared internal model for client-provided function tools."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .tool_safety import valid_tool_name


MAX_CLIENT_TOOLS = 16
MAX_TOOL_DESCRIPTION_BYTES = 4 * 1024
MAX_TOOL_SCHEMA_BYTES = 64 * 1024


class ToolDefinitionError(ValueError):
    """A client-provided function definition is invalid or unsupported."""


@dataclass(frozen=True)
class FunctionToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]

    def registry_schema(self, registry_name: str) -> dict[str, Any]:
        return {
            "name": registry_name,
            "description": self.description,
            "parameters": self.parameters,
        }


def parse_function_tools(
    values: Any,
    *,
    nested: bool,
    allow_dotted_names: bool,
) -> list[FunctionToolDefinition]:
    """Parse flat Realtime or nested Conference function definitions."""
    if not isinstance(values, list):
        raise ToolDefinitionError("tools must be an array")
    if len(values) > MAX_CLIENT_TOOLS:
        raise ToolDefinitionError("too many tools")
    try:
        encoded = json.dumps(
            values, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (RecursionError, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ToolDefinitionError("tools must contain valid JSON") from exc
    if len(encoded) > MAX_TOOL_SCHEMA_BYTES:
        raise ToolDefinitionError("tool schemas are too large")

    parsed: list[FunctionToolDefinition] = []
    names: set[str] = set()
    outer_keys = (
        {"type", "function"}
        if nested
        else {"type", "name", "description", "parameters"}
    )
    inner_keys = {"name", "description", "parameters"}
    for index, value in enumerate(values):
        label = f"tools[{index}]"
        if not isinstance(value, dict):
            raise ToolDefinitionError(f"{label} must be an object")
        unknown = set(value) - outer_keys
        if unknown:
            raise ToolDefinitionError(f"unsupported {label} field: {sorted(unknown)[0]}")
        if value.get("type") != "function":
            raise ToolDefinitionError(f"{label}.type must be function")
        definition = value.get("function") if nested else value
        if not isinstance(definition, dict):
            raise ToolDefinitionError(f"{label}.function must be an object")
        if nested:
            unknown = set(definition) - inner_keys
            if unknown:
                raise ToolDefinitionError(
                    f"unsupported {label}.function field: {sorted(unknown)[0]}"
                )
        name = definition.get("name")
        if not valid_tool_name(name) or (not allow_dotted_names and "." in str(name)):
            raise ToolDefinitionError(f"{label}.name is invalid")
        if name in names:
            raise ToolDefinitionError(f"duplicate tool: {name}")
        names.add(name)
        description = definition.get("description", "")
        if not isinstance(description, str):
            raise ToolDefinitionError(f"{label}.description must be a string")
        if len(description.encode("utf-8")) > MAX_TOOL_DESCRIPTION_BYTES:
            raise ToolDefinitionError(f"{label}.description is too large")
        parameters = definition.get("parameters")
        if not isinstance(parameters, dict) or parameters.get("type") != "object":
            raise ToolDefinitionError(f"{label}.parameters must be an object schema")
        parsed.append(FunctionToolDefinition(name, description, parameters))
    return parsed


def parse_tool_choice(
    value: Any,
    tool_names: set[str],
) -> str | dict[str, str]:
    """Validate the OpenAI Realtime function-tool choice subset."""
    if value == "auto" or value == "none":
        return value
    if value == "required":
        if not tool_names:
            raise ToolDefinitionError("tool_choice required needs at least one tool")
        return value
    if isinstance(value, dict) and set(value) == {"type", "name"}:
        name = value.get("name")
        if (
            value.get("type") == "function"
            and isinstance(name, str)
            and name in tool_names
        ):
            return {"type": "function", "name": name}
    raise ToolDefinitionError(
        "tool_choice must be auto, none, required, or a declared function"
    )
