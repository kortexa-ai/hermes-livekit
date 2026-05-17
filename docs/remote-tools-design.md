# Remote tools over the LiveKit data channel — design plan

> **Status:** decisions resolved 2026-05-16. Ready to implement Phase 1.

---

## Preamble — for the future-self reading this cold

### What this is about

`hermes-livekit` already lets a client and a hermes-agent (Avery, in our case)
talk to each other over WebRTC: audio in both directions, video in (with
on-demand frame sampling triggered by `client:capture-frame`), text via the
data channel. The next step is the inverse: let a connected **client publish
tools that the agent can call** over the data channel.

The motivating use cases are physical-world / out-of-process actuators
the agent doesn't have any other way to touch:

- A robotic arm rig with its own control daemon. The web/desktop client
  talks to the daemon locally. It exposes a `robot.move_to(x, y, z)` tool to
  the agent via WebRTC. The agent says *"pick up the red cup"*, plans the
  motion, calls the tool, the client executes locally and reports back.
- A browser tab the user is in front of. Client exposes `browser.click`,
  `browser.read_dom`, `browser.fill_form`. Agent drives the page on the
  user's behalf with the user still in physical control.
- A 3D-printer queue manager. Smart-light controller. A custom audio
  workstation. Any local-only API that has a frontend the user is already
  running.

### Why through the LiveKit plugin and not, say, MCP

We already have:
- A bidirectional, low-latency channel between the agent and a specific
  client (the data channel)
- A presence model — the agent knows when this client is connected and
  reachable
- An identity model — every tool offer can be attributed to the client
  that registered it

MCP solves a different problem (long-lived process-to-process tool
servers). The use cases above are "tools that exist only while a human is
present in the room, on the device they're sitting in front of." Coupling
them to the LiveKit session is exactly what we want — disconnect means
the tools go away, no separate server lifecycle to manage.

### End goal

A user opens `voice-agent.desktop` (or a web client we build), the client
loads a configuration that includes some local-tool integrations
(robot.move_to, browser.click, whatever), joins the LiveKit room. The
agent, mid-conversation, can:

1. See the new tools in its tool list on its very next turn.
2. Invoke them through normal LLM tool-calling — same code path as any
   other hermes tool.
3. Receive the result and react to it.
4. Lose access cleanly when the client disconnects.

User experience: it should be indistinguishable from a regular agent tool
call, except the work happens on the user's machine instead of in the
agent's container.

---

## Where the codebase is right now (2026-05-15, plugin v0.2.1)

Already in place — DO NOT reimplement:

- **Data-channel dispatcher**: `hermes_livekit/adapter.py::_on_data_received`.
  Receives every inbound data packet on the room. Filters to topic
  `hermes-control`, JSON-decodes, dispatches on the `type` field via a
  `handlers` dict. Adding a new `client:tool-register` handler is one line.
- **Reserved type names**: `client:tool-register`, `client:tool-unregister`,
  `client:tool-result` are publicly documented in the README + CHANGELOG
  as "reserved, schema may change, do not bind clients to these yet."
  No conflicting protocol traffic exists.
- **Outbound publish via `_publish_agent_event`**: serializes JSON,
  publishes on the default (no topic) data channel, reliable. Currently
  always broadcast to the room — for tool calls we'll want a variant
  that targets a single participant via `destination_identities`.
- **Tool-result media drain**: `_drain_pending_captures()` shows the
  pattern for "things buffered between dispatches" — applies cleanly to
  tool calls in flight.

Hermes-agent core API surface we'll lean on:

- **`tools.registry.ToolRegistry.register(name, toolset, schema, handler,
  is_async=True, ...)`** at `tools/registry.py:234`. Runtime-callable
  with no plugin-load-time constraint. MCP already uses this for
  dynamic tool discovery — perfect prior art.
- **`tools.registry.ToolRegistry.deregister(name)`** at line 290. Drops
  a tool. Generation counter bumps; the agent re-fetches tool definitions
  on the next turn automatically.
