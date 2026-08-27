"""Room release stays on the public LiveKit SDK contract."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

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
