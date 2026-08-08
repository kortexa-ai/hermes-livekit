"""Room release stays on the public LiveKit SDK contract."""

from __future__ import annotations

import pytest

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