- **`PluginContext.register_tool(...)`** at `hermes_cli/plugins.py:317`.
  Wraps the registry; we want the raw `tools.registry` for runtime
  register/deregister, not this plugin-time API.

### Note to future-me — start here

1. **Re-read this doc end-to-end.**
2. Skim `tools/registry.py:151-310` to refamiliarize with `ToolRegistry`
   shape — `ToolEntry` fields, the `_generation` counter, MCP-style
   nuke-and-repave usage.
3. Re-read `hermes_livekit/adapter.py::_on_data_received` so the dispatch
   pattern is fresh; this is where new tool message types plug in.
4. Start with **Phase 1** of the plan. The open questions have been
   resolved — see "Resolved decisions" below.

### Resolved decisions (2026-05-16)

- **Safety model**: trust-on-connect. Any room participant can register
  any tool. No allowlist in v1. (Allowlist + consent flows → deferred.)
- **Multi-client scoping**: single-client assumption. The plugin
  registers tools for whichever participant sent `client:tool-register`;
  if a second client tries to register the same name, that's undefined
  behaviour for v1. No identity prefixing. (Multi-client → deferred.)
- **Mid-call interruption**: cancel outstanding tool calls when the
  agent loop terminates (`/stop`, `/new`). Emit
  `agent:tool-call-cancelled` and resolve the pending future with a
  cancellation error.
- **First demo tools**:
  - `desktop.notify` — Phase 1. Small JSON ack, exercises the basic
    register/call/result path.
  - `camera.snapshot` — Phase 1.5. *Not* a wrapper over the existing
    `client:capture-frame` flow — a client can offer it even without
    publishing a video stream, to grab a single still on demand.
    Result is a large binary, so it depends on the large-result
    transport landing — see "Large tool results — design" below. Phase 1
    ships the protocol and `desktop.notify` only; large-result work
    follows once the core is stable.
- **Toolset activation**: explicit config. The plugin registers tools
  under toolset `hermes-livekit-tools`; the agent operator must add
  this toolset to the agent's active toolset list. The plugin does
  not auto-activate. Update hermes-agent config when writing the code.

---

## Protocol

All new messages live on the existing `hermes-control` topic (inbound)
or untopic'd default (outbound, same as `agent:*` lifecycle events).
JSON payloads. Single dispatcher, same pattern as v0.2.x.

### Inbound — client → agent

#### `client:tool-register`

Client offers a new tool. Schema is JSON Schema for the input
parameters, matching the shape hermes already uses for built-in tools
(see `model_tools.py::get_tool_definitions`).

```jsonc
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
  },
  "metadata": {                          // optional, free-form
    "max_execution_time_ms": 5000
  }
}
```

On receipt:

1. Validate `name` matches `^[a-zA-Z_][a-zA-Z0-9_]*$`.
2. Validate `input_schema` is at least a dict with `"type": "object"`.
   (Full JSON-Schema validation is overkill; reject the obvious garbage.)
3. Build a handler closure that proxies the call to *this* participant
   (see "Handler" section).
4. Call `tools.registry.register(name=..., toolset="hermes-livekit-tools",
   schema=..., handler=..., is_async=True, description=...)`.
5. Track ownership: `self._client_tools[participant_identity].add(name)`.
   (Single-client assumption means there's effectively one entry, but
   we still track per-identity so participant-disconnect cleanup is
   uniform.)
6. Emit `agent:tool-registered` back to the participant.

#### `client:tool-unregister`

```jsonc
{ "type": "client:tool-unregister", "name": "desktop_notify" }
```

On receipt: verify ownership, `tools.registry.deregister(name)`, drop
from `_client_tools[identity]`, emit `agent:tool-unregistered`.

#### `client:tool-result`

Response to an outstanding `agent:tool-call`. Required: `call_id`.
Exactly one of `result` / `error` must be present.

```jsonc
{
  "type": "client:tool-result",
  "call_id": "tc_abc123def456",
  "result": {                           // arbitrary JSON, returned to agent loop
    "shown": true
  }
}
// — or —
{
  "type": "client:tool-result",
  "call_id": "tc_abc123def456",
  "error": "notification permission denied"
}
```

