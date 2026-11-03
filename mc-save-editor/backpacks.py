"""Sophisticated Backpacks 背包内容（SavedData）读写。

数据位置（源码研究结论，见 docs/mod_research/sophisticated_backpacks.md）:
  <world>/data/sophisticatedbackpacks.dat
根结构:
  {DataVersion:int, data:{backpackContents:[{uuid:int[4], contents:{...}}], accessLogRecords:[...]}}
每个 contents 内的 inventory:
  {Size:int, Items:[{Slot:int, id, count, components} ...]}

背包 ItemStack 自身只保存 ``sophisticatedcore:storage_uuid`` 组件；实际内容
全部在本文件中。本模块只编辑 contents.inventory.Items，保留 contents / 根文件
里的其它字段（升级、设置、访问日志等）。
"""

from __future__ import annotations

import os
import uuid as uuidlib
from dataclasses import dataclass, field
from typing import Any, Optional

from .items import item_count, item_extra, item_id, make_item, update_item_fields
from .nbt import ByteArray, CompoundTag, Int, IntArray, ListTag, TAG_COMPOUND
from .world import WorldError

BACKPACK_DATA_PATH = "data/sophisticatedbackpacks.dat"
BACKPACK_RECOVERY_ITEM_ID = "sophisticatedbackpacks:backpack"
STORAGE_UUID_COMPONENT = "sophisticatedcore:storage_uuid"
DATA_KEY = "data"
CONTENTS_KEY = "backpackContents"
UUID_KEY = "uuid"
CONTENTS_TAG_KEY = "contents"
INVENTORY_KEY = "inventory"
SIZE_KEY = "Size"
ITEMS_KEY = "Items"
SLOT_KEY = "Slot"


class BackpackDataError(ValueError):
    """背包数据格式错误。"""


def _signed32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x100000000 if value >= 0x80000000 else value


def uuid_to_ints(value: str) -> IntArray:
    """标准 UUID 字符串 -> MC 的 int[4]（四个有符号 32 位整数）。"""
    parsed = uuidlib.UUID(str(value))
    return IntArray([
        _signed32(parsed.int >> 96),
        _signed32(parsed.int >> 64),
        _signed32(parsed.int >> 32),
        _signed32(parsed.int),
    ])


def ints_to_uuid(raw: Any) -> str:
    """MC 的 int[4] -> 标准 UUID 字符串；长度不为 4 时抛 BackpackDataError。"""
    try:
        values = [int(v) & 0xFFFFFFFF for v in raw]
    except (TypeError, ValueError) as exc:
        raise BackpackDataError(f"uuid 不是整数数组: {raw!r}") from exc
    if len(values) != 4:
        raise BackpackDataError(f"uuid 长度应为 4，实际 {len(values)}")
    number = (
        (values[0] << 96) | (values[1] << 64) | (values[2] << 32) | values[3]
    )
    return str(uuidlib.UUID(int=number))


def _read_items(contents: CompoundTag) -> dict[int, CompoundTag]:
    inventory = contents.get(INVENTORY_KEY)
    if not isinstance(inventory, CompoundTag):
        return {}
    items = inventory.get(ITEMS_KEY)
    if not isinstance(items, ListTag):
        return {}
    result: dict[int, CompoundTag] = {}
    for item in items:
        if not isinstance(item, CompoundTag):
            continue
        slot = item.get_int(SLOT_KEY, -1)
        if slot >= 0:
            result[slot] = item
    return result


def _inventory(contents: CompoundTag) -> CompoundTag:
    inventory = contents.get(INVENTORY_KEY)
    if not isinstance(inventory, CompoundTag):
        inventory = CompoundTag()
        inventory[SIZE_KEY] = Int(0)
        inventory[ITEMS_KEY] = ListTag([], TAG_COMPOUND)
        contents[INVENTORY_KEY] = inventory
    items = inventory.get(ITEMS_KEY)
    if not isinstance(items, ListTag):
        inventory[ITEMS_KEY] = ListTag([], TAG_COMPOUND)
    return inventory


