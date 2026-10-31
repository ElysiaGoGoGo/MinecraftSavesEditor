"""level.dat (世界存档元数据) 读写。

对应 Minecraft 1.21.1 源码:
  mc_src/net/minecraft/world/level/storage/LevelStorageSource.java
  mc_src/net/minecraft/world/level/storage/PrimaryLevelData.java
  mc_src/net/minecraft/world/level/storage/WorldData.java
  mc_src/net/minecraft/server/MinecraftServer.java

文件: <world>/level.dat, gzip 压缩 NBT:
  { Data: { DataVersion:int, Version:{Name,Id,Snapshot,Series},
            LevelName, GameType, hardcore, Difficulty(byte),
            Time(long), DayTime(long), LastPlayed(long),
            SpawnX/Y/Z(int), SpawnAngle(float),
            raining, rainTime, clearWeatherTime, thundering, thunderTime,
            GameRules:{规则名: "值字符串", ...},
            WorldGenSettings:{seed:long, generate_features, dimensions:{...}},
            Player:{...} (单人存档主机玩家快照),
            DataPacks:{Enabled:[...],Disabled:[...]},
            DragonFight:{...}, CustomBossEvents, ScheduledEvents, ... } }

保存时游戏会:
  1. 写临时文件
  2. level.dat -> level.dat_old
  3. 临时文件 -> level.dat      (Util.safeReplaceFile)
本工具采用相同策略。
"""

from __future__ import annotations

import os
import shutil
import time
from typing import Any, Optional

from .nbt import (
    Byte,
    CompoundTag,
    Float,
    Int,
    Long,
    NbtError,
    read_nbt_file,
    to_snbt,
    write_nbt,
)

GAME_TYPES = {
    0: "survival",
    1: "creative",
    2: "adventure",
    3: "spectator",
}
GAME_TYPE_IDS = {v: k for k, v in GAME_TYPES.items()}
DIFFICULTIES = {0: "peaceful", 1: "easy", 2: "normal", 3: "hard"}
DIFFICULTY_IDS = {v: k for k, v in DIFFICULTIES.items()}


class LevelDataError(NbtError):
    pass


