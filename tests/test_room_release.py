"""``_release_room`` must free FFI resources even when disconnect() no-ops.

``Room.disconnect()`` returns early when the room is already disconnected,
skipping ``await self._task`` and the FFI queue unsubscribe. The listen task is
a bound coroutine, so while it lives the event loop keeps the Room alive, its
handle is never dropped, and the UDP sockets it gathered for ICE stay open for
the life of the process — eventually ``[Errno 24] Too many open files``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from hermes_livekit.adapter import LiveKitAdapter


class _FakeHandle:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


class _FakeRoom:
    """Stands in for rtc.Room, including disconnect()'s early return."""

    def __init__(self, *, connected: bool) -> None:
        self._connected = connected
        self.disconnect_called = False
        self._ffi_handle = _FakeHandle()
        self._ffi_queue = object()
        self._task = asyncio.get_event_loop().create_future()  # pending == alive

    async def disconnect(self) -> None:
        self.disconnect_called = True
        if not self._connected:
            return  # the early return this whole test exists for
        self._task.cancel()


class _Adapter(LiveKitAdapter):
    # `name` is a read-only property on the base adapter; shadow it so we can
    # build one without a PlatformConfig.
    name = "livekit-test"


def _adapter() -> LiveKitAdapter:
    return object.__new__(_Adapter)


@pytest.mark.asyncio
@pytest.mark.parametrize("connected", [True, False])
async def test_release_room_disposes_handle(connected):
    """The handle is dropped whether or not disconnect() did any work."""
    room = _FakeRoom(connected=connected)

    await _adapter()._release_room(room, why="test")

    assert room.disconnect_called
    assert room._ffi_handle.disposed, "FFI handle left undisposed — sockets leak"
    assert room._task.cancelled() or room._task.done(), "listen task still pins the Room"


@pytest.mark.asyncio
async def test_release_room_tolerates_a_failing_disconnect():
    """A disconnect that raises must not skip the cleanup."""

    class _Exploding(_FakeRoom):
        async def disconnect(self) -> None:
            raise RuntimeError("engine already gone")

    room = _Exploding(connected=False)

    await _adapter()._release_room(room, why="test")

    assert room._ffi_handle.disposed
    assert room._task.cancelled()


@pytest.mark.asyncio
async def test_release_room_ignores_none():
    await _adapter()._release_room(None, why="test")  # must not raise


@pytest.mark.asyncio
async def test_release_room_survives_an_unfamiliar_room_object():
    """Private SDK attributes are best-effort: a shape change must not break us."""

    class _Opaque:
        async def disconnect(self) -> None:
            return None

    await _adapter()._release_room(_Opaque(), why="test")  # must not raise
