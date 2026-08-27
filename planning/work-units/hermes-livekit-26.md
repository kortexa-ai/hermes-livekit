# Realtime typed-turn sequencing

Issue: https://github.com/kortexa-ai/hermes-livekit/issues/26

## Decision

`conversation.item.create` records and acknowledges a typed user item. It does
not start Hermes inference. The next `response.create` consumes that queued
input and dispatches exactly one Hermes turn. This matches the OpenAI Realtime
client sequence on both `oai-events` and `conference.events`.

## Validation

- Protocol tests cover deferred dispatch, the standard two-event sequence,
  cancellation, and rejection of `response.create` without new input.
- Direct WebRTC and LiveKit Conference typed turns are exercised with the iOS
  client against the running gateway.
