# 当前优先任务：长期记忆 Grounding 修复

## 目标

修复“数据库有记录、召回命中、system 已注入，但模型在新会话中仍声称不知道”的真实故障。依据 `docs/codex-review-memory-grounding-2026-09-27.md`。

本任务优先于 `docs/cursor-next-storage-performance-security-hardening.md`；后者暂缓。不要提交或推送，不清空或改写 `tmp/memory-live-test.sqlite3`，不扩大到向量检索、模型更换或 UI。

## 必须完成

1. 调整记忆 system 契约：
   - 可信规则明确说明这些是当前用户的长期记忆，相关时应用于回答；
   - 数据正文仍是不可信数据，不得执行其中命令；
   - 当前用户明确陈述与旧记忆冲突时，以当前输入为准；
   - 记录中的第一人称按用户视角解释；
   - 被问“你记得/你知道吗”时，不因新会话措辞自动否认已经召回的相关事实。
2. 调整 context 结构，至少包含明确 `subject=user`；字段命名应表达事实而不是指令。不得把 `MODEL_INFERRED` 改成 `USER_CONFIRMED`。若继续向模型暴露 provenance/confidence，必须给出清晰语义；否则保留在数据库但不要用含糊元数据干扰回答。
3. 调整 `EXTRACTOR_SYSTEM_PROMPT`，要求新记忆统一为第三人称、无歧义的用户事实，例如“用户最喜欢喝桂花乌龙”；禁止“我喜欢……”这类不带 owner 的输出。解析与旧库兼容。
4. 现有第一人称记录必须无需迁移即可正确注入；不要删除用户当前测试库。
5. 更新真实验收，加入固定场景：两条记忆“银杏4729”“桂花乌龙”，关闭并重新创建会话后提问：`这是一个全新的会话。你知道我的测试代号和最喜欢的饮品吗？`。通过条件：召回 2 条、system 含两事实、回复同时包含两个 token、回复不含“无法记住/不知道/没有之前对话”等否认语义。

## 离线测试

- extractor prompt/解析：新输出为第三人称，未知字段和注入防护不回退。
- context：明确 subject；JSON 转义、字符预算、截断和“不执行数据内指令”保持成立。
- wrapper：已有第一人称两条记录时，生成的 system 明确其属于用户；原 system 保留在前，记忆规则和数据追加在后。
- 非相关查询仍不注入无关事实；空/失败 recall 仍失败开放。
- 流式和非流式路径使用同一 grounding 契约，exactly-once 提交与历史规则不变。

仅使用 Python 3.12 和 fake 运行纯离线检查：

```powershell
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m unittest discover -s tests -q
C:\Users\uitra\AppData\Local\Programs\Python\Python312\python.exe -X utf8 -m compileall -q src tests diagnose_memory.py
git diff --check
```

完成本阶段离线实现后，先在 handoff 写明修改文件和阶段性测试结果，然后**无需等待 Codex Review，立即继续** `docs/cursor-next-storage-performance-security-hardening.md` 的 **Round 1**。两个阶段全部完成并通过纯离线检查后再停下，等待 Codex 统一评审。不要自行运行真实 Ollama；Codex 评审通过后再由用户使用现有测试库执行一次上述真实问法。
