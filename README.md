# hermes-livekit

LiveKit WebRTC voice gateway plugin for [hermes-agent](https://github.com/NousResearch/hermes-agent).

Lets a Hermes gateway join a LiveKit room as an agent participant, transcribe
participant speech via Hermes's STT pipeline, run the agent loop, and publish
TTS replies back to the room as audio.

## Requirements

- An existing `hermes-agent` install (this plugin attaches to it; it does not
  vendor hermes itself).
- `ffmpeg` on `PATH` — used to decode TTS audio for the WebRTC publish path.
  - macOS: `brew install ffmpeg`
  - Debian / Ubuntu: `sudo apt install ffmpeg`
- A reachable LiveKit server (LiveKit Cloud or self-hosted) with an API key /
  secret pair.

## Install

Install into the **same Python environment** as your `hermes-agent`:

```bash
pip install git+https://github.com/kortexa-ai/hermes-livekit.git
```

pip resolves the pinned `livekit` / `livekit-api` SDK versions automatically.
The plugin is auto-discovered through the `hermes_agent.plugins` entry-point
group — no edits to hermes-agent's source tree are required.

> Note: `hermes plugins install kortexa-ai/hermes-livekit` is **not** the
> right path for this plugin. That command `git clone`s into
> `~/.hermes/plugins/` without resolving pip deps; you'd then have to
> `pip install 'livekit==1.1.7' 'livekit-api==1.1.0'` by hand. The pip
> install above is one command and keeps the SDK pins in sync with the
> plugin version.

### Local / editable install

For development on a checkout (e.g. `~/src/hermes-livekit/`):

```bash
pip install -e ~/src/hermes-livekit
```

## Enable

After install, add `livekit` to the enabled-plugins list:

```bash
hermes plugins enable livekit
```

(Or edit `~/.hermes/config.yaml` and add `livekit` to `plugins.enabled`.)

Then enable the platform in the same config:

```yaml
platforms:
  livekit:
    enabled: true
plugins:
  enabled:
    - livekit
```

## Configure

Set these env vars (or supply equivalents under `platforms.livekit.extra`
in `~/.hermes/config.yaml`):

| Var                              | Required | Notes                                                              |
|----------------------------------|----------|--------------------------------------------------------------------|
| `LIVEKIT_URL`                    | yes      | `wss://your-project.livekit.cloud` or `wss://your-self-hosted/`    |
| `LIVEKIT_API_KEY`                | yes      | from your LiveKit project / server config                          |
| `LIVEKIT_API_SECRET`             | yes      | from your LiveKit project / server config                          |
| `LIVEKIT_ROOM`                   | no       | room the agent joins; default `hermes`                             |
| `LIVEKIT_AGENT_NAME`             | no       | display name; default `Hermes` (asks the LLM if unset)             |
| `LIVEKIT_AGENT_AVATAR`           | no       | avatar URL or local image path (encoded as data URI)               |
| `LIVEKIT_HOME_CHANNEL`           | no       | cron / cross-platform delivery target; defaults to `LIVEKIT_ROOM`  |
| `LIVEKIT_ALLOWED_USERS`          | no       | comma-separated participant identities                             |
| `LIVEKIT_ALLOW_ALL_USERS`        | no       | `1`/`true` allows any participant (dev only)                       |
| `LIVEKIT_PRESENCE_POLL_INTERVAL` | no       | seconds; auto-picked (cloud 30s, local 5s)                         |

Or run the interactive prompt:

```bash
hermes config
```

## Verify

```bash
hermes gateway restart
hermes gateway status      # should show 🎙️ LiveKit as connected
```

Join the configured room from any LiveKit client (web, mobile, voice-agent
desktop). The agent watches the room when empty and joins as soon as a real
participant arrives, then transcribes incoming audio and replies via TTS.

## Status

Experimental. Carved out of the `kortexa/gateway-livekit` branch on the
[kortexa-ai/hermes-agent](https://github.com/kortexa-ai/hermes-agent) fork
(PR [NousResearch/hermes-agent#3894](https://github.com/NousResearch/hermes-agent/pull/3894))
so it can be installed on top of upstream `main` without patching core.
