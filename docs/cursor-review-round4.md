# 第四轮最终评审：仍有两项阻塞验收

独立全量最近结果：115 tests / 2.104s / OK。评审保留现有工作，不提交推送，不启动真实设备，STT 独立修改排除。

## P1：CAS 失败后退回无条件 invalidate，重新引入误清新轮

assistant.py `_begin_generation_turn` 在释放 `_turn_lock` 后清理 player；`compare_and_invalidate(prev_speech_id)` 返回 False 时调用 `player.invalidate()`。若输入 B 切换 token 后暂停，输入 C 切换并建立自己的播报轮，B 恢复就会 CAS 失败并无条件清掉 C。这抵消了 CAS 本身的保护。没有 prev_speech_id 的分支也无条件 invalidate。

此外流式入口 `_turn_is_active` 检查后 `_ensure_speech_player` / `player.begin_turn()` 不在统一归属临界区，旧轮暂停后恢复也可清掉新轮。请将 token 切换、player 初始化和播报轮归属分配设计为一致的短临界区；失去所有权后不得调用无条件 invalidate/begin_turn。锁不能覆盖 HTTP、speak 或队列等待。使用 Event 控制 B/C 入口交错，而非仅单独测试 player.compare_and_invalidate，证明 C 待播项与身份不变、仅一个消费者。

## P2：播放失败被 CLI 标为生成失败，重复输出全文

已用 fake 调用 `_print_reply_streaming` 复现：on_delta('已经生成的完整回答。') 后返回 DispatchOutcome(全文,False,False)，输出全文一次后又输出 `[生成失败] 全文`。

原因是 CLI 用 `not outcome.allow_tts` 推断生成失败，而最新 assistant 在 COMPLETED 后播放失败正返回这组字段。请为展示提供显式的生成/取消/播放阶段状态或独立提示，保留已有构造兼容；不要用 TTS 许可或文本前缀猜测生成状态。已成功生成/提交的全文只能显示一次，播放失败输出简短的独立提示；生成失败仍区分于正常角色台词。涵盖真实客户端+fake HTTP 的完成后尾句入队失败、CLI 展示、历史确实提交一次及普通失败/空回复/错误前缀成功台词回归。

完成这两项后全量离线测试，更新 ADR、基线与交接状态。不要增加其他功能。停止编辑等待 Codex 复核。
