# ADR: 装配默认工厂按需导入 TTS/STT

## 状态

Accepted（2026-11-04，Cursor 验证后 STOP 待 Root review）

## 背景

五包 lazy 已 Root 验收，但 `import src.core.assembly` 仍因模块顶栏与四个函数默认实参 `TTSEngine`/`STTEngine` 解析而加载 `src.voice.tts.engine`。文本/interactive 启动不应为此支付成本。

## 决策

- 移除 assembly 对 `TTSEngine`/`STTEngine` 的 eager import。
- 新增 `default_tts_engine_cls` / `default_stt_engine_cls`：函数体内 import 真实类并 `**kwargs` 构造；作为 `build_*` / `assemble_default` 默认 callable。
- 模块级 `TTSEngine`/`STTEngine` 经 PEP 562 兼容暴露；显式 `None` 注入行为不变。
- `AssembledParts.tts_engine_cls` / 助手懒加载注入路径不变。

## 验证

- `tests/test_assembly_lazy_defaults.py`
- 相关回归：`test_voice_lazy_exports`、`test_assembly_rollback`、`test_mode_assembly_round2`、`test_app_voice_activation`、`test_voice_foreground_bound`

## 未改

assembly 以外 Core 关闭语义、Root 探针、mmap/三模型声称；无真实设备。
