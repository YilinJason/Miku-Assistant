# 两模型受控对照路由（小切片，仅tmp，先fake）

监督器已Root分层PASS，见 docs/codex-review-real-diagnostic-supervision-2026-11-03.md。现在只修诊断路由，不改生产、真实模型预算、声学/Safety；不运行真实模型/用户服务/main/设备/正式memory/提交。保留dirty工作区。

确定错误：smoke的--no-ollama只是不给llm赋值，随后仍走App.ensure→prepare_local_voice→_wait_fresh_ollama(None)，会必然失败于Ollama阶段。不能拿这个结果判断语音模型能否共存。

为控制变量，--no-ollama 路径必须**绕开三模型App协调器**，直接严格串行实际 engine.prepare_rvc()→engine.prepare_local_tts()；不得构造FakeLlm来伪称真实App全链通过。不构造/连接OllamaClient。失败立即跳过后续阶段，finally仍使用完整六字段shutdown aggregate+TFM现有回收；不改src协调器允许None通过。

可在现有tmp/codex_real_voice_supervision.py加一个纯回调、可注入clock/emit的很小prepare_two_model_control函数，返回ok/stage/stage_elapsed_sec等固定metadata；引擎接口不复制模型逻辑。不要新建庞大状态机或为20行helper再建框架。smoke中先if no_ollama，再elif coordinated；--prepare-only 不合成不转换不播放（现有RVC.prepare内部固定无播放warmup保留）。结果路径/报告明确two-model-control、no_ollama=True，不冒称生产三模型准备成功。保留现有三模型真实路径。

fake验证实际helper调用顺序、rvcFalse不调用Cosy、任一prepare抛错仍不走更后阶段、时钟报告阶段耗时。最小验证路由实际选对分支（允许将路由决定抽成纯函数并给smoke调用，不用源字符串检查代替行为）；assert no-ollama下无Ollama构造/请求。Root6和既有tmp16保持。交付handoff后STOP；Root源码验收后才由Root运行一次短真实two-model --prepare-only对照。
