"""mctool 图形界面 (Tkinter / ttk)。

用法::

    python -m mctool [--world PATH] [--tab N]
    python -m mctool.gui [--world PATH] [--tab N]

设计要点:
  - 只依赖 Python 标准库 + tkinter/ttk, 不联网、不依赖第三方包。
  - 打开世界时获取 ``session.lock`` 独占锁, 关闭窗口/关闭世界时释放。
  - 所有写操作立即 ``World.flush()`` 落盘 (核心层在覆盖前自动备份),
    备份路径显示在底部状态栏。
  - 校验 (verify) 与整世界备份在后台线程执行, 通过 ``after()`` 回到主线程更新 UI。
  - 标签页 / App 的公开方法不弹对话框、失败时抛异常, 便于自动化测试直接调用;
    只有按钮回调会捕获异常并弹出中文错误提示。
"""

from __future__ import annotations

import argparse
import glob
import math
import os
import queue
import sys
import threading
import traceback
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from . import __version__ as APP_VERSION
from .level import DIFFICULTIES, GAME_TYPES, LevelData
from .lock import WorldInUseError
from .nbt import (
    CompoundTag,
    Int,
    NbtError,
    coerce_typed,
    nbt_get_path,
    nbt_set_path,
    parse_snbt,
    to_snbt,
    to_snbt_pretty,
)
from .palette import BlockState
from .curios import (
    CURIO_MC_VERSION,
    CURIO_MOD_VERSION,
    CURIO_NEOFORGE_VERSION,
    CURIO_VALIDATION_WARNING,
    CURIO_VERSION_WARNING,
    delete_curio_item,
    get_curio_item,
    has_curios,
    list_groups,
    make_curio_item,
    set_curio_item,
    slot_display_name,
    update_curio_item,
)
from .items import (
    INVENTORY_SLOTS,
    describe_item,
    extra_key,
    item_count,
    item_extra,
    item_id,
    make_item,
    normalize_item_id,
    slot_label,
    update_item_fields,
)
from .player import DIMENSIONS as PLAYER_DIMENSION_IDS
from .search import DISPLAY_LIMIT, SHORT_MAX, normalize_block_entity_id
from .versions import SUPPORTED_VERSIONS, UnsupportedVersionError
from .world import World, WorldError, find_saves_dir

__all__ = [
    "McToolGUI",
    "WorldTab",
    "BlockTab",
    "PlayerTab",
    "NbtTab",
    "VerifyTab",
    "main",
]

APP_TITLE = f"mctool — Minecraft 存档工具（自动识别版本，v{APP_VERSION}）"
WINDOW_SIZE = "1150x760"
MIN_WINDOW_SIZE = (980, 620)

GAME_TYPE_CHOICES = tuple(GAME_TYPES.values())  # survival/creative/adventure/spectator
DIFFICULTY_CHOICES = tuple(DIFFICULTIES.values())  # peaceful/easy/normal/hard
WEATHER_CHOICES = ("clear", "rain", "thunder")
WEATHER_LABELS = {"clear": "晴朗", "rain": "下雨", "thunder": "雷雨"}
NBT_VALUE_TYPES = (
    "auto",
    "byte",
    "short",
    "int",
    "long",
    "float",
    "double",
    "string",
    "boolean",
    "byte_array",
    "int_array",
    "long_array",
    "compound",
    "list",
)
PLAYER_DIMENSION_CHOICES = tuple(PLAYER_DIMENSION_IDS.keys())  # overworld/nether/end


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _exc_text(exc: BaseException) -> str:
    """把异常转成适合展示给用户的中文文本。"""
    if isinstance(exc, WorldInUseError):
        return str(exc)
    text = str(exc).strip()
    if not text:
        text = exc.__class__.__name__
    return text


def _parse_int(
    text: Any,
    field: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    raw = str(text).strip()
    if raw == "":
        raise ValueError(f"{field}不能为空")
    try:
        value = int(raw, 10)
    except ValueError:
        raise ValueError(f"{field}必须是整数，当前输入为 {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ValueError(f"{field}必须大于等于 {minimum}（当前 {value}）")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field}必须小于等于 {maximum}（当前 {value}）")
    return value


def _parse_float(
    text: Any,
    field: str,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    raw = str(text).strip()
    if raw == "":
        raise ValueError(f"{field}不能为空")
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{field}必须是数字，当前输入为 {raw!r}") from None
    if not math.isfinite(value):
        raise ValueError(f"{field}必须是有限数字")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field}必须大于等于 {minimum:g}（当前 {value:g}）")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field}必须小于等于 {maximum:g}（当前 {value:g}）")
    return value


def _set_text(widget: tk.Text, text: str, readonly: bool = True) -> None:
    """覆盖只读/可写 Text 控件内容。"""
    widget.configure(state="normal")
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.see("1.0")
    widget.configure(state="disabled" if readonly else "normal")


def _get_text(widget: tk.Text) -> str:
    return widget.get("1.0", "end").strip()


def _dimension_short(dimension: str) -> str:
    """把 ``minecraft:overworld`` 之类的完整维度名转成短名。"""
    low = str(dimension or "").strip().lower()
    aliases = {"minecraft:overworld": "overworld", "minecraft:the_nether": "nether",
               "minecraft:the_end": "end"}
    if low in aliases:
        return aliases[low]
    for short, full in PLAYER_DIMENSION_IDS.items():
        if low in (short, str(full).lower()):
            return short
    return low or "overworld"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


# --------------------------------------------------------------------------- #
# 标签页基类
# --------------------------------------------------------------------------- #
class _Tab(ttk.Frame):
    """所有标签页的基类。"""

    title = "标签页"

    def __init__(self, master: tk.Misc, app: "McToolGUI"):
        super().__init__(master, padding=10)
        self.app = app
        self._build()

    # ---- 生命周期 -------------------------------------------------------- #
    def _build(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def refresh(self) -> None:
        """世界打开/关闭后刷新界面；子类覆盖。"""

    # ---- 便捷方法 -------------------------------------------------------- #
    @property
    def world(self) -> Optional[World]:
        return self.app.world

    def require_world(self) -> World:
        world = self.app.world
        if world is None:
            raise RuntimeError(
                "尚未打开世界。请点击上方“打开世界...”选择一个包含 level.dat 的存档目录。"
            )
        return world

    def _run_ui(self, action: Callable[[], Any], status: Optional[str] = None) -> Any:
        """执行按钮回调：失败时弹中文错误框（公开方法本身不会弹框）。"""
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001 - UI 边界统一处理
            self.app.show_error("操作失败", exc)
            return None
        if status:
            self.app.set_status(status)
        return result

    @staticmethod
    def _pick(overrides: dict[str, Any], keys: tuple[str, ...], var: tk.StringVar) -> Any:
        for key in keys:
            if key in overrides and overrides[key] is not None:
                return overrides[key]
        return var.get()


# --------------------------------------------------------------------------- #
# Tab 1: 世界信息 / level.dat
# --------------------------------------------------------------------------- #
class WorldTab(_Tab):
    """世界信息展示 + level.dat 常用字段修改 + GameRules。"""

    title = "世界信息"

    def _build(self) -> None:
        self.columnconfigure(0, weight=3)
        self.columnconfigure(1, weight=2, minsize=430)
        self.rowconfigure(0, weight=1)

        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        # ---- 只读信息 ---------------------------------------------------- #
        self._info_vars: dict[str, tk.StringVar] = {}
        info_box = ttk.LabelFrame(left, text="世界信息（只读）", padding=8)
        info_box.grid(row=0, column=0, sticky="ew")
        info_box.columnconfigure(1, weight=1)
        info_rows = [
            ("level_name", "世界名称"),
            ("data_version", "DataVersion"),
            ("version", "游戏版本"),
            ("detected_version", "检测版本"),
            ("selected_version", "处理版本"),
            ("version_mode", "版本模式"),
            ("seed", "种子"),
            ("game_type", "默认游戏模式"),
            ("difficulty", "难度"),
            ("hardcore", "极限模式"),
            ("allow_commands", "允许命令"),
            ("spawn", "出生点"),
            ("time", "时间"),
            ("weather", "天气"),
            ("dimensions", "维度"),
        ]
        for row, (key, label) in enumerate(info_rows):
            var = tk.StringVar(value="—")
            self._info_vars[key] = var
            ttk.Label(info_box, text=f"{label}：", width=12, anchor="w").grid(
                row=row, column=0, sticky="w", pady=1
            )
            ttk.Label(info_box, textvariable=var, anchor="w", wraplength=430).grid(
                row=row, column=1, sticky="w", pady=1
            )

        # ---- 可编辑设置 -------------------------------------------------- #
        settings = ttk.LabelFrame(left, text="修改世界设置", padding=8)
        settings.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        self.level_name_var = tk.StringVar()
        self.gametype_var = tk.StringVar(value=GAME_TYPE_CHOICES[0])
        self.game_type_var = self.gametype_var  # 别名
        self.difficulty_var = tk.StringVar(value="normal")
        self.time_var = tk.StringVar(value="0")
        self.game_time_var = self.time_var  # 别名
        self.day_time_var = tk.StringVar(value="0")
        self.weather_var = tk.StringVar(value=WEATHER_CHOICES[0])
        self.weather_duration_var = tk.StringVar(value="6000")
        self.spawn_x_var = tk.StringVar(value="0")
        self.spawn_y_var = tk.StringVar(value="-60")
        self.spawn_z_var = tk.StringVar(value="0")
        self.spawn_angle_var = tk.StringVar(value="0.0")

        ttk.Label(settings, text="世界名称：").grid(row=0, column=0, sticky="w", pady=3)
        self.level_name_entry = ttk.Entry(settings, textvariable=self.level_name_var)
        self.level_name_entry.grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)

        ttk.Label(settings, text="默认游戏模式：").grid(row=1, column=0, sticky="w", pady=3)
        self.gametype_combo = ttk.Combobox(
            settings, textvariable=self.gametype_var, values=GAME_TYPE_CHOICES,
            state="readonly", width=14,
        )
        self.gametype_combo.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(settings, text="难度：").grid(row=1, column=2, sticky="e", padx=(12, 6), pady=3)
        self.difficulty_combo = ttk.Combobox(
            settings, textvariable=self.difficulty_var, values=DIFFICULTY_CHOICES,
            state="readonly", width=14,
        )
        self.difficulty_combo.grid(row=1, column=3, sticky="w", pady=3)

        ttk.Label(settings, text="游戏时间 Time：").grid(row=2, column=0, sticky="w", pady=3)
        self.time_entry = ttk.Entry(settings, textvariable=self.time_var, width=16)
        self.time_entry.grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(settings, text="昼间时间 DayTime：").grid(row=2, column=2, sticky="e", padx=(12, 6), pady=3)
        self.day_time_entry = ttk.Entry(settings, textvariable=self.day_time_var, width=16)
        self.day_time_entry.grid(row=2, column=3, sticky="w", pady=3)

        ttk.Label(settings, text="天气：").grid(row=3, column=0, sticky="w", pady=3)
        self.weather_combo = ttk.Combobox(
            settings, textvariable=self.weather_var, values=WEATHER_CHOICES,
            state="readonly", width=14,
        )
        self.weather_combo.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Label(settings, text="持续时间(刻)：").grid(row=3, column=2, sticky="e", padx=(12, 6), pady=3)
        self.weather_duration_entry = ttk.Entry(settings, textvariable=self.weather_duration_var, width=16)
        self.weather_duration_entry.grid(row=3, column=3, sticky="w", pady=3)

        ttk.Label(settings, text="出生点：").grid(row=4, column=0, sticky="w", pady=3)
        spawn_frame = ttk.Frame(settings)
        spawn_frame.grid(row=4, column=1, columnspan=3, sticky="w", pady=3)
        self.spawn_entries: dict[str, ttk.Entry] = {}
        for index, (label, var) in enumerate(
            (("X", self.spawn_x_var), ("Y", self.spawn_y_var),
             ("Z", self.spawn_z_var), ("朝向", self.spawn_angle_var))
        ):
            ttk.Label(spawn_frame, text=label).grid(
                row=0, column=index * 2, padx=(0 if index == 0 else 10, 2)
            )
            entry = ttk.Entry(spawn_frame, textvariable=var, width=8)
            entry.grid(row=0, column=index * 2 + 1)
            self.spawn_entries[label] = entry

        button_row = ttk.Frame(settings)
        button_row.grid(row=5, column=0, columnspan=4, sticky="w", pady=(10, 0))
        self.apply_button = ttk.Button(
            button_row, text="应用世界设置", style="Accent.TButton",
            command=self._ui_apply,
        )
        self.apply_button.pack(side="left")
        self.reload_button = ttk.Button(
            button_row, text="重新读取 level.dat", command=self._ui_reload,
        )
        self.reload_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            button_row, text="（写入前自动备份，备份路径显示在底部状态栏）",
            style="Hint.TLabel",
        ).pack(side="left", padx=(10, 0))

        # ---- GameRules --------------------------------------------------- #
        rules_box = ttk.LabelFrame(self, text="游戏规则（GameRules）", padding=8)
        rules_box.grid(row=0, column=1, sticky="nsew")
        rules_box.columnconfigure(0, weight=1)
        rules_box.rowconfigure(0, weight=1)

        tree_frame = ttk.Frame(rules_box)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.gamerule_tree = ttk.Treeview(
            tree_frame, columns=("name", "value"), show="headings", height=16,
            selectmode="browse",
        )
        self.gamerule_tree.heading("name", text="规则名")
        self.gamerule_tree.heading("value", text="值")
        self.gamerule_tree.column("name", width=200, anchor="w")
        self.gamerule_tree.column("value", width=80, anchor="w")
        rule_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.gamerule_tree.yview)
        self.gamerule_tree.configure(yscrollcommand=rule_scroll.set)
        self.gamerule_tree.grid(row=0, column=0, sticky="nsew")
        rule_scroll.grid(row=0, column=1, sticky="ns")
        self.gamerule_tree.bind("<<TreeviewSelect>>", self._on_rule_select)
        self.gamerule_tree.bind("<Configure>", self._resize_rule_columns)

        controls = ttk.Frame(rules_box)
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="规则：").grid(row=0, column=0, sticky="w")
        self.gamerule_name_var = tk.StringVar()
        self.gamerule_name_combo = ttk.Combobox(
            controls, textvariable=self.gamerule_name_var, state="readonly",
        )
        self.gamerule_name_combo.grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(controls, text="值：").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.gamerule_value_var = tk.StringVar()
        self.gamerule_value_entry = ttk.Entry(controls, textvariable=self.gamerule_value_var)
        self.gamerule_value_entry.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.set_rule_button = ttk.Button(controls, text="设置规则", command=self._ui_set_rule)
        self.set_rule_button.grid(row=1, column=2, padx=(8, 0), pady=(6, 0))

    # ---- 内部 ------------------------------------------------------------ #
    def _resize_rule_columns(self, event: "tk.Event | None" = None) -> None:
        width = int(getattr(event, "width", 0) or 0)
        if width <= 40:
            return
        name_width = max(120, int(width * 0.6))
        value_width = max(70, width - name_width - 4)
        try:
            self.gamerule_tree.column("name", width=name_width)
            self.gamerule_tree.column("value", width=value_width)
        except tk.TclError:
            pass

    def _on_rule_select(self, _event: "tk.Event | None" = None) -> None:
        selection = self.gamerule_tree.selection()
        if not selection:
            return
        name = selection[0]
        self.gamerule_name_var.set(name)
        for row_id in self.gamerule_tree.get_children():
            pass
        item = self.gamerule_tree.item(name)
        values = item.get("values") if isinstance(item, dict) else None
        if values and len(values) >= 2:
            self.gamerule_value_var.set(str(values[1]))

    def _ui_apply(self) -> None:
        self._run_ui(self.apply_world_settings)

    def _ui_reload(self) -> None:
        self._run_ui(self.app.reload_level, status="已重新读取 level.dat")

    def _ui_set_rule(self) -> None:
        name = self.gamerule_name_var.get().strip()
        value = self.gamerule_value_var.get().strip()
        self._run_ui(lambda: self.set_gamerule(name, value))

    def _fill_defaults(self) -> None:
        self.level_name_var.set("")
        self.gametype_var.set(GAME_TYPE_CHOICES[0])
        self.difficulty_var.set("normal")
        self.time_var.set("0")
        self.day_time_var.set("0")
        self.weather_var.set(WEATHER_CHOICES[0])
        self.weather_duration_var.set("6000")
        self.spawn_x_var.set("0")
        self.spawn_y_var.set("-60")
        self.spawn_z_var.set("0")
        self.spawn_angle_var.set("0.0")

    def _fill_editable(self, summary: dict) -> None:
        self.level_name_var.set(str(summary.get("level_name") or ""))
        self.gametype_var.set(str(summary.get("game_type") or GAME_TYPE_CHOICES[0]))
        self.difficulty_var.set(str(summary.get("difficulty") or "normal"))
        self.time_var.set(str(summary.get("time", 0)))
        self.day_time_var.set(str(summary.get("day_time", 0)))

        weather = summary.get("weather") or {}
        if weather.get("thundering"):
            current = "thunder"
        elif weather.get("raining"):
            current = "rain"
        else:
            current = "clear"
        self.weather_var.set(current)
        if current == "thunder":
            duration = weather.get("thunder_time", 0)
        elif current == "rain":
            duration = weather.get("rain_time", 0)
        else:
            duration = weather.get("clear_weather_time", 0)
        self.weather_duration_var.set(str(duration if duration else 6000))

        spawn = summary.get("spawn") or [0, -60, 0]
        self.spawn_x_var.set(str(spawn[0]))
        self.spawn_y_var.set(str(spawn[1]))
        self.spawn_z_var.set(str(spawn[2]))
        self.spawn_angle_var.set(str(summary.get("spawn_angle", 0.0)))

    def _fill_info(self, world: World, summary: dict) -> None:
        version = summary.get("version") or {}
        weather = summary.get("weather") or {}
        rain_time = weather.get("rain_time", 0)
        thunder_time = weather.get("thunder_time", 0)
        clear_time = weather.get("clear_weather_time", 0)
        if weather.get("thundering"):
            weather_text = f"雷雨（rainTime={rain_time}, thunderTime={thunder_time}）"
        elif weather.get("raining"):
            weather_text = f"下雨（rainTime={rain_time}）"
        else:
            weather_text = f"晴朗（clearWeatherTime={clear_time}）"

        spawn = summary.get("spawn") or [0, -60, 0]
        info = {
            "level_name": str(summary.get("level_name") or ""),
            "data_version": str(summary.get("data_version", -1)),
            "version": (
                f"{version.get('name')}（Id={version.get('id')}, "
                f"Series={version.get('series')}）"
            ),
            "seed": str(summary.get("seed", 0)),
            "game_type": str(summary.get("game_type") or ""),
            "difficulty": str(summary.get("difficulty") or ""),
            "hardcore": "是" if summary.get("hardcore") else "否",
            "allow_commands": "是" if summary.get("allow_commands") else "否",
            "spawn": (
                f"({spawn[0]}, {spawn[1]}, {spawn[2]})，"
                f"朝向 {summary.get('spawn_angle', 0.0):g}°"
            ),
            "time": f"Time={summary.get('time', 0)}, DayTime={summary.get('day_time', 0)}",
            "weather": weather_text,
            "dimensions": "、".join(world.list_dimensions()),
        }
        detected = getattr(world, "detected_version", None)
        profile = getattr(world, "version_profile", None)
        info["detected_version"] = detected.display if detected is not None else "—"
        info["selected_version"] = (
            f"{profile.id}（{profile.status_label}）" if profile is not None else "—"
        )
        mode = "自动（受支持）" if getattr(world, "version_auto", True) else "用户指定"
        if getattr(world, "version_override", False):
            mode += "（与检测版本不一致，写入有风险）"
        info["version_mode"] = mode
        for key, value in info.items():
            self._info_vars[key].set(value)

    def _fill_gamerules(self, rules: dict[str, str]) -> None:
        self.gamerule_tree.delete(*self.gamerule_tree.get_children())
        for name in sorted(rules):
            self.gamerule_tree.insert("", "end", iid=name, values=(name, rules[name]))
        values = sorted(rules)
        self.gamerule_name_combo["values"] = values
        if values and not self.gamerule_name_var.get():
            self.gamerule_name_var.set(values[0])
        if values:
            current = self.gamerule_name_var.get()
            if current in rules:
                self.gamerule_value_var.set(rules[current])

    # ---- 公开方法 -------------------------------------------------------- #
    def refresh(self) -> None:
        world = self.app.world
        if world is None:
            for var in self._info_vars.values():
                var.set("（未打开世界）")
            self.gamerule_tree.delete(*self.gamerule_tree.get_children())
            self.gamerule_name_combo["values"] = ()
            self.gamerule_name_var.set("")
            self.gamerule_value_var.set("")
            self._fill_defaults()
            return
        summary = world.level.summary()
        self._fill_info(world, summary)
        self._fill_editable(summary)
        self._fill_gamerules(world.level.gamerules())

    def apply_world_settings(self, **overrides: Any) -> list[str]:
        """把界面上的世界设置写入 level.dat，返回本次新增的备份路径列表。"""
        world = self.require_world()

        name = str(self._pick(overrides, ("level_name", "name"), self.level_name_var)).strip()
        if not name:
            raise ValueError("世界名称不能为空")
        game_type = str(
            self._pick(overrides, ("game_type", "gametype"), self.gametype_var)
        ).strip().lower()
        if game_type not in GAME_TYPE_CHOICES:
            raise ValueError(
                f"未知游戏模式：{game_type!r}（可选 {' / '.join(GAME_TYPE_CHOICES)}）"
            )
        difficulty = str(
            self._pick(overrides, ("difficulty",), self.difficulty_var)
        ).strip().lower()
        if difficulty not in DIFFICULTY_CHOICES:
            raise ValueError(
                f"未知难度：{difficulty!r}（可选 {' / '.join(DIFFICULTY_CHOICES)}）"
            )
        weather = str(
            self._pick(overrides, ("weather",), self.weather_var)
        ).strip().lower()
        if weather not in WEATHER_CHOICES:
            raise ValueError(
                f"未知天气：{weather!r}（可选 {' / '.join(WEATHER_CHOICES)}）"
            )

        game_time = _parse_int(
            self._pick(overrides, ("time", "game_time"), self.time_var),
            "游戏时间 Time",
            minimum=0,
        )
        day_time = _parse_int(
            self._pick(overrides, ("day_time",), self.day_time_var),
            "昼间时间 DayTime",
            minimum=0,
        )
        weather_duration = _parse_int(
            self._pick(overrides, ("weather_duration", "duration"), self.weather_duration_var),
            "天气持续时间",
            minimum=0,
        )
        spawn_x = _parse_int(self._pick(overrides, ("spawn_x", "x"), self.spawn_x_var), "出生点 X")
        spawn_y = _parse_int(self._pick(overrides, ("spawn_y", "y"), self.spawn_y_var), "出生点 Y")
        spawn_z = _parse_int(self._pick(overrides, ("spawn_z", "z"), self.spawn_z_var), "出生点 Z")
        spawn_angle = _parse_float(
            self._pick(overrides, ("spawn_angle", "angle"), self.spawn_angle_var),
            "出生点朝向",
        )

        world.level.set_level_name(name)
        world.level.set_time(game_time, day_time)
        world.level.set_weather(weather, weather_duration)
        world.level.set_game_type(game_type)
        world.level.set_difficulty(difficulty)
        world.level.set_spawn(spawn_x, spawn_y, spawn_z, spawn_angle)
        world.mark_level_dirty()

        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])

        self.refresh()
        self.app.show_backups(new_backups)
        self.app.set_status(
            f"已应用世界设置：名称={name!r}，模式={game_type}，难度={difficulty}，"
            f"Time={game_time}，DayTime={day_time}，天气={weather}（{weather_duration} 刻）"
        )
        return new_backups

    def set_gamerule(self, name: str, value: str) -> None:
        """设置一条游戏规则并立即保存。"""
        world = self.require_world()
        rule_name = str(name).strip()
        rule_value = str(value).strip()
        if not rule_name:
            raise ValueError("规则名不能为空")
        if not rule_value:
            raise ValueError("规则值不能为空（例如 true / false / 10）")
        if any(ch.isspace() for ch in rule_value):
            raise ValueError(f"规则值不能包含空白字符：{rule_value!r}")

        world.level.set_gamerule(rule_name, rule_value)
        world.mark_level_dirty()

        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])

        self.refresh()
        self.app.show_backups(new_backups)
        self.app.set_status(f"已设置游戏规则 {rule_name} = {rule_value}")


