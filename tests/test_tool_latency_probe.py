"""Tool-canary correlation, without credentials, model calls or playback."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def probe_module(monkeypatch):
    directory = Path(__file__).parents[1] / "tools"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("tool_latency_probe", directory / "tool_latency_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sealed(name, call_id, arguments=None):
    return {"type": "response.done", "response": {"id": "response-" + call_id,
        "status": "completed", "output": [{"type": "function_call", "name": name,
        "call_id": call_id, "arguments": json.dumps(arguments or {})}]}}


def answer(text, status="completed"):
    return {"type": "response.done", "response": {"id": "spoken", "status": status,
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_audio", "transcript": text}]}]}}


def settled_turn(module, case="success"):
    turn = module.ToolTurn(case, dict(zip(module.NAMES, (713, 829))))
    turn.started = 1.0
    for index, name in enumerate(module.NAMES):
        call_id = f"fixture-{index}"
        arguments = {"alpha_value": turn.values[module.NAMES[0]]} if case == "dependent" and index == 1 else {}
        assert turn.accept(sealed(name, call_id, arguments), 2 + index) == [call_id]
        if case == "timeout" and index == 1:
            assert turn.accept({"type": "error", "error": {"code": "tool_timeout"}}, 4) == [call_id]
        events = turn.result_events(call_id, 5 + index)
        assert events[0]["item"]["call_id"] == call_id
        assert json.loads(events[0]["item"]["output"]) == (
            {"error": "fixture_unavailable"} if case == "mixed" and index == 1
            else {"value": turn.values[name]})
        if case == "timeout" and index == 1:
            assert len(events) == 1  # A stale result must not request another response.
            turn.accept({"type": "error", "error": {
                "code": "unknown_tool_call", "event_id": events[0]["event_id"]}}, 7)
        else:
            assert events[1] == {"type": "response.create"}
            turn.accept({"type": "conversation.item.done", "item": events[0]["item"]}, 7)
    return turn


@pytest.mark.parametrize("case", ["success", "parallel", "dependent", "mixed", "timeout"])
def test_completion_requires_both_results_and_matching_spoken_answer(probe_module, case):
    turn = probe_module.ToolTurn(case, dict(zip(probe_module.NAMES, (713, 829))))
    turn.accept(answer("Welcome."), 0.5)
    turn.started = 1
    turn.accept(answer("Welcome."), 1.5)
    assert not turn.done.is_set()
    turn = settled_turn(probe_module, case)
    assert not turn.done.is_set()  # Two function response seals are not an answer.
    text = "Alpha 713. Beta 829." if case in {"success", "parallel", "dependent"} else "Alpha 713. Beta unavailable."
    turn.accept({"type": "response.output_audio_transcript.delta", "response_id": "spoken", "delta": "Alpha "}, 7.5)
    turn.accept({"type": "output_audio_buffer.started", "response_id": "spoken"}, 8)
    turn.accept_audio(8.1)
    turn.accept(answer(text), 9)
    result = turn.result()
    assert turn.done.is_set()
    assert result["answer"] == text
    assert result["first_caption_s"] == 6.5
    assert result["first_non_silent_rtp_s"] > result["audio_start_s"]
    assert {c["name"] for c in result["tools"]} == set(probe_module.NAMES)
    assert sum(c["acknowledged"] for c in result["tools"]) == (1 if case == "timeout" else 2)


@pytest.mark.parametrize("missing", ["call", "ack", "rtp", "answer", "status", "late-rejection"])
def test_incomplete_evidence_cannot_report_success(probe_module, missing):
    case = "timeout" if missing == "late-rejection" else "success"
    turn = settled_turn(probe_module, case)
    turn.accept({"type": "output_audio_buffer.started", "response_id": "spoken"}, 8)
    if missing != "rtp":
        turn.accept_audio(8.1)
    text = "Alpha 713. Beta 829." if case == "success" else "Alpha 713. Beta unavailable."
    turn.accept(answer("Unrelated answer." if missing == "answer" else text,
                       "failed" if missing == "status" else "completed"), 9)
    if missing == "call":
        turn.calls.pop("fixture-1")
    elif missing == "ack":
        turn.calls["fixture-0"]["acked_at"] = None
    elif missing == "late-rejection":
        turn.errors.remove("unknown_tool_call")
    with pytest.raises(RuntimeError):
        turn.result()


@pytest.mark.parametrize("invalid", ["duplicate-call", "duplicate-name", "wrong-name", "arguments",
                                    "unsent-ack", "unknown-error", "unrelated-late-error"])
def test_unexpected_calls_or_uncorrelated_results_are_rejected(probe_module, invalid):
    turn = probe_module.ToolTurn("timeout", dict(zip(probe_module.NAMES, (713, 829))))
    turn.started = 1
    event = sealed(probe_module.NAMES[0], "fixture-0")
    turn.accept(event, 2)
    if invalid == "duplicate-name":
        event = sealed(probe_module.NAMES[0], "fixture-1")
    elif invalid == "wrong-name":
        event = sealed("not_a_fixture", "fixture-1")
    elif invalid == "arguments":
        event = sealed(probe_module.NAMES[1], "fixture-1")
        event["response"]["output"][0]["arguments"] = '{"unrequested":true}'
    elif invalid == "unsent-ack":
        event = {"type": "conversation.item.done", "item": {
            "type": "function_call_output", "call_id": "fixture-0"}}
    elif invalid == "unknown-error":
        event = {"type": "error", "error": {"code": "tool_timeout"}}
    elif invalid == "unrelated-late-error":
        turn.errors.append("tool_timeout")
        event = {"type": "error", "error": {"code": "unknown_tool_call", "event_id": "other"}}
    with pytest.raises(RuntimeError):
        turn.accept(event, 3)


def cancelled_turn(module):
    turn = module.CancelledToolTurn("cancel", dict(zip(module.NAMES, (713, 829))))
    turn.started = 1
    turn.accept(sealed(module.NAMES[0], "alpha"), 2)
    first = turn.result_events("alpha", 2.4)
    turn.accept({"type": "conversation.item.done", "item": first[0]["item"]}, 2.5)
    turn.accept(sealed(module.NAMES[1], "beta"), 3)
    assert turn.result_events("beta", 3.1) == [{"type": "response.cancel", "event_id": "fixture-cancel"}]
    assert turn.accept({"type": "response.done", "response": {"id": "cancelled", "status": "cancelled", "output": []}}, 3.2) == ["beta"]
    late = turn.result_events("beta", 3.3)
    assert len(late) == 1 and late[0]["item"]["type"] == "function_call_output"
    assert not turn.done.is_set()
    turn.accept({"type": "error", "error": {"code": "unknown_tool_call", "event_id": late[0]["event_id"]}}, 3.4)
    return turn


def test_cancel_requires_terminal_response_and_correlated_stale_rejection(probe_module):
    turn = cancelled_turn(probe_module)
    assert turn.done.is_set()
    assert turn.result() == {"case": "cancel", "cancel_s": 0.1,
                             "errors": ["unknown_tool_call"], "audio_responses": 0}


@pytest.mark.parametrize("failure", ["audio", "alpha-ack", "beta-ack", "stale-rejection", "status"])
def test_cancellation_cannot_report_incomplete_or_spurious_delivery(probe_module, failure):
    turn = cancelled_turn(probe_module)
    if failure == "audio":
        turn.accept({"type": "output_audio_buffer.started", "response_id": "stale"}, 4)
    elif failure == "alpha-ack":
        turn.calls["alpha"]["acked_at"] = None
    elif failure == "beta-ack":
        turn.calls["beta"]["acked_at"] = 4
    elif failure == "stale-rejection":
        turn.errors.clear()
    else:
        turn.final["status"] = "completed"
    with pytest.raises(RuntimeError):
        turn.result()


@pytest.mark.parametrize("arguments", [{}, {"alpha_value": 828}, {"alpha_value": "713"},
    {"alpha_value": 713.0}, {"alpha_value": True}, {"alpha_value": 713, "extra": 1}])
def test_dependent_tool_must_use_exact_returned_integer(probe_module, arguments):
    turn = probe_module.ToolTurn("dependent", dict(zip(probe_module.NAMES, (713, 829))))
    turn.started = 1
    turn.accept(sealed(probe_module.NAMES[0], "alpha"), 2)
    result = turn.result_events("alpha", 2.4)
    turn.accept({"type": "conversation.item.done", "item": result[0]["item"]}, 2.5)
    with pytest.raises(RuntimeError):
        turn.accept(sealed(probe_module.NAMES[1], "beta", arguments), 3)


@pytest.mark.parametrize("alpha_state", ["absent", "called", "sent"])
def test_dependent_tool_cannot_skip_result_dependency(probe_module, alpha_state):
    turn = probe_module.ToolTurn("dependent", dict(zip(probe_module.NAMES, (713, 829))))
    turn.started = 1
    if alpha_state != "absent":
        turn.accept(sealed(probe_module.NAMES[0], "alpha"), 2)
    if alpha_state == "sent":
        turn.result_events("alpha", 2.4)
    with pytest.raises(RuntimeError, match="preceded"):
        turn.accept(sealed(probe_module.NAMES[1], "beta", {"alpha_value": 713}), 3)


def test_dependent_schema_requires_unknown_alpha_result(probe_module):
    session = probe_module.tool_session("dependent")
    assert session["tools"][0]["parameters"]["properties"] == {}
    assert session["tools"][1]["parameters"] == {"type": "object", "additionalProperties": False,
        "properties": {"alpha_value": {"type": "integer"}}, "required": ["alpha_value"]}
    assert "Call alpha first" in session["instructions"]
    assert "in parallel" in probe_module.tool_session("parallel")["instructions"]


@pytest.mark.parametrize("input_mode,fixture", [
    ("text", b"\x00\x10"), ("voice", b""), ("voice", b"a"),
    ("voice", "not pcm"), ("voice", bytes(48000 * 2 * 30 + 2)), ("other", None),
])
def test_bad_input_rejected_before_connection(probe_module, input_mode, fixture):
    with pytest.raises(ValueError):
        probe_module.validate_input(input_mode, fixture)


def test_defaults_preserve_typed_profile_and_voice_is_explicit(probe_module):
    args = probe_module.parse_args([])
    assert args.input == "text" and args.model == "profile"
    args = probe_module.parse_args(["--input", "voice", "--model", "gpt-5.6-terra", "--case", "dependent"])
    assert args.input == "voice" and args.case == "dependent" and args.model == "gpt-5.6-terra"
    probe_module.validate_input("text", None)
    probe_module.validate_input("voice", None)
    probe_module.validate_input("voice", b"\x00\x10")


@pytest.mark.parametrize("invalid", [None, "no-speech", "incomplete", "split", "early", "missing"])
def test_voice_timings_require_one_completed_utterance(probe_module, invalid):
    turn = settled_turn(probe_module, "dependent")
    endpoint = {"type": "input_audio_buffer.speech_stopped"}
    transcript = {"type": "conversation.item.input_audio_transcription.completed"}
    turn.accept(endpoint, 1.2 if invalid == "early" else 2)
    if invalid != "missing":
        turn.accept(transcript, 2.1)
    turn.accept({"type": "output_audio_buffer.started", "response_id": "spoken"}, 8)
    turn.accept_audio(8.1)
    turn.accept(answer("Alpha 713. Beta 829."), 9)
    microphone = SimpleNamespace(last_voice_at=None if invalid == "no-speech" else 1.5,
        index=0 if invalid == "incomplete" else 1, chunks=[b"pcm"])
    if invalid == "split":
        with pytest.raises(RuntimeError, match="split"):
            turn.accept(endpoint, 3)
    elif invalid:
        with pytest.raises(RuntimeError, match="discard timings"):
            probe_module.measured_result(turn, microphone)
    else:
        result = probe_module.measured_result(turn, microphone)
        assert result["timing_reference"] == "speech_end"
        assert result["first_non_silent_rtp_s"] == 6.6
        assert result["endpoint_s"] == 0.5 and result["transcription_s"] == 0.6
        assert turn.started == 1  # Never relabel the send-time timestamp as speech end.
        assert probe_module.measured_result(turn, None)["timing_reference"] == "text_submitted"


@pytest.mark.parametrize("setup_event", [
    {"type": "error", "error": {"message": "do not print provider details"}},
    {"type": "output_audio_buffer.started", "response_id": "unexpected"},
    {"type": "response.done", "response": {"status": "completed", "output": [{
        "type": "message", "role": "assistant", "content": [{"type": "output_text",
        "text": "Model switched to `gpt-6-astra`\nProvider: ChatGPT or Codex Subscription\n_(session only — add `--global` to persist)_"}]}]}},
    None,
])
@pytest.mark.asyncio
async def test_failed_model_setup_closes_peer_without_starting_speech(probe_module, monkeypatch, setup_event):
    sent, microphones, peers = [], [], []

    class Channel:
        def on(self, name):
            def register(callback):
                self.callback = callback
            return register

        def send(self, raw):
            sent.append(json.loads(raw))
            if sent[-1]["type"] == "response.create" and setup_event is not None:
                self.callback(json.dumps(setup_event))

    class Peer:
        def __init__(self, config):
            self.channel, self.closed = Channel(), False
            peers.append(self)

        def createDataChannel(self, name):
            return self.channel

        def on(self, name):
            return lambda callback: None

        def addTrack(self, track):
            pass

        async def createOffer(self):
            return SimpleNamespace(sdp="synthetic offer")

        async def setLocalDescription(self, description):
            self.localDescription = description

        async def setRemoteDescription(self, description):
            self.channel.callback(json.dumps({"type": "session.created"}))

        async def close(self):
            self.closed = True

    class Microphone:
        def __init__(self, fixture):
            self.started = self.stopped = False
            microphones.append(self)

        def begin_turn(self):
            self.started = True

        def stop(self):
            self.stopped = True

    class HTTP:
        status, headers = 201, {"Location": "/v1/realtime/calls/isolated"}

        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def post(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            return self

        async def text(self):
            return "synthetic answer"

    original_wait = probe_module.ModelSetup.wait

    async def bounded_wait(self):
        await original_wait(self, timeout=0.01)

    monkeypatch.setattr(probe_module.ModelSetup, "wait", bounded_wait)
    monkeypatch.setattr(probe_module, "RTCPeerConnection", Peer)
    monkeypatch.setattr(probe_module, "SyntheticMicrophone", Microphone)
    monkeypatch.setattr(probe_module, "ClientSession", HTTP)
    result = await probe_module.probe("http://fixture.invalid", {"gateway_key": "dummy-secret"},
        case="dependent", input_mode="voice", model="gpt-5.6-terra", fixture=b"\x00\x10")
    assert result["ok"] is False and result["call_id"] == "isolated"
    assert result["failure"] == "Tool probe setup or transport failed"
    assert "dummy-secret" not in json.dumps(result) and "provider details" not in json.dumps(result)
    assert peers[0].closed and microphones[0].stopped and not microphones[0].started
    assert len(sent) == 2  # Only the setup command, never a measured input_text fallback.
