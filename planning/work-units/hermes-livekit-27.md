# Native TTS delivery for Realtime transports

Issue: https://github.com/kortexa-ai/hermes-livekit/issues/27

## Decision

Both adapters implement the gateway's native `send_voice` contract by routing
the generated audio file through their existing transport playback method.
This keeps audio, transcript, and speaking lifecycle events on the same native
Realtime connection without changes to hermes-agent.

## Validation

- Adapter tests cover native voice dispatch for LiveKit Conference and direct
  WebRTC.
- The complete test suite passes.
- Both transports are exercised with the iOS client against the running
  gateway.
