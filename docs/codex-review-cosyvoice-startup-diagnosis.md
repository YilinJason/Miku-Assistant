# Codex startup诊断评审

状态：三项返修经Codex复查通过，独立23项startup/main测试OK，exit0。允许受控固定文本真实smoke定位卡点；尚未进入C。以下为返修依据。

1. **测试清理回归**：`CosyVoiceStartupProgressTests`四处调用`CosyVoiceClientLifecycleTests()._client_with_fake`，TFM的addCleanup因此注册在另一个从未run的TestCase；当前tearDown只清worker，不清该manager。改为把helper绑定到正在执行的self（无须改生产），或明确_register_tfm_shutdown(self,...)。专项前后精确miku WAV集合不得增长，不删除历史未归属文件。
2. **所有第三方导入保持干净协议**：Cosy导入/AutoModel构造已重定向，但`_apply_local_wetext`仍直接import第三方，未包重定向。将该调用也放stdout→stderr保护内，协议仍用固定sink；fake导入打印验证协议无污染。
3. **时钟接口**：StartupProgressReporter(clock=...)仅初始t0用注入clock，_elapsed_ms却固定time.perf_counter，注入时钟会输出无意义数。保存并持续使用同一clock，补递增fakeclock断言实际elapsed_ms，保持单位/非有限校验。

其余review：事件总数14、immutable snapshot、未知stage/phase/非有限拒绝、取消与原deadline、固定日志字段符合设计；本次不扩大为其它接口重构。完成后只跑startup专项+原相关一次并记录真实RC；Codex独立复验后才真实smoke。默认timeout30、RVC/config/权限均保持。
