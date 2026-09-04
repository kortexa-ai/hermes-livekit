# PLAN.md — execution status & roadmap

Operational state of the plugin and dependencies that don't show up in
`git log`. For protocol-level design, see
[`docs/remote-tools-design.md`](docs/remote-tools-design.md).

## Where we are

- **v0.4.0** is the latest tagged release.
- Conference clients register one portable catalog with
  `conference.tools.register` on `conference.tools`. Small Hermes tool calls
  use LiveKit native RPC; bounded binary results use byte streams after the
  portable ready handshake. The SDK owns RPC correlation and timeouts.
- Remote tools are scoped by participant identity and operator policy.
  Multiple clients can advertise the same RPC method without replacing each
  other. Results can be small JSON values or bounded byte streams.
  `examples/test_client.py` implements the complete contract with
  `desktop_notify` and a deterministic `camera.snapshot` PNG fixture.
- Remote tools default to denied. A bounded operator policy binds exact
  participant/tool pairs to Tier 1, expiring Tier 2 consent, or always-denied
  Tier 3. A fixed-field in-memory audit ring records lifecycle outcomes without
  tool data or arbitrary diagnostics.
- Advertised method names use bounded case-sensitive dotted ASCII identifiers;
  policy and RPC keep the exact method while Hermes registry suffixes remain
  model-safe.
- The former Hermes `/stop` hook dependency applied only to the removed custom
  pending-call table. Cancelling the calling coroutine now abandons the native
  RPC wait, so this plugin no longer needs session-reset hooks.

## Next phases

### Phase 1.5: large / binary tool results

Adds `camera.snapshot` (and future tools returning binary payloads) via
LiveKit byte streams. Mechanism confirmed: `stream_bytes` /
`register_byte_stream_handler` ship in the current `livekit==1.1.14` pin.

The bounded version 1 reference, identity binding, per-stream topic, size and
timeout limits, cancellation rules, Hermes image mapping, and non-image
fallback are defined in `docs/remote-tools-design.md` and the executable
`tool_result_protocol` fixtures. The adapter implements the bounded receiver,
including exact owner/header checks, pending and byte caps, cancellation drain,
and room-generation cleanup. The example client registers `camera.snapshot`,
targets the invoking agent, and covers the ready/cancel/cleanup lifecycle with
a camera-free PNG fixture. RPC still carries invocation and the small stream
reference; the byte stream carries the payload.

## OpenAI Realtime support