On receipt: look up `self._pending_tool_calls[call_id]`, resolve the
future with the result/exception, pop the entry.

### Outbound — agent → client

#### `agent:tool-call`

Sent **only** to the participant who registered the tool, via
`publish_data(destination_identities=[owner_identity])`.

```jsonc
{
  "type": "agent:tool-call",
  "call_id": "tc_abc123def456",
  "name": "desktop_notify",
  "arguments": {"title": "Hi", "body": "from your agent"}
}
```

#### `agent:tool-registered` / `agent:tool-unregistered`

Ack/nack to the registering client. Payload includes the tool name and
a `success: bool` field. Failure cases for v1: `name-invalid`,
`schema-invalid`, `not-owned-by-you` (for unregister). (Collision
failures are deferred — see single-client assumption.)

#### `agent:tool-call-cancelled`

Sent when the agent loop terminates mid-call (e.g. user `/stop`). Client
SHOULD abort the in-progress work but isn't required to (the call_id is
already abandoned on the agent side; any late `client:tool-result` is
ignored).

#### `agent:tool-call-timeout`

Sent when the plugin times out waiting for a result. Same semantics as
cancelled from the client's perspective.

---

## Handler structure (the agent-side proxy closure)

When `client:tool-register` lands, we build a handler that hermes calls
when the LLM picks the tool:

```python
def _build_tool_handler(self, owner_identity: str, registered_name: str):
    """Return a coroutine fn that proxies a tool call to a connected client."""

    async def proxy(**kwargs):
        if owner_identity not in self._room.remote_participants:
            raise RuntimeError(
                f"client {owner_identity!r} who registered {registered_name!r} is gone"
            )
        call_id = f"tc_{uuid.uuid4().hex[:12]}"
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_tool_calls[call_id] = future
        try:
            await self._publish_to_identity(
                owner_identity,
                {
                    "type": "agent:tool-call",
                    "call_id": call_id,
                    "name": registered_name,
                    "arguments": kwargs,
                },
            )
            return await asyncio.wait_for(future, timeout=TOOL_CALL_TIMEOUT)
        except asyncio.TimeoutError:
            await self._publish_to_identity(
                owner_identity,
                {"type": "agent:tool-call-timeout", "call_id": call_id},
            )
            raise
        finally:
            self._pending_tool_calls.pop(call_id, None)

    return proxy
```

The handler is async (`is_async=True` when registering with hermes).
Hermes's tool dispatcher will await it. Result is returned to the
agent loop verbatim — JSON-serializable values pass through, errors
propagate via the registry's existing exception path.

`_publish_to_identity` is a new helper that wraps
`self._room.local_participant.publish_data(...)` with
`destination_identities=[identity]` so the call goes only to the owner.

---

## State the adapter has to track

```python
# In __init__:
self._client_tools: dict[str, set[str]] = {}      # identity → owned tool names
self._pending_tool_calls: dict[str, asyncio.Future] = {}
self._tool_owners: dict[str, str] = {}            # tool_name → owner_identity
                                                   # (inverse of _client_tools, fast lookup)
```

Constants:

```python
TOOL_CALL_TIMEOUT_DEFAULT = 30.0  # seconds
TOOL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
TOOLSET_NAME = "hermes-livekit-tools"
```

Configurable via env:

```
HERMES_LIVEKIT_TOOL_TIMEOUT_SEC    default: 30
```

---

## Lifecycle hooks

| Event | What happens |
|---|---|
| `client:tool-register` arrives | validate → register → emit `agent:tool-registered` |
| `client:tool-unregister` arrives | verify ownership → deregister → emit `agent:tool-unregistered` |
| `client:tool-result` arrives | resolve `_pending_tool_calls[call_id]` future |
| participant disconnects (`_on_participant_disconnected`) | drop all their tools via `tools.registry.deregister`, fail all their pending calls |
| `_join_room` re-entered (reconnect) | clear `_client_tools` and `_pending_tool_calls` — clients will re-register |
| `disconnect()` (full teardown) | nuke all tools the adapter ever registered |
| Agent loop `/stop` or `/new` | for every pending call, emit `agent:tool-call-cancelled` to the owner and resolve the future with a cancellation error; clear `_pending_tool_calls` |

