# Hermes LiveKit #18 — shared Realtime protocol core

- Issue: https://github.com/kortexa-ai/hermes-livekit/issues/18
- Keep session, conversation, response, transcript, cancellation, identifiers,
  limits, and correlated errors independent of WebRTC and LiveKit.
- Pass authoritative transport participant identity into inbound client events.
- Keep media, authentication, admission, Hermes message construction, and
  native LiveKit tool extensions outside the protocol core.
- Use the `api.server` direct and Conference event fixtures as the compatibility
  baseline.
