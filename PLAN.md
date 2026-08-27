# PLAN.md — execution status & roadmap

Operational state of the plugin and dependencies that don't show up in
`git log`. For protocol-level design, see
[`docs/remote-tools-design.md`](docs/remote-tools-design.md).

## Where we are

- **v0.4.0** is the latest tagged release.
- Client tools use JSON `client:tool-register` / `client:tool-unregister`
  messages for discovery and LiveKit native RPC for invocation. The SDK owns
  correlation, response timeouts, and error transport.
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
The existing proprietary Hermes topics are temporary compatibility code, not a
fifth backend.

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
- Remaining direct-production gap: configurable ICE/TURN for calls that cross
  NAT. Loopback/LAN exercise on snappy does not require it.
- Remaining shared surfaces: complete cross-transport function tools in #22,
  and finish conformance fixtures, off-LAN smoke procedures, release docs, and
  packaging in #23. The voice/text/session/response/cancellation spine is
  complete and exercised in the shipped clients.

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

There are no third-party `hermes-livekit` clients to preserve, so this can be a
clean breaking replacement of `agent:*`, `client:*`, `hermes-chat`, and
`hermes-control`. Our applications will move to the shared contracts. A neutral
project rename can follow after the new interfaces stabilize.

- [#18](https://github.com/kortexa-ai/hermes-livekit/issues/18) — shared
  Realtime protocol and session core.
- [#19](https://github.com/kortexa-ai/hermes-livekit/issues/19) — Hermes bridge
  implemented only through public adapter hooks.
- [#20](https://github.com/kortexa-ai/hermes-livekit/issues/20) — direct OpenAI
  Realtime-compatible WebRTC service.
- [#21](https://github.com/kortexa-ai/hermes-livekit/issues/21) — Kortexa
  Realtime Conference-compatible LiveKit service.
- [#22](https://github.com/kortexa-ai/hermes-livekit/issues/22) — shared
  function tools plus bounded Conference extensions.
- [#23](https://github.com/kortexa-ai/hermes-livekit/issues/23) — four-backend
  conformance tests, examples, migration notes, and release documentation.
- [#24](https://github.com/kortexa-ai/hermes-livekit/issues/24) — neutral rename
  after the dual-transport gateway stabilizes.

## Deferred indefinitely

Documented in `docs/remote-tools-design.md` "Deferred for future":

- UX polish (`agent:tools-list`, bundled example client)
