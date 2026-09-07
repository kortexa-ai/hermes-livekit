# Streaming TTS transport

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/40

Connect Hermes Agent's streaming PCM contract to the direct WebRTC track and
the LiveKit audio source. The TTS provider is unchanged. For Mira it runs on
Smarty at `192.168.2.3:4003`; the gateway runs on Snappy and the kiosk on the Pi.

The transport preserves resampler state across HTTP chunks, bounds queued
audio, and separates audio playback completion from response completion.
Cancellation must release backpressure and must not clear a newer response.
Whole-file fallback is valid only before playable audio reaches the sink.

Validation includes arbitrary PCM boundaries, provider failures, cancellation,
stale handles, actual RTP reception, and an opt-in production-provider canary.
The gateway's consumer completion budget must allow normal long replies to
finish before production rollout. Deployment affects only the Mira gateway;
no WPE build, kiosk rebuild, ASR change, or TTS service restart is required.

Keep runtime measurements, delivery SHAs, and deployment evidence in the issue.
