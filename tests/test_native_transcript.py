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
