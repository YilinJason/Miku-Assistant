# 语音惰性导出重复代码精简（已放行）

属于用户要求的保行为精简，前置预取19+相关138/182+最终fixture68与里程碑均已Root通过。Root设计评审、官方Cursor CLI实施，读AGENTS与四repo skills/安全规则。巨大dirty保留，禁git回退/正式data/memory/真实模型设备下载清理/提交推送。不改声音、模型、权限、资源上限、现有线程协议。

## 范围与目的

src/voice、audio、rvc、tts、cosyvoice五个__init__.py重复相同PEP562导出解析/缓存/dir。集中到一个纯stdlib小helper（例如src/_lazy_exports.py），包内保留原TYPE_CHECKING导入、__all__和_LAZY_EXPORTS映射，只安装两项dunder或用很薄代理；不改assembly的默认工厂，不把全部模块提前导入。

优先一个install_lazy_exports(namespace)安装闭包：未知名AttributeError类型/文本保留（取所属包__name__），已知名只import对应模块，成功后缓存到所属namespace；异常原对象原样传播且不缓存失败。dir仍按实时namespace与实时__all__排序并去重，不以常量闭包冻结可变导出表。公共导出名称、对象identity、旧from-import与star-import一致；私有helper名称出现在dir的变化不当作公开API。没有无界外部registry、无锁/线程/文件/模型/设备副作用，不引第三方库。

避免为去重增加大型工厂/类框架或扩大公共API。不以删安全边界追求行数。报告净行数和重复块减少，仅此模块行为不变，不把其它错误修复混入。

## 验证

先读现有tests/test_voice_lazy_exports.py与test_assembly_lazy_defaults.py。优先复用已有fresh process import sentinel证明普通package import/dir不导入numpy/torch/音频/模型，逐个导出对象identity/缓存/错误传播/未知名与sorted dir。只有缺关键契约才补有意义测试，不加源码字符串匹配。

主Python312+现有scoped gate，唯一label前台顺序<=120秒，不raw unittest/discover/detach/并发、不运行修改Rootprobe/监督器。指定compileall+diffcheck、简短ADR/交接后STOP。Root独立fresh sentinel与源码评审过后再考虑下一精简切片或最终性能，不做真实长测。
