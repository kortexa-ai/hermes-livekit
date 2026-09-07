# Voice model timing

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/43

Use existing Hermes hooks to observe the first visible text and completed model
requests. Keep the feature opt-in and scoped to the plugin/profile. Keep local
logs numeric plus bounded identifiers: no prompts, replies, tool arguments,
reasoning or audio. Bound deduplication memory. Leave unknown provider timings
unknown. Observer callback time includes queue delay and is not model prefill.
Hook workers can run out of order; correlate identifiers rather than line order.

Record first PCM entering the adapter before resampling or onset trimming.
Keep greeting, factual and calculation probe cases separate. No model, prompt,
reasoning or tool-capability changes are part of this unit. Do not rebuild WPE.

Validate opt-in config through real Hermes imports in temporary profiles,
first-text deduplication and isolation, numeric sanitization, payload exclusion,
and unchanged audio under chunking/cancellation. Run the full plugin suite and
serial silent Pi canaries after deploying only Mira's gateway. Record delivery,
rollback, runtime health and measured stage evidence in the issue. This work
selects the next optimization; it does not complete the end-to-end latency goal.
