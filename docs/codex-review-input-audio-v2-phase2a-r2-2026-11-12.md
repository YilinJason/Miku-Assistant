# Phase2a R2 — pending construction is an owned operation

Review of R1 source shows close now requires literal True and common input guards are corrected. Remaining lifecycle problem:

`_ensure_silero_vad` constructs outside `_silero_vad_lock` with no in-flight token. Shutdown can return True while construction is blocked; two callers can construct duplicate adapters. `_discard_unpublished_silero_vad` is called WITH the lifecycle lock held, and ignores close False/errors. This does not satisfy the original R1 constraints.

Use a minimal single-flight creation token and at most one retiring candidate reference, or equivalent clear state. A second ensure returns busy (no queue/wait or duplicate construction). Set token under lifecycle lock, construct outside, recheck closing before exposing a usable owner. Shutdown must report pending while creation is in flight. If closing wins, the candidate must be closed outside the lifecycle lock; retain it in an explicit retiring ownership slot if close is not literal True or raises. Retry shutdown must reclaim it. Never drop a pending/failed-close reference. Status APIs must account for construction/retiring state. Do not mark permanently closed early when any such work remains. All flags/ref transitions are identity-safe under the gate; never hold it across native close.

Deterministic fake tests: blocked constructor -> shutdown False/status incomplete; release -> no usable late owner, eventual retry True. Concurrent ensure -> only one factory invocation, second returns busy. Retiring close returns 1/dict/raises -> retained pending, retry True -> cleared. Ensure thread exceptions propagate and all threads join with bounded waits.

Scope stays Phase2a. Preserve corrected fake bootstrap import and Mic/ORT blockers. Run documented fake set under NEW label `input_v2_integration_r2_cursor_nov12c`, then STOP. No real models/devices/ASR/app or other phases. Do not edit this reviewer document.
