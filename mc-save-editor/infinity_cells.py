"""ExtendedAE-Plus 无限存储单元（Infinity BigInteger Cell）读写。

数据位置（源码研究结论，见 docs/mod_research/extendedae_plus_infinity_cell.md）:
  <world>/data/infinity_biginteger_cells.dat
根结构:
  {DataVersion:int, data:{infinity_format_version:int,
     infinity_cell_list:[{
       infinity_cell_uuid:int[4],
       infinity_cell_data:{
         infinity_cell_keys:[AEKey generic tag, ...],
         infinity_cell_amounts:[{value:[B;...]}, ...],
         infinity_cell_item_count:[B;...]
       }
     }]}}
keys 与 amounts 是平行列表；amount 的 value 是 Java BigInteger 的补码大端
byte[]。物品 ItemStack 里只有 minecraft:custom_data.infinity_cell_uuid 指针，
实际内容全部在本文件。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from .backpacks import BackpackDataError, ints_to_uuid, uuid_to_ints
from .nbt import ByteArray, CompoundTag, IntArray, ListTag, TAG_COMPOUND, to_snbt
from .world import WorldError

INFINITY_DATA_PATH = "data/infinity_biginteger_cells.dat"
DATA_KEY = "data"
FORMAT_VERSION_KEY = "infinity_format_version"
CELL_LIST_KEY = "infinity_cell_list"
CELL_UUID_KEY = "infinity_cell_uuid"
CELL_DATA_KEY = "infinity_cell_data"
CELL_KEYS_KEY = "infinity_cell_keys"
CELL_AMOUNTS_KEY = "infinity_cell_amounts"
CELL_ITEM_COUNT_KEY = "infinity_cell_item_count"
AMOUNT_VALUE_KEY = "value"


class InfinityDataError(BackpackDataError):
    """无限存储单元数据格式错误。"""


def decode_big_integer(raw: Any) -> Optional[int]:
    """Java BigInteger(byte[]) -> Python int；空数组/非法数据返回 None。"""
    if isinstance(raw, ByteArray):
        data = raw.data
    elif isinstance(raw, (bytes, bytearray)):
        data = bytes(raw)
    elif isinstance(raw, (list, tuple)):
        try:
            data = bytes(int(v) & 0xFF for v in raw)
        except (TypeError, ValueError):
            return None
    else:
        return None
    if not data:
        return None
    try:
        return int.from_bytes(data, "big", signed=True)
    except (ValueError, OverflowError):
        return None


def encode_big_integer(value: int) -> ByteArray:
    """Python int -> Java BigInteger.toByteArray()（最小补码大端）。"""
    if value <= 0:
        raise InfinityDataError("数量必须是正整数")
    length = (value.bit_length() + 8) // 8
    raw = value.to_bytes(length, "big", signed=True)
    return ByteArray(raw)


def key_id(key_tag: CompoundTag) -> str:
    """尽量给出可读的 AEKey 标识；无法识别时返回类型/id 摘要。"""
    if not isinstance(key_tag, CompoundTag):
        return "?"
    item_id = key_tag.get_string("id")
    if item_id:
        return item_id
    key_type = key_tag.get_string("#t")
    return key_type or "?"


def key_description(key_tag: CompoundTag, max_length: int = 180) -> str:
    text = to_snbt(key_tag)
    return text if len(text) <= max_length else text[: max_length - 3] + "..."


@dataclass
class CellEntry:
    index: int
    key_tag: CompoundTag
    amount: Optional[int]

    @property
    def item_id(self) -> str:
        return key_id(self.key_tag)

    @property
    def valid(self) -> bool:
        return self.amount is not None and self.amount > 0


@dataclass
class InfinityCell:
    uuid: str
    uuid_ints: IntArray
    entry: CompoundTag
    data: CompoundTag

    def entries(self) -> list[CellEntry]:
        keys = self.data.get(CELL_KEYS_KEY)
        amounts = self.data.get(CELL_AMOUNTS_KEY)
        if not isinstance(keys, ListTag):
            return []
        if not isinstance(amounts, ListTag):
            return []
        result: list[CellEntry] = []
        for index in range(min(len(keys), len(amounts))):
            key_tag = keys[index]
            amount_tag = amounts[index]
            if not isinstance(key_tag, CompoundTag):
                continue
            amount_value = None
            if isinstance(amount_tag, CompoundTag):
                amount_value = decode_big_integer(amount_tag.get(AMOUNT_VALUE_KEY))
            result.append(CellEntry(index=index, key_tag=key_tag, amount=amount_value))
        return result

    def item_types(self) -> int:
        return len(self.entries())

    def item_count(self) -> Optional[int]:
        return self.data.get(CELL_ITEM_COUNT_KEY) and decode_big_integer(
            self.data.get(CELL_ITEM_COUNT_KEY)
        )

    def summary(self) -> dict[str, Any]:
        entries = self.entries()
        total = sum(entry.amount for entry in entries if entry.amount is not None)
        return {
            "uuid": self.uuid,
            "types": len(entries),
            "total": total,
        }


@dataclass
class InfinityCellStore:
    world: Any
    root: Optional[CompoundTag] = None
    data: Optional[CompoundTag] = None
    cells: list[InfinityCell] = field(default_factory=list)

    @property
    def path(self) -> str:
        return INFINITY_DATA_PATH

    def load(self) -> list[InfinityCell]:
        self.root = None
        self.data = None
        self.cells = []
        try:
            path = self.world.resolve_world_file(self.path)
        except Exception as exc:  # noqa: BLE001
            raise InfinityDataError(f"无法定位无限存储数据文件: {exc}") from exc
        if not os.path.isfile(path):
            return self.cells
        try:
            _name, root = self.world.read_dat(self.path)
        except WorldError as exc:
            raise InfinityDataError(f"读取无限存储数据失败: {exc}") from exc
        if not isinstance(root, CompoundTag):
            raise InfinityDataError("无限存储数据根 tag 不是 compound")
        self.root = root
        data = root.get(DATA_KEY)
        if not isinstance(data, CompoundTag):
            raise InfinityDataError(f"无限存储数据缺少 {DATA_KEY!r} compound")
        self.data = data
        raw_list = data.get(CELL_LIST_KEY)
        if not isinstance(raw_list, ListTag):
            data[CELL_LIST_KEY] = ListTag([], TAG_COMPOUND)
            raw_list = data[CELL_LIST_KEY]
        for entry in raw_list:
            if not isinstance(entry, CompoundTag):
                continue
            raw_uuid = entry.get(CELL_UUID_KEY)
            if not isinstance(raw_uuid, (IntArray, list)):
                continue
            try:
                uuid_text = ints_to_uuid(raw_uuid)
            except BackpackDataError:
                continue
            cell_data = entry.get(CELL_DATA_KEY)
            if not isinstance(cell_data, CompoundTag):
                cell_data = CompoundTag()
                entry[CELL_DATA_KEY] = cell_data
            self.cells.append(
                InfinityCell(
                    uuid=uuid_text,
                    uuid_ints=IntArray([int(v) for v in raw_uuid]),
                    entry=entry,
                    data=cell_data,
                )
            )
        return self.cells

    def get(self, uuid: str) -> Optional[InfinityCell]:
        wanted = str(uuid)
        for cell in self.cells:
            if cell.uuid == wanted:
                return cell
        return None

    def set_amount(self, cell: InfinityCell, index: int, value: int) -> None:
        """把某个 entry 的数量改成正整数；保留 key/其它字段。"""
        keys = cell.data.get(CELL_KEYS_KEY)
        amounts = cell.data.get(CELL_AMOUNTS_KEY)
        if not isinstance(keys, ListTag) or not isinstance(amounts, ListTag):
            raise InfinityDataError("cell 缺少 keys/amounts 列表")
        index = int(index)
        if not 0 <= index < min(len(keys), len(amounts)):
            raise InfinityDataError(f"条目标号 {index} 超出范围")
        amount_tag = amounts[index]
        if not isinstance(amount_tag, CompoundTag):
            amount_tag = CompoundTag()
            amounts[index] = amount_tag
        amount_tag[AMOUNT_VALUE_KEY] = encode_big_integer(int(value))

    def add_entry(self, cell: InfinityCell, key_tag: CompoundTag, value: int) -> None:
        """新增一个 AEKey -> BigInteger 条目；key_tag 必须是 compound。"""
        if not isinstance(key_tag, CompoundTag):
            raise InfinityDataError("AEKey 必须是 compound SNBT")
        keys = cell.data.get(CELL_KEYS_KEY)
        amounts = cell.data.get(CELL_AMOUNTS_KEY)
        if not isinstance(keys, ListTag) or not isinstance(amounts, ListTag):
            raise InfinityDataError("cell 缺少 keys/amounts 列表")
        amount_tag = CompoundTag()
        amount_tag[AMOUNT_VALUE_KEY] = encode_big_integer(int(value))
        keys.append(key_tag)
        amounts.append(amount_tag)

    def delete_entry(self, cell: InfinityCell, index: int) -> bool:
        keys = cell.data.get(CELL_KEYS_KEY)
        amounts = cell.data.get(CELL_AMOUNTS_KEY)
        if not isinstance(keys, ListTag) or not isinstance(amounts, ListTag):
            return False
        index = int(index)
        if not 0 <= index < min(len(keys), len(amounts)):
            return False
        del keys[index]
        del amounts[index]
        return True

    def save(self) -> list[str]:
        if self.root is None:
            raise InfinityDataError("没有可保存的无限存储数据（文件不存在或未加载）")
        before = len(getattr(self.world, "backups", []))
        self.world.write_dat(self.path, self.root)
        return list(getattr(self.world, "backups", [])[before:])


def list_cells(world) -> InfinityCellStore:
    store = InfinityCellStore(world)
    store.load()
    return store


def get_cell(world, uuid: str) -> Optional[InfinityCell]:
    return list_cells(world).get(uuid)


__all__ = [
    "INFINITY_DATA_PATH",
    "CellEntry",
    "InfinityCell",
    "InfinityCellStore",
    "InfinityDataError",
    "decode_big_integer",
    "encode_big_integer",
    "get_cell",
    "key_description",
    "key_id",
    "list_cells",
    "uuid_to_ints",
]
