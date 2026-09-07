# Realtime source review

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/39

Review the plugin's transport lifecycle, audio and video paths, protocol state,
and remote tools. Apply focused correctness and latency fixes and document
remaining opportunities with their test requirements. Scope excludes
`vision.server`.

Franci initially deferred execution on 2026-09-06 because both machines were
busy. He subsequently authorized validating and committing this baseline
before streaming TTS. The full existing-environment suite passed on Snappy:
241 tests, without dependency updates. The stale-room registration fixture
now covers rejection both at receipt and after task scheduling.

The [review note](../../docs/code-review.md) records findings, changes, remaining
limitations, and the verification sequence. Changes cover media lifetime and
queue bounds, response completion, request ordering, setup cleanup, and tool
event-loop ownership. Existing unit suites were extended and shared media
worker tests were added. No performance claim can be treated as measured.
