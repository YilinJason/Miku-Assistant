# 首个自然分句提前准备语音（已放行）

先读AGENTS、四skills、安全规则。上个mmap切片已Root162与真实六句验收；不要改它。仅允许src/core/sentence_splitter.py、src/core/assistant.py，新增tests/test_first_clause_latency.py。保留其它dirty，正式data/memory禁访问；不改config、声学、模型、资源/超时线、prefetch队列、历史/生成提交语义或旧测试。无test/model/device运行权限，AST后STOP等Root。

事实：stream splitter目前只在句号/问号等句末或max_buffer=500才放出首段，一条带多个逗号的长句会等到句尾并整句合成。13字Cosy+RVC约4.3秒；不能用随机半精度提速（已因变慢撤回）。目标只让首个完整自然分句提前进入已有单producer/1项预取，增加至多一个分段，不改整段显示文本与历史。

设计：
- IncrementalSentenceSplitter新增可选keyword first_clause_min_chars: int|None=None；None原行为完全兼容。启用时严格int非bool，1..max_buffer范围；主程序生成stream只传12（若既有max_buffer<12则用min(12,max_buffer)）。不加新config键/框架。
- 在整个turn第一次实际输出前，允许在中文逗号，或后跟空白的英文逗号处分句；该prefix strip后长度>=阈值。选择最早合格逗号，但若已有更早硬句末则先硬句末；超过max_buffer仍按原上限，无缺字/重排。只软切一次，后面完全沿用硬句末/overflow。换行/空白不算已输出，首次非空硬切/overflow也消耗首段资格。
- discard清buf并重置首段状态以兼容复用；finish尾句照旧，不能多flush重复。不创建线程/缓存/新队列，不改English数字1,000的切分。
- assistant只在现有IncrementalSentenceSplitter构造处启用，不改_try_enqueue、SpeechProjector、display callbacks/取消/生成终结/commit。
- 新离线行为测试：逐字符与整delta输出一致、早硬句末优先、prefix太短跳过、仅首段软切、1,000不切、默认兼容、阈值严格、overflow/finish/discard复用、无重复无丢字。Fake流式集成可验证首段在生成完成前进入现有消费者，但不要构造实际播放器/模型/正式DB；复用现有注入夹具，不吞异常和遗留线程。

返回精确diff与局限，禁止自报音质或首音频真实性能通过。Root审新fixture与调用方，受监督跑相关stream/结构化/取消回归，再以相同固定长句对比首个可播放音频时间；保持capture-only，最多单项预取。通过后才进入30分钟真实模型长测。
