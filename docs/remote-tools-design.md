# Remote tools over LiveKit

## Status

Small JSON-shaped remote tools are implemented. Clients advertise tools over
the existing `hermes-control` data topic and serve calls through LiveKit native
RPC. Large or binary results remain future work.

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

The adapter validates the name and object schema, registers a late-bound Hermes
tool under `hermes-livekit-tools`, records the participant owner, and returns a
targeted `agent:tool-registered` acknowledgement.

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
| `client:tool-register` | Validate, register the Hermes tool, acknowledge |
| Hermes invokes the tool | Target the owner with native `perform_rpc` |
| `client:tool-unregister` | Verify ownership, deregister, acknowledge |
| Owner disconnects | Deregister all tools owned by that participant |
| Adapter reconnects or stops | Clear all client-owned tool registrations |
| Agent call is cancelled | The native RPC wait is abandoned with the coroutine |

## Safety and scope

- Trust on connect: any authorized room participant can offer a tool.
- Single-client assumption: collisions between different clients are not yet
  specified.
- Tool names match `^[a-zA-Z_][a-zA-Z0-9_]{0,63}$`.
- Input schemas must at least be objects with `type: object`.
- Tool activation is explicit. Operators add `hermes-livekit-tools` to
  `platform_toolsets.livekit`.
- Arguments and results are small JSON values. RPC payload limits are not a
  binary transport.

## Large and binary results

Future tools such as `camera.snapshot` need LiveKit byte streams in addition to
RPC. Invocation can stay on RPC, while the result carries a bounded stream
reference. Before implementation, resolve these questions:

1. Whether Hermes receives a `media_url`-shaped reference or a new result type.
2. Whether stream topics are per call or multiplexed.
3. How expected result size changes the timeout.
4. How cancellation closes the reader and removes partial data.

## Deferred

- Multi-client naming and collision behavior.
- Per-participant allowlists and explicit consent.
- Audit-log inspection.
- Full JSON Schema validation.
- Per-call result size caps below LiveKit's own payload bounds.
- Client-facing tool-list introspection.

The complete Python client example is `examples/test_client.py`.
