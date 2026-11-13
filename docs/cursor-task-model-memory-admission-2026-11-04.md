# 本地模型内存准入（已放行）

完整设计：docs/architecture/adr-model-memory-admission-2026-11-04.md，必须读完再实现。Root设计+独立review，官方Cursor实施。读AGENTS与四skills/安全规则、检查dirty保留所有已有修改。前置TFM已PASS，不能顺带改TFM/声音/模型/权限/其它生命周期。

允许范围：新src/core/model_memory_admission.py、network_endpoint.py固定/api/ps、assembly.py/assistant.py guard ownership与关闭、OllamaClient/GenerateStatus、memory runtime/extractor的窄注入与post gate、必要相关tests/docs。已有OwnedThreadTask/host_memory只复用不改，不能新造线程框架。主要风险：metadata timeout底层未退出就重开、默认应用漏注入memory、低层兼容变成生产关闭开关、Core关闭把旧owner当完成、slow响应造成新前台卡顿。严格按ADR持有owner和有限前台预算，body硬上限，无缓存驻留许可。

只源码和tests编写、局部compile/diffcheck后STOP，不运行任何tests/gate/Rootprobe，不造新监督器、不覆盖报告。Root独立测试。所有fixture构造前隔离设备，独占目录/内存fake，gate最终释放→实际join/guard.close严格True→恢复patch→删除目录；不能吞线程异常或通过revoke丢owned文件。新status是资源错误，不能把拒绝映射连接失败/成功；不提交历史或写库。

禁止正式data/memory访问、真实模型/音频设备/下载安装/扫目录清理、注册表/OS配置、git回退提交推送。metadata新网络只固定sealed loopback并禁redirect；用户授权必要代码文档发送官方Cursor服务及workspace修改已持续有效。stateful guard创建装配前后无HTTP；rollback/主关闭都必须具名ownership。ADR保持未Accepted到Root裁定。
