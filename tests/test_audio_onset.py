"""Exact PCM preservation and bounded work for optional voice-onset trimming."""

import struct

import pytest

from hermes_livekit.audio_onset import (
    FRAME_BYTES, LEAD_IN_FRAMES, MAX_SCAN_FRAMES, LeadingSilenceTrimmer,
)


def frame(value):
    return struct.pack("<h", value) * (FRAME_BYTES // 2)


def trim(frames):
    trimmer = LeadingSilenceTrimmer()
    return trimmer, list(trimmer.feed(frames)) + list(trimmer.finish())


def test_only_prefix_changes_and_lead_in_is_preserved_exactly():
    quiet = [frame(i % 5) for i in range(30)]
    speech = [frame(500), frame(-500)]
    tail = [frame(0)] * 20 + [frame(1000)]
    trimmer, result = trim(quiet + speech + tail)
    assert result == quiet[-LEAD_IN_FRAMES:] + speech + tail
    assert trimmer.trimmed_frames == 26


@pytest.mark.parametrize("offset", [0, 1918])
@pytest.mark.parametrize("sample", [-32768, -17, 17, 32767])
def test_single_low_level_sample_stops_trimming(offset, sample):
    consonant = bytearray(frame(0))
    struct.pack_into("<h", consonant, offset, sample)
    frames = [bytes(consonant)] + [frame(0)] * 60
    trimmer, result = trim(frames)
    assert result == frames
    assert trimmer.trimmed_frames == 0


def test_quiet_short_reply_is_not_dropped_on_finish():
    frames = [frame(1), frame(2)]
    trimmer, result = trim(frames)
    assert result == frames
    assert trimmer.trimmed_frames == 0


def test_all_quiet_stream_has_bounded_buffer_and_scan():
    trimmer = LeadingSilenceTrimmer()
    for _ in range(MAX_SCAN_FRAMES - 1):
        assert list(trimmer.feed([frame(1)])) == []
        assert len(trimmer._pending) <= LEAD_IN_FRAMES
    assert list(trimmer.feed([frame(2)])) == [frame(1)] * 3 + [frame(2)]
    assert trimmer.trimmed_frames == MAX_SCAN_FRAMES - LEAD_IN_FRAMES
    frames = [frame(0)] * 500
    assert list(trimmer.feed(frames)) == frames
    assert list(trimmer.finish()) == []


def test_discard_cannot_flush_buffered_audio_later():
    trimmer = LeadingSilenceTrimmer()
    assert list(trimmer.feed([frame(1)] * 10)) == []
    trimmer.discard()
    assert list(trimmer.finish()) == []


def test_invalid_frame_size_is_rejected():
    with pytest.raises(ValueError, match="complete 20 ms"):
        list(LeadingSilenceTrimmer().feed([b"\0\0"]))
