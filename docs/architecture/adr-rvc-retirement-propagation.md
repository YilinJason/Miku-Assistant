# ADR：持久 RVC 关闭结果向 TTSEngine 与 App 传播

- **状态**：待 Codex 严格复审（契约整理：core/shutdown_status + 对象级 getattr_static）
- **日期**：2026-11-02
- **任务**：`docs/cursor-task-rvc-retirement-propagation-2026-11-02.md`

## 问题

生产代码曾依赖 `unittest.mock` 类型特判；API 检测只看类型而非对象静态属性，漏掉 instance 声明并误放行 noncallable。

## 决策

1. **stdlib 契约模块** `src/core/shutdown_status.py`：dataclass、字段常量、`shutdown_completion_flag`、`engine_declares_shutdown_api(engine, name)`（`inspect.getattr_static(engine, name, sentinel)`）、`verify_aggregate_shutdown_status`（先验全部字段为 `bool`，再验全 `True`；错误仅字段名/稳定类别）。
2. **Engine** 严格 `is True` 完成语义；从 core 导入并 **re-export** 既有公开名。
3. **App** 无 Mock 特判：聚合 API 静态存在 → 校验；否则 Cosy getter 同样静态存在才走 Cosy 路径；`engine_type=='cosyvoice'` 且无 Cosy getter 仍拒绝；全无声明的旧 fake 保持 shutdown-only 兼容。静态声明 `local_cosyvoice_shutdown_status` 的引擎（含 Edge 等非 Cosy 假引擎）**不得**用 `None` 声称完成；须返回 `complete is True` 的状态对象后 App 才推进 TFM。生产 `TTSEngine` 正常走 aggregate `get_shutdown_status`，不依赖 Cosy getter。
4. 其余 TFM/loader/RVC 边界同前切片。

## 明确未覆盖

单次 infer/probe、C2b2b candidate、真实模型/data/memory。

## 回退

删除 `shutdown_status.py` 与 App 分支，恢复旧检测（不推荐）。
