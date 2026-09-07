#!/usr/bin/env python3
"""Silent, isolated voice canary: synthetic speech -> gateway -> received RTP.

Run on the Pi with aiortc, aiohttp and av installed. Credentials are fetched
over SSH into memory; the probe never records the microphone or plays sound.
It creates real Hermes voice turns, so it does use the configured services.
"""

from __future__ import annotations

import argparse
import asyncio
from array import array
from fractions import Fraction
import hashlib
import json
import math
import random
import re
import shlex
import subprocess
import time

from aiohttp import ClientSession, ClientTimeout, FormData
from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from av import AudioFrame, AudioResampler

RATE = 48000
SAMPLES = 960
PROMPT = "Please tell me what two plus two equals in one short sentence."
DEFAULT_INSTRUCTIONS = "This is a voice latency check. Reply with one short sentence and do not use tools."
FIRST_SENTENCE_ANSWER = (
    "Yes. Streaming speech begins playback before the whole answer is ready, "
    "so the conversation can feel faster without waiting for a long response to finish."
)
PROMPTS = {
    "greeting": "Please greet me in one short sentence, Hermes.",
    "fact": "Please tell me the color of a clear daytime sky in one short sentence.",
    "calculation": PROMPT,
    "first_sentence": "Please explain the benefit of streaming speech.",
}


def case_options(case: str) -> dict:
    options = {"prompt": PROMPTS[case]}
    if case == "first_sentence":
        options.update(
            instructions=("For this isolated latency check, answer the user's spoken question "
                          "with exactly the following text, including both sentences. "
                          "Do not use tools: " + FIRST_SENTENCE_ANSWER),
            expected_answer=FIRST_SENTENCE_ANSWER,
        )
    return options


def completion_text(response: dict) -> str:
    """Bounded text from this test call's completed response, never reasoning."""
    parts = []
    for item in response.get("output", []):
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        for content in item.get("content", []):
            if content.get("type") not in {"output_audio", "output_text"}:
                continue
            value = content.get("transcript") or content.get("text")
            if isinstance(value, str):
                parts.append(value[:500])
    return " ".join(parts)[:500]


class ReasoningSetup:
    """Session-only native command, confirmed before any measured speech is sent."""

    def __init__(self, effort: str):
        if effort not in {"low", "medium"}:
            raise ValueError("Only low and medium test overrides are allowed")
        self.effort = effort
        self.done = asyncio.Event()
        self.error = None

    def send(self, channel):
        channel.send(json.dumps({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": f"/reasoning {self.effort}"}],
        }}))
        channel.send(json.dumps({"type": "response.create"}))

    def accept(self, event):
        if event.get("type") in {"error", "output_audio_buffer.started"}:
            self.error = "Reasoning setup failed or produced unexpected audio"
            self.done.set()
        if event.get("type") == "response.done":
            response = event.get("response", {})
            text = completion_text(response)
            expected = f"Reasoning effort set to `{self.effort}` (session only"
            # Ignore unrelated first-contact notices. Fail closed if the server
            # reports a different scope, setting, or failed command response.
            if "Reasoning effort set to" in text:
                if expected not in text or response.get("status") != "completed":
                    self.error = "Gateway did not confirm the requested session-only reasoning setting"
                self.done.set()

    async def wait(self, timeout=15):
        await asyncio.wait_for(self.done.wait(), timeout)
        if self.error:
            raise RuntimeError(self.error)


def profile_credentials(host: str, profile: str) -> dict:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", profile) or host.startswith("-"):
        raise ValueError("Invalid profile or SSH host")
    # Use Snappy's existing environment; no secret is placed in argv or logs.
    program = """
import json, sys, yaml
from pathlib import Path
from dotenv import dotenv_values
p = Path.home() / '.hermes' / 'profiles' / sys.argv[1]
c = yaml.safe_load((p / 'config.yaml').read_text())
e = dotenv_values(p / '.env')
r = c['platforms']['realtime'].get('extra', {})
print(json.dumps({'gateway_key': r.get('api_key') or e.get('HERMES_REALTIME_API_KEY'),
                  'tts': c['tts']['openai']}))
"""
    command = shlex.join([
        "/Users/francip/src/hermes-agent/venv/bin/python", "-c", program, profile,
    ])
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", host, command],
                            check=True, capture_output=True, text=True, timeout=15)
    credentials = json.loads(result.stdout)
    if not credentials.get("gateway_key"):
        raise RuntimeError("Profile has no explicit realtime API key")
    return credentials


