# Cursor 任务：语音自然度扩展状态试听集

> 先读取根 `AGENTS.md`、`.cursor/rules/system-safety.mdc` 与相关仓库 skills。基于当前语音自然度 Round 1 PASS 工作区继续，保留全部既有未提交修改。只完成本任务，完成后停止等待 Codex Review。不得提交/推送，不得接触 `data/memory`，不得运行真实 Edge/RVC/设备。

## 目标

在 `test_rvc_conversion.py` 增加可选的固定扩展试听套件，用生产相同的 EdgeProsody/TTSEngine/SafetyPolicy/RVC 路径生成更多语气状态 A/B。默认行为与现有 `core` 四组完全兼容。

## 接口与固定样本

1. 增加 `--suite {core,extended}`，默认 `core`。
2. `core` 保持现有四组、现有输出目录和文件名不变。
3. `extended` 只包含以下十个固定、非敏感样本，顺序与 label 固定：

   ```python
   (
       ("warm_greeting", "早上好，今天也一起慢慢来。"),
       ("caring_question", "你听起来有点累，需要先休息一下吗？"),
       ("curious_question", "后来发生了什么，你愿意告诉我吗？"),
       ("pleasant_surprise", "你真的做到了？！"),
       ("happy_excitement", "真的成功了！太棒了！"),
       ("encouragement", "没关系！我们再试一次就好！"),
       ("soothing", "不用着急……我会一直陪着你……"),
       ("hesitant_shy", "嗯……其实，我也很期待……"),
       ("serious_calm", "这一步需要谨慎处理，我们先确认清楚。"),
       ("good_night", "晚安，做个好梦。"),
   )
   ```

4. `extended --execute` 输出到 `data/test/voice-naturalness/extended/`，每组仍为 `NN_label_base.mp3` 与 `NN_label_rvc.wav`。不得覆盖或清理 core 文件。
5. dry-run 输出当前 suite、计划目录、固定样本及生产 helper 解析出的 rate/pitch/volume；仍然零网络、零 RVC、零目录/文件创建。

## 契约

- 复用现有 `build_execute_safety_policy()`、`TTSEngine`、fail-closed purge、非空检查与 finally shutdown；禁止复制第二套真实执行路径。
- 样本数量不能硬编码为 4；进度、成功判定和 fake 测试均以所选 suite 的长度为准。
- suite 只接受 argparse choices；不接受用户任意文本、label 或输出路径。
- 不新增生产请求、线程或情绪推断；这是显式人工诊断入口，不改主程序运行行为。
- 真实日志不打印外部响应或用户对话；仅打印这些代码内固定样本。
- 继续保证旧文件删除失败时在 Edge 网络调用前 fail closed；部分失败不复用旧产物，前序完整样本可保留。

## 离线测试

扩充 `tests/test_voice_naturalness_round1.py`：

- 默认/显式 core 仍为四组和原目录；extended 精确为十组、独立子目录、label/文本顺序正确。
- extended dry-run 零 health/profile/TTS/mkdir，显示十组的有效 profile；至少断言 mixed `？！` 走 question 优先级、安慰/犹豫走 ellipsis、认真/晚安走 neutral。
- fake extended execute 生成十组成对文件、调用次数精确、shutdown 一次、返回 0。
- extended 中任一失败仍返回非零并遵守现有清理契约。
- 所有现有 core、安全和 save_to_file 测试继续通过。

## 验证与交接

运行专项、既有相关回归、全量 unittest、`compileall`、`git diff --check`，以及 `core`/`extended` 两个 dry-run。更新 `docs/architecture/adr-edge-sentence-prosody.md` 与 `docs/cursor-handoff-status.md` 顶部。明确真实 `--execute --suite extended` 留给 Codex 复验后运行。完成后停止等待 Review。
