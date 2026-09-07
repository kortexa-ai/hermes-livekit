# Adaptive room noise and configurable endpointing

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/41

Expose per-platform `silence_duration` in seconds. Preserve the 1.5-second
default and validate the range 0.2–5.0; use a 0.7-second trial for Mira. The
direct path checks received frames and the conference path polls every 200 ms.

Keep the adaptive energy gate lightweight: no added model or dependency.
Calibrate from the quieter half of the initial sample window, permit louder
room baselines, and update with audio-time-based rise/fall constants. Learn
confident background frames during pauses but exclude detected speech and
near-threshold phonemes. Recalibrate when input is unmuted.

Validation covers configuration rejection, real call configuration propagation,
both endpoint paths, mid-sentence pause/reset, noise ramps, different room
levels, frozen speech learning, and frame-size-invariant adaptation. Endpoint
logs provide timing and noise-floor diagnostics without retaining audio.

Energy cannot distinguish all speech from music or sudden loud background
noise. Semantic endpointing and learned speech classification remain separate
future improvements; do not claim they are provided by RMS adaptation.

Coordinate the targeted Mira gateway deployment with streaming TTS issue #40.
Keep service evidence and measured latency in the issue, not this work note.
