#!/usr/bin/env python3
"""Measure TTS onset and compare identical PCM before/after gateway trimming.

Uses the existing profile and production TTS service. No microphone capture,
speaker playback, model load, or credential logging. Timings do not include
ASR/LLM/RTP. Run serially with other latency probes.
"""

import argparse
from array import array
import asyncio
import importlib.util
import json
from pathlib import Path
import time

from aiohttp import ClientSession, ClientTimeout
from voice_latency_probe import converted_pcm, profile_credentials

# This pure-PCM helper needs no installed Hermes host. Avoid importing the
# plugin package, whose __init__ registers platform adapters in a Hermes env.
spec = importlib.util.spec_from_file_location(
    "audio_onset", Path(__file__).resolve().parents[1] / "hermes_livekit/audio_onset.py",
)
onset = importlib.util.module_from_spec(spec)
spec.loader.exec_module(onset)


def audible_offset(pcm, rate):
    return next((i / rate for i, v in enumerate(array("h", pcm)) if abs(v) > 40), None)


async def probe(tts, repeat):
    async with ClientSession(timeout=ClientTimeout(total=30)) as http:
        for trial in range(repeat):
            started = time.monotonic()
            first_body = first_signal = None
            pcm = bytearray()
            async with http.post(
                tts["base_url"].rstrip("/") + "/audio/speech",
                headers={"Authorization": "Bearer " + tts["api_key"]},
                json={"model": tts["model"], "voice": tts["voice"],
                      "input": "Two plus two equals four.", "response_format": "pcm"},
            ) as response:
                response.raise_for_status()
                async for chunk in response.content.iter_any():
                    if not chunk:
                        continue
                    if first_body is None:
                        first_body = time.monotonic() - started
                    previous = len(pcm) // 2 * 2
                    pcm.extend(chunk)
                    complete = len(pcm) // 2 * 2
                    if first_signal is None and audible_offset(pcm[previous:complete], 24000) is not None:
                        first_signal = time.monotonic() - started
            if len(pcm) % 2 or not pcm or first_signal is None:
                raise RuntimeError("TTS returned invalid or inaudible PCM")
            source = converted_pcm(bytes(pcm))
            frames = [source[i:i + onset.FRAME_BYTES].ljust(onset.FRAME_BYTES, b"\0")
                      for i in range(0, len(source), onset.FRAME_BYTES)]
            source = b"".join(frames)
            trimmer = onset.LeadingSilenceTrimmer()
            cpu_started = time.perf_counter()
            result = b"".join(list(trimmer.feed(frames)) + list(trimmer.finish()))
            cpu_ms = (time.perf_counter() - cpu_started) * 1000
            assert result == source[trimmer.trimmed_frames * onset.FRAME_BYTES:]
            print(json.dumps({
                "trial": trial + 1, "first_body_s": round(first_body, 3),
                "first_signal_body_s": round(first_signal, 3),
                "raw_onset_s": round(audible_offset(source, 48000), 3),
                "trimmed_onset_s": round(audible_offset(result, 48000), 3),
                "removed_s": trimmer.trimmed_frames * 0.02,
                "filter_cpu_ms": round(cpu_ms, 3), "retained_suffix_identical": True,
            }), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-host", default="snappy")
    parser.add_argument("--profile", default="mira")
    parser.add_argument("--repeat", type=int, choices=range(1, 11), default=3)
    args = parser.parse_args()
    credentials = profile_credentials(args.config_host, args.profile)
    asyncio.run(probe(credentials["tts"], args.repeat))


if __name__ == "__main__":
    main()
