# ADR: Cosy runtime worker 本地 Modelscope 导入边界

## 状态

Accepted（2026-11-03，Root 边界 + scoped gate 通过后待短真实对照）

## 背景

两模型 `--prepare-only` 实测 `wetext_import` 约 14.391s，其中 `wetext.wetext` 在 import 时 `from modelscope import snapshot_download` 会拉取完整 Modelscope SDK；生产随后在 worker 内将该函数替换为固定本地 FST 路径，SDK 在本路径上无需网络模型发现。Cosy `AutoModel` import 亦仅依赖同一下载符号。

## 决策

在 **专用 Cosy runtime worker**（`scripts/cosyvoice_runtime_worker.py`）导入 `wetext` 与 `cosyvoice.cli.cosyvoice` 时，通过 `src/voice/cosyvoice/local_modelscope_import.py` 提供 **短生命周期** 的 `sys.modules` stub：

- `resolve_trusted_wetext_root`：`path_trust` + 固定 `PROJECT_ROOT` / `WETEXT_DIR_REL`；拒绝任意目录与 symlink 逃逸。
- 仅绑定顶层 `modelscope.snapshot_download` 到本地 resolver（已知 repo `pengzhendong/wetext` → 已 pin 的 wetext 根目录）。
- `repo_id` 须为精确 `str`（禁止 `str(object)` / 宽化）；未知 repo / 额外 args/kwargs → fail-closed。
- `_ThreadBoundSnapshotModule`：owner-thread 才可访问 `snapshot_download`；退出 scope 时 **不** 覆写 foreign 替换的 `sys.modules['modelscope']`。
- 禁止嵌套与并发 scope（全局 depth + RLock）。
- import 结束后，`wetext.wetext.snapshot_download` 仍指向 **同一 strict local** 函数。

`_import_cosyvoice_automodel_class(cosy_root)` 保持可测：`wetext_dir` / `repo_root` 可选，默认走 `resolve_trusted_worker_paths`。

不改变 vendor、site-packages、默认 30s startup、声学/Normalizer 语义、Native Job、TFM、SafetyPolicy。

## 未声称

- 未单独测量 Modelscope SDK 子成本；不宣称 14.391s 全部消除。
- 未验证 constructor 在 30s 内完成；需 Root 短真实 `--no-ollama --prepare-only` 前后对照。

## 验证

- `tests/test_cosyvoice_local_modelscope_import.py`（含 fake MetaPathFinder 导入链；主解释器无 wetext 时 integration skip 不代表 vendor 缺包）
- Root：`tmp/codex_local_import_root_boundaries.py`（**10** 项，断言不可改）
- 120s 模块回归（Root 独立复跑）：`tmp/codex_scoped_test_gate_2026_11_02.py --modules` …  
  旧 `test_edge_status_none_still_closes_tfm` 已按 `docs/codex-review-cosy-local-import-startup-2026-11-03.md` 顶节迁移为 `test_edge_declared_status_unknown_defers_tfm_until_complete`（仅测试+ADR，对齐 ADR-rvc-retirement 决策 3；**不改 Core**）。
