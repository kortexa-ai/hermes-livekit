#!/usr/bin/env python3
"""Silent Mira client-tool canary; no microphone, playback, or config changes.

Run with the same environment as tools/voice_latency_probe.py. The success
and mixed cases inject a 400 ms client wait. The timeout case withholds beta's
result until the server expires it, then checks rejection of that late result.
Credentials stay in memory. Only fixture results and bounded timings are kept.
"""

from __future__ import annotations

import argparse
import asyncio
from array import array
import hashlib
import json
import random
import time

from aiohttp import ClientSession, ClientTimeout, FormData
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from av import AudioResampler

from voice_latency_probe import (
    MODEL_CHOICES, RATE, ModelSetup, SyntheticMicrophone, completion_text,
    profile_credentials, speech_fixture, validate_single_turn,
)


NAMES = ("latency_alpha", "latency_beta")
CASES = ("success", "parallel", "dependent", "mixed", "timeout", "cancel")
SPOKEN_PROMPT = "Please check both fixture values and tell me the results."
INSTRUCTIONS = (
    "This is an isolated client-tool latency test. Use both supplied functions "
    "exactly once, and no other tools. Their values are unknown until they return. "
    "After both settle, answer exactly: Alpha <alpha value>. Beta <beta value>. "
    "Use digits for values. If a function fails or times out, use unavailable "
    "for that value. Do not retry failed tools or speak before both settle."
)


def tool_session(case: str) -> dict:
    """The dependent case cannot be solved without consuming alpha's result."""
    instructions = INSTRUCTIONS
    tools = [{"type": "function", "name": name,
              "description": "Return the " + name.removeprefix("latency_") + " fixture value.",
              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}
             for name in NAMES]
    if case == "parallel":
        instructions += (
            " Alpha and beta are independent. Request both in the same tool round, "
            "in parallel; do not wait for alpha's result to request beta."
        )
    if case == "dependent":
        instructions += " Call alpha first, then pass its returned value as beta's alpha_value argument."
        tools[1]["parameters"].update(
            properties={"alpha_value": {"type": "integer"}}, required=["alpha_value"],
        )
    return {"type": "realtime", "instructions": instructions, "tool_choice": "auto", "tools": tools}


def validate_input(input_mode: str, fixture: bytes | None) -> None:
    if input_mode not in {"text", "voice"}:
        raise ValueError("Input must be text or voice")
    if fixture is not None and (input_mode != "voice" or not isinstance(fixture, bytes)
                               or not 0 < len(fixture) <= RATE * 2 * 30 or len(fixture) % 2):
        raise ValueError("Fixture requires voice input and at most 30 seconds of mono 48 kHz int16 PCM")


