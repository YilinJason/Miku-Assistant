# Phase2a R3 — independently reproduced retirement gap

Independent fake supervisor `input_v2_retire_independent_nov12j` fails in 0.040s, no native/device use, WAV set unchanged. Probe: `tmp/codex_silero_retire_boundary_2026_11_12.py`. It blocks factory, begins shutdown, releases factory, blocks candidate.close, then calls shutdown: it incorrectly returns True while candidate close is still running.

Cause: creation flag is cleared in publication gate, but retiring reference is stored only AFTER the close returns False. There is an ownership gap during the close call. Correct it atomically: store retiring candidate AND an in-progress-close claim under lifecycle gate before clearing creation flag. Shutdown/status must report pending while that close claim exists, and must NOT call close concurrently on the same candidate. Perform close outside the gate, then clear claim in finally; only literal True clears the identity-matched ownership reference. Failed/exception closes remain retained. The same claim can cover published close to avoid concurrent shutdown double-close; prefer one simple shared mechanism over duplicated flags/helpers.

Pending should be based on retained reference/in-flight ownership, not trusting candidate.closed=True without a confirmed literal True return. Shutdown retry after all owned work is reclaimed succeeds. Remove unused helper/redundant branches introduced by prior draft where appropriate, keeping scope narrow.

Add deterministic formal regression of the exact schedule, including close False/error and retry; capture thread exceptions and finally release/join each thread, assert not alive. Existing concurrent ensure test should start t1, wait factory entered, then start t2 rather than assuming thread order by .start order; use finally cleanup on assertions.

Do not edit/run Codex reviewer probe. Run same eight-module fake gate using new label `input_v2_integration_r3_cursor_nov12k`; then STOP. No other phase or native/model/device/app startup. Codex will rerun independent boundary and broader gates.
