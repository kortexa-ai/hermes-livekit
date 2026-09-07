from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from hermes_livekit.adapter import LiveKitAdapter
import hermes_livekit.adapter as adapter_module
from gateway.platforms.base import ProcessingOutcome


class LocalParticipant:
    def __init__(self) -> None:
        self.messages: list[tuple[dict, dict]] = []

    async def publish_data(self, data: bytes, **options) -> None:
        self.messages.append((json.loads(data), options))


def conference_adapter() -> tuple[LiveKitAdapter, LocalParticipant]:
    local = LocalParticipant()
    adapter = object.__new__(LiveKitAdapter)
    adapter.platform = SimpleNamespace(value="livekit")
    adapter._room_name = "room-a"
    adapter._room_generation = 1
    adapter._room = SimpleNamespace(
        local_participant=local,
        remote_participants={"client-a": object()},
    )
    adapter._audio_source = None
    adapter._paused = False
    adapter._silence_duration = 1.5
    adapter._audio_buffers = {"client-a": bytearray()}
    adapter._last_audio_time = {}
    adapter._speaking_participants = set()
    adapter._audio_gates = {}
    adapter._muted_inputs = set()
    adapter._audio_ready = asyncio.Event()
    adapter._realtime_protocol = adapter._new_realtime_protocol()
    return adapter, local


@pytest.mark.asyncio
async def test_conference_input_audio_state_is_scoped_and_acknowledged() -> None:
    adapter, local = conference_adapter()
    adapter._process_voice_input = AsyncMock()
    adapter._audio_buffers["client-a"].extend(b"\x00\x00" * 48_000)
    adapter._speaking_participants.add("client-a")

    await adapter._handle_input_audio_state(
        {"type": "hermes.input_audio.state", "muted": True},
        "client-a",
    )
    assert "client-a" in adapter._muted_inputs
    assert adapter._audio_buffers["client-a"] == b""
    acknowledgement = next(
        (message, options)
        for message, options in local.messages
        if message["type"] == "hermes.input_audio.state_updated"
    )
    assert acknowledgement[0] == {
        "type": "hermes.input_audio.state_updated",
        "muted": True,
    }
    assert acknowledgement[1]["topic"] == adapter.DATA_CHANNEL_EXTENSIONS_TOPIC
    assert acknowledgement[1]["destination_identities"] == ["client-a"]
    await asyncio.sleep(0)
    adapter._process_voice_input.assert_awaited_once()

    await adapter._handle_input_audio_state(
        {"type": "hermes.input_audio.state", "muted": False},
        "client-a",
    )
    assert "client-a" not in adapter._muted_inputs
    assert adapter._audio_gates["client-a"].ready is False


@pytest.mark.asyncio
async def test_lifecycle_uses_shared_conference_event_topic() -> None:
    adapter, local = conference_adapter()
    protocol = adapter._realtime_protocol

    await protocol.client_connected("client-a")
    await adapter._publish_agent_event("agent:listening-start", {"identity": "client-a"})
    await adapter._publish_agent_event("agent:listening-stop", {"identity": "client-a"})
    await adapter._publish_agent_event(
        "agent:user-transcript",
        {"identity": "client-a", "transcript": "hello", "final": True},
    )
    await adapter._publish_agent_event("agent:thinking-start")
    await adapter._publish_agent_event("agent:speaking-start")
    await adapter._publish_agent_event("agent:speaking-stop")
    await adapter.send("room-a", "hi")

    assert [message["type"] for message, _ in local.messages] == [
        "session.created",
        "input_audio_buffer.speech_started",
        "input_audio_buffer.speech_stopped",
        "conversation.item.added",
        "conversation.item.input_audio_transcription.completed",
        "conversation.item.done",
        "response.created",
        "response.output_item.added",
        "conversation.item.added",
        "response.content_part.added",
        "output_audio_buffer.started",
        "output_audio_buffer.stopped",
        "response.output_audio_transcript.done",
        "response.content_part.done",
        "conversation.item.done",
        "response.output_item.done",
        "response.done",
    ]
    assert all(
        options["topic"] == LiveKitAdapter.DATA_CHANNEL_EVENTS_TOPIC
        for _, options in local.messages
    )
    assert local.messages[0][1]["destination_identities"] == ["client-a"]
    assert all(
        options["destination_identities"] == []
        for _, options in local.messages[1:]
    )


