# PLAN.md — execution status & roadmap

Operational state of the plugin and dependencies that don't show up in
`git log`. For protocol-level design, see
[`docs/remote-tools-design.md`](docs/remote-tools-design.md).

## Where we are

- **v0.3.0** (current) — protocol shipped: client-registered tools, agent
  invokes via `agent:tool-call` JSON over data channel, `client:tool-result`
  comes back. Single-client, trust-on-connect, small JSON-shaped results
  only. End-to-end verified with the agent (Avery) and `examples/test_client.py`.
- Plugin subscribes to `on_session_finalize` (live now) and
  `agent_loop_stopped` (no-op until the upstream PR below lands). Both
  call `LiveKitAdapter.cancel_pending_tool_calls_for_session_reset`.

## Pending upstream — required for `/stop` cancellation

The plugin's `agent_loop_stopped` subscription is wired but the core
doesn't fire that hook yet.

- **Issue:** https://github.com/NousResearch/hermes-agent/issues/27206
- **PR:** https://github.com/NousResearch/hermes-agent/pull/27208
- **Branch:** `kortexa/agent-loop-stopped-hook` on `kortexa-ai/hermes-agent`
- **What lands when merged:** `_invoke_hook("agent_loop_stopped", ...)`
  in `gateway/run.py::_interrupt_and_clear_session`. Our existing
  subscription starts firing automatically — no plugin change needed.
- **If it lingers:** when our `kortexa-ai/hermes-agent` fork rebases on
  upstream main, the branch stays in our fork as a long-lived patch.
  We can carry it locally indefinitely. The behaviour `/stop` is missing
  in upstream is a real gap, just not a blocker for v0.3.0.

## Next phases

### v0.4.0 — LiveKit native RPC pivot

Replace the hand-rolled JSON `agent:tool-call` / `client:tool-result`
correlation with `local_participant.register_rpc_method` (client side)
and `perform_rpc` (agent side). SDK handles correlation, timeouts,
errors natively. Keep JSON `client:tool-register` — RPC has no discovery
API.

Estimated scope: half a day plus regression testing. Net deletion of
~50 lines in the plugin (timeout/correlation/error logic gone), similar
on the test client. No backwards-compat needed since v0.3.0 has not
been published externally.

Worth doing before piling more protocol on top of the JSON layer. See
"Aside — LiveKit native RPC" in `docs/remote-tools-design.md`.

### v0.5.0 — Phase 1.5: large / binary tool results

Adds `camera.snapshot` (and future tools returning binary payloads) via
LiveKit byte streams. Mechanism confirmed: `stream_bytes` /
`register_byte_stream_handler` ship in `livekit==1.1.7`. Protocol shape
sketched in `docs/remote-tools-design.md` (`### Large tool results — design`).

Four open questions to resolve before coding (also in the design doc):

1. How hermes ingests binary tool results — `media_url`-style reference
   matching `client:capture-frame`, or a new return shape?
2. Topic naming — per-call vs single shared topic.
3. Timeout scaling — 30s default is short for multi-MB transfers; need
   a `metadata.expected_result_bytes` hint.
4. Cancellation mid-transfer — close the reader, drop the buffer.

If v0.4 lands first, this work uses RPC for invocation but still needs
byte streams for the result payload (RPC payloads are strings).

## Deferred indefinitely

Documented in `docs/remote-tools-design.md` "Deferred for future":

- Multi-client coexistence (per-identity prefix + opt-out)
- Tier-2 / Tier-3 safety (env allowlist, explicit consent, audit log)
- UX polish (`agent:tools-list`, bundled example client)
