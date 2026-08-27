# hermes-livekit #28 — Conference base-protocol alignment

## Scope

Align the transport-neutral protocol used by Hermes Conference with the base
conversation contract shared by OpenAI Realtime and `api.server`, without
changing `hermes-agent`.

## Decisions

- Emit current `conversation.item.added` and `conversation.item.done` events.
- Emit response output-item and content-part lifecycle events around audio.
- Keep typed input and response cancellation transport-neutral.
- Let `response.create` without a new item enter the normal Hermes message
  pipeline through an adapter callback.
- Keep LiveKit admission, participant identity, RPC tools, and binary transfer
  as explicit Conference extensions rather than base-protocol forks.

## Validation

- Protocol tests cover audio, typed input, microphone-free response requests,
  cancellation, bounds, and event correlation.
- Conference tests cover the same lifecycle over targeted and broadcast
  LiveKit data packets.
