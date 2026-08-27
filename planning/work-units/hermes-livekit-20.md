# Hermes LiveKit #20 — direct OpenAI-compatible WebRTC

- Issue: https://github.com/kortexa-ai/hermes-livekit/issues/20
- Register a second `realtime` Hermes platform without modifying
  `hermes-agent`.
- Serve bounded SDP signalling at `POST /v1/realtime/calls` and the unversioned
  compatibility alias.
- Use RTP for inbound microphone and outbound TTS audio, with `oai-events` for
  the shared Realtime protocol.
- Keep one Hermes session per call, and clean up peer, protocol, queued audio,
  tasks, and session processing on close or cancellation.
- Require Bearer authentication for non-loopback listeners; bound active calls
  and maximum call duration.
- Verify an actual aiortc offer/answer plus `session.created`, not only mocked
  signalling.
- Defer configurable TURN to the production-hardening follow-up; host
  candidates cover the snappy loopback/LAN exercise target.
