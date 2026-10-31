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
from .i18n import get_language, tr
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
    ARMOR_LABELS,
    INVENTORY_SLOTS,
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

APP_TITLE = tr("app.title", version=APP_VERSION)
WINDOW_SIZE = "1150x760"
MIN_WINDOW_SIZE = (980, 620)

GAME_TYPE_CHOICES = tuple(GAME_TYPES.values())  # survival/creative/adventure/spectator
DIFFICULTY_CHOICES = tuple(DIFFICULTIES.values())  # peaceful/easy/normal/hard
WEATHER_CHOICES = ("clear", "rain", "thunder")
WEATHER_LABELS = {
    "clear": tr("weather.clear"),
    "rain": tr("weather.rain"),
    "thunder": tr("weather.thunder"),
}
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
# 国际化辅助（仅处理显示文本，技术值保持原样）
# --------------------------------------------------------------------------- #
def _label(text: str) -> str:
    """给表单标签补上本地化冒号。"""
    return tr("common.label_format", label=text)


def _info_label_width() -> int:
    """只读信息栏标签宽度：中文 12，英文放宽以容纳更长的单词。"""
    return 12 if get_language() == "zh_cn" else 18


def _slot_label(slot: int) -> str:
    """本地化的物品栏槽位名（0..8 快捷栏、9..35 背包、盔甲、副手）。"""
    if 0 <= slot <= 8:
        return tr("items.slot.hotbar", index=slot + 1)
    if 9 <= slot <= 35:
        return tr("items.slot.inventory", index=slot + 1)
    if slot in ARMOR_LABELS:
        return tr(f"items.slot.armor.{slot}", default=slot_label(slot))
    if slot == 150:
        return tr("items.slot.offhand")
    return tr("items.slot.other", slot=slot)


def _describe_item(tag: CompoundTag, item_format: str) -> str:
    """本地化的物品简述：id x count (+ 是否有额外 NBT)。"""
    extra = item_extra(tag, item_format)
    suffix = ""
    if extra is not None and len(extra) > 0:
        suffix = tr("items.describe.extra_suffix")
    return tr(
        "items.describe.pattern",
        id=item_id(tag) or "?",
        count=item_count(tag, item_format),
        suffix=suffix,
    )


def _curio_slot_name(identifier: str) -> str:
    """本地化的 Curios 槽位组名；未知标识回退到核心层显示名。"""
    return tr(
        f"curios.group.{identifier}",
        default=slot_display_name(identifier),
    )


def _version_status_label(profile: Any) -> str:
    """本地化的版本状态标签（已验证 / 兼容）。"""
    return tr(
        f"version.status.{getattr(profile, 'status', '')}",
        default=getattr(profile, "status_label", ""),
    )


def _version_choice_label(profile: Any) -> str:
    """本地化的版本选择项文本（id + 状态 + DataVersion）。"""
    return tr(
        "version.choice_label",
        id=getattr(profile, "id", ""),
        status=_version_status_label(profile),
        data_version=getattr(profile, "data_version", "?"),
    )



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
        raise ValueError(tr("error.field_empty", field=field))
    try:
        value = int(raw, 10)
    except ValueError:
        raise ValueError(
            tr("error.field_not_int", field=field, value=repr(raw))
        ) from None
    if minimum is not None and value < minimum:
        raise ValueError(
            tr("error.field_min", field=field, minimum=minimum, value=value)
        )
    if maximum is not None and value > maximum:
        raise ValueError(
            tr("error.field_max", field=field, maximum=maximum, value=value)
        )
    return value


def _parse_float(
    text: Any,
    field: str,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
) -> float:
    raw = str(text).strip()
    if raw == "":
        raise ValueError(tr("error.field_empty", field=field))
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            tr("error.field_not_number", field=field, value=repr(raw))
        ) from None
    if not math.isfinite(value):
        raise ValueError(tr("error.field_not_finite", field=field))
    if minimum is not None and value < minimum:
        raise ValueError(
            tr("error.field_min", field=field, minimum=minimum, value=value)
        )
    if maximum is not None and value > maximum:
        raise ValueError(
            tr("error.field_max", field=field, maximum=maximum, value=value)
        )
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

    title_key = "tab.generic"

    @property
    def title(self) -> str:
        """标签页标题（已按当前语言翻译）。"""
        return tr(self.title_key)

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
            raise RuntimeError(tr("error.no_world_hint"))
        return world

    def _run_ui(self, action: Callable[[], Any], status: Optional[str] = None) -> Any:
        """执行按钮回调：失败时弹出错误框（公开方法本身不会弹框）。"""
        try:
            result = action()
        except Exception as exc:  # noqa: BLE001 - UI 边界统一处理
            self.app.show_error(tr("dialog.error.operation.title"), exc)
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

    title_key = "tab.world"

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
        info_box = ttk.LabelFrame(left, text=tr("world.info_box"), padding=8)
        info_box.grid(row=0, column=0, sticky="ew")
        info_box.columnconfigure(1, weight=1)
        info_rows = [
            ("level_name", tr("world.info.level_name")),
            ("data_version", "DataVersion"),
            ("version", tr("world.info.version")),
            ("detected_version", tr("world.info.detected_version")),
            ("selected_version", tr("world.info.selected_version")),
            ("version_mode", tr("world.info.version_mode")),
            ("seed", tr("world.info.seed")),
            ("game_type", tr("world.info.game_type")),
            ("difficulty", tr("world.info.difficulty")),
            ("hardcore", tr("world.info.hardcore")),
            ("allow_commands", tr("world.info.allow_commands")),
            ("spawn", tr("world.info.spawn")),
            ("time", tr("world.info.time")),
            ("weather", tr("world.info.weather")),
            ("dimensions", tr("world.info.dimensions")),
        ]
        for row, (key, label) in enumerate(info_rows):
            var = tk.StringVar(value="—")
            self._info_vars[key] = var
            ttk.Label(info_box, text=_label(label), width=_info_label_width(), anchor="w").grid(
                row=row, column=0, sticky="w", pady=1
            )
            ttk.Label(info_box, textvariable=var, anchor="w", wraplength=430).grid(
                row=row, column=1, sticky="w", pady=1
            )

        # ---- 可编辑设置 -------------------------------------------------- #
        settings = ttk.LabelFrame(left, text=tr("world.settings_box"), padding=8)
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

        ttk.Label(settings, text=_label(tr("world.label.level_name"))).grid(
            row=0, column=0, sticky="w", pady=3
        )
        self.level_name_entry = ttk.Entry(settings, textvariable=self.level_name_var)
        self.level_name_entry.grid(row=0, column=1, columnspan=3, sticky="ew", pady=3)

        ttk.Label(settings, text=_label(tr("world.label.game_type"))).grid(
            row=1, column=0, sticky="w", pady=3
        )
        self.gametype_combo = ttk.Combobox(
            settings, textvariable=self.gametype_var, values=GAME_TYPE_CHOICES,
            state="readonly", width=14,
        )
        self.gametype_combo.grid(row=1, column=1, sticky="w", pady=3)
        ttk.Label(settings, text=_label(tr("world.label.difficulty"))).grid(
            row=1, column=2, sticky="e", padx=(12, 6), pady=3
        )
        self.difficulty_combo = ttk.Combobox(
            settings, textvariable=self.difficulty_var, values=DIFFICULTY_CHOICES,
            state="readonly", width=14,
        )
        self.difficulty_combo.grid(row=1, column=3, sticky="w", pady=3)

        ttk.Label(settings, text=_label(tr("world.label.time"))).grid(
            row=2, column=0, sticky="w", pady=3
        )
        self.time_entry = ttk.Entry(settings, textvariable=self.time_var, width=16)
        self.time_entry.grid(row=2, column=1, sticky="w", pady=3)
        ttk.Label(settings, text=_label(tr("world.label.day_time"))).grid(
            row=2, column=2, sticky="e", padx=(12, 6), pady=3
        )
        self.day_time_entry = ttk.Entry(settings, textvariable=self.day_time_var, width=16)
        self.day_time_entry.grid(row=2, column=3, sticky="w", pady=3)

        ttk.Label(settings, text=_label(tr("world.label.weather"))).grid(
            row=3, column=0, sticky="w", pady=3
        )
        self.weather_combo = ttk.Combobox(
            settings, textvariable=self.weather_var, values=WEATHER_CHOICES,
            state="readonly", width=14,
        )
        self.weather_combo.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Label(settings, text=_label(tr("world.label.weather_duration"))).grid(
            row=3, column=2, sticky="e", padx=(12, 6), pady=3
        )
        self.weather_duration_entry = ttk.Entry(settings, textvariable=self.weather_duration_var, width=16)
        self.weather_duration_entry.grid(row=3, column=3, sticky="w", pady=3)

        ttk.Label(settings, text=_label(tr("world.label.spawn"))).grid(
            row=4, column=0, sticky="w", pady=3
        )
        spawn_frame = ttk.Frame(settings)
        spawn_frame.grid(row=4, column=1, columnspan=3, sticky="w", pady=3)
        self.spawn_entries: dict[str, ttk.Entry] = {}
        for index, (label, var) in enumerate(
            (("X", self.spawn_x_var), ("Y", self.spawn_y_var),
             ("Z", self.spawn_z_var), (tr("world.label.spawn_angle"), self.spawn_angle_var))
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
            button_row, text=tr("button.apply_world"), style="Accent.TButton",
            command=self._ui_apply,
        )
        self.apply_button.pack(side="left")
        self.reload_button = ttk.Button(
            button_row, text=tr("button.reload_level"), command=self._ui_reload,
        )
        self.reload_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            button_row, text=tr("world.hint.backup"),
            style="Hint.TLabel",
        ).pack(side="left", padx=(10, 0))

        # ---- GameRules --------------------------------------------------- #
        rules_box = ttk.LabelFrame(self, text=tr("world.gamerules_box"), padding=8)
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
        self.gamerule_tree.heading("name", text=tr("world.heading.rule_name"))
        self.gamerule_tree.heading("value", text=tr("world.heading.value"))
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
        ttk.Label(controls, text=_label(tr("world.heading.rule_name"))).grid(
            row=0, column=0, sticky="w"
        )
        self.gamerule_name_var = tk.StringVar()
        self.gamerule_name_combo = ttk.Combobox(
            controls, textvariable=self.gamerule_name_var, state="readonly",
        )
        self.gamerule_name_combo.grid(row=0, column=1, columnspan=2, sticky="ew")
        ttk.Label(controls, text=_label(tr("world.heading.value"))).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        self.gamerule_value_var = tk.StringVar()
        self.gamerule_value_entry = ttk.Entry(controls, textvariable=self.gamerule_value_var)
        self.gamerule_value_entry.grid(row=1, column=1, sticky="ew", pady=(6, 0))
        self.set_rule_button = ttk.Button(
            controls, text=tr("button.set_rule"), command=self._ui_set_rule,
        )
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
        self._run_ui(
            self.app.reload_level, status=tr("status.level_reloaded"),
        )

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
            weather_text = tr(
                "world.weather.thunder",
                rain_time=rain_time,
                thunder_time=thunder_time,
            )
        elif weather.get("raining"):
            weather_text = tr("world.weather.rain", rain_time=rain_time)
        else:
            weather_text = tr("world.weather.clear", clear_time=clear_time)

        spawn = summary.get("spawn") or [0, -60, 0]
        info = {
            "level_name": str(summary.get("level_name") or ""),
            "data_version": str(summary.get("data_version", -1)),
            "version": tr(
                "world.info.version_format",
                name=version.get("name"),
                id=version.get("id"),
                series=version.get("series"),
            ),
            "seed": str(summary.get("seed", 0)),
            "game_type": str(summary.get("game_type") or ""),
            "difficulty": str(summary.get("difficulty") or ""),
            "hardcore": tr("common.yes") if summary.get("hardcore") else tr("common.no"),
            "allow_commands": (
                tr("common.yes") if summary.get("allow_commands") else tr("common.no")
            ),
            "spawn": tr(
                "world.info.spawn_format",
                x=spawn[0],
                y=spawn[1],
                z=spawn[2],
                angle=summary.get("spawn_angle", 0.0),
            ),
            "time": f"Time={summary.get('time', 0)}, DayTime={summary.get('day_time', 0)}",
            "weather": weather_text,
            "dimensions": tr("common.list_separator").join(world.list_dimensions()),
        }
        detected = getattr(world, "detected_version", None)
        profile = getattr(world, "version_profile", None)
        info["detected_version"] = detected.display if detected is not None else "—"
        info["selected_version"] = (
            tr(
                "world.info.selected_version_format",
                id=profile.id,
                status=_version_status_label(profile),
            )
            if profile is not None
            else "—"
        )
        mode = tr("version.mode_auto") if getattr(world, "version_auto", True) else tr("version.mode_manual")
        if getattr(world, "version_override", False):
            mode += tr("version.mode_mismatch_suffix")
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
                var.set(tr("common.no_world"))
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
            raise ValueError(tr("world.error.name_empty"))
        game_type = str(
            self._pick(overrides, ("game_type", "gametype"), self.gametype_var)
        ).strip().lower()
        if game_type not in GAME_TYPE_CHOICES:
            raise ValueError(
                tr(
                    "error.unknown_game_type",
                    value=repr(game_type),
                    choices=" / ".join(GAME_TYPE_CHOICES),
                )
            )
        difficulty = str(
            self._pick(overrides, ("difficulty",), self.difficulty_var)
        ).strip().lower()
        if difficulty not in DIFFICULTY_CHOICES:
            raise ValueError(
                tr(
                    "error.unknown_difficulty",
                    value=repr(difficulty),
                    choices=" / ".join(DIFFICULTY_CHOICES),
                )
            )
        weather = str(
            self._pick(overrides, ("weather",), self.weather_var)
        ).strip().lower()
        if weather not in WEATHER_CHOICES:
            raise ValueError(
                tr(
                    "error.unknown_weather",
                    value=repr(weather),
                    choices=" / ".join(WEATHER_CHOICES),
                )
            )

        game_time = _parse_int(
            self._pick(overrides, ("time", "game_time"), self.time_var),
            tr("field.game_time"),
            minimum=0,
        )
        day_time = _parse_int(
            self._pick(overrides, ("day_time",), self.day_time_var),
            tr("field.day_time"),
            minimum=0,
        )
        weather_duration = _parse_int(
            self._pick(overrides, ("weather_duration", "duration"), self.weather_duration_var),
            tr("field.weather_duration"),
            minimum=0,
        )
        spawn_x = _parse_int(
            self._pick(overrides, ("spawn_x", "x"), self.spawn_x_var),
            tr("field.spawn_x"),
        )
        spawn_y = _parse_int(
            self._pick(overrides, ("spawn_y", "y"), self.spawn_y_var),
            tr("field.spawn_y"),
        )
        spawn_z = _parse_int(
            self._pick(overrides, ("spawn_z", "z"), self.spawn_z_var),
            tr("field.spawn_z"),
        )
        spawn_angle = _parse_float(
            self._pick(overrides, ("spawn_angle", "angle"), self.spawn_angle_var),
            tr("field.spawn_angle"),
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
            tr(
                "status.world_settings_applied",
                name=repr(name),
                game_type=game_type,
                difficulty=difficulty,
                time=game_time,
                day_time=day_time,
                weather=weather,
                duration=weather_duration,
            )
        )
        return new_backups

    def set_gamerule(self, name: str, value: str) -> None:
        """设置一条游戏规则并立即保存。"""
        world = self.require_world()
        rule_name = str(name).strip()
        rule_value = str(value).strip()
        if not rule_name:
            raise ValueError(tr("world.error.rule_name_empty"))
        if not rule_value:
            raise ValueError(tr("world.error.rule_value_empty"))
        if any(ch.isspace() for ch in rule_value):
            raise ValueError(
                tr("world.error.rule_value_whitespace", value=repr(rule_value))
            )

        world.level.set_gamerule(rule_name, rule_value)
        world.mark_level_dirty()

        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])

        self.refresh()
        self.app.show_backups(new_backups)
        self.app.set_status(
            tr("status.gamerule_set", name=rule_name, value=rule_value)
        )


