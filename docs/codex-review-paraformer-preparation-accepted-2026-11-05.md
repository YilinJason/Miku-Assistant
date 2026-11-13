# ASR preparation milestone — accepted for supervised execution

Official Cursor authored setup/diagnostic/requirements/tests; Codex independently reviewed and executed fake checks. Rejected earlier attempts remain recorded in review documents and unique gate reports.

- `tmp/codex_gate_asr_preparation_accepted_nov5j_2026_11_02.json`: 73 tests OK, including preparation, diagnostic, both core ASR modules and assembly rollback.
- `tmp/codex_gate_asr_preparation_root_fixed_nov5i_2026_11_02.json`: 8 independent Codex fake diagnostic success/failure cases OK (including report persistence, inference/empty/wrapper failure, incomplete shutdown, changed WAV/reference and failed report write).
- Existing core milestone: 127 related tests (1 existing skip) + 17 independent lifecycle/contract cases.
- Read-only prepare --check confirms Python3.12.3, pinned requirement entries, host memory and >700GB free D: disk; new env/model files are missing as expected. No models/devices started yet.

Reviewed setup uses explicit --execute, separate `.venv-asr`, exact CPU package pins, isolated pip with no cache and bounded retries/timeouts, pinned official HF assets with exact sizes and streamed hashes, no overwrite of unknown/raced destination, exact-owned partial cleanup. Runtime dependency environments remain separate. New assembly STT rollback is covered. Diagnostic uses fixed speech only, rejects errors/empty as success, requires shutdown/PID disappearance/unchanged reference and WAV metadata and persisted final report. Process liveness delegates to the already reviewed Windows helper.

Next: execute through `tmp/codex_asr_owned_gate_2026_11_05.py` (NativeJob + physical/commit admission, hard deadline, bounded log/sample count), then review actual installation and fixed-audio evidence. Default config remains unchanged until real inference acceptance. No commit/push and no formal memory DB access.
