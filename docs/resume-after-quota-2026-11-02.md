# 额度刷新后恢复入口

2026-11-02，Codex 实时短周期 usedPercent=90、周=62，依用户 <=10% 剩余即停工规则保存。刷新 Unix 时间 1793681936；必须再次查询实时接口确认。未自动使用 reset credits。

## 当前停止位置

- 用户三项顺序：退出/回收 → 测试可靠性 → 真实等待。blocked-write fixture 已通过独立复验；持久 RVC 与此前上层传播曾独立通过，但当前单次分支修改后必须重新回归。
- 活跃切片是单次 infer/probe owned run。Cursor 官方会话 `16ecbb8c-3982-492e-ac96-ee1c48c2e778`，最新执行 23976 已 exit 0。最新返修待 Root 严格评审；未 commit/push。
- 依据 `docs/codex-review-rvc-oneshot-owned-run-2026-11-02.md` 顶部第二次评审。Root 七项旧 probe 独立通过；新增 `tmp/codex_rvc_oneshot_admission_probe_2026_11_02.py` 在返修前独立确认双启动、creator 异常不 ack。最新版本仅有 Cursor 自报通过。
- Cursor 最新声称：原子 compare-and-reserve，creator finally ack；旧7+新增2+status4；9 owned-run tests；408 gate。不得直接采信为最终验收。

## 恢复后首先核对

1. 查询额度，检查 git status，阅读 AGENTS 与相关仓库 skills，保留所有既有修改。Root 不改生产代码，继续官方 Cursor 编写、Root 设计/独立评审流程。
2. 阅读最新 owned_rvc_run.py、SafetyPolicy/TFM/App/Engine 调用方及测试。尤其核对 public retire 与新准入是否真正互斥、creator 部分成功的 owner/reader 始终可达、lease quiet 语义与异常身份。
3. **待核实的测试问题**：新增 admission probe 旧实现“检查”函数现已同时 reserve，旧 probe 的 barrier hook 可能让合法单启动也卡到诊断期限。Cursor 报 spawn_count=0 不能证明正常请求能启动。保留旧 probe，新增真实成功的受控 gate 用例，同时证明第二请求零新分配、第一请求成功、所有线程退出。
4. **待核实语义**：Cursor 当前新增 test_errors_only_does_not_imply_text，报告认为 errors-only 保持 bytes。需对照本机 Python 3.12 subprocess 实际源码/无副作用构造语义验证，不能把错误预期固化为测试。另前次要求的 bootstrap 行为矩阵未见完成证据。
5. Root 独立 probe/targeted 完成后再一个正常 Windows <=180s 扩展 gate。沙箱 asyncio.socketpair 已有堆栈证据会阻塞，涉及原 Edge fake suites 使用已授权 require_escalated 正常 Windows 上下文，不改生产 asyncio。

## 证据与工具

- `tmp/codex_scoped_test_gate_2026_11_02.py`：SafetyPolicy 管理自有测试树、硬超时、精确 WAV 集合。--probe 有固定白名单；新增 admission probe 目前不在白名单，可直接执行纯 fake 脚本（无 subprocess）或由 Root 扩展固定白名单。
- 旧七项最新 Root gate `tmp/codex_gate_oneshot_contract_revision_2026_11_02.json`：0.469秒，7 true，WAV826→826。此前825与本次826跨轮来源未核明，不能宣称本次新增。
- 已验证生产前置备份：`tmp/review-baselines-2026-11-02-oneshot/`，只用于精确比较，禁止整目录回滚。
- 官方 CLI：`tmp/cursor-cli-2026.10.22/dist-package/cursor-agent.ps1`；composer-2.5，--resume 上述会话。用户已明确授权本任务必要代码/文档发送官方 Cursor 服务及修改工作区。

## 尚未完成（不得跨越）

单次 RVC 最终验收；持久 convert 取消未知的 IO pin；完整未发布候选/加载退出所有权；回收大板块里程碑评审。随后才做三模型真实共存诊断、主程序预热接入、有界预取与取消/长句测试。真实语音长测未完成。保持原选定 Nahida 音色/默认，不启动 main/模型/设备仅为审计；正式 data/memory 不读改删。