class ToolTurn:
    """Correlate tool seals, result acknowledgements and actual audio completion."""

    def __init__(self, case: str, values: dict[str, int]):
        self.case, self.values = case, values
        self.started = None
        self.calls = {}
        self.audio = {}
        self.captions = {}
        self.active_audio = None
        self.errors = []
        self.failure = None
        self.final = None
        self.input_events, self.input_times = [], {}
        self.done = asyncio.Event()

    def accept(self, event: dict, now: float) -> list[str]:
        """Return sealed calls ready for a client result (never act on deltas)."""
        if self.started is None:
            return []
        kind = event.get("type")
        if kind in {"input_audio_buffer.speech_stopped", "conversation.item.input_audio_transcription.completed"}:
            if kind in self.input_times:
                raise RuntimeError("Spoken fixture split into multiple turns; discard timings")
            self.input_events.append({"type": kind})
            self.input_times[kind] = now
        if (kind in {"response.output_audio_transcript.delta", "response.output_audio_transcript.done"}
                and (event.get("delta") or event.get("transcript"))):
            response_id = event.get("response_id")
            if not isinstance(response_id, str) or len(self.captions) >= 8 and response_id not in self.captions:
                raise RuntimeError("Unexpected caption response count or identity")
            self.captions.setdefault(response_id, now)
        if kind == "error":
            error = event.get("error", {})
            code = error.get("code")
            if self.case == "timeout" and code == "tool_timeout":
                pending = [key for key, call in self.calls.items()
                           if call["name"] == NAMES[1] and call["sent_at"] is None]
                if len(pending) == 1 and code not in self.errors:
                    self.errors.append(code)
                    self.calls[pending[0]]["timed_out"] = True
                    return pending
            if (self.case == "timeout" and code == "unknown_tool_call" and code not in self.errors
                    and "tool_timeout" in self.errors
                    and error.get("event_id") == "fixture-late-result"):
                self.errors.append(code)
                return []
            if code in {"no_active_response", "conversation_already_has_active_response"}:
                self.errors.append(code)
            raise RuntimeError("Unexpected protocol error")
        if kind in {"conversation.item.added", "conversation.item.done"}:
            item = event.get("item", {})
            if item.get("type") == "function_call_output":
                call = self.calls.get(item.get("call_id"))
                if call is None or call["sent_at"] is None or call["timed_out"]:
                    raise RuntimeError("Unsent or expired tool result was acknowledged")
                call["acked_at"] = call["acked_at"] or now
        if kind == "output_audio_buffer.started":
            self.active_audio = event.get("response_id")
            if len(self.audio) >= 4 or not isinstance(self.active_audio, str):
                raise RuntimeError("Unexpected audio response count or identity")
            self.audio.setdefault(self.active_audio, {"started_at": now, "rtp_at": None})
        if kind != "response.done":
            return []
        response = event.get("response", {})
        output = response.get("output", [])
        functions = [item for item in output if item.get("type") == "function_call"]
        if functions:
            if response.get("status") != "completed":
                raise RuntimeError("Function response did not complete")
            return [self._add_call(item, now) for item in functions]
        # Welcome text and function-call responses are not the spoken answer.
        if response.get("id") in self.audio:
            self.final = response
            self.done.set()
        return []

    def _add_call(self, item: dict, now: float) -> str:
        name, call_id = item.get("name"), item.get("call_id")
        arguments = json.loads(item.get("arguments", "null"))
        expected = {}
        if self.case == "dependent" and name == NAMES[1]:
            alpha = next((c for c in self.calls.values() if c["name"] == NAMES[0]), None)
            if alpha is None or alpha["acked_at"] is None:
                raise RuntimeError("Dependent beta preceded alpha's acknowledged result")
            expected = {"alpha_value": self.values[NAMES[0]]}
            if not isinstance(arguments, dict) or type(arguments.get("alpha_value")) is not int:
                raise RuntimeError("Dependent beta requires the returned integer alpha value")
        if (name not in NAMES or len(self.calls) >= 2
                or not isinstance(call_id, str) or not 1 <= len(call_id) <= 128
                or call_id in self.calls or any(c["name"] == name for c in self.calls.values())
                or arguments != expected):
            raise RuntimeError("Unexpected or duplicate fixture invocation")
        self.calls[call_id] = {"name": name, "received_at": now,
                               "sent_at": None, "acked_at": None, "timed_out": False}
        return call_id

    def result_events(self, call_id: str, now: float) -> list[dict]:
        call = self.calls[call_id]
        if call["sent_at"] is not None:
            raise RuntimeError("Fixture result already sent")
        failed = self.case == "mixed" and call["name"] == NAMES[1]
        value = {"error": "fixture_unavailable"} if failed else {"value": self.values[call["name"]]}
        call["sent_at"] = now
        events = [{"type": "conversation.item.create",
                   "event_id": "fixture-late-result" if call["timed_out"] else "fixture-result-" + call_id,
                   "item": {"type": "function_call_output", "call_id": call_id,
                            "output": json.dumps(value)}}]
        if not call["timed_out"]:
            events.append({"type": "response.create"})
        return events

    def accept_audio(self, now: float) -> None:
        audio = self.audio.get(self.active_audio)
        if audio is not None and audio["rtp_at"] is None:
            audio["rtp_at"] = now

    def result(self, *, reference: float | None = None) -> dict:
        reference = self.started if reference is None else reference
        if self.failure:
            raise RuntimeError(self.failure)
        if self.final is None or self.final.get("status") != "completed":
            raise RuntimeError("No completed spoken answer")
        if {c["name"] for c in self.calls.values()} != set(NAMES):
            raise RuntimeError("Answer omitted a required fixture call")
        beta = self.values[NAMES[1]] if self.case in {"success", "parallel", "dependent"} else "unavailable"
        expected = f"Alpha {self.values[NAMES[0]]}. Beta {beta}."
        if completion_text(self.final).strip() != expected:
            raise RuntimeError("Answer did not preserve the fixture results")
        audio = self.audio[self.final["id"]]
        if audio["rtp_at"] is None:
            raise RuntimeError("No non-silent RTP received for the answer")
        if len(self.audio) != 1 or min(audio["started_at"], audio["rtp_at"]) < reference:
            raise RuntimeError("Unexpected or premature audio; discard timings")
        for call in self.calls.values():
            if not call["timed_out"] and (call["acked_at"] is None or call["acked_at"] > audio["started_at"]):
                raise RuntimeError("Answer preceded a required tool result")
        if self.case == "timeout" and sorted(self.errors) != ["tool_timeout", "unknown_tool_call"]:
            raise RuntimeError("Timeout and stale-result rejection were not both observed")
        return {"case": self.case, "answer": expected,
                "first_caption_s": (round(self.captions[self.final["id"]] - reference, 3)
                                    if self.final["id"] in self.captions else None),
                "audio_start_s": round(audio["started_at"] - reference, 3),
                "first_non_silent_rtp_s": round(audio["rtp_at"] - reference, 3),
                "errors": self.errors,
                "tools": [{"name": c["name"],
                           "requested_s": round(c["received_at"] - reference, 3),
                           "client_wait_s": round(c["sent_at"] - c["received_at"], 3),
                           "acknowledged": c["acked_at"] is not None,
                           "timed_out": c["timed_out"]} for c in self.calls.values()]}