---

## Safety / consent (v1)

**Trust on connect.** Any room participant can register any tool name
with any schema. No allowlist, no consent prompt, no audit log. This is
acceptable because:

- The LiveKit room is already an authenticated session (token-gated).
- v1 deployments are single-user dev/personal use.
- The registration set is bounded by what the client code chooses to
  offer — the client author is the implicit consent surface.

Tighter models (env allowlist, explicit consent, audit log) live in
"Deferred for future" and will land when we have a real multi-tenant
need.

---

## Multi-client coexistence (v1)

**Single-client assumption.** v1 expects exactly one client to register
tools at a time. The plugin does not attempt to disambiguate when a
second client offers a tool with a name already registered — behaviour
is undefined and we don't promise it'll be useful. No identity
prefixing, no collision-rejection logic, no last-write-wins arbitration.

If/when a real multi-client use case appears (multi-operator robot
rigs, shared dashboards), revisit the "Deferred for future" section's
multi-client subsection for the design we already sketched.

---

## Large tool results — design

`desktop.notify` returns a tiny JSON ack and fits in a single
data-channel message. `camera.snapshot` does not — a JPEG is hundreds
of KB to a few MB, past any reasonable single-message budget. We
need a separate transport for binary results.

**Scope decision**: Phase 1 ships protocol + `desktop.notify` only, on
the existing JSON-on-data-channel path. Large-result transport lands
in Phase 1.5 once the core register/call/result loop is stable in
production. `camera.snapshot` is the demo for that phase.

### Mechanism — LiveKit byte streams

