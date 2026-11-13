# 持久 RVC IO 生命周期（正式放行，2026-11-03）

单次 owned-run 已由 Root PASS：独立469测试OK(skip1)、故障探针通过，依据 docs/codex-review-owned-run-final-2026-11-03.md。现在只实施本切片。读取 AGENTS/architecture-contracts/engineering-quality/collaboration-review。保留 Native Job、现有持久 stdout 读取/启动协议、原默认与声学设置；不访问正式 data/memory，不 main/模型/设备/提交推送。

## 同一回收边界的全局引用问题

`infer.get_rvc_worker` 在 process_alive=False 分支忽略 old.stop()=False 仍覆盖全局client；旧raw已退出但reader/pin未退时会丢owner。无论raw状态如何，旧stop必须真实完成才能替换；保留旧引用，busy拒绝。全局get/rebuild/stop必须互斥发布（当前无锁）；并发两个get不能双构造，stop快照旧client后不能清除另一个新client。短状态锁+单次生命周期准入，IO在锁外；busy返回结构化既有Unavailable，不新增无限等待。构造未返回也须在途保护，stop不能假报全局静止。临时管理器绑定不能把旧lease转嫁到新manager。

## 目标

持久 worker.convert 的输入/输出在 worker 仍可能读写时必须受 TempFileManager 的有界 IO lease 保护。不能 cancel 后返回 False 就立即允许删除输出。复用现有 pin_io_paths/release_io_lease，不复制临时文件管理器或创建额外清理线程。

## 简单有界协议

- 每个持久 worker 至多一个 convert lease；新 convert 必须在分配/发送前检查上一个已回收，无旧 token/mgr 覆盖。assert 与 pin 使用已有 TFM 原子验证，pin在发出可能写入的请求之前。
- 正常收到当前请求的明确终态响应，表示 worker 已完成该请求的文件操作，可以释放（包括明确失败响应）。进程继续常驻不影响本次 lease 正常释放。
- cancel/timeout/读异常/请求可能部分写入而响应未知，保留 lease，现有 stop/terminate 实际确认整个owner+reader+stdin结束后释放。generation失效/过期响应不得作为新请求完成证据。
- token/mgr 与 generation 绑定，捕获部分 stdin.write 失败；lease释放/查询异常保留所有权并阻止新 convert，stop不得假报成功。不要等待所有caller锁或持spawn锁进行IO。
- 进程已确认静止但 lease 释放失败，保留可重试记录，重复 stop 可以重试释放；不得因 _proc=None 就忽略 token。防止停机回调与convert finally重复解pin。所有路径与现有stop布尔状态一致。
- 调用方正常finally remove/remove_delayed只删除未pin项；TFM.shutdown在pin活跃时不complete；不扩大文件白名单，不删除模型、用户文件或非owned路径。

## fake 行为覆盖

普通成功/明确失败响应释放；stdin部分失败、timeout/cancel仍活保留pin；stop/reader未退出不得解pin；晚响应不解新lease；清理失败后重试；新请求零新分配；TFM remove/delayed/shutdown在pin时保留文件；多个成功循环pin计数回0。沿用隔离临时目录，所有测试线程join。

做原持久/owned/TFM/上层关闭相关一次有界回归与Root独立probe。返回handoff/ADR并STOP，未放行候选/预热/预取。