class CancelledToolTurn(ToolTurn):
    """Cancel beta while pending, then require rejection of its late result."""

    def __init__(self, case, values):
        super().__init__(case, values)
        self.cancel_sent_at = self.cancelled_at = None

    def result_events(self, call_id, now):
        if self.calls[call_id]["name"] != NAMES[1]:
            return super().result_events(call_id, now)
        if self.cancel_sent_at is None:
            self.cancel_sent_at = now
            return [{"type": "response.cancel", "event_id": "fixture-cancel"}]
        if self.cancelled_at is None or self.calls[call_id]["sent_at"] is not None:
            raise RuntimeError("Late fixture result requires confirmed cancellation")
        self.calls[call_id]["sent_at"] = now
        return [{"type": "conversation.item.create", "event_id": "fixture-late-result",
                 "item": {"type": "function_call_output", "call_id": call_id,
                          "output": json.dumps({"value": self.values[NAMES[1]]})}}]

    def accept(self, event, now):
        response = event.get("response", {})
        if event.get("type") == "response.done" and response.get("status") == "cancelled":
            pending = [key for key, call in self.calls.items() if call["name"] == NAMES[1]]
            if self.cancel_sent_at is None or self.cancelled_at is not None or len(pending) != 1:
                raise RuntimeError("Unrequested or duplicate cancellation")
            self.final, self.cancelled_at = response, now
            return pending
        error = event.get("error", {})
        if event.get("type") == "error" and error.get("code") == "unknown_tool_call":
            if (self.cancelled_at is None or self.errors
                    or error.get("event_id") != "fixture-late-result"
                    or not any(c["name"] == NAMES[1] and c["sent_at"] is not None for c in self.calls.values())):
                raise RuntimeError("Uncorrelated stale-result rejection")
            self.errors.append("unknown_tool_call")
            self.done.set()
            return []
        return super().accept(event, now)

    def result(self, *, reference=None):
        if self.failure:
            raise RuntimeError(self.failure)
        if (self.final is None or self.final.get("status") != "cancelled" or self.audio
                or self.cancel_sent_at is None or self.cancelled_at is None
                or self.errors != ["unknown_tool_call"]):
            raise RuntimeError("Cancellation did not settle without audio and reject the stale result")
        if {c["name"] for c in self.calls.values()} != set(NAMES):
            raise RuntimeError("Cancellation fixture omitted a required call")
        for call in self.calls.values():
            if call["name"] == NAMES[0] and (call["acked_at"] is None or call["acked_at"] > self.cancel_sent_at):
                raise RuntimeError("Cancellation lost the earlier tool acknowledgement")
            if call["name"] == NAMES[1] and (call["sent_at"] is None or call["acked_at"] is not None):
                raise RuntimeError("Cancelled result was unsent or incorrectly acknowledged")
        return {"case": self.case, "cancel_s": round(self.cancelled_at - self.cancel_sent_at, 3),
                "errors": self.errors, "audio_responses": len(self.audio)}


