"""玩家物品栏 (Inventory) 的 ItemStack NBT 帮助函数。

Minecraft 1.20.1 与 1.21.1 的物品 NBT 结构不同:
  1.20.1  : { id: "minecraft:diamond", Count: 5b, tag: {...} }
  1.20.5+ : { id: "minecraft:diamond", count: 5, components: {...} }

背包列表中的每个物品额外带 Slot 字节（见 PlayerData/Inventory）：
  0..8    快捷栏 1..9
  9..35   主背包
  100..103 盔甲（100=靴子, 101=护腿, 102=胸甲, 103=头盔）
  150     副手

本模块只处理物品条目的字段，不依赖 World/GUI，方便核心测试与复用。
"""

from __future__ import annotations

import re
from typing import Optional

from .nbt import Byte, CompoundTag, Int

INVENTORY_SLOTS: tuple[int, ...] = tuple(range(0, 36)) + (100, 101, 102, 103, 150)
ARMOR_LABELS: dict[int, str] = {100: "靴子", 101: "护腿", 102: "胸甲", 103: "头盔"}

_ITEM_ID_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")


def normalize_item_id(text: str) -> str:
    """校验并补全物品注册名，如 'diamond' -> 'minecraft:diamond'。"""
    value = str(text or "").strip()
    if not value:
        raise ValueError("物品 id 不能为空（例如 minecraft:diamond）")
    if ":" not in value:
        value = "minecraft:" + value
    if not _ITEM_ID_RE.match(value):
        raise ValueError(
            f"物品 id 格式不合法：{value!r}（应类似 minecraft:diamond）"
        )
    return value


def is_tag_format(item_format: str) -> bool:
    return item_format == "tag"


def count_key(item_format: str) -> str:
    return "Count" if is_tag_format(item_format) else "count"


def extra_key(item_format: str) -> str:
    return "tag" if is_tag_format(item_format) else "components"


def item_id(tag: CompoundTag) -> str:
    value = tag.get("id")
    return value if isinstance(value, str) else ""


def item_count(tag: CompoundTag, item_format: str) -> int:
    """读取物品数量；兼容两种字段名，缺省为 1。"""
    preferred = count_key(item_format)
    for key in (preferred, "count", "Count"):
        value = tag.get(key)
        if isinstance(value, int):
            return int(value)
    return 1


def item_extra(tag: CompoundTag, item_format: str) -> Optional[CompoundTag]:
    value = tag.get(extra_key(item_format))
    return value if isinstance(value, CompoundTag) else None


def _coerce_extra(extra: Optional[CompoundTag]) -> Optional[CompoundTag]:
    if extra is None:
        return None
    if isinstance(extra, CompoundTag):
        return extra.copy()
    if isinstance(extra, dict):
        return CompoundTag(extra).copy()
    raise ValueError("额外 NBT 必须是 CompoundTag 或 dict")


def make_item(
    item_id_value: str,
    count: int,
    extra: Optional[CompoundTag] = None,
    item_format: str = "components",
    slot: Optional[int] = None,
) -> CompoundTag:
    """创建一个符合指定版本格式的物品条目。"""
    item_id_value = normalize_item_id(item_id_value)
    count = _validate_count(count)
    extra = _coerce_extra(extra)
    tag = CompoundTag()
    tag["id"] = item_id_value
    if is_tag_format(item_format):
        tag["Count"] = Byte(count)
    else:
        tag["count"] = Int(count)
    if extra is not None and len(extra) > 0:
        tag[extra_key(item_format)] = extra
    if slot is not None:
        tag["Slot"] = Byte(slot)
    return tag


def update_item_fields(
    existing: Optional[CompoundTag],
    item_id_value: str,
    count: int,
    extra: Optional[CompoundTag],
    item_format: str,
) -> CompoundTag:
    """在已有物品条目上修改 id/count/额外 NBT，保留其它未知字段。"""
    item_id_value = normalize_item_id(item_id_value)
    count = _validate_count(count)
    extra = _coerce_extra(extra)
    tag = existing.copy() if isinstance(existing, CompoundTag) else CompoundTag()
    tag["id"] = item_id_value
    if is_tag_format(item_format):
        tag["Count"] = Byte(count)
        tag.pop("count", None)
    else:
        tag["count"] = Int(count)
        tag.pop("Count", None)
    key = extra_key(item_format)
    if extra is not None and len(extra) > 0:
        tag[key] = extra
    else:
        tag.pop(key, None)
    return tag


def _validate_count(count: int) -> int:
    try:
        value = int(count)
    except (TypeError, ValueError):
        raise ValueError(f"物品数量必须是整数：{count!r}") from None
    if not 1 <= value <= 99:
        raise ValueError(f"物品数量必须在 1..99（当前 {value}）")
    return value


def slot_label(slot: int) -> str:
    if 0 <= slot <= 8:
        return f"快捷栏 {slot + 1}"
    if 9 <= slot <= 35:
        return f"背包 {slot + 1}"
    if slot in ARMOR_LABELS:
        return ARMOR_LABELS[slot]
    if slot == 150:
        return "副手"
    return f"槽位 {slot}"


def describe_item(tag: CompoundTag, item_format: str) -> str:
    """简短描述：id x count (+额外 NBT 是否存在)。"""
    extra = item_extra(tag, item_format)
    suffix = " +额外NBT" if extra is not None and len(extra) else ""
    return f"{item_id(tag) or '?'} x{item_count(tag, item_format)}{suffix}"
