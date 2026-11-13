# Local ASR real milestone — accepted for final migration

Codex independently reviewed Cursor's diagnostic fixture corrections, strict retained ownership, separate report/shared-WAV paths and measured Paraformer memory gate (4096MiB available physical /6144MiB available commit). Shared RVC/Cosy guards unchanged.

Evidence:

- `tmp/codex_gate_asr_resume_contracts_nov5q_2026_11_02.json`:96 related fake tests OK (1 existing skip).
- Root independent probes: `asr_resume_root_verdict_nov5q`:8 OK; `asr_resume_root_lifecycle_nov5q`:17 OK, under existing scoped gate.
- `tmp/asr_gate_infer_nov5r.json`: actual CPU inference exit0,20.109s, NativeJob cleanup_complete=True, no abort. Full final child report `tmp/paraformer_asr_diagnostic_4009d86f1dd642ccb24f8d999e1a6f14.json`: complete/report_written/all8 clips/wrapper/shutdown/STTshutdown/PIDs-gone/reference/WAV metadata all True. Actual recorded PIDs44120/52940 independently absent after exit.
- Cold first transcription12129.38ms (includes process imports/model loading and13.052s official audio inference); warm p50=340.58ms, p95=673.82ms across mixed fixed clips/repeats. These are inference timings after audio capture; no mic, speaker, LLM/TTS/RVC session tested here. No accuracy score or low-end-hardware certification inferred from nonblank results.
- Outer sample tree peak RSS2268.77MiB/private4507.29MiB; min available RAM45060.09MiB / commit38661.85MiB. The6144MiB commit gate covers observed cold peak plus headroom. Actual process retirement, not Python GC alone, releases model memory.
- All828 shared miku WAV exact size/mtime metadata unchanged against pre-real snapshot; selected Nahida WAV SHA256 remains7c4aee8b69428e4bbe3bb773cfc47efae1618efb46b7b80ebaf1465739fe19ba. Formal memory DB was not accessed.
- Previous failed real report preserved by Codex from shared data/tmp to same basename under repo tmp: `tmp/paraformer_asr_diagnostic_e528d04932804be3beed5b01748b63bf.json`. No old/unknown files deleted. Main/RVC/Cosy environments unchanged; ASR isolated setup previously validated.

Final authorized phase: Cursor defaults/docs patch, Codex final review/targeted regression. No commit/push. Actual mic accent/noise accuracy and long full-voice soak remain distinct user acceptance beyond this fixed-audio migration gate.
