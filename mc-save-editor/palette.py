"""方块状态 / 生物群系调色板容器 (PalettedContainer) 编解码。

对应 Minecraft 1.21.1 源码:
  mc_src/net/minecraft/world/level/chunk/PalettedContainer.java
  mc_src/net/minecraft/world/level/chunk/LinearPalette.java
  mc_src/net/minecraft/world/level/chunk/HashMapPalette.java
  mc_src/net/minecraft/world/level/chunk/SingleValuePalette.java
  mc_src/net/minecraft/world/level/chunk/GlobalPalette.java
  mc_src/net/minecraft/util/SimpleBitStorage.java
  mc_src/net/minecraft/world/level/block/state/StateHolder.java

1.21.1 区块 NBT 中:
  sections[i].block_states = { palette:[{Name:"minecraft:stone",Properties:{...}}], data:[L;...] }
  sections[i].biomes       = { palette:["minecraft:plains"], data:[L;...] }

关键点 (源码 PalettedContainer#pack 与 Strategy#calculateBitsForSerialization):
  - 写盘时的位宽并不等于内存中的位宽, 而是由 "调色板条目数 n" 决定:
      方块 (SECTION_STATES, size=4096):
          n == 1            -> 0 bit, 完全没有 data 字段
          2 <= n <= 16      -> 4 bit
          n >= 17           -> ceil(log2(n)) bit
      生物群系 (SECTION_BIOMES, size=64):
          n == 1            -> 0 bit
          n >= 2            -> ceil(log2(n)) bit (最大 6)
  - SimpleBitStorage 每个 long 放 valuesPerLong = 64 // bits 个值,
    低位在前, 不跨 long。原始 long 数 = ceil(size / valuesPerLong)。
  - 因此读盘时只需要 palette 长度即可推出位宽, 不需要方块注册表大小。
  - data 中的值是调色板下标, 不是全局方块 id (全局调色板在读取时由游戏重映射)。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .nbt import (
    CompoundTag,
    ListTag,
    LongArray,
    NbtError,
    TAG_COMPOUND,
    TAG_STRING,
)

STRATEGY_STATES = "states"
STRATEGY_BIOMES = "biomes"

SIZE_STATES = 4096
SIZE_BIOMES = 64
MASK64 = 0xFFFFFFFFFFFFFFFF


class PaletteError(NbtError):
    """调色板数据错误。"""


def _ceil_log2(n: int) -> int:
    if n <= 1:
        return 0
    return (n - 1).bit_length()


def calculate_bits(strategy: str, palette_size: int) -> int:
    """按 Strategy#calculateBitsForSerialization 计算写盘位宽。"""
    if palette_size <= 1:
        return 0
    i = _ceil_log2(palette_size)
    if strategy == STRATEGY_STATES:
        # getConfiguration: i=1..4 -> LINEAR palette (bits=4); i=5..8 -> HASHMAP(bits=i);
        # i>=9 -> GLOBAL 且序列化位宽 = i
        return 4 if i <= 4 else i
    if strategy == STRATEGY_BIOMES:
        # i=1..3 -> LINEAR(bits=i); i>=4 -> GLOBAL 且序列化位宽 = i
        return i
    raise PaletteError(f"未知调色板策略: {strategy!r}")


def container_size(strategy: str) -> int:
    return SIZE_STATES if strategy == STRATEGY_STATES else SIZE_BIOMES


def raw_length(bits: int, size: int) -> int:
    """SimpleBitStorage 的 long 数组长度。"""
    if bits == 0:
        return 0
    values_per_long = 64 // bits
    return (size + values_per_long - 1) // values_per_long


def unpack_indices(bits: int, size: int, longs: Iterable[int]) -> list[int]:
    """SimpleBitStorage 解包。"""
    if bits == 0:
        return [0] * size
    values_per_long = 64 // bits
    mask = (1 << bits) - 1
    longs = list(longs)
    expected = raw_length(bits, size)
    if len(longs) != expected:
        raise PaletteError(
            f"data 长度错误: bits={bits}, size={size} 期望 {expected} 个 long, 实际 {len(longs)}"
        )
    out: list[int] = []
    for value in longs:
        value &= MASK64
        for k in range(values_per_long):
            if len(out) >= size:
                return out
            out.append((value >> (k * bits)) & mask)
    return out