@pytest.mark.asyncio
async def test_inbound_event_uses_authoritative_participant_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, local = conference_adapter()
    scheduled: list[object] = []
    monkeypatch.setattr(asyncio, "create_task", lambda coroutine: scheduled.append(coroutine))
    packet = SimpleNamespace(
        topic=LiveKitAdapter.DATA_CHANNEL_EVENTS_TOPIC,
        participant=SimpleNamespace(identity="client-a"),
        data=json.dumps({"type": "unknown", "event_id": "event-1"}).encode(),
    )

    adapter._on_data_received(packet)
    await scheduled.pop()

    message, options = local.messages[-1]
    assert message["type"] == "error"
    assert message["error"]["event_id"] == "event-1"
    assert options["destination_identities"] == ["client-a"]


def test_legacy_hermes_topics_are_not_client_protocol_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter, _local = conference_adapter()
    monkeypatch.setattr(
        asyncio,
        "create_task",
        lambda _coroutine: pytest.fail("legacy topic scheduled protocol work"),
    )
    packet = SimpleNamespace(
        topic="hermes-control",
        participant=SimpleNamespace(identity="client-a"),
        data=json.dumps({"type": "client:tool-register"}).encode(),
    )

    adapter._on_data_received(packet)



@pytest.mark.asyncio
async def test_extension_events_stay_off_conversation_topic() -> None:
    adapter, local = conference_adapter()

    await adapter._publish_agent_event(
        "agent:frame-captured",
        {"identity": "client-a", "width": 1, "height": 1},
    )

    message, options = local.messages[-1]
    assert message["type"] == "agent:frame-captured"
    assert options["topic"] == LiveKitAdapter.DATA_CHANNEL_EXTENSIONS_TOPIC


@pytest.mark.asyncio
async def test_tts_waits_for_playout_and_transcript_before_response_done(monkeypatch):
    adapter, local = conference_adapter()
    source = SimpleNamespace(capture_frame=AsyncMock(), wait_for_playout=AsyncMock(), clear_queue=Mock())
    adapter._audio_source = source
    monkeypatch.setattr(adapter, "_decode_audio_to_pcm", lambda path: b"\x00\x00" * 960)

    async def echo_guard(delay):
        source.wait_for_playout.assert_awaited_once()
        assert adapter._paused

    monkeypatch.setattr(adapter_module.asyncio, "sleep", echo_guard)
    result = await adapter.play_tts("room-a", "unused.wav")
    assert result.success
    assert not adapter._paused
    assert not any(message["type"] == "response.done" for message, _ in local.messages)
    await adapter.send("room-a", "hello")
    assert sum(message["type"] == "response.created" for message, _ in local.messages) == 1
    assert sum(message["type"] == "response.done" for message, _ in local.messages) == 1


@pytest.mark.asyncio
async def test_cancelled_tts_clears_source_and_unpauses_capture(monkeypatch):
    adapter, _ = conference_adapter()
    source = SimpleNamespace(
        capture_frame=AsyncMock(side_effect=asyncio.CancelledError),
        wait_for_playout=AsyncMock(), clear_queue=Mock(),
    )
    adapter._audio_source = source
    monkeypatch.setattr(adapter, "_decode_audio_to_pcm", lambda path: b"\x00\x00" * 960)
    with pytest.raises(asyncio.CancelledError):
        await adapter.play_tts("room-a", "unused.wav")
    source.clear_queue.assert_called_once()
    assert not adapter._paused


@pytest.mark.asyncio
async def test_response_cancel_uses_synchronous_sdk_clear_queue(monkeypatch):
    adapter, _ = conference_adapter()
    adapter._audio_source = SimpleNamespace(clear_queue=Mock())
    adapter.build_source = Mock(return_value=object())
    adapter._session_key_profile = Mock(return_value="default")
    adapter.cancel_session_processing = AsyncMock()
    monkeypatch.setattr(adapter_module, "build_session_key", lambda *args, **kwargs: "room-key")
    await adapter._cancel_realtime_response("client-a")
    adapter.cancel_session_processing.assert_awaited_once_with("room-key")
    assert adapter._audio_source.clear_queue.call_count == 2


