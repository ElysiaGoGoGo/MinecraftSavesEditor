"""Curios（NeoForge 饰品栏模组）玩家数据读取/修改。

Curios 把饰品栏数据保存在玩家 NBT 的 NeoForge attachments 中：

    neoforge:attachments
      curios:inventory
        Curios: [                         # 每种饰品槽位一个 compound
          {
            Identifier: "ring",           # 槽位类型/标识
            StacksHandler: {
              SavedBaseSize: 2,
              Visible: 1b,
              HasCosmetic: 0b,
              DropRule: "DEFAULT",
              Stacks: {
                Size: 2,                  # 槽位数量
                Items: [                  # 已装备物品（可能稀疏，靠 Slot 定位）
                  { Slot: 0, id: "minecraft:diamond", count: 1, components: {...} }
                ]
              },
              Cosmetics: { Size: 2, Items: [] },
              ...渲染/激活状态等字段...
            }
          },
          ...
        ]

本模块只修改 `StacksHandler.Stacks.Items`，保留其它所有字段（渲染、Cosmetics、
DropRule、附件里的其它模组数据等），因此对存档其余部分无损。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .items import (
    item_count,
    item_extra,
    item_id,
    make_item,
    update_item_fields,
)
from .nbt import Byte, CompoundTag, Int, ListTag, TAG_COMPOUND

ATTACHMENTS_KEY = "neoforge:attachments"
CURIOS_INVENTORY_KEY = "curios:inventory"
CURIOS_LIST_KEY = "Curios"
STACKS_HANDLER_KEY = "StacksHandler"
STACKS_KEY = "Stacks"
ITEMS_KEY = "Items"
COSMETICS_KEY = "Cosmetics"
SIZE_KEY = "Size"
IDENTIFIER_KEY = "Identifier"

# 版本元数据（来自工作区 Curios-1.21.1/gradle.properties 与源码）
CURIO_MOD_VERSION = "9.5.1+1.21.1"
CURIO_MC_VERSION = "1.21.1"
CURIO_NEOFORGE_VERSION = "21.1.60+（范围 [21.1.60,)）"
CURIO_VERSION_WARNING = (
    "版本警告：Curios / Minecraft / NeoForge 加载器任一版本偏差都可能导致未定义行为。"
    "本页面按上述版本的数据结构实现；修改前请先备份世界。"
)
CURIO_VALIDATION_WARNING = (
    "Curios 在加载时会校验物品是否适用于对应槽位；放入不匹配的物品可能被游戏丢弃。"
)

# 常见 Curios 槽位标识的中文显示名；未知标识直接显示原 id
CURIO_SLOT_LABELS: dict[str, str] = {
    "head": "头饰",
    "necklace": "项链",
    "ring": "戒指",
    "belt": "腰带",
    "charm": "护符",
    "back": "背部",
    "body": "身体",
    "hands": "手部",
    "curio": "通用饰品",
    "spellbook": "法术书",
    "brooch": "胸针",
    "rune": "符文",
    "geas": "誓约",
    "an_focus": "专注",
    "magnetizer": "磁化器",
    "adv_pattern_encoder": "高级样板编码器",
    "overloaded_frequency_card": "过载频率卡",
}


def slot_display_name(identifier: str) -> str:
    return CURIO_SLOT_LABELS.get(identifier, identifier)


@dataclass
class CurioGroup:
    index: int
    identifier: str
    size: int
    items: dict[int, CompoundTag]
    cosmetics: dict[int, CompoundTag]
    group_tag: CompoundTag
    handler_tag: CompoundTag

    def __repr__(self) -> str:
        return (
            f"CurioGroup({self.identifier!r}, size={self.size}, "
            f"equipped={sorted(self.items)})"
        )


def curios_inventory(player) -> Optional[CompoundTag]:
    """返回玩家 NBT 中的 curios:inventory compound；没有 Curios 数据时返回 None。"""
    tag = getattr(player, "tag", None)
    if not isinstance(tag, CompoundTag):
        return None
    attachments = tag.get(ATTACHMENTS_KEY)
    if not isinstance(attachments, CompoundTag):
        return None
    inventory = attachments.get(CURIOS_INVENTORY_KEY)
    return inventory if isinstance(inventory, CompoundTag) else None


def has_curios(player) -> bool:
    inventory = curios_inventory(player)
    return isinstance(inventory.get(CURIOS_LIST_KEY), ListTag) if inventory else False


def _item_slot(item: CompoundTag) -> Optional[int]:
    raw = item.get("Slot")
    if isinstance(raw, int):
        return int(raw) & 0xFF
    return None


def _read_items(container: Optional[CompoundTag]) -> dict[int, CompoundTag]:
    result: dict[int, CompoundTag] = {}
    if not isinstance(container, CompoundTag):
        return result
    items = container.get(ITEMS_KEY)
    if not isinstance(items, ListTag):
        return result
    for item in items:
        if not isinstance(item, CompoundTag):
            continue
        slot = _item_slot(item)
        if slot is not None:
            result[slot] = item
    return result


def _write_items(container: CompoundTag, slot_to_item: dict[int, CompoundTag]) -> None:
    """把 slot->item 写回容器的 Items 列表，并对 Slot 排序。"""
    items = container.get(ITEMS_KEY)
    if not isinstance(items, ListTag):
        items = ListTag([], TAG_COMPOUND)
    items.clear()
    for slot in sorted(slot_to_item):
        item = slot_to_item[slot]
        item["Slot"] = Byte(slot)
        items.append(item)
    container[ITEMS_KEY] = items


def list_groups(player) -> list[CurioGroup]:
    """列出玩家所有 Curios 槽位组（含空槽位组）。"""
    inventory = curios_inventory(player)
    if inventory is None:
        return []
    raw_groups = inventory.get(CURIOS_LIST_KEY)
    if not isinstance(raw_groups, ListTag):
        return []
    groups: list[CurioGroup] = []
    for index, group in enumerate(raw_groups):
        if not isinstance(group, CompoundTag):
            continue
        handler = group.get(STACKS_HANDLER_KEY)
        if not isinstance(handler, CompoundTag):
            continue
        stacks = handler.get(STACKS_KEY)
        size = 0
        if isinstance(stacks, CompoundTag):
            size = stacks.get_int(SIZE_KEY, 0)
        if size < 0:
            size = 0
        groups.append(
            CurioGroup(
                index=index,
                identifier=group.get_string(IDENTIFIER_KEY),
                size=size,
                items=_read_items(stacks),
                cosmetics=_read_items(handler.get(COSMETICS_KEY)),
                group_tag=group,
                handler_tag=handler,
            )
        )
    return groups


def find_group(player, identifier: str) -> Optional[CurioGroup]:
    wanted = str(identifier).strip()
    for group in list_groups(player):
        if group.identifier == wanted:
            return group
    return None


def make_curio_item(
    item_id_value: str,
    count: int,
    extra: Optional[CompoundTag],
    item_format: str,
    slot: int,
) -> CompoundTag:
    """创建 Curios 槽位物品（物品格式与当前处理版本一致）。"""
    return make_item(item_id_value, count, extra, item_format, slot=slot)


def get_curio_item(player, identifier: str, slot: int) -> Optional[CompoundTag]:
    group = find_group(player, identifier)
    if group is None:
        return None
    return group.items.get(int(slot))


def set_curio_item(
    player,
    identifier: str,
    slot: int,
    item: CompoundTag,
    item_format: str = "components",
) -> None:
    """写入/替换某个 Curios 槽位的物品，保留组内其它字段。"""
    group = find_group(player, identifier)
    if group is None:
        raise ValueError(f"玩家没有 Curios 槽位组：{identifier!r}")
    slot = int(slot)
    if not 0 <= slot < group.size:
        raise ValueError(
            f"Curios 槽位 {slot} 超出范围（{identifier!r} 的 Size={group.size}）"
        )
    if not isinstance(item, CompoundTag):
        raise ValueError("Curios 物品必须是 TAG_Compound")
    if not isinstance(item.get("id"), str):
        raise ValueError("Curios 物品缺少字符串字段 id")
    stacks = group.handler_tag.get(STACKS_KEY)
    if not isinstance(stacks, CompoundTag):
        stacks = CompoundTag()
        stacks[SIZE_KEY] = Int(group.size)
        group.handler_tag[STACKS_KEY] = stacks
    slot_items = dict(group.items)
    slot_items[slot] = item
    _write_items(stacks, slot_items)
    group.items = slot_items


def delete_curio_item(player, identifier: str, slot: int) -> bool:
    """删除某个 Curios 槽位物品；返回是否原本存在。"""
    group = find_group(player, identifier)
    if group is None:
        return False
    slot = int(slot)
    if slot not in group.items:
        return False
    stacks = group.handler_tag.get(STACKS_KEY)
    if not isinstance(stacks, CompoundTag):
        return False
    slot_items = dict(group.items)
    del slot_items[slot]
    _write_items(stacks, slot_items)
    group.items = slot_items
    return True


def update_curio_item(
    player,
    identifier: str,
    slot: int,
    item_id_value: str,
    count: int,
    extra: Optional[CompoundTag],
    item_format: str,
) -> CompoundTag:
    """修改已有 Curios 槽位物品的 id/count/额外 NBT，保留未知字段。"""
    group = find_group(player, identifier)
    if group is None:
        raise ValueError(f"玩家没有 Curios 槽位组：{identifier!r}")
    existing = group.items.get(int(slot))
    item = update_item_fields(existing, item_id_value, count, extra, item_format)
    set_curio_item(player, identifier, slot, item, item_format)
    return item
