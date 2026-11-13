# 语音包按需导出：当前资源板块的小切片

用户要求持续优化资源，Codex设计/review，Cursor实施。读取AGENTS及四个相关skills、Safety规则，先git status，保留全部dirty修改，未提交推送。只做此切片，完成STOP等待Root。不启动模型、设备、主程序、清理，不读取正式data/memory。

## 已确认事实

src.voice及tts/audio/rvc/cosyvoice五个__init__.py都提前导入具体实现。导入轻量worker_resources或TempFileManager也会连带TTSEngine→RVCConverter→NumPy/SciPy。此前tmp观察器通过去掉语音包导入降为约13MB private，但主程序自身仍有assembly直接导入TTSEngine，**此切片不声称已解决整个文本启动开销**。三模型共存仍FAIL；mmap隔离对照Ollama120秒超时，不进入生产。

## 范围与契约

仅五个__init__.py、tests/test_voice_lazy_exports.py及短ADR/交接。保持每个现有__all__名称、对象真实identity、from包import名称、直接子模块import、dir可发现性及类型检查入口。用PEP562模块__getattr__按固定映射importlib.import_module，实现成功后缓存真实对象；未知名称AttributeError；依赖导入失败原异常传播、不缓存占位或失败，不吞KeyboardInterrupt/SystemExit。TYPE_CHECKING保留静态导入。重复/并发访问必须返回同一真实定义；不新增线程/锁框架/自动构造组件。避免泛化注册器造成五个短文件变复杂。无配置/生命周期/声学/默认值变化。不改assembly/Core/engine/Postprocess，也不改已有Root探针。

## 必要验证

新鲜解释器行为测试经SafetyPolicy safe_subprocess_run/Popen固定Python、固定-c代码或固定fixture、timeout和有界输出，禁止裸subprocess。对子模块轻量import如worker_resources、audio.temp_files，meta_path sentinel拒绝numpy/scipy/torch/soundfile及tts.engine、stt、rvc.converter/player等无关实现：原结构应失败，新结构成功；分别独立进程防止sys.modules污染掩盖回归。包import和dir/__all__不触发实现加载。每个导出从包访问identity等于具体模块定义（可在另一个新鲜解释器做真实import，不创建实例/设备；fake mapping测试不能替代真实identity）。未知属性AttributeError；注入导入失败后下一次可重试；并发首次访问同identity。无sys.modules全局清空、无断言字符串匹配代替行为。测试按现有Root scoped gate --modules运行，控制总180秒，WAV基线零差。跑相关生产slice_a/voice_foreground测试一次，compileall/git diff --check。仅新增边界测试，不全量discover。交接区分Cursor验证和Root验收，不声称三模型/真实音频已完成。
