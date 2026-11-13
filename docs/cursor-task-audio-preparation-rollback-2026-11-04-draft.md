# 音频准备事务回滚（已放行）

队列前置已Root最终PASS：独立9及最终相关123，具名review顶部为准。

这是有界预取的必要边界：准备函数分配文件后若遇KBI/Exit或二次清理异常，外层拿不到返回路径，不能让下一轮继续分配以掩盖泄漏。范围是现有三个适配器的准备事务，不改已通过的TFM/NativeJob/IO owner。读AGENTS、四repo skills、安全规则，保留巨大dirty，禁git回退/正式data/memory/真实模型设备/下载清理/提交推送。Root设计review，Cursor生产与仓库测试。

## 已确认来源与窄范围

- src/voice/rvc/converter.py:convert_file 创建output后仅except Exception，KBI/Exit从infer或polish抛出时输出未发起删除且路径不返回；普通失败清理异常也可能覆盖原错误或二次进入清理。
- src/voice/tts/engine.py:_edge_tts_to_mp3、_mp3_to_wav、_prepare_playable_audio 都仅捕获Exception；底层新路径分配后遭BaseException外层不知地址；remove_many的清理异常能取代原始prepare错误。
- src/voice/tts/cosyvoice_backend.py:synthesize_owned_wav、prepare_playable 类似，仅普通异常会保留失败输出/退休与回收责任；KBI/Exit可绕过登记。失败client可能还在写，不能立即删/假quiet。
- 不扩大到save_to_file/所有引擎错误处理/声学/模型设置。本切片不接入预取、不改变成功返回与普通转换失败的基础音频回退，不让主程序真实跑起来。

## 设计契约

1. 每个“创建新文件并返回所有权”的窄函数，在成功返回前都有明确owner。异常覆盖BaseException，启动/转换/合成异常原对象仍原样抛出（RVC普通Exception既有returnNone语义保留，KBI/Exit绝不能被转成None）。不要通过BaseException一把吞掉来称成功。
2. 每一层只清理自己新分配或已接收的路径，不删输入音频，不扫共享TFM其它STT/save文件，不直接unlink。统一走现有TFM remove/remove_many；TFM pin/身份/失败pending保留不绕开，禁止新建cleanup线程。**多路径回滚必须逐路径尝试，即使第一个remove抛错，也要请求其它所有已分配路径清理，不能一个remove_many异常就丢掉后续路径意图。** 可用一个小型纯适配helper集中“原异常存在时逐路径请求回滚”，不改TFM实现、不添owner/后台线程/异常历史。异常清理若又抛任意BaseException，原始主异常仍优先；二次清理失败仅安全阶段/类别（或不额外日志），不打印原异常traceback或路径正文。无主异常时的正常清理致命错误不能静默吞成成功。
3. RVC输出失败后即使infer在途，remove只发起意图，pin保护仍有效。转换成功才转交output路径；普通inferFalse/Exception保持None，polish普通失败保持现有返回有效音频，polish KBI/Exit需回滚并原对象上抛。有限一次rollback，不为了二次清理失败死循环。
4. Cosy synth失败/抛任意BaseException（已创建且未返回WAV）都先进入现有failed-output/worker-retire fence；不能把未知仍写入路径简单删除后当quiet。prepare已获base且后续RVC异常，回收base并track仍owned；原异常优先。保留client的IO pin/retire顺序及pending上限，不新增无界retained列表、不关闭用户服务。
5. 若新分配本身抛出、尚未返回路径，函数不能猜测TFM未知路径；TFM.create自身原子性不在此切片改。函数未分配路径就不得删除输入/别人的路径。已成功返回的原所有权不回滚。

## 验证

新增tests/test_audio_preparation_rollback.py，用fake client/infer/cloud/resampler/TFM与TemporaryDirectory；覆盖每个创建后阶段普通/KBI/Exit，原异常对象、正常返回、不删输入与无关owner；二次cleanup RuntimeError/KBI/Exit不能替换主错误；RVC普通回退及polish非致命兼容；Cosy失败必须先标failed并阻新分配，晚写/retire未quiet不强删pin；最后释放gate并真正quiet。只临时目录，不读改真实data/tmp集合、无asyncio真实网络或播放器/模型。

先唯一label scoped gate新模块，再相关tests.test_cosyvoice_production_slice_c1/c2b2a、test_rvc_persistent_io_lease、test_security_round3a/review_fixes、现有音频/自然语音模块（先读选取无副作用）。所有fixture必须主线程捕获BaseException、检查wait、finally释放且实际join确认，不以daemon证明。禁止raw unittest/discover/detach/并发和Root探针修改。静态编译指定文件+diffcheck、短ADR/交接后STOP。Root源码与独立错误矩阵通过才允许下一音频预取实现。
