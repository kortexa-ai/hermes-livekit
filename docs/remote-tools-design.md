# Remote tools over LiveKit

## Status

Small JSON-shaped and bounded binary-result remote tools are implemented.
Clients advertise tools over the existing `hermes-control` data topic and
serve calls through LiveKit native RPC; binary payloads use targeted LiveKit
byte streams after the RPC returns their reference.

## Goal

A connected client can expose a local capability—such as a desktop
notification, browser action, or hardware command—to the Hermes tool registry.
The tool exists only while that client is present in the LiveKit room.

This transport fits the lifecycle: the room already supplies connectivity,
participant identity, and a bidirectional low-latency channel. It is not a
replacement for MCP, which covers long-lived process-to-process tool servers.

## Current contract

### Discovery

Discovery remains a reliable JSON message on topic `hermes-control`, because
LiveKit RPC has no discovery API.

The client first registers an RPC method, then sends:

```json
{
  "type": "client:tool-register",
  "name": "desktop_notify",
  "description": "Show a desktop notification to the user.",
  "input_schema": {
    "type": "object",
    "properties": {
      "title": {"type": "string"},
      "body": {"type": "string"}
    },
    "required": ["title", "body"]
  }
}
```

The adapter validates the participant identity, name, and object schema. It
registers a late-bound Hermes tool under `hermes-livekit-tools`, records the
participant owner, and returns a targeted `agent:tool-registered`
acknowledgement. The acknowledgement keeps the advertised name.

The model-visible Hermes registry name is participant-scoped. It has the form
`lk_<16 hex characters>_<advertised-name-prefix>`, where the digest covers the
length-prefixed UTF-8 participant identity and the full advertised name. The
readable suffix is truncated so the complete name is at most 64 characters.
The adapter checks its owner and method maps plus the existing Hermes registry
slot before mutation. A derived-name collision therefore fails closed instead
of replacing another registration.

Registration and unregistration tasks retain the room object and room
generation that received the control packet. Before registry mutation, the
adapter also requires that exact room to remain current and the sender to
remain present. A queued packet from a disconnected participant or replaced
room is ignored.

To remove the tool, the client sends:

```json
{"type": "client:tool-unregister", "name": "desktop_notify"}
```

The client also unregisters its local RPC method. Participant disconnect and
adapter teardown deregister the matching Hermes tools automatically.

### Invocation

The client's RPC method name is exactly the advertised tool name. The adapter
calls:

```python
result_payload = await room.local_participant.perform_rpc(
    destination_identity=owner_identity,
    method=registered_name,
    payload=json.dumps(arguments),
    response_timeout=configured_timeout,
)
result = json.loads(result_payload)
```

Arguments and results must be valid JSON. A client failure is a LiveKit
`RpcError`. LiveKit owns request IDs, response correlation, response timeout,
disconnect errors, and error transport. Cancelling the caller abandons its RPC
wait. There is no custom `agent:tool-call`, `client:tool-result`, or
pending-future table.

The default response timeout is 30 seconds. Operators can set
`HERMES_LIVEKIT_TOOL_TIMEOUT_SEC` to a positive number.

## Lifecycle

| Event | Result |
|---|---|
| `client:tool-register` | Validate, register a participant-scoped Hermes tool, acknowledge |
| Hermes invokes the tool | Target the owner with native `perform_rpc` |
| `client:tool-unregister` | Verify ownership, deregister, acknowledge |
| Owner disconnects | Deregister all tools owned by that participant |
| Adapter reconnects or stops | Clear all client-owned tool registrations |
| Agent call is cancelled | The native RPC wait is abandoned with the coroutine |

## Safety and scope

- Trust on connect: any authorized room participant can offer a tool.
- Different participants can advertise the same RPC method name. Each gets a
  distinct model-visible registry name and only that participant's handler.
- Participant identities must be non-empty, have no leading/trailing
  whitespace or Unicode control, format, or surrogate characters, and fit
  within 128 UTF-8 bytes.
