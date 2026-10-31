"""mctool - Minecraft Java 存档读取/修改工具。

设计目标:
  - 只操作 "未被游戏占用" 的世界 (通过 session.lock 独占锁保证)
  - 自动识别游戏版本: 受支持版本直接使用对应处理逻辑; 否则由用户选择
    (默认版本号最近的受支持版本), 见 mctool.versions
  - 支持读取/修改 level.dat、玩家数据、区块方块、方块实体
  - 修改前自动备份, 写入使用临时文件 + 原子替换
  - 纯 Python 标准库实现 (无第三方依赖)

模块:
  nbt      NBT 二进制格式 + SNBT + NBT 路径
  region   Anvil region (.mca) 文件
  palette  调色板容器 / 位打包 / 方块状态
  chunk    区块、section、方块、方块实体
  level    level.dat
  player   playerdata/<uuid>.dat
  items    物品栏 ItemStack / 槽位（1.20.1 tag 与 1.21.1 components）
  curios   Curios 模组玩家饰品栏（neoforge:attachments → curios:inventory）
  lock     session.lock 占用检测
  versions 版本识别 / 受支持版本注册表 / 最近版本选择
  world    世界目录、维度、备份、校验
  gui      Tkinter 图形界面（唯一产品界面）
"""

__version__ = "1.7.0"
__target_minecraft__ = "1.21.1"
__target_data_version__ = 3955

from .chunk import Chunk, ChunkError, Section
from .level import LevelData
from .nbt import (
    CompoundTag,
    ListTag,
    NbtError,
    parse_snbt,
    read_nbt,
    to_snbt,
    to_snbt_pretty,
    write_nbt,
)
from .palette import BlockState, PalettedContainer
from .player import PlayerData
from .region import RegionFile
from .versions import (
    SUPPORTED_VERSIONS,
    DetectedVersion,
    UnsupportedVersionError,
    VersionProfile,
    detect_world_version,
    nearest_supported_version,
    resolve_version,
)
from .world import World

__all__ = [
    "Chunk",
    "ChunkError",
    "Section",
    "LevelData",
    "CompoundTag",
    "ListTag",
    "NbtError",
    "parse_snbt",
    "read_nbt",
    "to_snbt",
    "to_snbt_pretty",
    "write_nbt",
    "BlockState",
    "PalettedContainer",
    "PlayerData",
    "RegionFile",
    "World",
    "SUPPORTED_VERSIONS",
    "DetectedVersion",
    "UnsupportedVersionError",
    "VersionProfile",
    "detect_world_version",
    "nearest_supported_version",
    "resolve_version",
    "__version__",
]
