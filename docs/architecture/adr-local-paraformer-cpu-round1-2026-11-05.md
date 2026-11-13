# Local Paraformer CPU ASR — phase 1

## Status

Accepted through real fixed-audio CPU milestone (`docs/codex-review-local-asr-real-accepted-2026-11-05.md`); **project default config** switched to `paraformer` with `local_asr_worker` / sealed-profile readiness (Cursor final switch 2026-11-05). Real microphone and long combined-stack soak remain separate acceptance.

## Context

The assistant needs offline Mandarin STT without cloud fallback, while keeping Google/Whisper as explicit legacy engines. GPU streaming ASR is out of scope for this phase.

## Decision

- **Whole-utterance CPU path**: SpeechRecognition captures one utterance (≤15 s @ 16 kHz mono PCM); a dedicated `.venv-asr` worker loads FunASR Paraformer-zh once per STT client and serves **one inflight** JSONL request at a time.
- **Protocol v1**: Separate inbound (PCM base64, ~640 KiB class) vs outbound (short JSON) line limits; strict schema, request IDs, and status/text invariants. Parent Job retirement is the primary cancel path; worker cancel lines are best-effort only.
- **Trust boundary**: Official HF revision pin (LFS SHA256 for `model.pt`, Git blob IDs for small files); no self-referential local manifest. `local_asr_worker` + sealed profile required to spawn. CosyVoice TTS profile is sealed at policy construction when configured (unchanged). ASR profile is sealed at construction **only when** fixed layout validates; missing vendor tree omits ASR seal so text/interactive still start—**restart the assistant after installing ASR assets** so a new policy can seal.
- **Lifecycle**: Client owns spawn→I/O publication transactionally; shutdown is bounded, may retain incomplete Job/I/O for retry, and never reports complete while spawn/transcribe can still publish resources. STT wrapper serializes operations, propagates shutdown to suppress late SUCCESS, and closes engine-owned TFM only after quiet worker shutdown.

## Consequences

- First-use voice latency includes worker cold start (≤45 s budget) plus inference (≤15 s).
- Asset preparation (venv, vendor tree, pins) is a separate gated step before default migration.
- Windows cold-start admission for Paraformer uses host_memory thresholds **4096 MiB physical / 6144 MiB commit** (RVC/Cosy thresholds unchanged); first supervised CPU diagnostic measured tree private≈4520 MiB (`tmp/asr_gate_infer_nov5o.json`).
- Streaming, VAD, hotword rewrite, and GPU ASR remain future work.
