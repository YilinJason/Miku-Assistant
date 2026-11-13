# 恢复后：先诊断真实startup超时（B，非C）

前置证据：B离线独立859项通过；旧锁持锁迁移修复且独立11项锁测试通过。真实固定文本cold启动30.094秒未ready，客户端终止/回收完整。旧eval路径6/6不代表新runtime成功。详见delivery-follow-through与tmp/codex_cosy_runtime_smoke.json。

目标：获得真实启动卡点证据后才修根因，不能直接延长默认30秒或移除资源上限。不改默认音色/config，不读真实data/memory，不改vendor/model pin，不下载/联网，不提交推送。

建议小切片：为现有JSON协议加严格startup_progress事件，仅固定stage枚举和有限非负elapsed_ms。阶段至少覆盖layout、资源准入、pin、cache、WeText导入、CosyVoice第三方导入、AutoModel构造。每个阶段最多begin/end各一次，总事件数有硬上限，不能落文本/路径/异常原文。父startup等待接受并保留有界阶段快照，不能因此重置或延长deadline；ready前取消/close/坏协议行为不变。日志只固定stage+毫秒。

注意：第三方导入也须stdout重定向，不能只包AutoModel构造。协议sink不能随sys.stdout重定向而丢失；可在重定向上下文之外发固定阶段事件，或保留专用协议流。不要打开任意stderr文件、暴露caller Popen kwargs，或绕过SafetyPolicy直启worker。默认生产stderr仍收敛。

离线fake测试：阶段schema/stage/非有限/过多事件拒绝；progress不延长startup timeout；progress期间cancel与shutdown回收；第三方导入/构造打印不会污染协议；快照长度有界。Cursor仅实现并离线测试，完成后停下由Codex审查。审查通过后Codex才执行无播放固定文本smoke，按实际stage定位导入、ONNX/模型构造或其它真实原因，再把针对性修复交给Cursor。不要在没有证据时猜根因或切C。

恢复前先查Codex账户额度，相关窗口剩余<=10%时按用户要求保持停止，刷新后继续。无当前活跃Cursor或模型worker。
