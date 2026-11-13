# Phase1 R2 — real graph metadata and remaining small boundaries

Native supervised run `input_v2_native_first_nov5d` loaded the fixed CPU model, then correctly rejected graph metadata because adapter assumptions are too concrete. Full evidence tmp/silero_native_20261105_3db4045577ad.json:

```
input float [None,None]
state float [2,None,128]
sr int64 []
output float [None,1]
stateN float [None,None,None]
providers [CPUExecutionProvider]
```

1. Fix _shape_entry_compatible to accept None / symbolic str as UNKNOWN graph dimensions compatible with known runtime dimensions, and require matching positive concrete int where specified (bool invalid). Use expected runtime shapes [1,576], [2,1,128], [], [1,1], [2,1,128]. A symbolic graph shape is not an invalid runtime tensor. Keep strict runtime output numpy.float32 shapes and finite checks. Test actual metadata fixture above plus wrong concrete dim, wrong rank/type, extra nodes, invalid bool/negative shape. Do not loosen runtime actual tensor validation or skip graph checking.

2. close_lock_timeout_sec / close(timeout_sec=...) currently float-coerce booleans, NaN/infinity/negative/huge values and can throw/hang in acquire. Validate real nonbool finite numeric 0..2 seconds before using, including constructor. Invalid argument raises ValueError before setting closing/event or native work; tests verify object stays usable after invalid close timeout. Keep timeout0 for deterministic tests. Bound waits, retain pending session, retry semantics.

3. _path_has_symlink_in_chain checks is_symlink only, which misses NTFS directory junctions/reparse points. Use lexical lstat and FILE_ATTRIBUTE_REPARSE_POINT (or is_junction on Python3.12) on fixed asset/parents under repo; reject and don't read the target. No registry or external target scan. Fake stat/reparse tests avoid creating privileged real junctions. Canonical root may be resolved once for explicitly supplied trusted repo root, but never resolve away vendor/asset links before checking.

4. Add deterministic close-during-model-factory-load barrier test and join/exception assertions, not just close during run. Add partial-construction cancellation at final publish check if needed; no late ok. Keep existing verified-bytes/fail-closed behavior. Optionally explicitly disable ORT telemetry/profiling in real CPU creation (mock in fake tests); no app-wide permission/network changes.

Run same authorized fake gate with NEW label input_v2_adapter_r2_cursor_nov5e; preserve failed artifacts, no real model/device/pip. Stop after pass; Codex repeats real native A/B. No default integration yet.