@pytest.mark.parametrize("topic", ["conference.events", "conference.extensions", "conference.tools"])
def test_old_room_and_departed_participant_packets_are_ignored(monkeypatch, topic):
    adapter, _ = conference_adapter()
    monkeypatch.setattr(asyncio, "create_task", lambda coro: pytest.fail("stale packet scheduled work"))
    packet = SimpleNamespace(topic=topic, participant=SimpleNamespace(identity="client-a"), data=b'{"type":"session.update"}')
    adapter._on_data_received(packet, receiving_room=object(), receiving_generation=0)
    packet.participant.identity = "departed"
    adapter._on_data_received(packet)


@pytest.mark.asyncio
async def test_room_change_before_queued_event_starts_drops_it(monkeypatch):
    adapter, local = conference_adapter()
    queued = []
    monkeypatch.setattr(asyncio, "create_task", lambda coroutine: queued.append(coroutine))
    packet = SimpleNamespace(
        topic="conference.events", participant=SimpleNamespace(identity="client-a"),
        data=b'{"type":"session.update","session":{"instructions":"stale"}}',
    )
    adapter._on_data_received(packet)
    protocol = adapter._realtime_protocol
    adapter._room = object()
    await queued.pop()
    assert protocol.instructions == ""
    assert not local.messages


def test_video_subscription_keeps_only_latest_frame(monkeypatch):
    adapter, _ = conference_adapter()
    adapter._video_streams = {}
    factory = Mock()
    monkeypatch.setattr(adapter_module.rtc, "VideoStream", factory)
    track = SimpleNamespace(kind=adapter_module.rtc.TrackKind.KIND_VIDEO)
    adapter._on_track_subscribed(track, None, SimpleNamespace(identity="client-a"))
    factory.assert_called_once_with(track, capacity=1)


@pytest.mark.asyncio
async def test_audio_receiver_closes_sdk_stream_on_cancel():
    adapter, _ = conference_adapter()
    started = asyncio.Event()

    class Stream:
        aclose = AsyncMock()

        def __aiter__(self):
            return self

        async def __anext__(self):
            started.set()
            await asyncio.Future()

    stream = Stream()
    task = asyncio.create_task(adapter._audio_receive_loop(stream, "client-a"))
    await started.wait()
    task.cancel()
    await task
    stream.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_silence_detector_wakes_when_audio_subscribes(monkeypatch):
    adapter, _ = conference_adapter()
    adapter._audio_buffers = {}
    adapter._running = True
    tick = asyncio.Event()
    real_sleep = asyncio.sleep

    async def poll_sleep(delay):
        assert delay == adapter_module.POLL_INTERVAL
        tick.set()
        await asyncio.Future()

    monkeypatch.setattr(adapter_module.asyncio, "sleep", poll_sleep)
    task = asyncio.create_task(adapter._check_silence_loop())
    await real_sleep(0)
    assert not tick.is_set()
    adapter._audio_buffers["client-a"] = bytearray()
    adapter._audio_ready.set()
    try:
        await asyncio.wait_for(tick.wait(), timeout=1)
    finally:
        task.cancel()
        await task


