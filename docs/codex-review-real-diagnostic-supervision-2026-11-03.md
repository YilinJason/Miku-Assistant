# 开发监督器：Root 分层 PASS（仅监督与回收控制流）

最终Root16项0.123秒OK，前一层Root6项0.126秒OK（partial owner/异常poll不挡server/坏metadata/未知quiet/共享10秒cleanup）；之前Root两层Native Job+实际第三代进程0.578秒已证完整终止。每次WAV826零差。默认GPU查询必须显式注入SafePolicy代理，运行循环无GPU外部进程；重复不安全CLI已删。未改变生产src/预算/声学，无新真实模型测试。fake套件已移tmp，不再把ignored工具依赖引入正式tests。

PASS范围不包括不可中断OS/kernel延迟的绝对硬实时保证；日志cap是按轮询检测，不能声称单次突发写绝不超过阈值。真实共存仍FAIL。**下一真实对照之前另修smoke --no-ollama路由**：当前llm=None仍调用强制Ollama成功的协调器，会在Ollama阶段失败，不能测到RVC+Cosy；此为诊断路由问题，不更改已通过监督器协议。下方历史保留。

## 最后一处入口一致性：删除重复不安全 CLI

主 gate 已配置安全 GPU sampler，但 helper 尾部还保留第二套 run_real_voice_gate/main/__main__，它未配置安全代理，默认 _sample 调旧 metadata sampler 的裸subprocess.run。第二入口的错误tail也read_text整份日志后截尾，绕开本次有界读取。无需再造配置层：删除未使用的重复CLI入口，helper仅import-safe库；默认 _sample 若include_gpu=True，必须显式提供安全sampler或本地实现SafePolicy固定查询，不能偷偷用旧裸sampler。主 gate 传入已配置好的 sampler.tree_sample，运行循环仍 include_gpu=False。无外部依赖、保留既有 fake注入契约、减少重复代码。

Root已独立完成真正两层Native Job+第三代实际OS子进程的终止证明：0.578秒，outer/inner/leaf三PID等待句柄全部已信号、JobquietTrue，WAV826零差。probe tmp/codex_nested_job_root_2026_11_03.py / 日志prefix tmp/codex_nested_job_root_467bcec30e。不需再重复Native测试；本条只需source+已有fake gate验证，不运行真实模型。解决后STOP。

## 统一返修后独立验证：仍一处独立清理链断裂

Root15项fake0.795秒OK；Root扩为6项，partial owner/共享budget等5过，poll故障一项失败。`_reclaim_owned` 在 `remaining>0 and owner.poll()` 等位置仍有不受保护的query，导致 `_run_shared_cleanup` 中断，server不kill，文件不close，最终报告未写。Root BrokenPoll fake明确server.kills=0，真实临时日志因句柄未close触发WinError32；无真实进程spawn，原始数据未碰。Root已让自己的fixture finally额外close，避免测试清理错误遮蔽原断言；生产监督器仍应自行完成所有步骤。

必须确保每个owner的poll异常记录unknown，仍对持有owner尝试非阻塞kill；child/server/pending每一项独立try/finally，文件关闭及报告即使前项失败也继续。保留原始错误。不得因为query异常跳过kill；尽量不要到处复制大段except，可用窄safe-query/cleanup-step。

一并收口：所有capture日志（含server两份）纳入总cap；结果对象非0时summary不得只给exit_code0（可单列child_exit_code保留rawcode）。这次临时工具的fake测试当前放tests/依赖被忽略tmp，会让干净checkout普通测试导入失败：把新建该临时测试移到tmp/，用Root supervisor固定probe白名单执行；不要把ignored tmp依赖引入正式tests入口。只移动本轮新文件，不删旧用户测试。

最后跑Root6项+原15项（临时测试位置可变但断言不弱化），必要一次model-free Native；STOP待Root。仍不允许真实模型/用户服务。

