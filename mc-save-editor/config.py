"""读取 ``mc-save-editor/config.toml`` 的轻量配置。

配置文件随源码一起提供，当前包含 GUI 语言选项::

    language = "zh_cn"

设计要点:
  - 只依赖 Python 标准库。Python 3.11+ 使用 :mod:`tomllib`；
    Python 3.10 使用内置的极简 TOML 解析，覆盖随附配置文件所需的
    顶层 ``key = "value"`` 语法。
  - 读取失败（文件缺失、无法读取、TOML 语法错误等）绝不抛异常，
    调用方据此回退到默认行为。
  - 本模块只读取界面配置，不接触任何 NBT/存档数据。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"

try:  # pragma: no cover - 取决于解释器版本
    import tomllib  # type: ignore[import-not-found]
except ImportError:  # Python 3.10 及更早版本没有 tomllib
    tomllib = None  # type: ignore[assignment]

_SIMPLE_LINE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*=\s*(.*?)\s*$")


def _strip_comment(line: str) -> str:
    """去掉行内 ``#`` 注释（忽略字符串内部的 ``#``）。"""
    in_string = False
    quote = ""
    for index, char in enumerate(line):
        if in_string:
            if char == quote:
                in_string = False
        elif char in ('"', "'"):
            in_string = True
            quote = char
        elif char == "#":
            return line[:index]
    return line


def _parse_scalar(raw: str) -> Any:
    """解析简单 TOML 标量（字符串/布尔/整数/浮点）。"""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def _parse_simple_toml(text: str) -> dict[str, Any]:
    """解析随附配置文件所需的 TOML 子集（顶层 ``key = value``）。"""
    data: dict[str, Any] = {}
    for line in text.splitlines():
        stripped = _strip_comment(line).strip()
        if not stripped or stripped.startswith("["):
            continue
        match = _SIMPLE_LINE.match(stripped)
        if match:
            data[match.group(1)] = _parse_scalar(match.group(2))
    return data


def load_config(path: Optional[Path] = None) -> dict[str, Any]:
    """读取并解析 TOML 配置；任何失败都返回空字典。"""
    try:
        config_path = Path(path) if path is not None else CONFIG_PATH
    except (TypeError, ValueError):
        return {}
    try:
        text = config_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return {}
    try:
        if tomllib is not None:
            data = tomllib.loads(text)
        else:  # pragma: no cover - Python 3.10 及更早版本
            data = _parse_simple_toml(text)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def load_language(path: Optional[Path] = None) -> Optional[str]:
    """从配置读取 ``language`` 原始值；缺失或类型不对时返回 ``None``。"""
    data = load_config(path)
    value = data.get("language")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


__all__ = ["CONFIG_PATH", "load_config", "load_language"]
