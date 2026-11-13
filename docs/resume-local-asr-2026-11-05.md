# Local ASR migration — current continuation point

## FINAL — migration completed and independently accepted

Current authority: `docs/codex-review-local-asr-final-2026-11-05.md`. Defaults, isolated environment, measured6GiB commit gate, missing-assets no-mic readiness, pending-I/O ownership and README are complete. Final232 related offline tests (1existing skip),18+8 root cases, installed-config no-model smoke and real `infer_final_nov5v` all pass. Real process tree/I/O/PIDs quiet; no active CLI/model/test. Do NOT repeat setup or resume old review tasks below. Current user default is Paraformer, cloud_stt=False. No commits/pushes or formal-memory access. User microphone/voice-device/low-end/combined-ASR long soak remain separate real-use acceptance.

The following entries are preserved historical work notes; references to absent env, Google default, incomplete fixes or stopped quota are superseded by the FINAL status above.

User chose Paraformer-zh + FunASR, first whole-utterance CPU phase, with official Cursor CLI writing code/tests and Codex designing/reviewing/executing. Do not replace this workflow with Codex production edits. No commits/pushes or formal data/memory access. Preserve extensive pre-existing dirty work. Stop only if Codex primary allowance remaining reaches10%; weekly quota is explicitly not a stop condition. No automatic quota reset.

## Accepted work

- Core whole-utterance PCM worker, narrow local_asr_worker permission, fixed asset/interpreter pins, CPU2 threads, NativeJob tree ownership, bounded JSON protocol / startup45s / inference15s / close2s, STT+assistant+unpublished retirement, independent text paths. See core accepted review and ADR.
- Core evidence: `tmp/codex_gate_paraformer_core_accepted_nov5g_2026_11_02.json`:127 tests (1 existing skip); `tmp/codex_gate_paraformer_root_races_final_nov5e_2026_11_02.json`:17 root tests.
- Setup/diagnostic authored and repeatedly reviewed; exact file sizes + streamed official hashes, env identity, explicit execution, truthful final diagnostic verdict, PID delegation, STT assembly rollback. `tmp/codex_gate_asr_preparation_accepted_nov5j_2026_11_02.json`:73 tests; `tmp/codex_gate_asr_preparation_root_fixed_nov5i_2026_11_02.json`:8 independent fake verdict tests.

## Current issue / active work

Latest update (after the earlier setup refusal was repaired): setup **SUCCEEDED**, `tmp/asr_gate_prepare_nov5n.json`,369.547s, NativeJob fully quiet. `.venv-asr` + all official model files are now installed and validated; `tmp/paraformer_asr_setup_5eb952939a2c.json` records exact pins and72 packages. Latest setup runner suite `tmp/codex_gate_asr_setup_runner_accepted_nov5m_2026_11_02.json`:82 tests OK.

First real CPU ASR run `tmp/asr_gate_infer_nov5o.json`: 8 fixed clips + repeats + production STT wrapper all succeeded; cold13727.93ms, warm p50=265.79ms/p95=505.52ms. Outer diagnostic20.094s and NativeJob cleanup_complete=True. Child final report false ONLY because immediate PID-gone probe was unconfirmed, although independent post-exit checks found both49112/53376 absent. Cursor is currently implementing `docs/codex-review-asr-first-real-inference-2026-11-05.md`: release diagnostic's retained proc reference before PID check, bounded conservative settle, move report root out of shared data/tmp to repo tmp. Original report to preserve: `data/tmp/paraformer_asr_diagnostic_e528d04932804be3beed5b01748b63bf.json`. No active model/installer process remains.

**Resource follow-up before default activation:** outer supervisor observed peak total tree RSS2285.23MiB/private4520.33MiB (10 samples), min RAM45322.86/commit38749.14. Current preliminary ASR startup commit admission4096MiB is below observed cold peak; raise Paraformer-only commit floor to at least6144MiB (physical4096 remains supported by measured RSS), with explicit evidence/tests. Do not lower other model guards. This change has NOT been authored yet. User primary Codex remaining was14% at last check (used86%); stop at remaining10%, no automatic reset.

Next after Cursor STOP: inspect the two diagnostic fixes, run targeted fake suite/root8+17, then unique supervised real diagnostic. Only after actual process/PID/ref/WAV/report acceptance, dispatch final default/config/README patch. Current user config STILL Google, so no incomplete ASR default was exposed.

First actual supervised installer attempt was safely refused by existing safe_subprocess_run sanitation (env/cwd forbidden), before env/model creation. All child processes exited. `tmp/asr_gate_prepare_nov5k.json`, `tmp/paraformer_asr_setup_e43c518f8109.json`. Do not weaken shared safety to fix this.

