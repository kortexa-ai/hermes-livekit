# Speech discrimination and bounded capture

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/13

Keep RMS VAD available without additional dependencies. Offer an explicit
`silero` backend with a validated confidence threshold, pinned offline model,
one CPU inference thread, a shared read-only inference session and independent
resampling/recurrent state per input. Do not use a GPU or download at runtime.
Retain the existing silence timeout and ASR-prefetch trigger.

Keep 500 ms of input pre-roll so detection latency does not lose word onsets.
Bound continuous capture at 120 seconds. On overflow, discard the entire
unsubmitted utterance, emit a recoverable input error, and suppress capture
until a full quiet interval or an explicit mute/unmute. Never transcribe a
truncated command or repeatedly submit stationary noise.

Validate both transports, independent streams, arbitrary frame boundaries,
quiet speech, noise steps, long input, mute/reset/disconnect, and recovery.
Use the existing synthetic speech fixture and generated noise without room
recording or physical playback. Keep model weights out of Git. Deliver through
Git and use a targeted Mira deployment with predeclared rollback, preserving
the custom WPE build. Real-room qualification remains distinct from fixtures.
