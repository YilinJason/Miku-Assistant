# 2026-10-18 恢复：CPU staging 初始化锁与确定性回归

用户继续授权：Codex设计/严格review，官方Cursor写代码；保留dirty，无提交推送。最新额度已刷新，可继续（10%停止线仍有效）。读AGENTS、相关skills、docs/codex-review-cosyvoice-cpu-checkpoint-staging.md最新段、原CPU staging任务。

本轮只收口初始化锁：cpu_checkpoint_staging_scope取得_SCOPE_LOCK后import torch、original_load读取和_make_patched_load仍在外层try前，任一异常可永久占锁。把所有取得锁后的初始化纳入外层finally无条件释放；只对已安装的wrapper执行deactivate/restore，避免未绑定变量掩盖原始异常。构造体异常/KeyboardInterrupt/SystemExit、import/属性读取/Path解析失败均恢复可再次进入。保留本轮闭包、别名scope外/下一scope安全原样委托、精确三文件/weights_only与线程边界，不扩大改动。

独立root两个probe保留且不得改：tmp/codex_cpu_staging_init_probe.py（上次False）、tmp/codex_cpu_staging_alias_probe.py（已True）。补初始化失败后可再次scope的fake。EOF fixture当前已存在_teardown_entered与_eof_delivered事件，先看已修改最终文件及assert顺序；不要盲目重复实现。若初次结果仍失败，保存完整失败trace再停止，不能以重跑绿替代原因调查。

先专项，tmp/cursor_cpu_checkpoint_staging_validation_gate.py相关模块门禁一次120秒/精确PID树/UTF8/文件集合前后零差。compile/diff。禁止真实模型/设备/主程序/清理任务/真实data/memory；禁止vendor/pin/超时/线程/音色/默认引擎/权限变更。交付停止Codex review，性能收益未实测，不允许App下一切片。
