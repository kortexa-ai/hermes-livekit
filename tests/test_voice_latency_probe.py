"""Offline probe case selection; no credentials or network used."""

import importlib.util
import asyncio
import json
from pathlib import Path

import pytest


@pytest.fixture
def probe_module():
    path = Path(__file__).parents[1] / "tools" / "voice_latency_probe.py"
    spec = importlib.util.spec_from_file_location("voice_latency_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("case", ["greeting", "fact", "calculation"])
def test_named_cases(probe_module, case):
    args = probe_module.parse_args(["--case", case])
    assert args.case == case
    assert probe_module.PROMPTS[case]


def test_default_avoids_calculation_and_retains_old_fixture(probe_module):
    assert probe_module.parse_args([]).case == "greeting"
    assert probe_module.PROMPTS["calculation"] == probe_module.PROMPT


def test_unknown_case_rejected(probe_module):
    with pytest.raises(SystemExit):
        probe_module.parse_args(["--case", "unknown"])


@pytest.mark.parametrize("extra_endpoint, endpoint_time, valid", [
    (False, 1.7, True), (True, 1.7, False), (False, 0.7, False),
])
def test_split_or_premature_turns_are_not_reported_as_latency(
    probe_module, extra_endpoint, endpoint_time, valid,
):
    endpoint = "input_audio_buffer.speech_stopped"
    transcript = "conversation.item.input_audio_transcription.completed"
    events = [{"type": endpoint}, {"type": transcript}]
    if extra_endpoint:
        events.append({"type": endpoint})
    times = {endpoint: endpoint_time, transcript: 2.0}
    if valid:
        probe_module.validate_single_turn(events, times, 1.0)
    else:
        with pytest.raises(RuntimeError, match="discard timings"):
            probe_module.validate_single_turn(events, times, 1.0)


def test_missing_transcript_rejected(probe_module):
    with pytest.raises(RuntimeError, match="discard timings"):
        probe_module.validate_single_turn([], {}, 1.0)


def completed(text, *, status="completed"):
    return {"type": "response.done", "response": {"status": status, "output": [{
        "type": "message", "role": "assistant", "content": [{
            "type": "output_audio", "transcript": text,
        }],
    }]}}


@pytest.mark.parametrize("effort", ["low", "medium"])
@pytest.mark.asyncio
async def test_reasoning_command_is_session_only_and_confirmed(probe_module, effort):
    sent = []

    class Channel:
        def send(self, raw):
            sent.append(json.loads(raw))

    setup = probe_module.ReasoningSetup(effort)
    setup.send(Channel())
    assert sent[0]["item"]["content"] == [{"type": "input_text", "text": f"/reasoning {effort}"}]
    assert sent[1] == {"type": "response.create"}
    setup.accept(completed("Use /sethome to choose a home channel"))
    assert not setup.done.is_set()
    setup.accept(completed(f"🧠 ✓ Reasoning effort set to `{effort}` (session only — add --global to persist)"))
    await setup.wait()


@pytest.mark.parametrize("effort", ["none", "high", "low --global", "--global medium", ""])
def test_only_bounded_test_efforts_are_accepted(probe_module, effort):
    with pytest.raises(ValueError):
        probe_module.ReasoningSetup(effort)


@pytest.mark.parametrize("response", [
    {"type": "error"}, {"type": "output_audio_buffer.started"},
    completed("Reasoning effort set to `low` (saved to config)"),
    completed("Reasoning effort set to `medium` (session only)"),
    completed("Reasoning effort set to `low` (session only)", status="failed"),
])
@pytest.mark.asyncio
async def test_unconfirmed_scope_or_setup_errors_fail(probe_module, response):
    setup = probe_module.ReasoningSetup("low")
    setup.accept(response)
    with pytest.raises(RuntimeError):
        await setup.wait()


@pytest.mark.asyncio
async def test_missing_confirmation_times_out(probe_module):
    setup = probe_module.ReasoningSetup("low")
    setup.accept(completed("Unrelated response"))
    with pytest.raises(asyncio.TimeoutError):
        await setup.wait(timeout=0.01)


def test_bounded_answer_excludes_reasoning(probe_module):
    response = completed("x" * 600)["response"]
    response["output"].insert(0, {"type": "reasoning", "role": "assistant",
                                   "content": [{"type": "output_text", "text": "private"}]})
    assert probe_module.completion_text(response) == "x" * 500


def test_reasoning_and_answer_output_are_opt_in(probe_module):
    args = probe_module.parse_args([])
    assert args.reasoning == "profile" and not args.show_answer
    args = probe_module.parse_args(["--reasoning", "low", "--show-answer"])
    assert args.reasoning == "low" and args.show_answer
