# Cursor：可选CosyVoice生产后端 切片A

先读AGENTS.md、architecture-contracts/engineering-quality/collaboration-review技能、安全规则和`docs/architecture/adr-cosyvoice-production-backend.md`。只做切片A；无真实模型/设备/程序启动，无提交推送，无真实data/memory访问。

## 范围

1. 建立`src/voice/cosyvoice/`窄模块。把现有`scripts/cosyvoice_eval_suite.py`中固定资产布局、pin常量、只读pin验证提取到stdlib-only资产模块，旧suite重导出保持全部诊断/测试兼容，不动固定SAMPLE_CASES行为。把diagnose_expressive_tts.py最小环境构建提取为可复用纯helper，旧入口薄代理兼容；不要从生产代码导入diagnose脚本。
2. 严格不可变配置`CosyVoiceSettings`，仅显式engine=cosyvoice才需要：startup_timeout<=60秒默认30、synth_timeout<=45默认20、idle_timeout<=120默认60、max_text_chars<=240默认240、cpu_threads1..4默认4、min_free_ram_mb>=8192默认8192、min_free_vram_mb>=4096默认4096、cuda_memory_limit_mb<=4096默认4096且>=3072。拒绝bool冒充数字、非有限、未知项、任意路径/解释器/模型/自由instruct。纯解析不mkdir/importtorch/读模型/启动线程。后续会复用，当前不接TTSEngine。
3. 在SafetyPolicy增加`Operation.LOCAL_TTS_WORKER`，裸/app/config缺省false，MIKU_DRY_RUN强制false。新TrustedLocalTtsProfile仅构造时封存，固定python、固定runtime worker脚本路径和固定资产（现有pin）。不得下层注册/更改。专用spawn方法只能调用固定argv，不接受用户args/executable/cwd/env/shell覆盖。环境由helper生成、cwd固定仓库/固定vendor路径、stdin/out由能力固定为PIPE/text/unbuffered，stderr=DEVNULL防阻塞泄露。使用现有runner注入支持fake，不能走通用safe_subprocess_popen旁路。
4. 新能力的runtime worker脚本将在B创建，A可以固定路径常量并在缺失脚本时拒绝；测试用临时假文件+patch固定路径验证。封存python/script/model文件身份，并在启动前复验；模型pin读stat/git元数据，不能每轮hash2.3GB或递归扫描vendor。拒绝symlink/junction、同名替换、任意路径、字符串命令和shell=True。
5. 本轮不在Config/YAML开启新许可，不改生产engine/RVC参数。允许在Config默认字典声明`local_tts_worker:false`及注释；不要对默认Edge路径引入模型读取或目录创建。

## 验证

有意义的fake行为测试覆盖：纯解析零副作用、坏值矩阵、裸/缺省/dryrun拒绝、显式许可且封存profile才调用fakePopen；固定argv/env/cwd、秘密env哨兵不传播；修改/替换资产后零Popen；旧expressive诊断完全兼容。不要源码字符串断言，不为测试修改生产判断。

运行本轮专项+现有安全/expressive/RVC artifact相关回归、compileall、diff-check、dry-run；全量到B/C跨模块整合后统一跑，避免每小片重复。交接顶部列出真实退出码和未运行项后停止，等待Codex严格评审。
