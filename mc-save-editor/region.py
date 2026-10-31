"""Minecraft Anvil region 文件 (.mca) 读写。

对应 Minecraft 1.21.1 源码:
  mc_src/net/minecraft/world/level/chunk/storage/RegionFile.java
  mc_src/net/minecraft/world/level/chunk/storage/RegionFileVersion.java
  mc_src/net/minecraft/world/level/chunk/storage/RegionFileStorage.java
  mc_src/net/minecraft/world/level/chunk/storage/IOWorker.java

格式:
  文件 = 8KiB 头部 + 若干 4KiB 扇区
  头部:
    0..4095     1024 个 location int (大端):
                sector_offset(高 24 位) << 8 | sector_count(低 8 位); 0 表示无区块
    4096..8191  1024 个 timestamp int (秒)
  扇区 i 位于文件偏移 i*4096。
  区块数据块:
    [u32 length][u8 compression][payload]  + 填充到 4KiB 边界
    length = payload.length + 1 (包含 compression 字节)
    compression: 1=gzip, 2=zlib(默认), 3=未压缩, 4=lz4
    若 compression 最高位为 1 (即 +128), 则 payload 存放在
    同目录 c.<chunkX>.<chunkZ>.mcc 文件中, 本文件只保留 5 字节存根
    (length=1, version|128)。
    sector_count >= 256 时写外置文件。单个区块最多 255 个内部扇区。

本实现为了安全与原子性, 修改后整文件重建 (未修改区块的压缩字节原样保留)。
"""

from __future__ import annotations

import gzip
import os
import struct
import zlib
from dataclasses import dataclass, field
from typing import Optional

from .nbt import CompoundTag, NbtError, read_nbt, write_nbt

SECTOR_BYTES = 4096
HEADER_BYTES = 8192
CHUNK_HEADER_BYTES = 5
MAX_INTERNAL_SECTORS = 255
EXTERNAL_STREAM_FLAG = 128
EXTERNAL_CHUNK_THRESHOLD = 256  # >=256 扇区时外置 (RegionFile.java:318)
EXTERNAL_EXT = ".mcc"

COMPRESSION_GZIP = 1
COMPRESSION_ZLIB = 2
COMPRESSION_NONE = 3
COMPRESSION_LZ4 = 4

COMPRESSION_NAMES = {
    COMPRESSION_GZIP: "gzip",
    COMPRESSION_ZLIB: "zlib",
    COMPRESSION_NONE: "none",
    COMPRESSION_LZ4: "lz4",
}


class RegionError(NbtError):
    """Region 文件损坏或不支持。"""


class UnsupportedCompression(RegionError):
    pass


@dataclass
class _Entry:
    """一个区块在文件中的原始 (压缩后) 数据。"""

    compressed: bytes
    version: int
    timestamp: int
    external: bool = False

    def sector_count(self) -> int:
        payload_len = CHUNK_HEADER_BYTES + len(self.compressed)
        return (payload_len + SECTOR_BYTES - 1) // SECTOR_BYTES


class _DeletedType:
    def __repr__(self) -> str:
        return "<deleted>"


DELETED = _DeletedType()


def _now_ts() -> int:
    return int(__import__("time").time())


def _sector_count_for(nbytes: int) -> int:
    return (nbytes + SECTOR_BYTES - 1) // SECTOR_BYTES


def decompress_payload(compressed: bytes, version: int) -> bytes:
    if version == COMPRESSION_GZIP:
        return gzip.decompress(compressed)
    if version == COMPRESSION_ZLIB:
        return zlib.decompress(compressed)
    if version == COMPRESSION_NONE:
        return compressed
    if version == COMPRESSION_LZ4:
        raise UnsupportedCompression(
            "该区块使用 LZ4 压缩 (region-file-compression=lz4), 本工具暂不支持。"
            "请在 server.properties 中把 region-file-compression 改回 deflate 让游戏重新保存, "
            "或使用原版客户端另存后再编辑。"
        )
    raise RegionError(f"未知区块压缩类型: {version}")