# --------------------------------------------------------------------------- #
# Tab 2: 方块 / 方块实体
# --------------------------------------------------------------------------- #
class BlockTab(_Tab):
    """方块读取/修改、区块摘要、方块实体 SNBT 编辑。"""

    title_key = "tab.block"

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky="ew")
        ttk.Label(top, text=_label(tr("label.dimension"))).pack(side="left")
        self.dimension_var = tk.StringVar(value="overworld")
        self.dimension_combo = ttk.Combobox(
            top, textvariable=self.dimension_var, values=["overworld"], width=24,
        )
        self.dimension_combo.pack(side="left")
        self.dimension_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_dimension_changed())
        ttk.Label(
            top, text=tr("block.hint.dimension"), style="Hint.TLabel",
        ).pack(side="left", padx=(8, 0))

        # ---- 方块 -------------------------------------------------------- #
        block_box = ttk.LabelFrame(self, text=tr("block.box"), padding=8)
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
            coord_frame, text=tr("button.read_block"), command=self._ui_read_block,
        )
        self.read_block_button.grid(row=0, column=6, padx=(12, 0))

        self.block_display_var = tk.StringVar(value=tr("block.not_read"))
        ttk.Label(
            block_box, textvariable=self.block_display_var, anchor="w",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=1, column=0, sticky="w", pady=(6, 2))

        state_frame = ttk.Frame(block_box)
        state_frame.grid(row=2, column=0, sticky="ew")
        state_frame.columnconfigure(1, weight=1)
        ttk.Label(state_frame, text=_label(tr("block.label.state"))).grid(
            row=0, column=0, sticky="w"
        )
        self.block_state_var = tk.StringVar(value="minecraft:stone")
        self.block_state_entry = ttk.Entry(state_frame, textvariable=self.block_state_var)
        self.block_state_entry.grid(row=0, column=1, sticky="ew")
        self.set_block_button = ttk.Button(
            state_frame, text=tr("button.set_block"), command=self._ui_set_block,
        )
        self.set_block_button.grid(row=0, column=2, padx=(8, 0))
        self.keep_be_var = tk.BooleanVar(value=False)
        self.keep_be_check = ttk.Checkbutton(
            state_frame, text=tr("block.keep_be"), variable=self.keep_be_var,
        )
        self.keep_be_check.grid(row=0, column=3, padx=(8, 0))

        self.chunk_info_var = tk.StringVar(value=tr("block.hint.chunk_info"))
        ttk.Label(
            block_box, textvariable=self.chunk_info_var, anchor="w",
            justify="left", wraplength=1050,
        ).grid(row=3, column=0, sticky="w", pady=(6, 0))

        # ---- 方块实体 ---------------------------------------------------- #
        be_box = ttk.LabelFrame(self, text=tr("block.be_box"), padding=8)
        be_box.grid(row=2, column=0, sticky="nsew", pady=(8, 0))
        be_box.columnconfigure(0, weight=1)
        be_box.rowconfigure(1, weight=1)

        be_bar = ttk.Frame(be_box)
        be_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.read_be_button = ttk.Button(
            be_bar, text=tr("button.read_be"), command=self._ui_read_be,
        )
        self.read_be_button.pack(side="left")
        self.write_be_button = ttk.Button(
            be_bar, text=tr("button.write_be"), command=self._ui_write_be,
        )
        self.write_be_button.pack(side="left", padx=(8, 0))
        self.delete_be_button = ttk.Button(
            be_bar, text=tr("button.delete_be"), command=self._ui_delete_be,
        )
        self.delete_be_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            be_bar,
            text=tr("block.hint.be_example"),
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
        bx = _parse_int(self.x_var.get() if x is None else x, tr("field.x_coord"))
        by = _parse_int(self.y_var.get() if y is None else y, tr("field.y_coord"))
        bz = _parse_int(self.z_var.get() if z is None else z, tr("field.z_coord"))
        return bx, by, bz

    def _on_dimension_changed(self) -> None:
        self.block_display_var.set(tr("block.dimension_changed"))
        self.chunk_info_var.set(tr("block.dimension_changed"))

    def _refresh_chunk_info(self, x: int, z: int, dimension: str) -> None:
        world = self.app.world
        if world is None:
            self.chunk_info_var.set(tr("common.no_world"))
            return
        cx, cz = x >> 4, z >> 4
        try:
            chunk = world.require_chunk(cx, cz, dimension)
        except Exception as exc:  # noqa: BLE001 - 仅用于展示
            self.chunk_info_var.set(
                tr("block.error.chunk_info", cx=cx, cz=cz, error=_exc_text(exc))
            )
            return
        summary = chunk.summary()
        lines = [
            tr(
                "block.chunk_info",
                cx=summary["chunk"][0],
                cz=summary["chunk"][1],
                status=summary["status"],
                data_version=summary["data_version"],
                min_section=summary["min_section"],
                sections=summary["sections"],
                block_entities=summary["block_entities"],
            ),
        ]
        entities: list[str] = []
        for be in chunk.iter_block_entities():
            entities.append(
                f"{be.get_string('id', '?')} @ "
                f"({be.get_int('x')}, {be.get_int('y')}, {be.get_int('z')})"
            )
        if entities:
            lines.append(
                tr(
                    "block.chunk_info.be",
                    items=tr("common.list_separator_semicolon").join(entities),
                )
            )
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
            raise ValueError(tr("error.dimension_empty"))
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
        self.block_display_var.set(tr("block.not_read"))
        self.chunk_info_var.set(tr("block.hint.chunk_info"))
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
        self.block_display_var.set(
            tr("block.display.read", x=bx, y=by, z=bz, dim=dim, state=state)
        )
        self.block_state_var.set(str(state))
        self._refresh_chunk_info(bx, bz, dim)
        self.app.set_status(
            tr("status.block_read", x=bx, y=by, z=bz, dim=dim, state=state)
        )
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
            raise ValueError(
                tr("block.error.invalid_state", value=repr(state_spec), error=_exc_text(exc))
            ) from exc

        change = world.set_block(
            bx, by, bz, state, dimension=dim, keep_block_entity=bool(keep_be)
        )
        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])

        self.block_display_var.set(
            tr("block.display.set", x=bx, y=by, z=bz, dim=dim, change=change)
        )
        self.block_state_var.set(str(state))
        self._refresh_chunk_info(bx, bz, dim)
        self.app.show_backups(new_backups)
        self.app.set_status(
            tr("status.block_set", x=bx, y=by, z=bz, dim=dim, change=change)
        )
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
            self.app.set_status(
                tr("status.be_missing", x=bx, y=by, z=bz, dim=dim)
            )
            return None
        # 读取方块实体时像格式化 JSON 一样输出多行缩进 SNBT，便于阅读/编辑
        _set_text(self.be_editor, to_snbt_pretty(be), readonly=False)
        self.app.set_status(tr("status.be_read", x=bx, y=by, z=bz, dim=dim))
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
            raise ValueError(tr("block.error.snbt_empty"))
        try:
            tag = parse_snbt(text)
        except Exception as exc:
            raise ValueError(
                tr("block.error.snbt_parse", error=_exc_text(exc))
            ) from exc
        if not isinstance(tag, CompoundTag):
            raise ValueError(tr("block.error.snbt_compound"))
        tag["x"] = Int(bx)
        tag["y"] = Int(by)
        tag["z"] = Int(bz)

        chunk = world.require_chunk(bx >> 4, bz >> 4, dim)
        try:
            chunk.set_block_entity(tag)
        except Exception as exc:
            raise WorldError(
                tr("block.error.write_be_failed", error=_exc_text(exc))
            ) from exc
        world.mark_chunk_dirty(chunk, dim)

        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])

        self._refresh_chunk_info(bx, bz, dim)
        _set_text(self.be_editor, to_snbt_pretty(tag), readonly=False)
        self.app.show_backups(new_backups)
        self.app.set_status(tr("status.be_written", x=bx, y=by, z=bz, dim=dim))

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
            raise WorldError(tr("block.error.no_be_delete", x=bx, y=by, z=bz, dim=dim))
        world.mark_chunk_dirty(chunk, dim)

        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])

        _set_text(self.be_editor, "", readonly=False)
        self._refresh_chunk_info(bx, bz, dim)
        self.app.show_backups(new_backups)
        self.app.set_status(tr("status.be_deleted", x=bx, y=by, z=bz, dim=dim))


