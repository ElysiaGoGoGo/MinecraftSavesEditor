"""玩家数据 (playerdata/<uuid>.dat) 读写。

对应 Minecraft 1.21.1 源码:
  mc_src/net/minecraft/world/level/storage/PlayerDataStorage.java
  mc_src/net/minecraft/world/entity/player/Player.java
  mc_src/net/minecraft/world/entity/Entity.java
  mc_src/net/minecraft/server/level/ServerPlayer.java
  mc_src/net/minecraft/world/food/FoodData.java
  mc_src/net/minecraft/world/entity/player/Inventory.java

文件: <world>/playerdata/<uuid>.dat
  - gzip 压缩 NBT
  - 根 tag 直接是玩家 compound, 没有 "Data" 包装层
  - 由 PlayerDataStorage.save 写入:
        CompoundTag t = player.saveWithoutId(new CompoundTag());
        NbtIo.writeCompressed(t, temp);  然后 level.dat 风格的 temp->正式 替换
  - 玩家保存根节点包含 DataVersion, Pos, Motion, Rotation, Health,
    foodLevel, foodSaturationLevel, XpLevel, XpP, XpTotal, playerGameType,
    Dimension, Inventory, EnderItems, abilities 等。

注意: 只有当玩家不在游戏中 (世界未被占用) 时本工具才能安全修改。
修改后的数据会在该玩家下次进入世界时由游戏读取。
"""

from __future__ import annotations

import os
import shutil
import uuid as uuidlib
from typing import Any, Optional

from .items import INVENTORY_SLOTS
from .nbt import (
    Byte,
    CompoundTag,
    Double,
    Float,
    Int,
    ListTag,
    NbtError,
    TAG_COMPOUND,
    TAG_DOUBLE,
    read_nbt_file,
    to_snbt,
    write_nbt,
)

GAME_TYPES = {0: "survival", 1: "creative", 2: "adventure", 3: "spectator"}
GAME_TYPE_IDS = {v: k for k, v in GAME_TYPES.items()}
DIMENSIONS = {
    "overworld": "minecraft:overworld",
    "nether": "minecraft:the_nether",
    "end": "minecraft:the_end",
}
DIMENSION_ALIASES = {v: k for k, v in DIMENSIONS.items()}


class PlayerDataError(NbtError):
    pass