# --------------------------------------------------------------------------- #
# Tab 2: 方块 / 方块实体
# --------------------------------------------------------------------------- #
class BlockTab(_Tab):
    """方块读取/修改、区块摘要、方块实体 SNBT 编辑。"""

    title = "方块 / 方块实体"

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text="维度：").pack(side="left")
        self.dimension_var = tk.StringVar(value="overworld")
        self.dimension_combo = ttk.Combobox(
            top, textvariable=self.dimension_var, values=["overworld"], width=24,
        )
        self.dimension_combo.pack(side="left")
        self.dimension_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_dimension_changed())
        ttk.Label(
            top, text="（overworld / nether / end 或 namespace:path）", style="Hint.TLabel",
        ).pack(side="left", padx=(8, 0))

        # ---- 方块 -------------------------------------------------------- #
        block_box = ttk.LabelFrame(self, text="方块", padding=8)
        block_box.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        block_box.columnconfigure(0, weight=1)

        coord_frame = ttk.Frame(block_box)
        coord_frame.grid(row=0, column=0, sticky="w")
        self.x_var = self.coord_x_var = tk.StringVar(value="0")
        self.y_var = self.coord_y_var = tk.StringVar(value="-60")
        self.z_var = self.coord_z_var = tk.StringVar(value="0")
        self.coord_entries: dict[str, ttk.Entry] = {}
        for index, (label, var) in enumerate(
            (("X", self.x_var), ("Y", self.y_var), ("Z", self.z_var))
        ):
            ttk.Label(coord_frame, text=label).grid(
                row=0, column=index * 2, padx=(0 if index == 0 else 10, 2)
            )
            entry = ttk.Entry(coord_frame, textvariable=var, width=9)
            entry.grid(row=0, column=index * 2 + 1)
            self.coord_entries[label] = entry
        self.read_block_button = ttk.Button(
            coord_frame, text="读取方块", command=self._ui_read_block,
        )
        self.read_block_button.grid(row=0, column=6, padx=(12, 0))

        self.block_display_var = tk.StringVar(value="（尚未读取方块）")
        ttk.Label(
            block_box, textvariable=self.block_display_var, anchor="w",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=(6, 2))

        state_frame = ttk.Frame(block_box)
        state_frame.grid(row=2, column=0, sticky="ew")
        state_frame.columnconfigure(1, weight=1)
        ttk.Label(state_frame, text="方块状态：").grid(row=0, column=0, sticky="w")
        self.block_state_var = tk.StringVar(value="minecraft:stone")
        self.block_state_entry = ttk.Entry(state_frame, textvariable=self.block_state_var)
        self.block_state_entry.grid(row=0, column=1, sticky="ew")
        self.set_block_button = ttk.Button(
            state_frame, text="设置方块", command=self._ui_set_block,
        )
        self.set_block_button.grid(row=0, column=2, padx=(8, 0))
        self.keep_be_var = tk.BooleanVar(value=False)
        self.keep_be_check = ttk.Checkbutton(
            state_frame, text="保留方块实体", variable=self.keep_be_var,
        )
        self.keep_be_check.grid(row=0, column=3, padx=(8, 0))

        self.chunk_info_var = tk.StringVar(
            value="（选择坐标后点击“读取方块”可查看区块信息）"
        )
        ttk.Label(
            block_box, textvariable=self.chunk_info_var, anchor="w",
            justify="left", wraplength=1050,
        ).grid(row=3, column=0, sticky="w", pady=(6, 0))

        # ---- 方块实体 ---------------------------------------------------- #
        be_box = ttk.LabelFrame(self, text="方块实体（SNBT，高级）", padding=8)
        be_box.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        be_box.columnconfigure(0, weight=1)
        be_box.rowconfigure(1, weight=1)

        be_bar = ttk.Frame(be_box)
        be_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.read_be_button = ttk.Button(
            be_bar, text="读取方块实体", command=self._ui_read_be,
        )
        self.read_be_button.pack(side="left")
        self.write_be_button = ttk.Button(
            be_bar, text="写入方块实体", command=self._ui_write_be,
        )
        self.write_be_button.pack(side="left", padx=(8, 0))
        self.delete_be_button = ttk.Button(
            be_bar, text="删除方块实体", command=self._ui_delete_be,
        )
        self.delete_be_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            be_bar,
            text='示例：{id:"minecraft:chest",Items:[]}；写入时自动注入当前 X/Y/Z 坐标。',
            style="Hint.TLabel",
        ).pack(side="left", padx=(10, 0))

        self.be_editor = ScrolledText(
            be_box, height=8, wrap="word", undo=False, font=self.app.mono_font,
        )
        self.be_editor.grid(row=1, column=0, sticky="nsew")

    # ---- 内部 ------------------------------------------------------------ #
    def _dimension(self) -> str:
        raw = self.dimension_var.get().strip() or "overworld"
        return World.normalize_dimension(raw)

    def _block_coords(
        self,
        x: Any = None,
        y: Any = None,
        z: Any = None,
    ) -> tuple[int, int, int]:
        bx = _parse_int(self.x_var.get() if x is None else x, "X 坐标")
        by = _parse_int(self.y_var.get() if y is None else y, "Y 坐标")
        bz = _parse_int(self.z_var.get() if z is None else z, "Z 坐标")
        return bx, by, bz

    def _on_dimension_changed(self) -> None:
        self.block_display_var.set("（维度已切换，请重新读取方块）")
        self.chunk_info_var.set("（维度已切换，请重新读取方块）")

    def _refresh_chunk_info(self, x: int, z: int, dimension: str) -> None:
        world = self.app.world
        if world is None:
            self.chunk_info_var.set("（未打开世界）")
            return
        cx, cz = x >> 4, z >> 4
        try:
            chunk = world.require_chunk(cx, cz, dimension)
        except Exception as exc:  # noqa: BLE001 - 仅用于展示
            self.chunk_info_var.set(f"区块 ({cx}, {cz})：{_exc_text(exc)}")
            return
        summary = chunk.summary()
        lines = [
            f"区块：({summary['chunk'][0]}, {summary['chunk'][1]})"
            f"    状态：{summary['status']}"
            f"    DataVersion：{summary['data_version']}"
            f"    最低 section Y：{summary['min_section']}"
            f"    section 数：{summary['sections']}"
            f"    方块实体数：{summary['block_entities']}",
        ]
        entities: list[str] = []
        for be in chunk.iter_block_entities():
            entities.append(
                f"{be.get_string('id', '?')} @ "
                f"({be.get_int('x')}, {be.get_int('y')}, {be.get_int('z')})"
            )
        if entities:
            lines.append("方块实体：" + "；".join(entities))
        self.chunk_info_var.set("\n".join(lines))

    def _ui_read_block(self) -> None:
        self._run_ui(self.read_block)

    def _ui_set_block(self) -> None:
        self._run_ui(
            lambda: self.set_block(
                self.block_state_var.get(), bool(self.keep_be_var.get())
            )
        )

    def _ui_read_be(self) -> None:
        self._run_ui(self.read_block_entity)

    def _ui_write_be(self) -> None:
        self._run_ui(lambda: self.write_block_entity(_get_text(self.be_editor)))

    def _ui_delete_be(self) -> None:
        self._run_ui(self.delete_block_entity)

    # ---- 公开方法 -------------------------------------------------------- #
    def set_dimension(self, dim: str) -> None:
        """切换当前维度（overworld / nether / end / namespace:path）。"""
        if dim is None or not str(dim).strip():
            raise ValueError("维度不能为空")
        normalized = World.normalize_dimension(str(dim).strip())
        self.dimension_var.set(normalized)
        self._on_dimension_changed()

    def refresh(self) -> None:
        world = self.app.world
        values = ["overworld"]
        if world is not None:
            values.extend(world.list_dimensions())
        values = _dedupe(values)
        self.dimension_combo["values"] = values
        if self.dimension_var.get().strip() not in values:
            self.dimension_var.set("overworld")
        self.block_display_var.set("（尚未读取方块）")
        self.chunk_info_var.set("（选择坐标后点击“读取方块”可查看区块信息）")
        _set_text(self.be_editor, "", readonly=False)

    def set_coords(self, x: int, y: int, z: int) -> None:
        """设置界面上的坐标（便于脚本/测试直接指定目标方块）。"""
        self.x_var.set(str(int(x)))
        self.y_var.set(str(int(y)))
        self.z_var.set(str(int(z)))

    def read_block(
        self,
        x: Any = None,
        y: Any = None,
        z: Any = None,
        dimension: Optional[str] = None,
    ):
        """读取方块，返回 :class:`BlockState`。"""
        world = self.require_world()
        bx, by, bz = self._block_coords(x, y, z)
        dim = World.normalize_dimension(
            self._dimension() if dimension is None else dimension
        )
        state = world.get_block(bx, by, bz, dimension=dim)
        self.block_display_var.set(f"({bx}, {by}, {bz}) [{dim}] = {state}")
        self.block_state_var.set(str(state))
        self._refresh_chunk_info(bx, bz, dim)
        self.app.set_status(f"已读取方块 ({bx}, {by}, {bz}) [{dim}] = {state}")
        return state

    def set_block(
        self,
        state_spec: str,
        keep_be: bool = False,
        x: Any = None,
        y: Any = None,
        z: Any = None,
        dimension: Optional[str] = None,
    ) -> str:
        """设置方块并立即保存，返回 "旧状态 -> 新状态" 字符串。"""
        world = self.require_world()
        bx, by, bz = self._block_coords(x, y, z)
        dim = World.normalize_dimension(
            self._dimension() if dimension is None else dimension
        )
        try:
            state = BlockState.parse(str(state_spec))
        except Exception as exc:  # PaletteError 等
            raise ValueError(f"非法的方块状态 {state_spec!r}：{_exc_text(exc)}") from exc

        change = world.set_block(
            bx, by, bz, state, dimension=dim, keep_block_entity=bool(keep_be)
        )
        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])

        self.block_display_var.set(f"已设置 ({bx}, {by}, {bz}) [{dim}]：{change}")
        self.block_state_var.set(str(state))
        self._refresh_chunk_info(bx, bz, dim)
        self.app.show_backups(new_backups)
        self.app.set_status(f"已设置方块 ({bx}, {by}, {bz}) [{dim}]：{change}")
        return change

    def read_block_entity(
        self,
        x: Any = None,
        y: Any = None,
        z: Any = None,
        dimension: Optional[str] = None,
    ) -> Optional[CompoundTag]:
        """读取方块实体；不存在时返回 None 并清空编辑器。"""
        world = self.require_world()
        bx, by, bz = self._block_coords(x, y, z)
        dim = World.normalize_dimension(
            self._dimension() if dimension is None else dimension
        )
        chunk = world.require_chunk(bx >> 4, bz >> 4, dim)
        be = chunk.find_block_entity(bx, by, bz)
        self._refresh_chunk_info(bx, bz, dim)
        if be is None:
            _set_text(self.be_editor, "", readonly=False)
            self.app.set_status(f"({bx}, {by}, {bz}) [{dim}] 没有方块实体")
            return None
        # 读取方块实体时像格式化 JSON 一样输出多行缩进 SNBT，便于阅读/编辑
        _set_text(self.be_editor, to_snbt_pretty(be), readonly=False)
        self.app.set_status(f"已读取方块实体 ({bx}, {by}, {bz}) [{dim}]")
        return be

    def write_block_entity(
        self,
        snbt: str,
        x: Any = None,
        y: Any = None,
        z: Any = None,
        dimension: Optional[str] = None,
    ) -> None:
        """把 SNBT 写入为当前坐标的方块实体（自动注入 x/y/z）并立即保存。"""
        world = self.require_world()
        bx, by, bz = self._block_coords(x, y, z)
        dim = World.normalize_dimension(
            self._dimension() if dimension is None else dimension
        )
        text = str(snbt or "").strip()
        if not text:
            raise ValueError("方块实体 SNBT 不能为空")
        try:
            tag = parse_snbt(text)
        except Exception as exc:
            raise ValueError(f"方块实体 SNBT 解析失败：{_exc_text(exc)}") from exc
        if not isinstance(tag, CompoundTag):
            raise ValueError(
                '方块实体必须是一个 compound SNBT，例如 {id:"minecraft:chest",Items:[]}'
            )
        tag["x"] = Int(bx)
        tag["y"] = Int(by)
        tag["z"] = Int(bz)

        chunk = world.require_chunk(bx >> 4, bz >> 4, dim)
        try:
            chunk.set_block_entity(tag)
        except Exception as exc:
            raise WorldError(f"写入方块实体失败：{_exc_text(exc)}") from exc
        world.mark_chunk_dirty(chunk, dim)

        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])

        self._refresh_chunk_info(bx, bz, dim)
        _set_text(self.be_editor, to_snbt_pretty(tag), readonly=False)
        self.app.show_backups(new_backups)
        self.app.set_status(f"已写入方块实体 ({bx}, {by}, {bz}) [{dim}]")

    def delete_block_entity(
        self,
        x: Any = None,
        y: Any = None,
        z: Any = None,
        dimension: Optional[str] = None,
    ) -> None:
        """删除当前坐标的方块实体并立即保存。"""
        world = self.require_world()
        bx, by, bz = self._block_coords(x, y, z)
        dim = World.normalize_dimension(
            self._dimension() if dimension is None else dimension
        )
        chunk = world.require_chunk(bx >> 4, bz >> 4, dim)
        if not chunk.delete_block_entity(bx, by, bz):
            raise WorldError(f"({bx}, {by}, {bz}) [{dim}] 没有方块实体可删除")
        world.mark_chunk_dirty(chunk, dim)

        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])

        _set_text(self.be_editor, "", readonly=False)
        self._refresh_chunk_info(bx, bz, dim)
        self.app.show_backups(new_backups)
        self.app.set_status(f"已删除方块实体 ({bx}, {by}, {bz}) [{dim}]")


