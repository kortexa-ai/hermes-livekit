"""Keep ``LiveKitAdapter`` callable the way the gateway calls the base class.

Twice now an override has drifted from ``BasePlatformAdapter`` and taken out a
whole path at runtime, each time with a ``TypeError`` raised *after* real work
had already been done:

- ``play_tts()`` did not accept ``caption``, which the auto-TTS path passes on
  every platform — so every voice reply died after its TTS audio had been
  generated, and the room got an error message instead of the answer.
- ``connect()`` did not accept ``is_reconnect``, which the reconnection watcher
  passes — so the platform stayed down permanently after any disconnect.

Both are invisible until the exact path runs against a live gateway. These
tests make them fail here instead.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import BasePlatformAdapter
from hermes_livekit.adapter import LiveKitAdapter

_VARIADIC = (inspect.Parameter.VAR_KEYWORD, inspect.Parameter.VAR_POSITIONAL)


def _overrides():
    """Yield (name, base_function, override_function) for each overridden method."""
    for name, attr in vars(LiveKitAdapter).items():
        if name.startswith("__") or not inspect.isfunction(attr):
            continue
        base_attr = inspect.getattr_static(BasePlatformAdapter, name, None)
        if inspect.isfunction(base_attr):
            yield name, base_attr, attr


def _takes_var_keyword(sig: inspect.Signature) -> bool:
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())


def test_adapter_actually_overrides_something():
    """Guard the guard: a rename upstream must not quietly empty these tests."""
    names = {name for name, _, _ in _overrides()}
    assert "play_tts" in names, f"play_tts is no longer an override; found {sorted(names)}"
    assert len(names) > 3, f"suspiciously few overrides detected: {sorted(names)}"


def test_overrides_accept_open_ended_base_signatures():
    """An override of a ``**kwargs`` base method must take ``**kwargs`` too.

    The base declares ``**kwargs`` precisely so the gateway can pass new
    keywords over time. An override that pins a fixed signature breaks the
    first time core adds one — this is exactly how ``caption`` broke TTS.
    """
    problems = [
        f"{name}(): base takes **kwargs, override does not — "
        f"a new keyword from core will TypeError here"
        for name, base_fn, override_fn in _overrides()
        if _takes_var_keyword(inspect.signature(base_fn))
        and not _takes_var_keyword(inspect.signature(override_fn))
    ]
    assert not problems, "adapter overrides drifted from the base contract:\n  " + "\n  ".join(problems)


def test_overrides_accept_every_base_parameter():
    """Whatever the base names as a parameter, the override must accept."""
    problems = []
    for name, base_fn, override_fn in _overrides():
        override_sig = inspect.signature(override_fn)
        if _takes_var_keyword(override_sig):
            continue
        missing = [
            param
            for param, spec in inspect.signature(base_fn).parameters.items()
            if spec.kind not in _VARIADIC and param not in override_sig.parameters
        ]
        if missing:
            problems.append(f"{name}(): base parameter(s) {missing} missing from override")

    assert not problems, "adapter overrides drifted from the base contract:\n  " + "\n  ".join(problems)


@pytest.mark.asyncio
async def test_livekit_send_voice_uses_native_audio_track():
    adapter = object.__new__(LiveKitAdapter)
    adapter.play_tts = AsyncMock(return_value="delivered")

    result = await adapter.send_voice(
        chat_id="room",
        audio_path="reply.wav",
        caption="caption",
        reply_to="message",
        metadata={"turn": 1},
    )

    assert result == "delivered"
    adapter.play_tts.assert_awaited_once_with(
        chat_id="room",
        audio_path="reply.wav",
        caption="caption",
        reply_to="message",
        metadata={"turn": 1},
    )


@pytest.mark.asyncio
async def test_livekit_send_completes_transcript_response():
    adapter = object.__new__(LiveKitAdapter)
    adapter._room = object()
    adapter._realtime_protocol = AsyncMock()
    adapter._tts_completed = True

    result = await adapter.send(chat_id="room", content="hello")

    assert result.success is True
    adapter._realtime_protocol.assistant_transcript.assert_awaited_once_with("hello")
    adapter._realtime_protocol.output_stopped.assert_awaited_once_with()
    assert adapter._tts_completed is False
