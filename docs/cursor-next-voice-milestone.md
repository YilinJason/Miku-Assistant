# Cursor 下一里程碑：语音会话接入增量回复与播报完成契约

## 背景与目标

上一里程碑已离线验收：CLI 可选增量显示、LLM 分句入队、取消/轮次隔离和显式 `ReplyStage`，全量 120 tests OK。当前 `src/cli/voice_session.py` 仍固定调用非流式 `process_text_input()`；若直接切到现有流式入口，生成结束后语音队列可能仍在播放，语音会话却已经重新打开麦克风，可能把助手自身播报识别成下一轮输入。

本里程碑只完成“轮流说话”模式下的语音流式接入：用户说完并识别后，模型增量生成，首个完整句子可提前进入 TTS；只有本轮播报明确完成后才恢复监听。不要实现后台持续录音、回声消除、唤醒词、随时插话或底层音频硬打断，也不要把这些能力写成已完成。

遵循 `AGENTS.md`，重点读取：

- `skills/architecture-contracts/SKILL.md`
- `skills/safe-refactoring/SKILL.md`
- `skills/engineering-quality/SKILL.md`
- `.cursor/rules/system-safety.mdc`
- `docs/architecture/adr-stream-playback.md`
- `src/cli/voice_session.py`、`src/core/assistant.py`、`src/core/speech_queue.py`

保留当前所有未提交修改，特别是独立的 `src/voice/stt.py` 修改。本轮不提交、不推送、不运行真实麦克风、扬声器、RVC、Ollama 或系统控制。

## 契约要求

### 1. 精确的播报完成状态

为 `TurnSpeechPlayer` 增加按 `turn_id` 等待本轮完成的窄接口。不得用 `queue.empty()` 推断完成，因为句子可能已出队但仍在 `tts.speak()` 中。

至少区分：

- `COMPLETED`：本轮所有已接受句子均已完成 `speak()`。
- `FAILED`：本轮消费者失败。
- `INVALIDATED`：被新轮或取消失效，待播项已丢弃；已在播放的底层音频可能尚未停止。
- `STOPPED`：播放器关闭。
- `TIMEOUT`：有界等待超时，不谎称播放完成。

实现可使用每轮计数 + `Condition` / Event，但必须防止丢通知，处理入队、出队、正在播放、失败、drain、CAS invalidate 与 shutdown 的全部路径。锁不得覆盖阻塞的 `tts.speak()`。等待需响应取消和 shutdown。过期轮完成或失败不能污染当前轮状态。

### 2. 助手的“生成完成”与“播报完成”边界

在兼容现有调用方的前提下，为流式助手入口提供明确的 `wait_for_playback`（或等价的高层契约）：

- 默认保持现有文本/交互模式的非阻塞播报行为。
- 语音会话启用时，在有效 `COMPLETED` 后等待所属播报轮终结，再返回给监听循环。
- 系统/搜索命中时仍只执行一次 handler；在该高层路径启用 TTS 时，也必须进入受管理的单消费者队列，使等待语义成立。不得回退为不可跟踪的每次新建 daemon Thread。
- 无 TTS、空回复、生成失败、取消时不得等待虚构的播报轮。
- 生成成功但播报 `FAILED` / `TIMEOUT` 时，历史成功事实保持不变，返回显式播放阶段状态/提示；不得改成生成失败或重复全文。
- 新输入、取消和 shutdown 仍遵循已验收的 token 所有权与 CAS 规则。

如果需要扩展 `ReplyStage` 或增加独立 `PlaybackStatus` / `playback_detail`，保持现有三参数 `DispatchOutcome(...)` 构造兼容。不要用错误文本或 `allow_tts` 反推阶段。

### 3. VoiceSession 可选流式接入

- `VoiceSession` 增加可选 `stream_reply`；默认值从 `assistant.stream_reply` 配置读取或由调用方显式传入。`--stream` 和交互命令 `stream` 进入语音模式后都应生效。
- 流式开启：及时打印 `Miku: ` 与增量文本，完成时不重复全文；按 `ReplyStage` / 播报状态打印简短提示。调用助手时启用 `wait_for_playback`，等待结束后才输出“继续监听”并进行下一次 STT。
- 流式关闭：保留当前非流式语音行为与入口。
- 停止词、切回文本、独立 voice 与嵌套 voice 行为保持。
- `KeyboardInterrupt` / 取消在等待阶段要退出或返回会话，不留下待播队列；不声称已经硬中断当前底层音频。
- 不要让 `voice_session` 读取 `_speech_player` 等私有字段；完成等待由助手公开窄接口或本次调用契约管理。

## 离线验收

新增 `tests/test_voice_session.py`（或等价文件），全部使用 fake；不得初始化真实音频、网络或系统操作。

必须覆盖：

1. 首个完整句子在模型完成前交给 TTS，但下一次 `process_voice_input` 仅在该轮最后一个 `speak` 返回后发生。用 Event 控制顺序，不用 sleep 猜测。
2. `queue.empty()` 已为真但 `speak` 仍阻塞时，等待不能返回 `COMPLETED`。
3. 消费失败、取消、invalidate、shutdown、timeout 均唤醒等待者并返回对应状态；worker 和测试线程在 `finally` 释放并 join。
4. 系统/搜索优先级及单次执行不变，启用等待时其 TTS 也可追踪；系统操作只用 fake/dry-run。
5. `--stream` 配置、交互开关进入嵌套语音、独立 voice 三条入口正确传递；流式关闭回归非流式调用一次。
6. CLI 输出：成功不重复，生成失败、取消、播放失败/超时各自可区分；以“错误:”开头的正常台词仍为成功。
7. 历史只由模型客户端提交一次；播放失败/超时不撤销或重复提交成功历史。
8. 全量离线测试保持通过。

## 文档与交接

写入 `docs/architecture/adr-voice-stream-turn-taking.md`，说明：

- 当前是顺序轮流说话；生成可与播报流水，但麦克风不会在助手播报中重新监听。
- 不支持回声消除、底层硬打断或真正 barge-in。
- 生成完成和播报完成是两个阶段。

更新 README、`docs/engineering-baseline.md` 和 `docs/cursor-handoff-status.md`，记录实现、实际测试命令/数量、明确未验证项及需要 Codex 评审的并发边界。完成后停止修改并等待独立评审。
