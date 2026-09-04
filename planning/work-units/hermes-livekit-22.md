# hermes-livekit #22 — unified Realtime and Conference tools

## Scope

Close the remaining tool-call parity and safety gaps across Direct Realtime and
Realtime Conference without changing `hermes-agent`.

## Decisions

- Parse flat Direct definitions and nested Conference definitions into one
  bounded `FunctionToolDefinition` model and derive both Hermes registry
  schemas from it.
- Support `auto`, `none`, `required`, and a declared named function on Direct
  calls. Enforce `none` and named choices at invocation; express `required`
  through the existing per-turn Hermes prompt seam.
- Keep Direct model calls serialized per session. This avoids ambiguous
  continuation ordering while remaining deterministic when Hermes requests
  several client tools concurrently.
- Clear pending Direct call state on timeout, task cancellation, peer close,
  and adapter disconnect. Late outputs remain unknown and cannot satisfy a
  later invocation.
- Retain the existing Conference default-deny policy, stable participant
  ownership, native RPC invocation, fixed-field audit log, and bounded binary
  result extension.

## Validation

- Focused Direct, protocol, Conference, and WebRTC tests cover supported tool
  choices, malformed definitions, duplicate names and outputs, catalog/schema/
  argument/output limits, deterministic concurrent requests, timeout,
  cancellation, disconnect cleanup, late results, and owner isolation.
- The full repository suite and static checks must pass before delivery.
