# api.server #50 — Public Direct Realtime Hermes routing

## Scope

Make Hermes Direct safe to select behind the authenticated public
`POST /v1/realtime/calls` endpoint without changing the provider-neutral
OpenAI Realtime multipart or data-channel contract.

## Decisions

- Keep the Hermes listener private and authenticate API-to-Hermes setup with a
  dedicated service bearer token.
- Accept a server-signed, body-bound principal and public call ID from
  `api.server`; reject partial, stale, replayed, or invalid metadata.
- Derive an opaque per-principal Hermes identity rather than trusting a client
  identity or exposing the API principal in Hermes routing metadata.
- Let Hermes own post-signalling capacity and maximum-duration enforcement;
  `api.server` owns authentication, setup admission, timeout, and public errors.
- Configure Direct ICE servers independently through
  `HERMES_REALTIME_ICE_SERVERS` so STUN/TURN candidates can reach off-LAN
  clients. Media and `oai-events` remain client-to-Hermes end to end.

## Validation

- Signed identity, replay/body binding, legacy direct identity, bounded ICE
  parsing, and the API cross-language HMAC fixture are covered.
- Full repository suite passes: `156 passed`.
- A real Node API proxy completed two sequential aiortc negotiations against an
  ephemeral signed Hermes listener and received `session.created` on both.
- Production off-LAN/TURN validation remains pending.
