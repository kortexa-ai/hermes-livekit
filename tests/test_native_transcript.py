"""Real Hermes text consumer -> voice protocol, without an audio device."""

import asyncio
from types import SimpleNamespace

import pytest

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
from hermes_livekit.adapter import LiveKitAdapter
from hermes_livekit.realtime_protocol import RealtimeProtocol
from hermes_livekit.realtime_webrtc import RealtimeWebRTCAdapter


def endpoint(kind):
    events = []
    updated = asyncio.Event()

    async def publish(event, recipient):
        events.append(event)
        if event["type"] == "response.output_audio_transcript.delta":
            updated.set()
        return True

    protocol = RealtimeProtocol(session_id="captions", model="test", voice="test", publish=publish)
    if kind == "realtime":
        adapter = object.__new__(RealtimeWebRTCAdapter)
        adapter._calls = {"chat": SimpleNamespace(protocol=protocol, closed=False, output_track=object())}
    else:
        adapter = object.__new__(LiveKitAdapter)
        adapter._room_name, adapter._room = "chat", object()
        adapter._audio_source, adapter._realtime_protocol = object(), protocol
    return adapter, protocol, events, updated


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_setup_notice_cannot_end_the_processing_turn_before_native_audio(kind, tmp_path, monkeypatch):
    from gateway.config import PlatformConfig
    from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
    from gateway.run_notifications import GatewayNotificationsMixin

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter, protocol, events, updated = endpoint(kind)
    BasePlatformAdapter.__init__(adapter, PlatformConfig(), SimpleNamespace(value=kind))
    source = adapter.build_source(chat_id="chat", chat_type="dm", user_id="fixture")
    event = MessageEvent(text="Hello", message_type=MessageType.VOICE, source=source)
    runner = SimpleNamespace(
        _adapter_for_source=lambda source: adapter,
        _thread_metadata_for_source=lambda source: None,
    )
    consumer = GatewayStreamConsumer(
        adapter, "chat", config=StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, cursor=""),
    )
    response_ids = []

    async def handler(event):
        response_ids.append(protocol.active_response_id)
        # The real first-contact notice is delivered before TurnRunner starts
        # its native consumer. It is not the final answer to the user's turn.
        await GatewayNotificationsMixin._deliver_platform_notice(runner, source, "Setup notice")
        task = asyncio.create_task(consumer.run())
        try:
            consumer.on_delta("I am Mira.")
            await asyncio.wait_for(updated.wait(), 2)
            await protocol.output_started()
            consumer.finish("I am Mira.")
            await asyncio.wait_for(task, 2)
            await protocol.output_playback_stopped()
            assert consumer._final_content_delivered
            return None  # Core suppresses normal final delivery after a native final.
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    adapter._message_handler = handler
    # Real base lifecycle, including the processing-complete hook; no manual
    # protocol.output_stopped() hiding a missing terminal event.
    await adapter._process_message_background(event, "fixture-session")
    done = [e["response"] for e in events if e["type"] == "response.done"]
    assert len(done) == 1
    assert done[0]["id"] == response_ids[0]
    assert done[0]["status"] == "completed"
    assert done[0]["output"][0]["content"][0]["transcript"] == "I am Mira."
    assert protocol.active_response_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
