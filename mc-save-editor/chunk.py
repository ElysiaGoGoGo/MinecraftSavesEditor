"""区块 (Chunk) 解析、方块与方块实体读写。

对应 Minecraft 1.21.1 源码:
  mc_src/net/minecraft/world/level/chunk/storage/ChunkSerializer.java
  mc_src/net/minecraft/world/level/chunk/LevelChunkSection.java
  mc_src/net/minecraft/world/level/chunk/LevelChunk.java
  mc_src/net/minecraft/world/level/block/entity/BlockEntity.java

1.21.1 区块 NBT (Status=minecraft:full) 关键结构:
  {
    DataVersion: int,
    xPos: int, zPos: int, yPos: int,          # yPos = 最低 section 的 Y
    Status: string,
    LastUpdate: long, InhabitedTime: long,
    sections: [ { Y: byte, block_states: {...}, biomes: {...},
                  BlockLight: byte[2048]?, SkyLight: byte[2048]? }, ... ],
    block_entities: [ { id, x, y, z, ... }, ... ],
    Heightmaps: { MOTION_BLOCKING: long[37], ... },
    structures: {...}, block_ticks:[], fluid_ticks:[], PostProcessing:[[...],...],
    isLightOn: 1b?
  }
  实体不在区块内: 1.21.1 的实体保存在 <dimension>/entities/r.x.z.mca
  (EntityStorage.java, 根为 { DataVersion, Entities:[...], Position:[cx,cz] })。
"""

from __future__ import annotations

from typing import Iterator, Optional

from .nbt import (
    Byte,
    ByteArray,
    CompoundTag,
    Int,
    ListTag,
    LongArray,
    NbtError,
    TAG_COMPOUND,
    TAG_LIST,
)
from .palette import (
    STRATEGY_BIOMES,
    STRATEGY_STATES,
    BlockState,
    PalettedContainer,
    PaletteError,
)

MIN_BUILD_HEIGHT = -64
MAX_BUILD_HEIGHT = 319


class ChunkError(NbtError):
    """区块数据错误。"""


class Section:
    """一个 16x16x16 区块段。"""

    __slots__ = ("tag", "y", "blocks", "biomes")

    def __init__(self, tag: CompoundTag, y: int):
        self.tag = tag
        self.y = y
        if "block_states" in tag:
            try:
                self.blocks = PalettedContainer.decode(tag.get_compound("block_states"), STRATEGY_STATES)
            except PaletteError as exc:
                raise ChunkError(f"section Y={y} 的 block_states 解析失败: {exc}") from exc
        else:
            self.blocks = PalettedContainer(STRATEGY_STATES, [BlockState("minecraft:air")], [0] * 4096)
        if "biomes" in tag:
            try:
                self.biomes = PalettedContainer.decode(tag.get_compound("biomes"), STRATEGY_BIOMES)
            except PaletteError as exc:
                raise ChunkError(f"section Y={y} 的 biomes 解析失败: {exc}") from exc
        else:
            self.biomes = PalettedContainer(STRATEGY_BIOMES, ["minecraft:plains"], [0] * 64)

    def get_block(self, lx: int, ly: int, lz: int) -> BlockState:
        return self.blocks.get((ly << 8) | (lz << 4) | lx)

    def set_block(self, lx: int, ly: int, lz: int, state: BlockState) -> None:
        self.blocks.set((ly << 8) | (lz << 4) | lx, state)

    def encode(self) -> None:
        """把内存中的调色板写回 section tag (保留 BlockLight/SkyLight/Y 等)。"""
        self.tag["Y"] = Byte(self.y)
        self.tag["block_states"] = self.blocks.encode()
        self.tag["biomes"] = self.biomes.encode()


