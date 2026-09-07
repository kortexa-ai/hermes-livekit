# Warm-session voice latency

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/45

Measure repeated voice turns over one isolated WebRTC connection. Synthesize
the chosen fixture once, retain identical PCM, and preserve the media clock
between utterances. Keep single-turn output compatible. Bound repeat counts
to 1–10 and keep reasoning overrides optional and session-only.

Each utterance owns its event times, identified audio reply and completion.
Ignore identified responses from earlier turns. Reject split, incomplete or
premature fixtures, early/residual audio, missing response identities, protocol
errors and multiple audio replies. Keep a settling interval outside the timed
turn. Expose call, response and fixture identities without credentials, prompts
or reasoning; bounded fixture-answer output remains opt-in.

Validate replayed PCM, continuous timestamps and measurement isolation with
real imported code. Use serial silent production canaries, correlate their
existing gateway metrics and retain failed measurements as failures. Compare
first and later turns, but do not infer pure prefill time or unchanged model
inputs: session history grows and repeat answers can differ. Keep Mira's
model, prompt, toolset and profile configuration unchanged. Diagnostic-only
delivery uses Git and needs no service restart or WPE build.
