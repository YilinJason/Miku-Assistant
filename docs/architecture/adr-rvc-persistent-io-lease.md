# ADR: 持久 RVC convert IO lease 与 global client 生命周期

**状态**: STOP — 完整 public 静止谓词 + targeted/双 probe exit 0  
**范围**: convert pin/release；get/stop；private `_try_release_owner` 语义不变。

## public 完成谓词 `_public_stop_succeeded`

须同时满足（fail-closed，不等待）：

- `_proc is None`
- 无 `_start_inflight`、`_pin_inflight`、`_converting`
- 无 stdout/stderr reader 活跃、无 stdin 写
- 无 `_io_lease_release_failed`、无 retained token（含 `_convert_io_blocks_new_pin()`）

private 层仍可清 raw 后 lease 失败；public 必须 False，`last_stop_exited=False`，global 保留至 retry 成功。

## 监督

- `persistent_targeted` 35s → 14 tests OK
- `persistent_root_boundary` / `persistent_pin_stop` → exit 0

## 代码

- `src/voice/rvc/worker.py`
- `tests/test_rvc_persistent_io_lease.py`
