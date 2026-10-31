"""Minecraft 世界目录抽象: 维度、region、区块、玩家、level.dat、备份与锁定。

对应 Minecraft 1.21.1 源码:
  mc_src/net/minecraft/world/level/storage/LevelStorageSource.java
  mc_src/net/minecraft/world/level/dimension/DimensionType.java#getStorageFolder
  mc_src/net/minecraft/server/level/ServerLevel.java
  mc_src/net/minecraft/server/level/ChunkMap.java
  mc_src/net/minecraft/world/level/chunk/storage/IOWorker.java

世界目录布局 (1.21.1):
  <world>/level.dat, level.dat_old, session.lock, icon.png
  <world>/region/r.<rx>.<rz>.mca                主世界区块
  <world>/entities/r.<rx>.<rz>.mca              主世界实体
  <world>/poi/r.<rx>.<rz>.mca                   主世界 POI
  <world>/data/*.dat                            杂项数据 (地图、袭击等)
  <world>/playerdata/<uuid>.dat                 玩家数据
  <world>/advancements/<uuid>.json              进度
  <world>/stats/<uuid>.json                     统计
  <world>/DIM-1/...                             下界 (同上结构)
  <world>/DIM1/...                              末地
  <world>/dimensions/<ns>/<path>/...            自定义维度
"""

from __future__ import annotations

import glob
import os
import shutil
import time
from typing import Iterator, Optional

from .chunk import Chunk, ChunkError
from .level import LevelData, LevelDataError
from .lock import DirectoryLock, WorldInUseError
from .nbt import CompoundTag, NbtError, TAG_COMPOUND, read_nbt_file, write_nbt
from .palette import PaletteError
from .player import PlayerData
from .region import RegionError, RegionFile, region_file_path
from .versions import (
    DetectedVersion,
    UnsupportedVersionError,
    VersionProfile,
    detect_level_version,
    resolve_version,
    supported_versions_summary,
)

OVERWORLD_DIR_NAMES = {"overworld", "world", "minecraft:overworld"}
NETHER_DIR_NAMES = {"nether", "the_nether", "minecraft:the_nether", "dim-1", "dim-1"}
END_DIR_NAMES = {"end", "the_end", "minecraft:the_end", "dim1"}


class WorldError(NbtError):
    pass