# --------------------------------------------------------------------------- #
# Tab 3: 方块搜索（按注册名反向查找）
# --------------------------------------------------------------------------- #
class BlockSearchTab(_Tab):
    """按方块注册名反向查找当前维度所有已生成区块中的方块。

    读取上限 SHORT_MAX=32767 个匹配（达到后停止扫描），
    结果列表最多显示 DISPLAY_LIMIT=256 个坐标。
    """

    title = "方块搜索"

    def _build(self) -> None:
        self._running = False
        self._cancel_event = threading.Event()
        self._progress_lock = threading.Lock()
        self._latest_progress: tuple[int, int, int] = (0, 0, 0)
        self._poll_after: Optional[str] = None
        self._last_result = None
        self._search_world_path: Optional[str] = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        # ---- 搜索条件 ---------------------------------------------------- #
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="维度：").pack(side="left")
        self.dimension_var = tk.StringVar(value="overworld")
        self.dimension_combo = ttk.Combobox(
            bar, textvariable=self.dimension_var, values=["overworld"], width=20,
        )
        self.dimension_combo.pack(side="left")
        ttk.Label(bar, text="  方块注册名：").pack(side="left")
        self.block_var = tk.StringVar(value="minecraft:chest")
        self.block_entry = ttk.Entry(bar, textvariable=self.block_var, width=30)
        self.block_entry.pack(side="left")
        self.search_button = ttk.Button(
            bar, text="开始查找", style="Accent.TButton", command=self._ui_start,
        )
        self.search_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(
            bar, text="停止", command=self._ui_stop, state="disabled",
        )
        self.stop_button.pack(side="left", padx=(6, 0))

        ttk.Label(
            self,
            text=f"扫描当前维度全部已生成区块；最多统计 {SHORT_MAX} 个匹配，"
                 f"列表最多显示 {DISPLAY_LIMIT} 个坐标。"
                 "支持 minecraft:oak_log[axis=x] 形式的精确状态过滤。",
            style="Hint.TLabel",
            wraplength=1080,
        ).grid(row=1, column=0, sticky="w", pady=(6, 4))

        # ---- 进度/状态 --------------------------------------------------- #
        progress_row = ttk.Frame(self)
        progress_row.grid(row=2, column=0, sticky="ew")
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(
            progress_row, orient="horizontal", mode="determinate", maximum=1, value=0,
        )
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status_var = tk.StringVar(value="就绪：选择维度、输入方块注册名后点击“开始查找”")
        ttk.Label(
            progress_row, textvariable=self.status_var, anchor="w", style="Hint.TLabel",
            wraplength=1080,
        ).grid(row=1, column=0, sticky="w", pady=(4, 6))

        # ---- 结果表 ------------------------------------------------------ #
        result_box = ttk.LabelFrame(self, text="匹配结果（双击行可定位并读取该方块）", padding=6)
        result_box.grid(row=3, column=0, sticky="nsew")
        result_box.columnconfigure(0, weight=1)
        result_box.rowconfigure(0, weight=1)
        columns = ("index", "x", "y", "z", "state")
        headings = {"index": "#", "x": "X", "y": "Y", "z": "Z", "state": "方块状态"}
        widths = {"index": 60, "x": 110, "y": 110, "z": 110, "state": 620}
        self.tree = ttk.Treeview(
            result_box, columns=columns, show="headings", height=10, selectmode="browse",
        )
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(result_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self._on_row_activate)
        self.tree.bind("<Return>", self._on_row_activate)

        bottom = ttk.Frame(result_box)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(
            bottom, text="定位并读取选中方块", command=self._on_read_selected,
        ).pack(side="left")
        ttk.Button(bottom, text="清空结果", command=self._clear_results).pack(
            side="left", padx=(8, 0)
        )
        self.result_var = tk.StringVar(value="尚无结果")
        ttk.Label(bottom, textvariable=self.result_var, style="Hint.TLabel").pack(
            side="left", padx=(14, 0)
        )

    # ---- 生命周期 -------------------------------------------------------- #
    def refresh(self) -> None:
        world = self.app.world
        if world is None:
            if self._running:
                self._cancel_event.set()
            self.dimension_combo["values"] = ["overworld"]
            self.status_var.set("（未打开世界）")
            self.search_button.configure(state="disabled")
            return
        if self._running and self._search_world_path not in (None, world.path):
            # 世界已被切换: 取消旧世界的搜索
            self._cancel_event.set()
        dimensions = world.list_dimensions()
        self.dimension_combo["values"] = dimensions
        current = World.normalize_dimension(self.dimension_var.get())
        if current not in dimensions:
            self.dimension_var.set(dimensions[0] if dimensions else "overworld")
        self.search_button.configure(state="disabled" if self._running else "normal")
        if not self._running:
            self.status_var.set(
                f"就绪：维度 {self.dimension_var.get()}，输入方块注册名后点击“开始查找”"
            )

    # ---- 搜索 ------------------------------------------------------------ #
    def start_search(self, block_spec: Optional[str] = None,
                     dimension: Optional[str] = None) -> None:
        world = self.require_world()
        if self._running:
            raise RuntimeError("已有搜索正在进行，请等待完成或点击“停止”。")
        spec = str(block_spec if block_spec is not None else self.block_var.get()).strip()
        if not spec:
            raise ValueError("请填写方块注册名，例如 minecraft:chest")
        # 提前解析以校验语法（不要求方块一定存在）
        BlockState.parse(spec)
        dim = World.normalize_dimension(dimension or self.dimension_var.get())
        if dim not in world.list_dimensions():
            raise ValueError(f"维度不存在或无 region：{dim}")

        self._running = True
        self._search_world_path = world.path
        self._cancel_event.clear()
        with self._progress_lock:
            self._latest_progress = (0, 0, 0)
        self.progress.configure(maximum=1, value=0)
        self.tree.delete(*self.tree.get_children())
        self._last_result = None
        self.result_var.set("搜索中...")
        self.search_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set(f"正在扫描维度 {dim} 的区块，查找 {spec} ...")
        self.app.set_status(f"方块搜索：{spec} @ {dim}")

        self.block_var.set(spec)
        self.dimension_var.set(dim)
        self._poll_progress()
        self.app.run_background(
            lambda: world.find_blocks(
                spec,
                dimension=dim,
                limit=SHORT_MAX,
                display_limit=DISPLAY_LIMIT,
                progress=self._worker_progress,
                cancel_event=self._cancel_event,
            ),
            self._on_search_done,
            description="block-search",
        )

    def stop_search(self) -> None:
        if not self._running:
            return
        self._cancel_event.set()
        self.status_var.set("正在停止搜索...")
        self.stop_button.configure(state="disabled")

    def _worker_progress(self, scanned: int, total: int, found: int) -> None:
        with self._progress_lock:
            self._latest_progress = (int(scanned), int(total), int(found))

    def _poll_progress(self) -> None:
        self._poll_after = None
        if not self._running:
            return
        with self._progress_lock:
            scanned, total, found = self._latest_progress
        if total > 0:
            self.progress.configure(maximum=total)
            self.progress.configure(value=min(scanned, total))
        self.status_var.set(
            f"正在扫描 {scanned}/{total if total else '?'} 个区块，"
            f"已找到 {found} 个匹配（最多统计 {SHORT_MAX} 个）..."
        )
        try:
            self._poll_after = self.after(150, self._poll_progress)
        except tk.TclError:
            self._poll_after = None

    def _on_search_done(self, payload: Any) -> None:
        self._running = False
        if self._poll_after is not None:
            try:
                self.after_cancel(self._poll_after)
            except Exception:  # noqa: BLE001
                pass
            self._poll_after = None
        self.search_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

        if isinstance(payload, BaseException):
            self.progress.configure(value=0)
            self.status_var.set(f"搜索失败：{_exc_text(payload)}")
            self.app.show_error("方块搜索失败", payload)
            return

        result = payload
        self._last_result = result
        self.progress.configure(maximum=max(result.chunks_total, 1))
        self.progress.configure(
            value=min(result.chunks_scanned, max(result.chunks_total, 1))
        )

        for index, match in enumerate(result.matches, start=1):
            self.tree.insert(
                "", "end", iid=str(index),
                values=(index, match.x, match.y, match.z, match.state),
            )
        shown = len(result.matches)
        if result.cancelled:
            status = (
                f"已取消：找到 {result.total_found} 个匹配"
                f"（扫描 {result.chunks_scanned}/{result.chunks_total} 个区块，"
                f"耗时 {result.elapsed:.1f}s）"
            )
        elif result.truncated:
            status = (
                f"已达到读取上限 {SHORT_MAX}，停止扫描；找到 {result.total_found} 个，"
                f"显示前 {shown} 个（耗时 {result.elapsed:.1f}s）"
            )
        else:
            status = (
                f"完成：找到 {result.total_found} 个匹配，显示前 {shown} 个"
                f"（扫描 {result.chunks_scanned}/{result.chunks_total} 个区块，"
                f"耗时 {result.elapsed:.1f}s）"
            )
        if result.errors:
            status += f"；{len(result.errors)} 个区块读取失败"
        self.status_var.set(status)
        self.result_var.set(
            f"维度 {result.dimension}，方块 {result.block}，"
            f"匹配 {result.total_found} 个，显示 {shown} 个"
        )
        self.app.set_status(status)

    # ---- 结果操作 -------------------------------------------------------- #
    def _on_row_activate(self, _event: "tk.Event | None" = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self._on_read_selected()

    def _on_read_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            self.app.set_status("请先在结果列表中选择一行")
            return
        values = self.tree.item(selection[0], "values")
        if len(values) < 4:
            return
        try:
            x, y, z = int(values[1]), int(values[2]), int(values[3])
        except (TypeError, ValueError):
            return
        result = self._last_result
        dimension = result.dimension if result is not None else self.dimension_var.get()
        block_tab = self.app.block_tab
        self.app.notebook.select(block_tab)
        block_tab.dimension_var.set(dimension)
        block_tab.set_coords(x, y, z)
        try:
            block_tab.read_block()
            state = block_tab.block_display_var.get()
            self.app.set_status(f"已定位 ({x}, {y}, {z}) [{dimension}] = {state}")
        except Exception as exc:  # noqa: BLE001 - UI 边界
            self.app.show_error("读取选中方块失败", exc)

    def _clear_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._last_result = None
        self.result_var.set("尚无结果")
        self.progress.configure(value=0)
        self.status_var.set("已清空结果")

    # ---- UI 回调 --------------------------------------------------------- #
    def _ui_start(self) -> None:
        self._run_ui(self.start_search)

    def _ui_stop(self) -> None:
        self.stop_search()


# --------------------------------------------------------------------------- #
# Tab 4: 方块实体搜索
# --------------------------------------------------------------------------- #
class BlockEntitySearchTab(_Tab):
    """按方块实体类型反向查找当前维度所有已生成区块中的方块实体。

    读取上限 SHORT_MAX=32767 个匹配（达到后停止扫描），
    结果列表最多显示 DISPLAY_LIMIT=256 个坐标。
    """

    title = "方块实体搜索"

    def _build(self) -> None:
        self._running = False
        self._cancel_event = threading.Event()
        self._progress_lock = threading.Lock()
        self._latest_progress: tuple[int, int, int] = (0, 0, 0)
        self._poll_after: Optional[str] = None
        self._last_result = None
        self._search_world_path: Optional[str] = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, sticky="ew")
        ttk.Label(bar, text="维度：").pack(side="left")
        self.dimension_var = tk.StringVar(value="overworld")
        self.dimension_combo = ttk.Combobox(
            bar, textvariable=self.dimension_var, values=["overworld"], width=20,
        )
        self.dimension_combo.pack(side="left")
        ttk.Label(bar, text="  方块实体类型：").pack(side="left")
        self.be_var = tk.StringVar(value="minecraft:chest")
        self.be_entry = ttk.Entry(bar, textvariable=self.be_var, width=30)
        self.be_entry.pack(side="left")
        self.search_button = ttk.Button(
            bar, text="开始查找", style="Accent.TButton", command=self._ui_start,
        )
        self.search_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(
            bar, text="停止", command=self._ui_stop, state="disabled",
        )
        self.stop_button.pack(side="left", padx=(6, 0))

        ttk.Label(
            self,
            text=f"扫描当前维度全部已生成区块的 block_entities；最多统计 {SHORT_MAX} 个匹配，"
                 f"列表最多显示 {DISPLAY_LIMIT} 个坐标。类型以方块实体 NBT 的 id 为准，"
                 "不要用方块 id 推断（例如 trapped_chest 的方块实体是 minecraft:chest）。",
            style="Hint.TLabel",
            wraplength=1080,
        ).grid(row=1, column=0, sticky="w", pady=(6, 4))

        progress_row = ttk.Frame(self)
        progress_row.grid(row=2, column=0, sticky="ew")
        progress_row.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(
            progress_row, orient="horizontal", mode="determinate", maximum=1, value=0,
        )
        self.progress.grid(row=0, column=0, sticky="ew")
        self.status_var = tk.StringVar(value="就绪：选择维度、输入方块实体类型后点击“开始查找”")
        ttk.Label(
            progress_row, textvariable=self.status_var, anchor="w", style="Hint.TLabel",
            wraplength=1080,
        ).grid(row=1, column=0, sticky="w", pady=(4, 6))

        result_box = ttk.LabelFrame(
            self, text="匹配结果（双击行可定位并读取该方块实体）", padding=6,
        )
        result_box.grid(row=3, column=0, sticky="nsew")
        result_box.columnconfigure(0, weight=1)
        result_box.rowconfigure(0, weight=1)
        columns = ("index", "x", "y", "z", "type")
        headings = {"index": "#", "x": "X", "y": "Y", "z": "Z", "type": "方块实体类型"}
        widths = {"index": 60, "x": 110, "y": 110, "z": 110, "type": 520}
        self.tree = ttk.Treeview(
            result_box, columns=columns, show="headings", height=10, selectmode="browse",
        )
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(result_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<Double-1>", self._on_row_activate)
        self.tree.bind("<Return>", self._on_row_activate)

        bottom = ttk.Frame(result_box)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(
            bottom, text="定位并读取选中方块实体", command=self._on_read_selected,
        ).pack(side="left")
        ttk.Button(bottom, text="清空结果", command=self._clear_results).pack(
            side="left", padx=(8, 0)
        )
        self.result_var = tk.StringVar(value="尚无结果")
        ttk.Label(bottom, textvariable=self.result_var, style="Hint.TLabel").pack(
            side="left", padx=(14, 0)
        )

    # ---- 生命周期 -------------------------------------------------------- #
    def refresh(self) -> None:
        world = self.app.world
        if world is None:
            if self._running:
                self._cancel_event.set()
            self.dimension_combo["values"] = ["overworld"]
            self.status_var.set("（未打开世界）")
            self.search_button.configure(state="disabled")
            return
        if self._running and self._search_world_path not in (None, world.path):
            self._cancel_event.set()
        dimensions = world.list_dimensions()
        self.dimension_combo["values"] = dimensions
        current = World.normalize_dimension(self.dimension_var.get())
        if current not in dimensions:
            self.dimension_var.set(dimensions[0] if dimensions else "overworld")
        self.search_button.configure(state="disabled" if self._running else "normal")
        if not self._running:
            self.status_var.set(
                f"就绪：维度 {self.dimension_var.get()}，"
                "输入方块实体类型后点击“开始查找”"
            )

    # ---- 搜索 ------------------------------------------------------------ #
    def start_search(self, be_spec: Optional[str] = None,
                     dimension: Optional[str] = None) -> None:
        world = self.require_world()
        if self._running:
            raise RuntimeError("已有搜索正在进行，请等待完成或点击“停止”。")
        spec = str(be_spec if be_spec is not None else self.be_var.get()).strip()
        if not spec:
            raise ValueError("请填写方块实体类型，例如 minecraft:chest")
        target = normalize_block_entity_id(spec)
        dim = World.normalize_dimension(dimension or self.dimension_var.get())
        if dim not in world.list_dimensions():
            raise ValueError(f"维度不存在或无 region：{dim}")

        self._running = True
        self._search_world_path = world.path
        self._cancel_event.clear()
        with self._progress_lock:
            self._latest_progress = (0, 0, 0)
        self.progress.configure(maximum=1, value=0)
        self.tree.delete(*self.tree.get_children())
        self._last_result = None
        self.result_var.set("搜索中...")
        self.search_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set(f"正在扫描维度 {dim} 的区块，查找方块实体 {target} ...")
        self.app.set_status(f"方块实体搜索：{target} @ {dim}")

        self.be_var.set(target)
        self.dimension_var.set(dim)
        self._poll_progress()
        self.app.run_background(
            lambda: world.find_block_entities(
                target,
                dimension=dim,
                limit=SHORT_MAX,
                display_limit=DISPLAY_LIMIT,
                progress=self._worker_progress,
                cancel_event=self._cancel_event,
            ),
            self._on_search_done,
            description="block-entity-search",
        )

    def stop_search(self) -> None:
        if not self._running:
            return
        self._cancel_event.set()
        self.status_var.set("正在停止搜索...")
        self.stop_button.configure(state="disabled")

    def _worker_progress(self, scanned: int, total: int, found: int) -> None:
        with self._progress_lock:
            self._latest_progress = (int(scanned), int(total), int(found))

    def _poll_progress(self) -> None:
        self._poll_after = None
        if not self._running:
            return
        with self._progress_lock:
            scanned, total, found = self._latest_progress
        if total > 0:
            self.progress.configure(maximum=total)
            self.progress.configure(value=min(scanned, total))
        self.status_var.set(
            f"正在扫描 {scanned}/{total if total else '?'} 个区块，"
            f"已找到 {found} 个匹配（最多统计 {SHORT_MAX} 个）..."
        )
        try:
            self._poll_after = self.after(150, self._poll_progress)
        except tk.TclError:
            self._poll_after = None

    def _on_search_done(self, payload: Any) -> None:
        self._running = False
        if self._poll_after is not None:
            try:
                self.after_cancel(self._poll_after)
            except Exception:  # noqa: BLE001
                pass
            self._poll_after = None
        self.search_button.configure(state="normal")
        self.stop_button.configure(state="disabled")

        if isinstance(payload, BaseException):
            self.progress.configure(value=0)
            self.status_var.set(f"搜索失败：{_exc_text(payload)}")
            self.app.show_error("方块实体搜索失败", payload)
            return

        result = payload
        self._last_result = result
        self.progress.configure(maximum=max(result.chunks_total, 1))
        self.progress.configure(
            value=min(result.chunks_scanned, max(result.chunks_total, 1))
        )

        for index, match in enumerate(result.matches, start=1):
            self.tree.insert(
                "", "end", iid=str(index),
                values=(index, match.x, match.y, match.z, match.be_type),
            )
        shown = len(result.matches)
        if result.cancelled:
            status = (
                f"已取消：找到 {result.total_found} 个匹配"
                f"（扫描 {result.chunks_scanned}/{result.chunks_total} 个区块，"
                f"耗时 {result.elapsed:.1f}s）"
            )
        elif result.truncated:
            status = (
                f"已达到读取上限 {SHORT_MAX}，停止扫描；找到 {result.total_found} 个，"
                f"显示前 {shown} 个（耗时 {result.elapsed:.1f}s）"
            )
        else:
            status = (
                f"完成：找到 {result.total_found} 个匹配，显示前 {shown} 个"
                f"（扫描 {result.chunks_scanned}/{result.chunks_total} 个区块，"
                f"耗时 {result.elapsed:.1f}s）"
            )
        if result.errors:
            status += f"；{len(result.errors)} 个区块读取失败"
        self.status_var.set(status)
        self.result_var.set(
            f"维度 {result.dimension}，方块实体 {result.block_entity}，"
            f"匹配 {result.total_found} 个，显示 {shown} 个"
        )
        self.app.set_status(status)

    # ---- 结果操作 -------------------------------------------------------- #
    def _on_row_activate(self, _event: "tk.Event | None" = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self._on_read_selected()

    def _on_read_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            self.app.set_status("请先在结果列表中选择一行")
            return
        values = self.tree.item(selection[0], "values")
        if len(values) < 4:
            return
        try:
            x, y, z = int(values[1]), int(values[2]), int(values[3])
        except (TypeError, ValueError):
            return
        result = self._last_result
        dimension = result.dimension if result is not None else self.dimension_var.get()
        block_tab = self.app.block_tab
        self.app.notebook.select(block_tab)
        block_tab.dimension_var.set(dimension)
        block_tab.set_coords(x, y, z)
        try:
            block_tab.read_block_entity()
            self.app.set_status(
                f"已定位方块实体 ({x}, {y}, {z}) [{dimension}]，请在方块实体编辑器中查看"
            )
        except Exception as exc:  # noqa: BLE001 - UI 边界
            self.app.show_error("读取选中方块实体失败", exc)

    def _clear_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._last_result = None
        self.result_var.set("尚无结果")

    # ---- UI 回调 --------------------------------------------------------- #
    def _ui_start(self) -> None:
        self._run_ui(self.start_search)

    def _ui_stop(self) -> None:
        self.stop_search()


# --------------------------------------------------------------------------- #
# Tab 5: 玩家数据
# --------------------------------------------------------------------------- #
class PlayerTab(_Tab):
    """玩家列表与属性编辑。"""

    title = "玩家数据"

    def _build(self) -> None:
        self.current_player: Optional[PlayerData] = None
        self.current_uuid: Optional[str] = None
        self._suppress_select = False
        self._inventory_dialog = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.rowconfigure(2, weight=2)

        tree_frame = ttk.Frame(self)
        tree_frame.grid(row=0, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        columns = ("uuid", "dimension", "game_type", "health", "food", "xp_level", "position")
        headings = {
            "uuid": "UUID", "dimension": "维度", "game_type": "模式",
            "health": "HP", "food": "饥饿", "xp_level": "XP 等级", "position": "位置",
        }
        widths = {"uuid": 290, "dimension": 150, "game_type": 110, "health": 70,
                  "food": 60, "xp_level": 80, "position": 170}
        self.tree = ttk.Treeview(
            tree_frame, columns=columns, show="headings", height=8, selectmode="browse",
        )
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_select)
        self.player_tree = self.tree  # 便于脚本/测试使用
        self.treeview = self.tree  # 别名

        hint_bar = ttk.Frame(self)
        hint_bar.grid(row=1, column=0, sticky="ew", pady=(6, 8))
        hint_bar.columnconfigure(0, weight=1)
        self.player_hint_var = tk.StringVar(value="（未打开世界）")
        self.player_hint_label = ttk.Label(
            hint_bar, textvariable=self.player_hint_var, anchor="w", style="Hint.TLabel",
            wraplength=1000,
        )
        self.player_hint_label.grid(row=0, column=0, sticky="w")
        self.refresh_players_button = ttk.Button(
            hint_bar, text="刷新玩家列表", command=lambda: self._run_ui(self.refresh_players),
        )
        self.refresh_players_button.grid(row=0, column=1)

        # ---- 属性编辑 ---------------------------------------------------- #
        edit_box = ttk.LabelFrame(self, text="玩家属性（选中玩家后可修改）", padding=8)
        edit_box.grid(row=2, column=0, sticky="nsew")
        edit_box.columnconfigure(1, weight=1)
        edit_box.columnconfigure(3, weight=1)
        edit_box.columnconfigure(5, weight=1)

        self.health_var = tk.StringVar()
        self.food_var = self.food_level_var = tk.StringVar()
        self.saturation_var = self.food_saturation_var = tk.StringVar()
        self.xp_level_var = tk.StringVar()
        self.xp_progress_var = self.xp_p_var = tk.StringVar()
        self.xp_total_var = tk.StringVar()
        self.pos_x_var = tk.StringVar()
        self.pos_y_var = tk.StringVar()
        self.pos_z_var = tk.StringVar()
        self.player_gametype_var = self.player_game_type_var = tk.StringVar(
            value=GAME_TYPE_CHOICES[0]
        )
        self.player_dimension_var = tk.StringVar(value="overworld")

        def add_field(row: int, column: int, label: str, var: tk.StringVar,
                      width: int = 12) -> ttk.Entry:
            ttk.Label(edit_box, text=f"{label}：").grid(
                row=row, column=column * 2, sticky="w", pady=3
            )
            entry = ttk.Entry(edit_box, textvariable=var, width=width)
            entry.grid(row=row, column=column * 2 + 1, sticky="w", pady=3)
            return entry

        self.player_entries: dict[str, ttk.Entry] = {}
        self.player_entries["health"] = add_field(0, 0, "生命值 Health", self.health_var)
        self.player_entries["food"] = add_field(0, 1, "饥饿值 foodLevel", self.food_var)
        self.player_entries["saturation"] = add_field(
            0, 2, "饱和度 foodSaturationLevel", self.saturation_var, width=14
        )
        self.player_entries["xp_level"] = add_field(1, 0, "经验等级 XpLevel", self.xp_level_var)
        self.player_entries["xp_progress"] = add_field(1, 1, "经验进度 XpP (0≤x<1)", self.xp_progress_var, width=14)
        self.player_entries["xp_total"] = add_field(1, 2, "总经验 XpTotal", self.xp_total_var)
        self.player_entries["pos_x"] = add_field(2, 0, "位置 X", self.pos_x_var)
        self.player_entries["pos_y"] = add_field(2, 1, "位置 Y", self.pos_y_var)
        self.player_entries["pos_z"] = add_field(2, 2, "位置 Z", self.pos_z_var)

        ttk.Label(edit_box, text="游戏模式：").grid(row=3, column=0, sticky="w", pady=3)
        self.player_gametype_combo = ttk.Combobox(
            edit_box, textvariable=self.player_gametype_var, values=GAME_TYPE_CHOICES,
            state="readonly", width=12,
        )
        self.player_gametype_combo.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Label(edit_box, text="维度：").grid(row=3, column=2, sticky="e", padx=(12, 6), pady=3)
        self.player_dimension_combo = ttk.Combobox(
            edit_box, textvariable=self.player_dimension_var,
            values=PLAYER_DIMENSION_CHOICES, state="readonly", width=14,
        )
        self.player_dimension_combo.grid(row=3, column=3, sticky="w", pady=3)

        self.player_current_var = tk.StringVar(value="当前玩家：—")
        ttk.Label(
            edit_box, textvariable=self.player_current_var, anchor="w", style="Hint.TLabel",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        button_row = ttk.Frame(edit_box)
        button_row.grid(row=5, column=0, columnspan=6, sticky="w", pady=(8, 0))
        self.apply_player_button = ttk.Button(
            button_row, text="应用修改", style="Accent.TButton", command=self._ui_apply,
        )
        self.apply_player_button.pack(side="left")
        self.heal_button = ttk.Button(
            button_row, text="回满血", command=self._ui_heal,
        )
        self.heal_button.pack(side="left", padx=(8, 0))
        self.inventory_button = ttk.Button(
            button_row, text="编辑物品栏...", command=self._ui_inventory,
        )
        self.inventory_button.pack(side="left", padx=(8, 0))

    # ---- 内部 ------------------------------------------------------------ #
    def _on_tree_select(self, _event: "tk.Event | None" = None) -> None:
        if self._suppress_select:
            return
        selection = self.tree.selection()
        if not selection:
            return
        selected_uuid = selection[0]
        # 关键防递归: select_player() 会调用 _sync_tree_selection()，
        # 而 selection_set() 又会触发 <<TreeviewSelect>>。若选中的就是当前已加载
        # 的玩家，直接返回，避免 "选中 -> 加载 -> 再选中" 无限循环导致界面无响应。
        if selected_uuid == self.current_uuid and self.current_player is not None:
            return
        try:
            self.select_player(selected_uuid)
        except Exception as exc:  # noqa: BLE001 - UI 事件边界
            self.app.show_error("加载玩家失败", exc)

    def _ui_apply(self) -> None:
        self._run_ui(self.apply_player_edits)

    def _ui_heal(self) -> None:
        self._run_ui(self.heal_player)

    def _ui_inventory(self) -> None:
        """打开当前玩家的物品栏编辑对话框。"""
        player = self.current_player
        if player is None:
            self.app.set_status("请先在玩家列表中选择一个玩家")
            return
        dialog = self._inventory_dialog
        if dialog is not None:
            try:
                if dialog.winfo_exists():
                    dialog.deiconify()
                    dialog.lift()
                    dialog.focus_force()
                    return
            except tk.TclError:
                pass
        try:
            self._inventory_dialog = InventoryEditorDialog(self, self.app, player)
        except Exception as exc:  # noqa: BLE001 - UI 边界
            self.app.show_error("打开物品栏失败", exc)

    def _clear_fields(self) -> None:
        for var in (
            self.health_var, self.food_var, self.saturation_var,
            self.xp_level_var, self.xp_progress_var, self.xp_total_var,
            self.pos_x_var, self.pos_y_var, self.pos_z_var,
        ):
            var.set("")
        self.player_gametype_var.set(GAME_TYPE_CHOICES[0])
        self.player_dimension_var.set("overworld")
        self.player_current_var.set("当前玩家：—")

    def _sync_tree_selection(self) -> None:
        if self.current_uuid and self.tree.exists(self.current_uuid):
            # 已经是选中状态时不要再 selection_set: 这会再次触发
            # <<TreeviewSelect>>，与 _on_tree_select 形成递归。
            if self.tree.selection() == (self.current_uuid,):
                return
            self._suppress_select = True
            try:
                self.tree.selection_set(self.current_uuid)
                self.tree.see(self.current_uuid)
            finally:
                self._suppress_select = False

    # ---- 公开方法 -------------------------------------------------------- #
    def refresh_players(self) -> None:
        """重新扫描 playerdata 目录并刷新玩家列表。"""
        self._suppress_select = True
        try:
            self.tree.delete(*self.tree.get_children())
            world = self.app.world
            self.current_player = None
            self.current_uuid = None
            self._clear_fields()
            if world is None:
                self.player_hint_var.set("（未打开世界）")
                return
            players = world.list_players()
            loaded = 0
            broken = 0
            for player in players:
                if not hasattr(player, "tag") or not isinstance(
                    getattr(player, "tag", None), CompoundTag
                ):
                    broken += 1
                    continue
                try:
                    summary = player.summary()
                except Exception:  # noqa: BLE001
                    broken += 1
                    continue
                position = summary.get("position")
                if position:
                    position_text = (
                        f"({position[0]:.1f}, {position[1]:.1f}, {position[2]:.1f})"
                    )
                else:
                    position_text = "—"
                self.tree.insert(
                    "", "end", iid=summary["uuid"],
                    values=(
                        summary["uuid"],
                        _dimension_short(summary.get("dimension", "")),
                        summary.get("game_type", "unknown"),
                        f"{summary.get('health', 0.0):.1f}",
                        summary.get("food_level", 0),
                        summary.get("xp_level", 0),
                        position_text,
                    ),
                )
                loaded += 1
            hints = [f"共 {loaded} 个玩家"]
            if broken:
                hints.append(f"{broken} 个文件读取失败")
            if loaded == 0:
                hints.append("未找到玩家数据（playerdata 目录为空）")
            warning = getattr(world, "host_player_warning", None)
            if warning:
                hints.append(f"警告：{warning}")
            elif getattr(world, "host_player_uuid", None):
                hints.append("单人主机：玩家修改会同步到 level.dat 的 Player 快照")
            self.player_hint_var.set("；".join(hints))
        finally:
            self._suppress_select = False

    def select_player(self, uuid: str) -> None:
        """按 UUID 加载玩家并在下方显示可编辑属性。"""
        world = self.require_world()
        text = str(uuid).strip()
        if not text:
            raise ValueError("玩家 UUID 不能为空")
        player = world.get_player(text)
        self.current_player = player
        self.current_uuid = player.uuid
        summary = player.summary()

        self.health_var.set(f"{summary.get('health', 20.0):g}")
        self.food_var.set(str(summary.get("food_level", 20)))
        self.saturation_var.set(f"{summary.get('food_saturation', 5.0):g}")
        self.xp_level_var.set(str(summary.get("xp_level", 0)))
        self.xp_progress_var.set(f"{summary.get('xp_progress', 0.0):g}")
        self.xp_total_var.set(str(summary.get("xp_total", 0)))
        position = summary.get("position")
        if position:
            self.pos_x_var.set(f"{position[0]:.3f}")
            self.pos_y_var.set(f"{position[1]:.3f}")
            self.pos_z_var.set(f"{position[2]:.3f}")
        else:
            self.pos_x_var.set("0")
            self.pos_y_var.set("0")
            self.pos_z_var.set("0")
        self.player_gametype_var.set(str(summary.get("game_type", "survival")))
        dimension = _dimension_short(str(summary.get("dimension", "overworld")))
        values = list(PLAYER_DIMENSION_CHOICES)
        if dimension not in values:
            values.append(dimension)
        self.player_dimension_combo["values"] = values
        self.player_dimension_var.set(dimension)
        self.player_current_var.set(
            f"当前玩家：{player.uuid}（{os.path.basename(player.path)}）"
        )
        self._sync_tree_selection()
        self.app.set_status(f"已加载玩家 {player.uuid}")

    def apply_player_edits(self, **overrides: Any) -> None:
        """校验并保存玩家属性修改。"""
        world = self.require_world()
        player = self.current_player
        if player is None:
            raise RuntimeError("请先在玩家列表中选择一个玩家（双击或单击选中）")

        def pick(keys: tuple[str, ...], var: tk.StringVar) -> Any:
            return self._pick(overrides, keys, var)

        health = _parse_float(pick(("health",), self.health_var), "生命值 Health", minimum=0.0)
        if health > 1e6:
            raise ValueError("生命值不能超过 1000000")
        food = _parse_int(pick(("food", "food_level"), self.food_var), "饥饿值 foodLevel", 0, 20)
        saturation = _parse_float(
            pick(("saturation", "food_saturation"), self.saturation_var),
            "饱和度 foodSaturationLevel", minimum=0.0,
        )
        xp_level = _parse_int(pick(("xp_level",), self.xp_level_var), "经验等级 XpLevel", minimum=0)
        xp_progress = _parse_float(pick(("xp_progress", "xp_p"), self.xp_progress_var), "经验进度 XpP")
        if not 0.0 <= xp_progress < 1.0:
            raise ValueError(f"经验进度 XpP 必须在 [0,1) 区间（当前 {xp_progress:g}）")
        xp_total = _parse_int(pick(("xp_total",), self.xp_total_var), "总经验 XpTotal", minimum=0)
        pos_x = _parse_float(pick(("pos_x", "x"), self.pos_x_var), "位置 X")
        pos_y = _parse_float(pick(("pos_y", "y"), self.pos_y_var), "位置 Y")
        pos_z = _parse_float(pick(("pos_z", "z"), self.pos_z_var), "位置 Z")
        game_type = str(pick(("game_type", "gametype"), self.player_gametype_var)).strip().lower()
        if game_type not in GAME_TYPE_CHOICES:
            raise ValueError(
                f"未知游戏模式：{game_type!r}（可选 {' / '.join(GAME_TYPE_CHOICES)}）"
            )
        dimension = str(pick(("dimension",), self.player_dimension_var)).strip()
        if not dimension:
            raise ValueError("维度不能为空")

        player.set_health(health)
        player.set_food(food, saturation)
        player.set_xp(xp_level, xp_progress, xp_total)
        player.set_position(pos_x, pos_y, pos_z)
        player.set_game_type(game_type)
        player.set_dimension(dimension)

        world.mark_player_dirty(player)
        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])

        player_uuid = player.uuid
        self.refresh_players()
        self.select_player(player_uuid)
        self.app.show_backups(new_backups)
        self.app.set_status(f"已保存玩家 {player_uuid} 的属性修改")

    def heal_player(self) -> None:
        """把当前玩家回满血并恢复饥饿度。"""
        world = self.require_world()
        player = self.current_player
        if player is None:
            raise RuntimeError("请先在玩家列表中选择一个玩家（双击或单击选中）")
        player.heal()
        world.mark_player_dirty(player)
        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])
        player_uuid = player.uuid
        self.refresh_players()
        self.select_player(player_uuid)
        self.app.show_backups(new_backups)
        self.app.set_status(f"玩家 {player_uuid} 已回满血")

    def refresh(self) -> None:
        self.refresh_players()


