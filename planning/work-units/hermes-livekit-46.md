# Overlap ASR with endpoint silence

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/46

Add opt-in, bounded speculative transcription to direct WebRTC and LiveKit.
Start after a configurable quiet interval, without advancing the normal voice
endpoint or agent dispatch. Use the existing Hermes STT resolution and the
worker-owned temporary-file lifetime. Reuse a candidate only for the same
speech marker and an identical PCM prefix with an exclusively quiet tail.
Inspect received PCM as well as the polling VAD marker, including explicit
mute before the next poll. Resumed speech, changed input, empty results and
failed requests retain the final batch path. Discarding a candidate must not
cancel its worker or permit another speculative worker while it is in flight.

Keep the default disabled. Validate configuration bounds, immutable snapshots,
single consumption, quiet-tail trimming, resumption, explicit endpoint/mute,
cancellation, worker failure, and both real transport dispatch paths. Run the
plugin suite, then serial production canaries using the existing ASR model.
Measure endpoint-to-transcript separately from the complete audible response;
do not attribute variable model time to this optimization. Accuracy and extra
requests during thinking pauses remain explicit tradeoffs.

Deliver by Git and restart only the managed Mira gateway after enabling a
0.35-second prefetch with its unchanged 0.7-second endpoint. Keep model, prompt,
toolset, TTS, ASR services and custom WPE unchanged. Verify the fresh gateway,
health, exact deployed revision, candidate reuse and matched fixture transcripts.
Rollback uses a focused revert plus removal of the opt-in profile setting and
a targeted managed gateway restart. Keep evidence on the owning issue rather
than turning this static note into a progress ledger.
