# Codex 最终评审：长期记忆关闭完整性与 context 稳定性

## 结论

本里程碑最终验收通过。Round 1 的临时目录持续泄漏与失真 `DEFAULT_NUM_CTX` 常量均已关闭；用户随后运行的真实 v3 验收也通过，未发现新的阻断缺陷。

真实命令由用户显式运行；Codex 只读复核报告、Ollama 日志与进程/显存状态，没有启动或停止 Ollama、主程序、语音或设备，没有提交或推送。

## 已验证行为

1. wrapper 在 seed 与 query 后分别执行有界 idle 等待；最终后台任务未完成时返回 `final_idle_timeout`，不会提交假通过报告。
2. `memory.extractor.num_ctx` 默认 `None/inherit`；提取 payload 省略 `options.num_ctx`。显式整数仍严格校验并写入。主聊天配置未被修改。
3. 第一次契约 close incomplete 时，报告固定保留 `close:incomplete` 和非零退出码。
4. reclaim 有界等待 idle，重试 `runtime.close()`；只有确认 worker 停止、store 关闭后才删除本轮临时目录。
5. reclaim 仍不能完成时如实报告 `retained_incomplete_close`，不再使用虚假的 deferred 状态，也不强杀线程或删除活动 SQLite。
6. 测试通过可注入目录管理自己的临时文件；全量测试不再增加 `%TEMP%/miku-memory-diag-*` 目录。
7. 未使用且名称失真的 `DEFAULT_NUM_CTX=2048` 已删除。

## Codex 独立验证

```text
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
→ Ran 288 tests in 7.711s … OK

C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
→ OK

git diff --check
→ OK（仅既有 LF/CRLF warning）

%TEMP%/miku-memory-diag-*：测试前 23，测试后 23
```

另做了不访问网络的真实 worker 动态复现：最终提取被 Event 阻塞，第一次 close 得到 `worker_alive=true / pending=1 / incomplete=true`；随后释放 worker，reclaim 成功关闭 runtime/store 并删除本轮目录。最终报告仍为 exit 1 且保留 `close:incomplete`，符合契约。

## 用户真实 v3 验收

用户运行：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 diagnose_memory.py --execute --report tmp\memory-text-report-v3.json
```

报告 `tmp/memory-text-report-v3.json`：

- schema 3 / tool 1.2.0；`num_ctx=null`（inherit）。
- 四个场景均 pass；算术与注入均本地跳过且无候选。
- wrapper pass；`seed_idle_ok=true`、`final_idle_ok=true`、`idle_wait_ok=true`。
- close：`drained=true / worker_alive=false / pending=0 / incomplete=false`。
- `temp_dir_cleanup=completed`；failures/warnings 为空；exit 0。
- Ollama 0.34.4 日志在本次窗口只启动一次 `-c 8192` runner，之后所有前台/提取请求复用同一 runner；未再出现 8192↔2048 重建。
- 超过 keep_alive 后无 Python/`llama-server` 残留；Ollama 服务本体约 106 MiB，GPU 回到约 1.68 GiB 系统基线。
- `%TEMP%/miku-memory-diag-*` 仍为历史 23 个，本次没有新增。

与真实 v2 失败样本相比：

```text
pref_drink wall：46939 ms → 6643 ms（下降 85.8%）
pref_drink load：28581 ms → 3695 ms（下降 87.1%）
wrapper wall：24325 ms → 14055 ms（下降 42.2%）
```

v3 wrapper 还包含 v2 缺失的最终后台 idle 等待，因此 wrapper 比较偏保守。

## 剩余边界

- 一次合成样本通过不代表长期普遍质量保证。
- 长时间 soak、运行中显存峰值曲线、独立小模型与端到端陪伴仍未验证。
- 历史遗留的 23 个临时目录未删除；每个约 20 KiB，不是此前 GPU 卡顿来源，需用户另行授权后才能清理。
