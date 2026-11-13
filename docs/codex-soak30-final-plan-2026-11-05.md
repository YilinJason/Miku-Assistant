# 最终30分钟真实App流式链路验收计划

前置：mmap与首段优化已独立评审和短测；协作停止已Root11/12/8/24门禁及真实取消通过。cooperative_cancel_nov5g：129.27秒、子退出130、父取消124，cooperative_stop_child_quiet与App/Timer/TFM/warmup/guard全部True，回收0.14秒、无cleanup_errors；WAV828项name/dev/ino/size/mtime完全零差。取消是有意测试，不计30分钟成功。

本次label固定soak30_full_nov5h，现有real gate --isolated-ollama --soak-30；不跳资源准入、不改模型、音色、生产超时、权限或并行上限。首轮请求沿生产原timeout，稳态诊断15秒；全程固定无隐私合成输入，真实App流式派发与消费者→Cosy→RVC→capture，不含STT/声卡播放或主观听感，不访问正式memory。

验收标准（开始前记录）：

- steady实测>=1800秒，成功turn>=60、<=120，cancelled=False、complete=True，无普通失败行；每个已完成turn音频verified增量>0且App stage OK、安全恢复监听。
- 父监督exit0，无timed_out/资源abort、child/server/cleanup实际True；连续采样覆盖全程（正常每5秒，记录最长间隔；>15秒需解释），不把部分不可观测的进程总量当完整测量。
- App/Timer/speech consumer/aggregate TTS/RVC/TFM/warmup/guard实际quiet；文件基线828精确元数据零差、只新增本次独占目录最多6个留存WAV，不触原未知两个文件。
- 比较预热后早/晚窗口RSS/private、句柄与线程；持续单向增长须调查。预先以private差额超过max(256MiB,早窗口5%)、句柄差>32或线程差>8作为需复查信号，同时展示曲线区间/平台期，不能低于阈值便保证无泄漏。子树不包含兄弟Ollama服务，父样本含全树，分开报告。
- 退出后独立只读RAM/commit/GPU/内核池快照与自有PID状态；后台桌面未冻结，不将全机小幅变化强归因于App。历史约60GB占用未归属，本窗口不能证明永久无泄漏。

前置快照tmp/soak30_full_nov5h.preflight-metadata.json，WAV基线同prefix.wav-baseline.json；最终父报告tmp/codex_real_soak30_full_nov5h.json，child报告由其stages末尾提供。失败/取消报告全部保留，禁止覆写label。Root每分钟监控；Codex primary<=10%时写唯一自有.stop并确认回收后暂停，不自动reset。

## 再次正式计时前的修订

full_nov5h在504.44秒第32轮失败：chars1的Cosy请求False、stage_playback_failed，真实文件32段已验证，31轮完成；所有退出完整、WAV828项精确零差。原记录保留，不能算PASS。trace_nov5k为元数据诊断，646.42秒/41完成轮后由Root主动停止以处理已证实的规划缺口；parent124/child130/cooperative quiet全部True，WAV828精确零差，不算30分钟PASS。

已独立证明Cosy计划会把纯句号送入合成，vendor normalizer却滤掉纯P/S片段。Cursor最小生产修复只在原上限校验后丢弃P/S/空白片段，保留字母/数字/其他类别与实义文本标点；不改显示或通用分句，真合成错误仍传播。Root122相关契约通过，元数据观察仅诊断且不包含文本。不能据此倒推h中一字必然是标点。

下一次正式label soak30_punctuation_nov5o；其余上述事先验收标准完全不变，不缩短1800秒、不降低成功轮数、不放宽资源/关闭/失败判定。以新运行前全src Python及config/config.yaml、相关诊断文件的哈希为冻结依据。任何失败另行分析，不以重测抹除历史。