- Tool names match `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$` exactly; the adapter does
  not trim or normalize them.
- Input schemas must at least be objects with `type: object`.
- Tool activation is explicit. Operators add `hermes-livekit-tools` to
  `platform_toolsets.livekit`.
- Arguments and results are small JSON values. RPC payload limits are not a
  binary transport.

## Large and binary results

Protocol version 1 keeps invocation on RPC and transfers only the result bytes
over a LiveKit byte stream. The RPC response is this exact JSON object; unknown,
missing, duplicate, or incorrectly typed fields are invalid:

```json
{
  "type": "livekit-byte-stream",
  "version": 1,
  "owner_identity": "camera-client",
  "stream_id": "0123456789abcdef0123456789abcdef",
  "topic": "hermes-tool-result/0123456789abcdef0123456789abcdef",
  "mime_type": "image/jpeg",
  "expected_size": 245760,
  "text_summary": "Camera snapshot."
}
```

The stream ID is exactly 32 lowercase hexadecimal characters. Each result gets
its own topic, derived as `hermes-tool-result/<stream_id>`. The adapter accepts
the reference only when `owner_identity` is the participant that owns the RPC
tool. It accepts the byte stream only from that same participant and only when
the LiveKit stream ID, topic, MIME type, and declared total size exactly match
the RPC reference. After validation, the adapter atomically reserves the topic
across all outstanding calls in the room. An already reserved topic fails with
the fixed `stream_collision` code before handler registration or a ready
message, even when the references name different owners. The reservation is
released on every terminal path, after the handler is removed. This prevents a
stream from satisfying a different concurrent call by reusing its topic.

`expected_size` is an integer from 1 through 12 MiB. MIME types have no
parameters and must match the protocol's bounded token grammar. The supported
Hermes image types are JPEG, PNG, WebP, and GIF; other `image/*` types fail
closed. `text_summary` is required, non-blank, and at most 1,024 characters.
The complete executable definition is
`hermes_livekit/tool_result_protocol.py`.

The two transports have no cross-channel ordering guarantee, so the sender must
not open the stream when its RPC handler returns. After parsing the reference,
the adapter installs the exact-topic handler and sends this reliable, targeted
control message to the owner:

```json
{
  "type": "agent:tool-result-stream-ready",
  "stream_id": "0123456789abcdef0123456789abcdef",
  "topic": "hermes-tool-result/0123456789abcdef0123456789abcdef"
}
```

The owner starts `stream_bytes` only after receiving the matching ready
message. A ready message from another participant, or one whose ID/topic does
not match an outstanding result, has no effect. This handshake prevents a fast
sender from publishing before the exact handler exists.

The transfer timeout is the larger of the configured RPC timeout and a 15
second setup allowance plus the expected size at 256 KiB/s, capped at 120
seconds. Binary references therefore require a configured timeout in the range
`(0, 120]`. The RPC first completes with the reference. The adapter then
validates and reserves the topic, installs its exact-topic handler, starts the
transfer deadline, and publishes the targeted ready message. The same deadline
covers the ready publish and byte transfer. The handler and reservation are
removed on every terminal path.

Only an exact-length completed transfer becomes a tool result. Short, overlong,
timed-out, disconnected, or cancelled transfers discard all partial bytes and
produce one fixed failure code: `transfer_incomplete`, `transfer_timeout`,
`owner_disconnected`, or `transfer_cancelled`. The receiver accounts for each
chunk before appending it and never buffers beyond `expected_size`; an overlong
chunk starts terminal handling without appending that chunk.

LiveKit Python 1.1.14 has no reader close or cancel operation. Unregistering a
topic handler affects only future streams; it does not remove an accepted
reader. Therefore local timeout, disconnect, cancellation, or overrun sends a
best-effort targeted `agent:tool-result-stream-cancel` message, stops appending,
and actively consumes and discards subsequent chunks. The sender should stop
and finish its stream on that matching message. If its trailer does not arrive
within the fixed 5-second drain deadline, the adapter locally disconnects and
abandons that entire `Room` generation, then joins with a new `Room` instance.
Dropping the old instance abandons the pinned SDK's otherwise unreachable
reader and queue; it does not evict or ban the remote participant.

