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

## Deferred indefinitely

Documented in `docs/remote-tools-design.md` "Deferred for future":

- UX polish (`agent:tools-list`, bundled example client)