# --------------------------------------------------------------------------- #
# Tab 3: 方块搜索（按注册名反向查找）
# --------------------------------------------------------------------------- #
class BlockSearchTab(_Tab):
    """按方块注册名反向查找当前维度所有已生成区块中的方块。

    读取上限 SHORT_MAX=32767 个匹配（达到后停止扫描），
    结果列表最多显示 DISPLAY_LIMIT=256 个坐标。
    """

    title_key = "tab.search"

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
        ttk.Label(bar, text=_label(tr("label.dimension"))).pack(side="left")
        self.dimension_var = tk.StringVar(value="overworld")
        self.dimension_combo = ttk.Combobox(
            bar, textvariable=self.dimension_var, values=["overworld"], width=20,
        )
        self.dimension_combo.pack(side="left")
        ttk.Label(bar, text=_label(tr("search.label.block_id"))).pack(
            side="left", padx=(12, 0)
        )
        self.block_var = tk.StringVar(value="minecraft:chest")
        self.block_entry = ttk.Entry(bar, textvariable=self.block_var, width=30)
        self.block_entry.pack(side="left")
        self.search_button = ttk.Button(
            bar, text=tr("button.start_search"), style="Accent.TButton", command=self._ui_start,
        )
        self.search_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(
            bar, text=tr("button.stop"), command=self._ui_stop, state="disabled",
        )
        self.stop_button.pack(side="left", padx=(6, 0))

        ttk.Label(
            self,
            text=tr(
                "search.hint.scan",
                max_matches=SHORT_MAX,
                display_limit=DISPLAY_LIMIT,
            ),
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
        self.status_var = tk.StringVar(value=tr("search.status.ready_initial"))
        ttk.Label(
            progress_row, textvariable=self.status_var, anchor="w", style="Hint.TLabel",
            wraplength=1080,
        ).grid(row=1, column=0, sticky="w", pady=(4, 6))

        # ---- 结果表 ------------------------------------------------------ #
        result_box = ttk.LabelFrame(self, text=tr("search.result_box"), padding=6)
        result_box.grid(row=3, column=0, sticky="nsew")
        result_box.columnconfigure(0, weight=1)
        result_box.rowconfigure(0, weight=1)
        columns = ("index", "x", "y", "z", "state")
        headings = {"index": "#", "x": "X", "y": "Y", "z": "Z",
                    "state": tr("search.heading.state")}
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
            bottom, text=tr("button.locate_block"), command=self._on_read_selected,
        ).pack(side="left")
        ttk.Button(bottom, text=tr("button.clear_results"), command=self._clear_results).pack(
            side="left", padx=(8, 0)
        )
        self.result_var = tk.StringVar(value=tr("common.no_results"))
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
            self.status_var.set(tr("common.no_world"))
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
                tr("search.status.ready", dim=self.dimension_var.get())
            )

    # ---- 搜索 ------------------------------------------------------------ #
    def start_search(self, block_spec: Optional[str] = None,
                     dimension: Optional[str] = None) -> None:
        world = self.require_world()
        if self._running:
            raise RuntimeError(tr("error.search_running"))
        spec = str(block_spec if block_spec is not None else self.block_var.get()).strip()
        if not spec:
            raise ValueError(tr("search.error.block_id_empty"))
        # 提前解析以校验语法（不要求方块一定存在）
        BlockState.parse(spec)
        dim = World.normalize_dimension(dimension or self.dimension_var.get())
        if dim not in world.list_dimensions():
            raise ValueError(tr("error.dimension_missing", dim=dim))

        self._running = True
        self._search_world_path = world.path
        self._cancel_event.clear()
        with self._progress_lock:
            self._latest_progress = (0, 0, 0)
        self.progress.configure(maximum=1, value=0)
        self.tree.delete(*self.tree.get_children())
        self._last_result = None
        self.result_var.set(tr("search.status.searching"))
        self.search_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set(
            tr("search.status.scanning", dim=dim, spec=spec)
        )
        self.app.set_status(tr("status.block_search", spec=spec, dim=dim))

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
        self.status_var.set(tr("status.stopping_search"))
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
            tr(
                "search.status.progress",
                scanned=scanned,
                total=total if total else "?",
                found=found,
                max_matches=SHORT_MAX,
            )
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
            self.status_var.set(
                tr("search.status.failed", error=_exc_text(payload))
            )
            self.app.show_error(tr("dialog.error.block_search.title"), payload)
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
            status = tr(
                "search.status.cancelled",
                total_found=result.total_found,
                scanned=result.chunks_scanned,
                total=result.chunks_total,
                elapsed=result.elapsed,
            )
        elif result.truncated:
            status = tr(
                "search.status.truncated",
                max_matches=SHORT_MAX,
                total_found=result.total_found,
                shown=shown,
                elapsed=result.elapsed,
            )
        else:
            status = tr(
                "search.status.done",
                total_found=result.total_found,
                shown=shown,
                scanned=result.chunks_scanned,
                total=result.chunks_total,
                elapsed=result.elapsed,
            )
        if result.errors:
            status += tr("search.status.chunk_errors", count=len(result.errors))
        self.status_var.set(status)
        self.result_var.set(
            tr(
                "search.result_summary",
                dim=result.dimension,
                block=result.block,
                total_found=result.total_found,
                shown=shown,
            )
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
            self.app.set_status(tr("status.select_row"))
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
            self.app.set_status(
                tr("status.located_block", x=x, y=y, z=z, dim=dimension, state=state)
            )
        except Exception as exc:  # noqa: BLE001 - UI 边界
            self.app.show_error(tr("dialog.error.read_selected_block.title"), exc)

    def _clear_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._last_result = None
        self.result_var.set(tr("common.no_results"))
        self.progress.configure(value=0)
        self.status_var.set(tr("status.results_cleared"))

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

    title_key = "tab.be_search"

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
        ttk.Label(bar, text=_label(tr("label.dimension"))).pack(side="left")
        self.dimension_var = tk.StringVar(value="overworld")
        self.dimension_combo = ttk.Combobox(
            bar, textvariable=self.dimension_var, values=["overworld"], width=20,
        )
        self.dimension_combo.pack(side="left")
        ttk.Label(bar, text=_label(tr("be_search.label.type"))).pack(
            side="left", padx=(12, 0)
        )
        self.be_var = tk.StringVar(value="minecraft:chest")
        self.be_entry = ttk.Entry(bar, textvariable=self.be_var, width=30)
        self.be_entry.pack(side="left")
        self.search_button = ttk.Button(
            bar, text=tr("button.start_search"), style="Accent.TButton", command=self._ui_start,
        )
        self.search_button.pack(side="left", padx=(8, 0))
        self.stop_button = ttk.Button(
            bar, text=tr("button.stop"), command=self._ui_stop, state="disabled",
        )
        self.stop_button.pack(side="left", padx=(6, 0))

        ttk.Label(
            self,
            text=tr(
                "be_search.hint.scan",
                max_matches=SHORT_MAX,
                display_limit=DISPLAY_LIMIT,
            ),
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
        self.status_var = tk.StringVar(value=tr("be_search.status.ready_initial"))
        ttk.Label(
            progress_row, textvariable=self.status_var, anchor="w", style="Hint.TLabel",
            wraplength=1080,
        ).grid(row=1, column=0, sticky="w", pady=(4, 6))

        result_box = ttk.LabelFrame(
            self, text=tr("be_search.result_box"), padding=6,
        )
        result_box.grid(row=3, column=0, sticky="nsew")
        result_box.columnconfigure(0, weight=1)
        result_box.rowconfigure(0, weight=1)
        columns = ("index", "x", "y", "z", "type")
        headings = {"index": "#", "x": "X", "y": "Y", "z": "Z",
                    "type": tr("be_search.heading.type")}
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
            bottom, text=tr("button.locate_be"), command=self._on_read_selected,
        ).pack(side="left")
        ttk.Button(bottom, text=tr("button.clear_results"), command=self._clear_results).pack(
            side="left", padx=(8, 0)
        )
        self.result_var = tk.StringVar(value=tr("common.no_results"))
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
            self.status_var.set(tr("common.no_world"))
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
                tr("be_search.status.ready", dim=self.dimension_var.get())
            )

    # ---- 搜索 ------------------------------------------------------------ #
    def start_search(self, be_spec: Optional[str] = None,
                     dimension: Optional[str] = None) -> None:
        world = self.require_world()
        if self._running:
            raise RuntimeError(tr("error.search_running"))
        spec = str(be_spec if be_spec is not None else self.be_var.get()).strip()
        if not spec:
            raise ValueError(tr("be_search.error.type_empty"))
        target = normalize_block_entity_id(spec)
        dim = World.normalize_dimension(dimension or self.dimension_var.get())
        if dim not in world.list_dimensions():
            raise ValueError(tr("error.dimension_missing", dim=dim))

        self._running = True
        self._search_world_path = world.path
        self._cancel_event.clear()
        with self._progress_lock:
            self._latest_progress = (0, 0, 0)
        self.progress.configure(maximum=1, value=0)
        self.tree.delete(*self.tree.get_children())
        self._last_result = None
        self.result_var.set(tr("search.status.searching"))
        self.search_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set(
            tr("be_search.status.scanning", dim=dim, target=target)
        )
        self.app.set_status(tr("status.be_search", spec=target, dim=dim))

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
        self.status_var.set(tr("status.stopping_search"))
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
            tr(
                "search.status.progress",
                scanned=scanned,
                total=total if total else "?",
                found=found,
                max_matches=SHORT_MAX,
            )
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
            self.status_var.set(
                tr("search.status.failed", error=_exc_text(payload))
            )
            self.app.show_error(tr("dialog.error.be_search.title"), payload)
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
            status = tr(
                "search.status.cancelled",
                total_found=result.total_found,
                scanned=result.chunks_scanned,
                total=result.chunks_total,
                elapsed=result.elapsed,
            )
        elif result.truncated:
            status = tr(
                "search.status.truncated",
                max_matches=SHORT_MAX,
                total_found=result.total_found,
                shown=shown,
                elapsed=result.elapsed,
            )
        else:
            status = tr(
                "search.status.done",
                total_found=result.total_found,
                shown=shown,
                scanned=result.chunks_scanned,
                total=result.chunks_total,
                elapsed=result.elapsed,
            )
        if result.errors:
            status += tr("search.status.chunk_errors", count=len(result.errors))
        self.status_var.set(status)
        self.result_var.set(
            tr(
                "be_search.result_summary",
                dim=result.dimension,
                block_entity=result.block_entity,
                total_found=result.total_found,
                shown=shown,
            )
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
            self.app.set_status(tr("status.select_row"))
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
                tr("status.located_be", x=x, y=y, z=z, dim=dimension)
            )
        except Exception as exc:  # noqa: BLE001 - UI 边界
            self.app.show_error(tr("dialog.error.read_selected_be.title"), exc)

    def _clear_results(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self._last_result = None
        self.result_var.set(tr("common.no_results"))

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

    title_key = "tab.player"

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
            "uuid": "UUID", "dimension": tr("player.heading.dimension"),
            "game_type": tr("player.heading.game_type"),
            "health": "HP", "food": tr("player.heading.food"),
            "xp_level": tr("player.heading.xp_level"),
            "position": tr("player.heading.position"),
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
        self.player_hint_var = tk.StringVar(value=tr("common.no_world"))
        self.player_hint_label = ttk.Label(
            hint_bar, textvariable=self.player_hint_var, anchor="w", style="Hint.TLabel",
            wraplength=1000,
        )
        self.player_hint_label.grid(row=0, column=0, sticky="w")
        self.refresh_players_button = ttk.Button(
            hint_bar, text=tr("button.refresh_players"),
            command=lambda: self._run_ui(self.refresh_players),
        )
        self.refresh_players_button.grid(row=0, column=1)

        # ---- 属性编辑 ---------------------------------------------------- #
        edit_box = ttk.LabelFrame(self, text=tr("player.edit_box"), padding=8)
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
            ttk.Label(edit_box, text=_label(label)).grid(
                row=row, column=column * 2, sticky="w", pady=3
            )
            entry = ttk.Entry(edit_box, textvariable=var, width=width)
            entry.grid(row=row, column=column * 2 + 1, sticky="w", pady=3)
            return entry

        self.player_entries: dict[str, ttk.Entry] = {}
        self.player_entries["health"] = add_field(
            0, 0, tr("field.health"), self.health_var,
        )
        self.player_entries["food"] = add_field(
            0, 1, tr("field.food"), self.food_var,
        )
        self.player_entries["saturation"] = add_field(
            0, 2, tr("field.saturation"), self.saturation_var, width=14
        )
        self.player_entries["xp_level"] = add_field(
            1, 0, tr("field.xp_level"), self.xp_level_var,
        )
        self.player_entries["xp_progress"] = add_field(
            1, 1, tr("player.label.xp_progress"), self.xp_progress_var, width=14
        )
        self.player_entries["xp_total"] = add_field(
            1, 2, tr("field.xp_total"), self.xp_total_var,
        )
        self.player_entries["pos_x"] = add_field(
            2, 0, tr("field.pos_x"), self.pos_x_var,
        )
        self.player_entries["pos_y"] = add_field(
            2, 1, tr("field.pos_y"), self.pos_y_var,
        )
        self.player_entries["pos_z"] = add_field(
            2, 2, tr("field.pos_z"), self.pos_z_var,
        )

        ttk.Label(edit_box, text=_label(tr("player.label.game_type"))).grid(
            row=3, column=0, sticky="w", pady=3
        )
        self.player_gametype_combo = ttk.Combobox(
            edit_box, textvariable=self.player_gametype_var, values=GAME_TYPE_CHOICES,
            state="readonly", width=12,
        )
        self.player_gametype_combo.grid(row=3, column=1, sticky="w", pady=3)
        ttk.Label(edit_box, text=_label(tr("label.dimension"))).grid(
            row=3, column=2, sticky="e", padx=(12, 6), pady=3
        )
        self.player_dimension_combo = ttk.Combobox(
            edit_box, textvariable=self.player_dimension_var,
            values=PLAYER_DIMENSION_CHOICES, state="readonly", width=14,
        )
        self.player_dimension_combo.grid(row=3, column=3, sticky="w", pady=3)

        self.player_current_var = tk.StringVar(value=tr("player.current_none"))
        ttk.Label(
            edit_box, textvariable=self.player_current_var, anchor="w", style="Hint.TLabel",
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

        button_row = ttk.Frame(edit_box)
        button_row.grid(row=5, column=0, columnspan=6, sticky="w", pady=(8, 0))
        self.apply_player_button = ttk.Button(
            button_row, text=tr("button.apply_player"), style="Accent.TButton",
            command=self._ui_apply,
        )
        self.apply_player_button.pack(side="left")
        self.heal_button = ttk.Button(
            button_row, text=tr("button.heal"), command=self._ui_heal,
        )
        self.heal_button.pack(side="left", padx=(8, 0))
        self.inventory_button = ttk.Button(
            button_row, text=tr("button.edit_inventory"), command=self._ui_inventory,
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
            self.app.show_error(tr("dialog.error.load_player.title"), exc)

    def _ui_apply(self) -> None:
        self._run_ui(self.apply_player_edits)

    def _ui_heal(self) -> None:
        self._run_ui(self.heal_player)

    def _ui_inventory(self) -> None:
        """打开当前玩家的物品栏编辑对话框。"""
        player = self.current_player
        if player is None:
            self.app.set_status(tr("player.status.select_first"))
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
            self.app.show_error(tr("dialog.error.open_inventory.title"), exc)

    def _clear_fields(self) -> None:
        for var in (
            self.health_var, self.food_var, self.saturation_var,
            self.xp_level_var, self.xp_progress_var, self.xp_total_var,
            self.pos_x_var, self.pos_y_var, self.pos_z_var,
        ):
            var.set("")
        self.player_gametype_var.set(GAME_TYPE_CHOICES[0])
        self.player_dimension_var.set("overworld")
        self.player_current_var.set(tr("player.current_none"))

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
                self.player_hint_var.set(tr("common.no_world"))
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
            hints = [tr("player.hint.total_players", count=loaded)]
            if broken:
                hints.append(tr("player.hint.broken_files", count=broken))
            if loaded == 0:
                hints.append(tr("player.hint.no_players"))
            warning = getattr(world, "host_player_warning", None)
            if warning:
                hints.append(tr("player.hint.warning", warning=warning))
            elif getattr(world, "host_player_uuid", None):
                hints.append(tr("player.hint.host_player"))
            self.player_hint_var.set(
                tr("common.list_separator_semicolon").join(hints)
            )
        finally:
            self._suppress_select = False

    def select_player(self, uuid: str) -> None:
        """按 UUID 加载玩家并在下方显示可编辑属性。"""
        world = self.require_world()
        text = str(uuid).strip()
        if not text:
            raise ValueError(tr("player.error.uuid_empty"))
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
            tr(
                "player.current",
                uuid=player.uuid,
                filename=os.path.basename(player.path),
            )
        )
        self._sync_tree_selection()
        self.app.set_status(tr("status.player_loaded", uuid=player.uuid))

    def apply_player_edits(self, **overrides: Any) -> None:
        """校验并保存玩家属性修改。"""
        world = self.require_world()
        player = self.current_player
        if player is None:
            raise RuntimeError(tr("player.error.select_player"))

        def pick(keys: tuple[str, ...], var: tk.StringVar) -> Any:
            return self._pick(overrides, keys, var)

        health = _parse_float(
            pick(("health",), self.health_var), tr("field.health"), minimum=0.0,
        )
        if health > 1e6:
            raise ValueError(tr("player.error.health_too_high"))
        food = _parse_int(
            pick(("food", "food_level"), self.food_var), tr("field.food"), 0, 20,
        )
        saturation = _parse_float(
            pick(("saturation", "food_saturation"), self.saturation_var),
            tr("field.saturation"), minimum=0.0,
        )
        xp_level = _parse_int(
            pick(("xp_level",), self.xp_level_var), tr("field.xp_level"), minimum=0,
        )
        xp_progress = _parse_float(
            pick(("xp_progress", "xp_p"), self.xp_progress_var), tr("field.xp_progress"),
        )
        if not 0.0 <= xp_progress < 1.0:
            raise ValueError(
                tr("player.error.xp_progress_range", value=xp_progress)
            )
        xp_total = _parse_int(
            pick(("xp_total",), self.xp_total_var), tr("field.xp_total"), minimum=0,
        )
        pos_x = _parse_float(pick(("pos_x", "x"), self.pos_x_var), tr("field.pos_x"))
        pos_y = _parse_float(pick(("pos_y", "y"), self.pos_y_var), tr("field.pos_y"))
        pos_z = _parse_float(pick(("pos_z", "z"), self.pos_z_var), tr("field.pos_z"))
        game_type = str(pick(("game_type", "gametype"), self.player_gametype_var)).strip().lower()
        if game_type not in GAME_TYPE_CHOICES:
            raise ValueError(
                tr(
                    "error.unknown_game_type",
                    value=repr(game_type),
                    choices=" / ".join(GAME_TYPE_CHOICES),
                )
            )
        dimension = str(pick(("dimension",), self.player_dimension_var)).strip()
        if not dimension:
            raise ValueError(tr("error.dimension_empty"))

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
        self.app.set_status(tr("status.player_saved", uuid=player_uuid))

    def heal_player(self) -> None:
        """把当前玩家回满血并恢复饥饿度。"""
        world = self.require_world()
        player = self.current_player
        if player is None:
            raise RuntimeError(tr("player.error.select_player"))
        player.heal()
        world.mark_player_dirty(player)
        before = len(world.backups)
        world.flush()
        new_backups = list(world.backups[before:])
        player_uuid = player.uuid
        self.refresh_players()
        self.select_player(player_uuid)
        self.app.show_backups(new_backups)
        self.app.set_status(tr("status.player_healed", uuid=player_uuid))

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
            raise RuntimeError(tr("inventory.error.no_world"))
        self.parent_tab = parent_tab
        self.app = app
        self.world = world
        self.player = player
        self.item_format = world.version_profile.item_format
        self.extra_tag_name = extra_key(self.item_format)
        self._current_slot: Optional[int] = None
        self._suppress = False

        self.title(tr("inventory.title", uuid=player.uuid))
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
            header, text=tr("inventory.header.player", uuid=self.player.uuid),
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text=tr(
                "inventory.header.details",
                version=self.world.version_profile.id,
                item_format=self.item_format,
                extra_tag=self.extra_tag_name,
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
        left = ttk.LabelFrame(body, text=tr("inventory.slots_box"), padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        columns = ("slot_label", "item_id", "count", "extra")
        headings = {
            "slot_label": tr("common.heading.slot"),
            "item_id": tr("common.heading.item_id"),
            "count": tr("common.heading.count"),
            "extra": tr("common.heading.extra_nbt"),
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
        right = ttk.LabelFrame(body, text=tr("inventory.editor_box"), padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(4, weight=1)

        ttk.Label(right, text=tr("common.label.current_slot")).grid(
            row=0, column=0, sticky="w", pady=2
        )
        self.slot_var = tk.StringVar(value=tr("common.not_selected"))
        ttk.Label(
            right, textvariable=self.slot_var, style="Hint.TLabel",
        ).grid(row=0, column=1, sticky="w", pady=2)

        ttk.Label(right, text=tr("common.label.item_id")).grid(
            row=1, column=0, sticky="w", pady=2
        )
        self.id_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.id_var).grid(
            row=1, column=1, sticky="ew", pady=2
        )

        ttk.Label(right, text=tr("common.label.count")).grid(
            row=2, column=0, sticky="w", pady=2
        )
        self.count_var = tk.StringVar(value="1")
        ttk.Entry(right, textvariable=self.count_var, width=12).grid(
            row=2, column=1, sticky="w", pady=2
        )

        ttk.Label(
            right,
            text=tr("common.label.extra_nbt_snbt", tag=self.extra_tag_name),
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 2))
        self.extra_editor = ScrolledText(
            right, height=12, wrap="word", undo=False, font=self.app.mono_font,
        )
        self.extra_editor.grid(row=4, column=0, columnspan=2, sticky="nsew")

        ttk.Label(
            right,
            text=tr("inventory.hint.extra"),
            style="Hint.TLabel",
            wraplength=360,
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        buttons = ttk.Frame(right)
        buttons.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.save_button = ttk.Button(
            buttons, text=tr("button.save_slot"), style="Accent.TButton", command=self._save,
        )
        self.save_button.pack(side="left")
        self.delete_button = ttk.Button(
            buttons, text=tr("button.delete_slot"), command=self._delete,
        )
        self.delete_button.pack(side="left", padx=(8, 0))
        ttk.Button(buttons, text=tr("button.reload"), command=self._refresh).pack(
            side="left", padx=(8, 0)
        )
        ttk.Button(buttons, text=tr("button.close"), command=self._close).pack(
            side="left", padx=(8, 0)
        )

        self.status_var = tk.StringVar(value=tr("common.status.select_slot_hint"))
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
        self.slot_var.set(
            tr("inventory.slot_label", slot=slot, label=_slot_label(slot))
        )
        if item is None:
            self.id_var.set("")
            self.count_var.set("1")
            _set_text(self.extra_editor, "", readonly=False)
            self.delete_button.configure(state="disabled")
            self.status_var.set(tr("inventory.status.empty_slot"))
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
            tr(
                "inventory.status.loaded",
                slot=slot,
                description=_describe_item(item, self.item_format),
            )
        )

    def _reload_rows(self, select_slot: Optional[int] = None) -> None:
        self._suppress = True
        try:
            self.tree.delete(*self.tree.get_children())
            items = self.player.inventory_slot_map()
            for slot in INVENTORY_SLOTS:
                item = items.get(slot)
                if item is None:
                    values = (_slot_label(slot), "—", "", "")
                else:
                    extra = item_extra(item, self.item_format)
                    preview = ""
                    if extra is not None and len(extra) > 0:
                        preview = to_snbt(extra)
                        if len(preview) > 80:
                            preview = preview[:77] + "..."
                    values = (
                        _slot_label(slot),
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
            raise ValueError(
                tr("error.extra_nbt_parse", error=_exc_text(exc))
            ) from exc
        if not isinstance(tag, CompoundTag):
            raise ValueError(tr("error.extra_nbt_compound"))
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
        if new_backups:
            return tr("status.backup_count", count=len(new_backups))
        return tr("status.written")

    def _save(self) -> None:
        try:
            if self._current_slot is None:
                raise ValueError(tr("inventory.error.no_slot"))
            if not self.id_var.get().strip():
                raise ValueError(tr("error.item_id_empty"))
            try:
                count = int(self.count_var.get().strip())
            except ValueError:
                raise ValueError(tr("error.item_count_not_int")) from None
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
            messagebox.showerror(
                tr("dialog.error.save_item.title"), _exc_text(exc), parent=self,
            )
            return
        slot = self._current_slot
        self._reload_rows(select_slot=slot)
        self.status_var.set(
            tr(
                "inventory.status.saved",
                slot=slot,
                description=_describe_item(item, self.item_format),
                backup=backup_status,
            )
        )
        self.app.set_status(tr("status.inventory_saved", slot=slot))

    def _delete(self) -> None:
        if self._current_slot is None:
            return
        slot = self._current_slot
        if self.player.get_inventory_item(slot) is None:
            self.status_var.set(tr("inventory.status.empty_no_delete"))
            return
        if not messagebox.askyesno(
            tr("dialog.confirm.delete_item.title"),
            tr(
                "inventory.confirm.delete",
                slot=slot,
                label=_slot_label(slot),
            ),
            parent=self,
        ):
            return
        try:
            self.player.delete_inventory_item(slot)
            backup_status = self._flush_player()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                tr("dialog.error.delete_item.title"), _exc_text(exc), parent=self,
            )
            return
        self._reload_rows(select_slot=slot)
        self.status_var.set(
            tr("inventory.status.deleted", slot=slot, backup=backup_status)
        )

    def _refresh(self) -> None:
        try:
            self.player = self.world.get_player(self.player.uuid)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                tr("dialog.error.reload.title"), _exc_text(exc), parent=self,
            )
            return
        self._reload_rows()
        self.status_var.set(tr("inventory.status.reloaded"))

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
        self.app.set_status(tr("status.inventory_closed"))
        self.destroy()


# --------------------------------------------------------------------------- #
# 模组数据页（子标签页：Curios）
# --------------------------------------------------------------------------- #
class ModDataTab(_Tab):
    """模组扩展数据页；每个子标签页自行标注模组/游戏/加载器版本。"""

    title_key = "tab.moddata"

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(self)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)
        self.curios_tab = CuriosTab(self.notebook, self.app)
        self.notebook.add(self.curios_tab, text=tr("tab.curios"))

    def refresh(self) -> None:
        self.curios_tab.refresh()


class CuriosTab(_Tab):
    """Curios 玩家饰品栏读取/修改。

    版本标注为工作区 Curios-1.21.1 源码的构建元数据：
      - Curios 9.5.1+1.21.1
      - Minecraft 1.21.1
      - NeoForge 21.1.60+（范围 [21.1.60,)）
    """

    title_key = "tab.curios"
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
            text=tr("curios.banner.mod", version=self.MOD_VERSION),
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            banner,
            text=tr(
                "curios.banner.versions",
                game=self.GAME_VERSION,
                loader=self.LOADER_VERSION,
            ),
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            banner,
            text=tr("curios.warning.version"),
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
            text=tr("curios.hint.data_path"),
            style="Hint.TLabel",
        ).grid(row=4, column=0, sticky="w", pady=(2, 0))
        ttk.Label(
            banner, text=tr("curios.warning.validation"),
            style="Hint.TLabel", wraplength=1100, justify="left",
        ).grid(row=5, column=0, sticky="w", pady=(2, 0))

        # ---- 玩家选择 ---------------------------------------------------- #
        player_bar = ttk.Frame(self, padding=(8, 6))
        player_bar.grid(row=1, column=0, sticky="ew")
        player_bar.columnconfigure(3, weight=1)
        ttk.Label(player_bar, text=_label(tr("curios.label.player"))).grid(
            row=0, column=0, sticky="w"
        )
        self.player_var = tk.StringVar()
        self.player_combo = ttk.Combobox(
            player_bar, textvariable=self.player_var, state="readonly", width=42,
        )
        self.player_combo.grid(row=0, column=1, sticky="w", padx=(0, 8))
        self.player_combo.bind("<<ComboboxSelected>>", self._on_player_change)
        ttk.Button(
            player_bar, text=tr("button.reload"), command=self._reload_player,
        ).grid(row=0, column=2, sticky="w")
        self.player_info_var = tk.StringVar(value=tr("common.no_world"))
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

        left = ttk.LabelFrame(body, text=tr("curios.slots_box"), padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        columns = ("group", "slot", "item", "count", "extra")
        headings = {
            "group": tr("curios.heading.group"),
            "slot": tr("common.heading.slot"),
            "item": tr("common.heading.item_id"),
            "count": tr("common.heading.count"),
            "extra": tr("common.heading.extra_nbt"),
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

        right = ttk.LabelFrame(body, text=tr("curios.editor_box"), padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(4, weight=1)

        ttk.Label(right, text=tr("common.label.current_slot")).grid(
            row=0, column=0, sticky="w", pady=2
        )
        self.slot_var = tk.StringVar(value=tr("common.not_selected"))
        ttk.Label(
            right, textvariable=self.slot_var, style="Hint.TLabel",
            wraplength=360, justify="left",
        ).grid(row=0, column=1, sticky="w", pady=2)

        ttk.Label(right, text=tr("common.label.item_id")).grid(
            row=1, column=0, sticky="w", pady=2
        )
        self.id_var = tk.StringVar()
        ttk.Entry(right, textvariable=self.id_var).grid(
            row=1, column=1, sticky="ew", pady=2
        )

        ttk.Label(right, text=tr("common.label.count")).grid(
            row=2, column=0, sticky="w", pady=2
        )
        self.count_var = tk.StringVar(value="1")
        ttk.Entry(right, textvariable=self.count_var, width=12).grid(
            row=2, column=1, sticky="w", pady=2
        )

        self.extra_label_var = tk.StringVar(value=tr("curios.label.extra_default"))
        ttk.Label(right, textvariable=self.extra_label_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 2),
        )
        self.extra_editor = ScrolledText(
            right, height=12, wrap="word", undo=False, font=self.app.mono_font,
        )
        self.extra_editor.grid(row=4, column=0, columnspan=2, sticky="nsew")
        ttk.Label(
            right,
            text=tr("curios.hint.extra"),
            style="Hint.TLabel", wraplength=380, justify="left",
        ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

        buttons = ttk.Frame(right)
        buttons.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.save_button = ttk.Button(
            buttons, text=tr("button.save_slot"), style="Accent.TButton", command=self._save,
        )
        self.save_button.pack(side="left")
        self.delete_button = ttk.Button(
            buttons, text=tr("button.delete_slot"), command=self._delete,
        )
        self.delete_button.pack(side="left", padx=(8, 0))
        ttk.Button(
            buttons, text=tr("button.reload"), command=self._reload_player,
        ).pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value=tr("common.status.select_slot_hint"))
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
            self.player_info_var.set(tr("common.no_world"))
            self.mismatch_var.set("")
            self.groups = []
            self._row_map.clear()
            self._reload_table()
            self._clear_editor()
            self.status_var.set(tr("status.no_world"))
            return
        if world.version_profile.id != self.GAME_VERSION:
            self.mismatch_var.set(
                tr(
                    "curios.mismatch",
                    version=world.version_profile.id,
                    game=self.GAME_VERSION,
                )
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
            self.player_info_var.set(tr("curios.no_players"))
            self._reload_table()
            self.status_var.set(tr("curios.status.no_player_data"))
            return
        try:
            self._load_player(active)
        except Exception as exc:  # noqa: BLE001 - UI 边界
            self.app.show_error(tr("dialog.error.load_curios.title"), exc)

    def _on_player_change(self, _event: "tk.Event | None" = None) -> None:
        uuid = self.player_var.get()
        if uuid:
            try:
                self._load_player(uuid)
            except Exception as exc:  # noqa: BLE001
                self.app.show_error(tr("dialog.error.load_curios.title"), exc)

    def _reload_player(self) -> None:
        world = self.app.world
        uuid = self.player_var.get()
        if world is None or not uuid:
            return
        try:
            self.player = world.get_player(uuid)
            self._reload_table()
            self.status_var.set(tr("curios.status.reloaded"))
        except Exception as exc:  # noqa: BLE001
            self.app.show_error(tr("dialog.error.reload_curios.title"), exc)

    def _load_player(self, uuid: str) -> None:
        if self.app.world is None:
            return
        self.player = self.app.world.get_player(uuid)
        self.groups = list_groups(self.player)
        item_format = self.app.world.version_profile.item_format
        self.extra_label_var.set(
            tr("curios.label.extra", tag=extra_key(item_format))
        )
        if not has_curios(self.player):
            self.player_info_var.set(
                tr("curios.info.no_data", uuid=uuid)
            )
        else:
            total_slots = sum(max(0, g.size) for g in self.groups)
            equipped = sum(len(g.items) for g in self.groups)
            self.player_info_var.set(
                tr(
                    "curios.info.summary",
                    uuid=uuid,
                    groups=len(self.groups),
                    slots=total_slots,
                    equipped=equipped,
                )
            )
        self._reload_table()
        self.status_var.set(tr("curios.status.loaded"))

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
                            tr(
                                "curios.group_display",
                                name=_curio_slot_name(group.identifier),
                                id=group.identifier,
                            ),
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
                            tr(
                                "curios.group_display",
                                name=_curio_slot_name(group.identifier),
                                id=group.identifier,
                            ),
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
            tr(
                "curios.slot_display",
                name=_curio_slot_name(identifier),
                id=identifier,
                slot=slot + 1,
                size=group.size,
            )
        )
        if item is None:
            self.id_var.set("")
            self.count_var.set("1")
            _set_text(self.extra_editor, "", readonly=False)
            self.delete_button.configure(state="disabled")
            self.status_var.set(tr("curios.status.empty_slot"))
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
            tr(
                "curios.status.loaded_row",
                identifier=identifier,
                slot=slot + 1,
                item_id=item_id(item) or "?",
            )
        )

    def _clear_editor(self) -> None:
        self._current = None
        self.slot_var.set(tr("common.not_selected"))
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
            raise ValueError(
                tr("error.extra_nbt_parse", error=_exc_text(exc))
            ) from exc
        if not isinstance(tag, CompoundTag):
            raise ValueError(tr("error.extra_nbt_compound"))
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
        if backups:
            return tr("status.backup_count", count=len(backups))
        return tr("status.written")

    def _reload_player_from_disk(self) -> None:
        """保存/删除前重新读盘，避免用打开页面时的旧玩家数据覆盖其它标签页的修改。"""
        if self.player is None or self.app.world is None:
            raise RuntimeError(tr("curios.error.no_player"))
        self.player = self.app.world.get_player(self.player.uuid)
        self.groups = list_groups(self.player)

    def _save(self) -> None:
        if self._current is None:
            messagebox.showinfo(
                tr("dialog.info.title"), tr("curios.info.select_slot"),
                parent=self.winfo_toplevel(),
            )
            return
        identifier, slot = self._current
        try:
            # 先刷新，避免覆盖玩家页/物品栏页刚才的修改
            self._reload_player_from_disk()
            if not self.id_var.get().strip():
                raise ValueError(tr("error.item_id_empty"))
            try:
                count = int(self.count_var.get().strip())
            except ValueError:
                raise ValueError(tr("error.item_count_not_int")) from None
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
            messagebox.showerror(
                tr("dialog.error.save_curio.title"), _exc_text(exc),
                parent=self.winfo_toplevel(),
            )
            return
        self._load_player(self.player.uuid)
        self._reload_table(select=(identifier, slot))
        self.status_var.set(
            tr(
                "curios.status.saved",
                identifier=identifier,
                slot=slot + 1,
                item_id=item_id(item),
                count=item_count(item, item_format),
                backup=backup_status,
            )
        )
        self.app.set_status(
            tr("status.curios_saved", identifier=identifier, slot=slot + 1)
        )

    def _delete(self) -> None:
        if self._current is None:
            return
        identifier, slot = self._current
        try:
            self._reload_player_from_disk()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                tr("dialog.error.reload_curios.title"), _exc_text(exc),
                parent=self.winfo_toplevel(),
            )
            return
        if get_curio_item(self.player, identifier, slot) is None:
            self.status_var.set(tr("inventory.status.empty_no_delete"))
            return
        if not messagebox.askyesno(
            tr("dialog.confirm.delete_curio.title"),
            tr(
                "curios.confirm.delete",
                name=_curio_slot_name(identifier),
                id=identifier,
                slot=slot + 1,
            ),
            parent=self.winfo_toplevel(),
        ):
            return
        try:
            delete_curio_item(self.player, identifier, slot)
            backup_status = self._flush_player()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                tr("dialog.error.delete_curio.title"), _exc_text(exc),
                parent=self.winfo_toplevel(),
            )
            return
        self._load_player(self.player.uuid)
        self._reload_table(select=(identifier, slot))
        self.status_var.set(
            tr(
                "curios.status.deleted",
                identifier=identifier,
                slot=slot + 1,
                backup=backup_status,
            )
        )


