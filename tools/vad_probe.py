#!/usr/bin/env python3
"""Offline CPU/capture qualification with supplied speech and generated noise.

No microphone, playback, ASR, model API, or service is used. The speech file
must be mono PCM16 WAV. Install the plugin's vad extra and prepare its model.
"""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import statistics
import time
from types import SimpleNamespace
import wave

import numpy as np
from av import AudioFrame, AudioResampler

import hermes_livekit.realtime_webrtc as realtime
from hermes_livekit.realtime_webrtc import QueuedAudioTrack, RealtimeCall
from hermes_livekit.vad import configured_vad_factory


def read_speech(path):
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError("Speech fixture must be mono PCM16 WAV")
        data = wav.readframes(wav.getnframes())
        frame = AudioFrame(format="s16", layout="mono", samples=len(data) // 2)
        frame.sample_rate = wav.getframerate()
        frame.planes[0].update(data)
    resampler = AudioResampler(format="s16", layout="mono", rate=48000)
    pcm = b"".join(bytes(f.planes[0])[:f.samples * 2]
                   for f in resampler.resample(frame) + resampler.resample(None))
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32)


async def qualify(model, path):
    started = time.perf_counter()
    factory = configured_vad_factory({"vad_backend": "silero", "vad_model_path": str(model)})
    load_s = time.perf_counter() - started
    speech = read_speech(path)
    foreground = np.flatnonzero(np.abs(speech) >= 100)
    if not len(foreground):
        raise ValueError("Fixture has no measurable foreground speech")
    fixture_onset = float(foreground[0]) / 48000
    rows = []
    original_clock = realtime.time
    try:
        for kind in ("white", "hum"):
            for scale in (1.0, 0.25):
                rng = np.random.default_rng(1301)

                def noise(n, rms, shape):
                    values = (rng.normal(0, rms, n) if shape == "white" else
                              rms * np.sqrt(2) * np.sin(2 * np.pi * 100 * np.arange(n) / 48000))
                    return values.astype(np.float32)

                signal = np.concatenate((noise(48000 * 3, 150, "white"), noise(48000 * 10, 650, kind),
                    speech * scale + noise(len(speech), 650, kind), noise(48000 * 3, 650, kind)))
                pcm = np.clip(np.rint(signal), -32768, 32767).astype("<i2").tobytes()
                clock = [0.0]
                realtime.time = SimpleNamespace(monotonic=lambda: clock[0])
                events, captures, timings = [], [], []

                async def speech_started(identity):
                    events.append({"event": "start", "at": round(clock[0], 3)})

                async def speech_stopped(identity):
                    events.append({"event": "stop", "at": round(clock[0], 3)})
                    return "fixture"

                async def overflow(identity):
                    events.append({"event": "overflow", "at": round(clock[0], 3)})

                async def capture(call, data, **kwargs):
                    captures.append(len(data))

                output = QueuedAudioTrack()
                call = RealtimeCall(SimpleNamespace(process_voice=capture), "offline-vad", None, output,
                    SimpleNamespace(speech_started=speech_started, speech_stopped=speech_stopped,
                                    input_audio_overflow=overflow),
                    vad=factory(), vad_factory=factory, silence_duration=0.7)
                max_buffer = 0
                try:
                    for offset in range(0, len(pcm), 1920):
                        chunk = pcm[offset:offset + 1920].ljust(1920, b"\0")
                        clock[0] += 0.02
                        start = time.perf_counter_ns()
                        await call.accept_pcm(chunk)
                        timings.append((time.perf_counter_ns() - start) / 1000)
                        max_buffer = max(max_buffer, len(call.audio_buffer))
                    await asyncio.gather(*call.tasks)
                finally:
                    output.stop()
                starts = [e["at"] for e in events if e["event"] == "start"]
                stops = [e["at"] for e in events if e["event"] == "stop"]
                capture_start = stops[0] - captures[0] / 96000 if len(stops) == len(captures) == 1 else None
                preserves_onset = capture_start is not None and capture_start <= 13 + fixture_onset
                passed = (len(captures) == 1 and len(starts) == 1 and 13 <= starts[0] < 14.0
                          and preserves_onset)
                rows.append({"noise": kind, "speech_scale": scale, "passed": passed, "events": events,
                    "capture_bytes": captures, "max_buffer_bytes": max_buffer,
                    "capture_start_s": round(capture_start, 3) if capture_start is not None else None,
                    "preserves_fixture_onset": preserves_onset,
                    "frame_us_p50": round(statistics.median(timings), 2),
                    "frame_us_p95": round(sorted(timings)[int(len(timings) * .95)], 2)})
    finally:
        realtime.time = original_clock
    report = {"fixture_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "fixture_onset_s": round(fixture_onset, 4), "load_s": round(load_s, 4), "rows": rows}
    print(json.dumps(report))
    return all(row["passed"] for row in rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--speech-fixture", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(qualify(args.model, args.speech_fixture)) else 1)
