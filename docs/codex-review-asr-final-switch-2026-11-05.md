# Final default switch review — concrete fixes required

Codex inspected source/callers and reran scoped regression after Cursor STOP. No real models/devices were started by these checks; all828 shared WAV names unchanged.

## P1 pending I/O ownership (independent reproduction)

`tmp/codex_gate_asr_final_root_ownership_nov5s_2026_11_02.json`:18 independent cases, the new pending-I/O case fails for both reader and writer. `_ensure_worker_until` rejects a live process but not a dead process whose old reader/writer is still alive/unjoined. `_release_owner_refs_if_quiescent` correctly retains them, yet execution then proceeds to spawn anyway. A successful spawn can overwrite old I/O references and grow orphaned threads on retries.

After the healthy-ready fast path, refuse a new worker while ANY prior process/reader/writer ownership is live or unknown. Use current strict `_has_live_owner` plus existing bounded retirement path; no new spawn after incomplete cleanup, no reference clearing to make status pass. Only confirmed quiet ownership may be discarded before start. Keep checks serialized by existing inflight/retire/lifecycle lanes. Add repository behavioral tests: dead process + unjoined reader, dead process + unjoined writer both refuse spawn and retain exact refs; once those owners actually become quiet, retry may start; repeated rejected calls do not create threads/processes. Do not change independent tmp/codex_* probes.

## Compatibility + test import

`tmp/codex_gate_asr_final_switch_review_nov5s_2026_11_02.json`:121 tests,2 errors:

- `assembly.py:679` unconditionally registers `stt_engine.shutdown`, causing old injected TrackingSTT with no shutdown method to fail construction. Preserve legacy no-resource adapters: register optional callable declared shutdown only, checking strict completion for resource-owning STT. Do NOT add fake shutdown solely to old TrackingSTT to hide incompatibility. Keep new resource-owning rollback test.
- `tests/test_diagnose_asr.py:493` forgot import of redirect_stderr. Correct the import and keep the fresh-import-under-StringIO assertions.

## Ready while closing

New `_paraformer_runtime_ready` should return False when `_shutdown_cancel.is_set()` even if incomplete shutdown hasn't set `_permanently_closed` yet. For non-SafetyPolicy duck checker use strict True and controlled exception->False; no readiness model load/hash. Test close-incomplete still not-ready/no mic, pending refs retained.

Preserve ASR defaults,6GiB commit floor, installed assets, no-cloud semantics, other stack settings, formal memories and known reference. You may author/update source and meaningful fake tests; DO NOT EXECUTE tests/models/devices/install or alter root probes. STOP for Codex review. Final document status remains pending until these gates pass.
