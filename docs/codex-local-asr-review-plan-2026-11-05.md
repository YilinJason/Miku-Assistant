# Local ASR migration review and acceptance

Status: implementation dispatched to official Cursor CLI, not yet accepted. Scope is whole-utterance Paraformer CPU, not streaming ASR or an entirely offline application.

## Review gates

1. Inspect authored code and callers: fixed assets/interpreter, capability sealing, no model imports/devices on text paths, cancellation and retryable actual-tree/I/O shutdown. Compare against pre-change source hashes in `tmp/paraformer-before-source-20261105.json`. Run meaningful fake contract tests only via the existing scoped test gate after Cursor stops. No concurrent source editing/testing.
2. After gate 1, Cursor authors a fixed-source preparation entry point and a supervised fixed-audio diagnostic. Codex reviews before installation/download/model execution. Main interpreter stays Python 3.12.3; RVC and Cosy environments unchanged. Prepare `.venv-asr` separately, CPU torch/torchaudio 2.6.0 and FunASR 1.4.22, with tested compatible dependency constraints and a resulting environment inventory. Never auto-install on normal app startup.
3. Model source: HF `funasr/paraformer-zh`, revision `d7811ee3ac581fbcfdeb37c98c6ba674028433dc`. Official API metadata saved to `tmp/paraformer-hf-official-metadata-20261105.json`. `model.pt`: 880502012 bytes, LFS SHA256 `5bba782a5e9196166233b9ab12ba04cadff9ef9212b4ff6153ed9290ff679025`. Non-LFS files must match the Git blob IDs from that revision; then record local SHA256s in a fixed reviewed manifest. Do not trust a locally regenerated manifest as upstream authenticity evidence. Obtain only this model, no optional VAD/punctuation/speaker weights. Preserve model card/license information. Pin and inspect the actual wheel; source inspection copy is `tmp/funasr-1.4.22-inspect.whl`, official PyPI metadata is `tmp/funasr-wheel-meta-20261105.json`.
4. Before real CPU inference, resample physical/commit availability. Use bounded, owned, supervised diagnostic; no main app/microphone/speaker or formal memory database. Use existing fixed synthetic speech WAVs read-only, and/or official fixed example WAV with checksum. Report cold load and warm transcription latency, resource observations and actual process-tree/I/O cleanup. Verify selected Nahida reference SHA256 and legacy shared temp metadata remain unchanged. Runtime network attempts must fail closed; this is a Python runtime guard, not an OS sandbox claim.
5. Switch default config and Config fallback to Paraformer plus narrowly enabled local_asr_worker / disabled cloud_stt only after real local inference acceptance. Update README/setup and delivery/handoff documentation to actual results. Keep explicit legacy ASR options and explain manual rollback with cloud permission requirements. Leave TTS/RVC/LLM/memory settings unchanged.

## Important review cases

- PCM geometry and upper bound before IPC, explicit sample-rate/width conversion and a hard recording cap on all Paraformer capture calls.
- Same request ID and version, exact terminal result schema, no stale text after timeout/cancel/close, no transcript leakage to logs.
- Thread/payload/queue/model count is bounded; no new background thread per utterance; no automatic retry when prior worker ownership remains uncertain.
- Close races against startup, blocked writes, active inference and device capture; incomplete close cannot mark assistant closed or release dependencies.
- Existing noise calibration uses a detached timeout thread; the new path must not introduce unsafe microphone reuse/close while calibration still accesses the source.
- Torch 2.6 defaults to restricted `weights_only` loading. Do not disable this as a blanket compatibility workaround; allow only the reviewed fixed checkpoint. No model-controlled remote code or arbitrary filesystem references.

## Validation limits

Fixed synthetic and official audio can prove local inference and measure the tested machine. They cannot establish the user's accent/noise accuracy or all low-end hardware behavior. User microphone comparison and sustained voice-session acceptance remain separate from fake/offline checks. ASR becoming local does not make configured Edge TTS offline.