Room replacement is coalesced at most once per generation. It fails every
outstanding call from that generation with the fixed `room_replaced` code and
clears all old handlers, topic reservations, and participant-owned tool
registrations. Clients must rediscover and register their tools after the new
room joins. Drain deadlines carry their room generation: a stale deadline, or
another deadline after replacement has started, is a no-op against the new
room. A trailer within the deadline releases the handler and topic reservation
normally. A persistent noncooperative sender can rejoin and cause repeated
whole-room availability disruption, but each old generation remains memory
bounded. No partial payload or protocol-supplied text is included in
diagnostics.

Supported images map to Hermes Agent's multimodal tool-result shape: bounded
summary text plus an `image_url` part containing a data URL. This 12 MiB decoded
cap remains below Hermes Agent's 20 MiB base64-data limit. For non-image MIME
types, version 1 returns only the bounded summary and `{mime_type, size,
available_to_model: false}` metadata after validating the bytes; the payload is
discarded because Hermes has no general binary attachment result shape.

The adapter detects this reference after the normal RPC completes. It reserves
at most eight binary results at once, registers one exact-topic byte handler,
and sends the ready message. Ordinary JSON RPC results keep their existing
path. The receiver validates the sender and every advertised header field
before buffering, accounts before every append, applies one deadline to ready
publication and transfer, and maps only exact-length completed bytes to the
result shapes above. A stream from another participant is drained separately
and cannot claim or fail the owner's pending result. At most four such ignored
readers are drained concurrently per room generation; another ignored header
coalesces the same bounded room-replacement escalation instead of creating an
unbounded task or queue set.

Participant departure fails that owner's pending binary results and clears
their partial buffers. Room disconnect or replacement fails every pending
result from the old generation, clears handlers and reservations, and requires
tool rediscovery after rejoin. Caller cancellation clears its result buffer and
waiter immediately, sends the targeted cancel message, and applies the bounded
drain behavior above to any accepted reader. Diagnostics expose fixed protocol
codes, not participant-supplied fields or payload bytes.

### `camera.snapshot` example

`examples/test_client.py` registers `camera.snapshot` through the same discovery
and RPC path as `desktop_notify`. Its RPC handler accepts an empty object,
reserves at most eight pending snapshots, and returns the version 1 reference.
The example source is a fixed 1x1 PNG so tests need no camera.

The client binds each pending snapshot to `RpcInvocationData.caller_identity`.
Only an exact ready or cancel message on `hermes-control` from that participant
can act on it. On ready, `stream_bytes` declares the exact PNG size, MIME, ID,
and topic and sets `destination_identities` to only that caller. Success closes
the writer trailer. Cancellation closes it with `transfer_cancelled`. Oversize,
empty, malformed, and over-cap requests fail before pending or stream state is
created. A reference not claimed within 120 seconds expires, and every terminal
path clears its fixture bytes and tasks.
Ready publication cancels the reference-expiry timer; the stream writer then
has its own fixed 120-second deadline and closes with `transfer_timeout` if it
does not finish. Writer close is started at most once. If cancellation or the
deadline arrives while the pinned SDK is sending its trailer, the client
shields and awaits that same close task; it never retries `aclose()` after the
SDK has marked the writer closed. The first close reason is immutable: if the
normal trailer was already in flight, a later cancel or timeout does not rewrite
it. Close has a separate fixed 5-second deadline. If the trailer call still has
not returned, the example detaches and observes that single task, clears the
pending slot and payload, and performs a bounded local room disconnect so
unregister and shutdown cannot wait forever.

## Deferred

- Per-participant allowlists and explicit consent.
- Audit-log inspection.
- Full JSON Schema validation.
- General non-image attachment delivery to the model.
- Client-facing tool-list introspection.

The complete Python client example is `examples/test_client.py`.