# --------------------------------------------------------------------------- #
# 玩家物品栏编辑对话框
# --------------------------------------------------------------------------- #
class InventoryEditorDialog(tk.Toplevel):
    """玩家物品栏（快捷栏/主背包/盔甲/副手）查看与修改。

    物品格式跟随世界检测/选择的处理版本：1.20.5+ 使用 components，1.20.1 使用 tag。
    每次保存立即写入 playerdata 并由核心层自动备份。
    """

    def __init__(self, parent_tab: "PlayerTab", app: "McToolGUI", player: PlayerData):
        super().__init__(app)
        world = app.world
        if world is None:
            raise RuntimeError("尚未打开世界，无法编辑物品栏。")
        self.parent_tab = parent_tab
        self.app = app
        self.world = world
        self.player = player
        self.item_format = world.version_profile.item_format
        self.extra_tag_name = extra_key(self.item_format)
        self._current_slot: Optional[int] = None
        self._suppress = False

        self.title(f"物品栏编辑 — {player.uuid}")
        self.transient(app)
        self.geometry("1080x680")
        self.minsize(920, 560)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._build()
        self._reload_rows()

    def _build(self) -> None:
        header = ttk.Frame(self, padding=(10, 8, 10, 0))
        header.pack(fill="x")
        ttk.Label(
            header, text=f"玩家：{self.player.uuid}",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text=(
                f"处理版本 {self.world.version_profile.id}；物品格式 {self.item_format}；"
                f"额外 NBT 字段 {self.extra_tag_name}。所有修改立即写入 playerdata 并自动备份。"
            ),
            style="Hint.TLabel",
            wraplength=1040,
        ).pack(anchor="w", pady=(2, 0))

        body = ttk.Frame(self, padding=10)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        # ---- 槽位列表 ---------------------------------------------------- #
        left = ttk.LabelFrame(body, text="物品栏槽位（双击/选中行编辑）", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        columns = ("slot_label", "item_id", "count", "extra")
        headings = {
            "slot_label": "槽位", "item_id": "物品 id", "count": "数量", "extra": "额外 NBT",
        }
        widths = {"slot_label": 130, "item_id": 260, "count": 60, "extra": 300}
        self.tree = ttk.Treeview(
            left, columns=columns, show="headings", height=18, selectmode="browse",
        )
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        # ---- 编辑表单 ---------------------------------------------------- #
        right = ttk.LabelFrame(body, text="槽位编辑", padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(4, weight=1)

        ttk.Label(right, text="当前槽位：").grid(row=0, column=0, sticky="w", pady=2)
        self.slot_var = tk.StringVar(value="（未选择）")
        ttk.Label(
            right, textvariable=self.slot_var, style="Hint.TLabel",
        ).grid(row=0, column=1, sticky="w", pady=2)

        ttk.Label(right, text="物品 id：").grid(row=1, column=0, sticky="w", pady=2)
        self.id_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.id_var).grid(
            row=1, column=1, sticky="ew", pady=2
        )

        ttk.Label(right, text="数量（1..99）：").grid(row=2, column=0, sticky="w", pady=2)
        self.count_var = tk.StringVar(value="1")
        ttk.Entry(right, textvariable=self.count_var, width=12).grid(
            row=2, column=1, sticky="w", pady=2
        )

        ttk.Label(
            right, text=f"额外 NBT（{self.extra_tag_name}，SNBT）：",
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 2))
        self.extra_editor = ScrolledText(
            right, height=12, wrap="word", undo=False, font=self.app.mono_font,
        )
        self.extra_editor.grid(row=4, column=0, columnspan=2, sticky="nsew")

        ttk.Label(
            right,
            text="留空表示无额外数据；保存时保留物品上其它未知字段。",
            style="Hint.TLabel",
            wraplength=360,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        buttons = ttk.Frame(right)
        buttons.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.save_button = ttk.Button(
            buttons, text="保存到该槽位", style="Accent.TButton", command=self._save,
        )
        self.save_button.pack(side="left")
        self.delete_button = ttk.Button(
            buttons, text="删除该槽位物品", command=self._delete,
        )
        self.delete_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text="重新读取", command=self._refresh).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(buttons, text="关闭", command=self._close).pack(
            side="left", padx=(8, 0)
        )

        self.status_var = tk.StringVar(value="选择左侧槽位后编辑")
        ttk.Label(
            right, textvariable=self.status_var, style="Hint.TLabel", wraplength=360,
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ---- 列表/表单 -------------------------------------------------------- #
    def _on_tree_select(self, _event: "tk.Event | None" = None) -> None:
        if self._suppress:
            return
        selection = self.tree.selection()
        if not selection:
            return
        try:
            slot = int(selection[0])
        except ValueError:
            return
        self._load_slot(slot)

    def _load_slot(self, slot: int) -> None:
        self._current_slot = slot
        item = self.player.get_inventory_item(slot)
        self.slot_var.set(f"{slot} — {slot_label(slot)}")
        if item is None:
            self.id_var.set("")
            self.count_var.set("1")
            _set_text(self.extra_editor, "", readonly=False)
            self.delete_button.configure(state="disabled")
            self.status_var.set("空槽位：填写物品 id/数量后点击“保存到该槽位”。")
            return
        self.id_var.set(item_id(item))
        self.count_var.set(str(item_count(item, self.item_format)))
        extra = item_extra(item, self.item_format)
        if extra is not None and len(extra) > 0:
            _set_text(self.extra_editor, to_snbt_pretty(extra), readonly=False)
        else:
            _set_text(self.extra_editor, "", readonly=False)
        self.delete_button.configure(state="normal")
        self.status_var.set(
            f"已读取槽位 {slot}：{describe_item(item, self.item_format)}"
        )

    def _reload_rows(self, select_slot: Optional[int] = None) -> None:
        self._suppress = True
        try:
            self.tree.delete(*self.tree.get_children())
            items = self.player.inventory_slot_map()
            for slot in INVENTORY_SLOTS:
                item = items.get(slot)
                if item is None:
                    values = (slot_label(slot), "—", "", "")
                else:
                    extra = item_extra(item, self.item_format)
                    preview = ""
                    if extra is not None and len(extra) > 0:
                        preview = to_snbt(extra)
                        if len(preview) > 80:
                            preview = preview[:77] + "..."
                    values = (
                        slot_label(slot),
                        item_id(item),
                        str(item_count(item, self.item_format)),
                        preview,
                    )
                self.tree.insert("", "end", iid=str(slot), values=values)
            target = select_slot if select_slot is not None else self._current_slot
            if target is None:
                nonempty = [slot for slot in INVENTORY_SLOTS if slot in items]
                target = nonempty[0] if nonempty else INVENTORY_SLOTS[0]
            if self.tree.exists(str(target)):
                self.tree.selection_set(str(target))
                self.tree.see(str(target))
        finally:
            self._suppress = False
        if target is not None and self.tree.exists(str(target)):
            self._load_slot(target)

    # ---- 操作 ------------------------------------------------------------ #
    def _parse_extra(self) -> Optional[CompoundTag]:
        text = _get_text(self.extra_editor).strip()
        if not text:
            return None
        try:
            tag = parse_snbt(text)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"额外 NBT 解析失败：{_exc_text(exc)}") from exc
        if not isinstance(tag, CompoundTag):
            raise ValueError("额外 NBT 必须是一个 compound，例如 { ... }")
        return tag

    def _flush_player(self) -> str:
        before = len(self.world.backups)
        self.world.mark_player_dirty(self.player)
        self.world.flush()
        new_backups = list(self.world.backups[before:])
        self.app.show_backups(new_backups)
        try:
            self.parent_tab.refresh_players()
            self.parent_tab.select_player(self.player.uuid)
        except Exception:  # noqa: BLE001 - 父页面刷新失败不影响写入
            pass
        return f"已备份 {len(new_backups)} 个文件" if new_backups else "已写入"

    def _save(self) -> None:
        try:
            if self._current_slot is None:
                raise ValueError("请先选择一个槽位")
            if not self.id_var.get().strip():
                raise ValueError("请填写物品 id（例如 minecraft:diamond）")
            try:
                count = int(self.count_var.get().strip())
            except ValueError:
                raise ValueError("物品数量必须是整数") from None
            extra = self._parse_extra()
            existing = self.player.get_inventory_item(self._current_slot)
            if existing is not None:
                item = update_item_fields(
                    existing, self.id_var.get(), count, extra, self.item_format,
                )
            else:
                item = make_item(
                    self.id_var.get(), count, extra, self.item_format,
                    slot=self._current_slot,
                )
            self.player.set_inventory_item(self._current_slot, item)
            backup_status = self._flush_player()
        except Exception as exc:  # noqa: BLE001 - 对话框边界
            messagebox.showerror("保存物品失败", _exc_text(exc), parent=self)
            return
        slot = self._current_slot
        self._reload_rows(select_slot=slot)
        self.status_var.set(
            f"已保存槽位 {slot}：{describe_item(item, self.item_format)}；{backup_status}"
        )
        self.app.set_status(f"物品栏：已保存槽位 {slot} 的物品")

    def _delete(self) -> None:
        if self._current_slot is None:
            return
        slot = self._current_slot
        if self.player.get_inventory_item(slot) is None:
            self.status_var.set("该槽位为空，无需删除")
            return
        if not messagebox.askyesno(
            "删除物品", f"确定删除槽位 {slot}（{slot_label(slot)}）的物品？", parent=self,
        ):
            return
        try:
            self.player.delete_inventory_item(slot)
            backup_status = self._flush_player()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("删除物品失败", _exc_text(exc), parent=self)
            return
        self._reload_rows(select_slot=slot)
        self.status_var.set(f"已删除槽位 {slot} 的物品；{backup_status}")

    def _refresh(self) -> None:
        try:
            self.player = self.world.get_player(self.player.uuid)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("重新读取失败", _exc_text(exc), parent=self)
            return
        self._reload_rows()
        self.status_var.set("已从 playerdata 重新读取物品栏")

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.parent_tab._inventory_dialog = None
        try:
            self.parent_tab.refresh_players()
            if self.parent_tab.current_uuid:
                self.parent_tab.select_player(self.parent_tab.current_uuid)
        except Exception:  # noqa: BLE001
            pass
        self.app.set_status("已关闭物品栏编辑器")
        self.destroy()


