# B真实smoke缺陷：旧模型锁兼容

离线859项通过后Codex受控真实smoke失败，协议固定label=model_lock_busy。已定位固定`vendor/CosyVoice/runtime/cosyvoice_model.lock`为早期本任务版本留下7个NUL字节，mtime08:00:57，无模型/Worker在运行。当前代码先检查文件长度==1，否则拒绝；故历史锁永远使新runtime无法启动。

只修model_lock.py与直接测试，不进入C，不跑真实模型/设备、不改config/data-memory/提交推送。保留已有dirty工作。

设计：固定路径调用方已有词法祖先校验，helper仍需拒绝lock目标link/reparse/非普通文件。exclusive create新文件，竞争FileExistsError重新打开普通旧锁。不要在拿到OS锁前写/truncate，不delete/recreate锁，避免两个不同inode锁并存。open r+b后先msvcrt锁字节0（零长文件也可锁超EOF区域），确认fstat普通/身份稳定再检查内容/尺寸：1字节正常；0或旧版少量NUL(<=64字节)可在已持锁句柄内truncate为1并写NUL/flush，保留同inode；其它内容/大文件拒绝释放句柄。若OS锁忙，绝不改动长度或内容。

行为测试使用TemporaryDirectory下普通测试文件：0/7个NUL能恢复同inode1字节；非NUL/大文件/链接拒绝；持锁第二进程不能改动文件；释放后可再次获取；exclusivecreate竞争不截断。可以运行真正Windows文件锁的短独立子进程测试，无模型/网络/设备，所有Popen有超时且子进程退出清理。

当前真实固定7字节文件不要用shell删改，交给修复后的runtime首次启动自己在拿到锁后迁移。Cursor专项现有A/B+锁新增测试（90秒guard），更新handoff停止；Codex独立review后重跑真实smoke。