class Chunk:
    """一个区块 (坐标 cx, cz 为绝对区块坐标)。"""

    def __init__(self, tag: CompoundTag, cx: int, cz: int):
        if not isinstance(tag, CompoundTag):
            raise ChunkError("区块根 tag 不是 TAG_Compound")
        self.tag = tag
        self.cx = cx
        self.cz = cz
        self.modified = False
        self.modified_block_entities = False
        tag_cx = tag.get_int("xPos", cx)
        tag_cz = tag.get_int("zPos", cz)
        if (tag_cx, tag_cz) != (cx, cz):
            raise ChunkError(
                f"区块文件位置与内容不一致: 期望 ({cx},{cz}), 文件内为 ({tag_cx},{tag_cz})"
            )
        self.min_section = tag.get_int("yPos", MIN_BUILD_HEIGHT >> 4)
        sections_tag = tag.get("sections")
        if not isinstance(sections_tag, ListTag):
            raise ChunkError(f"区块 ({cx},{cz}) 缺少 sections 列表")
        self._section_tags: dict[int, CompoundTag] = {}
        for sec in sections_tag:
            if not isinstance(sec, CompoundTag):
                raise ChunkError(f"区块 ({cx},{cz}) 的 sections 元素不是 compound")
            self._section_tags[sec.get_byte("Y")] = sec
        self._sections: dict[int, Section] = {}

    # ------------------------------------------------------------------ #
    # 基本信息
    # ------------------------------------------------------------------ #
    @property
    def status(self) -> str:
        return self.tag.get_string("Status")

    @property
    def data_version(self) -> int:
        return self.tag.get_int("DataVersion", -1)

    @property
    def is_full(self) -> bool:
        return self.status == "minecraft:full"

    @property
    def min_section_y(self) -> int:
        return self.min_section

    def section_y_range(self) -> range:
        return range(self.min_section, self.min_section + len(self._section_tags))

    # ------------------------------------------------------------------ #
    # Section
    # ------------------------------------------------------------------ #
    def get_section(self, sy: int, create: bool = False) -> Optional[Section]:
        if not create and sy in self._sections:
            return self._sections[sy]
        if sy in self._section_tags:
            if sy not in self._sections:
                self._sections[sy] = Section(self._section_tags[sy], sy)
            return self._sections[sy]
        if not create:
            return None
        # 新建 section: 默认空气 + 从已有 section 复制生物群系
        tag = CompoundTag()
        tag["Y"] = Byte(sy)
        biome_palette: list = ["minecraft:plains"]
        biome_indices = [0] * 64
        for other in self._sections.values():
            biome_palette = [str(b) for b in other.biomes.palette]
            biome_indices = list(other.biomes.indices)
            break
        blocks = PalettedContainer(STRATEGY_STATES, [BlockState("minecraft:air")], [0] * 4096)
        biomes = PalettedContainer(STRATEGY_BIOMES, biome_palette, biome_indices)
        tag["block_states"] = blocks.encode()
        tag["biomes"] = biomes.encode()
        sections_tag = self.tag.get("sections")
        if not isinstance(sections_tag, ListTag):
            sections_tag = ListTag([], TAG_COMPOUND)
            self.tag["sections"] = sections_tag
        sections_tag.append(tag)
        self._section_tags[sy] = tag
        self._sections[sy] = Section(tag, sy)
        self.modified = True
        return self._sections[sy]

    def iter_sections(self) -> Iterator[Section]:
        for sy in sorted(self._section_tags):
            yield self.get_section(sy)  # type: ignore[misc]

    def iter_section_tags(self) -> Iterator[tuple[int, CompoundTag]]:
        """按 section Y 升序返回原始 section NBT（不解码调色板/索引）。

        供“按方块名搜索”等场景做快速过滤：先看 palette，命中后再解码索引。
        """
        for sy in sorted(self._section_tags):
            yield sy, self._section_tags[sy]

    # ------------------------------------------------------------------ #
    # 方块
    # ------------------------------------------------------------------ #
    @staticmethod
    def block_index(lx: int, ly: int, lz: int) -> int:
        return (ly << 8) | (lz << 4) | lx

    def get_block(self, x: int, y: int, z: int) -> BlockState:
        lx, ly, lz = x & 15, y & 15, z & 15
        sec = self.get_section(y >> 4, create=False)
        if sec is None:
            return BlockState("minecraft:air")
        return sec.get_block(lx, ly, lz)

    def set_block(
        self,
        x: int,
        y: int,
        z: int,
        state: BlockState,
        keep_block_entity: bool = False,
    ) -> None:
        if not MIN_BUILD_HEIGHT <= y <= MAX_BUILD_HEIGHT:
            raise ChunkError(f"Y={y} 超出 1.21.1 主世界可编辑高度 {MIN_BUILD_HEIGHT}..{MAX_BUILD_HEIGHT}")
        sec = self.get_section(y >> 4, create=True)
        assert sec is not None
        sec.set_block(x & 15, y & 15, z & 15, state)
        self.modified = True
        if not keep_block_entity and self.find_block_entity(x, y, z) is not None:
            self.delete_block_entity(x, y, z)

    def fill_blocks(
        self,
        x1: int, y1: int, z1: int, x2: int, y2: int, z2: int,
        state: BlockState, keep_block_entity: bool = False,
    ) -> int:
        """填充长方体, 返回修改的方块数。坐标闭区间。"""
        lo_x, hi_x = sorted((x1, x2))
        lo_y, hi_y = sorted((y1, y2))
        lo_z, hi_z = sorted((z1, z2))
        count = 0
        for y in range(lo_y, hi_y + 1):
            for z in range(lo_z, hi_z + 1):
                for x in range(lo_x, hi_x + 1):
                    self.set_block(x, y, z, state, keep_block_entity=keep_block_entity)
                    count += 1
        return count

    # ------------------------------------------------------------------ #
    # 方块实体
    # ------------------------------------------------------------------ #
    def block_entities_tag(self, create: bool = False) -> ListTag:
        tag = self.tag.get("block_entities")
        if isinstance(tag, ListTag):
            return tag
        if not create:
            return ListTag([], TAG_COMPOUND)
        new = ListTag([], TAG_COMPOUND)
        self.tag["block_entities"] = new
        return new

    def iter_block_entities(self) -> Iterator[CompoundTag]:
        for entry in self.block_entities_tag():
            if isinstance(entry, CompoundTag):
                yield entry

    def find_block_entity(self, x: int, y: int, z: int) -> Optional[CompoundTag]:
        for entry in self.iter_block_entities():
            if (entry.get_int("x"), entry.get_int("y"), entry.get_int("z")) == (x, y, z):
                return entry
        return None

    def set_block_entity(self, tag: CompoundTag) -> None:
        """写入/替换一个方块实体 NBT。必须包含 id/x/y/z 或由调用方提供坐标。"""
        if not isinstance(tag, CompoundTag):
            raise ChunkError("方块实体数据必须是 TAG_Compound")
        if "id" not in tag or not isinstance(tag.get("id"), str):
            raise ChunkError("方块实体数据缺少字符串字段 id (例如 minecraft:chest)")
        x, y, z = tag.get_int("x"), tag.get_int("y"), tag.get_int("z")
        if (x, y, z) == (0, 0, 0) and ("x" not in tag or "y" not in tag or "z" not in tag):
            raise ChunkError("方块实体数据缺少 x/y/z 坐标")
        entries = self.block_entities_tag(create=True)
        for i, entry in enumerate(entries):
            if isinstance(entry, CompoundTag) and (
                entry.get_int("x"), entry.get_int("y"), entry.get_int("z")
            ) == (x, y, z):
                entries[i] = tag
                self.modified = True
                self.modified_block_entities = True
                return
        entries.append(tag)
        self.modified = True
        self.modified_block_entities = True

    def delete_block_entity(self, x: int, y: int, z: int) -> bool:
        entries = self.block_entities_tag()
        for i, entry in enumerate(entries):
            if isinstance(entry, CompoundTag) and (
                entry.get_int("x"), entry.get_int("y"), entry.get_int("z")
            ) == (x, y, z):
                del entries[i]
                self.modified = True
                self.modified_block_entities = True
                return True
        return False

    # ------------------------------------------------------------------ #
    # 元数据 / 保存
    # ------------------------------------------------------------------ #
    def mark_needs_relight(self) -> None:
        """方块改变后让游戏重算光照与高度图 (移除旧的缓存数据)。

        ChunkSerializer.read 中:
          - 缺少 Heightmaps 条目 -> Heightmap.primeHeightmaps 从方块重新计算
          - isLightOn 缺失/false -> 区块光照被视为不可信, 由光照引擎重新计算
        """
        self.tag.pop("isLightOn", None)
        self.tag.pop("Heightmaps", None)
        for sec_tag in self._section_tags.values():
            sec_tag.pop("BlockLight", None)
            sec_tag.pop("SkyLight", None)

    def flush_sections(self) -> None:
        for sec in self._sections.values():
            sec.encode()
            tag = self._section_tags.get(sec.y)
            if tag is not None:
                tag["Y"] = Byte(sec.y)

    def finalize(self) -> CompoundTag:
        """把所有修改写回 tag 并返回。"""
        if self.modified:
            self.flush_sections()
        return self.tag

    def summary(self) -> dict:
        counts = {"sections": len(self._section_tags)}
        be_count = len(list(self.iter_block_entities()))
        return {
            "chunk": [self.cx, self.cz],
            "status": self.status,
            "data_version": self.data_version,
            "min_section": self.min_section,
            "sections": counts["sections"],
            "block_entities": be_count,
            "is_full": self.is_full,
        }

    def __repr__(self) -> str:
        return f"Chunk({self.cx},{self.cz}, status={self.status!r})"