def converted_pcm(pcm: bytes) -> bytes:
    frame = AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
    frame.sample_rate = 24000
    frame.planes[0].update(pcm)
    resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
    return b"".join(bytes(f.planes[0])[:f.samples * 2]
                    for f in resampler.resample(frame) + resampler.resample(None))


class SyntheticMicrophone(MediaStreamTrack):
    kind = "audio"

    def __init__(self, pcm: bytes):
        super().__init__()
        self.chunks = [pcm[i:i + SAMPLES * 2].ljust(SAMPLES * 2, b"\x00")
                       for i in range(0, len(pcm), SAMPLES * 2)]
        rng = random.Random(41)
        self.room = array("h", [round(rng.gauss(0, 150)) for _ in range(SAMPLES)]).tobytes()
        self.started = False
        self.index = self.timestamp = 0
        self.deadline = None
        self.last_voice_at = None

    def begin_turn(self):
        """Replay the fixture without resetting the live RTP clock."""
        if self.started and self.index < len(self.chunks):
            raise RuntimeError("Cannot replace an unfinished speech fixture")
        self.index = 0
        self.last_voice_at = None
        self.started = True

    async def recv(self):
        now = time.monotonic()
        deadline = max(now, self.deadline or now)
        await asyncio.sleep(max(0, deadline - now))
        now = time.monotonic()
        self.deadline = now + 0.02
        pcm = self.room
        if self.started and self.index < len(self.chunks):
            pcm = self.chunks[self.index]
            self.index += 1
            values = array("h", pcm)
            if math.sqrt(sum(v * v for v in values) / len(values)) > 220:
                self.last_voice_at = now + 0.02
        frame = AudioFrame(format="s16", layout="mono", samples=SAMPLES)
        frame.planes[0].update(pcm)
        frame.sample_rate, frame.pts, frame.time_base = RATE, self.timestamp, Fraction(1, RATE)
        self.timestamp += SAMPLES
        return frame


def validate_single_turn(events: list, times: dict, last_voice_at: float) -> None:
    """Reject split/cut-off fixtures rather than mixing unrelated turn timestamps."""
    for name in ("input_audio_buffer.speech_stopped",
                 "conversation.item.input_audio_transcription.completed"):
        if sum(e.get("type") == name for e in events) != 1:
            raise RuntimeError("Probe fixture was split or not transcribed as one turn; discard timings")
        if times[name] < last_voice_at:
            raise RuntimeError("Probe endpoint preceded the end of the spoken fixture; discard timings")


