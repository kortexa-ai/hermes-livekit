# hermes-livekit #23 — Direct Realtime conformance tranche

## Scope

Make the Hermes direct WebRTC setup and base response request behavior
interchangeable with OpenAI Realtime clients without changing `hermes-agent`.

## Decisions

- Prefer OpenAI multipart setup with required `sdp` and optional `session`.
- Retain raw `application/sdp` as a compatibility input.
- Return `201 application/sdp` and a call `Location` header.
- Route a bare `response.create` through the normal Hermes message pipeline.
- Register OpenAI flat function tools in a Realtime-only Hermes toolset while
  each call is active. Keep the advertised client name on the wire and use a
  call-scoped internal registry name inside Hermes.
- Resolve Hermes's persisted session ID back to its gateway routing key before
  proxying a tool call. This keeps cross-session execution fail-closed without
  a `hermes-agent` change.
- Marshal Hermes worker-loop tool execution onto the WebRTC gateway loop and
  serialize calls per session. The client must return a matching
  `function_call_output` and then send `response.create` before Hermes resumes.
- Reject `tool_choice: required` explicitly because Hermes cannot enforce it
  without a core change. `auto` and `none` are supported.
- Accept that concurrent Direct sessions can see each other's transient tool
  schema metadata in Hermes's process-wide registry. Scoped names and the
  persisted-session ownership check prevent cross-session execution.

## Validation

- A real in-process aiortc negotiation covers multipart setup without an
  explicit session, SDP answer application, `session.created`, status,
  location, and bare response dispatch.
- The full repository suite passes: `152 passed`.
- A live LFM2.5 8B A1B WebRTC call completed the full client-tool lifecycle:
  native Hermes tool call, `response.function_call_arguments.done`, matching
  client output acknowledgement, follow-up `response.create`, and two
  completed `response.done` phases.
- The shared differential fixture's looser prompt ("with value ready") is not
  reliable on the local LFM model: it sometimes asks for the value instead of
  calling the tool. An explicit JSON-argument prompt passes on the same wire
  path; this is model interpretation, not a proxy or protocol failure.
