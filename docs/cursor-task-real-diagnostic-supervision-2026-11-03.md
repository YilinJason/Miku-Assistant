# 真实诊断监督器可靠性：先于追加模型负载

参见 docs/codex-review-coexist-control-2026-11-03.md。程序前台切片已PASS；本任务只收口开发工具，**不改 src/、生产预算/声学/默认配置**，不运行真实模型/main/设备/正式memory，不提交推送。所有外部进程仍经 SafetyPolicy。读取 AGENTS、工程质量/协作技能。

范围：tmp/codex_app_real_voice_gate_2026_11_03.py、tmp/codex_app_real_voice_smoke_2026_11_03.py 及必要窄helper/fake测试；保留旧诊断报告。把 gate 包进 import-safe main，让 fake 可验证实际监督逻辑，避免以 source 字符串断言代替。

## 必须结果

1. 总 deadline 从执行开始计算，包含子进程启动与采样；退出额外预算固定且报告。运行循环**不再同步跑 GPU 外部查询**。初始空闲GPU/RAM预检保留；之后仅轻量 RAM/CPU/进程元数据，GPU标 unavailable/未采样，不用旧值假装当前。缺失信息诚实标注，RAM低阈值6GB已实现仍保留。不要新增采样线程或绕过 SafetyPolicy。
2. print+flush并及时保存自有PID与阶段/时间/中断原因，不等所有结束才写报告。日志文件上限/元数据样本数有界。所有 BaseException（含KBI/SystemExit）、失败启动与timeout路径统一 finally 回收自有 child/server/文件句柄；保留原异常并真实报告 kill/quiet 失败，不吞后标complete。不能无限communicate/wait。
3. 优先复用已通过 src/core/owned_worker_job.spawn_owned_voice_worker，不改其核心：固定Python诊断child早期join Job，parent退出能回收真实后代。临时Ollama若需要Python小launcher，先earlyjoin，再通过SafePolicy启动固定安装的ollama.exe serve（受控env127.0.0.1:11445），后代继承Job；禁止任意命令/路径/env、shell或用户服务kill。只操作所持有对象/句柄，Job quiet未确认不伪成功。父退出兜底与正常kill/quiet都记录。不要为方便丢原root Popen句柄或复制Native实现。
4. 新 --label 校验/唯一报告保留，禁止覆盖已有成功/失败证据；路径均在tmp，默认端口已占用直接跳过。支持下一轮 --no-ollama（只实际RVC+Cosy、无Ollama协调）及 --prepare-only 到固定smoke；但本任务仅fake验证，不运行真实。

## 验证

可注入 monotonic、sleep、spawn/kill/quiet 及metadata sampler，fake精确验证成功/timeout/低RAM/异常/KeyboardInterrupt/partial启动失败全路径，样本<=36、GPU外部查询在运行循环为零、资源未quiet不假成功、report及时产生。采样异常不吞截止时间；真实不可中断kernel调用不能承诺绝对硬实时，明确外部限制。

可单独无模型Windows Native Job dummy子孙进程验证正常退出/deadline终止/父退出，10秒级监督；固定子脚本、SafePolicy、所有自有PID确认结束，不碰用户Ollama服务。不用真实测试来验证工具本身。用标准库 unittest，一次有界 gate，失败STOP。更新handoff+简短工具契约，交付后STOP待Root独立评审。