class VoiceTurn:
    """One utterance's measurements; never reuse timestamps across replies."""

    def __init__(self, *, ignored_response_ids=()):
        self.events, self.times = [], {}
        self.caption_times = {}
        self.audio_response_ids = set()
        self.ignored_response_ids = set(ignored_response_ids)
        self.done = asyncio.Event()
        self.completion = None

    def accept(self, event, now):
        kind = event.get("type")
        response_id = event.get("response_id") or event.get("response", {}).get("id")
        if response_id in self.ignored_response_ids:
            return
        self.events.append(event)
        self.times.setdefault(kind, now)
        if kind == "response.output_audio_transcript.delta" and isinstance(response_id, str):
            # A native frame can replace a draft with a full snapshot and an
            # empty delta. Ignore empty frames and final-only setup notices.
            caption = event.get("transcript", event.get("delta"))
            if isinstance(caption, str) and caption.strip():
                self.caption_times.setdefault(response_id, now)
        if kind == "output_audio_buffer.started":
            self.audio_response_ids.add(response_id)
        # First-contact notices are text-only, not completion of the voice answer.
        if kind == "response.done" and response_id in self.audio_response_ids:
            self.completion = event["response"]
            self.done.set()
        if kind == "error":
            self.done.set()

    def accept_audio(self, now):
        self.times.setdefault("first_audible_rtp", now)

    def result(self, *, last_voice_at, fixture_complete, reasoning="profile", show_answer=False,
               expected_answer=None):
        if any(e.get("type") == "error" for e in self.events):
            raise RuntimeError("Gateway emitted a protocol error")
        if self.completion is None or self.completion.get("status") != "completed":
            raise RuntimeError("Gateway response did not complete successfully")
        if len(self.audio_response_ids) != 1 or None in self.audio_response_ids:
            raise RuntimeError("Expected exactly one identified audio reply; discard timings")
        if "first_audible_rtp" not in self.times or last_voice_at is None:
            raise RuntimeError("No synthesized response audio received")
        validate_single_turn(self.events, self.times, last_voice_at)
        if not fixture_complete:
            raise RuntimeError("Reply completed before the spoken fixture finished; discard timings")
        if self.times["first_audible_rtp"] < last_voice_at:
            raise RuntimeError("Response audio preceded the end of the spoken fixture; discard timings")
        result = {"response_id": self.completion["id"]}
        for label, event_name in [
            ("endpoint", "input_audio_buffer.speech_stopped"),
            ("transcript", "conversation.item.input_audio_transcription.completed"),
            ("audio_event", "output_audio_buffer.started"),
            ("audible_rtp", "first_audible_rtp"),
        ]:
            result[label + "_after_speech_s"] = round(self.times[event_name] - last_voice_at, 3)
        result["response_count"] = sum(e.get("type") == "response.done" for e in self.events)
        result["audio_response_count"] = len(self.audio_response_ids)
        result["reasoning"] = reasoning
        if (caption_at := self.caption_times.get(self.completion["id"])) is not None:
            if caption_at < last_voice_at:
                raise RuntimeError("Response caption preceded the end of speech; discard timings")
            result.update(
                caption_after_speech_s=round(caption_at - last_voice_at, 3),
                caption_to_audio_event_s=round(self.times["output_audio_buffer.started"] - caption_at, 3),
                caption_to_audible_rtp_s=round(self.times["first_audible_rtp"] - caption_at, 3),
            )
        answer = completion_text(self.completion)
        if expected_answer is not None and answer != expected_answer:
            raise RuntimeError("Fixed-answer fixture was not followed; discard timings")
        result["answer_word_count"] = len(answer.split())
        if show_answer:
            result["answer"] = answer
        return result


