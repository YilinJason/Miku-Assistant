# Local ASR core milestone — accepted for asset preparation

Official Cursor authored production code and repository tests; Codex designed, inspected callers and independently ran all checks. Earlier failed rounds remain recorded in the three review documents and unique tmp gate reports; they are not treated as passes.

Latest evidence:

- `tmp/paraformer_core_accepted_nov5g.json`: 127 related tests OK (1 existing skip), 4.636s test time. Covers STT contract, two ASR test modules, assistant activation, assembly rollback/lazy defaults and host memory admission.
- `tmp/paraformer_root_races_final_nov5e.json`: 17 independent Codex boundary cases OK; includes realistic 1s/15s PCM, Windows env, strict status/settings, capability kwargs, blocked-lock close bound, startup/close race, partial I/O failure, quiet-after-failed-close retry, combined caller/shutdown cancellation, no stale success and network guard without network attempts.
- Every gate retained the exact 828 shared WAV names; selected Nahida reference SHA256 still `7c4aee8b69428e4bbe3bb773cfc47efae1618efb46b7b80ebaf1465739fe19ba`. Formal memory database was not accessed.

Design reviewed: fixed `.venv-asr` and pinned Paraformer assets; CPU-only 2 threads; no main-process torch/model load; no automatic cloud fallback; bounded JSON requests/results, deadlines and one inflight operation; Windows NativeJob tree ownership; retryable shutdown including STT/assistant/unpublished candidates; missing ASR assets do not disable text mode or a valid TTS profile. Existing TTS behavior preserved after review corrections. Runtime guard is Python-level outbound blocking, not an OS sandbox.

This milestone does NOT prove model inference, microphone accuracy, actual memory use or latency. Default config remains Google until separate setup + real fixed-audio acceptance. The next authorized step is `docs/cursor-task-local-paraformer-preparation-2026-11-05.md`: Cursor authors setup/diagnostic/testing, Codex reviews then executes under owned hard supervision. No commits/pushes.
