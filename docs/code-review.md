# Realtime code review — 2026-09-06

Scope: `hermes-livekit` only. The review used the plugin, its tests, the local
Hermes adapter contract, and installed LiveKit/aiortc source. `vision.server`
was excluded at Franci's request and was not changed.

The initial review was source-only. Franci later authorized validation and
delivery before the streaming-TTS work. The complete existing-environment
suite passed: 241 tests. One test fixture was corrected to cover stale events
both before receipt and after task scheduling. No runtime behavior was
weakened. This review does not claim measured latency or memory gains.
Tracking: [#39](https://github.com/kortexa-ai/hermes-livekit/issues/39).

## Changes

| Priority | Finding and consequence | Change |
| --- | --- | --- |
| P1 | `rtc.VideoStream(track)` starts a background receiver with an unlimited queue. Leaving video idle retains decoded frames; a later snapshot consumes old footage. | Keep one latest frame. Correct the camera documentation. Close video receivers on room leave and replacement. |
| P1 | Both TTS paths completed the protocol response before the base adapter sent the transcript. `send()` then opened another response. Text without successful TTS could stay active indefinitely. | Separate audio playout completion from response completion. `send()` completes the transcript response. Remove the transport-specific `tts_completed` flags. Processing hooks finish audio-only and failed turns, with a response ownership check to protect newer replies. |
| P1 | LiveKit cancellation awaited synchronous `AudioSource.clear_queue()`, causing a `TypeError`. `CancelledError` also bypassed playback cleanup. LiveKit resumed capture before its queued audio finished. | Use the synchronous SDK call, clear cancelled audio, restore capture in `finally`, and wait for actual playout before the echo guard. Direct playback also clears cancelled audio, including the frame already removed from its queue for pacing. |
| P1 | Direct call shutdown could flush another utterance from the cancelled receive loop and leave gateway processing alive. A cancelled HTTP setup could retain its peer and call slot. | Mark calls closed before draining tasks; discard their buffered speech; cancel gateway processing. Release unsuccessful setup in `finally`. HTTP handlers finish before the shutdown call snapshot. Upload and negotiation share a 30-second deadline. |
| P1 | A failed LiveKit join left `_room` set, so the presence watcher could stop retrying. Duplicate disconnect events could start competing reconnect loops. Old media receivers could survive a replacement. | Release failed rooms, coalesce reconnect attempts, cancel reconnect work on shutdown, and share receiver cleanup across disconnect, leave, join replacement, and binary-transfer recovery. Discard undispatched snapshots at room boundaries. |
| P1 | Hermes can invoke async tools on worker event loops. LiveKit binary callbacks run on the gateway loop, while the old proxy created futures and changed transfer state on the worker loop. | Route the full tool invocation to its captured gateway loop. Reject small RPC results when the room changed during the wait. |
| P2 | A `conversation.item.create` publication can suspend before recording pending text. The separately scheduled `response.create` task could overtake it. | Serialize incoming protocol messages across publication and dispatch awaits. Tool execution remains outside that lock. |
| P2 | Transcription used the participant's latest speech ID when its worker finished. A new utterance could replace that ID first. | Capture the stopped utterance's ID and pass it with the transcript. Ignore transcription results from a replaced room or closed direct call. This preserves IDs; it does not reorder concurrent transcription results. |
| P2 | Direct cancellation could unlink a WAV while its transcription thread still read it. LiveKit transcription exceptions could leave WAV files behind. Both paths copied PCM into a second in-memory WAV. | Share a synchronous worker that writes the WAV, transcribes it, and removes it in `finally`. The worker owns file lifetime even if its asyncio waiter is cancelled. Remove the unused WAV-copy helper. |
| P2 | The idle silence detector slept for two seconds. A short first utterance could occur during that gap and be missed by tail-based RMS checks. | Wake on audio subscription, then use the existing 200 ms active cadence. Do not run the detector over frozen buffers during TTS. |
| P2 | JPEG conversion and compression ran on the event loop and allocated an intermediate RGBA image. | Encode in a worker and convert directly to RGB. Discard the result if its room or video stream changed before it returned. Drain captured paths in linear time instead of repeated front removal. |
| P2 | Old room callbacks could inject protocol events and controls into the replacement room. Tool calls cancelled during event publication could leave their pending slot occupied. Malformed Unicode or deep JSON could escape protocol error handling. | Check room ownership on receipt and at task dispatch, release pending tool state on all emission exits, and reject malformed JSON through bounded protocol errors. Partial session updates no longer require repeating `type: realtime`. |

## Remaining findings and opportunities

1. **P1 — Turn ownership needs a single coordinator.** Concurrent voice
   transcriptions still dispatch in completion order. The base Hermes adapter
   can interrupt or combine pending messages; the plugin does not implement
   the deterministic multi-participant turn queue described in `PLAN.md`.
   Outgoing response methods also contain awaits outside the incoming-message
   lock. Build synthetic two-speaker, interruption, and tool-continuation
   fixtures before changing those semantics. Completion hooks intentionally
   refuse to finish a response whose ID changed; an audio-only or failed
   continuation after a client tool may need explicit turn-level ownership.
2. **P1 — LiveKit tools lack the Direct bridge's session check.** The LiveKit
   proxy checks the participant/tool policy and room membership, but ignores
   the invoking Hermes `session_id`. If another session exposes this toolset,
   it can invoke tools in the active room. Add session ownership checks with
   persisted-session and cross-platform fixtures. Direct tools reject foreign
   invocations, but their schemas still share a global toolset catalog; several
   simultaneous calls can expose irrelevant schemas to each other's models.
3. **P2 — Buffer and task admission remain incomplete.** Speech buffers have
   no utterance duration cap. STT jobs, queued input-event tasks, explicit JPEG
   requests, and direct output PCM can grow with input. Bound these with
   explicit overload behavior. A slow STT worker should not accumulate an
   unlimited queue of already obsolete turns.
4. **P2 — Endpointing remains coarse.** Normal speech waits for 1.5 seconds of
   silence plus a poll interval on LiveKit. Polling rechecks the buffer tail,
   even when no fresh samples arrived; a stalled track can leave speech active.
   Direct endpointing includes the trailing silence in its minimum-duration
   check. Move both transports to one sample-count-based utterance collector,
   with synthetic fan noise, packet stalls, short words, and mute transitions.
   Tune silence thresholds only after recorded-audio comparisons.
5. **P2 — Audio remains buffered end to end.** Hermes synthesizes files; ffmpeg
   decodes each entire file before RTP begins. Direct then copies it into 20 ms
   queue entries. Streaming decode/output can reduce time to first audio and
   peak memory. It needs decoder cancellation, bounded backpressure, and
   playback timing tests. The existing echo guards also prevent normal barge-in;
   reducing them without an echo strategy would risk self-transcription.
6. **P2 — Delivery success is optimistic.** Protocol `_emit()` ignores a false
   publish result, and LiveKit `send()` reports success after publication
   exceptions. A lost data channel can produce silent transcript loss or a
   tool wait that cannot complete. Define transport failure/retry semantics
   before changing the gateway's delivery result contract.
7. **P2 — Capture cleanup after dispatch is incomplete.** Once a snapshot is
   attached to a message, the adapter drops its file reference. Room cleanup
   removes only undispatched captures; it cannot reclaim delivered files.
   Tie file cleanup to actual media consumption or turn completion. Concurrent
   capture requests and multiple tracks per participant also need explicit
   ownership rules; current maps keep one audio and one video stream per identity.
8. **P2 — Compatibility claims exceed enforced behavior.** Required/named
   function choices rely partly on prompt instructions. Initial Direct session
   fields and `response.create` options can be accepted without implementation.
   Mutable session fields use stricter rejection. Define and test one supported
   subset instead of silently accepting provider features. No external API
   parity claim was verified during this review.

## Verification

Use the existing environment without installing or updating dependencies. A
focused unit selection can exclude tests that start a local HTTP listener:

```sh
uv run --no-sync pytest -q tests/test_media.py tests/test_adapter_contract.py tests/test_realtime_protocol.py tests/test_realtime_conference.py tests/test_realtime_webrtc.py tests/test_remote_tools.py tests/test_room_release.py tests/test_binary_tool_receiver.py -k 'not listener'
```

The full suite, including local listener tests, passed on Snappy:

```sh
uv run --no-sync pytest -q
```

The new fixtures cover publication ordering, malformed events, stable speech
IDs, worker-owned WAV cleanup, cancelled setup/playback, receiver shutdown,
room generation checks, response completion, RGB encoding, and worker-loop
tool dispatch. The existing binary-transfer suite must cover the shared room
cleanup change as well. Source inspection, the syntax check, `git diff --check`,
and all 241 tests passed. Full-room operational checks below remain separate
from deterministic regression coverage.

For later integration, exercise both transports with one fixed audio clip:

- TTS then transcript: one response ID, one `response.done`, and correct final
  text; also cover text-only, audio-only, synthesis failure, and tool continuation.
- Cancel while decoding, queueing, playing, and waiting for client tools. Start
  a new turn immediately and check for old audio or stale lifecycle events.
- Speak immediately after an idle room joins, then overlap two speakers and
  delay one transcription. Verify item IDs and inspect turn order separately.
- Leave and reconnect repeatedly, including failed setup. Check tasks, native
  media handles, call slots, pending captures, and tool registrations.
- Leave a camera subscribed without captures, then capture a visible timestamp.
  Compare memory growth, frame freshness, and event-loop responsiveness.
- Measure end-of-speech to transcript, response start, first audible sample,
  final audible sample, and protocol completion separately. Keep model and
  audio inputs constant; do not infer speedups from source changes alone.