# --------------------------------------------------------------------------- #
# 模组数据页（子标签页：Curios）
# --------------------------------------------------------------------------- #
class ModDataTab(_Tab):
    """模组扩展数据页；每个子标签页自行标注模组/游戏/加载器版本。"""

    title = "模组数据"

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.curios_tab = CuriosTab(self.notebook, self.app)
        self.notebook.add(self.curios_tab, text="Curios")

    def refresh(self) -> None:
        self.curios_tab.refresh()


class CuriosTab(_Tab):
    """Curios 玩家饰品栏读取/修改。

    版本标注为工作区 Curios-1.21.1 源码的构建元数据：
      - Curios 9.5.1+1.21.1
      - Minecraft 1.21.1
      - NeoForge 21.1.60+（范围 [21.1.60,)）
    """

    title = "Curios"
    MOD_NAME = "Curios"
    MOD_VERSION = CURIO_MOD_VERSION
    GAME_VERSION = CURIO_MC_VERSION
    LOADER_VERSION = CURIO_NEOFORGE_VERSION

    def _build(self) -> None:
        self.player: Optional[PlayerData] = None
        self.groups = []
        self._row_map: dict[str, tuple[str, int]] = {}
        self._current: Optional[tuple[str, int]] = None
        self._suppress_select = False

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # ---- 版本标注 / 警告 --------------------------------------------- #
        banner = ttk.Frame(self, padding=(8, 6, 8, 0))
        banner.grid(row=0, column=0, sticky="ew")
        banner.columnconfigure(0, weight=1)
        ttk.Label(
            banner,
            text=f"适用模组：Curios {self.MOD_VERSION}",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            banner,
            text=(
                f"适用游戏：Minecraft {self.GAME_VERSION}　　"
                f"适用加载器：NeoForge {self.LOADER_VERSION}"
            ),
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            banner,
            text=CURIO_VERSION_WARNING,
            style="Warn.TLabel",
            wraplength=1100,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(3, 0))
        self.mismatch_var = tk.StringVar()
        ttk.Label(
            banner, textvariable=self.mismatch_var, style="Warn.TLabel",
            wraplength=1100, justify="left",
        ).grid(row=3, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            banner,
            text=(
                "数据路径：playerdata/<uuid>.dat → neoforge:attachments → "
                "curios:inventory → Curios（存档不记录模组/加载器版本，请自行核对实际安装版本）"
            ),
            style="Hint.TLabel",
        ).grid(row=4, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            banner, text=CURIO_VALIDATION_WARNING,
            style="Hint.TLabel", wraplength=1100, justify="left",
        ).grid(row=5, column=0, sticky="w", pady=(2, 0))

        # ---- 玩家选择 ---------------------------------------------------- #
        player_bar = ttk.Frame(self, padding=(8, 6))
        player_bar.grid(row=1, column=0, sticky="ew")
        player_bar.columnconfigure(3, weight=1)
        ttk.Label(player_bar, text="玩家：").grid(row=0, column=0, sticky="w")
        self.player_var = tk.StringVar()
        self.player_combo = ttk.Combobox(
            player_bar, textvariable=self.player_var, state="readonly", width=42,
        )
        self.player_combo.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.player_combo.bind("<<ComboboxSelected>>", self._on_player_change)
        ttk.Button(
            player_bar, text="重新读取", command=self._reload_player,
        ).grid(row=0, column=2, sticky="w")
        self.player_info_var = tk.StringVar(value="（未打开世界）")
        ttk.Label(
            player_bar, textvariable=self.player_info_var, style="Hint.TLabel",
            wraplength=700, justify="left",
        ).grid(row=0, column=3, sticky="w", padx=(8, 0))

        # ---- 槽位列表 / 编辑表单 ----------------------------------------- #
        body = ttk.Frame(self, padding=(8, 0, 8, 4))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(body, text="Curios 装备槽（Stacks；外观槽 Cosmetics 保留只读）", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        columns = ("group", "slot", "item", "count", "extra")
        headings = {
            "group": "槽位组", "slot": "槽位", "item": "物品 id",
            "count": "数量", "extra": "额外 NBT",
        }
        widths = {"group": 210, "slot": 70, "item": 260, "count": 55, "extra": 330}
        self.tree = ttk.Treeview(
            left, columns=columns, show="headings", height=18, selectmode="browse",
        )
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        right = ttk.LabelFrame(body, text="Curios 槽位编辑", padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(4, weight=1)

        ttk.Label(right, text="当前槽位：").grid(row=0, column=0, sticky="w", pady=2)
        self.slot_var = tk.StringVar(value="（未选择）")
        ttk.Label(
            right, textvariable=self.slot_var, style="Hint.TLabel",
            wraplength=360, justify="left",
        ).grid(row=0, column=1, sticky="w", pady=2)

        ttk.Label(right, text="物品 id：").grid(row=1, column=0, sticky="w", pady=2)
        self.id_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.id_var).grid(
            row=1, column=1, sticky="ew", pady=2
        )

        ttk.Label(right, text="数量（1..99）：").grid(row=2, column=0, sticky="w", pady=2)
        self.count_var = tk.StringVar(value="1")
        ttk.Entry(right, textvariable=self.count_var, width=12).grid(
            row=2, column=1, sticky="w", pady=2
        )

        self.extra_label_var = tk.StringVar(value="额外 NBT（components，SNBT）：")
        ttk.Label(right, textvariable=self.extra_label_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 2),
        )
        self.extra_editor = ScrolledText(
            right, height=12, wrap="word", undo=False, font=self.app.mono_font,
        )
        self.extra_editor.grid(row=4, column=0, columnspan=2, sticky="nsew")
        ttk.Label(
            right,
            text=(
                "只写入所选槽位组的 Stacks.Items；保留 Renders/ActiveStates/"
                "Cosmetics/DropRule 与附件里的其它模组数据。留空表示无额外 NBT。"
            ),
            style="Hint.TLabel", wraplength=380, justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        buttons = ttk.Frame(right)
        buttons.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.save_button = ttk.Button(
            buttons, text="保存到该槽位", style="Accent.TButton", command=self._save,
        )
        self.save_button.pack(side="left")
        self.delete_button = ttk.Button(
            buttons, text="删除该槽位物品", command=self._delete,
        )
        self.delete_button.pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text="重新读取", command=self._reload_player,
        ).pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="选择左侧槽位后编辑")
        ttk.Label(
            right, textvariable=self.status_var, style="Hint.TLabel",
            wraplength=380, justify="left",
        ).grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))

    # ---- 玩家 / 数据加载 -------------------------------------------------- #
    def refresh(self) -> None:
        world = self.app.world
        if world is None:
            self.player = None
            self.groups = []
            self._row_map.clear()
            self.player_combo.configure(values=[])
            self.player_var.set("")
            self.player_info_var.set("（未打开世界）")
            self.mismatch_var.set("")
            self.groups = []
            self._row_map.clear()
            self._reload_table()
            self._clear_editor()
            self.status_var.set("未打开世界")
            return
        if world.version_profile.id != self.GAME_VERSION:
            self.mismatch_var.set(
                f"当前存档处理版本 {world.version_profile.id} 与 Curios 子标签适用版本 "
                f"Minecraft {self.GAME_VERSION} 不一致；结构可能不同，修改有风险。"
            )
        else:
            self.mismatch_var.set("")
        players = [
            p for p in world.list_players()
            if isinstance(getattr(p, "tag", None), CompoundTag)
            and not getattr(p, "_load_error", None)
        ]
        values = [p.uuid for p in players]
        active = self.player_var.get()
        if active not in values:
            active = values[0] if values else ""
        self.player_combo.configure(values=values)
        self.player_var.set(active)
        if not values:
            self.player = None
            self.groups = []
            self._row_map.clear()
            self.player_info_var.set("playerdata 中没有可读玩家")
            self._reload_table()
            self.status_var.set("没有玩家数据")
            return
        try:
            self._load_player(active)
        except Exception as exc:  # noqa: BLE001 - UI 边界
            self.app.show_error("加载 Curios 数据失败", exc)

    def _on_player_change(self, _event: "tk.Event | None" = None) -> None:
        uuid = self.player_var.get()
        if uuid:
            try:
                self._load_player(uuid)
            except Exception as exc:  # noqa: BLE001
                self.app.show_error("加载 Curios 数据失败", exc)

    def _reload_player(self) -> None:
        world = self.app.world
        uuid = self.player_var.get()
        if world is None or not uuid:
            return
        try:
            self.player = world.get_player(uuid)
            self._reload_table()
            self.status_var.set("已从 playerdata 重新读取 Curios 数据")
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("重新读取 Curios 数据失败", exc)

    def _load_player(self, uuid: str) -> None:
        if self.app.world is None:
            return
        self.player = self.app.world.get_player(uuid)
        self.groups = list_groups(self.player)
        item_format = self.app.world.version_profile.item_format
        self.extra_label_var.set(f"额外 NBT（{extra_key(item_format)}，SNBT）：")
        if not has_curios(self.player):
            self.player_info_var.set(
                f"{uuid}：没有 Curios 数据（可能未安装 Curios 或该玩家从未使用）"
            )
        else:
            total_slots = sum(max(0, g.size) for g in self.groups)
            equipped = sum(len(g.items) for g in self.groups)
            self.player_info_var.set(
                f"{uuid}：{len(self.groups)} 个槽位组，{total_slots} 个槽位，"
                f"{equipped} 件已装备"
            )
        self._reload_table()
        self.status_var.set("已加载 Curios 数据")

    def _reload_table(self, select: Optional[tuple[str, int]] = None) -> None:
        self._suppress_select = True
        try:
            self.tree.delete(*self.tree.get_children())
            self._row_map.clear()
            item_format = (
                self.app.world.version_profile.item_format if self.app.world else "components"
            )
            for group in self.groups:
                for slot in range(max(0, group.size)):
                    item = group.items.get(slot)
                    iid = f"{group.index}:{slot}"
                    self._row_map[iid] = (group.identifier, slot)
                    if item is None:
                        values = (
                            f"{slot_display_name(group.identifier)}（{group.identifier}）",
                            f"{slot + 1}/{group.size}", "—", "", "",
                        )
                    else:
                        extra = item_extra(item, item_format)
                        preview = ""
                        if extra is not None and len(extra) > 0:
                            preview = to_snbt(extra)
                            if len(preview) > 90:
                                preview = preview[:87] + "..."
                        values = (
                            f"{slot_display_name(group.identifier)}（{group.identifier}）",
                            f"{slot + 1}/{group.size}",
                            item_id(item),
                            str(item_count(item, item_format)),
                            preview,
                        )
                    self.tree.insert("", "end", iid=iid, values=values)
            target = select
            if target is not None:
                target_iid = next(
                    (iid for iid, ref in self._row_map.items() if ref == target), None
                )
                if target_iid is None:
                    target_iid = next(iter(self._row_map), None)
            else:
                target_iid = next(iter(self._row_map), None)
            if target_iid is not None and self.tree.exists(target_iid):
                self.tree.selection_set(target_iid)
                self.tree.see(target_iid)
        finally:
            self._suppress_select = False
        selection = self.tree.selection()
        if selection:
            self._load_row(selection[0])
        else:
            self._clear_editor()

    # ---- 编辑器 ---------------------------------------------------------- #
    def _on_tree_select(self, _event: "tk.Event | None" = None) -> None:
        if self._suppress_select:
            return
        selection = self.tree.selection()
        if selection:
            self._load_row(selection[0])

    def _load_row(self, iid: str) -> None:
        reference = self._row_map.get(iid)
        if reference is None:
            return
        identifier, slot = reference
        group = next((g for g in self.groups if g.identifier == identifier), None)
        if group is None:
            return
        self._current = (identifier, slot)
        item = group.items.get(slot)
        self.slot_var.set(
            f"{slot_display_name(identifier)}（{identifier}）— 槽位 "
            f"{slot + 1}/{group.size}"
        )
        if item is None:
            self.id_var.set("")
            self.count_var.set("1")
            _set_text(self.extra_editor, "", readonly=False)
            self.delete_button.configure(state="disabled")
            self.status_var.set("空 Curios 槽位：填写物品后点击“保存到该槽位”。")
            return
        item_format = self.app.world.version_profile.item_format
        self.id_var.set(item_id(item))
        self.count_var.set(str(item_count(item, item_format)))
        extra = item_extra(item, item_format)
        if extra is not None and len(extra) > 0:
            _set_text(self.extra_editor, to_snbt_pretty(extra), readonly=False)
        else:
            _set_text(self.extra_editor, "", readonly=False)
        self.delete_button.configure(state="normal")
        self.status_var.set(
            f"已读取 {identifier} 槽位 {slot + 1}：{item_id(item) or '?'}"
        )

    def _clear_editor(self) -> None:
        self._current = None
        self.slot_var.set("（未选择）")
        self.id_var.set("")
        self.count_var.set("1")
        _set_text(self.extra_editor, "", readonly=False)
        self.delete_button.configure(state="disabled")

    def _parse_extra(self) -> Optional[CompoundTag]:
        text = _get_text(self.extra_editor).strip()
        if not text:
            return None
        try:
            tag = parse_snbt(text)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"额外 NBT 解析失败：{_exc_text(exc)}") from exc
        if not isinstance(tag, CompoundTag):
            raise ValueError("额外 NBT 必须是一个 compound，例如 { ... }")
        return tag

    def _flush_player(self) -> str:
        before = len(self.app.world.backups)
        self.app.world.mark_player_dirty(self.player)
        self.app.world.flush()
        backups = list(self.app.world.backups[before:])
        self.app.show_backups(backups)
        # 玩家页持有另一份 PlayerData；刷新它，避免之后用旧数据覆盖 Curios 修改。
        try:
            self.app.player_tab.refresh_players()
            if self.app.player_tab.current_uuid:
                self.app.player_tab.select_player(self.app.player_tab.current_uuid)
        except Exception:  # noqa: BLE001 - 刷新失败不影响写入
            pass
        return f"已备份 {len(backups)} 个文件" if backups else "已写入"

    def _reload_player_from_disk(self) -> None:
        """保存/删除前重新读盘，避免用打开页面时的旧玩家数据覆盖其它标签页的修改。"""
        if self.player is None or self.app.world is None:
            raise RuntimeError("没有可用的玩家数据")
        self.player = self.app.world.get_player(self.player.uuid)
        self.groups = list_groups(self.player)

    def _save(self) -> None:
        if self._current is None:
            messagebox.showinfo("提示", "请先选择一个 Curios 槽位。", parent=self.winfo_toplevel())
            return
        identifier, slot = self._current
        try:
            # 先刷新，避免覆盖玩家页/物品栏页刚才的修改
            self._reload_player_from_disk()
            if not self.id_var.get().strip():
                raise ValueError("请填写物品 id（例如 minecraft:diamond）")
            try:
                count = int(self.count_var.get().strip())
            except ValueError:
                raise ValueError("物品数量必须是整数") from None
            extra = self._parse_extra()
            item_format = self.app.world.version_profile.item_format
            existing = get_curio_item(self.player, identifier, slot)
            if existing is not None:
                item = update_curio_item(
                    self.player, identifier, slot,
                    self.id_var.get(), count, extra, item_format,
                )
            else:
                item = make_curio_item(
                    self.id_var.get(), count, extra, item_format, slot=slot,
                )
                set_curio_item(self.player, identifier, slot, item, item_format)
            backup_status = self._flush_player()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存 Curios 物品失败", _exc_text(exc),
                                 parent=self.winfo_toplevel())
            return
        self._load_player(self.player.uuid)
        self._reload_table(select=(identifier, slot))
        self.status_var.set(
            f"已保存 {identifier} 槽位 {slot + 1}：{item_id(item)} x"
            f"{item_count(item, item_format)}；{backup_status}"
        )
        self.app.set_status(f"Curios：已保存 {identifier} 槽位 {slot + 1} 的物品")

    def _delete(self) -> None:
        if self._current is None:
            return
        identifier, slot = self._current
        try:
            self._reload_player_from_disk()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("重新读取 Curios 数据失败", _exc_text(exc),
                                 parent=self.winfo_toplevel())
            return
        if get_curio_item(self.player, identifier, slot) is None:
            self.status_var.set("该槽位为空，无需删除")
            return
        if not messagebox.askyesno(
            "删除 Curios 物品",
            f"确定删除 {slot_display_name(identifier)}（{identifier}）"
            f"槽位 {slot + 1} 的物品？",
            parent=self.winfo_toplevel(),
        ):
            return
        try:
            delete_curio_item(self.player, identifier, slot)
            backup_status = self._flush_player()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("删除 Curios 物品失败", _exc_text(exc),
                                 parent=self.winfo_toplevel())
            return
        self._load_player(self.player.uuid)
        self._reload_table(select=(identifier, slot))
        self.status_var.set(
            f"已删除 {identifier} 槽位 {slot + 1} 的物品；{backup_status}"
        )