def compress_payload(raw: bytes, version: int = COMPRESSION_ZLIB) -> bytes:
    if version == COMPRESSION_GZIP:
        return gzip.compress(raw, mtime=0)
    if version == COMPRESSION_ZLIB:
        return zlib.compress(raw, 6)
    if version == COMPRESSION_NONE:
        return raw
    if version == COMPRESSION_LZ4:
        raise UnsupportedCompression("本工具不支持写出 LZ4 压缩的区块")
    raise RegionError(f"未知区块压缩类型: {version}")


class RegionFile:
    """单个 r.<rx>.<rz>.mca 文件。

    使用方式:
        rf = RegionFile(path)
        tag = rf.read_chunk(cx, cz)       # 绝对区块坐标
        rf.write_chunk(cx, cz, tag)
        rf.save()                          # 备份由上层 World 负责
    """

    def __init__(self, path: str, compression: int = COMPRESSION_ZLIB):
        self.path = str(path)
        self.region_dir = os.path.dirname(os.path.abspath(self.path))
        self.compression = compression
        stem = os.path.basename(self.path)
        if stem.endswith(".mca"):
            parts = stem[:-4].split(".")
            if len(parts) != 3 or parts[0] != "r":
                raise RegionError(f"非法的 region 文件名: {stem}")
            try:
                self.region_x = int(parts[1])
                self.region_z = int(parts[2])
            except ValueError as exc:
                raise RegionError(f"非法的 region 文件名: {stem}") from exc
        else:
            raise RegionError(f"非 region 文件: {stem}")

        self._locations: dict[tuple[int, int], _Entry] = {}
        self._timestamps: dict[tuple[int, int], int] = {}
        self._changes: dict[tuple[int, int], object] = {}
        self.warnings: list[str] = []
        self._loaded = False

    # ------------------------------------------------------------------ #
    # 头部解析
    # ------------------------------------------------------------------ #
    def load(self) -> "RegionFile":
        if self._loaded:
            return self
        self._loaded = True
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            # 新 region, 空文件
            return self
        if size == 0:
            return self
        if size < HEADER_BYTES:
            raise RegionError(f"region 文件头部不完整 ({size} < 8192 字节): {self.path}")
        with open(self.path, "rb") as f:
            header = f.read(HEADER_BYTES)
        if len(header) != HEADER_BYTES:
            raise RegionError(f"region 文件头部读取失败: {self.path}")
        locs = struct.unpack(">1024i", header[:SECTOR_BYTES])
        timestamps = struct.unpack(">1024i", header[SECTOR_BYTES:HEADER_BYTES])
        for index in range(1024):
            loc = locs[index]
            if loc == 0:
                continue
            sector = (loc >> 8) & 0xFFFFFF
            count = loc & 0xFF
            cx = self.region_x * 32 + (index % 32)
            cz = self.region_z * 32 + (index // 32)
            if sector < 2 or count == 0 or sector * SECTOR_BYTES >= size:
                self.warnings.append(
                    f"忽略非法区块头: 区块({cx},{cz}) sector={sector} count={count} (文件 {size} 字节)"
                )
                continue
            self._locations[(cx, cz)] = _Entry(
                compressed=b"",  # 惰性读取
                version=0,
                timestamp=timestamps[index],
                external=False,
            )
            self._timestamps[(cx, cz)] = timestamps[index]
            # 记录原始的 location 信息需要重新读 header, 保存到额外字典
        # 记录原始 loc/count, 供读取扇区
        self._raw_locs = {(cx, cz): locs[(cz & 31) * 32 + (cx & 31)]
                          for (cx, cz) in self._locations}
        return self

    def list_chunks(self) -> list[tuple[int, int]]:
        """返回文件中存在的区块坐标 (绝对坐标, 排序)。"""
        self.load()
        keys = set(self._locations)
        keys.update(k for k, v in self._changes.items() if v is not DELETED)
        return sorted(keys, key=lambda c: (c[1], c[0]))

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def _read_entry(self, cx: int, cz: int) -> Optional[_Entry]:
        self._check_chunk_in_region(cx, cz)
        self.load()
        if (cx, cz) in self._changes:
            entry = self._changes[(cx, cz)]
            return None if entry is DELETED else entry  # type: ignore[return-value]
        entry = self._locations.get((cx, cz))
        if entry is None:
            return None
        if entry.compressed:
            return entry
        # 从文件读取原始数据
        loc = self._raw_locs[(cx, cz)]
        sector = (loc >> 8) & 0xFFFFFF
        count = loc & 0xFF
        with open(self.path, "rb") as f:
            f.seek(sector * SECTOR_BYTES)
            data = f.read(count * SECTOR_BYTES)
        if len(data) < CHUNK_HEADER_BYTES:
            raise RegionError(f"区块 ({cx},{cz}) 数据块不完整")
        length, version_byte = struct.unpack(">iB", data[:5])
        external = (version_byte & EXTERNAL_STREAM_FLAG) != 0
        version = version_byte & 0x7F
        if external:
            mcc = self._external_path(cx, cz)
            if not os.path.isfile(mcc):
                raise RegionError(f"区块 ({cx},{cz}) 声明外置存储但找不到 {mcc}")
            with open(mcc, "rb") as ef:
                compressed = ef.read()
        else:
            if length < 1:
                raise RegionError(f"区块 ({cx},{cz}) length 非法: {length}")
            payload_len = length - 1
            if CHUNK_HEADER_BYTES + payload_len > len(data):
                raise RegionError(
                    f"区块 ({cx},{cz}) 数据截断: 需要 {payload_len} 字节, 实际 {len(data) - 5}"
                )
            compressed = data[CHUNK_HEADER_BYTES:CHUNK_HEADER_BYTES + payload_len]
        entry.compressed = compressed
        entry.version = version
        entry.external = external
        return entry

    def read_chunk_bytes(self, cx: int, cz: int) -> Optional[tuple[bytes, int]]:
        """返回 (解压后的 NBT 字节, 压缩类型)。"""
        entry = self._read_entry(cx, cz)
        if entry is None:
            return None
        return decompress_payload(entry.compressed, entry.version), entry.version

    def read_chunk(self, cx: int, cz: int) -> Optional[CompoundTag]:
        data = self.read_chunk_bytes(cx, cz)
        if data is None:
            return None
        raw, _version = data
        name, tag = read_nbt(raw, decompress=None)
        if not isinstance(tag, CompoundTag):
            raise RegionError(f"区块 ({cx},{cz}) 的根 tag 不是 TAG_Compound")
        return tag

    # ------------------------------------------------------------------ #
    # 写入 (先记录, save() 时统一重建)
    # ------------------------------------------------------------------ #
    def write_chunk(self, cx: int, cz: int, tag: CompoundTag, timestamp: Optional[int] = None) -> None:
        self._check_chunk_in_region(cx, cz)
        raw = write_nbt(tag, "", None)
        compressed = compress_payload(raw, self.compression)
        self._changes[(cx, cz)] = _Entry(
            compressed=compressed,
            version=self.compression,
            timestamp=_now_ts() if timestamp is None else timestamp,
        )

    def delete_chunk(self, cx: int, cz: int) -> None:
        self._check_chunk_in_region(cx, cz)
        self.load()
        if (cx, cz) in self._locations or (cx, cz) in self._changes:
            self._changes[(cx, cz)] = DELETED

    @property
    def dirty(self) -> bool:
        return bool(self._changes)

    # ------------------------------------------------------------------ #
    # 保存
    # ------------------------------------------------------------------ #
    def save(self) -> None:
        """重建 region 文件 (原子替换)。所有被修改的压缩数据写入, 其余原样复制。"""
        self.load()
        if not self._changes:
            return
        chunks: list[tuple[int, int]] = sorted(
            set(self._locations) | {k for k, v in self._changes.items() if v is not DELETED},
            key=lambda c: (c[1], c[0]),
        )
        header = bytearray(HEADER_BYTES)
        body = bytearray()
        next_sector = 2
        new_externals: dict[str, bytes] = {}
        keep_externals: set[str] = set()

        for (cx, cz) in chunks:
            entry = self._read_entry(cx, cz)
            if entry is None:
                continue
            # 扇区数按 "5 字节区块头 + 压缩数据" 的总长度计算
            total_len = CHUNK_HEADER_BYTES + len(entry.compressed)
            sectors = _sector_count_for(total_len)
            if sectors >= EXTERNAL_CHUNK_THRESHOLD or sectors > MAX_INTERNAL_SECTORS:
                # 外置存储: 写 .mcc, region 中放 5 字节存根
                name = os.path.basename(self._external_path(cx, cz))
                new_externals[name] = entry.compressed
                keep_externals.add(name)
                stub = struct.pack(">iB", 1, entry.version | EXTERNAL_STREAM_FLAG)
                sectors = 1
                next_sector = self._append(header, cx, cz, next_sector, sectors,
                                           entry.timestamp, stub, body)
            else:
                # 注意: 区块头 length 字段 = 压缩数据长度 + 1 (包含 1 字节压缩类型),
                # 不含 4 字节 length 本身; 参见 RegionFile.ChunkBuffer#close:
                #   i = count - 5 + 1; bytebuffer.putInt(0, i)
                length_field = len(entry.compressed) + 1
                data = struct.pack(">iB", length_field, entry.version) + entry.compressed
                next_sector = self._append(header, cx, cz, next_sector, sectors,
                                           entry.timestamp, data, body)
        total = HEADER_BYTES + len(body)
        if total % SECTOR_BYTES:
            body.extend(b"\x00" * (SECTOR_BYTES - total % SECTOR_BYTES))
        self._write_atomic(bytes(header) + bytes(body), new_externals, keep_externals)

    @staticmethod
    def _append(header: bytearray, cx: int, cz: int, next_sector: int, sectors: int,
                timestamp: int, data: bytes, body: bytearray) -> int:
        index = ((cz & 31) * 32) + (cx & 31)
        loc = (next_sector << 8) | sectors
        struct.pack_into(">i", header, index * 4, loc)
        struct.pack_into(">i", header, SECTOR_BYTES + index * 4, timestamp)
        padded = data + b"\x00" * (sectors * SECTOR_BYTES - len(data))
        body.extend(padded)
        return next_sector + sectors

    def _write_atomic(self, data: bytes, new_externals: dict[str, bytes],
                      keep_externals: set[str]) -> None:
        tmp = self.path + ".mctool.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        # 先写外置文件临时文件
        ext_temps: dict[str, str] = {}
        for name, payload in new_externals.items():
            target = os.path.join(self.region_dir, name)
            temp = target + ".mctool.tmp"
            with open(temp, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            ext_temps[name] = temp
        os.replace(tmp, self.path)
        for name, temp in ext_temps.items():
            os.replace(temp, os.path.join(self.region_dir, name))
        # 清理不再需要的外置文件
        for name in os.listdir(self.region_dir):
            if name.endswith(".mcc") and name.startswith("c."):
                stem = name[:-4].split(".")
                if len(stem) != 3:
                    continue
                try:
                    cx = int(stem[1])
                    cz = int(stem[2])
                except ValueError:
                    continue
                if (cx, cz) in self._locations or (cx, cz) in self._changes:
                    in_region = (cx >> 5, cz >> 5) == (self.region_x, self.region_z)
                    if in_region and name not in keep_externals:
                        try:
                            os.remove(os.path.join(self.region_dir, name))
                        except OSError:
                            pass
        self._locations = {}
        self._timestamps = {}
        self._raw_locs = {}
        self._changes = {}
        self._loaded = False

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    def _check_chunk_in_region(self, cx: int, cz: int) -> None:
        if (cx >> 5) != self.region_x or (cz >> 5) != self.region_z:
            raise RegionError(
                f"区块 ({cx},{cz}) 不属于 region ({self.region_x},{self.region_z})"
            )

    def _external_path(self, cx: int, cz: int) -> str:
        return os.path.join(self.region_dir, f"c.{cx}.{cz}{EXTERNAL_EXT}")

    def __repr__(self) -> str:
        return f"RegionFile({self.path!r}, chunks={len(self.list_chunks())})"


def region_coords_for_chunk(cx: int, cz: int) -> tuple[int, int]:
    """区块坐标 -> region 坐标 (Minecraft 使用向下取整, 与 >> 5 一致)。"""
    return cx >> 5, cz >> 5


def region_file_path(region_dir: str, cx: int, cz: int) -> str:
    rx, rz = region_coords_for_chunk(cx, cz)
    return os.path.join(region_dir, f"r.{rx}.{rz}.mca")