def measured_result(turn, microphone):
    if microphone is None:
        return {"timing_reference": "text_submitted", **turn.result()}
    reference = microphone.last_voice_at
    if reference is None or microphone.index != len(microphone.chunks):
        raise RuntimeError("Spoken fixture did not finish; discard timings")
    validate_single_turn(turn.input_events, turn.input_times, reference)
    return {"timing_reference": "speech_end", **turn.result(reference=reference),
            "endpoint_s": round(turn.input_times["input_audio_buffer.speech_stopped"] - reference, 3),
            "transcription_s": round(turn.input_times["conversation.item.input_audio_transcription.completed"] - reference, 3)}


async def probe(gateway: str, credentials: dict, *, case: str, timeout: float = 120,
                input_mode: str = "text", model: str = "profile", fixture: bytes | None = None) -> dict:
    validate_input(input_mode, fixture)
    if case not in CASES:
        raise ValueError("Unsupported fixture case")
    setup = None if model == "profile" else ModelSetup(model)
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    channel = peer.createDataChannel("oai-events")
    ready, jobs = asyncio.Event(), set()
    turn_type = CancelledToolTurn if case == "cancel" else ToolTurn
    turn = turn_type(case, dict(zip(NAMES, random.sample(range(100, 1000), 2))))
    location = None
    microphone = None

    def identity():
        return {"call_id": location, "case": case, "input": input_mode, "model": model,
                "fixture_sha256": hashlib.sha256(fixture).hexdigest() if fixture is not None else None}

    def fail(_exc):
        # Provider/transport diagnostics may contain arbitrary text; do not echo them.
        turn.failure = "Tool probe event or transport failed"
        turn.done.set()
        if setup is not None:
            setup.error = turn.failure
            setup.done.set()

    def spawn(coro):
        task = asyncio.create_task(coro)
        jobs.add(task)
        def finished(done):
            jobs.discard(done)
            if not done.cancelled() and done.exception() is not None:
                fail(done.exception())
        task.add_done_callback(finished)

    async def deliver(call_id):
        call = turn.calls[call_id]
        if case == "timeout" and call["name"] == NAMES[1] and not call["timed_out"]:
            return  # Let the real server deadline fire; never shorten production's timeout.
        if call["name"] == NAMES[0]:
            await asyncio.sleep(0.4)
        for event in turn.result_events(call_id, time.monotonic()):
            channel.send(json.dumps(event))

    @channel.on("message")
    def on_message(raw):
        try:
            if not isinstance(raw, str) or len(raw) > 65536:
                raise ValueError("invalid frame")
            event = json.loads(raw)
            if event.get("type") == "session.created":
                ready.set()
            if setup is not None:
                setup.accept(event)
                return
            for call_id in turn.accept(event, time.monotonic()):
                spawn(deliver(call_id))
        except Exception as exc:
            fail(exc)

    @peer.on("track")
    def on_track(track):
        async def drain():
            resampler = AudioResampler(format="s16", layout="mono", rate=48000)
            while True:
                frame = await track.recv()
                for converted in resampler.resample(frame):
                    pcm = bytes(converted.planes[0])[:converted.samples * 2]
                    if any(abs(v) > 40 for v in array("h", pcm)):
                        turn.accept_audio(time.monotonic())
        spawn(drain())

    try:
        async with ClientSession(timeout=ClientTimeout(total=30)) as http:
            if input_mode == "voice":
                if fixture is None:
                    fixture = await speech_fixture(http, credentials["tts"], SPOKEN_PROMPT)
                validate_input(input_mode, fixture)
                microphone = SyntheticMicrophone(fixture)
                peer.addTrack(microphone)
            else:
                peer.addTransceiver("audio", direction="recvonly")
            await peer.setLocalDescription(await peer.createOffer())
            form = FormData(default_to_multipart=True)
            form.add_field("sdp", peer.localDescription.sdp)
            form.add_field("session", json.dumps(tool_session(case)))
            async with http.post(gateway + "/v1/realtime/calls", data=form,
                                 headers={"Authorization": "Bearer " + credentials["gateway_key"]},
                                 allow_redirects=False) as response:
                if response.status != 201:
                    raise RuntimeError(f"Call setup returned HTTP {response.status}")
                location = response.headers.get("Location", "").rsplit("/", 1)[-1]
                await peer.setRemoteDescription(RTCSessionDescription(sdp=await response.text(), type="answer"))
            await asyncio.wait_for(ready.wait(), 15)
            if setup is not None:
                setup.send(channel)
                await setup.wait()
                setup = None
            if microphone is not None:
                await asyncio.sleep(1)  # Calibrate on the same synthetic room noise as the voice canary.
            if turn.failure:
                raise RuntimeError(turn.failure)
            turn.started = time.monotonic()
            if microphone is not None:
                microphone.begin_turn()
            else:
                channel.send(json.dumps({"type": "conversation.item.create", "item": {
                    "type": "message", "role": "user", "content": [{"type": "input_text",
                    "text": "Use latency_alpha and latency_beta once each. Report both returned values."}]}}))
                channel.send(json.dumps({"type": "response.create"}))
            try:
                await asyncio.wait_for(turn.done.wait(), timeout)
                if case == "cancel":
                    # Observe immediate late work without ever opening playback.
                    await asyncio.sleep(2)
                result = measured_result(turn, microphone)
            except (RuntimeError, asyncio.TimeoutError) as exc:
                # Retain routing and fixture-only state when a real model does
                # not follow the contract; do not lose the failed call's identity.
                return {"ok": False, **identity(),
                        "failure": str(exc) if isinstance(exc, RuntimeError) else "Tool probe deadline expired",
                        "errors": turn.errors,
                        "tools": list(turn.calls.values()),
                        "answer": completion_text(turn.final or {})[:500]}
            return {"ok": True, **identity(), **result}
    except (RuntimeError, asyncio.TimeoutError):
        return {"ok": False, **identity(), "failure": "Tool probe setup or transport failed"}
    finally:
        if microphone is not None:
            microphone.stop()
        for task in list(jobs):
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        await peer.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default="http://192.168.2.6:8092")
    parser.add_argument("--config-host", default="snappy")
    parser.add_argument("--profile", default="mira")
    parser.add_argument("--case", choices=CASES, default="success")
    parser.add_argument("--input", choices=("text", "voice"), default="text",
                        help="Voice sends synthetic PCM through ASR; neither mode captures or plays audio")
    parser.add_argument("--model", choices=MODEL_CHOICES, default="profile",
                        help="Native session-only model override; never persists a profile change")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    credentials = profile_credentials(args.config_host, args.profile)
    result = asyncio.run(probe(args.gateway.rstrip("/"), credentials, case=args.case,
                              input_mode=args.input, model=args.model))
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
