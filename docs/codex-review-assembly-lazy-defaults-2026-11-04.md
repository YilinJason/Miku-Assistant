# 装配按需导入：Root PASS

Root对比本轮修改前快照与assembly最终源码：仅延迟默认工厂与兼容导出，四个旧入口保留kwargs、注入/回滚及AssembledParts引用。无新增线程/模型构造时机以外行为。新鲜解释器独立sentinel拒绝TTS/STT/NumPy等时import assistant成功；注入轻量假模块后默认工厂调用及kwargs/类identity兼容成立，无模型设备。

Root监督gate assembly_lazy_root_20261104：75 tests/22.060s OK，总22.922s，WAV826零差；编译与差异检查退出0。包括lazy包、assembly回滚、模式装配、App激活、前台等待。未发现阻断此切片的问题。没有重测真实音频/三模型，不宣称整体内存/启动延迟达标。当前资源板块继续RVC缺本地基础资产快失败，随后重复预检及系统commit准入。
