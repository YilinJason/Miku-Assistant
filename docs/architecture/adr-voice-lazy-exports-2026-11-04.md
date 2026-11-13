# ADR: 语音子包 PEP 562 按需导出

## 状态

Accepted（2026-11-04，Codex 初审返修后 STOP 待 Root 独立 review）

## 背景

五个 `src/voice/**/__init__.py` eager 导入会连带 TTSEngine/RVC/NumPy 等重栈。轻量路径（如 `worker_resources`、`TempFileManager`）不应触发无关实现。此切片**不**解决 `assembly` 等对 `TTSEngine` 的直接导入。

## 决策

对 `src/voice`、`tts`、`audio`、`rvc`、`cosyvoice` 五个包使用固定映射的 `__getattr__`；成功解析后写入 `globals()[name]`（模块属性缓存，后续不走 `__getattr__`）。`TYPE_CHECKING` 保留静态类型；`__all__` 不变。`__dir__` 返回 `sorted(set(globals()) | set(__all__))`，不触发 lazy 导出。

## 验证

- `tests/test_voice_lazy_exports.py`（独立子进程：sentinel、全表 identity、`__dir__`、ImportError/KBI/SystemExit 重试、并发首次访问）
- Root scoped gate：`tests.test_voice_lazy_exports`
- 相关回归：`tests.test_cosyvoice_production_slice_a`、`tests.test_voice_foreground_bound`

## 未改

assembly/Core/engine/Postprocess/mmap/声学/配置；无真实模型或设备。
