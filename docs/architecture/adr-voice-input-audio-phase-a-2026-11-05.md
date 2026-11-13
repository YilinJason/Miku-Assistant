# Voice input acoustic guard — phase A

## Status

Accepted for code/offline verification (2026-11-05). See `docs/codex-review-voice-input-final-2026-11-05.md`; real user microphone acceptance remains pending.

## Context

Quiet-room false triggers: SpeechRecognition dynamic `energy_threshold` could drift below calibrated floor; short recording duration was treated as speech evidence; Paraformer ASR ran on any non-empty transcript without local VAD.

## Decision

- **Settings** under `voice.stt.input_audio` (stdlib validation at assembly) and optional `voice.stt.microphone_device_index`.
- **Bounded energy** genuine SpeechRecognition **subclass** (factory) on Paraformer capture when guard enabled; calibration floor uses `energy_floor`, not `energy_threshold * 0.5`. Inherited `listen` / `adjust_for_ambient_noise` clamp on the same instance.
- **WebRTC VAD** via pinned `webrtcvad-wheels==2.0.14.post1` in main requirements; lazy import with ImportError/OSError fail-closed; `input_audio_readiness_detail()` for preflight.
- **Pre-ASR gate** applies only to **microphone capture path** (`apply_acoustic_guard=True`); direct `_recognize_paraformer_result` / diagnostic AudioData keeps prior contract (`apply_acoustic_guard=False` default).
- **PCM protocol**: 16 kHz mono s16 max **480000** bytes (15 s). Invalid empty/misaligned/oversized/wrong-type PCM is **rejected** on all Paraformer transcribe paths (no silent trim). Microphone `listen` phrase limit uses source **CHUNK as sample frames** (`frame_duration = CHUNK / SAMPLE_RATE`, matching SpeechRecognition) plus `non_speaking_duration` reserve; `SAMPLE_WIDTH` does not change the time allowance.
- **Scope**: acoustic guard and bounded recognizer apply to Paraformer microphone capture. Legacy engines retain their capture behavior; explicit microphone selection and strict configuration validation are shared. `enabled: false` is an explicit Paraformer compatibility option.

## Consequences

Thresholds are provisional tuning targets, not accuracy guarantees. Duplex echo timing remains phase B. No speaker identification.

## References

- [py-webrtcvad-wheels](https://github.com/daanzu/py-webrtcvad-wheels)
- Task: `docs/cursor-task-voice-false-trigger-2026-11-05.md`