# --------------------------------------------------------------------------- #
# Tab 4: 原始 NBT
# --------------------------------------------------------------------------- #
class NbtTab(_Tab):
    """任意 NBT 文件的路径读取/写入（高级）。"""

    title = "原始 NBT"

    def _build(self) -> None:
        self.file_presets: list[str] = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        file_frame = ttk.Frame(self)
        file_frame.grid(row=0, column=0, sticky="ew")
        file_frame.columnconfigure(1, weight=1)
        ttk.Label(file_frame, text="文件（相对世界目录）：").grid(row=0, column=0, sticky="w")
        self.file_var = tk.StringVar()
        self.file_entry = ttk.Entry(file_frame, textvariable=self.file_var)
        self.file_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.browse_button = ttk.Button(file_frame, text="浏览...", command=self._ui_browse)
        self.browse_button.grid(row=0, column=2)

        preset_frame = ttk.Frame(self)
        preset_frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        preset_frame.columnconfigure(1, weight=1)
        ttk.Label(preset_frame, text="常用文件：").grid(row=0, column=0, sticky="w")
        self.preset_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(
            preset_frame, textvariable=self.preset_var, state="readonly",
        )
        self.preset_combo.grid(row=0, column=1, sticky="ew")
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        self.refresh_button = ttk.Button(
            preset_frame, text="刷新列表", command=lambda: self._run_ui(self.refresh),
        )
        self.refresh_button.grid(row=0, column=2, padx=(6, 0))

        path_frame = ttk.Frame(self)
        path_frame.grid(row=2, column=0, sticky="ew", pady=(6, 6))
        path_frame.columnconfigure(1, weight=1)
        ttk.Label(path_frame, text="NBT 路径：").grid(row=0, column=0, sticky="w")
        self.path_var = self.nbt_path_var = tk.StringVar()
        self.path_entry = self.nbt_path_entry = ttk.Entry(path_frame, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.read_button = ttk.Button(path_frame, text="读取", command=self._ui_read)
        self.read_button.grid(row=0, column=2)
        ttk.Label(
            path_frame, text="（留空显示整个根 compound；支持 a.b[0].c 形式）", style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))

        self.output = ScrolledText(
            self, height=14, wrap="word", undo=False, font=self.app.mono_font,
        )
        self.output.grid(row=3, column=0, sticky="nsew")
        _set_text(self.output, "（选择文件并点击“读取”。）")

        write_box = ttk.LabelFrame(self, text="写入", padding=8)
        write_box.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        write_box.columnconfigure(3, weight=1)
        ttk.Label(write_box, text="类型：").grid(row=0, column=0, sticky="w")
        self.value_type_var = tk.StringVar(value="auto")
        self.value_type_combo = ttk.Combobox(
            write_box, textvariable=self.value_type_var, values=NBT_VALUE_TYPES,
            state="readonly", width=12,
        )
        self.value_type_combo.grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Label(write_box, text="值（SNBT）：").grid(row=0, column=2, sticky="w")
        self.value_var = self.value_text_var = tk.StringVar()
        self.value_entry = ttk.Entry(write_box, textvariable=self.value_var)
        self.value_entry.grid(row=0, column=3, sticky="ew")
        self.create_var = tk.BooleanVar(value=False)
        self.create_check = ttk.Checkbutton(
            write_box, text="自动创建中间节点", variable=self.create_var,
        )
        self.create_check.grid(row=0, column=4, padx=(8, 0))
        self.write_button = ttk.Button(write_box, text="写入", command=self._ui_write)
        self.write_button.grid(row=0, column=5, padx=(8, 0))
        ttk.Label(
            write_box,
            text="写入 level.dat 后会自动重新加载世界信息；写入 playerdata 后会自动刷新玩家列表。",
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

    # ---- 内部 ------------------------------------------------------------ #
    def _ui_browse(self) -> None:
        try:
            world = self.app.world
            initial = world.path if world is not None else os.path.expanduser("~")
            selected = filedialog.askopenfilename(
                parent=self,
                title="选择 NBT 文件（.dat）",
                initialdir=initial,
                filetypes=[("NBT 数据文件", "*.dat"), ("所有文件", "*.*")],
            )
            if not selected:
                return
            path = selected
            if world is not None:
                try:
                    path = os.path.relpath(selected, world.path)
                except ValueError:
                    path = selected
            self.file_var.set(path)
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("选择文件失败", exc)

    def _on_preset_selected(self, _event: "tk.Event | None" = None) -> None:
        preset = self.preset_var.get().strip()
        if preset:
            self.file_var.set(preset)

    def _ui_read(self) -> None:
        self._run_ui(self.read)

    def _ui_write(self) -> None:
        self._run_ui(
            lambda: self.write(
                self.value_var.get(),
                self.value_type_var.get(),
                bool(self.create_var.get()),
            )
        )

    def _file_target(self, file: Optional[str]) -> str:
        target = str(file if file is not None else self.file_var.get()).strip()
        if not target:
            raise ValueError("请填写要操作的 NBT 文件（相对世界目录，例如 level.dat）")
        return target

    # ---- 公开方法 -------------------------------------------------------- #
    def refresh(self) -> None:
        world = self.app.world
        presets: list[str] = []
        if world is not None:
            if os.path.isfile(os.path.join(world.path, "level.dat")):
                presets.append("level.dat")
            for sub in ("playerdata", "data"):
                pattern = os.path.join(world.path, sub, "*.dat")
                for path in sorted(glob.glob(pattern)):
                    presets.append(os.path.relpath(path, world.path))
        self.file_presets = presets
        self.preset_combo["values"] = presets
        if presets:
            if not self.file_var.get().strip():
                self.file_var.set(presets[0])
                self.preset_var.set(presets[0])
        else:
            self.preset_var.set("")
            if world is None:
                self.file_var.set("")
                _set_text(self.output, "（未打开世界）")

    def read(
        self,
        file: Optional[str] = None,
        path: Optional[str] = None,
    ) -> str:
        """读取指定文件/路径并返回 SNBT 文本（同时显示在输出区）。"""
        world = self.require_world()
        target = self._file_target(file)
        nbt_path = str(path if path is not None else self.path_var.get()).strip()
        _name, tag = world.read_dat(target)
        if nbt_path:
            try:
                value = nbt_get_path(tag, nbt_path)
            except (KeyError, IndexError, NbtError) as exc:
                raise NbtError(f"读取路径 {nbt_path!r} 失败：{_exc_text(exc)}") from exc
        else:
            value = tag
        text = to_snbt(value)
        _set_text(self.output, text)
        suffix = f" 的 {nbt_path}" if nbt_path else "（根节点）"
        self.app.set_status(f"已读取 {target}{suffix}")
        return text

    def write(
        self,
        value_text: str,
        value_type: str = "auto",
        create: bool = False,
        file: Optional[str] = None,
        path: Optional[str] = None,
    ) -> None:
        """把 SNBT 值写入指定文件的 NBT 路径并立即落盘。"""
        world = self.require_world()
        target = self._file_target(file)
        nbt_path = str(path if path is not None else self.path_var.get()).strip()
        if not nbt_path:
            raise NbtError("写入时必须指定 NBT 路径（不能替换根节点）")
        raw_value = str(value_text or "").strip()
        if not raw_value:
            raise ValueError("写入值不能为空（请填写 SNBT，例如 1000 / \"text\" / {a:1}）")
        value_type = (str(value_type or "auto").strip().lower() or "auto")
        if value_type not in NBT_VALUE_TYPES:
            raise ValueError(
                f"未知值类型：{value_type!r}（可选 {' / '.join(NBT_VALUE_TYPES)}）"
            )

        _name, tag = world.read_dat(target)
        if value_type == "auto":
            try:
                existing = nbt_get_path(tag, nbt_path)
            except (KeyError, IndexError, NbtError):
                existing = None
            try:
                parsed = parse_snbt(raw_value)
            except Exception as exc:
                raise ValueError(f"SNBT 解析失败：{_exc_text(exc)}") from exc
            value = coerce_typed(parsed, "auto", existing)
        else:
            value = coerce_typed(raw_value, value_type, None)

        try:
            nbt_set_path(tag, nbt_path, value, create=bool(create))
        except (KeyError, IndexError, NbtError) as exc:
            raise NbtError(
                f"写入路径 {nbt_path!r} 失败：{_exc_text(exc)}\n"
                "提示：可勾选“自动创建中间节点”。"
            ) from exc

        before = len(world.backups)
        world.write_dat(target, tag)
        new_backups = list(world.backups[before:])

        resolved = world.resolve_world_file(target)
        if os.path.normcase(resolved) == os.path.normcase(os.path.abspath(world.level_path)):
            self.app.reload_level()
        else:
            player_dir = os.path.normcase(os.path.abspath(world.player_dir)) + os.sep
            if os.path.normcase(resolved).startswith(player_dir):
                try:
                    self.app.player_tab.refresh_players()
                except Exception:  # noqa: BLE001 - 刷新失败不影响写入结果
                    pass

        written = to_snbt(value)
        _set_text(self.output, written)
        suffix = f" 的 {nbt_path}" if nbt_path else ""
        self.app.show_backups(new_backups)
        self.app.set_status(f"已写入 {target}{suffix} = {written}")


# --------------------------------------------------------------------------- #
# Tab 5: 校验 / 备份
# --------------------------------------------------------------------------- #
class VerifyTab(_Tab):
    """世界校验（浅层/深度）与整世界备份（后台线程）。"""

    title = "校验 / 备份"

    def _build(self) -> None:
        self.is_running = False
        self.last_report: Optional[dict] = None
        self._busy_kind = ""

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        button_bar = ttk.Frame(self)
        button_bar.grid(row=0, column=0, sticky="ew")
        self.shallow_button = ttk.Button(
            button_bar, text="浅层校验", command=lambda: self._ui_start_verify(False),
        )
        self.shallow_button.pack(side="left")
        self.deep_button = ttk.Button(
            button_bar, text="深度校验", command=lambda: self._ui_start_verify(True),
        )
        self.deep_button.pack(side="left", padx=(8, 0))
        self.backup_button = ttk.Button(
            button_bar, text="整世界备份", command=lambda: self._ui_start_backup(),
        )
        self.backup_button.pack(side="left", padx=(8, 0))
        self.open_backup_button = ttk.Button(
            button_bar, text="打开备份目录", command=self._ui_open_backup_dir,
        )
        self.open_backup_button.pack(side="left", padx=(8, 0))

        status_bar = ttk.Frame(self)
        status_bar.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        status_bar.columnconfigure(1, weight=1)
        self.progress = ttk.Progressbar(
            status_bar, mode="determinate", length=220, maximum=100, value=0,
        )
        self.progress.grid(row=0, column=0, sticky="w")
        self.task_var = tk.StringVar(value="空闲")
        self.task_label = ttk.Label(status_bar, textvariable=self.task_var, anchor="w")
        self.task_label.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.output = ScrolledText(
            self, height=18, wrap="word", undo=False, font=self.app.mono_font,
        )
        self.output.grid(row=2, column=0, sticky="nsew")
        _set_text(self.output, "（点击“浅层校验”或“深度校验”开始，报告将显示在这里。）")

    # ---- 内部 ------------------------------------------------------------ #
    def _set_output(self, text: str) -> None:
        _set_text(self.output, text)

    def _append_output(self, text: str) -> None:
        self.output.configure(state="normal")
        self.output.insert("end", text)
        self.output.see("end")
        self.output.configure(state="disabled")

    def _set_busy(self, busy: bool, task: str = "") -> None:
        self.is_running = busy
        self._busy_kind = task if busy else ""
        if busy:
            try:
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
            except tk.TclError:
                pass
            self.task_var.set(task or "处理中...")
        else:
            try:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0)
            except tk.TclError:
                pass
            self.task_var.set("空闲")
        state = "disabled" if busy else "normal"
        for button in (
            self.shallow_button, self.deep_button, self.backup_button,
            self.open_backup_button,
        ):
            button.configure(state=state)

    def _ui_start_verify(self, deep: bool) -> None:
        self._run_ui(lambda: self.start_verify(deep))

    def _ui_start_backup(self) -> None:
        self._run_ui(self.start_backup)

    def _ui_open_backup_dir(self) -> None:
        try:
            world = self.require_world()
            target = world.backup_root if os.path.isdir(world.backup_root) else world.path
            if os.name == "nt":
                os.startfile(target)  # type: ignore[attr-defined]
            else:  # pragma: no cover - 非 Windows 兜底
                import subprocess

                subprocess.Popen(["xdg-open", target])
            self.app.set_status(f"已打开目录：{target}")
        except Exception as exc:  # noqa: BLE001
            self.app.show_error("打开备份目录失败", exc)

    def _finish_verify(self, payload: Any, deep: bool) -> None:
        self._set_busy(False)
        if isinstance(payload, BaseException):
            self._set_output(f"校验失败：{_exc_text(payload)}")
            self.app.set_status("校验失败")
            return
        self.last_report = payload
        self._set_output(self.format_report(payload))
        result = "通过" if isinstance(payload, dict) and payload.get("ok") else "失败"
        self.app.set_status(f"{'深度' if deep else '浅层'}校验完成：{result}")

    def _finish_backup(self, payload: Any) -> None:
        self._set_busy(False)
        if isinstance(payload, BaseException):
            self._set_output(f"备份失败：{_exc_text(payload)}")
            self.app.set_status("整世界备份失败")
            return
        backup_path = str(payload)
        self._set_output(f"整世界备份完成。\n备份目录：{backup_path}")
        self.app.show_backups([backup_path])
        self.app.set_status(f"整世界备份完成：{backup_path}")

    # ---- 公开方法 -------------------------------------------------------- #
    def refresh(self) -> None:
        if self.is_running:
            return
        if self.app.world is None:
            self.task_var.set("未打开世界")
        else:
            self.task_var.set("空闲")

    def start_verify(self, deep: bool = False) -> None:
        """在后台线程执行世界校验；完成后自动回到主线程更新界面。"""
        world = self.require_world()
        if self.is_running:
            raise RuntimeError("已有后台任务正在运行，请等待其完成。")
        self._set_busy(True, "深度校验中..." if deep else "浅层校验中...")
        self._set_output(
            f"正在执行{'深度' if deep else '浅层'}校验，请稍候...\n"
            f"世界目录：{world.path}\n"
        )
        self.app.set_status(f"正在执行{'深度' if deep else '浅层'}校验（后台）...")
        self.app.run_background(
            lambda: world.verify(deep=deep),
            lambda payload: self._finish_verify(payload, deep),
            description="verify-deep" if deep else "verify-shallow",
        )

    def start_backup(self) -> None:
        """在后台线程执行整世界备份；完成后刷新界面。"""
        world = self.require_world()
        if self.is_running:
            raise RuntimeError("已有后台任务正在运行，请等待其完成。")
        self._set_busy(True, "整世界备份中...")
        self._set_output(f"正在备份整个世界，请稍候...\n世界目录：{world.path}\n")
        self.app.set_status("正在执行整世界备份（后台）...")
        self.app.run_background(
            world.backup_world, self._finish_backup, description="backup-world",
        )

    def format_report(self, report: dict) -> str:
        """把 ``World.verify()`` 返回的报告格式化成中文文本。"""
        if not isinstance(report, dict):
            return f"校验报告格式错误：{report!r}"
        ok = bool(report.get("ok"))
        level = report.get("level") or {}
        lines: list[str] = []
        lines.append("=" * 68)
        lines.append(f"校验结果：{'通过' if ok else '失败'}")
        lines.append("=" * 68)
        lines.append(
            "level.dat："
            f"DataVersion={level.get('data_version', '?')}，"
            f"世界名称={level.get('level_name', '?')!r}，"
            f"已检查 section 数={level.get('sections_checked', 0)}"
        )
        dimensions = report.get("dimensions") or {}
        if dimensions:
            lines.append("")
            lines.append("维度：")
            for dim, stats in dimensions.items():
                stats = stats if isinstance(stats, dict) else {}
                lines.append(
                    f"  [{dim}] region 文件={stats.get('regions', 0)}，"
                    f"区块={stats.get('chunks', 0)}，错误={stats.get('errors', 0)}"
                )
        players = report.get("players") or {}
        lines.append("")
        lines.append(f"玩家数据：{len(players)} 个")
        for uuid, info in players.items():
            if isinstance(info, dict) and "error" in info:
                lines.append(f"  - {uuid}：读取失败 {info['error']}")
            elif isinstance(info, dict):
                lines.append(
                    f"  - {uuid}：维度={info.get('dimension', '?')}，"
                    f"模式={info.get('game_type', '?')}，DataVersion={info.get('data_version', '?')}"
                )

        warnings = report.get("warnings") or []
        errors = report.get("errors") or []
        if warnings:
            lines.append("")
            lines.append(f"警告（{len(warnings)} 条）：")
            for warning in warnings[:200]:
                lines.append(f"  ! {warning}")
            if len(warnings) > 200:
                lines.append(f"  ... 另有 {len(warnings) - 200} 条警告")
        if errors:
            lines.append("")
            lines.append(f"错误（{len(errors)} 条）：")
            for error in errors[:200]:
                lines.append(f"  x {error}")
            if len(errors) > 200:
                lines.append(f"  ... 另有 {len(errors) - 200} 条错误")
        if not warnings and not errors:
            lines.append("")
            lines.append("未发现警告或错误。")
        lines.append("")
        lines.append(f"（备份目录：{getattr(self.app.world, 'backup_root', '未打开世界')}）")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 版本选择对话框
