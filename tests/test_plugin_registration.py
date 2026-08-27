"""Hermes platform registration follows the current plugin contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hermes_livekit
from hermes_livekit.adapter import TOOLSET_NAME, check_livekit_requirements
from hermes_livekit.realtime_webrtc import check_realtime_requirements


_CREDENTIAL_VARS = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")


def _clear_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CREDENTIAL_VARS:
        monkeypatch.delenv(name, raising=False)


def test_dependency_probe_is_passive_and_credential_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_credentials(monkeypatch)

    assert check_livekit_requirements() is True


def test_config_validation_requires_all_connection_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_credentials(monkeypatch)
    complete = SimpleNamespace(
        extra={"url": "wss://example", "api_key": "key", "api_secret": "secret"}
    )
    incomplete = SimpleNamespace(extra={"url": "wss://example"})

    assert hermes_livekit._validate_config(complete) is True
    assert hermes_livekit._validate_config(incomplete) is False


def test_yaml_bridge_accepts_direct_and_nested_adapter_settings() -> None:
    result = hermes_livekit._apply_yaml_config(
        {},
        {
            "url": "wss://direct",
            "room": "direct-room",
            "extra": {"room": "nested-room", "agent_name": "Avery"},
        },
    )

    assert result == {
        "url": "wss://direct",
        "room": "nested-room",
        "agent_name": "Avery",
    }


def test_register_exposes_current_platform_callbacks() -> None:
    observed: dict[str, dict[str, object]] = {}

    class Context:
        def register_platform(self, **kwargs: object) -> None:
            observed[str(kwargs["name"])] = kwargs

        def register_hook(self, *_args: object, **_kwargs: object) -> None:
            return None

    hermes_livekit.register(Context())

    livekit = observed["livekit"]
    assert livekit["check_fn"] is check_livekit_requirements
    assert livekit["validate_config"] is hermes_livekit._validate_config
    assert livekit["is_connected"] is hermes_livekit._is_connected
    assert livekit["apply_yaml_config_fn"] is hermes_livekit._apply_yaml_config

    realtime = observed["realtime"]
    assert realtime["check_fn"] is check_realtime_requirements
    assert realtime["validate_config"] is hermes_livekit._validate_realtime_config
    assert realtime["is_connected"] is hermes_livekit._validate_realtime_config
    assert realtime["apply_yaml_config_fn"] is hermes_livekit._apply_realtime_yaml_config


def test_realtime_env_enablement_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HERMES_REALTIME_ENABLED", raising=False)
    monkeypatch.delenv("HERMES_REALTIME_API_KEY", raising=False)
    assert hermes_livekit._realtime_env_enablement() is None

    monkeypatch.setenv("HERMES_REALTIME_ENABLED", "true")
    assert hermes_livekit._realtime_env_enablement() is None

    monkeypatch.setenv("HERMES_REALTIME_API_KEY", "test-token")
    assert hermes_livekit._realtime_env_enablement() == {
        "host": "127.0.0.1",
        "port": 8091,
        "api_key": "test-token",
    }


def test_register_declares_late_bound_remote_toolset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from toolsets import TOOLSETS, validate_toolset

    monkeypatch.delitem(TOOLSETS, TOOLSET_NAME, raising=False)

    class Context:
        def register_platform(self, **_kwargs: object) -> None:
            return None

        def register_hook(self, *_args: object, **_kwargs: object) -> None:
            return None

    hermes_livekit.register(Context())

    assert validate_toolset(TOOLSET_NAME) is True
    assert TOOLSETS[TOOLSET_NAME]["tools"] == []


def test_register_does_not_add_obsolete_remote_tool_cancellation_hooks() -> None:
    hooks: list[tuple[str, object]] = []

    class Context:
        def register_platform(self, **_kwargs: object) -> None:
            return None

        def register_hook(self, name: str, callback: object) -> None:
            hooks.append((name, callback))

    hermes_livekit.register(Context())

    assert hooks == []
