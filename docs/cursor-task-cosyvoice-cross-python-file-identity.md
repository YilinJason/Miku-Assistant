# B真实缺陷：跨Python文件设备标识不一致

Codex有界startup诊断已复审，独立23项OK。真实extended-cold模型14.598秒ready，随后output_rejected；不是模型不能加载。直接只读对比同一`src/voice/cosyvoice/worker_output.py`：主Python3.12 st_dev=14415720814859885606，CosyPython3.11 st_dev=250453030；st_ino均6755399441099317。因此直接把父os.stat.st_dev交给子os.stat比较不兼容。

## 方案

在Cosy模块内新增窄的stdlib-only跨进程文件标识helper，支持path和已打开fd。Windows用相同native FILE_ID_INFO（GetFileInformationByHandleEx/FileIdInfo，完整64位VolumeSerialNumber+128位FileId），对fd用msvcrt.get_osfhandle；声明正确ctypes HANDLE/BOOL/argtypes/restype。非Windows使用标准st_dev/st_ino。path helper打开仅为读身份，所有分支finally关闭；调用方继续现有词法目录/普通文件/reparse检查，helper不替代信任边界。

父构建synth请求时从已登记owned输出获取此portable pair，协议output_dev/output_ino可沿用字段但明确现在是portable identity（schema bump必要时写清，父子同版本固定）；child validate_worker_output_path及run_inference_instruct_to_wav打开后expected检查均用同一helper。父TFM和最终音频验证继续原本同进程FileIdentity登记与复验；不要改全局capture_file_identity或RVC安全逻辑，不牵连主程序。

严禁通过掩码截短dev、只比inode、跳过dev、接受任意owned路径、撤销output ownership来“修好”。仍逐级拒绝link/reparse、固定TMP、同inode打开跨inference、身份替换failclosed、parent单句柄WAV校验。native API不可用必须拒绝而不是降级成不同格式。

Codex已核对官方接口定义：[Microsoft FILE_ID_INFO](https://learn.microsoft.com/en-us/windows/win32/api/winbase/ns-winbase-file_id_info)：ULONGLONG VolumeSerialNumber + FILE_ID_128，二者组合标识本机同一文件。使用同一API完整值，不依赖各Python版本stat的设备编码。

## 验证

固定隔离文件（无模型/无设备/无网络）上，主3.12与vendor固定3.11 helper返回完整pair相同；可用短固定解释器子进程、有限超时，并真实回收。path与fd一致；不同文件不同；路径替换/异常fd/API异常拒绝；原恶意输出测试保留。现有直接用os.stat构造worker请求的测试需改正确portable helper，不写mock特例绕过生产。

只改Cosy identity/client/worker_output与相关测试/文档，无RVC/config/assembly/C/data-memory/vendor改动，无提交推送。先读AGENTS及相关skills，保留既有脏改动。新增测试+专项A/B/lock/startup一次90秒真超时，记录RC与临时文件净增长0；完成停下待Codex严格复审。Cursor不得真实启动模型。

后续Codex重跑默认30秒cold/warm/idle smoke，判断真实首启波动（前次WeText导入12.3秒，此次3.4秒）。不能因此默认扩大timeout；若首次初始化波动仍超过30秒，需独立性能方案和测量。