def pack_indices(bits: int, size: int, values: Iterable[int]) -> list[int]:
    """SimpleBitStorage 打包, 返回 Python 有符号 long 列表 (用于 NBT TAG_Long_Array)。"""
    values = list(values)
    if len(values) != size:
        raise PaletteError(f"values 数量错误: 期望 {size}, 实际 {len(values)}")
    if bits == 0:
        return []
    mask = (1 << bits) - 1
    values_per_long = 64 // bits
    out: list[int] = []
    for start in range(0, size, values_per_long):
        chunk = values[start:start + values_per_long]
        acc = 0
        for k, v in enumerate(chunk):
            v = int(v)
            if v < 0 or v > mask:
                raise PaletteError(f"值 {v} 超出 {bits} bit 范围")
            acc |= (v & mask) << (k * bits)
        out.append(acc)
    # 转成有符号 64 位, 与 Java long 一致
    return [v - (1 << 64) if v >= (1 << 63) else v for v in out]


# --------------------------------------------------------------------------- #
# 方块状态
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, order=True)
class BlockState:
    """一个方块状态, 例如 minecraft:oak_log[axis=y]。"""

    name: str
    properties: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise PaletteError("方块 id 不能为空")

    def __str__(self) -> str:
        if not self.properties:
            return self.name
        props = ",".join(f"{k}={v}" for k, v in self.properties)
        return f"{self.name}[{props}]"

    @property
    def block_id(self) -> str:
        return self.name

    def to_tag(self) -> CompoundTag:
        tag = CompoundTag()
        tag["Name"] = self.name
        if self.properties:
            props = CompoundTag()
            for k, v in self.properties:
                props[k] = v
            tag["Properties"] = props
        return tag

    @classmethod
    def from_tag(cls, tag: CompoundTag) -> "BlockState":
        name = tag.get("Name")
        if not isinstance(name, str) or not name:
            raise PaletteError("block_states.palette 条目缺少 Name 字符串")
        props_raw = tag.get("Properties")
        props: list[tuple[str, str]] = []
        if isinstance(props_raw, CompoundTag):
            for k, v in props_raw.items():
                if not isinstance(v, str):
                    raise PaletteError(f"方块属性 {k!r} 的值不是字符串")
                props.append((str(k), v))
        elif props_raw is not None:
            raise PaletteError("block_states.palette 条目的 Properties 不是 compound")
        props.sort()
        return cls(name=name, properties=tuple(props))

    @classmethod
    def parse(cls, spec: str) -> "BlockState":
        """解析 'minecraft:oak_log[axis=y]' / 'stone' 形式。"""
        text = spec.strip()
        if not text:
            raise PaletteError("方块状态为空")
        props: list[tuple[str, str]] = []
        if "[" in text:
            if not text.endswith("]"):
                raise PaletteError(f"方块状态格式错误 (缺少 ]): {spec!r}")
            name_part, props_part = text.split("[", 1)
            props_part = props_part[:-1]
            for item in props_part.split(","):
                item = item.strip()
                if not item:
                    continue
                if "=" not in item:
                    raise PaletteError(f"方块属性格式错误 (缺少 =): {item!r}")
                key, value = item.split("=", 1)
                props.append((key.strip(), _strip_quotes(value.strip())))
            props.sort()
        else:
            name_part = text
        name = name_part.strip()
        if not name:
            raise PaletteError(f"方块 id 为空: {spec!r}")
        if ":" not in name:
            name = "minecraft:" + name
        return cls(name=name, properties=tuple(props))


def _strip_quotes(s: str) -> str:
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


# --------------------------------------------------------------------------- #
# 调色板容器
# --------------------------------------------------------------------------- #

