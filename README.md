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
`HERMES_REALTIME_MAX_CALL_SECONDS` defaults to 7200. Offer upload and ICE
negotiation share a 30-second setup deadline; unsuccessful setup releases its
call slot, peer, and tools. The current direct edge
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

### Voice endpointing and room noise

Set `silence_duration` in each platform's configuration, in seconds:

```yaml
platforms:
  realtime:
    extra:
      silence_duration: 0.7
  livekit:
    extra:
      silence_duration: 0.7
```

The default remains 1.5 seconds. Values from 0.2 to 5.0 are accepted; invalid
values fail configuration instead of silently changing voice behavior. Restart
the affected profile's gateway after changing it. Hermes CLI's
`voice.silence_duration` is a separate recorder setting and does not affect
these transports. Direct WebRTC checks on received audio frames; LiveKit checks
every 200 ms, so it can add up to one polling interval.

The energy gate calibrates from the quieter half of the initial 400 ms, then
tracks background noise continuously during idle audio and confident quiet
pauses. It has separate speech-start and speech-stop thresholds. Noise rises
slowly and falls faster, using audio duration rather than frame count. Detected
speech and near-threshold phonemes during a turn are excluded from adaptation.
Mute/unmute recalibrates the gate against the real microphone.

This is still energy-based detection, not semantic turn detection. A loud new
noise above the speech threshold can look like speech; a shorter timeout can
split thinking pauses. Validate with the actual room and microphone before
lowering it further. Endpoint logs include observed silence, configured target,
and learned noise RMS; they do not contain microphone recordings.

For speech/noise discrimination, optionally install the `vad` extra in the
gateway's Python environment and prepare the pinned Silero v6.2 model:

```sh
python -m pip install -e '.[vad]'
python tools/prepare_vad.py /absolute/path/to/silero-v6.2.onnx
```

Set these keys under either platform's `extra` configuration, then restart
only the affected gateway:

```yaml
vad_backend: silero
vad_model_path: /absolute/path/to/silero-v6.2.onnx
vad_threshold: 0.5
```