The SDK we pin (`livekit==1.1.7`) ships
[byte streams](https://docs.livekit.io/home/client/data/byte-streams/)
natively. Confirmed in the installed source:

- Sender: `local_participant.stream_bytes(name=..., topic=..., destination_identities=[...])`
  returns a `ByteStreamWriter`. There's also `send_file(file_path, topic=...)`
  for the common case. Chunking and backpressure are handled by the SDK.
- Receiver: `room.register_byte_stream_handler(topic, handler)` —
  handler receives a `(ByteStreamReader, participant_identity)` pair.
- Text-stream counterparts (`stream_text` / `register_text_stream_handler`)
  exist for large text payloads.

Verified in `livekit/rtc/participant.py:630` (`stream_bytes`),
`livekit/rtc/participant.py:660` (`send_file`),
`livekit/rtc/room.py:576` (`register_byte_stream_handler`).

This kills the chunked-JSON-frames option as obsolete — we'd just be
re-implementing the SDK's transport on top of itself. Other options
(external staging, ad-hoc video tracks) stay rejected.

### Protocol shape — Phase 1.5 sketch

When a tool result is large, the client:

1. Computes the result and a stream topic specific to the call —
   e.g. `tool-result-<call_id>`.
2. Opens a byte stream to the agent participant with that topic and
   writes the payload.
3. Sends a normal `client:tool-result` JSON message with
   `result: {"_stream_topic": "tool-result-<call_id>", "mime": "image/jpeg"}`
   instead of inline data.

Plugin side:
- `register_byte_stream_handler("tool-result-*", ...)` at adapter
  start (or per-call registration — TBD).
- On byte-stream completion, read the buffer and resolve the pending
  future with `(bytes, mime)` (or a hermes-native media object — see
  open question 1 below).

Tools whose results are small still go through the JSON path
unchanged. The two modes coexist; the client picks per-call based on
size.

### Open questions for Phase 1.5

1. **How does hermes ingest binary tool results?** The registry's
   handler can return JSON-serializable values. Image bytes are
   neither JSON nor automatically routable to the vision pipeline.
   Need to confirm whether we return a `media_url`-shaped reference
   (matching how `client:capture-frame` already feeds the vision
   pipeline) or invent a new return shape.
2. **Topic naming**: per-call topic (cheap to register, easy to time
   out) vs single shared topic with envelope routing (fewer
   registrations, multiplexing logic ours). Per-call is the obvious
   default unless we find a reason not to.
3. **Timeout scaling**: the 30s default tool timeout is shorter than
   a multi-MB transfer can take on a constrained link. Probably want
   a per-tool `metadata.expected_result_bytes` hint so the plugin can
   extend the timeout.
4. **Cancellation**: if the agent loop dies mid-transfer, we close
   the byte-stream reader and drop the partial buffer.

### Aside — LiveKit native RPC

While checking byte streams, found that `livekit==1.1.7` also ships
RPC: `local_participant.register_rpc_method(name, handler)` +
`local_participant.perform_rpc(destination_identity, method, payload)`
with `RpcError` for failures. (See
`livekit/rtc/participant.py:314,357,411`.)

That maps almost exactly to our register/call/result/error semantics —
the client would register tools as RPC methods, the agent would
`perform_rpc` to call them, and request/response correlation, timeout,
and error propagation are SDK responsibilities. We'd delete most of
the JSON-message scaffolding.

Not pivoting v1 — the JSON-on-data-channel protocol is half-built
already (lifecycle events, `client:capture-frame`) and a pivot is
its own meaningful piece of work. **But this is worth a real
conversation before we commit to the JSON protocol long-term.** Logged
in "Deferred for future" as a v0.4+ protocol revisit candidate.

---

## Implementation phases

### Phase 1 — single-client, trust-on-connect (target: 0.3.0)

Files touched in `hermes-livekit`:
- `hermes_livekit/adapter.py` — new state members, new dispatcher entries,
  `_build_tool_handler`, `_publish_to_identity`, `_register_client_tool`,
  `_unregister_client_tool`, `_handle_tool_result`,
  `_cancel_pending_tool_calls`, participant-disconnect hook extension,
  agent-loop-stop hook extension.
- `hermes_livekit/__init__.py` — no changes expected.
- `README.md` — protocol section + TypeScript example client snippet.
- `CHANGELOG.md` — `[0.3.0]` section.
- `pyproject.toml` — version bump.

Companion changes in `hermes-agent` (separate repo, do as part of the
same effort):
- Wire `hermes-livekit-tools` into the agent's active toolset config so
  client-registered tools are visible to the LLM. The plugin does not
  auto-activate — see "Resolved decisions".

Demo tool to drive the e2e test (client-side, separate from the plugin):
- `desktop.notify(title, body)` — pops a macOS notification. Small
  JSON ack as result.

(`camera.snapshot` moves to Phase 1.5 once large-result transport
lands — see below.)

Cancellation semantics: when the agent loop terminates mid-call
(`/stop`, `/new`), the plugin emits `agent:tool-call-cancelled` to the
owner for every entry in `_pending_tool_calls`, resolves each future
with `asyncio.CancelledError`, and clears the dict. Hook into whatever
the agent core exposes for loop-termination (TBD — find the right
signal when writing the code).

Out of scope for Phase 1:
- Large / binary tool results (Phase 1.5 — see "Large tool results — design").
- Per-tool confirmation flows.
- A built-in example tool that doesn't require a separate client.
- Everything in "Deferred for future".

### Phase 1.5 — large tool results via byte streams (target: 0.3.1)

Files touched in `hermes-livekit`:
- `hermes_livekit/adapter.py` — register a byte-stream handler at
  start; teach `_handle_tool_result` to inspect for the
  `_stream_topic` envelope and await the buffered bytes from the
  stream handler before resolving the pending future; close-on-cancel
  cleanup.
- `README.md` — protocol addendum for the stream-result envelope.
- `CHANGELOG.md` — `[0.3.1]` section.

Demo tool: `camera.snapshot()` returns JPEG bytes from the client's
local camera (no published video track needed).

Resolve the four open questions in the "Large tool results — design"
section before coding.

---

## Test plan

Mirror the capture-frame e2e test we already did:

1. **Headless client** (Python script using the LiveKit SDK directly):
   - Joins `hermes-avery` with `can_publish_data=True, can_subscribe=True`
   - Registers a trivial tool: `current_time` (no args, returns ISO timestamp)
   - Listens for `agent:tool-call`, responds with `client:tool-result`
   - Logs everything to stdout
2. **Voice prompt** via lk publishing a `say`-generated "what time is it on
   the user's machine?"
3. **Verify**:
   - Gateway log shows tool registration with the unmodified name
   - Agent loop calls the tool (gateway log shows `Tool call: current_time`)
   - Client receives `agent:tool-call`, responds
   - Agent's reply mentions the timestamp the client provided
4. **Disconnect test**: kill the client mid-conversation, verify the
   tool gets deregistered, agent's next turn doesn't see it

---

## Deferred for future

Decisions and features intentionally pushed past v1. Revisit when the
constraints that justify them actually exist.

### Multi-client coexistence

When a real multi-client use case appears, the three options on the
table are:

| | Behavior | Pros | Cons |
|---|---|---|---|
| **Reject collisions** | second register fails with `agent:tool-registered {success: false, reason: "name-collision"}` | simple; matches `tools.registry`'s existing shadow-rejection | one client has to disconnect before another can take over |
| **Last-write-wins** | second register deregisters the first | simplest UX | first client silently loses its tool |
| **Per-identity prefix** | registered name becomes `lk_<sanitized-identity>_<tool>`; both visible to agent simultaneously | most flexible; multi-arm scenarios work | tool names get ugly; LLM may not pick the right one without help |

Leading candidate: per-identity prefix with a `metadata.bare_name: true`
opt-out for the single-client common case.

### Safety hardening (former Phase 2)

- Env allowlist `HERMES_LIVEKIT_TOOL_ALLOWED` (comma-list or regex)
  pinning the set of registerable tool names. Per-participant variant
  `HERMES_LIVEKIT_TOOL_ALLOWED_FROM`.
- Audit-log every tool call attempt (input args, owner, timing, result
  status) to a ring buffer inspectable via
  `hermes plugins inspect livekit` or similar.
- Real JSON-Schema validation of `input_schema` instead of the shallow
  "dict with `type: object`" check.
- Per-call result size cap.

### UX polish (former Phase 3)

- Outbound `agent:tools-list` on demand so clients can introspect what
  they have registered.
- A dummy-client example bundled in the repo
  (`examples/python-tool-client.py`) so developers can try this without
  writing a client first.

### Explicit consent (former Phase 4)

- `agent:tool-registration-pending` flow — registration arrives, agent
  emits a pending event, tool isn't callable until acked by the user
  (via the client or a separate admin channel).
- Persistent consent decisions in `~/.hermes/plugins/livekit/consent.json`
  so users don't get re-prompted every restart.

### Protocol pivot to LiveKit native RPC (v0.4+ candidate)

`livekit==1.1.7` ships `register_rpc_method` / `perform_rpc` which
maps almost 1:1 onto our register/call/result/error semantics with
SDK-handled correlation, timeouts, and error propagation. Pivoting
would delete most of the hand-rolled JSON-message scaffolding in this
design.

Cost: client-side rework, breaking change to the protocol we're about
to publish in v0.3.0, and we'd want migration cover for any
`client:capture-frame`-style flows that stay on JSON. Worth a real
design conversation before v0.4; do not silently pivot.

**On the "binds us to LiveKit" objection.** Any client that joins a
LiveKit room is already bound to LiveKit — the room uses LiveKit's
signaling protocol (over WebSocket) on top of WebRTC, not raw WebRTC.
A generic WebRTC client cannot dial into a LiveKit room. Even our
existing `publish_data(topic=..., destination_identities=...)` calls
use LiveKit-flavoured data-channel semantics on top of the data
packet envelope (`DataPacket` proto). So the choice between
JSON-on-data-channel and LiveKit RPC is "which LiveKit SDK API do we
sit on top of," not "do we keep the option of switching transports."
That option doesn't exist today anyway. The LiveKit SDKs cover the
platforms a real client cares about (JS/TS, Swift, Kotlin, Flutter,
Unity, Python, Go, Rust), so portability of *clients* is fine; what
isn't portable is *the room itself* off LiveKit infrastructure.