# --------------------------------------------------------------------------- #
class VersionChoiceDialog(tk.Toplevel):
    """检测到不受支持的游戏版本时，让用户从受支持版本中选择处理逻辑。

    默认选中 :attr:`UnsupportedVersionError.nearest`（按版本号最近的受支持版本）。
    模态对话框；结果放在 :attr:`result`（受支持版本的 id，取消为 None）。
    """

    def __init__(self, parent: tk.Misc, error: UnsupportedVersionError):
        super().__init__(parent)
        self.result: Optional[str] = None
        self._error = error
        self._labels: dict[str, str] = {}

        self.title("选择 Minecraft 版本处理方式")
        self.transient(parent)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text=f"检测到不受支持的 Minecraft 版本：{error.detected.display}",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        supported_text = "、".join(
            f"{p.id}（{p.status_label}，DataVersion={p.data_version}）"
            for p in error.supported
        )
        ttk.Label(
            body,
            text=f"本程序当前支持的版本：{supported_text}",
            wraplength=560,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            body,
            text=f"请选择用于读写该存档的处理逻辑（默认：版本号最近的受支持版本 {error.nearest.id}）",
            wraplength=560,
        ).pack(anchor="w", pady=(6, 0))

        values: list[str] = []
        for profile in error.supported:
            label = profile.choice_label
            self._labels[label] = profile.id
            values.append(label)
        self.choice_var = tk.StringVar(value=error.nearest.choice_label)
        self.combo = ttk.Combobox(
            body, textvariable=self.choice_var, values=values, state="readonly", width=58,
        )
        self.combo.pack(fill="x", pady=(10, 0))

        ttk.Label(
            body,
            text="注意：处理版本与存档实际版本不一致时，写入可能有风险；"
                 "工具会在覆盖前自动备份。",
            style="Hint.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(buttons, text="使用所选版本打开", command=self._ok).pack(side="right")
        ttk.Button(buttons, text="取消", command=self._cancel).pack(side="right", padx=(0, 8))

        self.grab_set()
        self.update_idletasks()
        try:
            width = max(self.winfo_width(), 620)
            height = max(self.winfo_height(), 250)
            x = parent.winfo_rootx() + max(20, (parent.winfo_width() - width) // 2)
            y = parent.winfo_rooty() + max(20, (parent.winfo_height() - height) // 3)
            self.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            pass
        self.focus_force()

    def selected_version_id(self) -> Optional[str]:
        return self._labels.get(self.choice_var.get())

    def _ok(self) -> None:
        selected = self.selected_version_id()
        if selected is None:
            self._cancel()
            return
        self.result = selected
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        try:
            self.destroy()
        except tk.TclError:
            pass


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class McToolGUI(tk.Tk):
    """mctool 图形界面主窗口。

    公开方法（``open_world`` / ``close_world`` / ``run_background`` 等）不会弹出
    模态对话框，失败时直接抛出异常，便于脚本和自动化测试调用。
    """

    def __init__(
        self,
        world_path: Optional[str] = None,
        initial_tab: int = 0,
        mc_version: Optional[str] = None,
    ):
        super().__init__()
        self._closing = False
        self.world: Optional[World] = None
        self.startup_error: Optional[str] = None
        self.startup_version_error: Optional[UnsupportedVersionError] = None
        self.mc_version = mc_version
        self.last_backups: list[str] = []
        self._bg_queue: "queue.Queue[tuple[str, Any, Callable[[Any], None]]]" = queue.Queue()
        self._bg_threads: list[threading.Thread] = []
        self._bg_pending = 0
        self._bg_after: Optional[str] = None

        self.title(APP_TITLE)
        self.geometry(WINDOW_SIZE)
        self.minsize(*MIN_WINDOW_SIZE)

        self._init_fonts()
        self._init_styles()
        self._build_ui()
        self._build_menu()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._select_initial_tab(initial_tab)
        if world_path:
            try:
                self.open_world(world_path, mc_version=self.mc_version)
            except UnsupportedVersionError as exc:
                # 版本不受支持: 主循环启动后再弹出“选择受支持版本”对话框
                self.startup_version_error = exc
                self.set_status(
                    f"检测到不受支持的版本：{exc.detected.display}，请选择受支持的处理版本"
                )
                self.lock_var.set("未锁定")
            except Exception as exc:  # noqa: BLE001 - 构造阶段不弹框，错误稍后展示
                self.startup_error = _exc_text(exc)
                self.set_status(f"打开世界失败：{self.startup_error}")
                self.lock_var.set("未锁定")

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def _init_fonts(self) -> None:
        try:
            family = "Microsoft YaHei UI"
            tkfont.nametofont("TkDefaultFont").configure(family=family, size=10)
            for name in ("TkTextFont", "TkMenuFont", "TkHeadingFont"):
                try:
                    tkfont.nametofont(name).configure(family=family)
                except tk.TclError:
                    pass
        except Exception:  # noqa: BLE001 - 字体美化失败不影响功能
            pass
        self.mono_font = ("Consolas", 10) if os.name == "nt" else ("TkFixedFont",)

    def _init_styles(self) -> None:
        style = ttk.Style(self)
        names = style.theme_names()
        for theme in ("vista", "winnative", "clam", "alt", "default"):
            if theme in names:
                try:
                    style.theme_use(theme)
                except tk.TclError:
                    pass
                break
        try:
            style.configure("Accent.TButton", font=("Microsoft YaHei UI", 10, "bold"))
            style.configure("Hint.TLabel", foreground="#666666")
            style.configure("Warn.TLabel", foreground="#B00020")
            style.configure("Treeview", rowheight=24)
        except tk.TclError:
            pass

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # ---- 顶部工具栏 -------------------------------------------------- #
        toolbar = ttk.Frame(self, padding=(10, 8, 10, 2))
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="世界目录：").grid(row=0, column=0, sticky="w")
        self.world_path_var = tk.StringVar()
        self.world_path_entry = ttk.Entry(toolbar, textvariable=self.world_path_var)
        self.world_path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.world_path_entry.bind("<Return>", lambda _e: self._ui_open_typed())
        self.open_world_button = ttk.Button(
            toolbar, text="打开世界...", command=self._ui_open_world,
        )
        self.open_world_button.grid(row=0, column=2)
        self.open_typed_button = ttk.Button(
            toolbar, text="打开路径", command=self._ui_open_typed,
        )
        self.open_typed_button.grid(row=0, column=3, padx=(6, 0))

        hint_bar = ttk.Frame(self, padding=(10, 0, 10, 2))
        hint_bar.grid(row=1, column=0, sticky="ew")
        hint_bar.columnconfigure(0, weight=1)
        ttk.Label(
            hint_bar,
            text="提示：打开世界后工具独占 session.lock；关闭窗口或“文件 → 关闭世界”时释放。"
                 "修改立即落盘并自动备份。",
            style="Hint.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.lock_var = tk.StringVar(value="未锁定")
        self.lock_label = ttk.Label(
            hint_bar, textvariable=self.lock_var, foreground="#b06a00", anchor="e",
        )
        self.lock_label.grid(row=0, column=1, sticky="e", padx=(12, 0))

        # ---- 中部标签页 -------------------------------------------------- #
        self.status_var = tk.StringVar(value="就绪")
        self.backup_var = tk.StringVar(value="备份：无")

        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 4))

        self.world_tab = WorldTab(self.notebook, self)
        self.block_tab = BlockTab(self.notebook, self)
        self.search_tab = BlockSearchTab(self.notebook, self)
        self.be_search_tab = BlockEntitySearchTab(self.notebook, self)
        self.player_tab = PlayerTab(self.notebook, self)
        self.moddata_tab = ModDataTab(self.notebook, self)
        self.nbt_tab = NbtTab(self.notebook, self)
        self.verify_tab = VerifyTab(self.notebook, self)
        self.tabs: list[_Tab] = [
            self.world_tab, self.block_tab, self.search_tab, self.be_search_tab,
            self.player_tab, self.moddata_tab, self.nbt_tab, self.verify_tab,
        ]
        tab_titles = [
            "世界信息", "方块 / 方块实体", "方块搜索", "方块实体搜索",
            "玩家数据", "模组数据", "原始 NBT", "校验 / 备份",
        ]
        for tab, title in zip(self.tabs, tab_titles):
            self.notebook.add(tab, text=title)
        self.tabs_by_name: dict[str, _Tab] = {
            "world": self.world_tab,
            "block": self.block_tab,
            "search": self.search_tab,
            "besearch": self.be_search_tab,
            "player": self.player_tab,
            "moddata": self.moddata_tab,
            "nbt": self.nbt_tab,
            "verify": self.verify_tab,
        }

        # ---- 底部状态栏 -------------------------------------------------- #
        status_frame = ttk.Frame(self, padding=(10, 0, 10, 6))
        status_frame.grid(row=3, column=0, sticky="ew")
        status_frame.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(
            status_frame, textvariable=self.status_var, anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.backup_label = ttk.Label(
            status_frame, textvariable=self.backup_var, anchor="e",
            foreground="#8a4b00",
        )
        self.backup_label.grid(row=0, column=1, sticky="e", padx=(12, 0))
        self.backup_label.bind("<Button-1>", self._show_backup_details)

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="打开世界...", command=self._ui_open_world)
        file_menu.add_command(label="打开路径", command=self._ui_open_typed)
        file_menu.add_command(label="关闭世界", command=self._ui_close_world)
        file_menu.add_separator()
        file_menu.add_command(label="退出", command=self.on_close)
        menubar.add_cascade(label="文件", menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="关于", command=self._ui_about)
        menubar.add_cascade(label="帮助", menu=help_menu)
        self.config(menu=menubar)

    def _select_initial_tab(self, initial_tab: int) -> None:
        try:
            index = int(initial_tab)
        except (TypeError, ValueError):
            index = 0
        index = max(0, min(index, len(self.tabs) - 1))
        try:
            self.notebook.select(index)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # UI 回调（允许弹框）
    # ------------------------------------------------------------------ #
    def _ui_open_world(self) -> None:
        initial = self.world_path_var.get().strip()
        if not initial or not os.path.isdir(initial):
            if self.world is not None:
                initial = self.world.path
            else:
                initial = find_saves_dir() or os.path.expanduser("~")
        try:
            selected = filedialog.askdirectory(
                parent=self,
                title="选择 Minecraft 世界目录（包含 level.dat）",
                initialdir=initial,
                mustexist=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.show_error("选择世界目录失败", exc)
            return
        if selected:
            self._ui_open_path(selected)

    def _ui_open_typed(self) -> None:
        path = self.world_path_var.get().strip().strip('"').strip("'")
        if not path:
            self.show_error("打开世界失败", ValueError("请先填写世界目录路径。"))
            return
        self._ui_open_path(path)

    def _ui_open_path(self, path: str) -> None:
        try:
            self.open_world(path)
        except UnsupportedVersionError as exc:
            choice = self._ask_version(exc)
            if not choice:
                self.set_status(
                    f"已取消打开：检测到不受支持的版本 {exc.detected.display}"
                )
                self.lock_var.set("未锁定")
                return
            try:
                self.open_world(path, mc_version=choice)
            except Exception as exc2:  # noqa: BLE001 - UI 边界
                self.show_error("打开世界失败", exc2)
                return
        except Exception as exc:  # noqa: BLE001 - UI 边界
            self.show_error("打开世界失败", exc)
            return
        world = self.world
        if world is None:
            self.set_status("打开世界失败：未知错误")
            return
        self.set_status(
            f"已打开世界：{world.path}（检测版本 {world.detected_version.display}，"
            f"处理版本 {world.version_profile.id}）"
        )
        if world.version_warnings:
            try:
                messagebox.showwarning(
                    "版本不一致警告", "\n".join(world.version_warnings), parent=self,
                )
            except tk.TclError:
                pass

    def _ask_version(self, error: UnsupportedVersionError) -> Optional[str]:
        """弹出受支持版本选择对话框，返回所选版本 id（取消返回 None）。"""
        dialog = VersionChoiceDialog(self, error)
        self.wait_window(dialog)
        return dialog.result

    def _ui_close_world(self) -> None:
        try:
            self.close_world()
        except Exception as exc:  # noqa: BLE001
            self.show_error("关闭世界失败", exc)
            return
        self.set_status("已关闭世界，session.lock 已释放")

    def _ui_about(self) -> None:
        supported = "、".join(
            f"{p.id}（{p.status_label}）" for p in SUPPORTED_VERSIONS.values()
        )
        messagebox.showinfo(
            "关于 mctool",
            f"{APP_TITLE}\n"
            f"支持的 Minecraft 版本：{supported}\n"
            f"主目标版本：1.21.1（DataVersion=3955）\n\n"
            "打开世界时会自动识别存档版本：受支持则自动选择对应处理逻辑；\n"
            "不受支持时会让你从受支持版本中选择（默认版本号最近的版本）。\n\n"
            "纯 Python 标准库 + tkinter/ttk 实现，不联网。\n"
            "通过 session.lock 独占锁保证不会与游戏同时写存档；\n"
            "每次写入前自动备份，备份路径显示在状态栏。\n\n"
            "功能：世界信息 / 方块与方块实体 / 方块搜索 / 方块实体搜索 / 玩家数据 / "
            "模组数据（Curios）/ 原始 NBT / 校验与备份。\n\n"
            "模组数据子标签页会标注适用的模组/游戏/加载器版本；版本偏差可能导致未定义行为。",
            parent=self,
        )

    def _show_backup_details(self, _event: "tk.Event | None" = None) -> None:
        if not self.last_backups:
            messagebox.showinfo("备份", "本次会话还没有产生备份。", parent=self)
            return
        messagebox.showinfo(
            "备份路径",
            "最近一次写入产生的备份：\n\n" + "\n".join(self.last_backups),
            parent=self,
        )

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #
    def open_world(self, path: str, mc_version: Optional[str] = None) -> dict:
        """打开世界（获取 session.lock 独占锁）并刷新所有标签页。

        会自动识别存档版本：受支持时直接使用对应处理逻辑；不受支持时抛出
        :class:`~mctool.versions.UnsupportedVersionError`（UI 层会弹版本选择框）。
        也可通过 ``mc_version`` 显式指定受支持版本。
        失败时抛出 ``WorldInUseError`` / ``WorldError`` 等异常，不弹对话框；
        若之前已打开另一个世界，失败时保留原世界不变。
        """
        if path is None:
            raise ValueError("世界路径不能为空")
        text = str(path).strip().strip('"').strip("'")
        if not text:
            raise ValueError("世界路径不能为空")

        previous = self.world
        same_world = False
        if previous is not None:
            try:
                same_world = (
                    os.path.normcase(os.path.abspath(text))
                    == os.path.normcase(previous.path)
                )
            except OSError:
                same_world = False
        if same_world:
            # 同一个世界无法同时持有两把锁: 先释放再重新打开
            self.close_world()
            previous = None

        world = World(text, minecraft_version=mc_version)  # 失败会抛 WorldInUseError / WorldError / 版本异常
        if previous is not None:
            try:
                previous.close()
            except Exception:  # noqa: BLE001 - 旧世界释放失败不应阻断新世界
                pass
        self.world = world
        self.world_path_var.set(world.path)
        self.lock_var.set("已锁定（session.lock）")
        self.last_backups = []
        self.backup_var.set("备份：无")
        self._refresh_all_tabs()
        self.set_status(f"已打开世界：{world.path}")
        return world.info()

    def close_world(self) -> None:
        """关闭当前世界并释放 session.lock。"""
        world = self.world
        self.world = None
        try:
            if world is not None:
                world.close()
        finally:
            self.lock_var.set("未锁定")
            self.last_backups = []
            self.backup_var.set("备份：无")
            self._refresh_all_tabs()

    def reload_level(self) -> None:
        """重新从磁盘读取 level.dat（用于原始 NBT 修改后刷新世界页）。"""
        world = self.world
        if world is None:
            raise RuntimeError("尚未打开世界，无法重新读取 level.dat。")
        try:
            world.reload_level()
        except Exception as exc:
            raise WorldError(f"重新读取 level.dat 失败：{_exc_text(exc)}") from exc
        self.world_tab.refresh()
        self.set_status("已重新读取 level.dat")

    def run_background(
        self,
        fn: Callable[[], Any],
        on_done: Callable[[Any], None],
        description: str = "",
    ) -> None:
        """在后台守护线程执行 ``fn``，完成后通过 ``after()`` 在主线程调用 ``on_done``。

        回调只接收一个参数：成功时为返回值，失败时为异常对象。
        """
        if self._closing:
            return
        if not callable(fn) or not callable(on_done):
            raise TypeError("run_background 需要可调用的 fn 与 on_done")

        self._bg_pending += 1

        def worker() -> None:
            try:
                result = fn()
            except BaseException as exc:  # noqa: BLE001 - 传给主线程处理
                self._bg_queue.put(("error", exc, on_done))
            else:
                self._bg_queue.put(("ok", result, on_done))

        thread = threading.Thread(
            target=worker,
            name=f"mctool-bg-{description or 'task'}",
            daemon=True,
        )
        self._bg_threads.append(thread)
        thread.start()
        if self._bg_after is None:
            try:
                self._bg_after = self.after(50, self._poll_background)
            except tk.TclError:
                self._bg_after = None

    def set_status(self, text: Any) -> None:
        """更新底部状态栏文本。"""
        self.status_var.set(str(text))

    def show_backups(self, paths: list[str]) -> None:
        """在状态栏显示备份路径（不弹对话框）。"""
        items = [str(p) for p in (paths or []) if p]
        self.last_backups = items
        if not items:
            self.backup_var.set("备份：无")
            return
        latest = items[-1]
        if len(latest) > 80:
            latest = "…" + latest[-79:]
        if len(items) == 1:
            self.backup_var.set(f"备份：{latest}")
        else:
            self.backup_var.set(f"备份：{latest}（共 {len(items)} 个文件）")

    def select_tab(self, index: int) -> None:
        """切换标签页（0..7）。"""
        index = max(0, min(int(index), len(self.tabs) - 1))
        self.notebook.select(index)

    def get_tab(self, key: Any) -> _Tab:
        """按索引或名称获取标签页（world/block/search/player/moddata/nbt/verify）。"""
        if isinstance(key, int):
            return self.tabs[key]
        name = str(key).strip().lower()
        if name in self.tabs_by_name:
            return self.tabs_by_name[name]
        for tab in self.tabs:
            if name in (tab.title.lower(), type(tab).__name__.lower()):
                return tab
        raise KeyError(f"未知标签页：{key!r}")

    def show_error(self, title: str, exc: BaseException) -> None:
        """显示中文错误对话框（仅在 UI 回调边界使用）。"""
        if self._closing:
            return
        try:
            messagebox.showerror(title, _exc_text(exc), parent=self)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def _refresh_all_tabs(self) -> None:
        for tab in getattr(self, "tabs", []):
            try:
                tab.refresh()
            except Exception as exc:  # noqa: BLE001 - 单个标签页失败不影响其它
                self.set_status(f"刷新“{tab.title}”失败：{_exc_text(exc)}")

    def _poll_background(self) -> None:
        self._bg_after = None
        if self._closing:
            return
        while True:
            try:
                kind, payload, callback = self._bg_queue.get_nowait()
            except queue.Empty:
                break
            self._bg_pending = max(0, self._bg_pending - 1)
            try:
                callback(payload)
            except Exception:  # noqa: BLE001 - 回调异常不应影响 Tk 主循环
                traceback.print_exc()
        self._bg_threads = [t for t in self._bg_threads if t.is_alive()]
        if self._bg_pending > 0 or not self._bg_queue.empty():
            try:
                self._bg_after = self.after(50, self._poll_background)
            except tk.TclError:
                self._bg_after = None

    def on_close(self) -> None:
        """窗口关闭：释放 session.lock 并销毁窗口。"""
        if self._closing:
            return
        self._closing = True
        if self._bg_after is not None:
            try:
                self.after_cancel(self._bg_after)
            except Exception:  # noqa: BLE001
                pass
            self._bg_after = None
        try:
            self.close_world()
        except Exception:  # noqa: BLE001 - 关闭阶段尽量不抛
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass

    def destroy(self) -> None:  # noqa: D401 - 覆盖 Tk.destroy
        """销毁窗口，并解除所有 Tk 变量的 Tcl 绑定。

        tkinter 的 ``Variable.__del__`` 会调用 Tcl 清理变量；如果这些变量在
        销毁之后才被垃圾回收，而回收恰好发生在后台线程里，就会从非主线程调用
        Tcl（RuntimeError 或死锁）。这里在销毁时主动置空 ``_tk``，避免该问题。
        """
        try:
            super().destroy()
        finally:
            self._neutralize_tk_variables()

    def _neutralize_tk_variables(self) -> None:
        """把本窗口创建的 tk.Variable 与 Tcl 解绑（幂等、异常安全）。"""
        seen: set[int] = set()
        stack: list[Any] = list(vars(self).values())
        while stack:
            obj = stack.pop()
            if obj is None or isinstance(obj, (str, bytes, int, float, bool)):
                continue
            obj_id = id(obj)
            if obj_id in seen:
                continue
            seen.add(obj_id)
            if isinstance(obj, tk.Variable):
                try:
                    obj._tk = None  # type: ignore[attr-defined]
                    obj._tclCommands = None  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
                continue
            if isinstance(obj, dict):
                stack.extend(obj.values())
                continue
            if isinstance(obj, (list, tuple, set, frozenset)):
                stack.extend(obj)
                continue
            if type(obj).__module__ == __name__:
                try:
                    stack.extend(vars(obj).values())
                except TypeError:
                    pass


# --------------------------------------------------------------------------- #
# 命令行入口
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mctool.gui",
        description="mctool 图形界面（Tkinter）：Minecraft 存档读取/修改工具（自动识别版本；"
                    "支持 " + "、".join(SUPPORTED_VERSIONS.keys()) + "）。",
    )
    parser.add_argument(
        "--world", "-w", default=os.environ.get("MCTOOL_WORLD"),
        help="启动时自动打开的世界目录（包含 level.dat）；也可用环境变量 MCTOOL_WORLD",
    )
    parser.add_argument(
        "--tab", "-t", type=int, default=0,
        help="初始标签页索引：0=世界信息 1=方块 2=方块搜索 3=方块实体搜索 "
             "4=玩家 5=模组数据 6=原始 NBT 7=校验/备份",
    )
    parser.add_argument(
        "--mc-version", default=None, metavar="VERSION",
        help="存档版本不受支持时，指定使用哪个受支持版本的处理逻辑（如 1.21.1）；"
             "受支持的存档会自动识别，无需此参数。",
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help="启动后自动关闭窗口并打印检查结果（用于自动化验收）",
    )
    parser.add_argument(
        "--self-test-ms", type=int, default=1500, help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m mctool`` / ``python -m mctool.gui`` 入口（产品只提供 GUI）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        app = McToolGUI(
            world_path=args.world, initial_tab=args.tab, mc_version=args.mc_version,
        )
    except tk.TclError as exc:
        print(f"错误：无法初始化 Tk 图形界面（可能没有可用的桌面环境）：{exc}", file=sys.stderr)
        return 1

    startup_ok = app.world is not None
    tab_count = len(app.tabs)
    if args.world and app.world is None and app.startup_version_error is not None:
        # 启动时自动打开失败（版本不受支持）：主循环启动后弹出“选择受支持版本”对话框
        app.after(150, lambda: app._ui_open_path(args.world))
    elif args.world and app.world is None and app.startup_error:
        try:
            messagebox.showerror("打开世界失败", app.startup_error, parent=app)
        except Exception:  # noqa: BLE001
            pass

    if args.self_test:
        delay = max(200, int(args.self_test_ms))
        app.after(delay, app.on_close)

    try:
        app.mainloop()
    except KeyboardInterrupt:
        app.on_close()
    finally:
        try:
            if app.winfo_exists():
                app.on_close()
        except Exception:  # noqa: BLE001
            pass

    if args.self_test:
        print(
            f"GUI self-test: tabs={tab_count}, world_loaded={startup_ok}, "
            f"initial_tab={args.tab}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - 模块入口
    sys.exit(main())
