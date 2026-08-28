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
- Use `conference.tools.register` and its shared acknowledgement on
  `conference.tools`; an empty list replaces a participant's registrations.
- Remove `hermes-control` from the client path. Put optional camera and runtime
  controls on `conference.extensions`, while native RPC and byte streams stay
  negotiated transport capabilities.
- Return validated client RPC JSON as serialized text because current Hermes
  accepts normal tool results only as strings. Preserve the supported
  multimodal dictionary envelope for verified image byte streams.

## Validation

- Protocol tests cover audio, typed input, microphone-free response requests,
  cancellation, bounds, and event correlation.
- Conference tests cover the same lifecycle over targeted and broadcast
  LiveKit data packets.
- Live local and Cloud calls must prove model invocation, native RPC delivery,
  accepted result, and a successful post-tool response without recording tool
  arguments, results, or transcript content.
- On 2026-08-27, the current adapter completed that model-initiated round trip
  on both local LiveKit and LiveKit Cloud. The full suite passes 143 tests.
