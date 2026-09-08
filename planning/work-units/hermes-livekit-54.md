# Attribute queued PCM onset

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/54

Observe the first PCM16 sample above absolute amplitude 40 in audio accepted
by the output sink, after resampling and optional leading-silence trimming.
Report its sample offset separately from the time that its frame was queued.
Use the existing response identifier; retain no audio or transcript content.

Scan at most one second of queued audio and stop after the first matching
sample. Keep the observer independent of trimming and of the adapter-owned
`audible` flag. A matching sample is not proof of speech or physical playback.
Do not change output bytes, pacing, queue capacity, cancellation, echo guards,
models, configuration, dependencies or WPE.

Verify exact offsets and bounded work, resampled/split input, trim on/off,
quiet prefixes, short silent streams and cancelled/stale handles on both
transports. Run the full plugin tests, then sync the exact reviewed commit
to Snappy and validate it before a targeted managed Mira gateway restart.
Use a focused revert to the recorded pre-change revision if verification fails.
Correlate a silent synthetic voice canary with gateway logs; keep measured
results, deployment receipts and physical playback qualification in the issues.
