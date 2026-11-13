# ADR：OwnedThreadTask 预取生命周期基础

## 状态

Accepted（2026-11-04，Root独立10及最终相关81均通过；见具名Root review。下一队列切片单独放行，本次不证明真实音频性能。）

## 背景

下一切片句间流式预取需要与 TFM 一致的线程 owner 语义（单次 start、unknown bootstrap、terminal≠OS 退出、join=quiet≠成功），但不应继续复制 TFM/前台 Job 内联旗标或引入池/监控线程。

## 决策

1. 新增窄 stdlib 模块 `src/core/owned_thread.py`：`OwnedThreadTask` + frozen `OwnedThreadStatus`（字段 `creator_pending`、`start_unknown`、`entered`、`terminal`、`alive`、`quiet`）。
2. **构造**仅存状态；`thread_factory` 仅用于构造未启动 `Thread`；`daemon=True` 固定，**不得**用 daemon 推断 quiet。
3. **start**：factory 前 claim+creator；锁内 cancel 准入、强持有、`os_start_committed`（在 `Thread.start()` **之前**）；锁外 `start()` 与 ack。**单一外层** `except`：记录原异常（不覆盖已有 `.error`），已 commit → unknown，原样 raise；`finally` 仅受理者收 creator。
4. **cancel**：永久 `Event`，不 clear；不宣称已停止。
5. **join**：有界 timeout；`_quiet_from_fields`；**无** entered+terminal+actual not alive **永不能**收口 `start_unknown`（snapshot 也不清 unknown）。
6. **error**：factory/start/target 均保留原对象；wrapper 捕获 target、terminal 后**不重抛**；不覆盖已有 error 伪造成功。
7. **quiet**：统一 `_quiet_from_fields`；known-no-start = 已 claim 且未 commit，**但若 thread 仍 alive 则 quiet=False**；commit 后须 entered+terminal+not alive。
8. 本切片**不**接线 App/TTS/TFM/RVC/NativeJob。

## 取舍

- lock 外 `Thread.start`/join，避免 creator 与 snapshot 死锁。
- commit-before-start 消除「start 返回后、异步 KBI 窗口误当 known-no-start」；不以 daemon/timeout 代替 actual not alive。
- 不引入 asyncio/进程池。

## 验证（仓库 / fake）

- `tests.test_owned_thread`（17）；gate `owned_thread_prefetch_foundation_20261104e` → `tmp/codex_gate_owned_thread_prefetch_foundation_20261104e_2026_11_02.json`
- Root10 由 Root 监督器复跑（Cursor 不 raw 对照 probe）

## 后续（非本切片）

- 流式句间预取登记 `OwnedThreadTask`；Root 授权前禁止接线。
