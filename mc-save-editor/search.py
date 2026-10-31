"""按方块注册名 / 方块实体类型反向查找世界数据。

设计要点:
  - 扫描指定维度所有 region 文件中的全部已生成区块;
  - 方块搜索通过 section 的 block_states.palette 先做快速过滤: 调色板里没有目标方块的
    section 直接跳过, 不解析 4096 个方块索引;
  - 方块实体搜索直接读取区块 NBT 的 block_entities 列表, 按条目的 id 匹配;
  - 匹配数上限 :data:`SHORT_MAX` = 32767（达到后停止扫描）;
  - 结果坐标最多收集 :data:`DISPLAY_LIMIT` = 256 个（GUI 显示上限）,
    但 ``total_found`` / ``truncated`` 仍反映是否达到 32767;
  - 支持进度回调与取消事件, 适合 GUI 后台线程使用;
  - 不经过 World 的 chunk 缓存, 逐区块读取后释放, 避免大世界占用过多内存。
"""

from __future__ import annotations

import glob
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .chunk import Chunk, ChunkError
from .nbt import CompoundTag, NbtError
from .palette import BlockState, PaletteError
from .region import RegionError, RegionFile

SHORT_MAX = 32767          # 匹配数读取上限 (Java Short.MAX_VALUE)
DISPLAY_LIMIT = 256        # 最多返回/显示的坐标数

_BLOCK_ENTITY_ID_RE = re.compile(r"^[a-z0-9_.-]+:[a-z0-9_./-]+$")

ProgressCallback = Callable[[int, int, int], None]  # (scanned, total, found)


@dataclass(frozen=True)
class BlockMatch:
    x: int
    y: int
    z: int
    state: str


@dataclass
class BlockSearchResult:
    block: str
    dimension: str
    matches: list[BlockMatch] = field(default_factory=list)
    total_found: int = 0
    limit: int = SHORT_MAX
    display_limit: int = DISPLAY_LIMIT
    truncated: bool = False       # 达到 limit 提前停止
    cancelled: bool = False       # 用户取消
    chunks_total: int = 0
    chunks_scanned: int = 0
    elapsed: float = 0.0
    errors: list[str] = field(default_factory=list)


def _matching_palette_ids(
    palette: list[BlockState], target: BlockState, exact: bool
) -> set[int]:
    if exact:
        return {i for i, state in enumerate(palette) if state == target}
    return {i for i, state in enumerate(palette) if state.name == target.name}


def find_blocks(
    world,
    block_spec: str | BlockState,
    dimension: Optional[str] = None,
    limit: int = SHORT_MAX,
    display_limit: int = DISPLAY_LIMIT,
    progress: Optional[ProgressCallback] = None,
    cancel_event=None,
) -> BlockSearchResult:
    """在 ``world`` 的指定维度中查找所有匹配 ``block_spec`` 的方块。

    ``block_spec`` 可以是 ``"minecraft:chest"``（按注册名匹配所有状态），
    也可以是 ``"minecraft:oak_log[axis=x]"``（精确匹配状态）。
    """
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    if display_limit <= 0:
        raise ValueError("display_limit 必须大于 0")

    started = time.perf_counter()
    dim = world.normalize_dimension(dimension or world.dimension)
    target = block_spec if isinstance(block_spec, BlockState) else BlockState.parse(str(block_spec))
    exact = bool(target.properties)

    result = BlockSearchResult(
        block=str(target),
        dimension=dim,
        limit=min(limit, SHORT_MAX),
        display_limit=min(display_limit, DISPLAY_LIMIT),
    )
    result.limit = min(limit, SHORT_MAX)
    result.display_limit = min(display_limit, DISPLAY_LIMIT)

    def report() -> None:
        if progress is not None:
            try:
                progress(result.chunks_scanned, result.chunks_total, result.total_found)
            except Exception:  # noqa: BLE001 - 进度回调不应中断搜索
                pass

    def cancelled() -> bool:
        return cancel_event is not None and bool(cancel_event.is_set())

    # 1) 枚举 region 文件及其区块, 先得到进度分母
    jobs: list[tuple[RegionFile, list[tuple[int, int]]]] = []
    for path in sorted(glob.glob(os.path.join(world.region_dir(dim), "*.mca"))):
        try:
            rf = RegionFile(path)
            chunks = rf.list_chunks()
        except RegionError as exc:
            if len(result.errors) < 50:
                result.errors.append(f"{os.path.basename(path)}: {exc}")
            continue
        if chunks:
            jobs.append((rf, chunks))
            result.chunks_total += len(chunks)

    report()

    # 2) 逐区块扫描
    stop = False
    for rf, chunks in jobs:
        for (cx, cz) in chunks:
            if cancelled():
                result.cancelled = True
                stop = True
                break
            try:
                tag = rf.read_chunk(cx, cz)
                if tag is None:
                    result.chunks_scanned += 1
                    continue
                chunk = Chunk(tag, cx, cz)
                for section_y, section_tag in chunk.iter_section_tags():
                    block_states = section_tag.get_compound("block_states")
                    palette_tag = block_states.get_list("palette")
                    if not palette_tag:
                        continue
                    palette: list[BlockState] = []
                    for entry in palette_tag:
                        if isinstance(entry, CompoundTag):
                            palette.append(BlockState.from_tag(entry))
                    if not palette:
                        continue
                    ids = _matching_palette_ids(palette, target, exact)
                    if not ids:
                        continue
                    section = chunk.get_section(section_y)
                    if section is None:
                        continue
                    indices = section.blocks.indices
                    base_y = section_y * 16
                    for index, palette_id in enumerate(indices):
                        if palette_id not in ids:
                            continue
                        result.total_found += 1
                        if len(result.matches) < result.display_limit:
                            lx = index & 15
                            lz = (index >> 4) & 15
                            ly = index >> 8
                            result.matches.append(
                                BlockMatch(
                                    x=(cx << 4) + lx,
                                    y=base_y + ly,
                                    z=(cz << 4) + lz,
                                    state=str(palette[palette_id]),
                                )
                            )
                        if result.total_found >= result.limit:
                            result.truncated = True
                            stop = True
                            break
                    if stop:
                        break
                result.chunks_scanned += 1
                report()
            except (RegionError, ChunkError, PaletteError, NbtError) as exc:
                result.chunks_scanned += 1
                if len(result.errors) < 50:
                    result.errors.append(f"区块({cx},{cz}): {exc}")
                report()
            if stop:
                break
        if stop:
            break

    result.elapsed = time.perf_counter() - started
    report()
    return result


