# hermes-livekit

<p align="center">
  <img src="docs/assets/hermes-gateway.png" width="220" alt="Hermes realtime gateway mark">
</p>

Realtime voice gateway plugin for [hermes-agent](https://github.com/NousResearch/hermes-agent).

It serves an OpenAI-compatible direct WebRTC endpoint and can join a LiveKit
room as a Realtime Conference agent. Both transports transcribe speech through
Hermes, run the same agent loop, return TTS audio, and share one conversation
event contract. The name is now historically accurate in only one direction;
the rename raccoon remains on the roadmap.

The gateway mark combines a transport arch, messenger wings, and an audio
pulse. `docs/assets/hermes-gateway-master.png` is the opaque lossless source;
`./scripts/export-readme-mark.sh` regenerates the optimized README image. The
original artwork was generated with OpenAI's built-in image generation tool on
2026-08-27 with text, third-party marks, and transparency prohibited.

## Requirements

- An existing `hermes-agent` install (this plugin attaches to it; it does not
  vendor hermes itself).
- `ffmpeg` on `PATH` — used to decode TTS audio for the WebRTC publish path.
  - macOS: `brew install ffmpeg`
  - Debian / Ubuntu: `sudo apt install ffmpeg`
- A reachable LiveKit server and API key/secret pair only when using the
  Conference transport. Direct WebRTC has no LiveKit dependency at runtime.

## Install

Install into the **same Python environment** as your `hermes-agent`:

```bash
python -m pip install git+https://github.com/kortexa-ai/hermes-livekit.git
```

pip resolves the pinned `livekit` / `livekit-api` SDK versions automatically.
If Hermes' environment does not contain pip, target its interpreter with uv:

```bash
uv pip install --python /path/to/hermes/python \
  git+https://github.com/kortexa-ai/hermes-livekit.git
```

The plugin is auto-discovered through the `hermes_agent.plugins` entry-point
group — no edits to hermes-agent's source tree are required. This revision
requires Hermes Agent 0.20.0 or newer because that host line carries the
Pillow 12.3 security fixes and the current platform-registration contract.
Until 0.20.0 is published, install against a current Hermes source checkout.

> Note: `hermes plugins install kortexa-ai/hermes-livekit` is **not** the
> right path for this plugin. That command `git clone`s into
> `~/.hermes/plugins/` without resolving pip deps; you'd then have to
> `pip install 'livekit==1.1.14' 'livekit-api==1.2.0'` by hand. The pip
> install above is one command and keeps the SDK pins in sync with the
> plugin version.

### Local / editable install

For development on a checkout (e.g. `~/src/hermes-livekit/`):

```bash
python -m pip install -e ~/src/hermes-livekit
```

Run that command with the Python interpreter from the same environment as
Hermes. Re-run it after pulling a change to `pyproject.toml`: editable installs
reflect Python source edits immediately, but package metadata such as the
version, dependencies, and plugin entry point is generated at install time.
The uv equivalent is:

```bash
uv pip install --python /path/to/hermes/python -e ~/src/hermes-livekit
```

## Enable

After install, add `livekit` to the enabled-plugins list:

```bash
hermes plugins enable livekit
```

(Or edit `~/.hermes/config.yaml` and add `livekit` to `plugins.enabled`.)

Then enable either or both platforms in the same config:

```yaml
platforms:
  livekit:
    enabled: true
  realtime:
    enabled: true
plugins:
  enabled:
    - livekit
```

## Configure

### Direct Realtime / WebRTC

The direct adapter implements OpenAI-style SDP signalling at
`POST /v1/realtime/calls` (with `/realtime/calls` as an alias), RTP audio, and
the `oai-events` data channel. The preferred setup request is multipart with a
required `sdp` field and optional `session` field. It returns `201
application/sdp` with the call resource in `Location`; raw `application/sdp`
offers remain accepted for older clients. Every listener requires a Bearer
token:

```yaml
platforms:
  realtime:
    enabled: true
    host: 127.0.0.1
    port: 8091
    api_key: ${HERMES_REALTIME_API_KEY}
```

The equivalent environment-only setup is:

```bash
HERMES_REALTIME_ENABLED=true
HERMES_REALTIME_HOST=127.0.0.1
HERMES_REALTIME_PORT=8091
HERMES_REALTIME_API_KEY=choose-a-long-random-token
HERMES_REALTIME_ALLOW_ALL_USERS=true
```

Send `HERMES_REALTIME_API_KEY` as a Bearer token. Startup fails closed if it is
missing. `HERMES_REALTIME_MAX_CALLS` defaults to 8 and
`HERMES_REALTIME_MAX_CALL_SECONDS` defaults to 7200. The current direct edge
advertises host ICE candidates; deployments across NAT still need a TURN-aware
front door before they are internet-ready.

Each listener is permanently bound to the Hermes profile of its gateway
process. Trusted routers can discover that fixed binding from
`GET /v1/realtime/discovery` with the same Bearer token:

```bash
curl -H "Authorization: Bearer $HERMES_REALTIME_API_KEY" \
  http://127.0.0.1:8091/v1/realtime/discovery
```

The bounded response contains only `version`, `profile`, and the relative
`realtime_path`. It does not enumerate other profiles or expose credentials,
filesystem paths, rooms, models, or provider configuration.

### Realtime Conference / LiveKit

Set these env vars. Current Hermes also accepts the matching lowercase keys
either directly under `platforms.livekit` or inside `platforms.livekit.extra`:

```yaml
platforms:
  livekit:
    enabled: true
    url: wss://your-project.livekit.cloud
    api_key: your-project-key
    api_secret: your-project-secret
    room: hermes
```

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
| `HERMES_LIVEKIT_TOOL_TIMEOUT_SEC` | no      | native RPC response timeout; default 30 seconds                    |
| `HERMES_LIVEKIT_REMOTE_TOOL_POLICY` | no    | bounded JSON policy; absent or invalid denies all remote tools     |

Or run the interactive prompt:

```bash
hermes config
```

## Verify

```bash
hermes gateway restart
hermes gateway status      # should show 🎙️ LiveKit as connected
```

For direct WebRTC, status also shows `⚡ Realtime` and the signalling endpoint
is `http://127.0.0.1:8091/v1/realtime/calls` with the defaults above.

Join the configured room from any LiveKit client (web, mobile, voice-agent
desktop). The agent watches the room when empty and joins as soon as a real
participant arrives, then transcribes incoming audio and replies via TTS.

## Data channel protocol

Reliable JSON messages on `conference.events` use the same OpenAI-compatible
session and conversation contract as `api.server` Conference calls. Audio
stays on LiveKit tracks. Every participant in the room shares one Hermes
conversation session; participant identity still scopes errors and tools.

The agent sends a targeted `session.created` snapshot when each participant
joins. Room lifecycle events are broadcast:

- `input_audio_buffer.speech_started` / `speech_stopped`
- `conversation.item.added` / `conversation.item.done`
- `conversation.item.input_audio_transcription.completed`
- `response.created` / `response.done`
- `response.output_item.added` / `response.output_item.done`
- `response.content_part.added` / `response.content_part.done`
- `response.output_audio_transcript.done`
- `output_audio_buffer.started` / `stopped` / `cleared`
- correlated `error` events

Clients send supported Realtime events on the same topic. Typed input uses a
normal user conversation item:

```json
{
  "type": "conversation.item.create",
  "item": {
    "type": "message",
    "role": "user",
    "content": [{"type": "input_text", "text": "Hello"}]
  }
}
```

`response.create` starts the pending typed turn or requests a new response
through the normal Hermes message pipeline. `response.cancel` cancels the
active Hermes room turn and clears queued output audio. Unsupported events
receive an explicit error targeted to the sending participant. The old
`hermes-chat` and raw conversation `agent:*` streams are not part of the new
contract.

Hermes adds one namespaced input-state extension because the portable OpenAI
Realtime event set has no microphone mute/unmute signal:

```json
{"type":"hermes.input_audio.state","muted":true}
```

Direct WebRTC clients send it on `oai-events`. Conference clients send the
same envelope reliably on `conference.extensions`. The server responds on the
same transport with `hermes.input_audio.state_updated`. Muting immediately
finalizes any active utterance and then ignores media frames; unmuting clears
stale audio and recalibrates the participant's adaptive noise gate. Clients
that omit this optional extension retain energy-based endpointing.

### Conference extensions

Portable tool discovery uses `conference.tools`, exactly as it does with
`api.server` Conference. Native RPC invocation and bounded byte streams are
negotiated LiveKit extensions. Triggered camera and runtime controls use the
separate `conference.extensions` topic.

Frame status messages use the extension topic:

- `agent:frame-captured`
- `agent:frame-capture-failed`

Portable registration acknowledgements on `conference.tools` are targeted to
the owning participant:

- `conference.tools.registered`
- `conference.tools.rejected`

Binary-result lifecycle messages are also targeted on `conference.tools`:

- `agent:tool-result-stream-ready` — the targeted binary-result receiver is
  installed; `{stream_id, topic}`
- `agent:tool-result-stream-cancel` — stop and close that targeted binary
  stream; `{stream_id, topic}`

#### Inbound extension controls

Reliable JSON payloads on `conference.extensions`:

```jsonc
// sample the next frame from this client's published video track
{"type": "conference.capture_frame"}

// runtime control hooks
{"type": "conference.control", "action": "pause"}    // stop sampling audio
{"type": "conference.control", "action": "resume"}   // resume sampling audio

// this speaker is done talking; close the utterance and dispatch it now (0.4.0+)
{"type": "conference.control", "action": "end-of-turn"}

// participant-scoped mute boundary and VAD recalibration
{"type": "hermes.input_audio.state", "muted": true}
{"type": "hermes.input_audio.state", "muted": false}
```

`end-of-turn` is for clients that endpoint locally. Without it the adapter can
only notice you stopped once it has seen `SILENCE_THRESHOLD_SECONDS` of silence,
and that wait lands on every single reply. Unlike `pause`/`resume` — which are
global — this is scoped to the sending participant, so one client ending its
turn cannot affect anyone else in the room.

Tool catalogs use the portable Conference envelope on `conference.tools`:

```jsonc
{
  "type": "conference.tools.register",
  "tools": [{
    "type": "function",
    "function": {
      "name": "desktop_notify",
      "description": "Show a desktop notification.",
      "parameters": {
        "type": "object",
        "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
        "required": ["title", "body"]
      }
    }
  }]
}

// replace this participant's catalog with no tools
{"type": "conference.tools.register", "tools": []}
```

Remote tools are disabled unless `HERMES_LIVEKIT_REMOTE_TOOL_POLICY` contains
an exact participant/name entry. Tier 1 permits the exact entry. Tier 2 also
requires a future Unix `consent_expires_at`. Tier 3 is always denied:

```json
{"tools":[{"participant_identity":"desktop-client","tool_name":"desktop_notify","tier":2,"consent_expires_at":1798761600}]}
```

The policy is loaded once when the adapter is constructed. See
[`docs/remote-tools-design.md`](docs/remote-tools-design.md) for the closed
classification, bounds, and audit contract.

Advertised RPC method names use case-sensitive ASCII identifier segments
separated by single dots, with a 64-character total limit. For example,
`camera.snapshot` is valid; `.camera`, `camera.`, `camera..snapshot`, path
separators, whitespace, Unicode, and normalized aliases are not.

Before advertising a tool, the client registers a LiveKit RPC method with the
same name. The agent calls that method with the tool arguments encoded as a
JSON object. The method returns either a JSON-shaped result or the bounded
byte-stream reference documented below, encoded as a JSON string, or raises
`RpcError`. LiveKit owns request correlation, response timeout, and error
transport. These calls do not use custom `agent:tool-call` or
`client:tool-result` data messages.

`examples/test_client.py` registers both `desktop_notify` and
`camera.snapshot`. The camera tool deliberately returns a built-in 1x1 PNG, so
the complete reference → targeted ready → byte stream → cleanup contract can be
tested without camera hardware. A real client can replace those fixture bytes
after applying the same 12 MiB bound. The stream header, chunks, and reference
target only the agent participant whose RPC invocation requested the snapshot.

For tools to be visible to the LLM, add `hermes-livekit-tools` to the
livekit toolset list in `~/.hermes/config.yaml`
(`platform_toolsets.livekit`). The plugin does not auto-activate the
toolset.

Tools are removed automatically when the registering participant disconnects.
Full JSON, bounded-binary, and participant-scoped multi-client design in
[`docs/remote-tools-design.md`](docs/remote-tools-design.md).

Unknown `type` values are ignored silently — keeps the topic compatible
with apps that share the same data channel for unrelated control traffic.

### Video / camera-frame semantics

The agent does **not** consume video tracks continuously. When you
publish a camera as a video track, the adapter just subscribes to it —
no frames are decoded until you ask. Send
`{"type": "conference.capture_frame"}` on `conference.extensions` and the
agent samples the **very next** frame, encodes
it as JPEG (quality 85), and queues it locally.

The frame attaches to **the next user message** dispatched by the adapter
(either a closed voice utterance or a standard typed Realtime turn). The Hermes agent
loop then processes it through its existing `image_input_mode: auto`
vision path — exactly the same code path used by image attachments on
other platforms.

Frames captured but never claimed by a message are cleaned up on
disconnect. Frames attached to a message stay on disk through the agent
turn (the agent loop is fire-and-forget after `handle_message`).

## Status

Experimental. Carved out of the `kortexa/gateway-livekit` branch on the
[kortexa-ai/hermes-agent](https://github.com/kortexa-ai/hermes-agent) fork
(PR [NousResearch/hermes-agent#3894](https://github.com/NousResearch/hermes-agent/pull/3894))
so it can be installed on top of upstream `main` without patching core.
