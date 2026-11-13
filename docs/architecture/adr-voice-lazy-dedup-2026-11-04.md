# ADR: 语音包 PEP 562 导出逻辑去重

## 状态

Accepted（2026-11-04，Root 独立探针 3/3 与仓库回归 17/17；详见 docs/codex-review-voice-lazy-dedup-2026-11-04.md）

## 背景

五个 `src/voice/**/__init__.py` 在 Root 通过的惰性导出切片上重复相同 `__getattr__` / `__dir__` / `importlib` 解析与 `globals()` 缓存。行为已验收；本切片仅保行为精简，不改映射、公开名、assembly 默认工厂或预取/底层栈。

## 决策

- 新增纯 stdlib `src/_lazy_exports.py`，提供 `install_lazy_exports(namespace)`：在包模块上安装闭包式 `__getattr__` / `__dir__`。
- 运行时读取 namespace 上的 `_LAZY_EXPORTS` 与 `__all__`（`__dir__` 不冻结可变表）；未知名 `AttributeError` 文案仍用所属包 `__name__`；成功解析写入 `namespace.__dict__[name]`；失败原样传播且不缓存。
- 各包保留 `TYPE_CHECKING`、`__all__`、`_LAZY_EXPORTS` 映射；末尾 `install_lazy_exports(sys.modules[__name__])`。无外部 registry、无第三方依赖。

## 验证

- 复用 `tests/test_voice_lazy_exports.py`、`tests/test_assembly_lazy_defaults.py`（未改测试）
- Scoped gate label `voice_lazy_dedup_20261104`：17 tests OK，WAV 826 零差 → `tmp/codex_gate_voice_lazy_dedup_20261104_2026_11_02.json`
- `compileall` 上述六个文件；`git diff --check` 退出 0

## 未改

assembly 默认 TTS/STT 工厂、Core/engine/Postprocess、声学/权限/资源上限、Root 探针与监督器。
