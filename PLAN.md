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
`register_byte_stream_handler` ship in the current `livekit==1.1.14` pin. Protocol shape
sketched in `docs/remote-tools-design.md` (`### Large tool results — design`).

Four open questions to resolve before coding (also in the design doc):

1. How hermes ingests binary tool results — `media_url`-style reference
   matching `client:capture-frame`, or a new return shape?
2. Topic naming — per-call vs single shared topic.
3. Timeout scaling — 30s default is short for multi-MB transfers; need
   a `metadata.expected_result_bytes` hint.
4. Cancellation mid-transfer — close the reader, drop the buffer.

This work uses RPC for invocation but still needs byte streams for the result
payload (RPC payloads are strings).

## Deferred indefinitely

Documented in `docs/remote-tools-design.md` "Deferred for future":

- Multi-client coexistence (per-identity prefix + opt-out)
- Tier-2 / Tier-3 safety (env allowlist, explicit consent, audit log)
- UX polish (`agent:tools-list`, bundled example client)
