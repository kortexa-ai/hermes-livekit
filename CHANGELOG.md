# Changelog

All notable changes to **hermes-livekit** are documented here, in the format
of [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] — 2026-05-15

### Added

- **Inbound data-channel protocol** (`hermes-control` topic, JSON
  `{"type": "client:<...>"}`). Single dispatcher routes by `type`; unknown
  types are ignored silently to stay friendly with apps that share the data
  channel for other traffic.
- **`client:capture-frame`** — sample the very next video frame from the
  sender's published video track, JPEG-encode (Pillow, quality 85), queue
  for attachment to the next `MessageEvent` (voice or text). The hermes
  vision pipeline picks up the file via `media_urls`. Frames not claimed by
  any message are cleaned up on disconnect.
- **`client:message`** — typed text from the client (skips STT). Pending
  captures attach automatically, same path as voice. Useful for IM-style
  fallback or accessibility.
- **`client:control`** — runtime hooks. `action: "pause"` / `"resume"`
  for explicit "mute me" UX. Placeholder for future controls.
- **Outbound `agent:frame-captured`** / **`agent:frame-capture-failed`** —
  acknowledge captures to clients so UIs can flash a "snapshot taken"
  indicator. Failure reasons: `no-video-track`, `timeout`, `encode-error`,
  `pillow-not-installed`.
- **Video track subscription** — `_on_track_subscribed` now branches on
  `KIND_VIDEO` and stores `rtc.VideoStream(track)`. Streams are NOT iterated
  eagerly; frames are only pulled on `client:capture-frame` trigger
  (sampling-on-demand, no continuous decoding cost).
- **`HERMES_LIVEKIT_LOG_LEVEL`** env var (`DEBUG` / `INFO` / `WARNING` / `ERROR`
  or numeric, default `INFO`). Lets users dial adapter verbosity without
  touching code. Mirrors hermes-agent's logging conventions so operational
  logs land in `gateway.log`.
- **Pillow** dependency, pinned to `==12.2.0` (2026-04-01 release, no yanks).
  Used to encode sampled frames.
- **LICENSE** (MIT), **CONTRIBUTING.md**, **CHANGELOG.md** (this file).
- README: full inbound/outbound protocol tables + sampling semantics.

### Changed

- Plugin default logger level is now `INFO` (was implicitly `WARNING` —
  Python's default for unconfigured loggers). All existing operational log
  lines should now be visible without further config.
- `_process_voice_input` drains pending captures into `MessageEvent`'s
  `media_urls` + `media_types`. Voice-only flow is unchanged when no
  captures are queued.

### Reserved (not yet implemented)

- `client:tool-register` / `client:tool-unregister` / `client:tool-result`
  message types — placeholder for future client-published tools (think:
  teleoperated hardware controlled by the agent over WebRTC). Don't ship
  clients that send these types; the schema may change.

## [0.1.0] — 2026-05-14

### Added

- Initial release. LiveKit WebRTC voice gateway plugin for hermes-agent.
- `LiveKitAdapter` — joins a LiveKit room as a participant, transcribes
  inbound audio via hermes's STT pipeline (qwen3-asr / OpenAI / whisper /
  faster-whisper, whichever hermes is configured for), publishes TTS replies
  back to the room.
- Presence-aware join: room empty → presence-watch poll, joins as soon as a
  real participant arrives, leaves when the last human leaves. Doesn't burn
  a participant slot on empty rooms.
- Capped exponential-backoff reconnect on disconnect.
- Outbound data-channel events (`agent:listening-start` / `-stop`,
  `agent:thinking-start`, `agent:speaking-start` / `-stop`,
  `agent:user-transcript`, `agent:agent-transcript`).
- Env-driven auto-configuration via `LIVEKIT_URL` / `LIVEKIT_API_KEY` /
  `LIVEKIT_API_SECRET` / `LIVEKIT_ROOM` / `LIVEKIT_AGENT_NAME` etc. —
  plugin auto-enables the platform when env is present.
- Hooks into hermes's `register_platform()` surface: `env_enablement_fn`,
  `is_connected`, `cron_deliver_env_var`, `allowed_users_env`,
  `allow_all_env`, `platform_hint`, `setup_fn`, plus a `hermes-livekit`
  toolset registration. Zero core hermes-agent edits required to install.
- Pinned dependencies: `livekit==1.1.7`, `livekit-api==1.1.0`.

[0.2.0]: https://github.com/kortexa-ai/hermes-livekit/releases/tag/v0.2.0
[0.1.0]: https://github.com/kortexa-ai/hermes-livekit/releases/tag/v0.1.0
