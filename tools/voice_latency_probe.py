#!/usr/bin/env python3
"""Silent, isolated voice canary: synthetic speech -> gateway -> received RTP.

Run on the Pi with aiortc, aiohttp and av installed. Credentials are fetched
over SSH into memory; the probe never records the microphone or plays sound.
It creates one real Hermes voice turn, so it does use the configured services.
"""

from __future__ import annotations

import argparse
import asyncio
from array import array
from fractions import Fraction
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
PROMPTS = {
    "greeting": "Please greet me in one short sentence, Hermes.",
    "fact": "Please tell me the color of a clear daytime sky in one short sentence.",
    "calculation": PROMPT,
}


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


async def probe(gateway: str, credentials: dict, *, prompt: str = PROMPT,
                reasoning: str = "profile", show_answer: bool = False) -> dict:
    events, times, audio_tasks = [], {}, []
    audio_response_ids = set()
    ready, done = asyncio.Event(), asyncio.Event()
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
                microphone = SyntheticMicrophone(converted_pcm(await response.read()))
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
                events.append(value)
                times.setdefault(kind, time.monotonic())
                if kind == "output_audio_buffer.started":
                    audio_response_ids.add(value.get("response_id"))
                # First-contact notices (e.g. /sethome guidance) are separate
                # text-only responses, not completion of the voice answer.
                if kind == "error" or (kind == "response.done" and
                        value.get("response", {}).get("id") in audio_response_ids):
                    done.set()

            @peer.on("track")
            def track_received(track):
                async def drain():
                    resampler = AudioResampler(format="s16", layout="mono", rate=RATE)
                    while True:
                        frame = await track.recv()
                        for converted in resampler.resample(frame):
                            pcm = bytes(converted.planes[0])[:converted.samples * 2]
                            if any(abs(v) > 40 for v in array("h", pcm)):
                                times.setdefault("first_audible_rtp", time.monotonic())
                audio_tasks.append(asyncio.create_task(drain()))

            await peer.setLocalDescription(await peer.createOffer())
            form = FormData(default_to_multipart=True)
            form.add_field("sdp", peer.localDescription.sdp)
            form.add_field("session", json.dumps({
                "type": "realtime", "tools": [], "tool_choice": "none",
                "instructions": "This is a voice latency check. Reply with one short sentence and do not use tools.",
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
                times.clear()  # Do not mix command/setup timing with speech timing.
            await asyncio.sleep(1)  # Calibrate on synthetic RMS-150 room noise first.
            microphone.started = True
            await asyncio.wait_for(done.wait(), 90)
            if any(e.get("type") == "error" for e in events):
                raise RuntimeError("Gateway emitted a protocol error")
            completion = next(e for e in reversed(events) if e.get("type") == "response.done")
            if completion["response"]["status"] != "completed":
                raise RuntimeError("Gateway response did not complete successfully")
            if "first_audible_rtp" not in times or microphone.last_voice_at is None:
                raise RuntimeError("No synthesized response audio received: " + json.dumps({
                    "event_types": [e.get("type") for e in events],
                }))
            validate_single_turn(events, times, microphone.last_voice_at)
            if microphone.index < len(microphone.chunks):
                raise RuntimeError("Reply completed before the spoken fixture finished; discard timings")
            result = {"call_id": location.rsplit("/", 1)[-1] if location else None}
            for label, event_name in [
                ("endpoint", "input_audio_buffer.speech_stopped"),
                ("transcript", "conversation.item.input_audio_transcription.completed"),
                ("audio_event", "output_audio_buffer.started"),
                ("audible_rtp", "first_audible_rtp"),
            ]:
                if event_name in times:
                    result[label + "_after_speech_s"] = round(times[event_name] - microphone.last_voice_at, 3)
            result["response_count"] = sum(e.get("type") == "response.done" for e in events)
            result["audio_response_count"] = len(audio_response_ids)
            result["reasoning"] = reasoning
            answer = completion_text(completion["response"])
            result["answer_word_count"] = len(answer.split())
            if show_answer:
                result["answer"] = answer
            return result
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
    return parser.parse_args(argv)


def main():
    args = parse_args()
    credentials = profile_credentials(args.config_host, args.profile)
    result = asyncio.run(probe(args.gateway.rstrip("/"), credentials, prompt=PROMPTS[args.case],
                              reasoning=args.reasoning, show_answer=args.show_answer))
    print(json.dumps({"case": args.case, **result}, sort_keys=True))


if __name__ == "__main__":
    main()
