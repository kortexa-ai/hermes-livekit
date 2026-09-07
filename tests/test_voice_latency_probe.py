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


def test_short_opening_case_binds_instructions_and_expected_answer(probe_module):
    args = probe_module.parse_args(["--case", "first_sentence"])
    options = probe_module.case_options(args.case)
    assert options["prompt"] == probe_module.PROMPTS[args.case]
    assert options["expected_answer"].startswith("Yes. ")
    assert options["expected_answer"] in options["instructions"]
    assert "expected_answer" not in probe_module.case_options("fact")


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


@pytest.mark.parametrize("bad_audio", [None, "early", "duplicate", "unidentified"])
def test_voice_turn_correlates_completion_and_rejects_early_audio(probe_module, bad_audio):
    turn = probe_module.VoiceTurn(ignored_response_ids={"previous"})
    turn.accept({"type": "output_audio_buffer.started", "response_id": "previous"}, 0.0)
    old = completed("previous answer")
    old["response"]["id"] = "previous"
    turn.accept(old, 0.1)
    assert not turn.done.is_set() and not turn.events

    turn.accept({"type": "input_audio_buffer.speech_stopped"}, 1.7)
    turn.accept({"type": "conversation.item.input_audio_transcription.completed"}, 2.0)
    turn.accept({"type": "output_audio_buffer.started", "response_id": "current"}, 3.0)
    turn.accept_audio(0.5 if bad_audio == "early" else 3.1)
    if bad_audio in {"duplicate", "unidentified"}:
        turn.accept({"type": "output_audio_buffer.started",
                     "response_id": "extra" if bad_audio == "duplicate" else None}, 3.2)
    answer = completed("current answer")
    answer["response"]["id"] = "current"
    turn.accept(answer, 4.0)
    assert turn.done.is_set()
    if bad_audio:
        with pytest.raises(RuntimeError, match="discard timings"):
            turn.result(last_voice_at=1.0, fixture_complete=True)
    else:
        result = turn.result(last_voice_at=1.0, fixture_complete=True, expected_answer="current answer")
        assert result["response_id"] == "current"
        assert result["audible_rtp_after_speech_s"] == 2.1
        assert result["response_count"] == result["audio_response_count"] == 1
        with pytest.raises(RuntimeError, match="Fixed-answer fixture"):
            turn.result(last_voice_at=1.0, fixture_complete=True, expected_answer="different answer")


@pytest.mark.parametrize("caption", [
    {"delta": "Yes."}, {"delta": "", "transcript": "Yes."}, None,
])
def test_caption_metrics_use_nonempty_current_response_frames(probe_module, caption):
    turn = probe_module.VoiceTurn(ignored_response_ids={"previous"})
    delta = {"type": "response.output_audio_transcript.delta", "response_id": "current"}
    turn.accept({**delta, "response_id": "previous", "delta": "Old"}, 0.1)
    turn.accept({**delta, "response_id": "notice", "delta": "Unrelated"}, 0.2)
    turn.accept({**delta, "delta": ""}, 0.3)
    turn.accept({**delta, "delta": "ignored", "transcript": ""}, 0.4)
    turn.accept({"type": "response.output_audio_transcript.done", "response_id": "current",
                 "transcript": "First-contact setup notice"}, 0.5)
    turn.accept({"type": "input_audio_buffer.speech_stopped"}, 1.7)
    turn.accept({"type": "conversation.item.input_audio_transcription.completed"}, 1.8)
    if caption is not None:
        turn.accept({**delta, **caption}, 2.0)
        turn.accept({**delta, "delta": " More text."}, 2.3)
    turn.accept({"type": "output_audio_buffer.started", "response_id": "current"}, 3.0)
    turn.accept_audio(3.2)
    response = completed("Yes. More text.")
    response["response"]["id"] = "current"
    turn.accept(response, 4.0)
    result = turn.result(last_voice_at=1.0, fixture_complete=True)
    if caption is None:
        assert "caption_after_speech_s" not in result
        assert "caption_to_audio_event_s" not in result
    else:
        assert result["caption_after_speech_s"] == 1.0
        assert result["caption_to_audio_event_s"] == 1.0
        assert result["caption_to_audible_rtp_s"] == 1.2
        turn.caption_times["current"] = 0.9
        with pytest.raises(RuntimeError, match="caption preceded"):
            turn.result(last_voice_at=1.0, fixture_complete=True)


@pytest.mark.asyncio
async def test_replayed_fixture_preserves_pcm_and_rtp_clock(probe_module):
    assert probe_module.parse_args([]).turns == 1
    assert probe_module.parse_args(["--turns", "4"]).turns == 4
    for invalid in ("0", "11", "1.5"):
        with pytest.raises(SystemExit):
            probe_module.parse_args(["--turns", invalid])
    pcm = b"\x00\x10" * probe_module.SAMPLES * 2
    microphone = probe_module.SyntheticMicrophone(pcm)
    try:
        frames = []
        for _ in range(2):
            microphone.begin_turn()
            assert microphone.last_voice_at is None
            frames.append([await microphone.recv(), await microphone.recv()])
            assert microphone.index == len(microphone.chunks)
        assert bytes(frames[0][0].planes[0]) == bytes(frames[1][0].planes[0]) == pcm[:probe_module.SAMPLES * 2]
        assert frames[1][0].pts > frames[0][-1].pts
        microphone.begin_turn()
        with pytest.raises(RuntimeError, match="unfinished"):
            microphone.begin_turn()
    finally:
        microphone.stop()
