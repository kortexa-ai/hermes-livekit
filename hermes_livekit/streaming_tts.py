"""Hermes PCM sinks shared by the direct WebRTC and LiveKit transports."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from gateway.platforms.base import AudioFormat, StreamingTTSHandle

from .audio_onset import LeadingSilenceTrimmer

logger = logging.getLogger("gateway.platforms.livekit.streaming_tts")
OUTPUT_RATE = 48_000
FRAME_SAMPLES = OUTPUT_RATE // 50
FRAME_BYTES = FRAME_SAMPLES * 2
SINK_TIMEOUT = 5.0


class PcmFramer:
    """Preserve partial samples and resampler state across arbitrary HTTP chunks."""

    def __init__(self, audio_format: AudioFormat) -> None:
        from av import AudioResampler

        self.sample_rate = audio_format.sample_rate
        self._tail = b""
        self._resampler = AudioResampler(
            format="s16", layout="mono", rate=OUTPUT_RATE, frame_size=FRAME_SAMPLES,
        )

    def feed(self, chunk: bytes):
        from av import AudioFrame

        data = self._tail + chunk
        end = len(data) - len(data) % 2
        self._tail = data[end:]
        # Bound each native conversion, even if a provider yields a large body.
        for offset in range(0, end, 8192):
            pcm = data[offset:min(offset + 8192, end)]
            frame = AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
            frame.sample_rate = self.sample_rate
            frame.planes[0].update(pcm)
            for converted in self._resampler.resample(frame):
                yield bytes(converted.planes[0])[:converted.samples * 2]

    def finish(self):
        if self._tail:
            raise ValueError("Streaming TTS ended with an incomplete PCM16 sample")
        for converted in self._resampler.resample(None):
            pcm = bytes(converted.planes[0])[:converted.samples * 2]
            yield pcm.ljust(FRAME_BYTES, b"\x00")


@dataclass
class AudioSinkHandle(StreamingTTSHandle):
    owner: Any = None
    protocol: Any = None
    sink: Any = None
    framer: PcmFramer | None = None
    response_id: str | None = None
    started: bool = False
    finished: bool = False
    pending: asyncio.Task | None = None
    opened_at: float = field(default_factory=time.monotonic)
    pcm_bytes: int = 0
    first_input_at: float | None = None
    leading_silence: LeadingSilenceTrimmer | None = None


class StreamingTTSMixin:
    """Turn-owned, backpressured implementation of Hermes's streaming-TTS ABC."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._tts_trim_leading_silence = (self.config.extra or {}).get(
            "tts_trim_leading_silence", False,
        )
        if not isinstance(self._tts_trim_leading_silence, bool):
            raise ValueError("tts_trim_leading_silence must be a boolean")
        self._tts_streams: dict[str, AudioSinkHandle] = {}
        self._tts_lifecycle_lock = asyncio.Lock()

    def supports_streaming_tts(self, chat_id: str, audio_format: AudioFormat) -> bool:
        return bool(
            audio_format.channels == 1 and audio_format.sample_width == 2
            and isinstance(audio_format.sample_rate, int)
            and 8_000 <= audio_format.sample_rate <= 96_000
            and self._tts_endpoint(chat_id) is not None
        )

    async def begin_streaming_tts(self, chat_id, audio_format, metadata=None):
        if not self.supports_streaming_tts(chat_id, audio_format):
            return None
        async with self._tts_lifecycle_lock:
            old = self._tts_streams.get(chat_id)
            if old is not None:
                await self._abort_tts_handle(old)
            endpoint = self._tts_endpoint(chat_id)
            if endpoint is None:
                return None
            owner, protocol, sink = endpoint
            handle = AudioSinkHandle(
                chat_id=chat_id, audio_format=audio_format, owner=owner,
                protocol=protocol, sink=sink, framer=PcmFramer(audio_format),
                leading_silence=(LeadingSilenceTrimmer()
                                 if getattr(self, "_tts_trim_leading_silence", False) else None),
            )
            self._tts_streams[chat_id] = handle
            return handle

    def _tts_current(self, handle: AudioSinkHandle) -> bool:
        endpoint = self._tts_endpoint(handle.chat_id)
        return bool(
            not handle.aborted and not handle.finished
            and self._tts_streams.get(handle.chat_id) is handle
            and endpoint is not None
            and all(a is b for a, b in zip(endpoint, (handle.owner, handle.protocol, handle.sink)))
        )

    async def _write_tts_frames(self, handle, frames):
        for pcm in frames:
            if not self._tts_current(handle):
                return
            if not handle.started:
                # A cancel/replacement must not finish between publishing the
                # start event and recording which response owns that event.
                async with self._tts_lifecycle_lock:
                    if not self._tts_current(handle):
                        return
                    self._tts_pause(handle, True)
                    await handle.protocol.output_started()
                    handle.response_id = handle.protocol.active_response_id
                    handle.started = True
            if not self._tts_current(handle):
                return
            handle.pending = asyncio.create_task(self._tts_write_frame(handle, pcm))
            try:
                await asyncio.wait_for(handle.pending, timeout=SINK_TIMEOUT)
            finally:
                handle.pending = None
            if not self._tts_current(handle):
                return
            if not handle.audible:
                trimmed_ms = handle.leading_silence.trimmed_frames * 20 if handle.leading_silence else 0
                logger.info("[%s] streaming TTS first PCM: %.3fs from stream open; trimmed=%dms",
                            handle.chat_id, time.monotonic() - handle.opened_at, trimmed_ms)
            handle.audible = True
            handle.pcm_bytes += len(pcm)

    async def write_streaming_tts(self, handle, chunk):
        if self._tts_current(handle):
            if chunk and handle.first_input_at is None:
                handle.first_input_at = time.monotonic()
                logger.info("[%s] streaming TTS first provider PCM: %.3fs from stream open",
                            handle.chat_id, handle.first_input_at - handle.opened_at)
            await self._write_tts_frames(handle, self._tts_frames(handle, handle.framer.feed(chunk)))

    def _tts_frames(self, handle, frames, *, final=False):
        if handle.leading_silence is None:
            yield from frames
            return
        yield from handle.leading_silence.feed(frames)
        if final:
            yield from handle.leading_silence.finish()

    async def finish_streaming_tts(self, handle, *, interrupted=False):
        if interrupted:
            await self.abort_streaming_tts(handle)
            return
        if not self._tts_current(handle):
            return
        await self._write_tts_frames(handle, self._tts_frames(handle, handle.framer.finish(), final=True))
        if not self._tts_current(handle):
            return
        await asyncio.wait_for(self._tts_drain(handle), timeout=SINK_TIMEOUT)
        if handle.started:
            await asyncio.sleep(self._tts_echo_guard)
        async with self._tts_lifecycle_lock:
            if self._tts_current(handle):
                await self._finish_tts_handle(handle)

    async def _finish_tts_handle(self, handle):
        # Stop audio only. Hermes sends the final transcript and completes the
        # response later; completing it here creates a duplicate response.
        if handle.started and handle.protocol.active_response_id == handle.response_id:
            await handle.protocol.output_playback_stopped(handle.response_id)
        if self._tts_streams.get(handle.chat_id) is handle:
            self._tts_pause(handle, False)
            self._tts_streams.pop(handle.chat_id)
        handle.finished = True
        logger.info("[%s] streaming TTS finished: %.2fs PCM, aborted=%s",
                    handle.chat_id, handle.pcm_bytes / (OUTPUT_RATE * 2), handle.aborted)

    async def _abort_tts_handle(self, handle):
        if handle.finished:
            return
        handle.aborted = True
        if handle.leading_silence is not None:
            handle.leading_silence.discard()
        if handle.pending is not None:
            handle.pending.cancel()
            await asyncio.gather(handle.pending, return_exceptions=True)
        self._tts_clear(handle)
        await self._finish_tts_handle(handle)

    async def abort_streaming_tts(self, handle, error=None):
        async with self._tts_lifecycle_lock:
            if self._tts_streams.get(handle.chat_id) is handle:
                await self._abort_tts_handle(handle)

    async def _abort_tts_for_chat(self, chat_id):
        handle = getattr(self, "_tts_streams", {}).get(chat_id)
        if handle is not None:
            await self.abort_streaming_tts(handle)