Cursor official session `027e8438-6142-4d10-a86d-3340dafa3eec` is currently implementing `docs/codex-review-asr-setup-execution-2026-11-05.md`: use policy-supported Popen + actual NativeJob per command/trampoline, bounded metadata capture, correct cleanup, fake policy-boundary tests. Codex must wait for STOP, inspect code/callers, then run offline checks through existing scoped gate. Previous core Cursor session was `91265b8e-2f72-40be-a25f-5fe396bb9362`; use current prep session for follow-ups.

## Execution after next review

- Main interpreter: `C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe` (3.12.3). Do not install into main/RVC/Cosy. New `.venv-asr` currently absent; model `vendor/paraformer-zh` absent. Approved package pins: CPU torch/torchaudio2.6.0+cpu, FunASR1.4.22, numpy1.26.4, transformers4.49.0. HF model revision `d7811ee3ac581fbcfdeb37c98c6ba674028433dc` with immutable pins in assets.py; no optional VAD/punc/speaker/GPU model.
- Only existing offline test supervisor `tmp/codex_scoped_test_gate_2026_11_02.py` with explicit module names / allowlisted root probes, unique --label and bounded timeout. New root probes are `codex_paraformer_root_2026_11_05.py` (17) and `codex_asr_preparation_root_2026_11_05.py` (8). Never run raw full discover or parallel tests.
- Actual setup/diagnostic must use root `tmp/codex_asr_owned_gate_2026_11_05.py prepare|diagnostic --label UNIQUE` via main Python. It uses SafetyPolicy/NativeJob, hard deadline1200s setup/180s diagnostic, memory preflight8GiB physical+commit, abort6GiB physical/4GiB commit,2MiB log cap, max256 samples and actual Job cleanup. Parent must give progress updates while tool session runs. Child entry joins supplied Job. Unique report `tmp/asr_gate_LABEL.*`; preserve earlier failures.
- After successful setup, run fixed-audio diagnostic and inspect actual transcripts (synthetic/official only), cold/warm times, tree RSS/private/handles/thread samples and strict actual shutdown/PID evidence. Production stderr is DEVNULL; a failed model startup may require a bounded root-only trusted diagnostic capture through policy injection, not weakening capability kwargs.

## Remaining after real acceptance

Final continuation note at primary allowance11% remaining: Cursor completed the diagnostic-only update (repo tmp report root, shared data/tmp snapshots,2s conservative PID settle, local `del launcher`). Review REJECTED a temporary `_release_diagnostic_handle_refs` which erased production owners; Cursor has now REMOVED that helper and added incomplete-close retention tests. Current Cursor session is stopped; no active installer/model remains. Do not restart old setup from scratch; env+model are already installed. Run/review `tests.test_diagnose_asr` latest gate `asr_diagnostic_retention_nov5p`, update root probe adapter for `verify_pids_gone_with_settle(..., **kwargs)` if necessary (do not weaken assertions), and rerun supervised real diagnostic with a NEW label. Previous report in data/tmp must be preserved. Default switch/README/current-delivery and measured6GiB commit floor are still NOT implemented. Do not claim migration fully complete until those pass review/tests. Stop at10% primary remaining as user required.

Cursor final patch: switch app config + Config defaults/fallbacks to Paraformer, local_asr_worker=true, cloud_stt=false; keep TTS/RVC/LLM/memory and retention settings unchanged. Update README/setup commands, limits, first-use cold latency, no cloud fallback, manual legacy rollback, ASR-only offline boundary (Edge TTS remains online), ADR/handoff/current delivery evidence. Codex final review and targeted regression; fixed audio is not user-microphone or low-end-device accuracy certification. No automatic prewarm change until actual latency is measured.

## Preserved data / evidence

- Shared legacy data/tmp miku WAVs:828; exact pre-real metadata snapshot `tmp/paraformer-before-real-wav-metadata-20261105.json`. No cleanup of old unknown files.
- Nahida reference `data/test/voice-naturalness/cosyvoice-instruct/01_warm_companion_rvc.wav` SHA256 `7c4aee8b69428e4bbe3bb773cfc47efae1618efb46b7b80ebaf1465739fe19ba` unchanged.
- Pre-task source hashes `tmp/paraformer-before-source-20261105.json`; core snapshot `tmp/paraformer-core-accepted-source-20261105.json`. Upstream metadata `tmp/paraformer-hf-official-metadata-20261105.json`; inspected wheel `tmp/funasr-1.4.22-inspect.whl`. Root tool CLI path `tmp/cursor-cli-2026.10.22/dist-package/cursor-agent.ps1`.
- Previous three-model 30min soak remains a separate accepted Nov5 milestone, not evidence that new ASR was tested. Default YAML still Google pending final accepted migration.
