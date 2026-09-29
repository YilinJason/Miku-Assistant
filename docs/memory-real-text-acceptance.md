# 长期记忆真实文本验收

本工具用于在本机 Ollama 上验收记忆提取与文本召回链路。默认 **dry-run**，不联网、不建库；只有显式 `--execute` 才会发 HTTP，并始终使用合成对话与临时 SQLite，**不写入正式记忆库**。

一次样本通过 **不代表** 普遍质量保证。2026-09-27 首次真实样本 exit 2（算术/注入误提取）；v2 内容通过但 close incomplete 且同模型 `num_ctx` 8192↔2048 重建；修复后的真实 v3 已由用户复跑并 exit 0：四场景、wrapper 双 idle、完整 close 与临时目录清理均通过，日志只加载一次 8192 runner。

## 命令

```powershell
# 默认 dry-run：校验并打印 keep_alive / num_ctx / num_predict
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 diagnose_memory.py

# 真实执行（需本机已有 Ollama；Cursor/自动化不得擅自跑）
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 diagnose_memory.py --execute `
  --keep-alive 1m --report tmp/memory-text-report.json

# 若需显式限制提取器 context（会写入 options.num_ctx）：
#   --num-ctx 2048 --num-predict 512
```

## 安全与资源边界

- 不要求 `memory.enabled=true`。
- **诊断 keep_alive 默认 `1m`**，可用 `--keep-alive` 覆盖；**不继承**主聊天 `ollama.keep_alive=30m`。产品启用时仍由 `memory.extractor.keep_alive`（可回退 ollama）决定。
- **`num_ctx` 默认 inherit（配置 `null`）**：不向 options 写入 `num_ctx`，避免同模型与前台默认 context 来回重建 runner。显式正整数或 CLI `--num-ctx` 仍写入。`num_predict` 默认 512。不改变主聊天客户端配置。
- wrapper：seed 与 query **各一次**有界 idle；最终 idle 超时为协议失败，不是内容 warning。
- incomplete close 后有界 reclaim（wait idle → 重试 close → 再删目录）；仍无法回收则 `temp_dir_cleanup=retained_incomplete_close`（失败证据，非「已安排延后」）。禁止在 worker 存活时删目录或强杀线程。
- endpoint URL：缺 scheme → `http://...`；仅 http/https；拒绝 query/fragment/非法端口。
- 不启动/停止 Ollama，不拉模型，不改全局环境/注册表。
- `--execute` 只用临时库；不写正式 `memory.db_path`。
- 报告不含完整 prompt/回复/记忆正文、凭据或本机绝对临时路径。

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 通过（本样本） |
| 1 | 契约失败 |
| 2 | 质量未达标或参数/配置错误 |
| argparse 非零 | 参数解析错误 |

## 报告字段（schema_version=3 / tool 1.2.0）

- `endpoint`：host/port/model/timeout/**keep_alive**/**num_ctx**（null=inherit）/**num_predict**
- `wrapper`：`idle_wait_ok`（聚合）+ `seed_idle_ok` + `final_idle_ok`
- `temp_dir_cleanup`：`completed` | `retained_incomplete_close` | `failed` | null（dry-run）
- `scenarios[]`：墙钟 `elapsed_ms`；Ollama 阶段耗时与 token 计数（缺失或畸形为 null）；`skipped_locally`
- 其余字段同前（写入/去重/recall/close/passed/warnings/failures）

旧 schema v2 报告可作历史对照；新契约以 v3 为准，不做迁移。

## 结果解读

- ephemeral_math / prompt_injection 期望无候选；稳定偏好/资料期望有候选。
- 短 keep-alive 是诊断行为，不是产品默认。
- 本地资格规则不能声称已证明真实模型质量；复跑 `--execute` 后才能更新真实结论。
