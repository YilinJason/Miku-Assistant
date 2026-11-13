# ADR: RVC 本地基础资产快失败（禁止隐式下载）

## 状态

Accepted（2026-11-04，Cursor fake 验证后 STOP 待 Root）

## 背景

``scripts/rvc_worker.py`` / ``scripts/rvc_infer.py`` 曾调用 ``download_rvc_models``，缺 ``hubert_base.pt``/``rmvpe.pt`` 时会无 timeout 网络下载；且 ``import rvc_python`` 本身很重。

## 决策

- 新增 ``src/voice/rvc/local_assets.py``：stdlib-only ``find_spec('rvc_python')`` + ``stat``/``S_ISREG`` 两个固定 ``base_model`` 文件；``find_spec`` 的 ImportError/ValueError/OSError 规范为 ``RvcLocalAssetsError``（不吞 KBI/SystemExit）；仅接受包 ``__init__.py``/``__init__.pyc`` origin，拒绝 namespace/单文件模块；失败用稳定错误码，不记录用户路径。
- Worker ``_load_vc`` / infer ``main``：资产检查在 ``torch`` patch 与 ``Config``/``VC`` 之前；删除 ``download_rvc_models``。
- 仅存在性检查，不验证权重内容；KBI/SystemExit 不吞。

## 验证

- ``tests/test_rvc_local_assets.py``（fake spec + 脚本入口顺序）
- 相关回归：``test_rvc_health_probe_budget``、``test_rvc_owned_worker``、``test_rvc_owned_run``、``test_rvc_worker_lifecycle``

## 未改

health_check/prepare sticky、vendor、声学默认、Root 探针；不声称三模型共存已解决。
