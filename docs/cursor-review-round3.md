# 第三轮评审：取消后不得继续推进生成器

上一轮旧 speak 故障隔离已修。仍有一个必须闭合的历史提交缺陷：assistant.py 的 `for event in stream` 先调用 next，再检查 token。若 on_delta 置 cancel，且 delta 没有完整句子（或 use_tts=False），循环仍会 next；真实 Ollama 生成器可在 next 内执行 _complete/commit_turn，然后助手才发现取消。结果是返回取消但历史已经写入。

请用真实 OllamaClient + fake HTTP 流复现：预置成功历史；单行含 message.content='半句'、done=True；on_delta 设置取消 Event。断言取消返回、完整历史快照不变、响应关闭。另测两行 delta 后 done 与旧轮被新轮替代。不要使用不写历史的 FakeStreamLLM 掩盖问题。

改为在每次主动 next 之前检查当前 token/取消/关闭及消费者故障；回调后同样检查，终结事件后立即结束并关闭。生成成功已提交后发生的播放失败应作为播放阶段失败说明，不能倒退声称生成历史没有提交。阻塞网络读取期间取消无法立即打断仍可作为明确限制，但已观察到取消后不应再主动拉取。

另修复所有权检查与清理的 TOCTOU：_release_own_speech 目前先读 player.turn_id 再 begin_turn，中间新轮可进入，导致旧轮清掉新轮。给 player 增加锁内 compare-and-invalidate(expected_turn_id)；助手新轮 token 切换与播报轮次归属的更新也应一致同步，避免旧入口迟到 invalidate 新轮。用 Event/barrier 控制交错验证，锁不应覆盖阻塞 speak/HTTP。

仅修上述已承诺取消/历史/轮次边界，补回归并全量离线测试，更新交接文档后等待评审。不提交推送、不启动真实设备，保留 STT。