class PalettedContainer:
    """一个 section 的方块状态或生物群系调色板容器 (4096 / 64 项)。"""

    __slots__ = ("strategy", "palette", "indices", "size")

    def __init__(self, strategy: str, palette: list, indices: list[int]):
        self.strategy = strategy
        self.size = container_size(strategy)
        self.palette = list(palette)
        if not self.palette:
            raise PaletteError("调色板不能为空")
        if len(indices) != self.size:
            raise PaletteError(f"索引数量错误: 期望 {self.size}, 实际 {len(indices)}")
        self.indices = [int(i) for i in indices]
        max_idx = max(self.indices)
        if max_idx >= len(self.palette):
            raise PaletteError(f"索引 {max_idx} 超出调色板大小 {len(self.palette)}")

    # ---- 读 ----
    @classmethod
    def decode(cls, tag: CompoundTag, strategy: str) -> "PalettedContainer":
        size = container_size(strategy)
        palette_tag = tag.get("palette")
        if not isinstance(palette_tag, ListTag):
            raise PaletteError("调色板容器缺少 palette 列表")
        palette: list = []
        if strategy == STRATEGY_STATES:
            for entry in palette_tag:
                if not isinstance(entry, CompoundTag):
                    raise PaletteError("block_states.palette 的元素必须是 compound")
                palette.append(BlockState.from_tag(entry))
        else:
            for entry in palette_tag:
                if not isinstance(entry, str):
                    raise PaletteError("biomes.palette 的元素必须是字符串")
                palette.append(entry)
        if not palette:
            raise PaletteError("调色板为空")
        bits = calculate_bits(strategy, len(palette))
        if bits == 0:
            if len(palette) != 1:
                raise PaletteError("0 位调色板必须恰好有 1 个条目")
            return cls(strategy, palette, [0] * size)
        data = tag.get("data")
        if not isinstance(data, LongArray):
            raise PaletteError(
                f"调色板条目数 {len(palette)} 需要 data 数组 (bits={bits}), 但缺少或类型错误"
            )
        indices = unpack_indices(bits, size, data)
        if any(i >= len(palette) for i in indices):
            raise PaletteError("data 中存在超出调色板的索引")
        return cls(strategy, palette, indices)

    # ---- 写 ----
    def encode(self) -> CompoundTag:
        tag = CompoundTag()
        if self.strategy == STRATEGY_STATES:
            palette_tag = ListTag([s.to_tag() for s in self.palette], TAG_COMPOUND)
        else:
            palette_tag = ListTag([str(b) for b in self.palette], TAG_STRING)
        tag["palette"] = palette_tag
        bits = calculate_bits(self.strategy, len(self.palette))
        if bits > 0:
            tag["data"] = LongArray(pack_indices(bits, self.size, self.indices))
        return tag

    # ---- 访问 ----
    def get(self, index: int):
        return self.palette[self.indices[index]]

    def set(self, index: int, value) -> None:
        try:
            pid = self.palette.index(value)
        except ValueError:
            pid = len(self.palette)
            self.palette.append(value)
        self.indices[index] = pid

    def fill(self, value) -> None:
        self.palette = [value]
        self.indices = [0] * self.size

    def __repr__(self) -> str:
        return (
            f"PalettedContainer(strategy={self.strategy!r}, palette_size={len(self.palette)}, "
            f"bits={calculate_bits(self.strategy, len(self.palette))})"
        )


# --------------------------------------------------------------------------- #
# 高度图 (Heightmap) 位打包
# --------------------------------------------------------------------------- #

# 注意: 位宽 = ceil(log2(worldHeight + 1)), 由维度高度决定:
#   主世界/末地高度 384 (-64..319) -> 9 bit -> 37 longs
#   下界高度 256 (0..255)         -> 8 bit -> 32 longs
# 本工具修改方块后会移除整个 Heightmaps 让游戏按实际方块重算, 因此不需要写
# 高度图; 下面的 9-bit 辅助函数仅用于读取/测试主世界高度图。
HEIGHTMAP_BITS = 9
HEIGHTMAP_SIZE = 256


def unpack_heightmap(longs: Iterable[int]) -> list[int]:
    return unpack_indices(HEIGHTMAP_BITS, HEIGHTMAP_SIZE, longs)


def pack_heightmap(values: Iterable[int]) -> list[int]:
    return pack_indices(HEIGHTMAP_BITS, HEIGHTMAP_SIZE, values)
