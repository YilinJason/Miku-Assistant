"""
配置管理模块。

负责加载、保存与读写 ``config/config.yaml``，支持点号分隔的嵌套键访问，
并提供 RVC 等相对路径到项目根目录的解析。
"""
import os
import yaml
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.paths import PROJECT_ROOT


class Config:
    """
    应用配置管理类。

    从 YAML 文件读取配置，文件不存在时写入默认配置；
    支持 ``get`` / ``set`` 及 ``[]`` 运算符访问嵌套键。
    """

    def __init__(self, config_path: str = None):
        """
        初始化配置管理器并立即加载配置文件。

        Args:
            config_path: 配置文件路径；为 ``None`` 时使用
                ``<项目根>/config/config.yaml``。
        """
        if config_path is None:
            config_path = PROJECT_ROOT / "config" / "config.yaml"

        self.config_path = Path(config_path)
        self._config: Dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        """
        从磁盘加载 YAML 配置。

        若配置文件不存在，则生成默认配置并保存到 ``config_path``。
        """
        if self.config_path.exists():
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = yaml.safe_load(f) or {}
        else:
            self._config = self._get_default_config()
            self.save()

    def save(self) -> None:
        """将当前内存中的配置写回 ``config_path``（自动创建父目录）。"""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, 'w', encoding='utf-8') as f:
            yaml.dump(self._config, f, allow_unicode=True, default_flow_style=False)

    def _get_default_config(self) -> Dict[str, Any]:
        """
        构建首次运行时的默认配置字典。

        Returns:
            包含 Ollama、语音、安全、搜索、扩展等各节的默认配置。
        """
        return {
            'ollama': {
                'base_url': 'http://localhost:11434',
                'model': 'Miku_Assistant-emotion:latest',
                'timeout': 60,
                'keep_alive': '30m',
                'warmup_on_start': True,
                'warmup_timeout': 120,
                'history': {
                    'max_stored_turns': 500,
                },
            },
            'voice': {
                'tts': {
                    'engine': 'edge-tts',
                    'voice': 'zh-CN-XiaoxiaoNeural',  # edge-tts 需要具体的 voice 名称
                    'language': 'zh-CN',
                    'rate': 150,
                    'volume': 0.8,
                    'fallback_enabled': True,
                    'fallback_engine': 'pyttsx3',
                    'pipeline_enabled': True,
                },
                'stt': {
                    'engine': 'speech_recognition',
                    'language': 'zh-CN',
                    'energy_threshold': 4000
                }
            },
            'system': {
                'windows_control_enabled': False,
                'require_confirmation': True,
            },
            'security': {
                'permissions': {
                    'registry_read': False,
                    'registry_write': False,
                    'system_power': False,
                    'subprocess': True,
                    'os_shell': False,
                    'browser_launch': False,
                    'file_delete': True,
                    'env_modify': False,
                },
            },
            'search': {
                'chrome_path': None,
                'search_engine': 'google',
                'auto_open': False
            },
            'extensions': {
                'character_model': {
                    'enabled': False,
                    'type': None,
                    'config': {}
                }
            },
            'assistant': {
                'auto_tts': True,
                'stream_reply': True,
                'duplex_listening': True,
                'duplex_listen_timeout': 0.8,
                'duplex_phrase_time_limit': 6.0,
                'duplex_echo_grace_seconds': 1.0,
                'duplex_interrupt_wait_timeout': 5.0,
                'duplex_min_interrupt_chars': 2,
                'duplex_echo_match_ratio': 0.72,
                'wake_word': 'miku',
                'log_level': 'INFO',
                'logging': {
                    'file_enabled': True,
                    'file_name': 'miku.log',
                    'max_size_mb': 10,
                    'backup_count': 5,
                },
                'file_cleanup': {
                    'interval_minutes': 10,
                    'cleanup_on_start': False,
                    'max_scan_files': 500,
                    'max_delete_files': 100,
                    'tmp_max_age_hours': 1.0,
                    'logs_max_age_days': 1,
                    'test_max_age_hours': 72,
                },
            },
            'memory': {
                'enabled': False,
                'db_path': None,
                'recall': {
                    'max_entries': 8,
                    'max_chars': 1200,
                    'candidate_limit': 200,
                },
                'ingestion': {
                    'queue_capacity': 32,
                    'max_extractions_per_turn': 16,
                    'close_drain': True,
                    'close_timeout': 5.0,
                },
                'extractor': {
                    'base_url': None,
                    'model': None,
                    'timeout': None,
                    'keep_alive': None,
                    'num_ctx': None,
                    'num_predict': 512,
                },
                'retention': {
                    'enabled': False,
                    'max_records': 2000,
                    'max_age_days': 365.0,
                    'maintenance_interval_hours': 24.0,
                    'preserve_user_confirmed': True,
                    'batch_limit': 64,
                },
            },
        }

    def get(self, key: str, default: Any = None) -> Any:
        """
        按点号分隔的路径读取嵌套配置值。

        Args:
            key: 配置键，如 ``ollama.model`` 或 ``voice.tts.rate``。
            default: 键不存在或中间节点非字典时返回的默认值。

        Returns:
            匹配的配置值，或 ``default``。
        """
        keys = key.split('.')
        value = self._config
        for k in keys:
            if isinstance(value, dict):
                value = value.get(k)
                if value is None:
                    return default
            else:
                return default
        return value if value is not None else default

    def set(self, key: str, value: Any) -> None:
        """
        按点号分隔的路径写入嵌套配置值（自动创建中间字典）。

        Args:
            key: 配置键，如 ``assistant.auto_tts``。
            value: 要写入的值。
        """
        keys = key.split('.')
        config = self._config
        for k in keys[:-1]:
            if k not in config:
                config[k] = {}
            config = config[k]
        config[keys[-1]] = value

    def __getitem__(self, key: str) -> Any:
        """
        支持 ``config[key]`` 语法，等价于 ``get(key)``。

        Args:
            key: 配置键。

        Returns:
            配置值；键不存在时行为与 ``get`` 一致（返回 ``None``）。
        """
        return self.get(key)

    def __setitem__(self, key: str, value: Any) -> None:
        """
        支持 ``config[key] = value`` 语法，等价于 ``set(key, value)``。

        Args:
            key: 配置键。
            value: 要写入的值。
        """
        self.set(key, value)

    def resolve_path(self, path: Optional[str]) -> Optional[str]:
        """
        将相对路径解析为基于项目根目录的绝对路径。

        Args:
            path: 待解析路径；空值原样返回。

        Returns:
            绝对路径字符串；已是绝对路径则不变。
        """
        if not path:
            return path
        if os.path.isabs(path):
            return path
        return str(PROJECT_ROOT / path)

    def get_rvc_config(self) -> Optional[Dict[str, Any]]:
        """
        获取已解析路径的 RVC（Retrieval-based Voice Conversion）配置。

        Returns:
            含 ``model_path`` 等已解析为绝对路径的配置副本；
            未配置 ``model_path`` 时返回 ``None``。
        """
        rvc_config = dict(self.get("voice.tts.rvc", {}) or {})
        if not rvc_config.get("model_path"):
            return None

        rvc_config["model_path"] = self.resolve_path(rvc_config.get("model_path"))
        if rvc_config.get("index_path"):
            rvc_config["index_path"] = self.resolve_path(rvc_config.get("index_path"))
        if rvc_config.get("python_path"):
            rvc_config["python_path"] = self.resolve_path(rvc_config.get("python_path"))
        return rvc_config
