# Silero ONNX VAD — Phase 1 adapter (candidate, not default)

## Status

Proposed (2026-11-05). **Not** enabled in production config or default microphone path. Codex review and fixed-audio native A/B pending before any default switch.

## Context

Phase A uses WebRTC VAD for acoustic guard. Research authorized evaluating Silero CPU ONNX as a candidate on the same data as WebRTC before any default change.

## Decision

- Add `src/voice/silero_vad.py`: stdlib import surface; numpy/onnxruntime loaded on demand; fixed repo asset `vendor/silero-vad/silero_vad.onnx` with SHA-256/size validation; CPU ORT session with bounded threading; segment-local state/context; cancel/close semantics; injectable session factory for offline tests only.
- **No** change to `voice.stt.input_audio` defaults, assembly, or live mic path in this phase.
- Runtime pins documented in `requirements-input-audio.txt` for Codex install; main `requirements.txt` unchanged.

## Consequences

Adapter errors fail closed; missing or tampered assets do not auto-accept all audio. Next phases (WebRTC comparison, continuous input, duplex) require separate Codex acceptance.

## References

- `docs/cursor-task-input-audio-v2-2026-11-05.md`
- `tmp/silero_asset_manifest_20261105.json`