# --------------------------------------------------------------------------- #
# 方块实体搜索
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class BlockEntityMatch:
    x: int
    y: int
    z: int
    be_type: str


@dataclass
class BlockEntitySearchResult:
    block_entity: str
    dimension: str
    matches: list[BlockEntityMatch] = field(default_factory=list)
    total_found: int = 0
    limit: int = SHORT_MAX
    display_limit: int = DISPLAY_LIMIT
    truncated: bool = False
    cancelled: bool = False
    chunks_total: int = 0
    chunks_scanned: int = 0
    elapsed: float = 0.0
    errors: list[str] = field(default_factory=list)


def normalize_block_entity_id(spec: str) -> str:
    """校验/补全方块实体类型 id，如 'chest' -> 'minecraft:chest'。"""
    value = str(spec or "").strip()
    if not value:
        raise ValueError("方块实体类型不能为空（例如 minecraft:chest）")
    if ":" not in value:
        value = "minecraft:" + value
    if not _BLOCK_ENTITY_ID_RE.match(value):
        raise ValueError(
            f"方块实体类型格式不合法：{value!r}（应类似 minecraft:chest）"
        )
    return value


def find_block_entities(
    world,
    be_spec: str,
    dimension: Optional[str] = None,
    limit: int = SHORT_MAX,
    display_limit: int = DISPLAY_LIMIT,
    progress: Optional[ProgressCallback] = None,
    cancel_event=None,
) -> BlockEntitySearchResult:
    """在指定维度所有已生成区块中查找 ``block_entities`` 的类型。

    与方块搜索一致:
      - 匹配数上限 :data:`SHORT_MAX` = 32767（达到后停止扫描）；
      - 坐标最多返回 :data:`DISPLAY_LIMIT` = 256 个；
      - 支持进度回调、取消事件；逐区块读取，不使用 chunk 缓存。
    """
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    if display_limit <= 0:
        raise ValueError("display_limit 必须大于 0")

    started = time.perf_counter()
    dim = world.normalize_dimension(dimension or world.dimension)
    target = normalize_block_entity_id(be_spec)
    result = BlockEntitySearchResult(
        block_entity=target,
        dimension=dim,
        limit=min(limit, SHORT_MAX),
        display_limit=min(display_limit, DISPLAY_LIMIT),
    )

    def report() -> None:
        if progress is not None:
            try:
                progress(result.chunks_scanned, result.chunks_total, result.total_found)
            except Exception:  # noqa: BLE001 - 进度回调不应中断搜索
                pass

    def cancelled() -> bool:
        return cancel_event is not None and bool(cancel_event.is_set())

    jobs: list[tuple[RegionFile, list[tuple[int, int]]]] = []
    for path in sorted(glob.glob(os.path.join(world.region_dir(dim), "*.mca"))):
        try:
            rf = RegionFile(path)
            chunks = rf.list_chunks()
        except RegionError as exc:
            if len(result.errors) < 50:
                result.errors.append(f"{os.path.basename(path)}: {exc}")
            continue
        if chunks:
            jobs.append((rf, chunks))
            result.chunks_total += len(chunks)
    report()

    stop = False
    for rf, chunks in jobs:
        for (cx, cz) in chunks:
            if cancelled():
                result.cancelled = True
                stop = True
                break
            try:
                tag = rf.read_chunk(cx, cz)
                if tag is None:
                    result.chunks_scanned += 1
                    continue
                block_entities = tag.get_list("block_entities")
                for entry in block_entities:
                    if not isinstance(entry, CompoundTag):
                        continue
                    if entry.get_string("id") != target:
                        continue
                    x = entry.get_int("x")
                    y = entry.get_int("y")
                    z = entry.get_int("z")
                    result.total_found += 1
                    if len(result.matches) < result.display_limit:
                        result.matches.append(
                            BlockEntityMatch(x=x, y=y, z=z, be_type=target)
                        )
                    if result.total_found >= result.limit:
                        result.truncated = True
                        stop = True
                        break
                result.chunks_scanned += 1
                report()
            except (RegionError, ChunkError, NbtError) as exc:
                result.chunks_scanned += 1
                if len(result.errors) < 50:
                    result.errors.append(f"区块({cx},{cz}): {exc}")
                report()
            if stop:
                break
        if stop:
            break

    result.elapsed = time.perf_counter() - started
    report()
    return result
