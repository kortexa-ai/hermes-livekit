# hermes-livekit #23 — Direct Realtime conformance tranche

## Scope

Make the Hermes direct WebRTC setup and base response request behavior
interchangeable with OpenAI Realtime clients without changing `hermes-agent`.

## Decisions

- Prefer OpenAI multipart setup with required `sdp` and optional `session`.
- Retain raw `application/sdp` as a compatibility input.
- Return `201 application/sdp` and a call `Location` header.
- Route a bare `response.create` through the normal Hermes message pipeline.

## Validation

- A real in-process aiortc negotiation covers multipart setup without an
  explicit session, SDP answer application, `session.created`, status,
  location, and bare response dispatch.
