"""Minecraft 版本识别与“受支持版本”选择。

背景
----
存档格式随游戏版本变化（区块调色板、物品 NBT 结构、DataVersion、level.dat 字段等）。
本模块负责:

1. 从 ``level.dat`` 自动识别存档的游戏版本（``Data.Version.Name`` 与 ``Data.DataVersion``）;
2. 维护“本程序当前支持的版本”注册表（:data:`SUPPORTED_VERSIONS`）;
3. 若检测到的版本受支持 -> 自动使用对应 :class:`VersionProfile` 的处理逻辑;
4. 若不受支持 -> 抛出 :class:`UnsupportedVersionError`，其中带有:
     - 检测到的版本信息
     - 受支持版本列表
     - **按版本号最近**推导出的默认选择（供 GUI 下拉框默认值使用）
5. 也允许用户显式指定一个受支持版本（GUI 版本选择对话框 / 核心库 ``minecraft_version`` 参数）。

重要原则
--------
- 选择“受支持版本的处理逻辑”不会改写存档的 DataVersion / Version；检测到的版本信息始终保留并展示。
- 用 A 版本的处理逻辑去操作 B 版本的存档属于“尽力兼容”，会有明确警告并要求自动备份。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from .nbt import NbtError, read_nbt_file

__all__ = [
    "VersionProfile",
    "DetectedVersion",
    "VersionError",
    "UnsupportedVersionError",
    "InvalidVersionChoice",
    "SUPPORTED_VERSIONS",
    "SUPPORTED_IDS",
    "detect_world_version",
    "detect_level_version",
    "parse_version_number",
    "nearest_supported_version",
    "find_supported_version",
    "resolve_version",
    "supported_versions_summary",
]


class VersionError(Exception):
    """版本识别/选择失败。"""


@dataclass(frozen=True)
class VersionProfile:
    """一个受支持版本对应的一组格式处理规则。"""

    id: str
    data_version: int
    item_format: str          # "components" (1.20.5+) 或 "tag" (旧版)
    status: str               # "validated" = 已做真实服务端端到端验证; "compatible" = 格式兼容
    notes: str = ""

    @property
    def status_label(self) -> str:
        return {"validated": "已验证", "compatible": "兼容"}.get(self.status, self.status)

    @property
    def choice_label(self) -> str:
        return f"{self.id}（{self.status_label}，DataVersion={self.data_version}）"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "data_version": self.data_version,
            "item_format": self.item_format,
            "status": self.status,
            "status_label": self.status_label,
            "notes": self.notes,
        }


# --------------------------------------------------------------------------- #
# 受支持版本注册表
# --------------------------------------------------------------------------- #
# 说明:
#   - 1.21.1 是本项目完整实现并做过“官方服务端生成/加载 + RCON”端到端验证的版本;
#   - 1.20.1 与 1.21.1 同属 1.18+ 调色板区块格式, 本工具用到的高层操作
#     (level.dat 字段 / 方块与方块实体原始 NBT / playerdata 数值字段) 结构一致,
#     差别主要在物品 NBT 形式 (1.20.1 用 tag, 1.20.5+ 用 components)。
#     物品原文一律按未知 tag 原样保留, 因此这里标记为“兼容”。
#   - 新增版本: 往 SUPPORTED_VERSIONS 增加 VersionProfile 即可, 选择/最近匹配逻辑自动生效。
SUPPORTED_VERSIONS: dict[str, VersionProfile] = {
    "1.20.1": VersionProfile(
        id="1.20.1",
        data_version=3465,
        item_format="tag",
        status="compatible",
        notes="1.18+ 调色板区块格式；物品为 tag 形式；高层字段与 1.21.1 一致。",
    ),
    "1.21.1": VersionProfile(
        id="1.21.1",
        data_version=3955,
        item_format="components",
        status="validated",
        notes="本项目主目标版本；已通过官方 1.21.1 服务端生成/加载与 RCON 端到端验证。",
    ),
}
SUPPORTED_IDS: tuple[str, ...] = tuple(SUPPORTED_VERSIONS.keys())


# --------------------------------------------------------------------------- #
# 版本号解析
# --------------------------------------------------------------------------- #
_VERSION_RE = re.compile(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version_number(name: Optional[str]) -> Optional[tuple[int, int, int]]:
    """把 '1.21.1' / '1.20' / '1.21.1-pre1' 解析为 (major, minor, patch)。

    快照名（如 '24w14a'）无法按此解析时返回 None。
    """
    if not name:
        return None
    match = _VERSION_RE.match(name)
    if not match:
        return None
    major = int(match.group(1))
    minor = int(match.group(2))
    patch = int(match.group(3)) if match.group(3) is not None else 0
    return major, minor, patch


def _version_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    """版本号“距离”，用于挑选最近的受支持版本。"""
    return abs(a[0] - b[0]) * 1_000_000 + abs(a[1] - b[1]) * 1_000 + abs(a[2] - b[2])


# --------------------------------------------------------------------------- #
# 版本检测
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class DetectedVersion:
    """从 level.dat 检测到的版本信息。"""

    name: Optional[str]
    data_version: Optional[int]
    series: Optional[str] = None
    snapshot: bool = False

    @property
    def number(self) -> Optional[tuple[int, int, int]]:
        return parse_version_number(self.name)

    @property
    def display(self) -> str:
        parts: list[str] = []
        if self.name:
            parts.append(str(self.name))
        if self.data_version is not None and self.data_version >= 0:
            parts.append(f"DataVersion={self.data_version}")
        if self.snapshot:
            parts.append("快照")
        return "，".join(parts) if parts else "未知版本"

    def matches(self, profile: VersionProfile) -> bool:
        if self.data_version is not None and self.data_version == profile.data_version:
            return True
        return bool(self.name) and self.name.strip() == profile.id

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "data_version": self.data_version,
            "series": self.series,
            "snapshot": self.snapshot,
            "display": self.display,
        }


def detect_level_version(level) -> DetectedVersion:
    """从已加载的 :class:`mctool.level.LevelData` 检测版本。"""
    data = getattr(level, "data", None)
    if data is None:
        raise VersionError("无法从 level.dat 检测版本：缺少 Data compound")
    version = data.get_compound("Version") if hasattr(data, "get_compound") else None
    name = None
    series = None
    snapshot = False
    if version is not None:
        raw_name = version.get("Name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
        raw_series = version.get("Series")
        if isinstance(raw_series, str) and raw_series.strip():
            series = raw_series.strip()
        snapshot = version.get_boolean("Snapshot") if hasattr(version, "get_boolean") else False
    data_version = data.get_int("DataVersion", -1) if hasattr(data, "get_int") else -1
    if data_version < 0:
        data_version = None  # type: ignore[assignment]
    if not name and data_version is None:
        raise VersionError("level.dat 中既没有 Version.Name 也没有 DataVersion，无法识别游戏版本")
    return DetectedVersion(
        name=name,
        data_version=data_version,
        series=series,
        snapshot=bool(snapshot),
    )


def detect_world_version(world_dir: str) -> DetectedVersion:
    """只读取 level.dat，检测世界版本；不获取 session.lock。"""
    import os

    level_path = os.path.join(world_dir, "level.dat")
    if not os.path.isfile(level_path):
        raise VersionError(f"不是有效的世界目录（缺少 level.dat）: {world_dir}")
    try:
        _name, tag = read_nbt_file(level_path)
    except (OSError, NbtError) as exc:
        raise VersionError(f"读取 level.dat 失败: {exc}") from exc
    if tag is None or not hasattr(tag, "get_compound"):
        raise VersionError("level.dat 根节点不是 TAG_Compound")
    data = tag.get_compound("Data")
    if not data:
        raise VersionError("level.dat 缺少 Data compound")
    version = data.get_compound("Version")
    name = None
    series = None
    snapshot = False
    if version:
        raw_name = version.get("Name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
        raw_series = version.get("Series")
        if isinstance(raw_series, str) and raw_series.strip():
            series = raw_series.strip()
        snapshot = version.get_boolean("Snapshot")
    data_version = data.get_int("DataVersion", -1)
    if data_version < 0:
        data_version = None  # type: ignore[assignment]
    if not name and data_version is None:
        raise VersionError("level.dat 中既没有 Version.Name 也没有 DataVersion，无法识别游戏版本")
    return DetectedVersion(
        name=name,
        data_version=data_version,
        series=series,
        snapshot=bool(snapshot),
    )


# --------------------------------------------------------------------------- #
# 受支持版本选择
# --------------------------------------------------------------------------- #
def find_supported_version(detected: DetectedVersion) -> Optional[VersionProfile]:
    """检测版本恰好受支持时返回对应 profile，否则返回 None。"""
    for profile in SUPPORTED_VERSIONS.values():
        if detected.matches(profile):
            return profile
    return None


def nearest_supported_version(detected: DetectedVersion) -> VersionProfile:
    """按“版本号最近”挑选默认的受支持版本。

    规则:
      - 若能解析检测到的 Version.Name（如 1.21.4）-> 比较版本号距离;
      - 否则若只有 DataVersion -> 按 DataVersion 数值距离;
      - 两者都没有 -> 默认取最新受支持版本（列表按版本号升序，取最后一个）。
    """
    profiles = list(SUPPORTED_VERSIONS.values())
    if not profiles:
        raise VersionError("程序没有任何受支持的 Minecraft 版本配置")
    number = detected.number
    if number is not None:
        def key(p: VersionProfile):
            pnum = parse_version_number(p.id)
            if pnum is None:
                return (1 << 60, 0)
            return (_version_distance(number, pnum), _version_distance(number, pnum))
        return min(profiles, key=key)
    if detected.data_version is not None:
        return min(
            profiles,
            key=lambda p: (abs(detected.data_version - p.data_version), p.data_version),
        )
    return profiles[-1]


class UnsupportedVersionError(VersionError):
    """检测到的版本不在受支持列表中，需要用户指定一个受支持版本。"""

    def __init__(
        self,
        detected: DetectedVersion,
        supported: list[VersionProfile],
        nearest: VersionProfile,
        reason: str = "",
    ):
        self.detected = detected
        self.supported = supported
        self.nearest = nearest
        self.reason = reason
        super().__init__(self._format_message())

    def _format_message(self) -> str:
        supported_text = "、".join(
            f"{p.id}（{p.status_label}，DataVersion={p.data_version}）" for p in self.supported
        )
        lines = [
            f"检测到不受支持的 Minecraft 版本：{self.detected.display}",
            f"本程序当前支持的版本：{supported_text}",
            f"建议默认选择版本号最近的受支持版本：{self.nearest.id}",
        ]
        if self.reason:
            lines.append(f"原因：{self.reason}")
        lines.append(
            "请选择一个受支持版本继续。注意：用与存档版本不一致的处理逻辑读写可能有风险，"
            "工具会在写入前自动备份。"
        )
        return "\n".join(lines)


class InvalidVersionChoice(VersionError):
    """用户显式指定了一个不在受支持列表中的版本。"""

    def __init__(self, requested: str, supported: list[VersionProfile]):
        self.requested = requested
        self.supported = supported
        super().__init__(
            f"不支持的版本选择：{requested!r}。"
            f"可选：{'、'.join(p.id for p in supported)}。"
        )


def resolve_version(
    detected: DetectedVersion,
    requested: Optional[str] = None,
) -> tuple[VersionProfile, bool]:
    """确定使用哪个 :class:`VersionProfile`。

    返回 ``(profile, auto)``：
      - ``auto=True`` 表示检测版本恰好受支持，自动选用;
      - ``auto=False`` 表示用户显式指定（或检测不受支持后指定）。
    """
    if requested is not None and str(requested).strip():
        text = str(requested).strip()
        if text in SUPPORTED_VERSIONS:
            return SUPPORTED_VERSIONS[text], False
        # 允许 "1.21.1（已验证...）" 这类从 GUI 下拉框带出的标签
        head = text.split("（", 1)[0].strip()
        if head in SUPPORTED_VERSIONS:
            return SUPPORTED_VERSIONS[head], False
        raise InvalidVersionChoice(text, list(SUPPORTED_VERSIONS.values()))

    profile = find_supported_version(detected)
    if profile is not None:
        return profile, True
    nearest = nearest_supported_version(detected)
    raise UnsupportedVersionError(
        detected=detected,
        supported=list(SUPPORTED_VERSIONS.values()),
        nearest=nearest,
    )


def supported_versions_summary() -> list[dict]:
    """给 GUI/JSON/核心库使用的受支持版本摘要。"""
    return [p.to_dict() for p in SUPPORTED_VERSIONS.values()]
