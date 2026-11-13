# RVC 子进程 CPU 预算（2026-11-04 已放行）

前置非流播报Root最终PASS。只读metadata核实独立.venv-rvc Torch 2.6.0+cu124；不可加载真实Torch/模型仅为测试，API通过fake验证。

问题：Cosy已有4线程上限，RVC两入口目前没有Torch/BLAS预算，CPU回退或多模型并存时易争抢。仅治理本程序所有RVC子进程；不改声音/推理参数、模型、全局环境/OS/权限。固定保守默认 n=max(1,min(4,(os.cpu_count() or 2)//2))，interop=1；当前不增加配置与热调参入口。

## 实施方向

- 纯stdlib小helper定义确定预算及从给定环境copy生成child env，强制OMP_NUM_THREADS/MKL_NUM_THREADS/OPENBLAS_NUM_THREADS/NUMEXPR_NUM_THREADS为n；不得修改父os.environ，不能setdefault沿用恶意巨大数。仅固定键，不加入任意env覆盖新能力。
- SafetyPolicy.spawn_rvc_worker在sanitize后、所有平台实际spawn前生成固定child env；Windows owned run的infer/probe在内部构造popen env时使用相同helper，非Windows受控run同理内部生成，外部sanitize仍禁止env/cwd。不要改变argv/Job/所有权/IO pin/超时/输出协议。
- scripts/rvc_worker.py与rvc_infer.py在任何模型推理/加载前设torch.set_num_threads(n)、set_num_interop_threads(1)。应在首次import torch后且库并行工作前，只调用一次；不在每次convert或patch_torch_load反复调用。helper导入本身不得import torch/numpy。失败受控退出，不能吞异常并无限线程运行。
- 固定rvc_probe若import torch也在执行前应用Torch预算；严格保持原固定probe白名单，不允许调用方传代码。复用预算helper，避免复制多套。
- 主进程只生成child env，通过现有SafetyPolicy具名能力传递；不调用safe_setenv要求用户开放通用env_modify、不修改用户环境。手动运行script也先设置Torch预算，自动启动时BLAS在进程创建前已有固定env。文档清楚声明不证明CPU使用率/真实延迟。

参考官方文档：torch.set_num_threads必须在eager/JIT/autograd工作前，set_num_interop_threads仅能在interop工作前调用一次：https://docs.pytorch.org/docs/2.15/generated/torch.set_num_threads.html 与 https://docs.pytorch.org/docs/2.15/generated/torch.set_num_interop_threads.html 。本地RVC解释器版本优先核实，禁安装依赖。

## 验证

fake torch记录调用顺序/次数；fakecpu(None/1/2/4/64)预算有界；父环境前后相同；已有巨大线程值被child copy覆盖；未知字段/权限拒绝仍拒绝且零spawn；worker/infer/probe三路径实际spawn fake捕获env，Windowsowned路径也覆盖；不只字符串源码断言。

读AGENTS四skills安全规则保留dirty。测试仅主Python312受监督唯一labels顺序，禁raw/discover/Rootprobe、真模型设备/正式memory/提交推送。注意tests中AudioPlayer构造前fake。实现后必要既有security/ownedrun/RVC生命周期回归+compile/diffcheck+ADR/handoff STOP等Root独立review。不要改legacy播报/TFM/LLM或进入其它切片。