async def probe(gateway: str, credentials: dict, *, prompt: str = PROMPT,
                reasoning: str = "profile", show_answer: bool = False, turns: int = 1,
                instructions: str = DEFAULT_INSTRUCTIONS, expected_answer: str | None = None) -> dict:
    if type(turns) is not int or not 1 <= turns <= 10:
        raise ValueError("turns must be an integer from 1 to 10")
    audio_tasks = []
    ready = asyncio.Event()
    measurement = None
    last_audible_at = 0.0
    setup = None if reasoning == "profile" else ReasoningSetup(reasoning)
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    microphone = None
    location = None
    headers = {"Authorization": "Bearer " + credentials["gateway_key"]}
    try:
        async with ClientSession(timeout=ClientTimeout(total=40)) as http:
            tts = credentials["tts"]
            async with http.post(tts["base_url"].rstrip("/") + "/audio/speech",
                                 headers={"Authorization": "Bearer " + tts["api_key"]},
                                 json={"model": tts["model"], "voice": tts["voice"],
                                       "input": prompt, "response_format": "pcm"}) as response:
                response.raise_for_status()
                fixture = converted_pcm(await response.read())
                microphone = SyntheticMicrophone(fixture)
            peer.addTrack(microphone)
            channel = peer.createDataChannel("oai-events")

            @channel.on("message")
            def event(raw):
                value = json.loads(raw)
                kind = value.get("type")
                if kind == "session.created":
                    ready.set()
                if setup is not None:
                    setup.accept(value)
                    return
                if measurement is not None:
                    measurement.accept(value, time.monotonic())

            @peer.on("track")
            def track_received(track):
                async def drain():
                    nonlocal last_audible_at
                    resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
                    while True:
                        frame = await track.recv()
                        for converted in resampler.resample(frame):
                            pcm = bytes(converted.planes[0])[:converted.samples * 2]
                            if any(abs(v) > 40 for v in array("h", pcm)):
                                last_audible_at = time.monotonic()
                                if measurement is not None:
                                    measurement.accept_audio(last_audible_at)
                audio_tasks.append(asyncio.create_task(drain()))

            await peer.setLocalDescription(await peer.createOffer())
            form = FormData(default_to_multipart=True)
            form.add_field("sdp", peer.localDescription.sdp)
            form.add_field("session", json.dumps({
                "type": "realtime", "tools": [], "tool_choice": "none",
                "instructions": instructions,
            }))
            async with http.post(gateway + "/v1/realtime/calls", data=form, headers=headers) as response:
                if response.status != 201:
                    raise RuntimeError(f"Call setup returned HTTP {response.status}")
                location = response.headers.get("Location")
                await peer.setRemoteDescription(RTCSessionDescription(sdp=await response.text(), type="answer"))
            await asyncio.wait_for(ready.wait(), 15)
            if setup is not None:
                setup.send(channel)
                await setup.wait()
                setup = None
            await asyncio.sleep(1)  # Calibrate on synthetic RMS-150 room noise first.
            results, previous_responses = [], set()
            for index in range(turns):
                if time.monotonic() - last_audible_at < 0.5:
                    raise RuntimeError("Previous audio has not settled; discard timings")
                measurement = VoiceTurn(ignored_response_ids=previous_responses)
                microphone.begin_turn()
                await asyncio.wait_for(measurement.done.wait(), 90)
                results.append({"turn": index + 1, **measurement.result(
                    last_voice_at=microphone.last_voice_at,
                    fixture_complete=microphone.index == len(microphone.chunks),
                    reasoning=reasoning, show_answer=show_answer, expected_answer=expected_answer,
                )})
                previous_responses.update(
                    e["response"]["id"] for e in measurement.events
                    if e.get("type") == "response.done" and e.get("response", {}).get("id")
                )
                measurement = None
                if index + 1 < turns:
                    # Keep the connection/noise floor live; let receiver tail and
                    # gateway echo suppression settle before the next utterance.
                    await asyncio.sleep(1)
            identity = {"call_id": location.rsplit("/", 1)[-1] if location else None,
                        "fixture_sha256": hashlib.sha256(fixture).hexdigest()}
            return {**identity, **results[0]} if turns == 1 else {**identity, "turns": results}
    finally:
        if microphone is not None:
            microphone.stop()
        await peer.close()
        for task in audio_tasks:
            task.cancel()
        await asyncio.gather(*audio_tasks, return_exceptions=True)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gateway", default="http://192.168.2.6:8092")
    parser.add_argument("--config-host", default="snappy")
    parser.add_argument("--profile", default="mira")
    parser.add_argument("--case", choices=PROMPTS, default="greeting",
                        help="Keep simple replies separate from calculation/tool round trips")
    parser.add_argument("--reasoning", choices=("profile", "medium", "low"), default="profile",
                        help="Optional override in this isolated call only; never changes profile config")
    parser.add_argument("--show-answer", action="store_true",
                        help="Include up to 500 characters of the fixture's answer for manual review")
    parser.add_argument("--turns", type=int, choices=range(1, 11), default=1,
                        help="Replay the same PCM fixture on one connection to compare cold and warm turns")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    credentials = profile_credentials(args.config_host, args.profile)
    result = asyncio.run(probe(args.gateway.rstrip("/"), credentials, **case_options(args.case),
                              reasoning=args.reasoning, show_answer=args.show_answer, turns=args.turns))
    print(json.dumps({"case": args.case, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
