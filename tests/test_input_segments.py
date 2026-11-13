# -*- coding: utf-8 -*-
"""纯有界分段器（无 native/Mic/能量门）；帧 tag 校验时间轴唯一性。"""
from __future__ import annotations

import struct
import unittest

from src.voice.input_segments import (
    FRAME_BYTES,
    FRAME_SAMPLES,
    MAX_FRAMES_PER_SEGMENT,
    BoundedInputSegmenter,
    START_THRESHOLD,
)


def tagged_frame(tag: int) -> bytes:
    lo = tag & 0xFF
    hi = (tag >> 8) & 0xFF
    return bytes([lo, hi]) * FRAME_SAMPLES


def _tags_in_pcm(pcm: bytes) -> list[int]:
    tags = []
    for offset in range(0, len(pcm), FRAME_BYTES):
        lo = pcm[offset]
        hi = pcm[offset + 1]
        tags.append(lo | (hi << 8))
    return tags


class SegmenterAdmissionTests(unittest.TestCase):
    def test_four_positive_frames_128ms_rejected(self) -> None:
        seg = BoundedInputSegmenter()
        for idx in range(4):
            seg.feed_frame(tagged_frame(idx), 0.95)
        self.assertIsNone(seg.finish())

    def test_six_positive_frames_192ms_accepted(self) -> None:
        seg = BoundedInputSegmenter()
        segments = []
        for idx in range(6):
            maybe = seg.feed_frame(tagged_frame(idx), 0.95)
            if maybe is not None:
                segments.append(maybe)
        tail = seg.finish()
        if tail is not None:
            segments.append(tail)
        self.assertEqual(len(segments), 1)
        seg0 = segments[0]
        self.assertGreaterEqual(seg0.voiced_ms, 180)
        self.assertEqual(_tags_in_pcm(seg0.pcm), list(range(6)))

    def test_false_frames_never_emit(self) -> None:
        seg = BoundedInputSegmenter()
        for idx in range(50):
            self.assertIsNone(seg.feed_frame(tagged_frame(idx), 0.1))
        self.assertIsNone(seg.finish())

    def test_max_split_three_segments_no_overlap(self) -> None:
        seg = BoundedInputSegmenter()
        emissions = []
        tag = 0
        total_frames = MAX_FRAMES_PER_SEGMENT * 2 + 10
        for _ in range(total_frames):
            maybe = seg.feed_frame(tagged_frame(tag), START_THRESHOLD + 0.05)
            tag += 1
            if maybe is not None:
                emissions.append(maybe)
        tail = seg.finish()
        if tail is not None:
            emissions.append(tail)
        self.assertGreaterEqual(len(emissions), 2)
        timeline: list[int] = []
        for seg_out in emissions:
            timeline.extend(_tags_in_pcm(seg_out.pcm))
        self.assertEqual(len(timeline), len(set(timeline)))
        self.assertEqual(timeline, list(range(len(timeline))))
        for seg_out in emissions:
            self.assertLessEqual(
                seg_out.end_sample - seg_out.start_sample,
                MAX_FRAMES_PER_SEGMENT * FRAME_SAMPLES,
            )

    def test_max_split_continuation_single_frame(self) -> None:
        seg = BoundedInputSegmenter()
        for idx in range(MAX_FRAMES_PER_SEGMENT):
            seg.feed_frame(tagged_frame(idx), 0.95)
        maybe = seg.feed_frame(tagged_frame(9001), 0.95)
        self.assertIsNotNone(maybe)
        self.assertEqual(maybe.end_reason, "max_duration")
        self.assertEqual(len(seg._segment_frames), 1)
        self.assertEqual(_tags_in_pcm(seg._segment_frames[0].pcm)[0], 9001)

    def test_idle_many_frames_bounded_memory(self) -> None:
        seg = BoundedInputSegmenter()
        for idx in range(50_000):
            seg.feed_frame(tagged_frame(idx % 1000), 0.05)
        self.assertEqual(len(seg._preroll), 8)
        self.assertFalse(seg._in_segment)

    def test_separate_utterances_no_tail_reemit(self) -> None:
        segmenter = BoundedInputSegmenter()
        emitted = []
        for i in range(52):
            prob = 0.95 if i % 26 < 6 else 0.1
            out = segmenter.feed_frame(
                struct.pack("<h", i + 1) * FRAME_SAMPLES, prob
            )
            if out is not None:
                emitted.append(out)
        self.assertEqual(len(emitted), 2)
        self.assertGreaterEqual(
            emitted[1].start_sample,
            emitted[0].end_sample,
        )

    def test_max_split_full_timeline_no_overlap(self) -> None:
        segmenter = BoundedInputSegmenter()
        original: list[bytes] = []
        emitted = []
        for i in range(1150):
            frame = struct.pack("<h", i + 1) * FRAME_SAMPLES
            original.append(frame)
            out = segmenter.feed_frame(frame, 0.95)
            if out is not None:
                emitted.append(out)
        tail = segmenter.finish()
        if tail is not None:
            emitted.append(tail)
        self.assertEqual(b"".join(s.pcm for s in emitted), b"".join(original))
        for previous, current in zip(emitted, emitted[1:]):
            self.assertEqual(previous.end_sample, current.start_sample)

    def test_stream_generation_rejects_bool(self) -> None:
        with self.assertRaises(ValueError):
            BoundedInputSegmenter(stream_generation=True)  # type: ignore[arg-type]

    def test_gap_reset_clears_segment(self) -> None:
        seg = BoundedInputSegmenter(stream_generation=1)
        for idx in range(10):
            seg.feed_frame(tagged_frame(idx), 0.95)
        seg.reset(stream_generation=2)
        self.assertFalse(seg._in_segment)
        self.assertEqual(seg.stream_generation, 2)


if __name__ == "__main__":
    unittest.main()
