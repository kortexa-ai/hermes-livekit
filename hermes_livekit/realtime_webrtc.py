"""OpenAI-compatible direct WebRTC transport for Hermes.

This module is a second Hermes platform adapter.  It deliberately uses only
the public platform-adapter API, so installing it does not require a patched
``hermes-agent``.  Signalling is compatible with ``POST /v1/realtime/calls``;
audio uses RTP and protocol events use the ``oai-events`` data channel.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Any, Optional

try:
    from aiohttp import web
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
    from av import AudioFrame, AudioResampler

    WEBRTC_AVAILABLE = True
except ImportError:
    web = None  # type: ignore[assignment]
    MediaStreamTrack = object  # type: ignore[assignment,misc]
    RTCPeerConnection = None  # type: ignore[assignment,misc]
    RTCSessionDescription = None  # type: ignore[assignment,misc]
    RTCConfiguration = None  # type: ignore[assignment,misc]
    RTCIceServer = None  # type: ignore[assignment,misc]
    AudioFrame = None  # type: ignore[assignment,misc]
    AudioResampler = None  # type: ignore[assignment,misc]
    WEBRTC_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import build_session_key

from .adapter import (
    MIN_SPEECH_DURATION,
    NUM_CHANNELS,
    RMS_SILENCE_FLOOR,
    SAMPLE_RATE,
    SILENCE_THRESHOLD_SECONDS,
    _compute_rms,
)
from .realtime_protocol import MAX_INSTRUCTIONS_BYTES, RealtimeProtocol
from .direct_tools import DirectToolBridge, DirectToolError, parse_direct_tools
from .vad import AdaptiveRmsGate


logger = logging.getLogger("gateway.platforms.realtime")
CLIENT_IDENTITY = "webrtc-client"
MAX_SDP_BYTES = 256 * 1024
MAX_SESSION_BYTES = 512 * 1024
DEFAULT_MAX_CALLS = 8
DEFAULT_MAX_CALL_SECONDS = 2 * 60 * 60
# Queued RTP frames have left the server when ``drained()`` returns, but the
# remote jitter buffer and speaker can still be playing their tail.  Keep
# capture suppressed briefly so that tail cannot become a new user turn.
OUTPUT_ECHO_GUARD_SECONDS = 0.75
PROXY_PRINCIPAL_HEADER = "X-Kortexa-Internal-Principal"
PROXY_CALL_HEADER = "X-Kortexa-Internal-Call-Id"
PROXY_TIMESTAMP_HEADER = "X-Kortexa-Internal-Timestamp"
PROXY_SIGNATURE_HEADER = "X-Kortexa-Internal-Signature"
PROXY_SIGNATURE_VERSION = "v1"
PROXY_TIMESTAMP_SKEW_SECONDS = 30
MAX_PROXY_PRINCIPAL_BYTES = 256
MAX_ICE_SERVERS = 8
MAX_ICE_URLS = 8
_CALL_ID_PATTERN = re.compile(r"^rt_[A-Za-z0-9_-]{8,96}$")
_PROFILE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ICE_URL_PATTERN = re.compile(r"^(?:stun|stuns|turn|turns):", re.IGNORECASE)


def _configured_int(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def check_realtime_requirements() -> bool:
    """Return whether the pinned HTTP, WebRTC, and media dependencies load."""
    return WEBRTC_AVAILABLE


def _gateway_profile_name(home: str | os.PathLike[str] | None = None) -> str:
    """Return the fixed profile represented by this gateway process."""
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    resolved = Path(home).expanduser().resolve(strict=False)
    if resolved.parent.name == "profiles" and _PROFILE_ID_PATTERN.fullmatch(resolved.name):
        return resolved.name
    return "default"


def _proxy_signature_payload(
    timestamp: str,
    call_id: str,
    principal: str,
    sdp: str,
    session_json: str,
) -> bytes:
    sdp_hash = hashlib.sha256(sdp.encode("utf-8")).hexdigest()
    session_hash = hashlib.sha256(session_json.encode("utf-8")).hexdigest()
    return (
        f"{PROXY_SIGNATURE_VERSION}\n{timestamp}\n{call_id}\n{principal}\n"
        f"{sdp_hash}\n{session_hash}"
    ).encode("utf-8")


def _parse_ice_servers(raw: Any) -> list[Any]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        try:
            values = json.loads(raw)
        except ValueError as exc:
            raise ValueError("HERMES_REALTIME_ICE_SERVERS must be valid JSON") from exc
    else:
        values = raw
    if not isinstance(values, list) or len(values) > MAX_ICE_SERVERS:
        raise ValueError(
            f"HERMES_REALTIME_ICE_SERVERS must be an array of at most {MAX_ICE_SERVERS} entries"
        )
    servers: list[Any] = []
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError(f"HERMES_REALTIME_ICE_SERVERS[{index}] must be an object")
        if set(value) - {"urls", "username", "credential"}:
            raise ValueError(f"HERMES_REALTIME_ICE_SERVERS[{index}] has unknown fields")
        urls = value.get("urls")
        url_values = [urls] if isinstance(urls, str) else urls
        if (
            not isinstance(url_values, list)
            or not url_values
            or len(url_values) > MAX_ICE_URLS
            or not all(
                isinstance(url, str)
                and len(url) <= 2048
                and _ICE_URL_PATTERN.match(url)
                for url in url_values
            )
        ):
            raise ValueError(f"HERMES_REALTIME_ICE_SERVERS[{index}].urls is invalid")
        username = value.get("username")
        credential = value.get("credential")
        if username is not None and (not isinstance(username, str) or len(username) > 1024):
            raise ValueError(f"HERMES_REALTIME_ICE_SERVERS[{index}].username is invalid")
        if credential is not None and (not isinstance(credential, str) or len(credential) > 1024):
            raise ValueError(f"HERMES_REALTIME_ICE_SERVERS[{index}].credential is invalid")
        servers.append(
            RTCIceServer(
                urls=urls,
                username=username,
                credential=credential,
            )
        )
    return servers


class QueuedAudioTrack(MediaStreamTrack):
    """A paced aiortc audio track fed with 48 kHz mono signed PCM."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._timestamp = 0
        self._next_frame_at: float | None = None

    async def recv(self) -> Any:
        chunk = await self._queue.get()
        try:
            samples = len(chunk) // 2
            duration = samples / SAMPLE_RATE
            loop = asyncio.get_running_loop()
            now = loop.time()

            # RTCRtpSender calls recv() again as soon as it has encoded the
            # previous frame.  Timestamps alone do not pace aiortc, so without
            # this wait an entire reply is emitted as one RTP burst.  Apart
            # from making output-start/output-stop lie about playback time,
            # that burst overruns browser jitter buffers and sounds garbled.
            # Never try to catch up after scheduler stalls or an idle period:
            # resume from the current monotonic time instead.
            deadline = self._next_frame_at
            if deadline is None or deadline < now:
                deadline = now
            delay = deadline - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_frame_at = deadline + duration

            frame = AudioFrame(format="s16", layout="mono", samples=samples)
            frame.planes[0].update(chunk)
            frame.sample_rate = SAMPLE_RATE
            frame.pts = self._timestamp
            frame.time_base = Fraction(1, SAMPLE_RATE)
            self._timestamp += samples
            return frame
        finally:
            self._queue.task_done()

    async def enqueue_pcm(self, pcm: bytes) -> None:
        samples_per_frame = SAMPLE_RATE // 50
        bytes_per_frame = samples_per_frame * 2
        for offset in range(0, len(pcm), bytes_per_frame):
            chunk = pcm[offset : offset + bytes_per_frame]
            if len(chunk) < bytes_per_frame:
                chunk += b"\x00" * (bytes_per_frame - len(chunk))
            await self._queue.put(chunk)

    async def drained(self) -> None:
        await self._queue.join()

    def clear(self) -> None:
        self._next_frame_at = None
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            else:
                self._queue.task_done()