@pytest.mark.asyncio
async def test_conference_endpoint_uses_configured_timeout_and_keeps_word_tail(monkeypatch):
    from hermes_livekit.vad import AdaptiveRmsGate

    adapter, _ = conference_adapter()
    adapter._silence_duration = 0.7
    adapter._running = True
    adapter._audio_gates["client-a"] = AdaptiveRmsGate(noise_rms=150)
    adapter._audio_buffers["client-a"] = bytearray(b"\x00\x00" * 48000)
    adapter._last_audio_time["client-a"] = 0.0
    adapter._speaking_participants.add("client-a")
    clock = [0.0]
    elapsed = iter([0.69, 0.71])

    async def poll_sleep(delay):
        clock[0] = next(elapsed)

    def flush(identity, speech_end):
        assert clock[0] > 0.7
        assert identity == "client-a"
        assert speech_end == 96000 - int(0.6 * 96000)
        adapter._running = False

    adapter._flush_utterance = Mock(side_effect=flush)
    monkeypatch.setattr(adapter_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(adapter_module.asyncio, "sleep", poll_sleep)
    await adapter._check_silence_loop()
    adapter._flush_utterance.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,status", [
    (ProcessingOutcome.SUCCESS, "completed"),
    (ProcessingOutcome.FAILURE, "failed"),
    (ProcessingOutcome.CANCELLED, "cancelled"),
])
async def test_processing_hook_finishes_turns_without_a_text_send(outcome, status):
    adapter, local = conference_adapter()
    await adapter._realtime_protocol.output_started()
    event = SimpleNamespace(source=SimpleNamespace(chat_id="room-a"))
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, outcome)
    await adapter.on_processing_complete(event, outcome)
    responses = [message["response"] for message, _ in local.messages if message["type"] == "response.done"]
    assert len(responses) == 1
    assert responses[0]["status"] == status


@pytest.mark.asyncio
async def test_old_processing_hook_does_not_finish_newer_response():
    adapter, local = conference_adapter()
    event = SimpleNamespace(source=SimpleNamespace(chat_id="room-a"))
    await adapter.on_processing_start(event)
    await adapter.send("room-a", "first")
    await adapter._realtime_protocol.response_started()
    new_id = adapter._realtime_protocol.active_response_id
    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)
    assert adapter._realtime_protocol.active_response_id == new_id


def test_video_encoder_preserves_dimensions_and_rgb_channels():
    import io
    from PIL import Image

    # A uniform synthetic RGB frame catches channel/stride mistakes without a
    # camera or native frame conversion. JPEG is lossy, so allow rounding.
    pixels = bytes([240, 20, 30]) * (8 * 4)
    frame = SimpleNamespace(convert=Mock(return_value=SimpleNamespace(width=8, height=4, data=pixels)))
    encoded = LiveKitAdapter._encode_video_frame(frame)
    frame.convert.assert_called_once_with(adapter_module.rtc.VideoBufferType.RGB24)
    with Image.open(io.BytesIO(encoded)) as image:
        assert image.size == (8, 4)
        assert image.format == "JPEG"
        assert all(abs(actual - expected) <= 3 for actual, expected in zip(image.getpixel((0, 0)), (240, 20, 30)))


def test_draining_captures_preserves_order_without_deleting_files(tmp_path):
    adapter, _ = conference_adapter()
    paths = [tmp_path / "first.jpg", tmp_path / "second.jpg"]
    for path in paths:
        path.write_bytes(b"fixture")
    adapter._pending_captures = [(str(path), "image/jpeg") for path in paths]
    assert adapter._drain_pending_captures() == ([str(path) for path in paths], ["image/jpeg", "image/jpeg"])
    assert adapter._pending_captures == []
    assert all(path.exists() for path in paths)


@pytest.mark.asyncio
async def test_delayed_video_encode_cannot_attach_to_replacement_room(monkeypatch, tmp_path):
    adapter, _ = conference_adapter()
    frame = SimpleNamespace(width=1, height=1)
    stream = SimpleNamespace(__anext__=AsyncMock(return_value=SimpleNamespace(frame=frame)))
    adapter._video_streams = {"client-a": stream}
    adapter._pending_captures = []

    def encode(frame):
        adapter._room = object()
        return b"not written"

    monkeypatch.setattr(adapter, "_encode_video_frame", encode)
    monkeypatch.setattr(adapter_module.tempfile, "gettempdir", lambda: str(tmp_path))
    await adapter._capture_next_frame("client-a")
    assert adapter._pending_captures == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_queued_transcription_does_not_start_after_room_teardown(monkeypatch):
    adapter, _ = conference_adapter()
    adapter._room = None
    adapter._realtime_protocol = None
    transcribe = Mock()
    monkeypatch.setattr(adapter_module, "transcribe_pcm", transcribe)
    await adapter._process_voice_input("client-a", b"\x00\x00")
    transcribe.assert_not_called()
