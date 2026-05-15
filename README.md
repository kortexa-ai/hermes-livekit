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

## Data channel protocol

Outbound (agent → client) is unchanged from earlier voice-only versions —
final text replies on topic `hermes-chat`, `agent:*` lifecycle events with
no topic. The 0.2.0 release adds an **inbound** channel for client-driven
control + camera snapshots.

### Outbound (agent → client)

| Topic | Payload | When |
|---|---|---|
| `hermes-chat` | UTF-8 text | After agent generates a reply |
| _(no topic)_ | JSON `{"type": "agent:<...>", "payload": {...}}` | Lifecycle events (see below) |

Agent lifecycle event types:

- `agent:listening-start` / `agent:listening-stop` — VAD detected speech start/end
- `agent:user-transcript` — STT (or typed message) finalized; payload `{transcript, final, identity, source?}`
- `agent:thinking-start` — agent about to invoke the LLM
- `agent:speaking-start` / `agent:speaking-stop` — TTS playback boundary
- `agent:agent-transcript` — assistant reply text mirrored on data channel
- `agent:frame-captured` — a video frame was sampled and queued; payload `{identity, width, height, bytes, timestamp}`
- `agent:frame-capture-failed` — `client:capture-frame` could not be honored; payload `{reason, identity?, detail?}`

### Inbound (client → agent), topic `hermes-control`

JSON payloads of the form `{"type": "client:<...>", ...}`:

```jsonc
// sample the next frame from this client's published video track
{"type": "client:capture-frame"}

// inject a typed message (skips STT). Pending captures attach automatically.
{"type": "client:message", "text": "what's in this picture?"}

// runtime control hooks
{"type": "client:control", "action": "pause"}    // stop sampling audio
{"type": "client:control", "action": "resume"}   // resume sampling audio
```

Unknown `type` values are ignored silently — keeps the topic compatible
with apps that share the same data channel for unrelated control traffic.

### Video / camera-frame semantics

The agent does **not** consume video tracks continuously. When you
publish a camera as a video track, the adapter just subscribes to it —
no frames are decoded until you ask. Send `{"type": "client:capture-frame"}`
on `hermes-control` and the agent samples the **very next** frame, encodes
it as JPEG (quality 85), and queues it locally.

The frame attaches to **the next user message** dispatched by the adapter
(either a closed voice utterance or a `client:message`). The hermes agent
loop then processes it through its existing `image_input_mode: auto`
vision path — exactly the same code path used by image attachments on
other platforms.

Frames captured but never claimed by a message are cleaned up on
disconnect. Frames attached to a message stay on disk through the agent
turn (the agent loop is fire-and-forget after `handle_message`).

### Reserved for future work

`client:tool-register`, `client:tool-unregister`, and `client:tool-result`
message types are reserved for a future iteration that lets the client
publish remote tools the agent can invoke over the data channel — think
teleoperated robotics, browser actions, hardware controllers. Don't ship
clients that send those types yet; the protocol shape may change.

## Status

Experimental. Carved out of the `kortexa/gateway-livekit` branch on the
[kortexa-ai/hermes-agent](https://github.com/kortexa-ai/hermes-agent) fork
(PR [NousResearch/hermes-agent#3894](https://github.com/NousResearch/hermes-agent/pull/3894))
so it can be installed on top of upstream `main` without patching core.
