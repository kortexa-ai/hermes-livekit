# Hermes LiveKit 29 — Full reconnect parity

Issue: https://github.com/kortexa-ai/hermes-livekit/issues/29

## Outcome

Keep a Hermes Conference room active across LiveKit signal and full reconnects
while still releasing a genuinely empty room promptly.

## Current execution

- Debounce the last-participant departure for a short reconnect grace period.
- Cancel the pending room leave when a participant returns.
- Cover both the genuine-empty and reconnect cases with deterministic tests.
- Validate reliable data and tool registration after a forced SDK reconnect on
  self-hosted LiveKit and LiveKit Cloud.

## Validation

- Run the focused room lifecycle tests and the full Hermes LiveKit suite.
- Run the shared Conference smoke client with a forced full reconnect.
- Record bounded evidence on the issue without tokens, SDP, audio, transcripts,
  or tool payloads.
