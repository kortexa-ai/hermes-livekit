# hermes-livekit #36 — fixed-profile Realtime discovery

Issue: https://github.com/kortexa-ai/hermes-livekit/issues/36

## Scope

Each Direct Realtime listener advertises the one Hermes profile to which its
gateway process is already bound.

## Decisions

- Reuse the existing listener and bearer credential.
- Derive the immutable profile name from the process-scoped Hermes home.
- Return only the protocol version, profile identifier, and fixed call path.
- Do not enumerate sibling profiles or expose filesystem and credential data.

## Validation

- Listener tests cover authenticated named-profile discovery, unauthorized
  access, the default profile, and the exact bounded response shape.