@dataclass
class RealtimeCall:
    adapter: "RealtimeWebRTCAdapter"
    call_id: str
    peer: Any
    output_track: QueuedAudioTrack
    protocol: RealtimeProtocol
    client_identity: str = CLIENT_IDENTITY
    tool_bridge: DirectToolBridge | None = None
    data_channel: Any = None
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    audio_buffer: bytearray = field(default_factory=bytearray)
    last_speech_at: float | None = None
    speaking: bool = False
    paused: bool = False
    input_muted: bool = False
    tts_completed: bool = False
    closed: bool = False
    vad: AdaptiveRmsGate = field(default_factory=AdaptiveRmsGate)
    vad_calibration_pcm: list[bytes] = field(default_factory=list)

    def spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def publish(self, event: dict[str, Any], _recipient: str | None) -> bool:
        channel = self.data_channel
        if channel is None or getattr(channel, "readyState", "") != "open":
            return False
        channel.send(json.dumps(event, separators=(",", ":")))
        return True

    def attach_data_channel(self, channel: Any) -> None:
        if getattr(channel, "label", "") != "oai-events":
            logger.debug("[%s] ignoring unexpected data channel %r", self.call_id, channel.label)
            return
        self.data_channel = channel

        @channel.on("open")
        def on_open() -> None:
            self.spawn(self.protocol.client_connected(self.client_identity))

        @channel.on("message")
        def on_message(message: Any) -> None:
            if isinstance(message, (str, bytes)):
                self.spawn(self.protocol.handle_client_message(message, self.client_identity))

        @channel.on("close")
        def on_close() -> None:
            self.protocol.client_disconnected(self.client_identity)

        if getattr(channel, "readyState", "") == "open":
            self.spawn(self.protocol.client_connected(self.client_identity))

    async def consume_audio(self, track: Any) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
        try:
            while not self.closed:
                frame = await track.recv()
                if self.paused or self.input_muted:
                    continue
                for converted in resampler.resample(frame):
                    size = converted.samples * NUM_CHANNELS * 2
                    pcm = bytes(converted.planes[0])[:size]
                    await self.accept_pcm(pcm)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if not self.closed:
                logger.debug("[%s] inbound audio ended: %s", self.call_id, exc)
        finally:
            if self.speaking and self.audio_buffer:
                await self.finish_utterance()

    async def accept_pcm(self, pcm: bytes) -> None:
        rms = _compute_rms(pcm)
        if not self.vad.ready:
            self.vad_calibration_pcm.append(pcm)
            if not self.vad.calibrate(rms):
                return
            logger.info(
                "[%s] adaptive VAD calibrated: noise_rms=%.1f start=%.1f stop=%.1f",
                self.call_id,
                self.vad.noise_rms,
                self.vad.start_threshold,
                self.vad.stop_threshold,
            )
            calibration_pcm, self.vad_calibration_pcm = self.vad_calibration_pcm, []
            for buffered in calibration_pcm:
                await self._accept_calibrated_pcm(buffered)
            return
        await self._accept_calibrated_pcm(pcm)

    async def _accept_calibrated_pcm(self, pcm: bytes) -> None:
        now = time.monotonic()
        rms = _compute_rms(pcm)
        if self.vad.is_speech(rms, speaking=self.speaking):
            if not self.speaking:
                self.audio_buffer.clear()
                self.speaking = True
                await self.protocol.speech_started(self.client_identity)
            self.last_speech_at = now
            self.audio_buffer.extend(pcm)
            return
        if not self.speaking:
            return
        self.audio_buffer.extend(pcm)
        if self.last_speech_at is not None and now - self.last_speech_at >= SILENCE_THRESHOLD_SECONDS:
            await self.finish_utterance()

    async def finish_utterance(self) -> None:
        pcm = bytes(self.audio_buffer)
        self.audio_buffer.clear()
        self.last_speech_at = None
        was_speaking, self.speaking = self.speaking, False
        if was_speaking:
            await self.protocol.speech_stopped(self.client_identity)
        duration = len(pcm) / (SAMPLE_RATE * NUM_CHANNELS * 2)
        if duration >= MIN_SPEECH_DURATION:
            self.spawn(self.adapter.process_voice(self, pcm))

    async def set_input_audio_state(self, muted: bool) -> None:
        """Apply an explicit client mute boundary to capture and endpointing."""
        if muted == self.input_muted:
            return
        self.input_muted = muted
        if muted:
            if self.speaking and self.audio_buffer:
                await self.finish_utterance()
            else:
                self.audio_buffer.clear()
                self.last_speech_at = None
                self.speaking = False
            self.vad_calibration_pcm.clear()
            logger.info("[%s] input muted by client", self.call_id)
            return

        # Calibrate against the real microphone after it replaces any muted
        # placeholder track. This avoids learning digital zero as room noise.
        self.audio_buffer.clear()
        self.last_speech_at = None
        self.speaking = False
        self.vad = AdaptiveRmsGate(minimum_floor=RMS_SILENCE_FLOOR)
        self.vad_calibration_pcm.clear()
        logger.info("[%s] input unmuted; adaptive VAD reset", self.call_id)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        current = asyncio.current_task()
        for task in list(self.tasks):
            if task is not current:
                task.cancel()
        self.output_track.clear()
        if self.tool_bridge is not None:
            self.tool_bridge.close()
        await self.protocol.close()
        try:
            await self.peer.close()
        except Exception:
            pass


