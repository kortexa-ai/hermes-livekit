# Hermes LiveKit #21 — Realtime Conference transport

- Issue: https://github.com/kortexa-ai/hermes-livekit/issues/21
- Publish shared session and conversation events on reliable
  `conference.events` messages.
- Target `session.created` and correlated errors to the authoritative LiveKit
  participant identity; broadcast room lifecycle events.
- Keep audio on LiveKit tracks and keep native RPC, binary tools, and triggered
  camera capture as bounded Conference extensions.
- Reset protocol state when the adapter releases or replaces a room.
- Use one Hermes conversation session per Conference room while retaining
  participant identity for attribution and tool ownership.
- Do not require changes in `hermes-agent`.
