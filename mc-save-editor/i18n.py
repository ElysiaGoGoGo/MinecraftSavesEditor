"""轻量 GUI 国际化（i18n）支持。

设计要点:
  - 只依赖 Python 标准库；翻译文件为 ``assets/lang/*.json``（UTF-8）。
  - 启动时按以下优先级确定界面语言:
      1. 环境变量 ``MCS_LANG``（``zh_cn`` / ``en_us``，显式覆盖，便于测试）；
      2. ``mc-save-editor/config.toml`` 中的 ``language``；
      3. 配置缺失/无法解析/语言无效时回退到英文 ``en_us``。
  - :func:`tr` 支持 ``{name}`` 命名占位符；缺键时安全回退:
    当前语言 -> 另一种语言 -> 传入的 default -> key。
    翻译文件缺失/损坏/占位符不匹配都不会抛异常。
  - 本模块只负责界面显示文本，不改变任何 NBT/存档数据。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .config import CONFIG_PATH, load_language

DEFAULT_LANGUAGE = "zh_cn"
FALLBACK_LANGUAGE = "en_us"
SUPPORTED_LANGUAGES = (DEFAULT_LANGUAGE, FALLBACK_LANGUAGE)

LANG_DIR = Path(__file__).resolve().parent / "assets" / "lang"

_ALIASES = {
    "zh": "zh_cn",
    "zh_cn": "zh_cn",
    "zh-hans": "zh_cn",
    "zh_hans": "zh_cn",
    "cn": "zh_cn",
    "en": "en_us",
    "en_us": "en_us",
    "en-us": "en_us",
    "en_gb": "en_us",
    "en-gb": "en_us",
}

_catalogs: dict[str, dict[str, str]] = {}
_language = DEFAULT_LANGUAGE


def _canonical_language(value: Any) -> Optional[str]:
    """把输入规范化成受支持的语言代码；无效时返回 ``None``。"""
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    language = _ALIASES.get(raw, raw)
    return language if language in SUPPORTED_LANGUAGES else None


def normalize_language(value: Any) -> str:
    """把 ``MCS_LANG`` 之类的输入规范成受支持的语言代码。"""
    return _canonical_language(value) or DEFAULT_LANGUAGE


def _load_catalog(language: str) -> dict[str, str]:
    """读取并缓存某语言的翻译表；文件缺失或损坏时返回空表。"""
    if language in _catalogs:
        return _catalogs[language]
    catalog: dict[str, str] = {}
    path = LANG_DIR / f"{language}.json"
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(key, str) and isinstance(value, str):
                catalog[key] = value
    _catalogs[language] = catalog
    return catalog


def _initial_language() -> str:
    """启动时确定语言：环境变量 -> config.toml -> 英文回退。"""
    try:
        env_language = _canonical_language(os.environ.get("MCS_LANG"))
    except Exception:  # noqa: BLE001 - 极端环境下也不应崩溃
        env_language = None
    if env_language is not None:
        return env_language
    try:
        config_language = _canonical_language(load_language())
    except Exception:  # noqa: BLE001 - 配置读取失败时回退
        config_language = None
    return config_language or FALLBACK_LANGUAGE


_language = _initial_language()


def reload_language() -> str:
    """重新按启动优先级读取语言（环境变量/配置文件）；返回生效语言。"""
    global _language
    _language = _initial_language()
    return _language


def _fallback_language() -> str:
    return FALLBACK_LANGUAGE if _language != FALLBACK_LANGUAGE else DEFAULT_LANGUAGE


def get_language() -> str:
    """返回当前语言代码（``zh_cn`` 或 ``en_us``）。"""
    return _language


def set_language(language: Any) -> str:
    """切换当前语言（主要用于测试）；返回生效后的语言代码。"""
    global _language
    _language = normalize_language(language)
    return _language


def has(key: Any) -> bool:
    """判断某个翻译键在当前语言或回退语言中是否存在。"""
    text_key = str(key)
    return (
        text_key in _load_catalog(_language)
        or text_key in _load_catalog(_fallback_language())
    )


def tr(key: Any, default: Optional[str] = None, **kwargs: Any) -> str:
    """翻译 ``key``，并用命名占位符 ``kwargs`` 格式化。

    缺键时的回退顺序: 当前语言 -> 另一种语言 -> ``default`` -> key 本身。
    格式化失败（缺参数、花括号不匹配等）时原样返回文本，绝不抛异常。
    """
    text_key = str(key)
    text: Optional[str] = _load_catalog(_language).get(text_key)
    if text is None:
        text = _load_catalog(_fallback_language()).get(text_key)
    if text is None:
        text = default if default is not None else text_key
    if not isinstance(text, str):
        text = str(text)
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError, AttributeError):
            return text
    return text


__all__ = [
    "CONFIG_PATH",
    "DEFAULT_LANGUAGE",
    "FALLBACK_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "LANG_DIR",
    "get_language",
    "has",
    "normalize_language",
    "reload_language",
    "set_language",
    "tr",
]
