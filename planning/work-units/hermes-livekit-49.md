# First-caption to audio latency

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/49

Extend the silent voice probe with response-correlated native-caption timing
and a fixed reply that starts with a short sentence. Reject a different final
answer in the fixed case; do not mix measurements across unlike spoken outputs.

Recognize both incremental text and full-snapshot replacement frames. Ignore
empty frames, other responses and final-only setup notices. Leave caption
metrics absent when the transport does not send native text frames. These are
client receipt times, not raw provider prefill or inference measurements.

Test the real imported probe without network credentials, then run a bounded
silent production canary with synthetic input. Keep profile configuration,
model, toolset, microphone and physical playback unchanged. Deliver this
diagnostic through Git without restarting a runtime. Keep measurements and
the separate core-change tracking blocker in the owning issue.
