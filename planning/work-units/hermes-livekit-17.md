# hermes-livekit #17 — dual-transport Realtime program

Issue: https://github.com/kortexa-ai/hermes-livekit/issues/17

## Compatibility decision

- OpenAI Realtime is the base conversation contract for direct WebRTC.
- Realtime Conference carries the same base contract over LiveKit and adds a
  separate room, participant, tool-routing, RPC, and binary-stream envelope.
- Backend selection must not require provider-specific conversation logic.
- Unsupported executable features fail explicitly with correlated bounded
  errors.
- No `hermes-agent` source change is required.

## Cross-repository work units

- `api.server#46`: current OpenAI direct-wire parity and real-API fixtures.
- `api.server#44`: direct-versus-Conference semantic parity.
- `api.server#45`: local LiveKit and LiveKit Cloud qualification.
- `hermes-livekit#22`: direct and Conference function tools.
- `hermes-livekit#23`: four-backend conformance and release evidence.
- `hermes-livekit#28`: Conference admission and base-protocol parity.
- `confcall.desktop#8`: one direct client and one Conference client.

## Constraints

- The active local LLM is `lfm2.5-8b-a1b` on the 4090 while the 6000 is
  reserved for LegoLM experiments.
- OpenAI and LiveKit Cloud credentials are read only from ignored local
  environment files and never copied into commands, fixtures, logs, issues, or
  repository files.
- Live evidence is redacted and bounded. Deterministic checked-in fixtures
  contain protocol shape, not user content, SDP, tokens, or media.
