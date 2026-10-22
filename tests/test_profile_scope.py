"""A shared gateway must build each transport from its owning profile."""

from types import SimpleNamespace

import pytest

import hermes_livekit
from agent import secret_scope
from gateway.config import PlatformConfig
from gateway.platform_registry import PlatformEntry, platform_registry
from hermes_livekit.adapter import LiveKitAdapter
from hermes_livekit.realtime_webrtc import RealtimeWebRTCAdapter


def profile_env(name, port):
    return {
        "LIVEKIT_URL": f"wss://{name}.example",
        "LIVEKIT_API_KEY": f"{name}-key",
        "LIVEKIT_API_SECRET": f"{name}-secret",
        "LIVEKIT_ROOM": f"hermes-{name}",
        "LIVEKIT_AGENT_NAME": name,
        "HERMES_REALTIME_ENABLED": "true",
        "HERMES_REALTIME_API_KEY": f"{name}-realtime",
        "HERMES_REALTIME_PORT": str(port),
        "HERMES_REALTIME_PROXY_SIGNING_KEY": f"{name}-proxy",
    }


@pytest.fixture(params=[True, False], ids=["configured", "missing"])
def secondary_scope(request, monkeypatch):
    for key, value in profile_env("avery", 8091).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LIVEKIT_HOME_CHANNEL", raising=False)
    scoped = profile_env("mira", 8092) if request.param else {}
    mode = secret_scope.set_multiplex_context(True)
    token = secret_scope.set_secret_scope(scoped)
    try:
        yield scoped
    finally:
        secret_scope.reset_secret_scope(token)
        secret_scope.reset_multiplex_context(mode)


def test_platform_discovery_uses_only_the_owning_profile(secondary_scope):
    import os

    livekit = hermes_livekit._env_enablement()
    realtime = hermes_livekit._realtime_env_enablement()
    if secondary_scope:
        assert livekit["room"] == "hermes-mira"
        assert livekit["api_key"] == "mira-key"
        assert livekit["home_channel"]["chat_id"] == "hermes-mira"
        assert realtime["port"] == 8092
        assert realtime["api_key"] == "mira-realtime"
    else:
        assert livekit is None
        assert realtime is None
    assert "LIVEKIT_HOME_CHANNEL" not in os.environ
    empty = SimpleNamespace(extra={})
    assert hermes_livekit._validate_config(empty) is bool(secondary_scope)
    assert hermes_livekit._validate_realtime_config(empty) is bool(secondary_scope)


def test_adapter_fallbacks_do_not_borrow_launch_profile_values(secondary_scope):
    entries = [("livekit", LiveKitAdapter), ("realtime", RealtimeWebRTCAdapter)]
    for name, factory in entries:
        platform_registry.register(PlatformEntry(
            name=name, label=name, adapter_factory=factory, check_fn=lambda: True,
        ), scope=platform_registry.current_scope_key())
    try:
        livekit = LiveKitAdapter(PlatformConfig(enabled=True, extra={"vad_backend": "rms"}))
        realtime = RealtimeWebRTCAdapter(PlatformConfig(enabled=True, extra={"vad_backend": "rms"}))
        assert livekit._api_key == secondary_scope.get("LIVEKIT_API_KEY", "")
        assert livekit._api_secret == secondary_scope.get("LIVEKIT_API_SECRET", "")
        assert livekit._room_name == secondary_scope.get("LIVEKIT_ROOM", "hermes")
        assert realtime._api_key == secondary_scope.get("HERMES_REALTIME_API_KEY", "")
        assert realtime._proxy_signing_key == secondary_scope.get("HERMES_REALTIME_PROXY_SIGNING_KEY", "")
        assert realtime._port == int(secondary_scope.get("HERMES_REALTIME_PORT", "8091"))
    finally:
        for name, _factory in entries:
            platform_registry.unregister(name)
