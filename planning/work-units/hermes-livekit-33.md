# Work unit: hermes-livekit #33

## Outcome

Route Hermes Kanban completion wakes back into an active Mira voice session
without starting the response while the user is speaking.

## Design

- Reuse Hermes core's existing profile-owned `notify+wake` subscription and
  gateway notifier. No second task bus or cross-profile polling is added.
- Declare direct WebRTC push-capable while a call is active; its existing
  `send`, session source, transcript, and TTS paths provide delivery.
- Defer only internal wake events while VAD reports active input speech.
  Ordinary user messages preserve the base adapter's interruption behavior.
- Drop a deferred wake if its room/call closes before speech ends.

## Validation

- Adapter contract tests cover LiveKit Conference deferral.
- Direct WebRTC tests cover async-delivery capability and per-call deferral.
- Full plugin test suite must pass before merge.
