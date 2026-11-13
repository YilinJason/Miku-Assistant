# 交接：CosyVoice retained 被动对账（Cursor → Root）

## 目标

修复 TFM 已不再 owned 但 `CosyVoiceTtsBackend._retained_outputs` 仍导致 turn stream admission 永久 busy 的漂移；**被动**同步集合，不改变 synth gate、retire 或 TFM pending 语义。

## 生产改动

| 文件 | 内容 |
| --- | --- |
| `src/voice/tts/cosyvoice_backend.py` | `reconcile_released_outputs()`；`_retained_revision` 与 mutator 递增 |
| `src/voice/tts/engine.py` | `_assert_turn_stream_admission_resources`：pending 后、`retained` 计数前 optional reconcile（`getattr` 兼容无该方法的 fake） |

## 并发与异常证明（Root 复审点）

- **锁序**：快照/写回用 `_lifecycle_lock`；`is_owned` 在锁外只读调用（与 `_release_retained_outputs` 复核段一致）。
- **revision**：快照后任意 retained mutator 递增 revision → 本轮 `difference_update` 放弃，避免误删并发新增。
- **异常**：`is_owned` 的 `BaseException` 不捕获；retained 集合保持可重试。
- **状态保留**：`_pending_worker_retire`、`stop_failed` 不被 reconcile 清除；仍 owned 的条目不被 prune。

## 测试（纯 fake，Root 相关 107/107 已通过）

| 模块 | 要点 |
| --- | --- |
| `tests/test_cosyvoice_retained_passive_reconcile.py` | **纯** fake ownership/client；真实 backend + `TTSEngine.__new__` admission；revision 覆盖见 `test_revision_bump_via_mark_failed_aborts_stale_prune` 与 `test_samepath_remark_aborts_stale_prune`（**已删**重复的 `test_concurrent_mark_failed_in_hook_with_join`；Root5 并发探针另证） |

Root 最后仅复验本模块即可（不必重复无变化的相关 107）。`cosy_retained_root_repo_20261104b` 已 WAV826 零差。

## Cursor 静态检查（fixture 删项轮）

| 项 | 结果 |
| --- | --- |
| `compileall` | `test_cosyvoice_retained_passive_reconcile.py` |
| `git diff --check` | 上述测试 + handoff |
| unittest / gate | **未运行** |
| 生产 | **未改**（冻结） |

## Root 待办

- 仅复跑 `tests/test_cosyvoice_retained_passive_reconcile.py`（删并发重复项后）
- 通过后 ADR 标 Accepted

## STOP

未提交、未推送；未改 TFM/CPU/LLM/声音配置权限/正式 memory；未触真实模型设备。**不进其它切片。**
