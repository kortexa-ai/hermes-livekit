# PLAN.md — execution status & roadmap

Operational state of the plugin and dependencies that don't show up in
`git log`. For protocol-level design, see
[`docs/remote-tools-design.md`](docs/remote-tools-design.md).

## Where we are

- **v0.4.0** is the latest tagged release.
- Client tools use JSON `client:tool-register` / `client:tool-unregister`
  messages for discovery and LiveKit native RPC for invocation. The SDK owns
  correlation, response timeouts, and error transport.
- Remote tools remain single-client, trust-on-connect, and limited to small
  JSON-shaped arguments and results. `examples/test_client.py` implements the
  complete client contract.
- The former Hermes `/stop` hook dependency applied only to the removed custom
  pending-call table. Cancelling the calling coroutine now abandons the native
  RPC wait, so this plugin no longer needs session-reset hooks.

## Next phases

### Next minor release — Phase 1.5: large / binary tool results

Adds `camera.snapshot` (and future tools returning binary payloads) via
LiveKit byte streams. Mechanism confirmed: `stream_bytes` /
`register_byte_stream_handler` ship in the current `livekit==1.1.14` pin.

The bounded version 1 reference, identity binding, per-stream topic, size and
timeout limits, cancellation rules, Hermes image mapping, and non-image
fallback are defined in `docs/remote-tools-design.md` and the executable
`tool_result_protocol` fixtures. The adapter receiver and camera example remain
separate phases. RPC still carries invocation and the small stream reference;
the byte stream carries the payload.

## Deferred indefinitely

Documented in `docs/remote-tools-design.md` "Deferred for future":

- Multi-client coexistence (per-identity prefix + opt-out)
- Tier-2 / Tier-3 safety (env allowlist, explicit consent, audit log)
- UX polish (`agent:tools-list`, bundled example client)