class LevelData:
    def __init__(self, path: str, root: CompoundTag):
        self.path = path
        self.root = root
        data = root.get("Data")
        if not isinstance(data, CompoundTag):
            raise LevelDataError(f"level.dat 缺少 Data compound: {path}")
        self.data: CompoundTag = data

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: str) -> "LevelData":
        name, tag = read_nbt_file(path)
        if not isinstance(tag, CompoundTag):
            raise LevelDataError(f"level.dat 根 tag 不是 TAG_Compound: {path}")
        if "Data" not in tag:
            raise LevelDataError(f"level.dat 缺少 Data: {path}")
        return cls(path, tag)

    def save(self, path: Optional[str] = None) -> None:
        """原子写入, 并保留 level.dat_old。"""
        target = path or self.path
        self.data["LastPlayed"] = Long(int(time.time() * 1000))
        raw = write_nbt(self.root, "", "gzip")
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
    def summary(self) -> dict:
        version = self.data.get_compound("Version")
        world_gen = self.data.get_compound("WorldGenSettings")
        game_type = self.data.get_int("GameType", 0)
        difficulty = self.data.get_byte("Difficulty", 2)
        return {
            "level_name": self.data.get_string("LevelName"),
            "data_version": self.data.get_int("DataVersion", -1),
            "version": {
                "name": version.get_string("Name"),
                "id": version.get_int("Id", -1),
                "snapshot": version.get_boolean("Snapshot"),
                "series": version.get_string("Series"),
            },
            "last_played": self.data.get_int("LastPlayed", 0),
            "game_type": GAME_TYPES.get(game_type, str(game_type)),
            "difficulty": DIFFICULTIES.get(difficulty, str(difficulty)),
            "hardcore": self.data.get_boolean("hardcore"),
            "allow_commands": self.data.get_boolean("allowCommands"),
            "initialized": self.data.get_boolean("initialized", True),
            "seed": world_gen.get_int("seed", 0),
            "time": self.data.get_int("Time", 0),
            "day_time": self.data.get_int("DayTime", 0),
            "spawn": [
                self.data.get_int("SpawnX", 0),
                self.data.get_int("SpawnY", 0),
                self.data.get_int("SpawnZ", 0),
            ],
            "spawn_angle": self.data.get_float("SpawnAngle", 0.0),
            "weather": {
                "raining": self.data.get_boolean("raining"),
                "rain_time": self.data.get_int("rainTime", 0),
                "thundering": self.data.get_boolean("thundering"),
                "thunder_time": self.data.get_int("thunderTime", 0),
                "clear_weather_time": self.data.get_int("clearWeatherTime", 0),
            },
        }

    # ------------------------------------------------------------------ #
    # 时间 / 天气 / 出生点 / 规则
    # ------------------------------------------------------------------ #
    def get_time(self) -> tuple[int, int]:
        return self.data.get_int("Time", 0), self.data.get_int("DayTime", 0)

    def set_time(self, game_time: Optional[int] = None, day_time: Optional[int] = None) -> None:
        if game_time is not None:
            self.data["Time"] = Long(int(game_time))
        if day_time is not None:
            self.data["DayTime"] = Long(int(day_time))

    def set_weather(self, weather: str, duration: int = 6000) -> None:
        weather = weather.lower()
        if weather == "clear":
            self.data["raining"] = Byte(0)
            self.data["thundering"] = Byte(0)
            self.data["clearWeatherTime"] = Int(int(duration))
            self.data["rainTime"] = Int(0)
            self.data["thunderTime"] = Int(0)
        elif weather == "rain":
            self.data["raining"] = Byte(1)
            self.data["thundering"] = Byte(0)
            self.data["clearWeatherTime"] = Int(0)
            self.data["rainTime"] = Int(int(duration))
            self.data["thunderTime"] = Int(int(duration))
        elif weather == "thunder":
            self.data["raining"] = Byte(1)
            self.data["thundering"] = Byte(1)
            self.data["clearWeatherTime"] = Int(0)
            self.data["rainTime"] = Int(int(duration))
            self.data["thunderTime"] = Int(int(duration))
        else:
            raise LevelDataError(f"未知天气: {weather!r} (支持 clear/rain/thunder)")

    def set_spawn(self, x: int, y: int, z: int, angle: Optional[float] = None) -> None:
        self.data["SpawnX"] = Int(int(x))
        self.data["SpawnY"] = Int(int(y))
        self.data["SpawnZ"] = Int(int(z))
        if angle is not None:
            self.data["SpawnAngle"] = Float(float(angle))

    def gamerules(self) -> dict[str, str]:
        rules = self.data.get_compound("GameRules")
        return {str(k): v for k, v in rules.items() if isinstance(v, str)}

    def set_gamerule(self, name: str, value: str) -> None:
        rules = self.data.get("GameRules")
        if not isinstance(rules, CompoundTag):
            rules = CompoundTag()
            self.data["GameRules"] = rules
        rules[str(name)] = str(value)

    def set_difficulty(self, difficulty: str, locked: Optional[bool] = None) -> None:
        difficulty = difficulty.lower()
        if difficulty not in DIFFICULTY_IDS:
            raise LevelDataError(f"未知难度: {difficulty!r} (peaceful/easy/normal/hard)")
        self.data["Difficulty"] = Byte(DIFFICULTY_IDS[difficulty])
        if locked is not None:
            self.data["DifficultyLocked"] = Byte(1 if locked else 0)

    def set_game_type(self, game_type: str) -> None:
        game_type = game_type.lower()
        if game_type not in GAME_TYPE_IDS:
            raise LevelDataError(f"未知游戏模式: {game_type!r} (survival/creative/adventure/spectator)")
        self.data["GameType"] = Int(GAME_TYPE_IDS[game_type])

    def set_level_name(self, name: str) -> None:
        self.data["LevelName"] = str(name)

    # ------------------------------------------------------------------ #
    # 任意路径
    # ------------------------------------------------------------------ #
    def get_path(self, path: str) -> Any:
        from .nbt import nbt_get_path

        return nbt_get_path(self.data, path)

    def set_path(self, path: str, value: Any, create: bool = False) -> None:
        from .nbt import nbt_set_path

        nbt_set_path(self.data, path, value, create=create)

    def __repr__(self) -> str:
        return f"LevelData({self.data.get_string('LevelName')!r}, DataVersion={self.data.get_int('DataVersion',-1)})"


def dump_level_data(level: LevelData) -> str:
    return to_snbt(level.data)
