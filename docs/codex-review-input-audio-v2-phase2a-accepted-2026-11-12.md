# Phase2a milestone — independently accepted, device testing still pending

Scope: default CPU Silero VAD in STT, fixed main numeric-thread bootstrap, VAD ownership and close semantics. Cursor authored production/formal tests; Codex reviewed source/callers and independently validated. No other phase is accepted by this document.

## Evidence

- `input_v2_independent_broad_nov12l`: 230 fake tests across 14 targeted modules passed, deadline90s, elapsed13.094s, WAV728→728 unchanged.
- `input_v2_retire_independent_nov12j` initially failed: shutdown returned True during unpublished-candidate close. Cursor repaired atomic ownership and in-flight close claim. Independent rerun `input_v2_retire_independent_nov12m` passed. Failed evidence retained.
- `input_v2_owned_native_nov12n`: actual main bootstrap before intercepted fake CLI import, import-main remains native/env inert; actual STTEngine-owned CPU VAD rejected25/25 fixed synthetic non-speech, admitted9/9 fixed RVC controls to fake ASR, no noise reached ASR. Session weakref reclaimed on VAD-only shutdown; no late ensure/transcription after shutdown. Report `tmp/silero_stt_native_20261112_e07846719d7f.json`.
- Native integrated process private22,708,224→62,210,048 bytes (about37.7MiB increase), after close50,130,944. Retained library allocator memory alone is not a model-session leak. Prior500-repeat stability proof remains in Phase1 acceptance.

Closed issues: single-flight Python owner creation, construction/retirement pending, literal-True close and retry retention, common cancel/disabled/geometry guards, bootstrap test isolation and environment restoration, backend hash-prefix diagnostic. No model/device/ASR/GPU launched by fake gates; native gate uses only small CPU VAD with fixed files and fake ASR. Formal `data/memory` untouched. No commit/push.

## Next admitted candidate

Codex researched `strands-labs/pywebrtc-audio`0.2.0 (WebRTC APM, Apache-2.0), isolated wheel only in `tmp/input-audio-candidate-020`, not installed in main environment yet. WindowsCPython312 wheel513391bytes SHA256 `0dabbdadd5d7fd7dcb88323266e5e14d46aaa136c58fa0573c4b0323b683c80b`, official PyPI source. Only runtime dependency numpy>=1.24 (existing2.3.5 retained).

`input_v2_ns_candidate_nov12o`, report `tmp/webrtc_ns_candidate_20261112_184fa1aab3d5.json`: NSlevel1+HP, AEC/AGC disabled, 10ms mono16k int16. Synthetic noise25/25 still rejected by Silero; fixed RVC controls9/9 accepted including gains.1/.25; stationary-noise reduction criterion passed. Native frame p50.0085ms/p95.0112ms on this machine;10,000frames private56,111,104→56,111,104 bytes stable. No extrapolation to all low-end hardware or real room accuracy.

Sources: https://github.com/strands-labs/pywebrtc-audio , https://pypi.org/project/pywebrtc-audio/0.2.0/ . NVIDIA PersonaPlex audio-front constraints are in the separate Nov12 research document. This admits narrow NS adapter implementation; it does not prove acoustic AEC or continuous-capture completion.