class RealtimeWebRTCAdapter(BasePlatformAdapter):
    """Hermes platform serving direct OpenAI-compatible WebRTC calls."""

    # An active call is a persistent outbound channel: Hermes may inject a
    # background completion turn and this adapter can publish its transcript
    # and TTS response over the existing peer connection.
    supports_async_delivery = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("realtime"))
        extra = config.extra or {}
        self.config.extra = extra
        self.config.extra["group_sessions_per_user"] = False
        self._host = str(extra.get("host") or os.getenv("HERMES_REALTIME_HOST", "127.0.0.1"))
        self._port = _configured_int(
            extra.get("port", os.getenv("HERMES_REALTIME_PORT", "8091")),
            8091,
            minimum=0,
        )
        self._api_key = str(extra.get("api_key") or os.getenv("HERMES_REALTIME_API_KEY", ""))
        self._proxy_signing_key = str(
            extra.get("proxy_signing_key")
            or os.getenv("HERMES_REALTIME_PROXY_SIGNING_KEY", "")
        )
        self._ice_servers = _parse_ice_servers(
            extra.get("ice_servers", os.getenv("HERMES_REALTIME_ICE_SERVERS", ""))
        )
        self._max_calls = _configured_int(
            extra.get("max_calls", os.getenv("HERMES_REALTIME_MAX_CALLS")),
            DEFAULT_MAX_CALLS,
        )
        self._max_call_seconds = _configured_int(
            extra.get("max_call_seconds", os.getenv("HERMES_REALTIME_MAX_CALL_SECONDS")),
            DEFAULT_MAX_CALL_SECONDS,
        )
        self._profile_name = _gateway_profile_name()
        self._calls: dict[str, RealtimeCall] = {}
        self._pending_calls = 0
        self._proxy_call_ids: dict[str, float] = {}
        self._runner: Any = None
        self._site: Any = None

    def _should_auto_tts_for_chat(self, chat_id: str) -> bool:
        return chat_id not in self._auto_tts_disabled_chats

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not WEBRTC_AVAILABLE:
            logger.warning("[%s] direct WebRTC dependencies are not installed", self.name)
            return False
        if not self._api_key:
            logger.error("[%s] HERMES_REALTIME_API_KEY is required", self.name)
            return False
        try:
            application = web.Application(client_max_size=MAX_SDP_BYTES + MAX_SESSION_BYTES)
            application.router.add_get("/v1/realtime/discovery", self._discovery)
            application.router.add_route("OPTIONS", "/v1/realtime/calls", self._options)
            application.router.add_post("/v1/realtime/calls", self._create_call)
            application.router.add_route("OPTIONS", "/realtime/calls", self._options)
            application.router.add_post("/realtime/calls", self._create_call)
            self._runner = web.AppRunner(application, access_log=None)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
            self._mark_connected()
            logger.info("[%s] listening on http://%s:%d/v1/realtime/calls", self.name, self._host, self._port)
            return True
        except Exception as exc:
            logger.error("[%s] failed to start direct WebRTC listener: %s", self.name, exc)
            await self.disconnect()
            return False

    async def disconnect(self) -> None:
        self._mark_disconnected()
        calls, self._calls = list(self._calls.values()), {}
        for call in calls:
            await call.close()
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None

    @staticmethod
    def _cors_headers() -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
        }

    async def _options(self, _request: Any) -> Any:
        return web.Response(status=204, headers=self._cors_headers())

    def _authorized(self, request: Any) -> bool:
        if not self._api_key:
            return True
        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        return header.startswith(prefix) and hmac.compare_digest(header[len(prefix) :], self._api_key)

    async def _discovery(self, request: Any) -> Any:
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized")
        return web.json_response(
            {
                "version": 1,
                "profile": self._profile_name,
                "realtime_path": "/v1/realtime/calls",
            },
            headers={"Cache-Control": "no-store"},
        )

    async def _read_offer(self, request: Any) -> tuple[str, dict[str, Any], str]:
        content_type = request.content_type.lower()
        if content_type == "application/sdp":
            raw = await request.read()
            if len(raw) > MAX_SDP_BYTES:
                raise web.HTTPRequestEntityTooLarge(max_size=MAX_SDP_BYTES, actual_size=len(raw))
            return raw.decode("utf-8"), {}, "{}"
        if not content_type.startswith("multipart/"):
            raise web.HTTPUnsupportedMediaType(text="expected multipart/form-data or application/sdp")
        reader = await request.multipart()
        sdp = ""
        session: dict[str, Any] = {}
        session_json = "{}"
        async for part in reader:
            if part.name == "sdp":
                raw = await part.read(decode=False)
                if len(raw) > MAX_SDP_BYTES:
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_SDP_BYTES, actual_size=len(raw))
                sdp = raw.decode("utf-8")
            elif part.name == "session":
                raw = await part.read(decode=False)
                if len(raw) > MAX_SESSION_BYTES:
                    raise web.HTTPRequestEntityTooLarge(max_size=MAX_SESSION_BYTES, actual_size=len(raw))
                try:
                    session_json = raw.decode("utf-8")
                    decoded = json.loads(session_json)
                except (TypeError, ValueError):
                    raise web.HTTPBadRequest(text="session must be a JSON object") from None
                if not isinstance(decoded, dict):
                    raise web.HTTPBadRequest(text="session must be a JSON object")
                session = decoded
            else:
                await part.release()
        if not sdp.strip():
            raise web.HTTPBadRequest(text="missing sdp")
        return sdp, session, session_json

    def _client_identity(
        self,
        request: Any,
        sdp: str,
        session_json: str,
    ) -> str:
        names = (
            PROXY_PRINCIPAL_HEADER,
            PROXY_CALL_HEADER,
            PROXY_TIMESTAMP_HEADER,
            PROXY_SIGNATURE_HEADER,
        )
        values = {name: request.headers.get(name) for name in names}
        supplied = [name for name, value in values.items() if value is not None]
        if not supplied:
            return CLIENT_IDENTITY
        if len(supplied) != len(names) or not self._proxy_signing_key:
            raise web.HTTPUnauthorized(text="invalid internal proxy metadata")

        principal = values[PROXY_PRINCIPAL_HEADER] or ""
        call_id = values[PROXY_CALL_HEADER] or ""
        timestamp = values[PROXY_TIMESTAMP_HEADER] or ""
        signature = values[PROXY_SIGNATURE_HEADER] or ""
        if not _CALL_ID_PATTERN.fullmatch(call_id):
            raise web.HTTPUnauthorized(text="invalid internal proxy metadata")
        try:
            timestamp_value = int(timestamp)
            principal_bytes = base64.urlsafe_b64decode(principal + "=" * (-len(principal) % 4))
            principal_text = principal_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPUnauthorized(text="invalid internal proxy metadata") from None
        if (
            not principal_text
            or len(principal_bytes) > MAX_PROXY_PRINCIPAL_BYTES
            or abs(int(time.time()) - timestamp_value) > PROXY_TIMESTAMP_SKEW_SECONDS
        ):
            raise web.HTTPUnauthorized(text="invalid internal proxy metadata")
        expected = hmac.new(
            self._proxy_signing_key.encode("utf-8"),
            _proxy_signature_payload(timestamp, call_id, principal, sdp, session_json),
            hashlib.sha256,
        ).hexdigest()
        if not signature.startswith(f"{PROXY_SIGNATURE_VERSION}=") or not hmac.compare_digest(
            signature[len(PROXY_SIGNATURE_VERSION) + 1 :], expected
        ):
            raise web.HTTPUnauthorized(text="invalid internal proxy metadata")

        now = time.time()
        self._proxy_call_ids = {
            key: seen_at
            for key, seen_at in self._proxy_call_ids.items()
            if now - seen_at <= PROXY_TIMESTAMP_SKEW_SECONDS * 2
        }
        if call_id in self._proxy_call_ids:
            raise web.HTTPConflict(text="internal proxy call was already used")
        self._proxy_call_ids[call_id] = now
        digest = hashlib.sha256(principal_bytes).hexdigest()[:24]
        return f"kortexa-{digest}"

    async def _create_call(self, request: Any) -> Any:
        headers = self._cors_headers()
        if not self._authorized(request):
            return web.Response(status=401, text="unauthorized", headers=headers)
        if len(self._calls) + self._pending_calls >= self._max_calls:
            return web.Response(status=429, text="too many active calls", headers=headers)
        self._pending_calls += 1
        try:
            sdp, session, session_json = await self._read_offer(request)
            client_identity = self._client_identity(request, sdp, session_json)
            if session.get("type", "realtime") != "realtime":
                raise web.HTTPBadRequest(text="only realtime sessions are supported")
            tools, tool_choice = parse_direct_tools(session)
            instructions = session.get("instructions", "")
            if (
                not isinstance(instructions, str)
                or len(instructions.encode("utf-8")) > MAX_INSTRUCTIONS_BYTES
            ):
                raise DirectToolError("instructions must be a bounded string")
            call_id = f"call_{uuid.uuid4().hex}"
            peer = RTCPeerConnection(RTCConfiguration(iceServers=self._ice_servers))
            output_track = QueuedAudioTrack()
            call: RealtimeCall

            async def publish(event: dict[str, Any], recipient: str | None) -> bool:
                return await call.publish(event, recipient)

            protocol = RealtimeProtocol(
                session_id=call_id,
                model="hermes",
                voice="hermes",
                publish=publish,
                instructions=instructions,
                tool_choice=tool_choice,
                on_text_input=lambda text, _identity: self.process_text(call, text),
                on_response_requested=lambda _identity: self.process_text(
                    call,
                    "Follow the session instructions and respond now.",
                ),
                on_response_cancelled=lambda _identity: self.cancel_call_response(call),
                on_input_audio_state=lambda muted, _identity: call.set_input_audio_state(muted),
            )
            call = RealtimeCall(
                self,
                call_id,
                peer,
                output_track,
                protocol,
                client_identity=client_identity,
            )
            if tools:
                bridge = DirectToolBridge(
                    session_id=self._session_key_for_call(call),
                    protocol=protocol,
                )
                bridge.register(tools)
                call.tool_bridge = bridge
            self._calls[call_id] = call
            peer.addTrack(output_track)

            @peer.on("datachannel")
            def on_datachannel(channel: Any) -> None:
                call.attach_data_channel(channel)

            @peer.on("track")
            def on_track(track: Any) -> None:
                if getattr(track, "kind", "") == "audio":
                    call.spawn(call.consume_audio(track))

            @peer.on("connectionstatechange")
            async def on_connectionstatechange() -> None:
                if peer.connectionState in {"failed", "closed", "disconnected"}:
                    self._calls.pop(call_id, None)
                    await call.close()

            call.spawn(self._expire_call(call))
            await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            await peer.setLocalDescription(await peer.createAnswer())
            response_headers = {
                **headers,
                "Location": f"/v1/realtime/calls/{call_id}",
            }
            return web.Response(
                status=201,
                text=peer.localDescription.sdp,
                content_type="application/sdp",
                headers=response_headers,
            )
        except DirectToolError as exc:
            if "call" in locals():
                await call.close()
            return web.Response(status=400, text=str(exc), headers=headers)
        except web.HTTPException:
            raise
        except Exception as exc:
            logger.warning("[%s] rejected WebRTC offer: %s", self.name, exc)
            if "call_id" in locals():
                failed = self._calls.pop(call_id, None)
                if failed is None and "call" in locals():
                    failed = call
                if failed is not None:
                    await failed.close()
            return web.Response(status=400, text="invalid WebRTC offer", headers=headers)
        finally:
            self._pending_calls -= 1

    async def _expire_call(self, call: RealtimeCall) -> None:
        await asyncio.sleep(self._max_call_seconds)
        self._calls.pop(call.call_id, None)
        await call.close()

    async def process_voice(self, call: RealtimeCall, pcm: bytes) -> None:
        path = ""
        try:
            from .adapter import _pcm_to_wav
            from tools.transcription_tools import transcribe_audio

            directory = os.path.join(tempfile.gettempdir(), "hermes_livekit")
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"utterance_{uuid.uuid4().hex[:12]}.wav")
            with open(path, "wb") as file:
                file.write(_pcm_to_wav(pcm, SAMPLE_RATE, NUM_CHANNELS))
            result = await asyncio.to_thread(transcribe_audio, path)
            transcript = (
                (result.get("transcript") or result.get("text") or "").strip()
                if isinstance(result, dict)
                else ""
            )
            if transcript:
                await call.protocol.user_transcript(transcript, call.client_identity)
                await self._dispatch_text(call, transcript, MessageType.VOICE)
        except Exception as exc:
            logger.error("[%s] voice processing failed: %s", call.call_id, exc)
            await call.protocol.response_failed()
        finally:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    async def process_text(self, call: RealtimeCall, text: str) -> None:
        await self._dispatch_text(call, text, MessageType.TEXT)

    async def handle_message(self, event: MessageEvent) -> None:
        """Defer an internal completion wake while this call has live speech."""
        call = self._calls.get(event.source.chat_id)
        if event.internal and call is not None and call.speaking:
            call.spawn(self._deliver_internal_when_silent(call, event))
            return
        await super().handle_message(event)

    async def _deliver_internal_when_silent(
        self,
        call: RealtimeCall,
        event: MessageEvent,
    ) -> None:
        while self._calls.get(call.call_id) is call and not call.closed and call.speaking:
            await asyncio.sleep(0.05)
        if self._calls.get(call.call_id) is call and not call.closed:
            await super().handle_message(event)

    async def _dispatch_text(self, call: RealtimeCall, text: str, kind: MessageType) -> None:
        source = self._source_for_call(call)
        prompts = [call.protocol.instructions.strip()]
        if call.tool_bridge is not None and call.protocol.tool_choice != "none":
            prompts.append(call.tool_bridge.prompt_hint().strip())
        channel_prompt = "\n\n".join(prompt for prompt in prompts if prompt) or None
        event = MessageEvent(
            text=text,
            message_type=kind,
            source=source,
            message_id=uuid.uuid4().hex[:12],
            timestamp=datetime.now(tz=timezone.utc),
            channel_prompt=channel_prompt,
        )
        await call.protocol.response_started()
        await self.handle_message(event)

    def _source_for_call(self, call: RealtimeCall) -> Any:
        return self.build_source(
            chat_id=call.call_id,
            chat_name=call.call_id,
            chat_type="dm",
            user_id=call.client_identity,
            user_name=call.client_identity,
        )

    def _session_key_for_call(self, call: RealtimeCall) -> str:
        source = self._source_for_call(call)
        return build_session_key(
            source,
            group_sessions_per_user=False,
            thread_sessions_per_user=False,
            profile=self._session_key_profile(source),
        )

    async def cancel_call_response(self, call: RealtimeCall) -> None:
        await self.cancel_session_processing(self._session_key_for_call(call))
        call.output_track.clear()
        call.paused = False

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        call = self._calls.get(chat_id)
        if call is None:
            return SendResult(success=False, error="Realtime call is closed")
        await call.protocol.assistant_transcript(content)
        if call.tts_completed:
            call.tts_completed = False
            await call.protocol.output_stopped()
        return SendResult(success=True, message_id=uuid.uuid4().hex[:12])

    async def play_tts(
        self,
        chat_id: str,
        audio_path: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        caption: Optional[str] = None,
        **kwargs: Any,
    ) -> SendResult:
        call = self._calls.get(chat_id)
        if call is None:
            return SendResult(success=False, error="Realtime call is closed")
        from .adapter import LiveKitAdapter

        pcm = await asyncio.to_thread(LiveKitAdapter._decode_audio_to_pcm, audio_path)
        if not pcm:
            await call.protocol.response_failed()
            return SendResult(success=False, error="Failed to decode audio")
        try:
            call.paused = True
            call.tts_completed = False
            await call.protocol.output_started()
            await call.output_track.enqueue_pcm(pcm)
            await call.output_track.drained()
            await asyncio.sleep(OUTPUT_ECHO_GUARD_SECONDS)
            await call.protocol.output_stopped()
            call.tts_completed = True
            return SendResult(success=True, message_id=uuid.uuid4().hex[:12])
        finally:
            call.paused = False

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SendResult:
        """Deliver voice attachments on the call's native audio track."""
        return await self.play_tts(
            chat_id=chat_id,
            audio_path=audio_path,
            caption=caption,
            reply_to=reply_to,
            metadata=metadata,
            **kwargs,
        )

    def prepare_tts_text(self, text: str) -> str:
        from .adapter import LiveKitAdapter

        return LiveKitAdapter.prepare_tts_text(self, text)

    async def send_typing(self, chat_id: str, metadata: Any = None) -> None:
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        call = self._calls.get(chat_id)
        return {
            "name": chat_id,
            "type": "dm",
            "chat_id": chat_id,
            "participants": [call.client_identity] if call is not None else [],
        }
