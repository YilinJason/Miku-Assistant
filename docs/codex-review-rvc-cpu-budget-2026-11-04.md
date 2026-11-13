# RVC CPU 预算 Root 最终验收 PASS

2026-11-04：最终源码逐行diff审阅通过。Root独立6/6（rvc_cpu_root6_20261104a）、新增仓库13/13、owned/security66/66、生命周期45/45，最后fixture修后安全+生命周期60/60（与45重叠）；全部无超时、WAV826精确集合零差。父env保持、casefold四键不能绕过、加载前setter一次、预算失败零模型加载、Windows三路径及非Windowsfake、权限拒绝、actualJob/TFM回收已离线证明。真实CPU/声音/模型未运行，不能据此宣称物理延迟/占用达标。

官方Cursor会话a96e9f30-5d43-4409-ac85-a81b44374101已STOP。中断报告b不作为验收证据，也不得覆盖；Root最终报告具名保留。下一切片仅Cosy retained被动对账，不动TFM/LLM。以下保留历史问题和修复依据。

## 第二次中断后的明确收口

Root已再次停止官方CLI exec35531；CIM无本轮Python/node残留，WAV仍826，最新文件创建在11/2，没有本轮共享WAV新增。观察到rvc_cpu_cursor_20261104b报告stderr由已有内容变为缺失再零长重建，JSON始终没有：**不得再删除/覆盖/复用任何测试报告label**；中断报告不足，不能声称通过。旧b现仅作未完成历史。

本轮返修只编写与局部compile/diffcheck，**不再启动任何测试**。Root先独立review再决定显式小组gate，避免盲目反复跑七模块。

- helper child env要处理Windows大小写不敏感：`omp_num_threads=999`与新写`OMP_NUM_THREADS=1`不能同时保留。生成copy时去掉四个预算键的所有大小写变体，再写标准键；其它环境原样保留，父环境不动。Root独立probe已有此真实边界。
- tests._retire_fake_owned中`if backend.created_names and joined_names != created_names: pass`是无意义假校验，去掉或替换有效证明（fakebackend的joined语义不等于fake主进程应join，不要强造断言）。实际assert returned owned.poll不为None且Job资源closed/active0，policyrunquiet。finally须先retire进程/释放IO lease再mgrshutdown，不能mgrshutdown失败就跳过进程回收；两项都独立尝试并把任何失败抛回主测试，不能吞BaseException。
- 只读核对test_rvc_worker_lifecycle部分构造fixture补字段范围。当前_stub临时MagicMock pending_count0可行，但不要改业务断言为无意义成功。已有daemon helper需finally gate解锁/actualjoin/主线程捕获异常，避免thread异常仅打印。不要全文件框架改造，只改受影响用例。
- 不得删改此前失败记录、不宣称STOP时测试其实还活着；本轮源码+局部compile/diffcheck+handoff STOP，不跑gate，Root随后独立测试。

Root读源时发现偏离明确任务，已Ctrl-C官方CLI(exec56328)，只读CIM确认没有本轮test/gate Python残留（唯一匹配为查询shell自身）。不得把未运行测试说成完成。

## 必须统一修复

1. cpu_budget.apply_rvc_torch_thread_budget直接写os.environ，且fake测试也不还原；违背任务“主进程只生成child env/不直接修改父环境”和安全规则。删除这四项写env；parent创建进程前的fixed child env已经覆盖worker/infer/probe BLAS。script内只应用Torch预算，不为手动入口扩环境权限。helper在fake调用前后所有环境必须一致。
2. 新建tmp/cursor_rvc_cpu_budget_gate_2026_11_04.py没有按指定复用唯一监督器，且timeout/失败不保证集合复查、额外全仓compile扩大范围。禁止运行它，不得修改Root监督器来迁就。仅tmp/codex_scoped_test_gate_2026_11_02.py --label rvc_cpu_cursor_20261104b --modules 明确tests名 --timeout 120，若失败实际读取日志和修复；不造第二监督器，不复用label。可保留废弃文件并标unused，不用删除非源码文件分散范围。
3. tests/test_rvc_cpu_budget.py fixture目前_make_policy没有file_delete，mgr造owned文件后只mgr.shutdown不检查complete，并忽略spawn返回的owned process。改fake policy对独占TemporaryDirectory授权delete，retain+retire fake owned process/Job，mgr.shutdown必须complete/owned0/workerquiet，再离开目录/patch；finally确保异常路径也回收。fake Torch调用测试不能污染父env。所有BaseException/关闭结果主测试断言。
4. 当前测试只证明helper调用一次，不证明两script/固定probe确实在加载/并行工作前调用预算一次。补importlib加载script但不main执行副作用的fake模块行为；实际main可fake参数/本地资产/torch/Config/VC/scipy/父watchdog与Job入口，不允许导入真实torch/scipy模型或启动设备进程。固定probe可在隔离namespace fake执行既有固定code，禁止用户代码能力扩大。包含预算失败零模型加载；不能源码字符串assert替代。
5. 拒绝路径原白名单/权限、Windowsowned三路径和非Windows内部env均保留，外部env/cwd仍拒绝。helperstdlib导入不拖重库；未知字段不能因预算便利变成新权限。预算范围1..4且cpu_count(None/1/2/4/64)与os.cpu_count None回退应fake确定。

所有其它已验收生产冻结。按既有task+上述清单返修、受监督gate+局部compile/diffcheck、ADR/handoff后STOP。Root独立probe与review再验收，不进入Cozy/TFM/LLM。读完整文档不要只改一项变绿。
