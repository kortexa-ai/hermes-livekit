# Multi-tool voice-turn qualification

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/15

Test the actual Hermes client-tool continuation before changing timeout policy.
Use isolated receive-only WebRTC calls, two no-side-effect fixture functions,
and fresh returned values that are absent from the prompt. Require both tool
calls, acknowledged results, the matching final transcript, and non-silent RTP.
Welcome notices and function-call response seals are not final spoken answers.

`python tools/tool_latency_probe.py --case success` injects a 400 ms client
wait. `--case parallel` additionally asks for both independent tools in one
model round; confirm actual API tool-call counts in the opt-in server metrics
before claiming model-level fan-out. `--case mixed` returns an explicit beta
failure. `--case timeout` waits
for the real server deadline, submits the expired beta result, and requires
its rejection without losing alpha's result or the final spoken answer.
The probe uses the existing profile credential loader; it must never record
microphone input, play sound, print credentials, or change profile settings.
Keep output bounded and close the isolated peer on every terminal path.

Validate fixture correlation offline, then run against Mira. Record findings
and exact revisions in the issue. Keep source changes separate from claims of
physical playback quality, native LiveKit SDK timeout timing, or LLM prefill.
Direct client functions currently serialize against one pending protocol slot;
removing only that lock is not a valid parallel-tool implementation.

The tool probe sends `input_text`. Hermes currently enables streaming TTS only
for voice input, so this probe's caption-to-audio gap includes whole-file TTS.
Use `voice_latency_probe.py` for the actual synthetic-speech streaming path;
do not treat these typed fixture timings as a streaming-TTS regression.

A server-side tool timeout must open the continuation response before Hermes
can publish native captions. Unlike a successful client result, it receives no
`response.create` from the client. Preserve the processing-turn identity and
reject stale continuation after cancellation or replacement. Cover caption
finalization both before and after the first TTS PCM; do not require audio to
open the response. Keep the tool deadline unchanged by this lifecycle repair.

`--case cancel` cancels while beta is pending, requires a separate cancelled
continuation response, submits a late beta result, and checks its rejection.
It watches a two-second quiet window for spurious audio. Offline tests also
cover cancellation between a received result and `response.create`, reentrant
processing-complete hooks, and queued sibling invocations. A queued invocation
must retain its processing-turn identity across the bridge lock and must not
join a replacement turn after cancellation. Stopping the turn must leave the
call usable for a fresh response without waiting for the tool deadline.
