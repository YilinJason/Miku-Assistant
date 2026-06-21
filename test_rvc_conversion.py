# -*- coding: utf-8 -*-
"""
RVC 音色转换端到端测试脚本。

流程：
    1. 读取 ``config/config.yaml`` 中的 RVC 配置并做健康检查
    2. 使用 edge-tts 生成基础 WAV（``data/test/test_X_base.wav``）
    3. 调用 ``TTSEngine.apply_rvc_model`` 做 Nahida 音色转换
    4. 将结果复制到 ``data/test/test_X_rvc.wav`` 供人工试听对比

用法：
    py test_rvc_conversion.py

退出码：0 表示至少一条 RVC 转换成功，1 表示失败或用户中断。
"""
import sys
import io
import os
from pathlib import Path

# Set UTF-8 encoding for Windows console
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 添加项目路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

import asyncio
import edge_tts
import logging
from src.voice.tts import TTSEngine
from src.core.config import Config
from src.core.paths import get_test_output_dir
from src.core.safety import SafetyPolicy
from src.voice.rvc.health_check import check_rvc_health

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

def test_rvc_conversion():
    """
    执行 RVC 音色转换测试套件。

    Returns:
        bool: 至少一条测试 RVC 转换成功时为 True，否则 False。
    """
    print("=" * 60)
    print("RVC音色转换测试")
    print("=" * 60)
    print()
    
    # 加载配置
    try:
        config = Config()
        rvc_config = config.get_rvc_config()
        
        if not rvc_config or not rvc_config.get('model_path'):
            print("❌ 错误: 未找到RVC配置")
            print("   请在 config/config.yaml 中配置RVC模型路径")
            return False
        
        model_path = rvc_config.get('model_path')
        index_path = rvc_config.get('index_path')
        device = rvc_config.get('device', 'cpu')
        pitch = rvc_config.get('pitch', 0)
        
        print(f"📋 配置信息:")
        print(f"   模型路径: {model_path}")
        print(f"   索引文件: {index_path or '未配置'}")
        print(f"   设备: {device}")
        print(f"   音调调整: {pitch}")
        print()

        report = check_rvc_health(rvc_config)
        print(report.summary())
        print()
        if not report.ok:
            print("❌ RVC 环境未就绪，请运行:")
            print("   powershell -ExecutionPolicy Bypass -File scripts/setup_voice_env.ps1")
            return False
        
        print("✅ RVC 环境与 Nahida 模型检查通过")
        print()
        
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # 创建测试输出目录（data/test）
    test_output_dir = get_test_output_dir()
    
    # 测试文本
    test_texts = [
        "你好，我是Nahida，很高兴认识你！",
        "这是一个RVC音色转换测试。",
        "如果音色转换成功，你应该能听到我的声音发生了变化。"
    ]
    
    print("🎤 开始生成测试音频...")
    print()
    
    # 初始化TTS引擎
    try:
        tts_engine = TTSEngine(
            engine='edge-tts',
            language='zh-CN',
            voice='zh-CN-XiaoxiaoNeural',
            rvc_config=rvc_config,
            safety_policy=SafetyPolicy.from_app_config(config),
        )
        print("✅ TTS引擎初始化成功")
        print()
    except Exception as e:
        print(f"❌ TTS引擎初始化失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # 测试每个文本
    results = []
    for i, text in enumerate(test_texts, 1):
        print(f"📝 测试 {i}/{len(test_texts)}: {text}")
        print("-" * 60)
        
        try:
            # 生成基础音频（edge-tts）
            print("  1. 生成基础音频（edge-tts）...")
            async def generate_base_audio():
                communicate = edge_tts.Communicate(text, 'zh-CN-XiaoxiaoNeural')
                base_audio_path = test_output_dir / f'test_{i}_base.wav'
                await communicate.save(str(base_audio_path))
                return base_audio_path
            
            base_audio_path = asyncio.run(generate_base_audio())
            print(f"     ✅ 基础音频已保存: {base_audio_path}")
            
            # 尝试RVC转换
            print("  2. 尝试RVC音色转换...")
            rvc_audio_path = tts_engine.apply_rvc_model(str(base_audio_path))
            
            if rvc_audio_path and os.path.exists(rvc_audio_path):
                # 复制RVC转换后的音频到测试目录
                import shutil
                final_rvc_path = test_output_dir / f'test_{i}_rvc.wav'
                shutil.copy2(rvc_audio_path, final_rvc_path)
                print(f"     ✅ RVC转换成功: {final_rvc_path}")
                results.append({
                    'text': text,
                    'base_audio': base_audio_path,
                    'rvc_audio': final_rvc_path,
                    'success': True
                })
            else:
                print(f"     ⚠️  RVC转换失败，使用基础音频")
                results.append({
                    'text': text,
                    'base_audio': base_audio_path,
                    'rvc_audio': None,
                    'success': False
                })
            
            print()
            
        except Exception as e:
            print(f"     ❌ 测试失败: {e}")
            import traceback
            traceback.print_exc()
            results.append({
                'text': text,
                'base_audio': None,
                'rvc_audio': None,
                'success': False,
                'error': str(e)
            })
            print()
    
    # 显示测试结果
    print("=" * 60)
    print("📊 测试结果汇总")
    print("=" * 60)
    print()
    
    success_count = sum(1 for r in results if r.get('success'))
    total_count = len(results)
    
    print(f"总测试数: {total_count}")
    print(f"成功数: {success_count}")
    print(f"失败数: {total_count - success_count}")
    print()
    
    if success_count > 0:
        print("✅ RVC音色转换功能正常！")
        print()
        print("📁 测试音频文件保存在:")
        print(f"   {test_output_dir}")
        print()
        print("📝 文件说明:")
        print("   - test_X_base.wav: 原始edge-tts音频（转换前）")
        print("   - test_X_rvc.wav: RVC转换后的音频（转换后）")
        print()
        print("💡 提示:")
        print("   1. 请对比 base 和 rvc 音频文件，检查音色是否发生变化")
        print("   2. 如果音色没有变化，说明RVC项目未正确集成")
        print("   3. 若未安装 RVC 环境，请运行 scripts/setup_voice_env.ps1")
    else:
        print("❌ RVC音色转换功能未正常工作")
        print()
        print("🔍 可能的原因:")
        print("   1. RVC项目未安装或未找到")
        print("   2. RVC模型文件路径错误")
        print("   3. RVC项目依赖未正确安装")
        print()
        print("💡 解决方案:")
        print("   1. 运行 scripts/setup_voice_env.ps1 安装 RVC 推理环境")
        print("   2. 检查 config/config.yaml 中的RVC配置")
        print("   3. 查看上面的详细日志信息")
    
    print()
    print("=" * 60)
    
    return success_count > 0

if __name__ == '__main__':
    try:
        success = test_rvc_conversion()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n测试已取消")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试过程出错: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)










