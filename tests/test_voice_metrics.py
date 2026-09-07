"""Local observer behavior, including real Hermes config/registration contracts."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_livekit import voice_metrics as metrics


def records(caplog):
    return [json.loads(r.message.removeprefix("voice_timing ")) for r in caplog.records
            if r.name == metrics.logger.name]


@pytest.fixture(autouse=True)
def log_metrics(caplog):
    caplog.set_level("INFO", logger=metrics.logger.name)


def text(observer, **kwargs):
    observer.on_text(**{"session_id": "session", "turn_id": "turn", "iteration": 1,
                        "surface": "realtime", "delta": "Hello", **kwargs})


def api(observer, **kwargs):
    observer.on_api(**{"session_id": "session", "turn_id": "turn", "api_call_count": 1,
                       "platform": "realtime", "started_at": 100.0, "api_duration": 5.0, **kwargs})


def test_first_visible_text_only_and_no_payload_retention(caplog, monkeypatch):
    observer = metrics.VoiceMetrics()
    monkeypatch.setattr(metrics.time, "time", lambda: 102.0)
    text(observer, delta=" ")
    text(observer, kind="reasoning", delta="private reasoning")
    text(observer, surface="telegram")
    text(observer, delta="secret reply", request={"api_key": "secret key"})
    text(observer, delta="more private text")
    assert records(caplog) == [{"event": "first_text_observed", "session_id": "session",
                              "turn_id": "turn", "iteration": 1, "observed_at": 102.0}]
    assert "secret" not in repr(observer.__dict__) + caplog.text


def test_keys_are_bounded_and_separate_turns_calls_and_sessions(caplog):
    observer = metrics.VoiceMetrics()
    for extra in ({}, {"iteration": 2}, {"turn_id": "next"}, {"session_id": "another"}):
        text(observer, **extra)
    assert len(records(caplog)) == 4
    for i in range(metrics._SEEN_LIMIT + 5):
        text(observer, session_id=str(i))
    assert len(observer._seen) == metrics._SEEN_LIMIT
    assert ("session", "turn", 1) not in observer._seen
    text(observer, session_id="", turn_id="")
    assert len(observer._seen) == metrics._SEEN_LIMIT


def test_thread_safe_deduplication(caplog):
    observer = metrics.VoiceMetrics()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: text(observer), range(100)))
    assert len(records(caplog)) == 1


@pytest.mark.parametrize("first, expected", [(None, None), (102, 2), (99, None),
                                          (106, None), (float("nan"), None), (True, None)])
def test_api_source_timing_and_usage_are_allowlisted(caplog, first, expected):
    observer = metrics.VoiceMetrics()
    api(observer, first_chunk_at=first, assistant_tool_call_count=1,
        usage={"prompt_tokens": 15000, "cache_read_tokens": 14900, "output_tokens": 12,
               "reasoning_tokens": 8, "raw_usage": "secret"},
        assistant_message="secret reply", request={"api_key": "secret"})
    row = records(caplog)[0]
    assert row["api_s"] == 5 and row["first_chunk_s"] == expected
    assert row["tool_calls"] == 1
    assert row["usage"] == {"prompt_tokens": 15000, "cache_read_tokens": 14900,
                            "output_tokens": 12, "reasoning_tokens": 8}
    assert "secret" not in caplog.text


def test_out_of_order_callbacks_do_not_invent_order_or_duration(caplog):
    observer = metrics.VoiceMetrics()
    api(observer)  # API hook can overtake the independent stream observer worker.
    text(observer)
    assert [r["event"] for r in records(caplog)] == ["api_completed", "first_text_observed"]
    assert records(caplog)[0]["first_chunk_s"] is None


def test_malformed_optional_fields_cannot_leak_or_break_json(caplog):
    observer = metrics.VoiceMetrics()
    api(observer, started_at=float("inf"), api_duration=-1, usage="secret",
        model="line\n" * 200, provider=object(), assistant_tool_call_count=False)
    row = records(caplog)[0]
    assert row["started_at"] is None and row["api_s"] is None
    assert row["tool_calls"] is None and row["provider"] == ""
    assert len(row["model"]) == 128
    api(observer, platform="cli")
    assert len(records(caplog)) == 1


@pytest.mark.parametrize("setting", [None, False, True, "true", 1])
def test_real_hermes_profile_config_and_registration(tmp_path, monkeypatch, caplog, setting):
    from hermes_cli.plugins import PluginContext, PluginManager
    from hermes_cli.plugins_manifest import PluginManifest

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(json.dumps({"plugins": {"entries": {
        "livekit": {"settings": {"voice_latency_metrics": setting}},
    }}}))
    manager = PluginManager()
    ctx = PluginContext(PluginManifest(name="livekit"), manager)
    metrics.register_voice_metrics(ctx)
    callbacks = manager.iter_hook_callbacks("on_stream_delta")
    assert bool(callbacks) is (setting is True)
    assert bool(manager.iter_hook_callbacks("post_api_request")) is (setting is True)
    if setting is True:
        callbacks[0](session_id="session", turn_id="turn", iteration=1,
                     surface="livekit", delta="private reply")
        assert records(caplog)[0]["event"] == "first_text_observed"


def test_profile_observers_do_not_share_seen_state(caplog):
    text(metrics.VoiceMetrics())
    text(metrics.VoiceMetrics())
    assert len(records(caplog)) == 2


def test_older_hermes_context_does_not_enable_metrics():
    metrics.register_voice_metrics(object())


def test_installed_entrypoint_discovers_and_receives_real_async_hook(tmp_path):
    (tmp_path / "config.yaml").write_text(json.dumps({"plugins": {
        "enabled": ["livekit"], "entries": {
            "livekit": {"settings": {"voice_latency_metrics": True}},
        },
    }}))
    program = """
import json, logging, threading
from hermes_cli.plugins import get_plugin_manager
from agent.plugin_stream_hooks import enqueue_plugin_stream_hook, shutdown_plugin_stream_hook_dispatcher
manager = get_plugin_manager()
manager.discover_and_load()
assert manager.iter_hook_callbacks('on_stream_delta')
assert manager.iter_hook_callbacks('post_api_request')
seen = threading.Event()
class Capture(logging.Handler):
    def emit(self, record):
        if record.name.endswith('.voice_metrics'):
            row = json.loads(record.getMessage().removeprefix('voice_timing '))
            assert row['event'] == 'first_text_observed'
            assert row['session_id'] == 'test-session'
            seen.set()
log = logging.getLogger('gateway.platforms.livekit.voice_metrics')
log.setLevel(logging.INFO)
log.addHandler(Capture())
assert enqueue_plugin_stream_hook('on_stream_delta', session_id='test-session',
    turn_id='test-turn', iteration=1, surface='realtime', delta='private reply', kind='text')
assert seen.wait(5), 'real async stream hook was not delivered'
shutdown_plugin_stream_hook_dispatcher()
print('voice observer discovery and async dispatch passed')
"""
    env = {**os.environ, "HERMES_HOME": str(tmp_path)}
    env.pop("HERMES_SAFE_MODE", None)
    result = subprocess.run([sys.executable, "-c", program], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "voice observer discovery and async dispatch passed" in result.stdout
