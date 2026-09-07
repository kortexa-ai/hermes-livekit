# Continuous audio clock across TTS gaps

Owning issue: https://github.com/kortexa-ai/hermes-livekit/issues/47

Keep the direct WebRTC sender paced at 20 ms even when its bounded speech
queue is empty. Send silent frames during idle intervals so RTP time advances
and a connected receiver stays primed. Select queued speech at the send
deadline, preserve its samples, and retain backpressure without prebuffering
or delaying new speech. Count only queued speech as unfinished work. Keep
response start/stop events tied to actual TTS delivery, not idle packets.
Clear must discard cancelled speech without resetting the stream clock, and
a stopped track must end promptly.

Test idle cadence, timestamps, burst-onset sample identity, queue draining,
clear races and stop. Send marked short segments through real RTP peers across
several silence gaps and verify that the first segment survives decoding.
Keep the long-reply regression counting actual speech, not new idle silence.
Run the full plugin suite, measure idle overhead and qualify the deployed
Mira path. A software RTP test is not proof that the physical Pi speaker no
longer clips; retain that distinction and seek playback confirmation.

Deliver through Git and restart only the managed Mira gateway. Do not rebuild
WPE or alter ASR, TTS, VAD, the onset trimmer or client UI in this work unit.
Rollback uses a focused Git revert and targeted gateway restart. Keep live
measurements, deployment state and remaining physical-playback checks on the
owning issue rather than in this static note.