@dataclass
class Backpack:
    """一个背包的内容条目（SavedData 中的一项）。"""

    uuid: str
    uuid_ints: IntArray
    entry: CompoundTag
    contents: CompoundTag

    @property
    def size(self) -> int:
        inventory = self.contents.get(INVENTORY_KEY)
        if not isinstance(inventory, CompoundTag):
            return 0
        return max(0, inventory.get_int(SIZE_KEY, 0))

    def item_map(self) -> dict[int, CompoundTag]:
        return _read_items(self.contents)

    def item_count(self) -> int:
        return len(self.item_map())

    def slot_items(self) -> list[tuple[int, Optional[CompoundTag]]]:
        """返回 0..Size-1 的 (槽位, 物品或 None)。"""
        items = self.item_map()
        return [(slot, items.get(slot)) for slot in range(self.size)]

    def summary(self, item_format: str = "components") -> dict[str, Any]:
        items = self.item_map()
        return {
            "uuid": self.uuid,
            "size": self.size,
            "items": len(items),
            "item_ids": [item_id(item) for item in items.values()],
            "item_format": item_format,
        }


@dataclass
class BackpackStore:
    """读取/保存一个世界的全部背包内容。"""

    world: Any
    root: Optional[CompoundTag] = None
    data: Optional[CompoundTag] = None
    backpacks: list[Backpack] = field(default_factory=list)

    @property
    def path(self) -> str:
        return BACKPACK_DATA_PATH

    def load(self) -> list[Backpack]:
        self.root = None
        self.data = None
        self.backpacks = []
        try:
            path = self.world.resolve_world_file(self.path)
        except Exception as exc:  # noqa: BLE001
            raise BackpackDataError(f"无法定位背包数据文件: {exc}") from exc
        if not os.path.isfile(path):
            return self.backpacks
        try:
            _name, root = self.world.read_dat(self.path)
        except WorldError as exc:
            raise BackpackDataError(f"读取背包数据失败: {exc}") from exc
        if not isinstance(root, CompoundTag):
            raise BackpackDataError("背包数据根 tag 不是 compound")
        self.root = root
        data = root.get(DATA_KEY)
        if not isinstance(data, CompoundTag):
            raise BackpackDataError(f"背包数据缺少 {DATA_KEY!r} compound")
        self.data = data
        raw_list = data.get(CONTENTS_KEY)
        if not isinstance(raw_list, ListTag):
            data[CONTENTS_KEY] = ListTag([], TAG_COMPOUND)
            raw_list = data[CONTENTS_KEY]
        for entry in raw_list:
            if not isinstance(entry, CompoundTag):
                continue
            raw_uuid = entry.get(UUID_KEY)
            if not isinstance(raw_uuid, (IntArray, list)):
                continue
            try:
                uuid_text = ints_to_uuid(raw_uuid)
            except BackpackDataError:
                # 保留损坏条目，但不在 GUI 中显示/编辑
                continue
            contents = entry.get(CONTENTS_TAG_KEY)
            if not isinstance(contents, CompoundTag):
                contents = CompoundTag()
                entry[CONTENTS_TAG_KEY] = contents
            self.backpacks.append(
                Backpack(
                    uuid=uuid_text,
                    uuid_ints=IntArray([int(v) for v in raw_uuid]),
                    entry=entry,
                    contents=contents,
                )
            )
        return self.backpacks

    def get(self, uuid: str) -> Optional[Backpack]:
        wanted = str(uuid)
        for backpack in self.backpacks:
            if backpack.uuid == wanted:
                return backpack
        return None

    def set_inventory_item(
        self,
        backpack: Backpack,
        slot: int,
        item: CompoundTag,
        item_format: str = "components",
    ) -> None:
        """写入/替换背包内某个槽位的物品，保留条目其它字段。"""
        if not isinstance(item, CompoundTag):
            raise BackpackDataError("物品必须是 TAG_Compound")
        if not isinstance(item.get("id"), str):
            raise BackpackDataError("物品缺少字符串字段 id")
        slot = int(slot)
        if not 0 <= slot < backpack.size:
            raise BackpackDataError(
                f"背包槽位 {slot} 超出范围（Size={backpack.size}）"
            )
        inventory = _inventory(backpack.contents)
        items = inventory.get(ITEMS_KEY)
        assert isinstance(items, ListTag)
        item[SLOT_KEY] = Int(slot)
        slot_map = {
            int(existing.get_int(SLOT_KEY, -1)): existing
            for existing in items
            if isinstance(existing, CompoundTag)
        }
        slot_map[slot] = item
        items.clear()
        for key in sorted(slot_map):
            items.append(slot_map[key])

    def delete_inventory_item(self, backpack: Backpack, slot: int) -> bool:
        slot = int(slot)
        inventory = _inventory(backpack.contents)
        items = inventory.get(ITEMS_KEY)
        assert isinstance(items, ListTag)
        kept: list[CompoundTag] = []
        removed = False
        for existing in items:
            if not isinstance(existing, CompoundTag):
                continue
            if existing.get_int(SLOT_KEY, -1) == slot:
                removed = True
            else:
                kept.append(existing)
        if removed:
            items.clear()
            items.extend(kept)
        return removed

    def save(self) -> list[str]:
        """写回文件；返回本次产生的备份路径。"""
        if self.root is None:
            raise BackpackDataError("没有可保存的背包数据（文件不存在或未加载）")
        before = len(getattr(self.world, "backups", []))
        self.world.write_dat(self.path, self.root)
        return list(getattr(self.world, "backups", [])[before:])


