# C2a：接入装配和关闭状态（仅此切片）

读取AGENTS与协作/架构/工程技能、C1最终评审顶部、相关assembly/assistant/config/safety实现。用户已授权Cursor官方CLI必要代码/文档外发和工作区修改。Codex设计与严格复审后才下一切片；保留已有dirty修改，禁真实data/memory、模型/设备/主程序、提交推送。

## 起点与范围

C1已经Codex独立274相关tests/owner probes通过，真实3组6段Cosy→RVC成功且资源回收。默认Edge保留。C2a只做可选装配与主程序关闭状态适配；**不**新增client.prepare/预热/预取线程/Timer/资源profile，不改现有B/C1生命周期、不改Nahida参数。C2b另行设计预热与有界预取。

1. 装配前用parse_cosyvoice_settings严格校验选定引擎内层配置，先于设备/日志/文件管理器等副作用。`build_tts_engine`仅在engine=cosyvoice时传`cosyvoice_config=tts_config.get('cosyvoice')`；Edge/pyttsx3注入工厂保持旧kwargs兼容。禁止自由路径/模型/instruct。
2. 共享SafetyPolicy只能在构造时传`TrustedLocalTtsProfile(PROJECT_ROOT)`；仅显式选用Cosy且local_tts_worker严格bool true时尝试，false时不查本地资产；不允许下层补封存或自行创建放行策略。权限缺省false，config新增注释/false键即可，engine仍Edge。选定Cosy的该权限非法类型在副作用前ValueError。
3. 固定资产缺失/不可信时保持未封存并fail closed，纯text/interactive模式仍可使用文本；进入语音会结构化失败，不能偷偷改引擎。可在构造策略失败时仅对可选local profile作PermissionError回退，再用原有trusted_rvc参数重建**尚未发布**的策略（非local的错误仍再次失败/抛出，不能吞掉）。metadata检查std lib only、不导入torch、不启动/下载、不创建本地缓存。不得改变已发布策略权限/根目录。日志固定标签，不落异常路径或正文。
4. AppAssistant.shutdown当前只要tts.shutdown没抛错就标_tts_shutdown_done。需在本地引擎返回后读`local_cosyvoice_shutdown_status()`，None/complete=false时对Cosy视为未完成，保留tts/共享TFM并允许下次shutdown重试；只有确认本地完整才允许TFM shutdown。旧Edge/注入替身无此方法时兼容原语义。主程序已经有speech→TTS→TFM顺序，保持且通过实际行为断言。C2a不预热新worker，未发布Cosy构造回滚不得启动模型。
5. 更新ADR/VOICE配置说明：可选本地路径已接装配但模型仍懒加载，默认未切；当前冷首句~19秒、热23–43字约5.8–8.3秒含RVC。不能显示/声称模型已预热或无等待。完整上线仍需C2b与后续验收。

## 验证

所有依赖fake，纯离线：Edge默认kwargs不变；Cosy正确inner参数/同一policy与TFM；非法配置/权限零设备；仅文本mode零TTS/RVC/localworker且资产缺失不破坏文本；显式Cosy且许可true才封存固定PROJECT_ROOT资产；缺失/拒绝语音结构化失败零fallback；shutdown本地incomplete时共享TFM零shutdown，重试complete后依序关闭；旧替身兼容。TFM每项addCleanup、不要_owned.clear/registry强制清理。复用现有fixture，不读真实记忆。

主Python312，先新增及mode_assembly/assembly_rollback/assistant_orchestration/stream相关专项，真正120秒测试树总超时。记录精确外层退出、测试计数、miku WAV文件集合零增；不重复900全量，不跑真实模型/设备。compile/diff-check后交接并停止Codex review。改动限assembly/assistant/必要config注释、窄helper、测试/文档；不顺手进入C2b。
