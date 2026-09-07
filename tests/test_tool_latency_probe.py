"""Tool-canary correlation, without credentials, model calls or playback."""

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def probe_module(monkeypatch):
    directory = Path(__file__).parents[1] / "tools"
    monkeypatch.syspath_prepend(str(directory))
    spec = importlib.util.spec_from_file_location("tool_latency_probe", directory / "tool_latency_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def sealed(name, call_id):
    return {"type": "response.done", "response": {"id": "response-" + call_id,
        "status": "completed", "output": [{"type": "function_call", "name": name,
        "call_id": call_id, "arguments": "{}"}]}}


def answer(text, status="completed"):
    return {"type": "response.done", "response": {"id": "spoken", "status": status,
        "output": [{"type": "message", "role": "assistant",
                    "content": [{"type": "output_audio", "transcript": text}]}]}}


def settled_turn(module, case="success"):
    turn = module.ToolTurn(case, dict(zip(module.NAMES, (713, 829))))
    turn.started = 1.0
    for index, name in enumerate(module.NAMES):
        call_id = f"fixture-{index}"
        assert turn.accept(sealed(name, call_id), 2 + index) == [call_id]
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


@pytest.mark.parametrize("case", ["success", "parallel", "mixed", "timeout"])
def test_completion_requires_both_results_and_matching_spoken_answer(probe_module, case):
    turn = probe_module.ToolTurn(case, dict(zip(probe_module.NAMES, (713, 829))))
    turn.accept(answer("Welcome."), 0.5)
    turn.started = 1
    turn.accept(answer("Welcome."), 1.5)
    assert not turn.done.is_set()
    turn = settled_turn(probe_module, case)
    assert not turn.done.is_set()  # Two function response seals are not an answer.
    text = "Alpha 713. Beta 829." if case in {"success", "parallel"} else "Alpha 713. Beta unavailable."
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