class RealtimeStreamingTTSMixin(StreamingTTSMixin):
    _tts_echo_guard = 0.75

    def _tts_endpoint(self, chat_id):
        call = self._calls.get(chat_id)
        if call is None or call.closed:
            return None
        return call, call.protocol, call.output_track

    def _tts_pause(self, handle, paused):
        # A closed call's flag is harmless; never mutate a replacement call.
        handle.owner.paused = paused

    async def _tts_write_frame(self, handle, pcm):
        await handle.sink.enqueue_pcm(pcm)

    async def _tts_drain(self, handle):
        await handle.sink.drained()

    def _tts_clear(self, handle):
        handle.sink.clear()


class LiveKitStreamingTTSMixin(StreamingTTSMixin):
    _tts_echo_guard = 0.3

    def _tts_endpoint(self, chat_id):
        if (chat_id != self._room_name or self._room is None
                or self._audio_source is None or self._realtime_protocol is None):
            return None
        return self._room, self._realtime_protocol, self._audio_source

    def _tts_pause(self, handle, paused):
        if self._room is handle.owner and self._audio_source is handle.sink:
            self._paused = paused

    async def _tts_write_frame(self, handle, pcm):
        from livekit import rtc

        await handle.sink.capture_frame(rtc.AudioFrame(
            data=pcm, sample_rate=OUTPUT_RATE, num_channels=1,
            samples_per_channel=len(pcm) // 2,
        ))

    async def _tts_drain(self, handle):
        await handle.sink.wait_for_playout()

    def _tts_clear(self, handle):
        handle.sink.clear_queue()
