"""集中读取和验证配置；导入模块不加载环境、不初始化业务对象。"""
from __future__ import annotations

import math
import os
from pathlib import Path
import sys
from threading import Lock
from urllib.parse import urlsplit

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[3]
_environment_lock = Lock()
_environment_loaded = False
_TRUE = {'1', 'true', 'yes', 'on'}
_FALSE = {'0', 'false', 'no', 'off'}
_PLACEHOLDERS = {'your_openai_compatible_key', 'your_amap_web_service_key',
                 'your_api_key', 'your_model', 'your_model_name', '你的模型服务密钥',
                 '你的高德地图web服务key', '供应商支持的模型名'}


class ConfigurationError(ValueError):
    """只报告配置名称和安全规则，不回显配置值。"""


# 由启动入口显式调用，线程安全地只加载一次项目 .env。
def load_environment() -> None:
    """Load .env once without replacing process environment; failed loads may retry."""
    global _environment_loaded
    with _environment_lock:
        if not _environment_loaded:
            load_dotenv(PROJECT_ROOT / '.env', override=False)
            _environment_loaded = True


# 必需项不提供猜测值；空白和模板占位符也视为未配置。
def required_string(name: str) -> str:
    value = os.getenv(name, '').strip()
    if not value or value.lower() in _PLACEHOLDERS or value.lower().startswith('your_'):
        raise ConfigurationError(f'{name}: 必须填写有效值，不能留空或使用模板占位符')
    return value


def boolean(name: str) -> bool:
    value = required_string(name).lower()
    if value not in _TRUE | _FALSE:
        raise ConfigurationError(f'{name}: 必须为 true/false 或 1/0（也支持 yes/no、on/off）')
    return value in _TRUE


def positive_timeout(name: str, maximum: float | None = None) -> float:
    raw = required_string(name)
    try:
        value = float(raw)
    except ValueError:
        raise ConfigurationError(f'{name}: 必须为有限正数秒数') from None
    if not math.isfinite(value) or value <= 0 or (maximum is not None and value > maximum):
        rule = f'且不超过 {maximum:g}' if maximum is not None else ''
        raise ConfigurationError(f'{name}: 必须为有限正数秒数{rule}')
    return value


def _url(name: str, *, https_only: bool = False) -> str:
    value = required_string(name)
    try:
        parts = urlsplit(value)
        valid = (parts.scheme in ({'https'} if https_only else {'http', 'https'})
                 and bool(parts.hostname) and parts.username is None
                 and parts.password is None and not parts.fragment
                 and not any(character.isspace() for character in value))
        parts.port  # 同时检查非法端口；不把解析异常中的原值暴露出去。
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(f'{name}: 必须为有效的 {"HTTPS" if https_only else "HTTP(S)"} 地址，不能包含用户密码或片段') from None
    return value


def model_base_url() -> str:
    return _url('OPENAI_BASE_URL')


def model_name() -> str:
    return required_string('OPENAI_MODEL')


# 唯一明确可留空的模型配置：空白表示禁用备用模型。
def fallback_model_name() -> str:
    value = os.getenv('OPENAI_FALLBACK_MODEL', '').strip()
    return required_string('OPENAI_FALLBACK_MODEL') if value else ''


def model_timeout() -> float:
    return positive_timeout('OPENAI_TIMEOUT_SECONDS')


def display_timeout() -> float:
    return positive_timeout('TRAVEL_DISPLAY_TIMEOUT_SECONDS')


def database_path() -> str:
    value = Path(required_string('CITY_DB_PATH'))
    return str(value if value.is_absolute() else PROJECT_ROOT / value)


def amap_url() -> str:
    return _url('AMAP_MCP_URL', https_only=True)


def amap_timeout() -> float:
    return positive_timeout('AMAP_MCP_TIMEOUT_SECONDS', maximum=120)


def amap_api_key() -> str:
    name = 'AMAP_MAPS_API_KEY' if os.getenv('AMAP_MAPS_API_KEY', '').strip() else 'AMAP_MAP_KEY'
    try:
        return required_string(name)
    except ConfigurationError:
        raise ConfigurationError('AMAP_MAPS_API_KEY / AMAP_MAP_KEY: 必须填写一个有效地图密钥，不能使用模板占位符') from None


def wire_api() -> str:
    value = required_string('OPENAI_WIRE_API').lower()
    if value not in {'responses', 'chat_completions'}:
        raise ConfigurationError('OPENAI_WIRE_API: 必须为 responses 或 chat_completions')
    return value


def store_responses() -> bool:
    value = required_string('OPENAI_STORE_RESPONSES').lower()
    if value not in {'true', 'false'}:
        raise ConfigurationError('OPENAI_STORE_RESPONSES: 必须为 true 或 false')
    return value == 'true'


# 按启动用途汇总错误；验证不读取文件、不创建资源，也不因 pytest 而跳过。
def validate_configuration(scope: str = 'web') -> None:
    if scope not in {'model', 'system', 'web'}:
        raise ValueError('Unknown configuration scope')
    checks = [lambda: required_string('OPENAI_API_KEY'), model_base_url, model_name,
              model_timeout, wire_api, store_responses, fallback_model_name]
    if scope in {'system', 'web'}:
        checks += [database_path, lambda: boolean('TRAVEL_PERSISTENCE'),
                   lambda: boolean('TRAVEL_DISPLAY_ENABLED'), amap_api_key, amap_url, amap_timeout]
        if os.getenv('TRAVEL_DISPLAY_ENABLED', '').strip().lower() in _TRUE:
            checks.append(display_timeout)
    if scope == 'web':
        checks.append(lambda: boolean('TRAVEL_COOKIE_SECURE'))
    errors = []
    for check in checks:
        try:
            check()
        except ConfigurationError as exc:
            errors.append(str(exc))
    if errors:
        raise ConfigurationError('启动配置缺失或无效：\n- ' + '\n- '.join(errors)) from None


# 执行开关与无网络测试保护不是部署配置校验的替代品。
def _llm_configured() -> bool:
    disabled = os.getenv('TRAVEL_AGENT_DISABLE_LLM', '').strip().lower()
    return disabled not in _TRUE and bool(os.getenv('OPENAI_API_KEY', '').strip())


def llm_enabled() -> bool:
    """Whether real model access is allowed, including both offline-test guards."""
    return (_llm_configured() and not os.getenv('PYTEST_CURRENT_TEST')
            and 'pytest' not in sys.modules)
