# 非流播报 Root review：最终 PASS

2026-11-04 Root最终验收：逐行生产差异及调用方复审通过。独立`legacy_playback_root5_final_20261104c`5/5（burst300、迟返、bootstrap普通/四类fatal、实际关闭重试）；`legacy_playback_root_related_20261104c`162/162；最后旧structured测试迁移后`legacy_playback_root_final_tests_20261104d`74/74（与162有重叠）。各报告WAV826零差，无超时。所有官方CLI已STOP。下一才放行RVC CPU预算；真实模型/音频未运行。

## Root复审b：最后窄修（Cursor 返修 c）

**生产**：`except BaseException` 在 CAS 释放后改为 `if not isinstance(exc, Exception): raise`；仅普通 `Exception` 记录「非流播报失败」并继续角色转发（不再只放行 KBI/SystemExit，避免吞 `GeneratorExit`/自定义 `BaseException`）。

**测试**：`_shutdown_assistant_with_player` 去掉无结果的 `wait_for_turn`；`_require_player_quiet` 增加 `player._owner.snapshot().quiet`；新增 `GeneratorExit`/自定义 `BaseException` bootstrap 传播用例；`test_public_speak_and_tracked_empty` finally 实际 quiet + shutdown。

**Cursor gate**：`legacy_playback_bound_cursor_20261104c` — **162 OK**，WAV826 零差 → `tmp/codex_gate_legacy_playback_bound_cursor_20261104c_2026_11_02.json`。Root 独立 probe 已扩 5 项/致命 4 类型，**尚未复跑**。

## 历史：Root复审b 初稿（已通过 Cursor 返修 b，窄修见上）

except BaseException后仅判断KeyboardInterrupt/SystemExit会吞GeneratorExit、asyncio.CancelledError及自定义BaseException，违反“BaseException不吞”契约。应判断`if not isinstance(exc, Exception): raise`，仍先CAS释放；普通Exception才记录跳过。补GeneratorExit/自定义BaseException原对象传播测试。Root独立probe已扩5项/致命4类型，尚未复跑。

fixture `_shutdown_assistant_with_player` 内 wait_for_turn 返回被忽略：teardown既然随后shutdown+actualquiet，可删除这段无必要等待（业务各测试自己的状态断言保留）；或验证明确允许状态，不能无条件忽略。`_require_player_quiet`再复核owner.snapshot().quiet。natural_companion同文件原test_public_speak_and_tracked_empty若仍touch则补finally实际quiet，不能只结尾不检查shutdown。除此不扩大源逻辑。修后STOP等Root。

2026-11-04：**返修 b** — claim→enqueue→mark 同一 `BaseException` 责任块；enqueue bootstrap `RuntimeError` 受控跳过且角色转发继续；fixture 统一 `_shutdown_assistant_with_player` / `addCleanup`；真实 `OwnedThreadTask.start` patch 覆盖普通/致命异常。Cursor gate `legacy_playback_bound_cursor_20261104b` **160 OK**，WAV826 零差。

## 历史（初稿未通过）

Root4 独立探针3PASS/1ERROR；报告 `tmp/codex_gate_legacy_playback_root4_20261104a_2026_11_02.json`。burst/迟返/shutdown 已通过；失败点为 enqueue bootstrap 漏捕获与 fixture 清理不可靠。

## 必修（初稿，已在返修 b 处理）

1. assistant.py `_apply_outcome_side_effects` try仅包claim，实际OwnedThreadTask.start发生在player.enqueue中。Root patch start抛RuntimeError，process_text_input向外报错且角色转发中断；与任务及ADR声称bootstrap受控跳过不符。把claim→enqueue→production_finished放在一个明确责任块；普通异常无文本日志、CAS回收本轮后继续文本/角色；致命BaseException同一对象传播，但先CAS释放已claim轮，不能吞或syncfallback。失败/成功/exception都不能遗留未终结轮，不能影响新轮。尽量扁平小分支减少当前嵌套。
2. tests/test_assistant_orchestration.py ProcessTextInputTests新启动真实player但没shutdown，_wait_player_turn只证明turn终结并不证明消费者退出。所有新受影响测试用finally/addCleanup实际shutdown与owner.quiet，作用域内清理，不只是结尾成功路径调用。_make/_assistant集中注册可行，但blocked gate必须先release；不要一律对所有未相关测试改大范围。
3. 新LegacyBoundPlaybackTests的gated wait有未检查返回、test_burst helper线程lambda吞BaseException且无finally释放/实际join，失败时后台残留；test_latest/shutdown等只有成功末尾清理。可不新起first request线程（API本来应异步）；若保留，capture全部BaseException并主线程assert。所有gates wait结果校验，finally release→实际join player确认quiet→app.shutdown确认相应完成，不能用daemon/退出进程视作回收。natural_companion新改相关fixture同样审计finally和shutdown结果。

补能真正触发enqueue bootstrap失败的行为测试，不以claim mock失败替代；runtime吞后文本/角色保持，KBI/SystemExit同对象传播且本轮已回收。所有tests仍受监督顺序/无设备，不改Rootprobe/监督器、player/TFM或其它性能项目。修后相关gate+compile/diffcheck+ADR/handoff STOP，Root4及相关独立复验再验收。