class World:
    def __init__(
        self,
        path: str,
        dimension: str = "overworld",
        acquire_lock: bool = True,
        minecraft_version: Optional[str] = None,
    ):
        self.path = os.path.abspath(path)
        if not os.path.isdir(self.path):
            raise WorldError(f"世界目录不存在: {self.path}")
        self.level_path = os.path.join(self.path, "level.dat")
        if not os.path.isfile(self.level_path):
            raise WorldError(
                f"不是有效的 Minecraft 世界目录 (缺少 level.dat): {self.path}\n"
                "提示: 请指向包含 level.dat 的存档文件夹 (例如 .minecraft/saves/World)。"
            )
        self.lock: Optional[DirectoryLock] = None
        if acquire_lock:
            self.lock = DirectoryLock(self.path)
        try:
            self.level: LevelData = LevelData.load(self.level_path)
            # 版本识别 + 受支持版本选择（失败会释放锁并抛出，绝不静默猜测）
            self.detected_version: DetectedVersion = detect_level_version(self.level)
            self.version_profile: VersionProfile
            self.version_auto: bool
            self.version_profile, self.version_auto = resolve_version(
                self.detected_version, minecraft_version
            )
        except Exception:
            if self.lock:
                self.lock.release()
            raise

        self.version_override = not self.version_auto
        self.version_warnings: list[str] = []
        if self.version_override and not self.detected_version.matches(self.version_profile):
            self.version_warnings.append(
                f"检测到 Minecraft {self.detected_version.display}，"
                f"但按用户选择的受支持版本 {self.version_profile.id} 的格式处理；"
                "两者版本不一致，写入有风险（工具会在覆盖前自动备份）。"
            )

        self.dimension = self.normalize_dimension(dimension)
        self._regions: dict[tuple[str, int, int], RegionFile] = {}
        self._chunks: dict[tuple[str, int, int], Chunk] = {}
        self._dirty_chunks: set[tuple[str, int, int]] = set()
        self._dirty_level = False
        self._dirty_players: dict[str, PlayerData] = {}
        self._saved = False
        self.backup_root = os.path.join(
            self.path, ".mctool_backups", time.strftime("%Y%m%d-%H%M%S")
        )
        self.backups: list[str] = []
        self._backup_counter = 0

        # 单人存档: 主机玩家数据以 level.dat 的 Data.Player 为准（见 PlayerList.load）,
        # 这里尝试匹配对应的 playerdata 文件，保存时同步回 level.dat。
        self.host_player_uuid: Optional[str] = None
        self.host_player_warning: Optional[str] = None
        self._detect_singleplayer_host()

    # ------------------------------------------------------------------ #
    # 维度路径
    # ------------------------------------------------------------------ #
    @staticmethod
    def normalize_dimension(dimension: Optional[str]) -> str:
        if not dimension:
            return "overworld"
        low = dimension.strip().lower()
        if low in OVERWORLD_DIR_NAMES:
            return "overworld"
        if low in NETHER_DIR_NAMES:
            return "nether"
        if low in END_DIR_NAMES:
            return "end"
        return low

    def dimension_path(self, dimension: Optional[str] = None) -> str:
        dim = self.normalize_dimension(dimension or self.dimension)
        if dim == "overworld":
            return self.path
        if dim == "nether":
            return os.path.join(self.path, "DIM-1")
        if dim == "end":
            return os.path.join(self.path, "DIM1")
        if ":" in dim:
            namespace, sub = dim.split(":", 1)
            return os.path.join(self.path, "dimensions", namespace, sub)
        raise WorldError(f"未知维度: {dimension!r} (overworld/nether/end 或 namespace:path)")

    def region_dir(self, dimension: Optional[str] = None) -> str:
        return os.path.join(self.dimension_path(dimension), "region")

    def entity_region_dir(self, dimension: Optional[str] = None) -> str:
        return os.path.join(self.dimension_path(dimension), "entities")

    # ------------------------------------------------------------------ #
    # Region / Chunk
    # ------------------------------------------------------------------ #
    def region_for_chunk(
        self, cx: int, cz: int, dimension: Optional[str] = None, create: bool = False
    ) -> Optional[RegionFile]:
        dim = self.normalize_dimension(dimension or self.dimension)
        rx, rz = cx >> 5, cz >> 5
        key = (dim, rx, rz)
        if key in self._regions:
            return self._regions[key]
        path = region_file_path(self.region_dir(dim), cx, cz)
        if not os.path.isfile(path) and not create:
            return None
        if create:
            os.makedirs(self.region_dir(dim), exist_ok=True)
        rf = RegionFile(path)
        self._regions[key] = rf
        return rf

    def get_chunk(
        self, cx: int, cz: int, dimension: Optional[str] = None
    ) -> Optional[Chunk]:
        dim = self.normalize_dimension(dimension or self.dimension)
        key = (dim, cx, cz)
        if key in self._chunks:
            return self._chunks[key]
        rf = self.region_for_chunk(cx, cz, dim)
        if rf is None:
            return None
        tag = rf.read_chunk(cx, cz)
        if tag is None:
            return None
        chunk = Chunk(tag, cx, cz)
        self._chunks[key] = chunk
        return chunk

    def require_chunk(self, cx: int, cz: int, dimension: Optional[str] = None) -> Chunk:
        chunk = self.get_chunk(cx, cz, dimension)
        if chunk is None:
            dim = self.normalize_dimension(dimension or self.dimension)
            raise WorldError(
                f"区块 ({cx},{cz}) 在维度 {dim} 中不存在 (可能尚未生成)。"
                "请确认坐标正确, 或先在游戏内到达该位置生成区块。"
            )
        return chunk

    def mark_chunk_dirty(self, chunk: Chunk, dimension: Optional[str] = None) -> None:
        dim = self.normalize_dimension(dimension or self.dimension)
        self._dirty_chunks.add((dim, chunk.cx, chunk.cz))

    # ------------------------------------------------------------------ #
    # 高层方块操作
    # ------------------------------------------------------------------ #
    def get_block(self, x: int, y: int, z: int, dimension: Optional[str] = None):
        chunk = self.require_chunk(x >> 4, z >> 4, dimension)
        return chunk.get_block(x, y, z)

    def set_block(
        self, x: int, y: int, z: int, state, dimension: Optional[str] = None,
        keep_block_entity: bool = False, recalc_light: bool = True,
    ) -> str:
        dim = self.normalize_dimension(dimension or self.dimension)
        chunk = self.require_chunk(x >> 4, z >> 4, dim)
        old = chunk.get_block(x, y, z)
        chunk.set_block(x, y, z, state, keep_block_entity=keep_block_entity)
        if recalc_light:
            chunk.mark_needs_relight()
        self._dirty_chunks.add((dim, chunk.cx, chunk.cz))
        return f"{old} -> {state}"

    def fill_blocks(
        self, x1: int, y1: int, z1: int, x2: int, y2: int, z2: int, state,
        dimension: Optional[str] = None, keep_block_entity: bool = False,
        recalc_light: bool = True,
    ) -> int:
        dim = self.normalize_dimension(dimension or self.dimension)
        chunks: dict[tuple[int, int], Chunk] = {}
        count = 0
        for y in range(min(y1, y2), max(y1, y2) + 1):
            for z in range(min(z1, z2), max(z1, z2) + 1):
                for x in range(min(x1, x2), max(x1, x2) + 1):
                    key = (x >> 4, z >> 4)
                    if key not in chunks:
                        chunks[key] = self.require_chunk(key[0], key[1], dim)
                    chunks[key].set_block(x, y, z, state, keep_block_entity=keep_block_entity)
                    count += 1
        for chunk in chunks.values():
            if recalc_light:
                chunk.mark_needs_relight()
            self._dirty_chunks.add((dim, chunk.cx, chunk.cz))
        return count

    def find_blocks(
        self,
        block_spec,
        dimension: Optional[str] = None,
        limit: int = 32767,
        display_limit: int = 256,
        progress=None,
        cancel_event=None,
    ):
        """按方块注册名（可带 [properties]）反向查找当前维度所有区块中的方块。

        返回 :class:`mctool.search.BlockSearchResult`；扫描上限默认 32767（SHORT_MAX），
        坐标最多收集 ``display_limit``（默认 256）个。
        """
        from .search import find_blocks

        return find_blocks(
            self,
            block_spec,
            dimension=dimension,
            limit=limit,
            display_limit=display_limit,
            progress=progress,
            cancel_event=cancel_event,
        )

    def find_block_entities(
        self,
        be_spec: str,
        dimension: Optional[str] = None,
        limit: int = 32767,
        display_limit: int = 256,
        progress=None,
        cancel_event=None,
    ):
        """按方块实体类型 id 反向查找当前维度所有区块中的方块实体。

        返回 :class:`mctool.search.BlockEntitySearchResult`；上限规则与
        :meth:`find_blocks` 相同（32767 匹配 / 256 个显示坐标）。
        """
        from .search import find_block_entities

        return find_block_entities(
            self,
            be_spec,
            dimension=dimension,
            limit=limit,
            display_limit=display_limit,
            progress=progress,
            cancel_event=cancel_event,
        )

    # ------------------------------------------------------------------ #
    # 玩家
    # ------------------------------------------------------------------ #
    @property
    def player_dir(self) -> str:
        return os.path.join(self.path, "playerdata")

    def list_players(self) -> list[PlayerData]:
        result: list[PlayerData] = []
        if not os.path.isdir(self.player_dir):
            return result
        for path in sorted(glob.glob(os.path.join(self.player_dir, "*.dat"))):
            if path.endswith("_old.dat"):
                continue
            try:
                result.append(PlayerData.load(path))
            except Exception as exc:  # 单个损坏文件不应影响其他
                result.append(
                    PlayerData(
                        path,
                        CompoundTag(),
                        os.path.splitext(os.path.basename(path))[0],
                    )
                )
                result[-1]._load_error = str(exc)  # type: ignore[attr-defined]
        return result

    def get_player(self, uuid_or_name: str) -> PlayerData:
        wanted = uuid_or_name.strip().lower()
        if os.path.isdir(self.player_dir):
            for path in glob.glob(os.path.join(self.player_dir, "*.dat")):
                if path.endswith("_old.dat"):
                    continue
                stem = os.path.splitext(os.path.basename(path))[0].lower()
                if stem == wanted or stem.replace("-", "") == wanted.replace("-", ""):
                    return PlayerData.load(path)
        raise WorldError(f"找不到玩家数据: {uuid_or_name} (目录 {self.player_dir})")

    def mark_level_dirty(self) -> None:
        self._dirty_level = True

    def mark_player_dirty(self, player: PlayerData) -> None:
        self._dirty_players[player.path] = player

    # ------------------------------------------------------------------ #
    # 单人存档主机玩家快照（level.dat/Data.Player）
    # ------------------------------------------------------------------ #
    def host_player_tag(self) -> Optional[CompoundTag]:
        """level.dat 中的单人主机玩家快照（Data.Player）；无则返回 None。"""
        raw = self.level.data.get("Player")
        return raw if isinstance(raw, CompoundTag) else None

    @staticmethod
    def _player_pos(tag: CompoundTag):
        pos = tag.get("Pos")
        if isinstance(pos, list) and len(pos) >= 3:
            try:
                return (float(pos[0]), float(pos[1]), float(pos[2]))
            except (TypeError, ValueError):
                return None
        return None

    def _detect_singleplayer_host(self) -> None:
        """识别单人主机玩家对应的 playerdata UUID。

        依据: PlayerList.load 在单人存档中优先使用 level.dat 的 loadedPlayerTag；
        而 PlayerDataStorage 同时保存 playerdata/<uuid>.dat。编辑玩家数据时必须两份都更新。
        """
        self.host_player_uuid = None
        self.host_player_warning = None
        host = self.host_player_tag()
        if host is None:
            return
        candidates = [
            p for p in self.list_players()
            if isinstance(getattr(p, "tag", None), CompoundTag)
            and not getattr(p, "_load_error", None)
        ]
        if not candidates:
            self.host_player_warning = (
                "检测到单人主机玩家快照（level.dat/Data.Player），但没有可读的 playerdata 文件；"
                "GUI 可能无法列出主机玩家。"
            )
            return
        if len(candidates) == 1:
            self.host_player_uuid = candidates[0].uuid
            return

        host_seed = host.get_int("XpSeed", -1)
        if host_seed >= 0:
            matches = [
                p for p in candidates
                if p.tag.get_int("XpSeed", -2) == host_seed
            ]
            if len(matches) == 1:
                self.host_player_uuid = matches[0].uuid
                return

        # 多玩家时用若干稳定字段打分匹配 host 快照
        host_xp = host.get_int("XpLevel", -1)
        host_total = host.get_int("XpTotal", -1)
        host_food = host.get_int("foodLevel", -1)
        host_inv = len(host.get_list("Inventory"))
        host_pos = self._player_pos(host)

        scored: list[tuple[int, str]] = []
        for player in candidates:
            tag = player.tag
            score = 0
            if host_seed >= 0 and tag.get_int("XpSeed", -2) == host_seed:
                score += 100
            if host_xp >= 0 and tag.get_int("XpLevel", -2) == host_xp:
                score += 20
            if host_total >= 0 and tag.get_int("XpTotal", -2) == host_total:
                score += 20
            if host_food >= 0 and tag.get_int("foodLevel", -2) == host_food:
                score += 5
            if len(tag.get_list("Inventory")) == host_inv and host_inv > 0:
                score += 5
            pos = self._player_pos(tag)
            if pos is not None and host_pos is not None:
                if all(abs(a - b) < 1.0 for a, b in zip(pos, host_pos)):
                    score += 10
            scored.append((score, player.uuid))
        scored.sort(reverse=True)
        if scored and scored[0][0] >= 50 and (
            len(scored) == 1 or scored[0][0] > scored[1][0]
        ):
            self.host_player_uuid = scored[0][1]
        else:
            self.host_player_warning = (
                "检测到单人主机玩家快照，但有多个 playerdata 文件且无法唯一匹配主机玩家；"
                "编辑 playerdata 可能不会在单人游戏中生效。"
            )

    def reload_level(self) -> None:
        """从磁盘重新读取 level.dat，并重新识别单人主机快照。"""
        self.level = LevelData.load(self.level_path)
        self._detect_singleplayer_host()

    def _sync_host_snapshot(self, player: PlayerData) -> bool:
        """把玩家数据同步到 level.dat/Data.Player（若该玩家是单人主机）。"""
        if not self.host_player_uuid or player.uuid != self.host_player_uuid:
            return False
        if self.host_player_tag() is None:
            return False
        self.level.data["Player"] = player.tag.copy()
        self._dirty_level = True
        return True

    # ------------------------------------------------------------------ #
    # 任意 NBT 文件
    # ------------------------------------------------------------------ #
    def resolve_world_file(self, relative_or_abs: str) -> str:
        candidate = relative_or_abs
        if not os.path.isabs(candidate):
            candidate = os.path.join(self.path, candidate)
        candidate = os.path.abspath(candidate)
        root = self.path + os.sep
        if candidate != self.path and not candidate.startswith(root):
            raise WorldError(f"文件不在世界目录内, 已拒绝: {candidate}")
        return candidate

    def read_dat(self, relative_or_abs: str) -> tuple[str, CompoundTag]:
        path = self.resolve_world_file(relative_or_abs)
        if not os.path.isfile(path):
            raise WorldError(f"文件不存在: {path}")
        name, tag = read_nbt_file(path)
        if not isinstance(tag, CompoundTag):
            raise WorldError(f"根 tag 不是 TAG_Compound: {path}")
        return name, tag

    def write_dat(self, relative_or_abs: str, tag: CompoundTag) -> None:
        path = self.resolve_world_file(relative_or_abs)
        if not os.path.isfile(path):
            raise WorldError(f"文件不存在 (本工具不创建新存档文件): {path}")
        self.backup_file(path)
        raw = write_nbt(tag, "", "gzip")
        temp = path + ".mctool.tmp"
        with open(temp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)

    # ------------------------------------------------------------------ #
    # 备份
    # ------------------------------------------------------------------ #
    def backup_file(self, path: str) -> str:
        if not os.path.isfile(path):
            return ""
        rel = os.path.relpath(path, self.path)
        target = os.path.join(self.backup_root, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            self._backup_counter += 1
            target = f"{target}.{self._backup_counter}"
        shutil.copy2(path, target)
        self.backups.append(target)
        return target

    def backup_world(self, include_regions: bool = True) -> str:
        """把 level.dat、playerdata、region 等复制到备份目录 (不包含 session.lock)。"""
        stamp = os.path.join(self.backup_root, "world")
        os.makedirs(stamp, exist_ok=True)
        for name in ("level.dat", "level.dat_old"):
            src = os.path.join(self.path, name)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(stamp, name))
        for sub in ("playerdata", "region", "entities", "data"):
            src_dir = os.path.join(self.path, sub)
            if os.path.isdir(src_dir):
                shutil.copytree(
                    src_dir,
                    os.path.join(stamp, sub),
                    ignore=shutil.ignore_patterns("*.mctool.tmp", "session.lock"),
                    dirs_exist_ok=True,
                )
        self.backups.append(stamp)
        return stamp

    # ------------------------------------------------------------------ #
    # 保存
    # ------------------------------------------------------------------ #
    def flush(self) -> None:
        """写入所有脏区块 / region / level.dat / 玩家数据, 并在覆盖前备份。"""
        # 0) 收集脏区块: 显式标记的 + 缓存中已 modified 的
        dirty = set(self._dirty_chunks)
        for key, chunk in self._chunks.items():
            if chunk.modified:
                dirty.add(key)
        # 1) 区块 -> region
        touched_regions: dict[tuple[str, int, int], RegionFile] = {}
        for (dim, cx, cz) in sorted(dirty):
            chunk = self._chunks.get((dim, cx, cz))
            if chunk is None or not chunk.modified:
                continue
            rf = self.region_for_chunk(cx, cz, dim, create=True)
            assert rf is not None
            tag = chunk.finalize()
            rf.write_chunk(cx, cz, tag)
            chunk.modified = False
            touched_regions[(dim, cx >> 5, cz >> 5)] = rf
        # 2) region 文件落盘
        for (dim, rx, rz), rf in touched_regions.items():
            if not rf.dirty:
                continue
            if os.path.isfile(rf.path):
                self.backup_file(rf.path)
            rf.save()
        self._dirty_chunks.clear()
        # 3) 玩家数据（先于 level.dat：单人主机快照需要把修改同步进去）
        for path, player in list(self._dirty_players.items()):
            if os.path.isfile(path):
                self.backup_file(path)
            player.save(path)
            self._sync_host_snapshot(player)
        self._dirty_players.clear()
        # 4) level.dat
        if self._dirty_level:
            self.backup_file(self.level_path)
            self.level.save()
            self._dirty_level = False
        self._saved = True

    def close(self) -> None:
        try:
            if not self._saved and (self._dirty_chunks or self._dirty_level or self._dirty_players):
                # 未显式 save 的修改不应静默丢失; 由调用方负责。这里只释放锁。
                pass
        finally:
            if self.lock is not None:
                self.lock.release()
                self.lock = None

    def __enter__(self) -> "World":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 信息 / 验证
    # ------------------------------------------------------------------ #
    def list_dimensions(self) -> list[str]:
        dims = ["overworld"]
        if os.path.isdir(os.path.join(self.path, "DIM-1")):
            dims.append("nether")
        if os.path.isdir(os.path.join(self.path, "DIM1")):
            dims.append("end")
        custom_root = os.path.join(self.path, "dimensions")
        if os.path.isdir(custom_root):
            for ns in sorted(os.listdir(custom_root)):
                ns_dir = os.path.join(custom_root, ns)
                if not os.path.isdir(ns_dir):
                    continue
                for sub in sorted(os.listdir(ns_dir)):
                    if os.path.isdir(os.path.join(ns_dir, sub)):
                        dims.append(f"{ns}:{sub}")
        return dims

    def region_stats(self, dimension: Optional[str] = None) -> dict:
        region_dir = self.region_dir(dimension)
        files = sorted(glob.glob(os.path.join(region_dir, "*.mca")))
        total = 0
        for f in files:
            total += os.path.getsize(f)
        return {"directory": region_dir, "files": len(files), "bytes": total}

    def info(self) -> dict:
        return {
            "world_dir": self.path,
            "level": self.level.summary(),
            "version": {
                "detected": self.detected_version.to_dict(),
                "selected": self.version_profile.id,
                "selected_profile": self.version_profile.to_dict(),
                "auto": self.version_auto,
                "override": self.version_override,
                "warnings": list(self.version_warnings),
                "supported": supported_versions_summary(),
            },
            "dimensions": {
                dim: self.region_stats(dim) for dim in self.list_dimensions()
            },
            "players": len([p for p in self.list_players() if hasattr(p, "tag")]),
            "host_player": {
                "singleplayer_snapshot": self.host_player_tag() is not None,
                "uuid": self.host_player_uuid,
                "warning": self.host_player_warning,
            },
            "locked_by_tool": self.lock is not None,
            "backup_root": self.backup_root,
        }

    def verify(self, deep: bool = False) -> dict:
        expected_data_version = self.version_profile.data_version
        report = {
            "ok": True,
            "errors": [],
            "warnings": list(self.version_warnings),
            "dimensions": {},
            "players": {},
            "level": {},
            "version": {
                "detected": self.detected_version.to_dict(),
                "selected": self.version_profile.id,
                "selected_profile": self.version_profile.to_dict(),
                "override": self.version_override,
            },
        }
        level = self.level
        dv = level.data.get_int("DataVersion", -1)
        report["level"] = {
            "data_version": dv,
            "level_name": level.data.get_string("LevelName"),
            "sections_checked": 0,
        }
        if dv != expected_data_version:
            report["warnings"].append(
                f"level.dat 的 DataVersion={dv} 与所选处理版本 "
                f"{self.version_profile.id}（DataVersion={expected_data_version}）不一致，可能有差异"
            )
        for dim in self.list_dimensions():
            dim_report = {"regions": 0, "chunks": 0, "errors": 0}
            region_dir = self.region_dir(dim)
            for path in sorted(glob.glob(os.path.join(region_dir, "*.mca"))):
                dim_report["regions"] += 1
                try:
                    rf = RegionFile(path)
                    for (cx, cz) in rf.list_chunks():
                        try:
                            chunk = self.get_chunk(cx, cz, dim)
                            if chunk is None:
                                dim_report["errors"] += 1
                                report["errors"].append(f"{dim} 区块({cx},{cz}) 读取为空")
                                continue
                            dim_report["chunks"] += 1
                            if chunk.data_version != expected_data_version:
                                report["warnings"].append(
                                    f"{dim} 区块({cx},{cz}) DataVersion={chunk.data_version}"
                                )
                            if deep:
                                for section in chunk.iter_sections():
                                    _ = section.blocks, section.biomes
                                    report["level"]["sections_checked"] += 1
                                for be in chunk.iter_block_entities():
                                    if "id" not in be:
                                        report["errors"].append(
                                            f"{dim} 区块({cx},{cz}) 有方块实体缺少 id"
                                        )
                                        dim_report["errors"] += 1
                                    bx, by, bz = be.get_int("x"), be.get_int("y"), be.get_int("z")
                                    if (bx >> 4, bz >> 4) != (cx, cz):
                                        report["errors"].append(
                                            f"{dim} 区块({cx},{cz}) 方块实体坐标 ({bx},{by},{bz}) 不在区块内"
                                        )
                                        dim_report["errors"] += 1
                        except (RegionError, ChunkError, PaletteError, NbtError) as exc:
                            dim_report["errors"] += 1
                            report["errors"].append(f"{dim} 区块({cx},{cz}) 解析失败: {exc}")
                except RegionError as exc:
                    dim_report["errors"] += 1
                    report["errors"].append(f"{dim} region 文件失败 {path}: {exc}")
            report["dimensions"][dim] = dim_report
        if os.path.isdir(self.player_dir):
            for path in sorted(glob.glob(os.path.join(self.player_dir, "*.dat"))):
                try:
                    p = PlayerData.load(path)
                    s = p.summary()
                    report["players"][p.uuid] = {
                        "dimension": s["dimension"],
                        "game_type": s["game_type"],
                        "data_version": s["data_version"],
                    }
                except Exception as exc:
                    report["players"][os.path.basename(path)] = {"error": str(exc)}
        report["ok"] = not report["errors"]
        return report


def find_saves_dir() -> Optional[str]:
    """尽量定位默认 .minecraft/saves 目录 (仅用于提示, 不自动修改)。"""
    candidates = [
        os.path.expandvars(r"%APPDATA%\.minecraft\saves"),
        os.path.expanduser("~/.minecraft/saves"),
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return os.path.abspath(c)
    return None
