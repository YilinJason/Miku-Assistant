# ADR — Input audio streaming core (DSP state + bounded segmenter)

**Status:** Accepted — streaming core, bounded capture controller, and duplex STT/`VoiceSession` wiring (2026-11-12).

## Context

NS utterance preprocessing and Silero batch VAD are accepted. Continuous capture requires persistent WebRTC NS state and Silero `[2,1,128]` state across 10 ms / 32 ms frames without per-frame session creation.

## Decision

1. **`SileroOnnxVAD` + `SileroVadStreamContext`** (`silero_vad_stream.py`): owner holds at most one stream context; shared non-blocking lock with batch `score_pcm`; factored `_run_silero_vad_frame`; exact 512-sample (1024-byte) frames; invalidate stream on error/cancel; context `close` does not close owner session.
2. **`WebRtcNoiseStream`** (`noise_suppression.py`): incremental `process_chunk` (≤4096 B) + `finish`; ≤159 residual samples; batch `suppress_utterance_pcm` delegates to stream with unchanged 15 s cap.
3. **`BoundedInputSegmenter`** (`input_segments.py`): pure stdlib; 256 ms preroll; 3×0.6 confirm; 20×0.45 quiet end; 12 s max utterance; admission from `InputAudioSettings` four thresholds; monotonic sample index only (not device capture time).
4. **`ContinuousInputCapture`** (`input_capture.py`): one owned thread; PyAudio read **only on worker** (512×32 ms); optional incremental NS + Silero stream context; segment queue **capacity 2** (drop oldest); stale >3 s; overflow resets capped at 3; `start`/`close`/`read_segment`/`status`; injectable factories **for tests only**. Does **not** close Silero owner session. **Wired** (2026-11-12 slice): explicit duplex `VoiceSession` + Paraformer + `capture_mode: continuous` + Silero backend only; `utterance`/provider/injected paths keep legacy SR listen. `RecognitionResult` may carry optional host **estimate** timestamps from segments—not playback/AEC truth.

## Duplex vs utterance (current)

- **`capture_mode: continuous`**（默认 YAML）：仅在全双工 `VoiceSession` 显式 `begin` + Paraformer + Silero + `input_audio.enabled` 时启动 **一条 owned 采集线程**；段队列容量 **2**，单段最长 **12 s**，静音结束约 **640 ms**，陈旧段 **>3 s** 丢弃。
- **`capture_mode: utterance`**：保留 legacy SpeechRecognition 整句麦克风路径；串行/诊断会话 **不** 后台采集。
- 采集时间戳为主机单调 **估计值**，非硬件时钟或声学 AEC 证明；默认 **不** 落盘录音。

## Echo timing (text-level, 2026-11-12)

全双工门控用 **采集区间** 与 **回复活动窗口**（`started_at` … `finished_at + grace`）重叠来判断 `output_active`，避免 ASR 返回墙钟晚于 grace 时误放行回声。参考文本仍是已生成任务文本的保守估计，**非**实际播放 PCM；声学 AEC / 扬声器回声验收仍为后续工作。

## Pending

- 真实房间阈值调参与连续 NS 听感验收；声学 AEC / 实际播放参考。

## Constraints preserved

Formal fake tests block real ORT/APM/Mic; native equivalence probes remain Codex-owned. Production duplex **does** use one bounded capture worker while that voice session is active (by design above).