class PlayerData:
    def __init__(self, path: str, tag: CompoundTag, player_uuid: str):
        self.path = path
        self.tag = tag
        self.uuid = player_uuid
        if not isinstance(tag, CompoundTag):
            raise PlayerDataError(f"玩家数据根 tag 不是 TAG_Compound: {path}")

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str) -> "PlayerData":
        stem = os.path.splitext(os.path.basename(path))[0]
        try:
            player_uuid = str(uuidlib.UUID(stem))
        except ValueError:
            player_uuid = stem
        _name, tag = read_nbt_file(path)
        return cls(path, tag, player_uuid)

    def save(self, path: Optional[str] = None) -> None:
        target = path or self.path
        raw = write_nbt(self.tag, "", "gzip")
        temp = target + ".mctool.tmp"
        with open(temp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        old = target + "_old"
        if os.path.exists(target):
            try:
                os.replace(target, old)
            except OSError:
                shutil.copy2(target, old)
        os.replace(temp, target)

    # ------------------------------------------------------------------ #
    # 摘要
    # ------------------------------------------------------------------ #
    def position(self) -> Optional[list[float]]:
        pos = self.tag.get("Pos")
        if isinstance(pos, ListTag) and len(pos) >= 3:
            try:
                return [float(pos[0]), float(pos[1]), float(pos[2])]
            except (TypeError, ValueError):
                return None
        return None

    def dimension(self) -> str:
        return self.tag.get_string("Dimension", "minecraft:overworld")

    def game_type(self) -> str:
        return GAME_TYPES.get(self.tag.get_int("playerGameType", 0), "unknown")

    def inventory_items(self) -> ListTag:
        inv = self.tag.get("Inventory")
        return inv if isinstance(inv, ListTag) else ListTag()

    def ender_items(self) -> ListTag:
        inv = self.tag.get("EnderItems")
        return inv if isinstance(inv, ListTag) else ListTag()

    # ------------------------------------------------------------------ #
    # 物品栏 (Inventory) 编辑
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_slot(slot: int) -> int:
        value = int(slot)
        if value not in INVENTORY_SLOTS:
            raise PlayerDataError(
                f"非法背包槽位 {value}；可用槽位: 0..35、100..103、150"
            )
        return value

    def inventory_slot_map(self) -> dict[int, CompoundTag]:
        """返回 {Slot: 物品 compound}（只含实际存在的物品）。"""
        result: dict[int, CompoundTag] = {}
        for item in self.inventory_items():
            slot = self._slot_from_item(item)
            if slot is not None:
                result[slot] = item
        return result

    @staticmethod
    def _slot_from_item(item: Any) -> Optional[int]:
        """读取物品的 Slot；Java byte 用 & 0xFF 还原（150 -> -106 的符号问题）。"""
        if not isinstance(item, CompoundTag):
            return None
        raw_slot = item.get("Slot")
        if isinstance(raw_slot, int):
            return int(raw_slot) & 0xFF
        return None

    def get_inventory_item(self, slot: int) -> Optional[CompoundTag]:
        slot = self._check_slot(slot)
        return self.inventory_slot_map().get(slot)

    def set_inventory_item(self, slot: int, item: CompoundTag) -> None:
        """写入/替换某个槽位的物品（自动写入 Slot 字节，保留物品其它字段）。"""
        slot = self._check_slot(slot)
        if not isinstance(item, CompoundTag):
            raise PlayerDataError("物品必须是 TAG_Compound")
        if not isinstance(item.get("id"), str):
            raise PlayerDataError("物品缺少字符串字段 id")
        item["Slot"] = Byte(slot)
        inventory = self.tag.get("Inventory")
        if not isinstance(inventory, ListTag):
            inventory = ListTag([], TAG_COMPOUND)
            self.tag["Inventory"] = inventory
        for index, existing in enumerate(inventory):
            if self._slot_from_item(existing) == slot:
                inventory[index] = item
                return
        inventory.append(item)

    def delete_inventory_item(self, slot: int) -> bool:
        """删除某个槽位的物品；返回是否原本存在。"""
        slot = self._check_slot(slot)
        inventory = self.tag.get("Inventory")
        if not isinstance(inventory, ListTag):
            return False
        for index, existing in enumerate(inventory):
            if self._slot_from_item(existing) == slot:
                del inventory[index]
                return True
        return False

    def summary(self) -> dict:
        rotation = self.tag.get("Rotation")
        rot = None
        if isinstance(rotation, ListTag) and len(rotation) >= 2:
            rot = [float(rotation[0]), float(rotation[1])]
        return {
            "uuid": self.uuid,
            "position": self.position(),
            "rotation": rot,
            "dimension": self.dimension(),
            "game_type": self.game_type(),
            "health": self.tag.get_float("Health", 20.0),
            "absorption": self.tag.get_float("AbsorptionAmount", 0.0),
            "food_level": self.tag.get_int("foodLevel", 20),
            "food_saturation": self.tag.get_float("foodSaturationLevel", 5.0),
            "xp_level": self.tag.get_int("XpLevel", 0),
            "xp_progress": self.tag.get_float("XpP", 0.0),
            "xp_total": self.tag.get_int("XpTotal", 0),
            "selected_slot": self.tag.get_int("SelectedItemSlot", 0),
            "inventory_items": len(self.inventory_items()),
            "ender_items": len(self.ender_items()),
            "data_version": self.tag.get_int("DataVersion", -1),
        }

    # ------------------------------------------------------------------ #
    # 修改
    # ------------------------------------------------------------------ #
    def set_health(self, health: float) -> None:
        if health < 0 or health > 1e6:
            raise PlayerDataError(f"非法生命值: {health}")
        self.tag["Health"] = Float(float(health))

    def set_food(self, food_level: int, saturation: Optional[float] = None) -> None:
        if not 0 <= food_level <= 20:
            raise PlayerDataError(f"饥饿值必须在 0..20: {food_level}")
        self.tag["foodLevel"] = Int(int(food_level))
        if saturation is not None:
            self.tag["foodSaturationLevel"] = Float(float(saturation))

    def set_xp(self, level: Optional[int] = None, progress: Optional[float] = None,
               total: Optional[int] = None) -> None:
        if level is not None:
            if level < 0:
                raise PlayerDataError("经验等级不能为负")
            self.tag["XpLevel"] = Int(int(level))
        if progress is not None:
            if not 0.0 <= progress < 1.0:
                raise PlayerDataError("经验进度必须在 [0,1) 区间")
            self.tag["XpP"] = Float(float(progress))
        if total is not None:
            if total < 0:
                raise PlayerDataError("总经验不能为负")
            self.tag["XpTotal"] = Int(int(total))

    def set_position(self, x: float, y: float, z: float) -> None:
        self.tag["Pos"] = ListTag([Double(float(x)), Double(float(y)), Double(float(z))], TAG_DOUBLE)

    def set_dimension(self, dimension: str) -> None:
        dimension = dimension.lower()
        dimension = DIMENSIONS.get(dimension, dimension)
        if ":" not in dimension:
            dimension = "minecraft:" + dimension
        self.tag["Dimension"] = dimension

    def set_game_type(self, game_type: str) -> None:
        game_type = game_type.lower()
        if game_type not in GAME_TYPE_IDS:
            raise PlayerDataError(
                f"未知游戏模式: {game_type!r} (survival/creative/adventure/spectator)"
            )
        self.tag["playerGameType"] = Int(GAME_TYPE_IDS[game_type])

    def heal(self) -> None:
        self.set_health(20.0)
        self.set_food(20, 5.0)
        self.tag["foodExhaustionLevel"] = Float(0.0)

    def clear_inventory(self) -> None:
        self.tag["Inventory"] = ListTag([], 10)
        self.tag["EnderItems"] = ListTag([], 10)

    # ------------------------------------------------------------------ #
    def get_path(self, path: str) -> Any:
        from .nbt import nbt_get_path

        return nbt_get_path(self.tag, path)

    def set_path(self, path: str, value: Any, create: bool = False) -> None:
        from .nbt import nbt_set_path

        nbt_set_path(self.tag, path, value, create=create)

    def __repr__(self) -> str:
        return f"PlayerData({self.uuid}, {self.dimension()}, pos={self.position()})"


def dump_player(player: PlayerData) -> str:
    return to_snbt(player.tag)
