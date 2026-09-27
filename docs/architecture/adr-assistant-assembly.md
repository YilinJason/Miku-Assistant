# ADR：助手依赖装配与业务编排分离

- **状态**：已采纳（第二小步）+ 关闭容错（第三小步）+ **装配失败回滚（第四小步，行为修复）**
- **日期**：与 `docs/engineering-baseline.md` 本轮更新同步

## 问题

`MikuAssistant.__init__` 将配置加载、日志、目录、引擎创建、清理器启动与会话编排绑在一起，导致无法在不触碰真实设备/服务的情况下对 `process_text_input` / `process_voice_input` / `speak` / `shutdown` 做离线行为测试。

另：既有 `shutdown()` 任一步异常会中断后续释放，且重复调用会再次执行清理；成功日志在部分失败时仍可能误导（第三小步前）。

## 决策

1. 新增 `src/core/assembly.py` 的 `assemble_default(...)`，集中默认副作用装配；构造器可通过关键字替换以便测配置传递。
2. `MikuAssistant(config_path=...)` 仍走默认装配，对外调用方式不变。
3. 新增 `MikuAssistant.from_dependencies(...)`：只绑定传入组件；缺失可选槽位保持 `None`，不自动创建真实引擎/清理器；不加载配置文件、不配置文件日志、不创建目录、不启动清理任务。
4. 默认路径与注入路径共用同一套业务方法（`_bind_parts` + 既有编排代码）。
5. 不引入 DI 框架；不新增流式、播报队列、取消或长期记忆。
6. **（行为修复）** `shutdown()` 容错与顺序幂等，见下节。
7. **（行为修复）** `assemble_default` 致命失败时逆序回收已登记资源，见「装配失败回滚」。

## 生命周期归属

| 组件 | 默认装配 | `shutdown` 时 |
|------|----------|----------------|
| `tts_engine` | 助手创建并持有 | **助手调用** `shutdown()` |
| `file_cleaner` | 助手创建并 `start_auto_cleanup` | **助手调用** `stop_auto_cleanup`；**仅当停止成功**再 `cleanup_now()` |
| `character_interface` | 配置启用时创建 | **助手调用** `close()` |
| `config` / `safety_policy` / `llm_client` / `stt_engine` / `windows_controller` / `chrome_searcher` / `router` | 默认路径由装配创建；注入路径由调用方提供 | **不由助手关闭** |

注入路径若传入 `tts_engine` / `file_cleaner` / `character_interface`，仍按上表由助手在 `shutdown` 中关闭；测试可用 fake。

可选注入 `logger`：若传入则 `self.logger` 为同一对象；未传入则 `logging.getLogger("src.core.assistant")`。

## 关闭流程行为变化（第三小步，明确行为修复）

| 项 | 变更前 | 变更后 |
|----|--------|--------|
| 顺序 | TTS → stop → cleanup → character | **不变** |
| 单步异常 | 可能中断后续 | 记录 `component`/`operation`/异常后**继续** |
| stop 失败 | 仍可能调用 cleanup | **跳过** `cleanup_now`，仍关闭角色 |
| 重复 `shutdown()` | 可能再次执行各步 | 整次流程标记后**跳过**；不重试失败步 |
| 成功日志 | 末尾无条件「助手已关闭」 | 仅全部成功时输出；否则「关闭流程已结束但部分资源释放失败」 |

**不宣称解决**：并发关闭、播报中退出、RVC Worker 底层释放、初始化失败部分回收。

## 装配失败回滚（第四小步，明确行为修复）

| 项 | 约定 |
|----|------|
| 登记时机 | TTS / 角色 **创建成功**后立即登记；清理器在**已取得对象**后登记 `stop`（含 `start_cleaner=False`） |
| 成功返回 | `abandon()`：不提前回收，生命周期交给助手 |
| 致命失败 | 逆序执行登记项：cleaner `stop_auto_cleanup` → character `close` → TTS `shutdown` |
| 禁止 | 回滚中不调用 `cleanup_now`；不删运行数据；不关 LLM/STT/系统/搜索 |
| 回收失败 | 记录后继续其他项；**原样重新抛出装配异常**（对象身份不变） |
| 清理器构造失败 | 记录，继续装配（不阻断启动） |
| 清理器启动失败 | **阶段一**立即尝试 stop；成功则不交给助手且不登记回滚；stop 也失败则保留引用并登记，供**阶段二**致命回滚再尝试一次（或成功装配后交助手 shutdown） |
| 回滚阶段 | 同一登记动作最多执行一次（与阶段一立即 stop 分开计数） |

**未解决限制**：构造器抛错且未返回对象时，构造器内部已分配资源的泄漏不在本层回收范围内（须由各构造器自行保证或另案处理）。LLM/STT/Windows/Search 成功创建后若后续失败，本层亦不关闭它们。

### 离线验证（回滚边界补齐后）

```text
py -3 -m unittest discover -s tests -v
→ Ran 39 tests in 0.027s … OK
```

`test_assembly_rollback.py` 现 11 项，新增边界：

| 用例 | 断言要点 |
|------|----------|
| `start_cleaner=False` 后路由失败 | 登记并执行 stop 恰好一次；`start`/`cleanup_now` 均为 0 |
| 多资源回收均抛错 | 逆序 cleaner→char→TTS 各一次；错误入日志；最终仍为原始装配异常对象 |
| 启动失败 + 立即 stop 失败 + 路由失败 | 阶段一 stop 1 次 + 回滚阶段再 1 次（共 2）；保留原始路由异常；仍回收 TTS；无 `cleanup_now` |
## 替代方案

- 在 `__init__` 增加大量 `optional` 参数：易与 `config_path` 混淆，拒绝。
- 引入 DI 容器：超出本步范围，拒绝。
- 关闭失败自动重试：与「每步最多一次 / 幂等」冲突，拒绝。

## 取舍

- **利**：编排可测；关闭更不易半残；日志区分「流程已跑」与「全部释放成功」。
- **弊**：LLM/STT 等未纳入装配回滚清单；构造器未返回对象时的内部泄漏未覆盖；并发 shutdown 未加锁。

## 迁移

- 生产入口继续 `MikuAssistant(config_path)` / `main.py`。
- 测试使用 `from_dependencies` + fake。
- 依赖「shutdown 抛错即中止」或「二次 shutdown 再清一次」的外部代码需按新语义调整（当前仓库 CLI 仅在 `finally` 调一次）。
