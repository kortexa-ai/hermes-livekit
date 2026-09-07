"""Room release stays on the public LiveKit SDK contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import hermes_livekit.adapter as adapter_module
from hermes_livekit.adapter import LiveKitAdapter


class _FakeRoom:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.disconnect_called = False

    async def disconnect(self) -> None:
        self.disconnect_called = True
        if self.fail:
            raise RuntimeError("engine already gone")


class _Adapter(LiveKitAdapter):
    # `name` is a read-only property on the base adapter; shadow it so we can
    # build one without a PlatformConfig.
    name = "livekit-test"


def _adapter() -> LiveKitAdapter:
    return object.__new__(_Adapter)


@pytest.mark.asyncio
async def test_release_room_uses_public_disconnect() -> None:
    room = _FakeRoom()

    await _adapter()._release_room(room, why="test")

    assert room.disconnect_called


@pytest.mark.asyncio
async def test_release_room_tolerates_a_failing_disconnect() -> None:
    room = _FakeRoom(fail=True)

    await _adapter()._release_room(room, why="test")

    assert room.disconnect_called


@pytest.mark.asyncio
async def test_release_room_ignores_none() -> None:
    await _adapter()._release_room(None, why="test")


@pytest.mark.asyncio
async def test_failed_join_releases_room_and_allows_presence_retry(monkeypatch):
    adapter = _adapter()
    adapter._room = None
    adapter._realtime_protocol = None
    adapter._room_generation = 0
    adapter._api_key = "fixture"
    adapter._api_secret = "fixture"
    adapter._agent_name = "Hermes"
    adapter._room_name = "room"
    adapter._url = "wss://unused.invalid"
    adapter._new_realtime_protocol = Mock(return_value=AsyncMock())
    room = SimpleNamespace(
        on=Mock(),
        connect=AsyncMock(side_effect=RuntimeError("fixture connect failure")),
        disconnect=AsyncMock(),
    )
    monkeypatch.setattr(adapter_module.rtc, "Room", lambda: room)
    token = Mock()
    token.with_identity.return_value = token
    token.with_name.return_value = token
    token.with_grants.return_value = token
    token.to_jwt.return_value = "fixture-token"
    monkeypatch.setattr(adapter_module, "AccessToken", lambda **kwargs: token)
    assert await adapter._join_room() is False
    assert adapter._room is None
    assert adapter._realtime_protocol is None
    room.disconnect.assert_awaited_once()
    assert adapter._graceful_leave is False


@pytest.mark.asyncio
async def test_duplicate_disconnect_events_share_one_reconnect_attempt():
    adapter = _adapter()
    adapter._running = True
    adapter._graceful_leave = False
    adapter._connect_task = None
    adapter._room_generation = 1
    adapter._fail_binary_generation = Mock()
    adapter._cleanup_all_client_tools = Mock()
    adapter._reconnect_loop = AsyncMock()
    adapter._on_disconnected("first")
    task = adapter._connect_task
    adapter._on_disconnected("second")
    assert adapter._connect_task is task
    await task
    adapter._reconnect_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_room_leave_closes_video_streams():
    adapter = _adapter()
    stream = SimpleNamespace(aclose=AsyncMock())
    adapter._silence_task = None
    adapter._audio_streams = {}
    adapter._video_streams = {"client": stream}
    adapter._audio_buffers = {}
    adapter._last_audio_time = {}
    adapter._audio_gates = {}
    adapter._muted_inputs = set()
    adapter._speaking_participants = set()
    adapter._room = None
    adapter._room_generation = 1
    adapter._realtime_protocol = None
    adapter._running = False
    adapter._cleanup_all_client_tools = Mock()
    adapter._fail_binary_generation = Mock()
    await adapter._leave_and_watch()
    stream.aclose.assert_awaited_once()
    assert adapter._video_streams == {}


@pytest.mark.asyncio
async def test_media_cleanup_waits_for_all_receivers_and_clears_old_audio():
    adapter = _adapter()
    finished = []
    entered = asyncio.Event()

    async def receiver():
        try:
            entered.set()
            await asyncio.Future()
        finally:
            finished.append(True)

    task = asyncio.create_task(receiver())
    await entered.wait()
    adapter._audio_streams = {"client": task}
    adapter._audio_buffers = {"client": bytearray(b"stale")}
    adapter._last_audio_time = {"client": 10.0}
    adapter._speaking_participants = {"client"}
    await adapter._close_capture_streams()
    assert task.done()
    assert finished == [True]
    assert adapter._audio_streams == {}
    assert adapter._audio_buffers == {}
    assert adapter._last_audio_time == {}
    assert adapter._speaking_participants == set()


def test_room_cleanup_removes_only_undispatched_captures(tmp_path):
    adapter = _adapter()
    pending = tmp_path / "pending.jpg"
    dispatched = tmp_path / "dispatched.jpg"
    pending.write_bytes(b"fixture")
    dispatched.write_bytes(b"fixture")
    adapter._pending_captures = [(str(pending), "image/jpeg")]
    adapter._discard_pending_captures()
    assert not pending.exists()
    assert dispatched.exists()
    assert adapter._pending_captures == []


@pytest.mark.asyncio
async def test_empty_room_grace_releases_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_module, "EMPTY_ROOM_GRACE_SECONDS", 0.01)
    adapter = _adapter()
    room = _FakeRoom()
    room.remote_participants = {}
    adapter._room = room
    adapter._room_generation = 4
    adapter._empty_room_task = None
    adapter._room_name = "parity-room"
    adapter._leave_and_watch = AsyncMock()

    task = asyncio.create_task(adapter._leave_after_empty_grace(room, 4))
    adapter._empty_room_task = task
    await task

    adapter._leave_and_watch.assert_awaited_once_with()
    assert adapter._empty_room_task is None


@pytest.mark.asyncio
async def test_empty_room_grace_preserves_reconnected_room(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(adapter_module, "EMPTY_ROOM_GRACE_SECONDS", 0.01)
    adapter = _adapter()
    room = _FakeRoom()
    room.remote_participants = {}
    adapter._room = room
    adapter._room_generation = 4
    adapter._empty_room_task = None
    adapter._room_name = "parity-room"
    adapter._leave_and_watch = AsyncMock()

    task = asyncio.create_task(adapter._leave_after_empty_grace(room, 4))
    adapter._empty_room_task = task
    room.remote_participants["client"] = object()
    await task

    adapter._leave_and_watch.assert_not_awaited()
    assert adapter._empty_room_task is None
