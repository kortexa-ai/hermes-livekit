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

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

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
    from hermes_livekit.realtime_protocol import RealtimeProtocol

    events = []

    async def publish(event, recipient):
        events.append(event)
        return True

    adapter = object.__new__(LiveKitAdapter)
    adapter._room = object()
    adapter._realtime_protocol = RealtimeProtocol(
        session_id="standalone", model="test", voice="test", publish=publish,
    )

    result = await adapter.send(chat_id="room", content="hello")

    assert result.success is True
    done = [e["response"] for e in events if e["type"] == "response.done"]
    assert len(done) == 1
    assert done[0]["status"] == "completed"
    assert done[0]["output"][0]["content"][0]["transcript"] == "hello"
    assert adapter._realtime_protocol.active_response_id is None


@pytest.mark.asyncio
async def test_livekit_internal_wake_waits_for_silence() -> None:
    adapter = object.__new__(LiveKitAdapter)
    adapter._room = object()
    adapter._speaking_participants = {"client"}
    adapter._deferred_internal_wakes = set()
    event = SimpleNamespace(internal=True)

    with patch.object(BasePlatformAdapter, "handle_message", new=AsyncMock()) as dispatch:
        await adapter.handle_message(event)
        await asyncio.sleep(0)
        dispatch.assert_not_awaited()

        adapter._speaking_participants.clear()
        await asyncio.gather(*adapter._deferred_internal_wakes)
        dispatch.assert_awaited_once_with(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("resume_before_poll", [False, True])
async def test_livekit_prefetch_waits_for_endpoint_and_checks_unpolled_audio(monkeypatch, resume_before_poll):
    import hermes_livekit.adapter as adapter_module
    from hermes_livekit.vad import AdaptiveRmsGate

    adapter = object.__new__(LiveKitAdapter)
    adapter.platform = SimpleNamespace(value="livekit")
    speech = b"\x00\x10" * 24000
    quiet = b"\x10\x00" * 19200
    adapter._audio_buffers = {"client": bytearray(speech + quiet)}
    # The speech marker represents already classified audio; only the quiet
    # tail is new at this poll. Replaying speech would reset the endpoint.
    adapter._audio_processed = {"client": (len(speech), True)}
    adapter._last_audio_time = {"client": 0.0}
    adapter._speaking_participants = {"client"}
    adapter._audio_gates = {"client": AdaptiveRmsGate(noise_rms=150)}
    adapter._early_asr = {}
    adapter._asr_prefetch_silence = 0.35
    adapter._silence_duration = 0.7
    adapter._running = True
    adapter._paused = False
    adapter._process_voice_input = AsyncMock()
    worker = Mock(return_value="candidate")
    monkeypatch.setattr(adapter_module, "transcribe_pcm", worker)
    clock = [0.0]
    candidate = None

    async def poll_sleep(delay):
        nonlocal candidate
        if not clock[0]:
            clock[0] = 0.4
            return
        candidate = adapter._early_asr["client"]._task
        assert await candidate == "candidate"
        adapter._process_voice_input.assert_not_awaited()
        if resume_before_poll:
            # The receive loop can append speech immediately before explicit
            # end-of-turn, without the polling VAD updating the speech marker.
            adapter._audio_buffers["client"].extend(speech)
            assert adapter._flush_utterance("client", len(adapter._audio_buffers["client"]))
        else:
            adapter._audio_buffers["client"].extend(b"\x10\x00" * 19200)
        clock[0] = 0.8
        adapter._running = False  # Finish this tick, including the real endpoint.

    monkeypatch.setattr(adapter_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with monkeypatch.context() as polling:
        polling.setattr(adapter_module.asyncio, "sleep", poll_sleep)
        await adapter._check_silence_loop()
    await asyncio.sleep(0)
    worker.assert_called_once_with(speech + quiet, 48000, 1)
    adapter._process_voice_input.assert_awaited_once()
    dispatched = adapter._process_voice_input.call_args.kwargs["prefetched"]
    assert dispatched is (None if resume_before_poll else candidate)
