"""Closed remote-tool policy and bounded secret-free audit records."""

from __future__ import annotations

import json
import math
import re
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from typing import Callable


MAX_POLICY_BYTES = 16 * 1024
MAX_POLICY_ENTRIES = 64
MAX_AUDIT_RECORDS = 256
MAX_PARTICIPANT_IDENTITY_BYTES = 128
MAX_TOOL_METHOD_CHARS = 64
TOOL_METHOD_RE = re.compile(
    r"[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*"
)

_POLICY_REASONS = {
    "not-allowlisted",
    "tier1-allowed",
    "consent-valid",
    "consent-expired",
    "tier3-denied",
}
_AUDIT_EVENTS = {
    "registration",
    "policy",
    "invocation",
    "cancellation",
    "owner_disconnect",
}
_AUDIT_OUTCOMES = _POLICY_REASONS | {
    "accepted",
    "denied",
    "error",
    "success",
    "cancelled",
    "removed",
}


class ToolPolicyError(ValueError):
    """A remote-tool policy is malformed or outside its closed bounds."""


def _closed_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ToolPolicyError("policy contains a duplicate key")
        result[key] = value
    return result


def valid_participant_identity(identity: object) -> bool:
    """Return whether an identity has one bounded, unambiguous UTF-8 form."""
    if not isinstance(identity, str) or not identity or identity != identity.strip():
        return False
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in identity):
        return False
    try:
        encoded = identity.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return len(encoded) <= MAX_PARTICIPANT_IDENTITY_BYTES


def valid_tool_name(name: object) -> bool:
    """Return whether a method uses bounded canonical dotted identifiers."""
    return (
        isinstance(name, str)
        and len(name) <= MAX_TOOL_METHOD_CHARS
        and TOOL_METHOD_RE.fullmatch(name) is not None
    )


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    tier: int
    reason: str


@dataclass(frozen=True)
class _PolicyEntry:
    tier: int
    consent_expires_at: float | None


class ToolPolicy:
    """Exact participant/tool policy loaded once from bounded JSON."""

    def __init__(
        self,
        entries: dict[tuple[str, str], _PolicyEntry] | None = None,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._entries = dict(entries or {})
        self._clock = clock

    @classmethod
    def parse(
        cls,
        raw: str,
        *,
        clock: Callable[[], float] = time.time,
    ) -> "ToolPolicy":
        if not isinstance(raw, str):
            raise ToolPolicyError("policy must be text")
        try:
            raw_bytes = raw.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ToolPolicyError("policy must be valid UTF-8") from exc
        if len(raw_bytes) > MAX_POLICY_BYTES:
            raise ToolPolicyError("policy is too large")
        if not raw.strip():
            return cls(clock=clock)
        try:
            document = json.loads(raw, object_pairs_hook=_closed_json_object)
        except (RecursionError, UnicodeError, ValueError) as exc:
            raise ToolPolicyError("policy must be valid JSON") from exc
        if not isinstance(document, dict) or set(document) != {"tools"}:
            raise ToolPolicyError("policy must contain only tools")
        tools = document["tools"]
        if not isinstance(tools, list) or len(tools) > MAX_POLICY_ENTRIES:
            raise ToolPolicyError("tools must be a bounded array")

        entries: dict[tuple[str, str], _PolicyEntry] = {}
        for item in tools:
            if not isinstance(item, dict):
                raise ToolPolicyError("tool policy entry must be an object")
            tier = item.get("tier")
            expected_keys = {"participant_identity", "tool_name", "tier"}
            if tier == 2:
                expected_keys.add("consent_expires_at")
            if (
                set(item) != expected_keys
                or isinstance(tier, bool)
                or not isinstance(tier, int)
                or tier not in {1, 2, 3}
            ):
                raise ToolPolicyError("tool policy entry has invalid fields")
            identity = item.get("participant_identity")
            name = item.get("tool_name")
            if not valid_participant_identity(identity) or not valid_tool_name(name):
                raise ToolPolicyError("tool policy identity is invalid")
            expiry: float | None = None
            if tier == 2:
                value = item.get("consent_expires_at")
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    raise ToolPolicyError("tier 2 consent expiry is invalid")
                expiry = float(value)
            key = (identity, name)
            if key in entries:
                raise ToolPolicyError("tool policy identity is duplicated")
            entries[key] = _PolicyEntry(tier=tier, consent_expires_at=expiry)
        return cls(entries, clock=clock)

    def decide(self, participant_identity: str, tool_name: str) -> PolicyDecision:
        entry = self._entries.get((participant_identity, tool_name))
        if entry is None:
            return PolicyDecision(False, 0, "not-allowlisted")
        if entry.tier == 1:
            return PolicyDecision(True, 1, "tier1-allowed")
        if entry.tier == 3:
            return PolicyDecision(False, 3, "tier3-denied")
        try:
            now = self._clock()
        except Exception:
            now = math.inf
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(now)
            or now < 0
        ):
            now = math.inf
        if now >= (entry.consent_expires_at or 0):
            return PolicyDecision(False, 2, "consent-expired")
        return PolicyDecision(True, 2, "consent-valid")


class ToolAuditLog:
    """A fixed-field in-memory ring that cannot retain tool data or errors."""

    def __init__(
        self,
        *,
        limit: int = MAX_AUDIT_RECORDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_AUDIT_RECORDS:
            raise ValueError("audit limit is invalid")
        self._records: deque[dict[str, object]] = deque(maxlen=limit)
        self._clock = clock
        self._sequence = 0

    def append(
        self,
        event: str,
        participant_identity: str,
        tool_name: str,
        tier: int,
        outcome: str,
    ) -> None:
        if event not in _AUDIT_EVENTS or outcome not in _AUDIT_OUTCOMES:
            raise ValueError("audit vocabulary is invalid")
        if not valid_participant_identity(participant_identity):
            participant_identity = ""
        if tool_name and not valid_tool_name(tool_name):
            tool_name = ""
        if isinstance(tier, bool) or tier not in {0, 1, 2, 3}:
            tier = 0
        try:
            recorded_at = self._clock()
        except Exception:
            recorded_at = 0.0
        if (
            isinstance(recorded_at, bool)
            or not isinstance(recorded_at, (int, float))
            or not math.isfinite(recorded_at)
        ):
            recorded_at = 0.0
        self._sequence += 1
        self._records.append(
            {
                "sequence": self._sequence,
                "recorded_at": float(recorded_at),
                "event": event,
                "participant_identity": participant_identity,
                "tool_name": tool_name,
                "tier": tier,
                "outcome": outcome,
            }
        )

    def snapshot(self) -> tuple[dict[str, object], ...]:
        return tuple(dict(record) for record in self._records)