# --------------------------------------------------------------------------- #
# Tab 4: 原始 NBT
# --------------------------------------------------------------------------- #
class NbtTab(_Tab):
    """任意 NBT 文件的路径读取/写入（高级）。"""

    title_key = "tab.nbt"

    def _build(self) -> None:
        self.file_presets: list[str] = []
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        file_frame = ttk.Frame(self)
        file_frame.grid(row=0, column=0, sticky="ew")
        file_frame.columnconfigure(1, weight=1)
        ttk.Label(file_frame, text=_label(tr("nbt.label.file"))).grid(
            row=0, column=0, sticky="w"
        )
        self.file_var = tk.StringVar()
        self.file_entry = ttk.Entry(file_frame, textvariable=self.file_var)
        self.file_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.browse_button = ttk.Button(
            file_frame, text=tr("button.browse"), command=self._ui_browse,
        )
        self.browse_button.grid(row=0, column=2)

        preset_frame = ttk.Frame(self)
        preset_frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        preset_frame.columnconfigure(1, weight=1)
        ttk.Label(preset_frame, text=_label(tr("nbt.label.preset"))).grid(
            row=0, column=0, sticky="w"
        )
        self.preset_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(
            preset_frame, textvariable=self.preset_var, state="readonly",
        )
        self.preset_combo.grid(row=0, column=1, sticky="ew")
        self.preset_combo.bind("<<ComboboxSelected>>", self._on_preset_selected)
        self.refresh_button = ttk.Button(
            preset_frame, text=tr("button.refresh_list"),
            command=lambda: self._run_ui(self.refresh),
        )
        self.refresh_button.grid(row=0, column=2, padx=(6, 0))

        path_frame = ttk.Frame(self)
        path_frame.grid(row=2, column=0, sticky="ew", pady=(6, 6))
        path_frame.columnconfigure(1, weight=1)
        ttk.Label(path_frame, text=_label(tr("nbt.label.path"))).grid(
            row=0, column=0, sticky="w"
        )
        self.path_var = self.nbt_path_var = tk.StringVar()
        self.path_entry = self.nbt_path_entry = ttk.Entry(path_frame, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        self.read_button = ttk.Button(path_frame, text=tr("button.read"), command=self._ui_read)
        self.read_button.grid(row=0, column=2)
        ttk.Label(
            path_frame, text=tr("nbt.hint.path"), style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))

        self.output = ScrolledText(
            self, height=14, wrap="word", undo=False, font=self.app.mono_font,
        )
        self.output.grid(row=3, column=0, sticky="nsew")
        _set_text(self.output, tr("nbt.output_initial"))

        write_box = ttk.LabelFrame(self, text=tr("nbt.write_box"), padding=8)
        write_box.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        write_box.columnconfigure(3, weight=1)
        ttk.Label(write_box, text=_label(tr("nbt.label.type"))).grid(
            row=0, column=0, sticky="w"
        )
        self.value_type_var = tk.StringVar(value="auto")
        self.value_type_combo = ttk.Combobox(
            write_box, textvariable=self.value_type_var, values=NBT_VALUE_TYPES,
            state="readonly", width=12,
        )
        self.value_type_combo.grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Label(write_box, text=_label(tr("nbt.label.value"))).grid(
            row=0, column=2, sticky="w"
        )
        self.value_var = self.value_text_var = tk.StringVar()
        self.value_entry = ttk.Entry(write_box, textvariable=self.value_var)
        self.value_entry.grid(row=0, column=3, sticky="ew")
        self.create_var = tk.BooleanVar(value=False)
        self.create_check = ttk.Checkbutton(
            write_box, text=tr("nbt.create_nodes"), variable=self.create_var,
        )
        self.create_check.grid(row=0, column=4, padx=(8, 0))
        self.write_button = ttk.Button(
            write_box, text=tr("button.write"), command=self._ui_write,
        )
        self.write_button.grid(row=0, column=5, padx=(8, 0))
        ttk.Label(
            write_box,
            text=tr("nbt.hint.write_effect"),
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=6, sticky="w", pady=(6, 0))

    # ---- 内部 ------------------------------------------------------------ #
    def _ui_browse(self) -> None:
        try:
            world = self.app.world
            initial = world.path if world is not None else os.path.expanduser("~")
            selected = filedialog.askopenfilename(
                parent=self,
                title=tr("nbt.file_dialog.title"),
                initialdir=initial,
                filetypes=[
                    (tr("nbt.file_dialog.nbt_type"), "*.dat"),
                    (tr("nbt.file_dialog.all_files"), "*.*"),
                ],
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
            self.app.show_error(tr("dialog.error.select_file.title"), exc)

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
            raise ValueError(tr("nbt.error.file_empty"))
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
                _set_text(self.output, tr("common.no_world"))

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
                raise NbtError(
                    tr(
                        "nbt.error.read_path",
                        path=repr(nbt_path),
                        error=_exc_text(exc),
                    )
                ) from exc
        else:
            value = tag
        text = to_snbt(value)
        _set_text(self.output, text)
        suffix = (
            tr("nbt.path_suffix", path=nbt_path) if nbt_path else tr("nbt.root_suffix")
        )
        self.app.set_status(tr("status.nbt_read", target=target, suffix=suffix))
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
            raise NbtError(tr("nbt.error.write_path_required"))
        raw_value = str(value_text or "").strip()
        if not raw_value:
            raise ValueError(tr("nbt.error.value_empty"))
        value_type = (str(value_type or "auto").strip().lower() or "auto")
        if value_type not in NBT_VALUE_TYPES:
            raise ValueError(
                tr(
                    "error.unknown_value_type",
                    value=repr(value_type),
                    choices=" / ".join(NBT_VALUE_TYPES),
                )
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
                raise ValueError(
                    tr("nbt.error.snbt_parse", error=_exc_text(exc))
                ) from exc
            value = coerce_typed(parsed, "auto", existing)
        else:
            value = coerce_typed(raw_value, value_type, None)

        try:
            nbt_set_path(tag, nbt_path, value, create=bool(create))
        except (KeyError, IndexError, NbtError) as exc:
            raise NbtError(
                tr(
                    "nbt.error.write_path",
                    path=repr(nbt_path),
                    error=_exc_text(exc),
                )
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
        suffix = tr("nbt.path_suffix", path=nbt_path) if nbt_path else ""
        self.app.show_backups(new_backups)
        self.app.set_status(
            tr("status.nbt_written", target=target, suffix=suffix, value=written)
        )


# --------------------------------------------------------------------------- #
# Tab 5: 校验 / 备份
# --------------------------------------------------------------------------- #
class VerifyTab(_Tab):
    """世界校验（浅层/深度）与整世界备份（后台线程）。"""

    title_key = "tab.verify"

    def _build(self) -> None:
        self.is_running = False
        self.last_report: Optional[dict] = None
        self._busy_kind = ""

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        button_bar = ttk.Frame(self)
        button_bar.grid(row=0, column=0, sticky="ew")
        self.shallow_button = ttk.Button(
            button_bar, text=tr("button.shallow_verify"),
            command=lambda: self._ui_start_verify(False),
        )
        self.shallow_button.pack(side="left")
        self.deep_button = ttk.Button(
            button_bar, text=tr("button.deep_verify"),
            command=lambda: self._ui_start_verify(True),
        )
        self.deep_button.pack(side="left", padx=(8, 0))
        self.backup_button = ttk.Button(
            button_bar, text=tr("button.backup_world"),
            command=lambda: self._ui_start_backup(),
        )
        self.backup_button.pack(side="left", padx=(8, 0))
        self.open_backup_button = ttk.Button(
            button_bar, text=tr("button.open_backup_dir"), command=self._ui_open_backup_dir,
        )
        self.open_backup_button.pack(side="left", padx=(8, 0))

        status_bar = ttk.Frame(self)
        status_bar.grid(row=1, column=0, sticky="ew", pady=(8, 6))
        status_bar.columnconfigure(1, weight=1)
        self.progress = ttk.Progressbar(
            status_bar, mode="determinate", length=220, maximum=100, value=0,
        )
        self.progress.grid(row=0, column=0, sticky="w")
        self.task_var = tk.StringVar(value=tr("status.idle"))
        self.task_label = ttk.Label(status_bar, textvariable=self.task_var, anchor="w")
        self.task_label.grid(row=0, column=1, sticky="w", padx=(10, 0))

        self.output = ScrolledText(
            self, height=18, wrap="word", undo=False, font=self.app.mono_font,
        )
        self.output.grid(row=2, column=0, sticky="nsew")
        _set_text(self.output, tr("verify.output_initial"))

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
            self.task_var.set(task or tr("verify.status.busy"))
        else:
            try:
                self.progress.stop()
                self.progress.configure(mode="determinate", value=0)
            except tk.TclError:
                pass
            self.task_var.set(tr("status.idle"))
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
            self.app.set_status(tr("status.opened_dir", path=target))
        except Exception as exc:  # noqa: BLE001
            self.app.show_error(tr("dialog.error.open_backup_dir.title"), exc)

    def _finish_verify(self, payload: Any, deep: bool) -> None:
        self._set_busy(False)
        if isinstance(payload, BaseException):
            self._set_output(
                tr("verify.status.failed_output", error=_exc_text(payload))
            )
            self.app.set_status(tr("verify.status.failed"))
            return
        self.last_report = payload
        self._set_output(self.format_report(payload))
        result = (
            tr("common.pass")
            if isinstance(payload, dict) and payload.get("ok")
            else tr("common.fail")
        )
        kind = tr("verify.kind.deep") if deep else tr("verify.kind.shallow")
        self.app.set_status(tr("verify.status.done", kind=kind, result=result))

    def _finish_backup(self, payload: Any) -> None:
        self._set_busy(False)
        if isinstance(payload, BaseException):
            self._set_output(
                tr("verify.status.backup_failed_output", error=_exc_text(payload))
            )
            self.app.set_status(tr("verify.status.backup_failed"))
            return
        backup_path = str(payload)
        self._set_output(tr("verify.output.backup_done", path=backup_path))
        self.app.show_backups([backup_path])
        self.app.set_status(tr("verify.status.backup_done", path=backup_path))

    # ---- 公开方法 -------------------------------------------------------- #
    def refresh(self) -> None:
        if self.is_running:
            return
        if self.app.world is None:
            self.task_var.set(tr("status.no_world"))
        else:
            self.task_var.set(tr("status.idle"))

    def start_verify(self, deep: bool = False) -> None:
        """在后台线程执行世界校验；完成后自动回到主线程更新界面。"""
        world = self.require_world()
        if self.is_running:
            raise RuntimeError(tr("error.background_running"))
        kind = tr("verify.kind.deep") if deep else tr("verify.kind.shallow")
        self._set_busy(
            True,
            tr("verify.status.deep_running")
            if deep
            else tr("verify.status.shallow_running"),
        )
        self._set_output(
            tr("verify.output.running", kind=kind, path=world.path)
        )
        self.app.set_status(tr("verify.status.running", kind=kind))
        self.app.run_background(
            lambda: world.verify(deep=deep),
            lambda payload: self._finish_verify(payload, deep),
            description="verify-deep" if deep else "verify-shallow",
        )

    def start_backup(self) -> None:
        """在后台线程执行整世界备份；完成后刷新界面。"""
        world = self.require_world()
        if self.is_running:
            raise RuntimeError(tr("error.background_running"))
        self._set_busy(True, tr("verify.status.backup_running"))
        self._set_output(tr("verify.output.backup_running", path=world.path))
        self.app.set_status(tr("verify.status.backup_running_status"))
        self.app.run_background(
            world.backup_world, self._finish_backup, description="backup-world",
        )

    def format_report(self, report: dict) -> str:
        """把 ``World.verify()`` 返回的报告格式化成当前语言文本。"""
        if not isinstance(report, dict):
            return tr("verify.report.invalid", report=repr(report))
        ok = bool(report.get("ok"))
        level = report.get("level") or {}
        lines: list[str] = []
        lines.append("=" * 68)
        lines.append(
            tr(
                "verify.report.result",
                result=tr("common.pass") if ok else tr("common.fail"),
            )
        )
        lines.append("=" * 68)
        lines.append(
            tr(
                "verify.report.level",
                data_version=level.get("data_version", "?"),
                level_name=repr(level.get("level_name", "?")),
                sections_checked=level.get("sections_checked", 0),
            )
        )
        dimensions = report.get("dimensions") or {}
        if dimensions:
            lines.append("")
            lines.append(tr("verify.report.dimensions"))
            for dim, stats in dimensions.items():
                stats = stats if isinstance(stats, dict) else {}
                lines.append(
                    tr(
                        "verify.report.dimension_line",
                        dim=dim,
                        regions=stats.get("regions", 0),
                        chunks=stats.get("chunks", 0),
                        errors=stats.get("errors", 0),
                    )
                )
        players = report.get("players") or {}
        lines.append("")
        lines.append(tr("verify.report.players", count=len(players)))
        for uuid, info in players.items():
            if isinstance(info, dict) and "error" in info:
                lines.append(
                    tr("verify.report.player_error", uuid=uuid, error=info["error"])
                )
            elif isinstance(info, dict):
                lines.append(
                    tr(
                        "verify.report.player_line",
                        uuid=uuid,
                        dim=info.get("dimension", "?"),
                        mode=info.get("game_type", "?"),
                        data_version=info.get("data_version", "?"),
                    )
                )

        warnings = report.get("warnings") or []
        errors = report.get("errors") or []
        if warnings:
            lines.append("")
            lines.append(tr("verify.report.warnings", count=len(warnings)))
            for warning in warnings[:200]:
                lines.append(f"  ! {warning}")
            if len(warnings) > 200:
                lines.append(
                    tr("verify.report.more_warnings", count=len(warnings) - 200)
                )
        if errors:
            lines.append("")
            lines.append(tr("verify.report.errors", count=len(errors)))
            for error in errors[:200]:
                lines.append(f"  x {error}")
            if len(errors) > 200:
                lines.append(
                    tr("verify.report.more_errors", count=len(errors) - 200)
                )
        if not warnings and not errors:
            lines.append("")
            lines.append(tr("verify.report.no_issues"))
        lines.append("")
        backup_root = (
            getattr(self.app.world, "backup_root", None)
            if self.app.world is not None
            else None
        )
        lines.append(
            tr(
                "verify.report.backup_dir",
                path=backup_root if backup_root else tr("status.no_world"),
            )
        )
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

        self.title(tr("version.dialog.title"))
        self.transient(parent)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(
            body,
            text=tr("version.dialog.detected", version=error.detected.display),
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w")
        supported_text = tr("common.list_separator").join(
            _version_choice_label(p) for p in error.supported
        )
        ttk.Label(
            body,
            text=tr("version.dialog.supported", versions=supported_text),
            wraplength=560,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            body,
            text=tr("version.dialog.choose", version=error.nearest.id),
            wraplength=560,
        ).pack(anchor="w", pady=(6, 0))

        values: list[str] = []
        for profile in error.supported:
            label = _version_choice_label(profile)
            self._labels[label] = profile.id
            values.append(label)
        self.choice_var = tk.StringVar(value=_version_choice_label(error.nearest))
        self.combo = ttk.Combobox(
            body, textvariable=self.choice_var, values=values, state="readonly", width=58,
        )
        self.combo.pack(fill="x", pady=(10, 0))

        ttk.Label(
            body,
            text=tr("version.dialog.warning"),
            style="Hint.TLabel",
            wraplength=560,
        ).pack(anchor="w", pady=(8, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(14, 0))
        ttk.Button(
            buttons, text=tr("button.use_version"), command=self._ok,
        ).pack(side="right")
        ttk.Button(
            buttons, text=tr("button.cancel"), command=self._cancel,
        ).pack(side="right", padx=(0, 8))

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
                    tr("status.unsupported_version", version=exc.detected.display)
                )
                self.lock_var.set(tr("status.unlocked"))
            except Exception as exc:  # noqa: BLE001 - 构造阶段不弹框，错误稍后展示
                self.startup_error = _exc_text(exc)
                self.set_status(
                    tr("status.open_world_failed", error=self.startup_error)
                )
                self.lock_var.set(tr("status.unlocked"))

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
        ttk.Label(toolbar, text=_label(tr("app.label.world_dir"))).grid(
            row=0, column=0, sticky="w"
        )
        self.world_path_var = tk.StringVar()
        self.world_path_entry = ttk.Entry(toolbar, textvariable=self.world_path_var)
        self.world_path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.world_path_entry.bind("<Return>", lambda _e: self._ui_open_typed())
        self.open_world_button = ttk.Button(
            toolbar, text=tr("button.open_world"), command=self._ui_open_world,
        )
        self.open_world_button.grid(row=0, column=2)
        self.open_typed_button = ttk.Button(
            toolbar, text=tr("button.open_path"), command=self._ui_open_typed,
        )
        self.open_typed_button.grid(row=0, column=3, padx=(6, 0))

        hint_bar = ttk.Frame(self, padding=(10, 0, 10, 2))
        hint_bar.grid(row=1, column=0, sticky="ew")
        hint_bar.columnconfigure(0, weight=1)
        ttk.Label(
            hint_bar,
            text=tr("app.hint.lock"),
            style="Hint.TLabel",
        ).grid(row=0, column=0, sticky="w")
        self.lock_var = tk.StringVar(value=tr("status.unlocked"))
        self.lock_label = ttk.Label(
            hint_bar, textvariable=self.lock_var, foreground="#b06a00", anchor="e",
        )
        self.lock_label.grid(row=0, column=1, sticky="e", padx=(12, 0))

        # ---- 中部标签页 -------------------------------------------------- #
        self.status_var = tk.StringVar(value=tr("status.ready"))
        self.backup_var = tk.StringVar(value=tr("status.backup_none"))

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
        for tab in self.tabs:
            self.notebook.add(tab, text=tab.title)
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
        file_menu.add_command(label=tr("button.open_world"), command=self._ui_open_world)
        file_menu.add_command(label=tr("button.open_path"), command=self._ui_open_typed)
        file_menu.add_command(label=tr("menu.close_world"), command=self._ui_close_world)
        file_menu.add_separator()
        file_menu.add_command(label=tr("menu.exit"), command=self.on_close)
        menubar.add_cascade(label=tr("menu.file"), menu=file_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label=tr("menu.about"), command=self._ui_about)
        menubar.add_cascade(label=tr("menu.help"), menu=help_menu)
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
                title=tr("app.dialog.choose_world_dir"),
                initialdir=initial,
                mustexist=True,
            )
        except Exception as exc:  # noqa: BLE001
            self.show_error(tr("dialog.error.select_world_dir.title"), exc)
            return
        if selected:
            self._ui_open_path(selected)

    def _ui_open_typed(self) -> None:
        path = self.world_path_var.get().strip().strip('"').strip("'")
        if not path:
            self.show_error(
                tr("dialog.error.open_world.title"),
                ValueError(tr("app.error.path_empty")),
            )
            return
        self._ui_open_path(path)

    def _ui_open_path(self, path: str) -> None:
        try:
            self.open_world(path)
        except UnsupportedVersionError as exc:
            choice = self._ask_version(exc)
            if not choice:
                self.set_status(
                    tr("status.open_cancelled", version=exc.detected.display)
                )
                self.lock_var.set(tr("status.unlocked"))
                return
            try:
                self.open_world(path, mc_version=choice)
            except Exception as exc2:  # noqa: BLE001 - UI 边界
                self.show_error(tr("dialog.error.open_world.title"), exc2)
                return
        except Exception as exc:  # noqa: BLE001 - UI 边界
            self.show_error(tr("dialog.error.open_world.title"), exc)
            return
        world = self.world
        if world is None:
            self.set_status(tr("status.open_world_failed_unknown"))
            return
        self.set_status(
            tr(
                "status.world_opened_detail",
                path=world.path,
                detected=world.detected_version.display,
                profile=world.version_profile.id,
            )
        )
        if world.version_warnings:
            try:
                messagebox.showwarning(
                    tr("dialog.warning.version.title"),
                    "\n".join(world.version_warnings),
                    parent=self,
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
            self.show_error(tr("dialog.error.close_world.title"), exc)
            return
        self.set_status(tr("status.world_closed"))

    def _ui_about(self) -> None:
        supported = tr("common.list_separator").join(
            tr(
                "world.info.selected_version_format",
                id=p.id,
                status=_version_status_label(p),
            )
            for p in SUPPORTED_VERSIONS.values()
        )
        messagebox.showinfo(
            tr("app.about.title"),
            tr(
                "app.about.body",
                app_title=APP_TITLE,
                supported=supported,
            ),
            parent=self,
        )

    def _show_backup_details(self, _event: "tk.Event | None" = None) -> None:
        if not self.last_backups:
            messagebox.showinfo(
                tr("dialog.info.backup.title"),
                tr("dialog.info.no_backup"),
                parent=self,
            )
            return
        messagebox.showinfo(
            tr("dialog.info.backup_paths.title"),
            tr("dialog.info.backup_paths.body") + "\n".join(self.last_backups),
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
            raise ValueError(tr("app.error.world_path_empty"))
        text = str(path).strip().strip('"').strip("'")
        if not text:
            raise ValueError(tr("app.error.world_path_empty"))

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
        self.lock_var.set(tr("status.locked"))
        self.last_backups = []
        self.backup_var.set(tr("status.backup_none"))
        self._refresh_all_tabs()
        self.set_status(tr("status.world_opened", path=world.path))
        return world.info()

    def close_world(self) -> None:
        """关闭当前世界并释放 session.lock。"""
        world = self.world
        self.world = None
        try:
            if world is not None:
                world.close()
        finally:
            self.lock_var.set(tr("status.unlocked"))
            self.last_backups = []
            self.backup_var.set(tr("status.backup_none"))
            self._refresh_all_tabs()

    def reload_level(self) -> None:
        """重新从磁盘读取 level.dat（用于原始 NBT 修改后刷新世界页）。"""
        world = self.world
        if world is None:
            raise RuntimeError(tr("app.error.no_world_reload"))
        try:
            world.reload_level()
        except Exception as exc:
            raise WorldError(
                tr("app.error.reload_level_failed", error=_exc_text(exc))
            ) from exc
        self.world_tab.refresh()
        self.set_status(tr("status.level_reloaded"))

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
            raise TypeError(tr("app.error.run_background_args"))

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
            self.backup_var.set(tr("status.backup_none"))
            return
        latest = items[-1]
        if len(latest) > 80:
            latest = "…" + latest[-79:]
        if len(items) == 1:
            self.backup_var.set(tr("status.backup_one", path=latest))
        else:
            self.backup_var.set(
                tr("status.backup_many", path=latest, count=len(items))
            )

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
        raise KeyError(tr("app.error.unknown_tab", key=repr(key)))

    def show_error(self, title: str, exc: BaseException) -> None:
        """显示错误对话框（仅在 UI 回调边界使用）。"""
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
                self.set_status(
                    tr("status.refresh_tab_failed", tab=tab.title, error=_exc_text(exc))
                )

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
    versions = tr("common.list_separator").join(SUPPORTED_VERSIONS.keys())
    parser = argparse.ArgumentParser(
        prog="python -m mctool.gui",
        description=tr("app.cli.description", versions=versions),
    )
    parser.add_argument(
        "--world", "-w", default=os.environ.get("MCTOOL_WORLD"),
        help=tr("app.cli.world_help"),
    )
    parser.add_argument(
        "--tab", "-t", type=int, default=0,
        help=tr("app.cli.tab_help"),
    )
    parser.add_argument(
        "--mc-version", default=None, metavar="VERSION",
        help=tr("app.cli.mc_version_help"),
    )
    parser.add_argument(
        "--self-test", action="store_true",
        help=tr("app.cli.self_test_help"),
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
        print(
            tr("app.error.tk_init", error=exc),
            file=sys.stderr,
        )
        return 1

    startup_ok = app.world is not None
    tab_count = len(app.tabs)
    if args.world and app.world is None and app.startup_version_error is not None:
        # 启动时自动打开失败（版本不受支持）：主循环启动后弹出“选择受支持版本”对话框
        app.after(150, lambda: app._ui_open_path(args.world))
    elif args.world and app.world is None and app.startup_error:
        try:
            messagebox.showerror(
                tr("dialog.error.open_world.title"), app.startup_error, parent=app,
            )
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
