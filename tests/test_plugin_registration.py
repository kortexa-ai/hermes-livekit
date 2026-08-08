"""Hermes platform registration follows the current plugin contract."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import hermes_livekit
from hermes_livekit.adapter import check_livekit_requirements


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
    observed: dict[str, object] = {}

    class Context:
        def register_platform(self, **kwargs: object) -> None:
            observed.update(kwargs)

        def register_hook(self, *_args: object, **_kwargs: object) -> None:
            return None

    hermes_livekit.register(Context())

    assert observed["check_fn"] is check_livekit_requirements
    assert observed["validate_config"] is hermes_livekit._validate_config
    assert observed["is_connected"] is hermes_livekit._is_connected
    assert observed["apply_yaml_config_fn"] is hermes_livekit._apply_yaml_config


def test_register_adds_loop_stop_hook_only_when_host_advertises_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_cli import plugins as hermes_plugins

    monkeypatch.setattr(
        hermes_plugins,
        "VALID_HOOKS",
        {*hermes_plugins.VALID_HOOKS, "agent_loop_stopped"},
    )
    hooks: list[tuple[str, object]] = []

    class Context:
        def register_platform(self, **_kwargs: object) -> None:
            return None

        def register_hook(self, name: str, callback: object) -> None:
            hooks.append((name, callback))

    hermes_livekit.register(Context())

    assert ("on_session_finalize", hermes_livekit._on_session_finalize_hook) in hooks
    assert ("agent_loop_stopped", hermes_livekit._on_agent_loop_stopped_hook) in hooks