@pytest.mark.parametrize("override", [None, True])
async def test_runner_routes_text_only_when_profile_streaming_policy_enables_it(kind, override):
    from gateway.config import StreamingConfig
    from gateway.display_config import resolve_display_setting
    from gateway.run_turn import GatewayTurnMixin
    from gateway.run_turn_runner import TurnRunner

    adapter, protocol, events, updated = endpoint(kind)
    await protocol.response_started()
    source = SimpleNamespace(platform=kind, chat_id="chat", chat_type="dm")
    user_config = {"display": {"streaming": False, "platforms": {kind: {"streaming": override}}}}
    runner = SimpleNamespace(config=SimpleNamespace(streaming=StreamingConfig(
        enabled=False, edit_interval=0.01, buffer_threshold=1,
    )), _adapter_for_source=lambda source: adapter)
    runner._build_stream_consumer_config = lambda *args, **kwargs: GatewayTurnMixin._build_stream_consumer_config(
        runner, *args, **kwargs,
    )
    ctx = SimpleNamespace(
        source=source, streaming_tts_consumer_holder=[None],
        resolve_display_setting=resolve_display_setting, user_config=user_config,
        interim_assistant_messages_enabled=True, _status_thread_metadata=None,
        progress_queue=None, stream_consumer_holder=[None], event_message_id="user",
        _run_still_current=lambda: True,
    )
    consumer, callback, _, _ = TurnRunner(runner, ctx)._setup_stream_consumer(kind)
    assert consumer is not None  # Interim-message capability alone is not text streaming.
    if override is None:
        assert callback is None
        return
    task = asyncio.create_task(consumer.run())
    try:
        callback("Actual runner delta")
        await asyncio.wait_for(updated.wait(), 3)
        assert events[-1]["delta"] == "Actual runner delta"
        consumer.finish("Actual runner delta")
        await asyncio.wait_for(task, 3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_actual_consumer_streams_and_finishes_text_without_finishing_audio(kind, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter, protocol, events, updated = endpoint(kind)
    await protocol.response_started()
    response_id = protocol.active_response_id
    consumer = GatewayStreamConsumer(
        adapter, "chat", config=StreamConsumerConfig(edit_interval=0.01, buffer_threshold=1, cursor=""),
    )
    task = asyncio.create_task(consumer.run())
    try:
        consumer.on_delta("I am Mira.")
        await asyncio.wait_for(updated.wait(), 3)
        assert events[-1]["delta"] == "I am Mira."
        # First caption can precede audio. Text completion must not close it.
        assert not any(e["type"] == "output_audio_buffer.started" for e in events)
        await protocol.output_started()
        updated.clear()
        consumer.on_delta(" I am here to help.")
        await asyncio.wait_for(updated.wait(), 3)
        assert events[-1]["delta"] == " I am here to help."
        consumer.finish("I am Mira. I am here to help.")
        await asyncio.wait_for(task, 3)
        assert protocol.active_response_id == response_id
        assert protocol._speaking
        assert not any(e["type"] in {"response.done", "output_audio_buffer.stopped"} for e in events)
        assert [e["transcript"] for e in events if e["type"] == "response.output_audio_transcript.done"] == [
            "I am Mira. I am here to help.",
        ]
        assert consumer._final_content_delivered
        await protocol.output_stopped()
        done = [e for e in events if e["type"] == "response.done"]
        assert len(done) == 1
        assert done[0]["response"]["output"][0]["content"][0]["transcript"] == "I am Mira. I am here to help."
        item_ids = {e["item_id"] for e in events if e["type"].startswith("response.output_audio_transcript.")}
        assert len(item_ids) == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
async def test_revisions_and_late_cancelled_frames_do_not_escape_their_response(kind):
    adapter, protocol, events, _ = endpoint(kind)
    await protocol.response_started()
    send = adapter.send_stream_frame
    await send("", chat_id="chat", turn_id="old")
    await send("I can help", chat_id="chat", turn_id="old")
    await send("Let me check", chat_id="chat", turn_id="old")
    assert events[-1]["transcript"] == "Let me check"
    assert events[-1]["delta"] == ""
    count = len(events)
    await send("Let me check", chat_id="chat", turn_id="old")
    assert len(events) == count
    await protocol.response_cancelled()
    count = len(events)
    await send("stale", chat_id="chat", turn_id="old", finalize=True)
    assert len(events) == count
    await protocol.response_started()
    await send("", chat_id="chat", turn_id="new")
    count = len(events)
    await send("late old draft", chat_id="chat", turn_id="old")
    await send("late old final", chat_id="chat", turn_id="old", finalize=True)
    await send("other chat", chat_id="other", turn_id="new", finalize=True)
    assert len(events) == count
    await send("New answer", chat_id="chat", turn_id="new")
    await send("Corrected final", chat_id="chat", turn_id="new", finalize=True)
    assert events[-1]["transcript"] == "Corrected final"
    assert protocol.active_response_id
    await protocol.close()
    count = len(events)
    await send("late after close", chat_id="chat", turn_id="new", finalize=True)
    assert len(events) == count


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["realtime", "livekit"])
@pytest.mark.parametrize("finalize", [False, True])
async def test_cancel_during_item_publication_cannot_relabel_old_text_as_new(kind, finalize):
    adapter, protocol, events, _ = endpoint(kind)
    await protocol.response_started()
    await adapter.send_stream_frame("", chat_id="chat", turn_id="old")
    entered, release = asyncio.Event(), asyncio.Event()
    publish = protocol._publish

    async def delayed_publish(event, recipient):
        result = await publish(event, recipient)
        if event["type"] == "response.output_item.added" and not entered.is_set():
            entered.set()
            await release.wait()
        return result

    protocol._publish = delayed_publish
    task = asyncio.create_task(adapter.send_stream_frame(
        "Old answer", chat_id="chat", turn_id="old", finalize=finalize,
    ))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await protocol.response_cancelled()
        await protocol.response_started()
        await adapter.send_stream_frame("", chat_id="chat", turn_id="new")
        await adapter.send_stream_frame("New answer", chat_id="chat", turn_id="new")
        count = len(events)
        release.set()
        await asyncio.wait_for(task, 2)
        assert len(events) == count
        assert protocol._active_transcript == "New answer"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