The goal is to turn this project into a dual-transport realtime gateway for
Hermes without changing `hermes-agent`. The gateway will support the OpenAI
Realtime wire contract over direct WebRTC and the matching Kortexa Realtime
Conference contract over LiveKit. The umbrella tracker is
[#17](https://github.com/kortexa-ai/hermes-livekit/issues/17).

### Four-backend client matrix

`confcall.desktop` uses one conversation state machine with four connection
choices:

| Provider | Direct Realtime / WebRTC | Realtime Conference / LiveKit |
| --- | --- | --- |
| `api.server` | `POST /v1/realtime/calls` | `/v1/conference/calls` |
| Hermes gateway | OpenAI-compatible endpoint | Conference-compatible endpoint |

The client implementation and native four-mode validation landed through
[`confcall.desktop#5`](https://github.com/kortexa-ai/confcall.desktop/issues/5)
and
[`confcall.desktop#6`](https://github.com/kortexa-ai/confcall.desktop/issues/6).
The legacy `hermes-chat`, `hermes-control`, and `client:*` client topics have
been removed. Hermes-internal `agent:*` events stay behind the gateway bridge
and are not part of the client protocol.

### Interchangeability target

The compatibility boundary is the transport, not the backend. Direct clients
must be able to switch among OpenAI Realtime, `api.server` Realtime, and Hermes
Realtime by changing only endpoint, credentials, and provider selections.
Conference clients must be able to switch between `api.server` and the Hermes
gateway the same way.

The base contract is the current OpenAI Realtime session, conversation, item,
response, audio, cancellation, function-tool, error, and identifier model. We
implement the largest practical common subset that the local AgentPeer and
Hermes pipelines can execute. A valid but unsupported provider feature receives
a bounded correlated error; it is not silently accepted or translated into a
different behavior.

Realtime Conference carries that base contract over LiveKit. For a 1:1 call it
is primarily a transport upgrade. Its explicit extensions are:

- authenticated room admission and returned LiveKit connection credentials;
- authoritative participant identity and room membership;
- deterministic n:1 turn serialization, with a future path to n:n;
- participant-owned and trusted server tools;
- targeted LiveKit native RPC and bounded binary byte streams when negotiated;
- room history, reconnect, and last-member lifecycle.

Those extensions do not replace or rename the shared conversation events.
`conference.events` carries the base protocol and `conference.tools` carries
the portable Conference tool contract. Native RPC and binary streams are
optional capabilities layered on top.

The same Conference implementation must run without code changes against the
Kortexa self-hosted LiveKit deployment and LiveKit Cloud. URLs, keys, secrets,
and target selection remain secret-backed runtime configuration.

Current compatibility assessment:

- basic 1:1 spoken turns work in all four local backend cells, and real OpenAI
  Realtime is exercised as a fifth reference provider;
- one secret-safe typed-input WebRTC probe passes unchanged against OpenAI,
  `api.server`, and Hermes Direct. The broader differential suite now covers
  typed response, `session.update`, correlated invalid-event errors, idle
  cancellation, and the complete function-call/result/continuation loop;
- all five broader fixtures pass against real OpenAI and production
  `api.server` on LFM2.5 8B A1B. Each api.server setup returns HTTP 201 with a
  call Location. The same suite still needs to run against Hermes Direct;
- Direct setup now aligns on optional multipart session data, HTTP 201, and a
  call `Location`. Typed input, cancellation, and flat function tools use the
  normal local agent pipelines. Direct tools share the bounded internal
  definition model used by Conference, support the OpenAI choice modes and a
  declared named function, and complete the call/output/continuation loop.
  Audio equivalence and the remaining session fields remain;
- API Conference and Hermes Conference use one authenticated
  `/v1/conference/calls` setup message and one ConfCall LiveKit client. The
  server derives a stable user-namespaced participant identity, and exact
  default-deny Hermes tool registration is proven in the real desktop app;
- typed input, cancellation, base lifecycle events, audio, and late-agent tool
  registration share the Conference implementation. Session history/logging,
  broader tool invocation fixtures, and n:n semantics remain;
- self-hosted LiveKit and LiveKit Cloud are exercised end to end with the same
  Conference smoke client. API Conference and Hermes Conference both pass
  admission, `session.created`, reliable data, tool registration, forced full
  reconnect, spoken ASR → LFM 8B A1B → TTS turns, a second independent call,
  participant cleanup, and room cleanup on both targets. Model-initiated client
  tool invocation, returned result, and post-tool response now also pass in all
  four local/Cloud × API/Hermes Conference cells. Relay-only LiveKit Cloud runs
  pass for both Conference backends with a verified relay candidate. Direct
  WebRTC TURN/off-LAN qualification remains.

New work units created from this stricter definition:

- [`api.server#46`](https://github.com/kortexa-ai/api.server/issues/46) — current
  OpenAI Realtime WebRTC wire parity and real-API differential fixtures.
- [`api.server#44`](https://github.com/kortexa-ai/api.server/issues/44) — make
  Conference a transport adapter of the shared Realtime contract (completed).
- [#28](https://github.com/kortexa-ai/hermes-livekit/issues/28) — match the
  `api.server` Conference setup and portable base protocol.
- [`confcall.desktop#8`](https://github.com/kortexa-ai/confcall.desktop/issues/8)
  — reduce four backend choices to one direct client and one Conference client.
- [`api.server#45`](https://github.com/kortexa-ai/api.server/issues/45) — run the
  same Conference qualification against local LiveKit and LiveKit Cloud
  (completed).
- [`api.server#47`](https://github.com/kortexa-ai/api.server/issues/47) — stable
  authenticated Conference participant identity for exact tool policy
  (completed).
- [#29](https://github.com/kortexa-ai/hermes-livekit/issues/29) — preserve the
  Hermes room and portable session across LiveKit full reconnects (completed).

### Architecture

A shared `RealtimeSession` core will own protocol state, stable identifiers,
validation, ordering, limits, errors, and lifecycle. Two thin media edges will
connect it to direct WebRTC and LiveKit Conference sessions. A shared Hermes
bridge will translate protocol actions to the public platform-adapter surface:
`on_processing_start`, `on_processing_complete`,
`cancel_session_processing`, `send`, and `play_tts`. The plugin will continue
to own VAD, transcription, audio buffering, and transport cleanup. No
`hermes-agent` change is required.

The direct service will accept bounded authenticated multipart SDP and session
configuration at `POST /v1/realtime/calls`, return an SDP answer, exchange RTP
audio, and carry events on the `oai-events` data channel. It must define
ICE/TURN behavior, admission and setup timeouts, idle and maximum duration
limits, and deterministic cleanup. One direct call maps to one Hermes session.

Current implementation status (2026-08-27):

- Shared transport-neutral session protocol: landed in `realtime_protocol.py`.
- Conference/LiveKit edge: aligned on `conference.events`; one shared Hermes
  session per room; typed input and cancellation use public adapter hooks.
- Direct WebRTC edge: landed as the separately registered `realtime` platform.
  It serves `/v1/realtime/calls`, negotiates real aiortc peers, carries events
  on `oai-events`, consumes RTP audio through VAD/STT, queues TTS audio as RTP,
  and bounds concurrent and maximum-duration calls.
- Standard typed turns now queue `conversation.item.create` and begin exactly
  once on `response.create` on both transports.
- Hermes TTS now reaches native WebRTC and LiveKit audio output with matched
  `output_audio_buffer.started` / `stopped` lifecycle events.
- ConfCall Desktop completed native spoken-turn validation against all four
  provider/transport combinations. Hermes LiveKit iOS completed typed-turn,
  transcript, native audio, and idle-state validation against both Hermes
  transports; its signed build is installed on `francip-max`.
- The shared Conference smoke harness now selects API/Hermes, endpoint, room,
  stable client identity, local/Cloud target, full/tool/transport mode, and
  forced reconnect at runtime. Local and Cloud both pass the complete exercised
  matrix without a code change. The tool mode requires model invocation,
  accepted client result, and a post-tool response. Hermes full reconnects use
  a bounded empty-room grace so transient participant loss cannot abandon the
  active room; genuine empty rooms still release after the grace period.
- API Conference now accepts initial OpenAI-shaped `session.instructions` and
  `session.tool_choice`, routes typed input and cancellation through the shared
  Realtime agent, and keeps the base tool catalog client-owned like direct
  Realtime. Hermes native RPC results are serialized as validated JSON text for
  the current Hermes tool contract; verified image envelopes remain structured.
- Remaining direct-production gap: configurable ICE/TURN for calls that cross
  NAT. Loopback/LAN exercise on snappy does not require it.
- Remaining shared surfaces: run the broader differential suite against Hermes
  Direct under #23, extend parity to audio and the deliberately unsupported
  mutable session fields in `api.server#46` and #23, finish portable session
  history/logging coverage in #28, and finish
  direct TURN/off-LAN exercise, release docs, and packaging under
  `api.server#46` and #23. The clients are now interchangeable for the exercised
  setup, typed-turn, cancellation implementation, spoken-turn, transcript,
  reliable-data, reconnect, repeat-call, base lifecycle, and model-initiated
  client-tool paths. Session logging, direct forced relay, and audio
  differential fixtures are the main parity boundary.

The Conference service will match `api.server` authentication and connection
setup, including the returned LiveKit URL, token, room, and participant
identity. Reliable data topics will carry the shared event contract. The room
will have authoritative participant ownership, deterministic turn
serialization, and last-member cleanup. One active room maps to one shared
Hermes session.

### Protocol surface

Both transports will use the same session, conversation, item, response,
content, audio, transcript, buffer, cancellation, tool-call, error, and usage
model. IDs for events, sessions, items, responses, and calls must remain stable
across asynchronous updates. The initial implementation must cover:

- session creation and updates;
- text, image, and audio conversation input;
- VAD, transcription, response creation, streaming audio and transcripts;
- response cancellation and output-audio buffer clearing;
- function tools, tool results, structured errors, and usage events.

"Full support" means that an OpenAI Realtime WebRTC client can use every
feature Hermes can execute without a proprietary adapter. Provider-only
behavior that Hermes cannot honor must return a correlated explicit error; it
must never be silently ignored.

Direct sessions will use OpenAI's flat function-tool and function-result flow.
Conference sessions will match `api.server` participant tool registration,
invocation, and result routing. Existing LiveKit native RPC and bounded binary
byte streams remain supported Conference extensions, subject to the current
deny-by-default policy, participant ownership, size limits, timeouts, and
audit rules.

### Migration and work units

There are no third-party `hermes-livekit` clients to preserve. The breaking
replacement of client-facing `client:*`, `hermes-chat`, and `hermes-control`
topics is complete; applications use the shared Conference topics. Internal
`agent:*` bridge events are implementation details. A neutral project rename
can follow after the new interfaces stabilize.

- [#18](https://github.com/kortexa-ai/hermes-livekit/issues/18) — shared
  Realtime protocol and session core.
- [#19](https://github.com/kortexa-ai/hermes-livekit/issues/19) — Hermes bridge
  implemented only through public adapter hooks.
- [#20](https://github.com/kortexa-ai/hermes-livekit/issues/20) — direct OpenAI
  Realtime-compatible WebRTC service.
- [#21](https://github.com/kortexa-ai/hermes-livekit/issues/21) — Kortexa
  Realtime Conference-compatible LiveKit service.
- [#22](https://github.com/kortexa-ai/hermes-livekit/issues/22) — shared
  function tools plus bounded Conference extensions (completed).
- [#23](https://github.com/kortexa-ai/hermes-livekit/issues/23) — four-backend
  conformance tests, examples, migration notes, and release documentation.
- [#24](https://github.com/kortexa-ai/hermes-livekit/issues/24) — neutral rename
  after the dual-transport gateway stabilizes.

## Deferred indefinitely

Documented in `docs/remote-tools-design.md` "Deferred for future":

- UX polish (`agent:tools-list`, bundled example client)
