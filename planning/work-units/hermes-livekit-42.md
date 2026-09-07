# Streaming voice onset

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/42

Make initial generated near-silence removable at the conversation gateway,
without changing the provider's raw output or other clients. The setting is
per transport and opt-in. Keep 80 ms of lead-in, stop trimming at any sample
above PCM16 peak 16, and scan at most the first second. Never trim later pauses
or later clauses. Buffer at most four fixed 20 ms output frames.

Validate exact sample preservation after onset, immediate/quiet speech,
all-quiet output, arbitrary provider boundaries, cancellation and fallback,
and real RTP delivery. Use serial production probes with in-memory synthetic
audio. Compare identical PCM with and without trimming to separate this effect
from nondeterministic TTS generation and LLM latency.

Enable only for Mira and restart only its gateway after validation. Keep
deployment SHAs, rollback evidence, service state and timing samples in the
issue. This unit does not finish the larger voice-latency goal.
