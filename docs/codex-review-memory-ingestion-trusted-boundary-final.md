# Codex 最终验收：长期记忆写入可信边界通过

## 结论

长期记忆提取的本地可信边界、整段语境约束和并列事实原子性已通过最终离线验收，未发现新的阻断项。

## 已确认的不变量

1. 模型只能选择本地候选 `unit_id`；不能生成或覆盖 `text`、`value`、`category`、`evidence_quote`。
2. `ExtractedMemory.text/category` 只由本地解析结果和 renderer 产生。
3. 标点 segment 在并列拆分前检查疑问终止符、疑问槽位/语气、不确定语气和已知句尾否认。
4. `并且|而且|以及` 采用 all-or-nothing：任何空 child、重复连接词、未解析 child 都使整个 segment 失败关闭，且不回退单事实贪婪解析。
5. 跨标点的合法兄弟可独立保留；原文 span 保持 `text == user_text[start:end]`。
6. 无安全候选时不发送提取 HTTP；模型不能借助手回答或恶意返回字段创建新事实。

## Codex 独立验证

- 历史污染、整段问句/否认、非法连接词和合法事实共 29 条独立矩阵：0 失败。
- `tests.test_memory_trusted_boundary` + `tests.test_memory_provenance`：33 tests / OK。
- 全量 `unittest discover -s tests -q`：379 tests / OK。
- `compileall`：OK。
- `git diff --check`：OK（仅 Git 的 LF/CRLF 提示）；关键新文件另行检查，无尾随空白。
- 删除前以 URI `mode=ro` + `PRAGMA query_only=ON` 检查 live SQLite：count=9，验收过程未修改数据库。
- 未运行真实 Ollama、主程序、音频/设备或清理任务；未提交、未推送。

## 现有污染记录

预防新污染的代码通过后，live 测试库当时仍有 3 条已确认的旧污染记录。已用只读审计生成确认计划：

- `tmp/memory-pollution-delete-plan.json`
- digest：`b2d10198c816bbe8c8862e319f3cfd744c7497a1206d314626696054387792cd`
- 目标 ID：`42b6f68fa0044fbbad26f29abcb5ae44`、`a8f25792de3e49e39205bdc7c12f0e1b`、`4a97c2e8ed194442ac99007bd4415237`

用户已于 2026-09-28 明确授权执行该计划。`diagnose_memory_ids.py --execute --plan tmp/memory-pollution-delete-plan.json` 完成原子删除，报告 `deleted=3`；随后以只读连接复核：live 库 `count=6`，三个目标 ID 的剩余数量为 0。

计划文件保留为审计记录，但其目标记录已不存在，属于已消费/失效计划，不得再次执行。
