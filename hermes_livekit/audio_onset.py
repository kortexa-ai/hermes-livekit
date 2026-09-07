"""Conservative, bounded onset trimming for 48 kHz mono PCM16 playout."""

from collections import deque
from collections.abc import Iterable, Iterator
import struct

FRAME_BYTES = 1920  # One 20 ms frame at the gateway's fixed output format.
QUIET_PEAK = 16  # About -66 dBFS; far below the microphone speech gate.
LEAD_IN_FRAMES = 4  # Retain 80 ms before the first non-quiet frame.
MAX_SCAN_FRAMES = 50  # Stop looking after one second, even for silent output.


class LeadingSilenceTrimmer:
    """Remove only the near-silent prefix, never pauses after the first onset.

    Uses sample peaks, not average energy, so a short quiet consonant is enough
    to stop trimming. Retained audio is byte-for-byte unchanged. At most four
    frames are buffered; no new wall-clock timer delays provider output.
    """

    def __init__(self):
        self._pending: deque[bytes] = deque(maxlen=LEAD_IN_FRAMES)
        self._scanned = 0
        self._done = False
        self.trimmed_frames = 0

    def feed(self, frames: Iterable[bytes]) -> Iterator[bytes]:
        for pcm in frames:
            if self._done:
                yield pcm
                continue
            if len(pcm) != FRAME_BYTES:
                raise ValueError("Onset trimming requires complete 20 ms PCM16 frames")
            self._scanned += 1
            if any(abs(sample) > QUIET_PEAK for (sample,) in struct.iter_unpack("<h", pcm)):
                yield from self.finish()
                yield pcm
                continue
            if len(self._pending) == LEAD_IN_FRAMES:
                self.trimmed_frames += 1
            self._pending.append(pcm)
            if self._scanned >= MAX_SCAN_FRAMES:
                yield from self.finish()

    def finish(self) -> Iterator[bytes]:
        self._done = True
        while self._pending:
            yield self._pending.popleft()

    def discard(self) -> None:
        self._pending.clear()
        self._done = True
