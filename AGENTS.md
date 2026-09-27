# Miku Assistant 开发入口

本项目是已有可用版本的 Windows 本地模型与语音助手。当前重点是渐进式工程化、实时对话、自然陪伴与长期记忆。未来可能适配 Quest 3，3D 资源和引擎尚未确定；不要把未来选项写成已确认技术决策。

## 项目 Skills

技能正文统一保存在仓库 `skills/`。按任务选择并阅读相关 SKILL.md，不必每次加载全部：

| 任务 | 技能入口 |
| --- | --- |
| 重构、职责拆分、兼容迁移 | [safe-refactoring](skills/safe-refactoring/SKILL.md) |
| 接口、事件、错误、生命周期和模块边界 | [architecture-contracts](skills/architecture-contracts/SKILL.md) |
| 测试、静态检查、配置与性能基线 | [engineering-quality](skills/engineering-quality/SKILL.md) |
| 任务交接、协同修改与代码评审 | [collaboration-review](skills/collaboration-review/SKILL.md) |

这些是仓库内的实际技能文件，不代表已安装到任一工具的个人技能目录。若客户端没有自动读取此入口，直接要求其读取本文件与对应技能；不要维护两套技能正文。

## 共同约束

- 修改前检查工作区，保留用户和其他开发者的已有修改。
- 将现有行为、建议设计和已验证结果分别说明。静态检查不代表真实音频链路已经测试。
- 系统操作遵循 `src/core/safety.py`；本地存在 `.cursor/rules/system-safety.mdc` 时一并读取。该文件目前被 Git 忽略，不能假定所有克隆都带有它。
- 测试不得真实关机、重启、锁屏；使用 fake 或现有 dry-run。不得绕过 SafetyPolicy 执行受管控副作用，不写注册表。注册表读取仅在用户明确要求且权限允许时进行。
- 主程序与 RVC 采用独立环境，执行或安装前核实解释器。不要仅为审计启动主程序、设备或清理任务。
- 重要跨模块决策及交接依据保存在仓库文档，不依赖聊天历史。技能不增加外部通信或发布权限。
