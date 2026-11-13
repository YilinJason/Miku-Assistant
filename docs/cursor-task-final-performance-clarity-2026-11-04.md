# 最终性能里程碑：窄幅保行为整理（已放行）

Root已验收TFM deadline与模型内存准入；只进行以下明确的可读性整理，不扩展新功能。实施仍由官方Cursor，Root随后严格复审，不自报最终Accepted。

先读AGENTS、skills/safe-refactoring/SKILL.md、skills/architecture-contracts/SKILL.md、skills/engineering-quality/SKILL.md、skills/collaboration-review/SKILL.md与存在的.cursor/rules/system-safety.mdc；检查dirty并保留所有已有修改。正式data/memory禁止读写删；不启动程序/设备/模型/清理，不改默认配置、音色、权限、任何tests或tmp。

允许修改仅四文件：

1. src/core/model_memory_admission.py：移除冗余_require_active字段/slot。_require_lock已是覆盖整个require的非阻塞单飞门，_begin_require获取失败返回False；获取成功但closing时释放后返回False；否则返回True并由既有finally调用_end_require释放。保留_close独立_lock与owner发布/取消/实际join、异常传播和deadline行为。不要引新的flag/helper/framework，不改schema/估算/任何HTTP逻辑。
2. src/memory/ollama_extractor.py：只删除新准入try末尾无实际作用的except BaseException: raise，让fatal自然原样传播；原ModelMemoryAdmissionError映射保留。
3. src/core/local_voice_warmup.py：仅更正模块docstring，“不接装配/CLI”已过时；实际由voice_load_lane→voice_activation调用。不要改其逻辑或公共接口。
4. src/core/assistant.py：仅更正shutdown docstring里“只重试memory”的过时描述：已完成步骤不重复，任何未实际完成的关闭步骤可重试；不得改shutdown实现。

不修改ADR Accepted状态、Root review、handoff状态或其它文件。可以只在返回消息列出改动，Root负责交接记录。禁止git checkout/reset/restore/clean/提交推送。无测试/gate/probe/监督器运行权限；只对这四文件做AST/局部编译与git diff --check（不把既有脏工作区重置）。完成后STOP，返回精确文件/改动/检查结果，等待Root。
