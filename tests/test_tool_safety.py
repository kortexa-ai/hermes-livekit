"""Remote-tool policy and audit behavior is deterministic without LiveKit."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hermes_livekit.adapter import LiveKitAdapter
from hermes_livekit.tool_safety import (
    MAX_AUDIT_RECORDS,
    MAX_POLICY_BYTES,
    ToolAuditLog,
    ToolPolicy,
    ToolPolicyError,
)
from tools.registry import registry


def policy_json(*tools: dict[str, object]) -> str:
    return json.dumps({"tools": list(tools)})


def entry(identity: str, name: str, tier: int, **extra: object) -> dict[str, object]:
    return {
        "participant_identity": identity,
        "tool_name": name,
        "tier": tier,
        **extra,
    }


def safety_adapter(policy: ToolPolicy) -> LiveKitAdapter:
    adapter = object.__new__(LiveKitAdapter)
    adapter.platform = SimpleNamespace(value="livekit")
    adapter._room_generation = 1
    adapter._tool_policy = policy
    adapter._tool_audit = ToolAuditLog(clock=lambda: 50.0)
    adapter._client_tools = {}
    adapter._tool_owners = {}
    adapter._tool_methods = {}
    adapter._publish_typed = AsyncMock()
    return adapter


def test_closed_tiers_allow_tier1_valid_consent_and_deny_tier3() -> None:
    now = [100.0]
    policy = ToolPolicy.parse(
        policy_json(
            entry("reader", "observe", 1),
            entry("actor", "notify", 2, consent_expires_at=110.0),
            entry("admin", "shell", 3),
        ),
        clock=lambda: now[0],
    )

    assert policy.decide("reader", "observe").reason == "tier1-allowed"
    assert policy.decide("actor", "notify").reason == "consent-valid"
    assert policy.decide("admin", "shell").reason == "tier3-denied"
    assert policy.decide("other", "observe").reason == "not-allowlisted"
    assert not policy.decide("reader", "notify").allowed

    now[0] = 110.0
    assert policy.decide("actor", "notify").reason == "consent-expired"


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        '{"tools":[],"tools":[]}',
        json.dumps({"tools": [], "unknown": True}),
        policy_json(entry("client", "tool", True)),
        policy_json(entry("client", "tool", 2)),
        policy_json(entry("client", "tool", 2, consent_expires_at=float("inf"))),
        policy_json(entry("client", "tool", 1), entry("client", "tool", 1)),
        policy_json(entry("client\u0085", "tool", 1)),
        "\ud800",
        "x" * (MAX_POLICY_BYTES + 1),
    ],
)
def test_malformed_policy_fails_closed_without_partial_entries(raw: str) -> None:
    with pytest.raises(ToolPolicyError):
        ToolPolicy.parse(raw)


def test_audit_ring_has_fixed_fields_bounds_and_detached_snapshots() -> None:
    audit = ToolAuditLog(limit=2, clock=lambda: 10.0)
    audit.append("registration", "client", "observe", 1, "accepted")
    audit.append("invocation", "client", "observe", 1, "success")
    audit.append("owner_disconnect", "client", "observe", 1, "removed")

    snapshot = audit.snapshot()
    assert [record["sequence"] for record in snapshot] == [2, 3]
    assert set(snapshot[0]) == {
        "sequence",
        "recorded_at",
        "event",
        "participant_identity",
        "tool_name",
        "tier",
        "outcome",
    }
    snapshot[0]["outcome"] = "changed"
    assert audit.snapshot()[0]["outcome"] == "success"
    assert MAX_AUDIT_RECORDS == 256


def test_malformed_environment_policy_logs_no_policy_text_and_denies_all(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = object.__new__(LiveKitAdapter)
    adapter.platform = SimpleNamespace(value="livekit")
    secret_shaped = "credential-shaped-policy-text"
    monkeypatch.setenv("HERMES_LIVEKIT_REMOTE_TOOL_POLICY", secret_shaped)

    policy = adapter._resolve_tool_policy()

    assert not policy.decide("client", "observe").allowed
    assert secret_shaped not in caplog.text


@pytest.mark.asyncio
async def test_missing_policy_denies_registration_before_registry_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = safety_adapter(ToolPolicy())
    adapter._room = SimpleNamespace(remote_participants={"client": object()})
    register = AsyncMock()
    monkeypatch.setattr(registry, "register", register)

    await adapter._register_client_tool(
        {
            "name": "observe",
            "description": "Read state.",
            "input_schema": {"type": "object"},
        },
        "client",
    )

    register.assert_not_called()
    assert adapter._publish_typed.await_args.args[0]["reason"] == "policy-denied"
    assert [record["event"] for record in adapter._tool_audit_snapshot()] == [
        "policy",
        "registration",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "name", "schema"),
    [
        ("invalid\u0085identity", "observe", {"type": "object"}),
        ("client", "invalid name with secret-shaped-tail", {"type": "object"}),
        ("client", "observe", {"type": "array", "private": "schema-secret"}),
    ],
)
async def test_invalid_registration_has_sanitized_denied_audit(
    identity: str,
    name: str,
    schema: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = safety_adapter(ToolPolicy())
    adapter._room = SimpleNamespace(remote_participants={identity: object()})
    register = AsyncMock()
    monkeypatch.setattr(registry, "register", register)

    await adapter._register_client_tool(
        {
            "name": name,
            "description": "description-secret",
            "input_schema": schema,
        },
        identity,
    )

    register.assert_not_called()
    records = adapter._tool_audit_snapshot()
    assert records[-1]["event"] == "registration"
    assert records[-1]["outcome"] == "denied"
    serialized = json.dumps(records)
    assert "secret-shaped-tail" not in serialized
    assert "schema-secret" not in serialized
    assert "description-secret" not in serialized
    assert "\u0085" not in serialized


@pytest.mark.asyncio
async def test_registration_failure_does_not_expose_exception_text(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    adapter = safety_adapter(
        ToolPolicy.parse(policy_json(entry("client", "observe", 1)))
    )
    adapter._room = SimpleNamespace(remote_participants={"client": object()})
    monkeypatch.setattr(registry, "get_entry", lambda _name: None)

    def fail_register(**_kwargs: object) -> None:
        raise RuntimeError("credential-shaped-registration-error")

    monkeypatch.setattr(registry, "register", fail_register)
    await adapter._register_client_tool(
        {"name": "observe", "input_schema": {"type": "object"}}, "client"
    )

    assert adapter._publish_typed.await_args.args[0] == {
        "type": "agent:tool-registered",
        "name": "observe",
        "success": False,
        "reason": "register-failed",
    }
    assert "credential-shaped-registration-error" not in caplog.text
    assert "credential-shaped-registration-error" not in json.dumps(
        adapter._tool_audit_snapshot()
    )


@pytest.mark.asyncio
async def test_expired_consent_and_identity_mismatch_deny_invocation_without_rpc() -> None:
    now = [100.0]
    policy = ToolPolicy.parse(
        policy_json(entry("client", "notify", 2, consent_expires_at=101.0)),
        clock=lambda: now[0],
    )
    adapter = safety_adapter(policy)
    participant = SimpleNamespace(perform_rpc=AsyncMock(return_value='{"ok":true}'))
    adapter._room = SimpleNamespace(
        remote_participants={"client": object(), "other": object()},
        local_participant=participant,
    )
    adapter._tool_call_timeout = 1.0

    with pytest.raises(RuntimeError, match="denied by policy"):
        await adapter._build_tool_handler("other", "notify")({"secret": "do-not-log"})
    now[0] = 101.0
    with pytest.raises(RuntimeError, match="denied by policy"):
        await adapter._build_tool_handler("client", "notify")({"secret": "do-not-log"})

    participant.perform_rpc.assert_not_called()
    serialized = json.dumps(adapter._tool_audit_snapshot())
    assert "do-not-log" not in serialized
    assert "not-allowlisted" in serialized
    assert "consent-expired" in serialized


@pytest.mark.asyncio
async def test_rpc_error_and_cancellation_audit_no_arguments_or_exception_text() -> None:
    policy = ToolPolicy.parse(policy_json(entry("client", "observe", 1)))
    adapter = safety_adapter(policy)
    started = asyncio.Event()

    async def failing_rpc(**_kwargs: object) -> str:
        raise RuntimeError("credential-shaped-private-error")

    participant = SimpleNamespace(perform_rpc=failing_rpc)
    adapter._room = SimpleNamespace(
        remote_participants={"client": object()}, local_participant=participant
    )
    adapter._tool_call_timeout = 1.0
    with pytest.raises(RuntimeError, match="credential-shaped-private-error"):
        await adapter._build_tool_handler("client", "observe")(
            {"private": "argument-body"}
        )

    async def blocking_rpc(**_kwargs: object) -> str:
        started.set()
        await asyncio.Future()
        raise AssertionError("unreachable")

    participant.perform_rpc = blocking_rpc
    task = asyncio.create_task(
        adapter._build_tool_handler("client", "observe")({"private": "second-body"})
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    serialized = json.dumps(adapter._tool_audit_snapshot())
    assert "credential-shaped-private-error" not in serialized
    assert "argument-body" not in serialized
    assert "second-body" not in serialized
    assert '"event": "invocation"' in serialized
    assert '"event": "cancellation"' in serialized


def test_disconnect_audit_is_bounded_to_owned_tool_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ToolPolicy.parse(policy_json(entry("client", "observe", 1)))
    adapter = safety_adapter(policy)
    adapter._client_tools = {"client": {"scoped"}}
    adapter._tool_owners = {"scoped": "client"}
    adapter._tool_methods = {"scoped": "observe"}
    monkeypatch.setattr(registry, "deregister", lambda _name: None)

    adapter._cleanup_client_tools("client")

    records = adapter._tool_audit_snapshot()
    assert records[-1]["event"] == "owner_disconnect"
    assert records[-1]["participant_identity"] == "client"
    assert records[-1]["tool_name"] == "observe"
    assert adapter._client_tools == {}


@pytest.mark.asyncio
async def test_policy_and_audit_persist_across_room_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = ToolPolicy.parse(policy_json(entry("client", "observe", 1)))
    adapter = safety_adapter(policy)
    adapter._room = SimpleNamespace(remote_participants={"client": object()})
    registered: dict[str, dict[str, object]] = {}
    monkeypatch.setattr(registry, "get_entry", lambda name: registered.get(name))
    monkeypatch.setattr(
        registry,
        "register",
        lambda **kwargs: registered.__setitem__(str(kwargs["name"]), kwargs),
    )
    monkeypatch.setattr(registry, "deregister", lambda name: registered.pop(name))
    message = {"name": "observe", "input_schema": {"type": "object"}}

    await adapter._register_client_tool(message, "client")
    adapter._cleanup_all_client_tools()
    adapter._room = SimpleNamespace(remote_participants={"client": object()})
    adapter._room_generation = 2
    await adapter._register_client_tool(message, "client")

    records = adapter._tool_audit_snapshot()
    assert sum(record["event"] == "registration" for record in records) == 2
    assert any(record["event"] == "owner_disconnect" for record in records)
    assert all(record["participant_identity"] == "client" for record in records)