The default backend remains `rms`, with no added runtime dependency. Silero
uses only CPU, one inference thread and independent state per input. The
model is shared between inputs, loaded at adapter startup and checked against
a pinned SHA-256. Missing dependencies or an invalid model fail startup; there
is no automatic download, GPU fallback or silent change back to energy VAD.
The confidence threshold accepts 0.2–0.9; lower values are more sensitive.
The existing silence timeout still applies: this is not semantic endpointing.
The timer starts at the detector's last positive decision, which can occur
after audible speech has ended. Equal timer values do not guarantee equal
endpoint latency across backends. Measure total endpoint delay and test
mid-sentence pauses before lowering the timer; a shorter timer can split turns.
The [model and its license](https://github.com/snakers4/silero-vad/tree/be95df9152c0d7618fa1edfeb296fc3dae32376f)
are MIT-licensed by the Silero Team; the installer saves the license beside
the model. Keep both outside the repository.

Both backends retain 500 ms of pre-roll to protect word onsets, without waiting
longer to dispatch the utterance. Continuous
capture is limited to two minutes per input. Overflow discards the whole
unsubmitted utterance and reports `input_audio_too_long`; no truncated command
is sent to ASR or Hermes. Pause for the configured silence interval, or mute
then unmute, before trying again. Other callers and ongoing replies continue.

For an offline noise-step and quiet-speech check through the actual direct
capture path, run:

```sh
python tools/vad_probe.py --model /absolute/path/to/silero-v6.2.onnx \
  --speech-fixture /absolute/path/to/mono-pcm16.wav
```

It reports
CPU timings and endpoint events without recording, playback or service calls.
Synthetic fixtures do not replace qualification with the actual room/mic.

To overlap transcription with that silence window, optionally set
`asr_prefetch_silence: 0.35` beside `silence_duration: 0.7` in either platform's
`extra` configuration. The default is `0` (disabled); enabled values must be
at least 0.1 seconds and below `silence_duration`. Restart the affected gateway.
This uses the existing Hermes STT provider, model and prompt. It is speculative
batch ASR, not a persistent streaming-ASR connection.

After a qualifying quiet pause, one early request runs in the background. The
gateway still waits for its normal endpoint before dispatching a user turn.
It reuses the early result only if speech has not resumed and the final input
differs solely by quiet PCM at the tail; otherwise it transcribes the final
audio normally. Empty or failed early results also fall back. Each input has
at most one speculative worker in flight; invalidating a candidate does not
cancel an active server request. A pause followed by more speech can therefore
cost an extra ASR request. This is an energy-based safety check, not proof that
very quiet speech was absent. Validate accuracy and load with the real mic.
Gateway logs indicate whether each final transcription reused an early result.

### Streaming speech

Direct WebRTC keeps a paced 20 ms audio stream open, including silent frames
between replies and between generated clauses. This keeps the receiver's
media clock running instead of restarting an audio burst after every pause.
Idle frames do not create response events or delay speech-queue draining;
they add a small ongoing Opus/network cost while a call is connected.

Both transports implement Hermes Agent's streaming-TTS audio sink. For voice
turns with auto-TTS enabled and a supported streaming provider, completed text
clauses can play while the rest of the reply is still being generated. The
configured provider and voice are preserved; no TTS-server or client changes
are needed. Unsupported providers retain whole-file playback.

PCM is resampled incrementally to 48 kHz mono and published in paced 20 ms
frames. Direct WebRTC queues at most 500 ms of audio ahead of the sender;
LiveKit uses its audio source's playout backpressure. A failure before playable
audio permits whole-file fallback. After playback starts, failure or cancellation
must not replay the reply from the beginning.

Streaming requires a Hermes Agent consumer that preserves adapter-owned
`handle.audible` and supports `tts.streaming.completion_timeout` (default 120
seconds, accepted range 1–600). This bounds the wait for a long reply to finish;
it does not delay the start of playback. The companion fixes are tracked in
[the streaming integration issue](https://github.com/kortexa-ai/hermes-livekit/issues/40).

Generated PCM can itself start with a long near-silent prefix. To shorten that
pause, set `platforms.realtime.extra.tts_trim_leading_silence: true` (or the
equivalent `platforms.livekit.extra` key) and restart that profile's gateway.
This boolean defaults to `false`. It removes only near-silent frames at the
start of each streamed reply, retaining 80 ms before the first sample above
PCM16 peak 16 (about -66 dBFS). It scans at most one second and buffers at most
four 20 ms frames. Internal pauses, later clauses, and retained samples are
unchanged. This is conservative amplitude trimming, not speech recognition;
disable it when exact leading timing or unusually faint output must be kept.
The provider's output contract and whole-file fallback are unchanged.

Run the transport tests with `uv run --no-sync pytest -q tests/test_streaming_tts.py`.
To test the configured TTS service through real local RTP peers, explicitly opt
in with `HERMES_TTS_CANARY_CONFIG=/path/to/profile/config.yaml` and select
`-k smarty-opt-in -s`. The canary prints timings, not credentials, and does not
play sound on a physical device.

For an end-to-end measurement from the Pi, run the isolated voice probe:

```bash
uv run --no-project --no-build \
  --with aiortc==1.15.0 --with aiohttp==3.14.3 --with av==17.0.0 \
  python tools/voice_latency_probe.py \
  --gateway http://192.168.2.6:8092 --config-host snappy --profile mira --case greeting
```

It fetches profile credentials over passwordless SSH into memory, synthesizes
one fixed question, and sends a real voice turn through ASR, Hermes, and TTS.
Choose `--case greeting`, `fact`, `calculation`, or `first_sentence`. The session requests no tools,
but that does not disable Hermes's own tools: inspect gateway API counts before
treating any case as a single model request.
The probe rejects split or premature endpoint detections instead of reporting
latency for mixed turns. Keep failed samples when evaluating endpoint quality;
do not treat them as valid latency results.
To compare reasoning effort on the same model, add `--reasoning medium` or
`--reasoning low`. The probe sends Hermes's native session-only `/reasoning`
command in its own new call and waits for an English acknowledgement before
speaking. It never sends `--global`, changes the kiosk call, or changes the model
or service tier. Command errors, unexpected audio and missing acknowledgements
fail the probe. The default `--reasoning profile` sends no command. Add
`--show-answer` to include at most 500 characters of the fixture answer for
manual review; otherwise only its word count is reported. Counterbalance effort
order and compare several serial runs. These simple questions do not establish
general reasoning/tool quality, so do not promote a default from timing alone.
Use `--turns 4` to compare the first reply with three later replies on the same
connection (1–10 turns; default 1). The probe synthesizes the question once and
replays identical PCM without resetting the RTP clock. Multi-turn output has a
`turns` array, one shared `call_id` and `fixture_sha256`, and a `response_id` per
reply. There is a one-second settling interval between completed replies and
the next utterance; recent residual audio, early response audio, split fixtures
and multiple audio replies invalidate the measurement. The interval is outside
the measured latency. Correlate these identities with the opt-in gateway metrics
to distinguish fresh-call prompt-cache misses from warm-session model time.
Repeated requests share conversation history, so later answers can differ;
this measures the conversation path, not identical model inputs or pure prefill.
Native transcript frames add `caption_after_speech_s`, `caption_to_audio_event_s`
and `caption_to_audible_rtp_s`. These use the first nonempty incremental frame
or full-snapshot replacement for the answer's response ID. Empty frames,
final-only setup notices and other responses do not start this clock. Older
transports without these frames omit the fields. These are client receipt times,
not the provider's first-token timestamps; a negative caption-to-audio interval
means that audio started before the caption arrived.
The `first_sentence` case requests a fixed two-sentence answer starting with
“Yes.” and rejects a different final answer. Use it to measure the delay from
buffering a short opening sentence into the next one. Its instructions belong
only to the isolated test call; it does not change Mira's profile or speech
chunking settings. Timing and text preservation do not establish voice quality.
It neither captures the microphone nor plays audio. The configuration host
must have Hermes at `/Users/francip/src/hermes-agent` with its existing venv;
the speech fixture expects the configured service's 24 kHz mono PCM format.
Run probes serially and avoid concurrent service benchmarks. JSON timings
measure from the last voiced source frame to endpoint detection, transcript,
audio-start event, and first audible RTP received by the Pi. The latter includes
network and receiver buffering, but not the kiosk's physical speaker latency.
Compare several runs: model generation time can vary independently of the
silence timeout or TTS transport.

For local model timing logs, set
`plugins.entries.livekit.settings.voice_latency_metrics: true` in the Hermes
profile and restart its gateway. It defaults to off and uses existing plugin
hooks; it does not rewrite requests or change model configuration. Hermes
treats stream observers as stream consumers, so enabling this in a mixed-channel
profile can also enable internal model streaming for other channels. Logs remain
limited to voice turns. `voice_timing` JSON logs contain the
first observed visible text and each completed API request, token/cache counts,
and tool-call count. Correlate `(session_id, turn_id, iteration)`; separate hook
workers can log out of order. First-text time includes observer queue delay.
Provider first-chunk time is `null` when unavailable (including current Codex
Responses); neither metric isolates provider queueing, network or model prefill.
Only timings and bounded identifiers are logged; no text, audio or credentials
are retained by this observer. Deduplication holds at most 1,024 request keys.
Existing audio logs distinguish first provider PCM entering the sink from the
first published PCM frame, so resampling/onset gating is measured separately.

Use the same isolated dependencies with `python tools/tts_onset_probe.py` to
measure provider first bytes versus audible PCM, then compare the identical
waveform with and without onset trimming. Its sample-offset difference is a
PCM-domain saving, not an end-to-end playback measurement. It also reports
filter CPU cost on the machine running the probe and verifies that every
retained sample is unchanged.

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
- `response.output_audio_transcript.delta` / `.done`
- `output_audio_buffer.started` / `stopped` / `cleared`
- correlated `error` events

When Hermes text streaming is enabled, captions arrive during generation,
independently of audio. Append `delta` to the item identified by `item_id`;
if a delta event includes the optional `transcript` snapshot, replace that
item's draft instead (native text can be revised at a tool boundary). The
final `.done` transcript replaces the draft, not a second message. Neither
text update nor text completion indicates that audio playback has stopped.

The adapter capability does not override a profile's disabled text-streaming
policy. Enable only the voice platforms when other clients should stay final-only:

```sh
hermes --profile mira config set display.platforms.realtime.streaming true
hermes --profile mira config set display.platforms.livekit.streaming true
```

Restart that profile's gateway after changing its configuration. The top-level
`streaming.enabled` can remain false; `display.streaming` controls the CLI,
not gateway delivery. A consumer created for interim messages alone does not
receive model text deltas. Qualify a real voice turn, not just adapter methods.

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
that omit this optional extension retain the configured server-side endpointing.

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

The LiveKit SDK receives and decodes a subscribed camera continuously. The
adapter keeps only the newest frame, so idle video does not accumulate an
unbounded queue or make later snapshots stale. Send
`{"type": "conference.capture_frame"}` on `conference.extensions` and the
agent samples the **latest available** frame (or waits for one), encodes
it as JPEG (quality 85), and queues it locally.

The frame attaches to **the next user message** dispatched by the adapter
(either a closed voice utterance or a standard typed Realtime turn). The Hermes agent
loop then processes it through its existing `image_input_mode: auto`
vision path — exactly the same code path used by image attachments on
other platforms.

Frames captured but never claimed by a message are cleaned up on
disconnect. Frames attached to a message stay on disk through the agent
turn (the agent loop is fire-and-forget after `handle_message`).

For source-review findings and deferred verification, see
[`docs/code-review.md`](docs/code-review.md).

## Status

Experimental. Carved out of the `kortexa/gateway-livekit` branch on the
[kortexa-ai/hermes-agent](https://github.com/kortexa-ai/hermes-agent) fork
(PR [NousResearch/hermes-agent#3894](https://github.com/NousResearch/hermes-agent/pull/3894))
so it can be installed on top of upstream `main` without patching core.
