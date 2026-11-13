# Cursor：CosyVoice生产后端 切片B

先读AGENTS、相关skills、安全规则、`docs/architecture/adr-cosyvoice-production-backend.md`、A模块与A评审。仅runtime worker/client、协议与资源准入；不接TTSEngine/装配/生产YAML，不跑真实模型设备，不碰data/memory，不提交推送。

## 接口与协议

- `CosyVoiceWorkerClient(settings, safety_policy, temp_file_manager)`构造无副作用；`synthesize(text, *, style_id, output_path, cancel_event)`；`interrupt()`；`shutdown()`返回complete/worker_alive/reader_alive状态，失败保留进程可重试。
- 单请求长度<=settings.max_text_chars，固定style表对应已验证六种instruct，禁止自由instruct。output只能是该TempFileManager预创建且身份未变、固定data/tmp内普通WAV；持久文件或任意路径拒绝。
- 版本化JSON-line、request_id/epoch、固定错误标签，禁止正文/路径/异常原文回显。单行长度/队列容量有硬上限，错误schema/id拒绝。中文UTF8。
- 单client最多一个活跃请求，忙则有界拒绝，无内部无限排队。同进程多实例及跨进程不得加载第二套模型。

## Worker

1. 固定`scripts/cosyvoice_runtime_worker.py`经A专用能力启动；扩展LocalTtsWorkerLaunch为parent_pid+严格校验settings，固定argv；所有bool/NaN/未知flag拒绝，旧A测试同步。
2. 先获取固定跨进程模型锁（Windows msvcrt；忙则零importtorch/零模型），再查RAM、导入torch查CUDA/mem_get_info。RAM>=min_free_ram_mb、VRAM>=min_free_vram_mb；不足/不可观测/无CUDA返回固定拒绝并退出。禁止下载/自动CPU回退。CPU threads<=4、interop<=1、线程env OMP/MKL/OpenBLAS/NUMEXPR<=4；CUDA fraction限制到cuda_memory_limit_mb，不静默提高。
3. 模型加载前固定资产pin与路径检查，本地WeText定向，无WebUI/端口/播放/麦克风。一次加载AutoModel，第三方stdout重定向，JSON通道干净。仅运行时创建固定缓存目录，先检查无reparse。
4. idle_timeout后完整退出释放模型/锁，下次显式新请求可懒启动。parent-watchdog父消失后退出。stdin有界读取与主循环分离以支持idle退出；不能每请求创建永不退出reader线程。
5. 父每个worker仅一个stdout reader，队列有界，线程终止可观察。输出由父create，child逐级验证固定tmp、dev/ino身份，直接写同一文件不能os.replace。音频有限、<=30秒/<=4MiB，chunks累计有界；不接受任意输出路径。失败父负责所有权回收。

## 取消与关闭

- startup/synth独立deadline，短轮询cancel/shutdown/epoch；冷启动算活跃可取消。超时/取消/坏协议有界terminate→kill，确认退出才关流清引用；停止失败保留句柄且禁止重启。
- interrupt推进epoch、丢弃晚到结果；空闲worker保留。shutdown永久closing，后续请求拒绝，shutdown与启动竞态不能在关闭后publish新worker。
- stop不得无界等待I/O锁或阻塞写管道；无自动重试/fallback。daemon不代表清理完成，reader退出有证据。
- B不得启动RVC/Edge/Ollama或fallback。备注解释deadline、所有权，协议/准入/loop/client可分窄模块，避免巨型脚本。

## 测试

fake模型/进程覆盖：单实例与单请求、schema/id/非有限、中文、未知style/长度/非owned/链接/替换输出、部分写失败、低RAM/低VRAM/pin失败零模型、idle释放锁、父退出、冷启动/请求取消、超时/stop失败保留且禁止重建、shutdown启动竞态/晚到结果、reader数量退出与队列上限。禁止源码字符串断言或Mock生产特例。

专项+相关+A/expressive/security/RVC生命周期；B涉及跨模块生命周期，全量一次；compile/diff/dryrun。更新交接后停下待Codex严格复审；Cursor禁止真实execute。
