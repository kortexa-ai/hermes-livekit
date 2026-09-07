# Voice-turn latency qualification

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/14

## Measurement contract

- Keep speech endpoint, transcript, first caption, audio-start event and first
  decoded non-silent RTP timestamps separate. Correlate them to one response.
- Compare the same complete input PCM and exact fixed answer. Reject split
  utterances, early endpointing, unmatched replies and changed fixture hashes.
- Separate cold and warm turns. Keep ASR/TTS models, room-noise fixture, prompt,
  tool configuration, reasoning effort and service tier fixed unless that
  specific variable is the declared subject of the comparison.
- First-text hook observations include dispatch delay. Missing native provider
  timestamps stay unknown; neither observation is proof of pure model prefill.
- An isolated model comparison may use the native
  `/model MODEL --provider openai-codex --session` command. Require confirmation
  of the exact model, provider and session-only scope before measured speech.
  Confirm the serving model from the matching API metrics afterward. Do not
  switch the live kiosk session or persist a model change in the profile.
- Use serial receive-only calls with synthetic input. Never open the physical
  microphone or output device. Close peers and media tasks on every exit.

## Qualification and delivery

Use `tools/voice_latency_probe.py` for the real streaming voice path. Typed
tool canaries exercise a different TTS policy and cannot substitute for it.
Record exact runtime revisions, profile hash, fixture hash, call/response IDs,
stage timings and limitations on the issue. Check service health and unchanged
profile configuration after experiments; no restart is needed for diagnostics.

A faster canned answer only identifies a candidate. A production model change
also needs representative voice, reasoning and tool-use quality checks and an
explicit deployment decision. Physical onset clipping and listening comfort
remain separate from decoded-RTP correctness and synthetic latency checks.