# --------------------------------------------------------------------------- #
# 对外辅助函数（GUI/脚本可用）
# --------------------------------------------------------------------------- #
def list_backpacks(world) -> BackpackStore:
    store = BackpackStore(world)
    store.load()
    return store


def get_backpack(world, uuid: str) -> Optional[Backpack]:
    return list_backpacks(world).get(uuid)


def item_description(item: CompoundTag, item_format: str) -> str:
    extra = item_extra(item, item_format)
    suffix = " +NBT" if extra is not None and len(extra) > 0 else ""
    return f"{item_id(item) or '?'} x{item_count(item, item_format)}{suffix}"


def make_recovery_item(backpack: Backpack, item_format: str = "components") -> CompoundTag:
    """创建一个只引用 backpack.uuid 真实内容的背包 ItemStack。

    物品本身不复制 contents；游戏会按 sophisticatedcore:storage_uuid 回查
    data/sophisticatedbackpacks.dat。该组件在 1.21.1 中使用 int[4] 编码。
    """
    if item_format != "components":
        raise BackpackDataError(
            "恢复背包物品只支持 1.21.1 的 components 物品格式"
        )
    raw_uuid = getattr(backpack, "uuid_ints", None)
    if not isinstance(raw_uuid, (IntArray, list)) or len(raw_uuid) != 4:
        raise BackpackDataError("背包 UUID 必须是 4 个 int")
    extra = CompoundTag()
    extra[STORAGE_UUID_COMPONENT] = IntArray([int(v) for v in raw_uuid])
    return make_item(BACKPACK_RECOVERY_ITEM_ID, 1, extra, "components")


def insert_recovery_item(
    world, player, backpack: Backpack, item_format: str = "components",
) -> int:
    """向玩家主物品栏（0..35）第一个空槽插入引用物品；返回槽位。

    物品栏满时不覆盖已有物品。写入由 world.flush() 落盘。
    """
    item = make_recovery_item(backpack, item_format)
    slot = next(
        (s for s in range(36) if player.get_inventory_item(s) is None),
        None,
    )
    if slot is None:
        raise BackpackDataError("目标玩家物品栏已满（0..35 无空槽位）")
    player.set_inventory_item(slot, item)
    world.mark_player_dirty(player)
    world.flush()
    return slot


__all__ = [
    "BACKPACK_DATA_PATH",
    "BACKPACK_RECOVERY_ITEM_ID",
    "STORAGE_UUID_COMPONENT",
    "Backpack",
    "BackpackDataError",
    "BackpackStore",
    "get_backpack",
    "insert_recovery_item",
    "ints_to_uuid",
    "item_description",
    "list_backpacks",
    "make_recovery_item",
    "uuid_to_ints",
]