范围仅开发tmp工具，不改生产。Root独立3项 `tmp/codex_real_supervisor_root_boundaries.py` 确定2fail+1error（0.281秒、WAV826零差）。Root先补齐临时仓库tmp目录后才得到此有效结果，前一次目录未模拟到位的3error不算产品证据。

请统一关闭下面完整清单后交付，避免逐点小补丁；不运行真实模型。

1. **P1 kill fallback被异常跳过**：`_kill_child_tree` / `_stop_owned_server` 先taskkill，若PermissionError/TimeoutExpired则外层except吞掉，原owned.kill永远不执行。Root deny taskkill明确kills=0、child_quiet=False。先对所持NativeJob kill（不靠新PID句柄），每个清理步骤独立try，失败继续后续，能确认quiet再完成。fallback taskkill只精确自有PID，不得异常跳过owner kill。
2. **P1 partial spawn owner丢失**：spawn_owned_voice_worker可抛携带pending owner的BaseException。两处spawn赋值失败时未take_owned_worker_from_exception，self._child/server仍None，quiet可能伪True。按既有已审RVC协议接回pending，在finally真实回收/报告；保留原中断异常，未知不能成功。
3. **P1 返回0但quiet=False**：Root fake child poll0 / _is_spawn_quiescentFalse 时run exit_code0。child/server任一unknown/active/quiet查询失败都必须非0、truthful incomplete；服务器也必须检查完整Jobquiet，不能只rawpoll。normal返回与timeout/abort的最终result都以真实cleanup为约束。
4. **P2 坏metadata破坏最终报告/遮蔽原异常**：Root stdout首行`{not-valid-json`使finally JSONDecodeError，最终report不存在。每行限长/限条/限读总字节，解析错误记固定标签跳过，不读整份无限日志。finally报告故障不可掩盖KBI/原错误；尽可能保留live及cleanup错误。stdout/stderr总大小须有监控上限，超限停止诊断，不让硬盘无限增。
5. **P1 SafetyPolicy被两处绕过**：isolated launcher直接subprocess.call；unittest gate直接subprocess.run。全部改用safe_*固定参数，不安装/改site-packages。launcher OLLAMA_HOST等四项env不能os.environ.get接受任意主机，应强制任务固定127.0.0.1:11445 / 1 / 1 / 1m；无新网络权限。现有Root supervisor可跑新unittest模块，不要重复写裸supervisor。
6. **P2 所谓10秒cleanup总预算实际可能40秒**：child taskkill10+wait10+server taskkill10+wait10逐项独立上限。共用一个cleanup_deadline，所有动作按remaining，无budget只非阻塞kill/query并报incomplete。记录实际预算和elapsed，不能只把常量写进报告。
7. **P2 总deadline仍漏阶段**：gate.main在run开始前做preflight（GPU外部查询）；_start_isolated_server自己的15秒不遵循总deadline；_spawn前无deadline检查；采样后用旧remaining再wait。统一起点，阶段后重查，启动过预算不再spawn新模型，wait只剩余预算。公开承认不可中断OS/kernel调用不保证绝对硬实时，但不能代码主动越界等。
8. **验证/产物**：当前Native probe只单子线程且退出码124，不是成功用例断言；quiet在等待前取值过期。可直接复用既有已通过Native dummy子孙探针，或实现正确dummy父退出/终止，不以timeout=通过。gate flags所有互斥模式严格验证（isolated/no_ollama/rvc_only/torch_only冲突拒绝）；已有任一label产物（live/log/stages/partial）不得覆盖，不能只检查final.json。import-safe保留。

补上述注入测试（真实监督控制流），包括spawn携带owner失败、callback推进假钟超过总budget不再spawn、两owner共享cleanup预算、KBI+坏metadata保留中断。所有fake wait推进时钟，真实线程最后确认join；禁止默认scope扩展。已审production/RVC Native核心不变。Root三项脚本断言不可改弱。交付STOP待独立验收。
