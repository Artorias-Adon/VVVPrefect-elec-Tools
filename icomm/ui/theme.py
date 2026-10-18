"""界面外观：配色、字号、密度。

外观由 :class:`Appearance` 描述（一份调色板 + 字体 + 字号 + 密度），
保存进软件设置，启动时套用。

**默认配色是「天蓝」**（:data:`PRESETS` 里的「天蓝」，字段默认值就是它），
另配了「纯黑蓝调」/「纯白蓝调」（近黑底 / 白底 + 蓝强调色）；
老主题（深色工业 / 深蓝夜间 / 浅色办公 / 高对比现场）都还在，随时切回来。
预设名改过一轮（开源前把带第三方品牌的名字换成了中性名），
老 settings.json 里的旧名字由 :data:`PRESET_ALIASES` 兜住。

**跟随系统**：``Appearance.follow_system`` 为真时不看 ``palette``，而是按系统的
深色/浅色偏好挑一套预设（:data:`FOLLOW_SYSTEM_PRESETS`，深色→天蓝、浅色→浅色办公），
系统偏好一变（Qt 的 ``colorSchemeChanged``，见 :func:`watch_system_scheme`）就跟着换。
用户手选的那套仍然记在 :func:`preference` 里，关掉跟随立刻还回来。

**扩展方式**：新增一套预设主题 -> 在 :data:`PRESETS` 里加一个 :class:`Palette`；
新增一个可调项 -> 给 :class:`Palette` / :class:`Appearance` 加字段，
:func:`build_stylesheet` 里用上它，设置界面会自动出现对应编辑器。

为了不打扰调用方，本模块同时暴露一批**模块级颜色常量**
（``theme.TEXT`` / ``theme.ACCENT`` 等），:func:`apply` 会就地更新它们，
所以 ``from .. import theme`` + ``theme.TEXT`` 的老写法继续有效。
"""

from __future__ import annotations

import copy
import json
import os
import weakref
from dataclasses import asdict, dataclass, field, fields
from typing import Any

# --------------------------------------------------------------- 调色板


@dataclass
class Palette:
    """一套配色。字段按用途分组，设置里的「高级」页可以逐项改。

    字段默认值 = **天蓝**（就是 :data:`DEFAULT_PRESET` 那一套），所以
    没配过配色的新装直接就是这一套；老配色照旧按名字从 ``PRESETS`` 里取。
    因此 ``PRESETS`` 里每一套都**写全了所有字段**，不靠这里的默认值撑。
    """

    name: str = "天蓝"

    # ---- 背景层次（由深到浅）----
    bg: str = "#0b1220"
    bg_panel: str = "#111a2b"
    bg_card: str = "#16223a"
    bg_input: str = "#0a1018"
    border: str = "#24344f"

    # ---- 文字 ----
    text: str = "#e8f1ff"
    text_dim: str = "#93a7c4"

    # ---- 强调色 ----
    accent: str = "#38bdf8"
    accent_alt: str = "#0ea5e9"
    accent_hover: str = "#7dd3fc"

    # ---- 控件 ----
    button_hover: str = "#1b2941"
    button_pressed: str = "#0d1524"
    button_disabled_text: str = "#5c6f8c"
    button_disabled_border: str = "#1a2536"
    danger: str = "#f43f5e"
    danger_hover: str = "#fb7185"
    header_bg: str = "#0f1729"
    input_button_bg: str = "#0f1729"
    input_button_hover: str = "#1b2941"
    table_alt: str = "#101a2c"
    table_selection: str = "#1e4b73"
    scrollbar_handle: str = "#2b3d59"
    scrollbar_hover: str = "#3d547a"

    # ---- 质量戳 ----
    quality_good: str = "#22c55e"
    quality_bad: str = "#f43f5e"
    quality_stale: str = "#f59e0b"
    quality_uncertain: str = "#f59e0b"

    # ---- 报警级别 ----
    alarm_info: str = "#38bdf8"
    alarm_warning: str = "#f59e0b"
    alarm_critical: str = "#f43f5e"

    # ---- 曲线配色 ----
    series: tuple[str, ...] = (
        "#38bdf8",
        "#22d3ee",
        "#34d399",
        "#fbbf24",
        "#f472b6",
        "#a78bfa",
        "#fb923c",
        "#94a3b8",
    )

    # ---- 绘图区（示波器画布 / 示意图画布 / 指示灯）----
    # 这些是自绘区域的颜色，不能只靠样式表，所以单独列出来跟随主题。
    #: 画布背景（比主背景再深一点，好让曲线跳出来）
    canvas_bg: str = "#070d17"
    #: 网格细线 / 粗线
    grid_minor: str = "#121c2e"
    grid_major: str = "#1b2941"
    #: 示波器双光标
    cursor_a: str = "#f59e0b"
    cursor_b: str = "#7dd3fc"
    #: 示意图连线：交流 / 直流 / 信号
    wire_ac: str = "#f59e0b"
    wire_dc: str = "#38bdf8"
    wire_signal: str = "#64748b"
    #: 指示灯未点亮时的灰色
    lamp_off: str = "#243349"
    #: 示意图图元底板
    node_bg: str = "#16223a"

    # -------------------------------------------------- 存取

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["series"] = list(self.series)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Palette":
        base = cls()
        if not data:
            return base
        allowed = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key not in allowed:
                continue
            if key == "series":
                if isinstance(value, (list, tuple)) and value:
                    base.series = tuple(str(item) for item in value)
                continue
            if isinstance(value, str) and value:
                setattr(base, key, value)
        return base

    def copy(self) -> "Palette":
        return copy.deepcopy(self)

    # -------------------------------------------------- 校验

    def normalize(self, fallback: "Palette | None" = None) -> list[str]:
        """把不合法的颜色修回默认值，返回被修过哪些字段。

        手改设置文件（或导入调色板）可能写出 ``#GGG`` 这种值，
        直接拿去拼样式表会让整个界面变黑/透明，所以这里兜住。
        """
        fallback = fallback or type(self)()
        problems: list[str] = []
        for spec in fields(self):
            if spec.name == "series":
                kept = tuple(item for item in self.series if is_color(item))
                if len(kept) != len(self.series):
                    problems.append("series")
                    self.series = kept or fallback.series
                continue
            value = getattr(self, spec.name)
            if spec.name == "name":
                if not isinstance(value, str) or not value:
                    setattr(self, spec.name, fallback.name)
                    problems.append(spec.name)
                continue
            if not is_color(value):
                setattr(self, spec.name, getattr(fallback, spec.name))
                problems.append(spec.name)
        return problems


def is_color(value: Any) -> bool:
    """``#rgb`` / ``#rrggbb`` / ``#rrggbbaa`` 都算合法。"""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text.startswith("#"):
        return False
    body = text[1:]
    if len(body) not in (3, 4, 6, 8):
        return False
    return all(ch in "0123456789abcdefABCDEF" for ch in body)


def _as_bool(value: Any) -> bool:
    """配置里存成 ``true`` / ``"true"`` / ``1`` 都能认。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return False


#: 内置预设主题。加一套主题只要在这里加一项。
#:
#: 「纯黑蓝调」与「纯白蓝调」这两套的**锚点色**是照着常见的深色蓝调界面风格自己调的
#: （近黑底 ``#000``、正文 ``#f5f5f7``、次级文字 ``#adb2b8``、主按钮 ``#5686fe``、
#: 成功绿 ``#22c55e``、警示橙 ``#f97316``；浅色底 ``#fff``、正文 ``#1d1d1f``、
#: 次级文字 ``#81858c``、主按钮 ``#3964fe``）。与本项目之外的任何产品/公司**无关联**。
#: 面板层次、网格、连线等字段是在同一色系里补齐的 —— 否则界面分不出层次。
PRESETS: dict[str, Palette] = {
    # 默认主题。``Palette()`` 的字段默认值就是这一套，所以这里直接用它
    # （免得两处各写一遍会走样）
    "天蓝": Palette(),
    # 这两套改成显式写全：Palette() 默认值现在是天蓝了，
    # 不写全它就会被默认值带跑
    "纯黑蓝调": Palette(
        name="纯黑蓝调",
        bg="#000000",
        bg_panel="#141518",
        bg_card="#1c1e22",
        bg_input="#0a0b0d",
        border="#2e3136",
        text="#f5f5f7",
        text_dim="#adb2b8",
        accent="#5686fe",
        accent_alt="#4d6bfe",
        accent_hover="#7098ff",
        button_hover="#24262b",
        button_pressed="#111214",
        button_disabled_text="#5a5f68",
        button_disabled_border="#1f2126",
        danger="#ef4444",
        danger_hover="#f87171",
        header_bg="#1a1c20",
        input_button_bg="#1a1c20",
        input_button_hover="#262a30",
        table_alt="#131418",
        table_selection="#1f3a7a",
        scrollbar_handle="#33363c",
        scrollbar_hover="#454950",
        quality_good="#22c55e",
        quality_bad="#ef4444",
        quality_stale="#f97316",
        quality_uncertain="#f97316",
        alarm_info="#5686fe",
        alarm_warning="#f97316",
        alarm_critical="#ef4444",
        series=(
        "#5686fe",
        "#22c55e",
        "#f97316",
        "#ef4444",
        "#a78bfa",
        "#38bdf8",
        "#fbbf24",
        "#94a3b8",
    ),
        canvas_bg="#08090b",
        grid_minor="#17191d",
        grid_major="#24272c",
        cursor_a="#f97316",
        cursor_b="#82a7ff",
        wire_ac="#f97316",
        wire_dc="#5686fe",
        wire_signal="#6b7280",
        lamp_off="#2b2e33",
        node_bg="#1c1e22",
    ),
    "纯白蓝调": Palette(
        name="纯白蓝调",
        bg="#ffffff",
        bg_panel="#f7f8fa",
        bg_card="#ffffff",
        bg_input="#ffffff",
        border="#dcdfe5",
        text="#1d1d1f",
        text_dim="#81858c",
        accent="#3964fe",
        accent_alt="#4d6bfe",
        accent_hover="#2148e8",
        button_hover="#eef0f4",
        button_pressed="#e2e5ea",
        button_disabled_text="#b0b4bb",
        button_disabled_border="#e4e6ea",
        danger="#dc2626",
        danger_hover="#ef4444",
        header_bg="#f2f3f6",
        input_button_bg="#f2f3f6",
        input_button_hover="#e6e8ec",
        table_alt="#f8f9fb",
        table_selection="#dbe4ff",
        scrollbar_handle="#c7cbd2",
        scrollbar_hover="#aeb3bc",
        quality_good="#16a34a",
        quality_bad="#dc2626",
        quality_stale="#ea580c",
        quality_uncertain="#ea580c",
        alarm_info="#3964fe",
        alarm_warning="#ea580c",
        alarm_critical="#dc2626",
        series=(
            "#3964fe",
            "#16a34a",
            "#ea580c",
            "#dc2626",
            "#7c3aed",
            "#0284c7",
            "#d97706",
            "#64748b",
        ),
        canvas_bg="#ffffff",
        grid_minor="#eef0f4",
        grid_major="#dcdfe5",
        cursor_a="#ea580c",
        cursor_b="#3964fe",
        wire_ac="#d97706",
        wire_dc="#3964fe",
        wire_signal="#94a3b8",
        lamp_off="#c7cbd2",
        node_bg="#f7f8fa",
    ),
    "深色工业": Palette(
        name="深色工业",
        bg="#1b1f24",
        bg_panel="#22272e",
        bg_card="#2b3138",
        bg_input="#161a1f",
        border="#3a424b",
        text="#dfe6ee",
        text_dim="#8b97a5",
        accent="#2f9e6b",
        accent_alt="#3d8bfd",
        accent_hover="#37b37b",
        button_hover="#343b44",
        button_pressed="#1f242a",
        button_disabled_text="#5d6875",
        button_disabled_border="#2c323a",
        danger="#a33a36",
        danger_hover="#bd4742",
        header_bg="#2e353d",
        input_button_bg="#2e353d",
        input_button_hover="#3d454f",
        table_alt="#262c33",
        table_selection="#33475f",
        scrollbar_handle="#3f4750",
        scrollbar_hover="#4e5862",
        quality_good="#2f9e6b",
        quality_bad="#d9534f",
        quality_stale="#c9a227",
        quality_uncertain="#c9a227",
        alarm_info="#3d8bfd",
        alarm_warning="#c9a227",
        alarm_critical="#d9534f",
        series=(
            "#3d8bfd",
            "#2f9e6b",
            "#c9a227",
            "#d9534f",
            "#a86ede",
            "#3fb9c4",
            "#e08c3b",
            "#8fa3b8",
        ),
        canvas_bg="#12161a",
        grid_minor="#20272e",
        grid_major="#2c353d",
        cursor_a="#e0a33b",
        cursor_b="#3fb9c4",
        wire_ac="#d9a13b",
        wire_dc="#3d8bfd",
        wire_signal="#6f7b88",
        lamp_off="#39424b",
        node_bg="#2b3138",
    ),
    "深蓝夜间": Palette(
        name="深蓝夜间",
        bg="#141a24",
        bg_panel="#1a2230",
        bg_card="#222c3c",
        bg_input="#101620",
        border="#2f3c4f",
        text="#dce6f5",
        text_dim="#8496ad",
        accent="#3d8bfd",
        accent_alt="#4fc3f7",
        accent_hover="#4f97ff",
        button_hover="#2b3849",
        button_pressed="#161d28",
        button_disabled_text="#556478",
        button_disabled_border="#24303f",
        # 显式写出来：字段默认值已经是「天蓝」的那套红，不写就会跟着变
        danger="#a33a36",
        danger_hover="#bd4742",
        header_bg="#243043",
        input_button_bg="#243043",
        input_button_hover="#31435a",
        table_alt="#1d2635",
        table_selection="#2f4d75",
        scrollbar_handle="#35455c",
        scrollbar_hover="#445877",
        quality_good="#3ddc97",
        quality_bad="#ff6b6b",
        quality_stale="#ffd166",
        quality_uncertain="#ffd166",
        alarm_info="#4fc3f7",
        alarm_warning="#ffd166",
        alarm_critical="#ff6b6b",
        series=(
            "#4fc3f7", "#3ddc97", "#ffd166", "#ff6b6b",
            "#b98cff", "#5fd0d8", "#ffa94d", "#9aa9bd",
        ),
        canvas_bg="#0e141d",
        grid_minor="#1a2330",
        grid_major="#243043",
        cursor_a="#ffc857",
        cursor_b="#5fd0d8",
        wire_ac="#ffc857",
        wire_dc="#4fc3f7",
        wire_signal="#6b7d93",
        lamp_off="#39465a",
        node_bg="#222c3c",
    ),
    "浅色办公": Palette(
        name="浅色办公",
        bg="#f2f4f7",
        bg_panel="#fbfcfd",
        bg_card="#ffffff",
        bg_input="#ffffff",
        border="#c8d0d9",
        text="#1e242b",
        text_dim="#5d6875",
        accent="#1f8a5c",
        accent_alt="#1f6feb",
        accent_hover="#23996a",
        button_hover="#e8ecf1",
        button_pressed="#d8dee6",
        button_disabled_text="#a7b1bd",
        button_disabled_border="#dde3ea",
        danger="#c0392b",
        danger_hover="#d0483a",
        header_bg="#e8ecf1",
        input_button_bg="#eef1f5",
        input_button_hover="#dfe5ec",
        table_alt="#f5f7fa",
        table_selection="#cfe3ff",
        scrollbar_handle="#c2cbd6",
        scrollbar_hover="#a9b5c2",
        quality_good="#1f8a5c",
        quality_bad="#c0392b",
        quality_stale="#b8860b",
        quality_uncertain="#b8860b",
        alarm_info="#1f6feb",
        alarm_warning="#b8860b",
        alarm_critical="#c0392b",
        series=(
            "#1f6feb", "#1f8a5c", "#b8860b", "#c0392b",
            "#7d4fbf", "#0f8b99", "#c47a12", "#5d6875",
        ),
        canvas_bg="#e9edf2",
        grid_minor="#dde3ea",
        grid_major="#c8d0d9",
        cursor_a="#b8860b",
        cursor_b="#0f8b99",
        wire_ac="#a86f00",
        wire_dc="#1f6feb",
        wire_signal="#8b97a5",
        lamp_off="#c2cbd6",
        node_bg="#ffffff",
    ),
    "高对比现场": Palette(
        name="高对比现场",
        bg="#000000",
        bg_panel="#0a0a0a",
        bg_card="#141414",
        bg_input="#000000",
        border="#5a5a5a",
        text="#ffffff",
        text_dim="#c0c0c0",
        accent="#00e676",
        accent_alt="#00b0ff",
        accent_hover="#33eb91",
        button_hover="#262626",
        button_pressed="#0f0f0f",
        button_disabled_text="#7a7a7a",
        button_disabled_border="#3a3a3a",
        danger="#ff1744",
        danger_hover="#ff5252",
        header_bg="#1c1c1c",
        input_button_bg="#1c1c1c",
        input_button_hover="#2e2e2e",
        table_alt="#101010",
        table_selection="#004d40",
        scrollbar_handle="#4a4a4a",
        scrollbar_hover="#666666",
        quality_good="#00e676",
        quality_bad="#ff1744",
        quality_stale="#ffd600",
        quality_uncertain="#ffd600",
        alarm_info="#00b0ff",
        alarm_warning="#ffd600",
        alarm_critical="#ff1744",
        series=(
            "#00b0ff", "#00e676", "#ffd600", "#ff1744",
            "#d500f9", "#1de9b6", "#ff9100", "#bdbdbd",
        ),
        canvas_bg="#000000",
        grid_minor="#1a1a1a",
        grid_major="#333333",
        cursor_a="#ffd600",
        cursor_b="#1de9b6",
        wire_ac="#ffd600",
        wire_dc="#00b0ff",
        wire_signal="#9e9e9e",
        lamp_off="#4a4a4a",
        node_bg="#141414",
    ),
}

#: 默认主题名（新装 / 恢复默认都用它）。``Palette()`` 的字段默认值就是这一套。
DEFAULT_PRESET = "天蓝"

#: 改过名的预设主题：老工程/老 settings.json 里存的是旧名字，这里映射到新名字，
#: 免得升级以后「设置 → 界面外观」里选中项是空的（配色本身完全没变）。
#: 曾经用过别的产品名当预设名，开源前统一改成了中性名。
PRESET_ALIASES: dict[str, str] = {
    "DeepSeek 深色": "纯黑蓝调",
    "DeepSeek 浅色": "纯白蓝调",
}


def resolve_preset(name: str) -> str:
    """把（可能是旧名字的）预设名收敛到 :data:`PRESETS` 里现存的键。"""
    if name in PRESETS:
        return name
    return PRESET_ALIASES.get(name, name)


# --------------------------------------------------------------- 跟随系统
# 「跟随系统」不是又一套配色，而是"看系统的深色/浅色偏好，挑对应的那套预设"：
# 深色系统用天蓝（默认那套），浅色系统用浅色办公。用户手选的配色仍然记着，
# 一旦关掉跟随就原样还回来。

#: 系统深色 / 浅色时各用哪套预设（想换配对只改这里）
FOLLOW_SYSTEM_PRESETS: dict[str, str] = {"dark": "天蓝", "light": "浅色办公"}

#: 系统偏好的两种取值
SCHEME_DARK = "dark"
SCHEME_LIGHT = "light"


def _qt_color_scheme() -> str | None:
    """问 Qt（6.5+ 的 ``QStyleHints.colorScheme``）系统偏好；拿不到给 None。"""
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
    except ImportError:  # pragma: no cover - 没装 Qt 的场景（纯逻辑测试）用不上
        return None
    app = QGuiApplication.instance()
    if app is None:
        return None
    hints = app.styleHints()
    reader = getattr(hints, "colorScheme", None)
    if reader is None:  # pragma: no cover - Qt < 6.5
        return None
    scheme = reader()
    if scheme == Qt.ColorScheme.Dark:
        return SCHEME_DARK
    if scheme == Qt.ColorScheme.Light:
        return SCHEME_LIGHT
    return None


def _windows_color_scheme() -> str | None:
    """Qt 说"不知道"时读注册表兜底（Windows 的"应用使用浅色主题"开关）。"""
    if os.name != "nt":  # pragma: no cover - 只在 Windows 上有意义
        return None
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return None
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            light, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
    except OSError:
        return None
    return SCHEME_LIGHT if int(light) else SCHEME_DARK


def system_color_scheme() -> str:
    """系统的深色/浅色偏好：``"dark"`` 或 ``"light"``（判断不出来就当深色）。"""
    return _qt_color_scheme() or _windows_color_scheme() or SCHEME_DARK


def follow_system_preset(scheme: str | None = None) -> str:
    """跟随系统时该用哪套预设。"""
    chosen = (scheme or system_color_scheme()).lower()
    name = FOLLOW_SYSTEM_PRESETS.get(chosen, FOLLOW_SYSTEM_PRESETS[SCHEME_DARK])
    return resolve_preset(name)


def effective_appearance(source: Appearance) -> Appearance:
    """把"用户选的"换算成"真正生效的"：跟随时换成按系统偏好挑的那套配色。"""
    if not source.follow_system:
        return source
    preset = PRESETS.get(follow_system_preset())
    if preset is None:  # pragma: no cover - 除非把映射写错
        return source
    return Appearance(
        palette=preset.copy(),
        font_family=source.font_family,
        font_size=source.font_size,
        density=source.density,
        follow_system=True,
    )


#: 界面密度 -> 内边距缩放系数
DENSITIES: dict[str, float] = {
    "紧凑": 0.75,
    "标准": 1.0,
    "宽松": 1.3,
}

FONT_SIZE_MIN = 10
FONT_SIZE_MAX = 20

#: 字体候选（留几个常用的，免得手写字体名写错）
FONT_FAMILIES: tuple[str, ...] = (
    '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif',
    '"Microsoft YaHei UI", "Microsoft YaHei", sans-serif',
    '"Segoe UI", "Microsoft YaHei UI", sans-serif',
    'Consolas, "Microsoft YaHei UI", monospace',
)


@dataclass
class Appearance:
    """完整的外观设置。"""

    palette: Palette = field(default_factory=Palette)
    font_family: str = FONT_FAMILIES[0]
    font_size: int = 13
    density: str = "标准"
    #: 跟随系统的深色/浅色偏好：为真时实际生效的配色由 :func:`follow_system_preset` 决定，
    #: 而 ``palette`` 仍保留用户手选的那一套（关掉跟随就还回来）。
    follow_system: bool = False

    def copy(self) -> "Appearance":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "palette": self.palette.to_dict(),
            "font_family": self.font_family,
            "font_size": self.font_size,
            "density": self.density,
            "follow_system": self.follow_system,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Appearance":
        if not data:
            return cls()
        appearance = cls()
        appearance.palette = Palette.from_dict(data.get("palette"))
        family = data.get("font_family")
        if isinstance(family, str) and family:
            appearance.font_family = family
        try:
            size = int(data.get("font_size", appearance.font_size))
        except (TypeError, ValueError):
            size = appearance.font_size
        appearance.font_size = min(FONT_SIZE_MAX, max(FONT_SIZE_MIN, size))
        density = data.get("density")
        appearance.density = density if density in DENSITIES else "标准"
        appearance.follow_system = _as_bool(data.get("follow_system"))
        return appearance

    @property
    def scale(self) -> float:
        return DENSITIES.get(self.density, 1.0)

    def px(self, base: int) -> int:
        """按密度缩放一个内边距值（至少留 1px）。"""
        return max(1, int(round(base * self.scale)))


# --------------------------------------------------------------- 样式表

_TEMPLATE = """
* {{ font-family: {font_family}; }}
QWidget {{
    background: {bg};
    color: {text};
    font-size: {font_size}px;
}}
QMainWindow::separator {{ background: {border}; }}

QToolBar {{
    background: {bg_panel};
    border: none;
    border-bottom: 1px solid {border};
    padding: {toolbar_pad}px;
    spacing: {toolbar_spacing}px;
}}
/* 菜单栏就是最上面那一行：菜单在左，采集启停与运行状态在右（见 MainWindow._build_menus） */
QMenuBar#mainMenuBar {{
    background: {bg_panel};
    border: none;
    border-bottom: 1px solid {border};
    padding: {toolbar_pad}px 0 {toolbar_pad}px {toolbar_pad}px;
    spacing: {toolbar_spacing}px;
}}
QMenuBar#mainMenuBar::item {{
    background: transparent;
    color: {text};
    padding: 4px 10px;
    border-radius: 4px;
}}
QMenuBar#mainMenuBar::item:selected {{ background: {button_hover}; }}
QMenuBar#mainMenuBar::item:pressed {{ background: {button_pressed}; }}
QWidget#runControls QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 4px 9px;
    color: {text};
}}
QWidget#runControls QToolButton:hover {{
    background: {button_hover};
    border-color: {border};
}}
QWidget#runControls QToolButton:pressed {{
    background: {button_pressed};
    border-color: {accent};
}}
QWidget#runControls QToolButton:checked {{
    background: {accent};
    border-color: {accent};
    color: #ffffff;
}}
QWidget#runControls QToolButton:disabled {{ color: {button_disabled_text}; }}
QWidget#runControls QLabel {{ background: transparent; }}
QLabel#appTitle {{
    font-size: {title_size}px;
    font-weight: 600;
    color: {text};
    padding-left: {title_pad}px;
}}

QListWidget#nav {{
    background: {bg_panel};
    border: none;
    border-right: 1px solid {border};
    outline: none;
    padding-top: {nav_top}px;
}}
QListWidget#nav::item {{
    padding: {nav_item_v}px {nav_item_h}px;
    color: {text_dim};
    border-left: 3px solid transparent;
}}
QListWidget#nav::item:selected {{
    background: {bg_card};
    color: {text};
    border-left: 3px solid {accent};
}}
QListWidget#nav::item:hover {{ color: {text}; }}

QPushButton {{
    background: {bg_card};
    border: 1px solid {border};
    border-radius: 4px;
    padding: {btn_v}px {btn_h}px;
    color: {text};
}}
QPushButton:hover {{ background: {button_hover}; }}
QPushButton:pressed {{ background: {button_pressed}; border-color: {accent}; }}
QPushButton:disabled {{ color: {button_disabled_text}; border-color: {button_disabled_border}; }}
QPushButton#primary {{
    background: {accent};
    border-color: {accent};
    color: #ffffff;
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {accent_hover}; }}
QPushButton#primary:pressed {{ background: {accent_alt}; border-color: {text}; }}
QPushButton#danger {{
    background: {danger};
    border-color: {danger};
    color: #ffffff;
}}
QPushButton#danger:hover {{ background: {danger_hover}; }}
QPushButton#danger:pressed {{ background: {danger}; border-color: {text}; }}
/* 小窗口控制面板上的按键：描边突出，方便盲操 */
QPushButton#controlKey {{
    background: {bg_input};
    border: 1px solid {accent};
    color: {text};
    font-weight: 600;
    padding: {key_v}px {key_h}px;
}}
QPushButton#controlKey:hover {{ background: {accent}; color: #ffffff; }}
QPushButton#controlKey:pressed {{
    background: {accent_alt};
    border-color: {text};
    color: #ffffff;
}}
QPushButton#controlKey:disabled {{ border-color: {button_disabled_border}; color: {button_disabled_text}; }}

QGroupBox {{
    border: 1px solid {border};
    border-radius: 6px;
    margin-top: {group_margin}px;
    padding: {group_pad}px;
    background: {bg_panel};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {text_dim};
}}

QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background: {bg_input};
    border: 1px solid {border};
    border-radius: 4px;
    padding: {input_v}px {input_h}px;
    selection-background-color: {accent};
}}
/* 数字框右侧有上下调节按钮，文本区要让开宽度，否则数字会被按钮压住 */
QSpinBox, QDoubleSpinBox {{ padding-right: 24px; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {accent_alt};
}}
QComboBox QAbstractItemView {{
    background: {bg_card};
    border: 1px solid {border};
    selection-background-color: {accent};
}}

/* 数字框的上下调节按钮：默认样式在深色主题下会渲染成怪异的圆圈，这里显式画三角箭头 */
QSpinBox::up-button, QDoubleSpinBox::up-button {{
    subcontrol-origin: border;
    subcontrol-position: top right;
    width: 17px;
    margin: 1px 1px 0 0;
    border-left: 1px solid {border};
    border-top-right-radius: 3px;
    background: {input_button_bg};
}}
QSpinBox::down-button, QDoubleSpinBox::down-button {{
    subcontrol-origin: border;
    subcontrol-position: bottom right;
    width: 17px;
    margin: 0 1px 1px 0;
    border-left: 1px solid {border};
    border-bottom-right-radius: 3px;
    background: {input_button_bg};
}}
QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {input_button_hover};
}}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: none;
    width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-bottom: 5px solid {text};
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: none;
    width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {text};
}}

QTableWidget, QTableView {{
    background: {bg_panel};
    alternate-background-color: {table_alt};
    gridline-color: {border};
    border: 1px solid {border};
    border-radius: 4px;
    selection-background-color: {table_selection};
}}
QHeaderView::section {{
    background: {header_bg};
    color: {text_dim};
    border: none;
    border-right: 1px solid {border};
    border-bottom: 1px solid {border};
    padding: {header_v}px {header_h}px;
}}
QTableCornerButton::section {{ background: {header_bg}; border: none; }}

QTabWidget::pane {{ border: 1px solid {border}; border-radius: 4px; }}
QTabBar::tab {{
    background: {bg_panel};
    color: {text_dim};
    padding: {tab_v}px {tab_h}px;
    border: 1px solid {border};
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
}}
QTabBar::tab:selected {{ background: {bg_card}; color: {text}; }}

QStatusBar {{ background: {bg_panel}; border-top: 1px solid {border}; color: {text_dim}; }}
QStatusBar::item {{ border: none; }}

QScrollArea {{ border: none; }}
QSplitter::handle {{ background: {border}; }}

QScrollBar:vertical {{ background: {bg}; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {scrollbar_handle}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {scrollbar_hover}; }}
QScrollBar:horizontal {{ background: {bg}; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: {scrollbar_handle}; border-radius: 5px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {border};
    border-radius: 3px;
    background: {bg_input};
}}
QCheckBox::indicator:checked {{ background: {accent}; border-color: {accent}; }}
/* 单选钮：方框是勾选框的样子，圆点才是单选钮 —— 早先只写了勾选框的 :checked，
   结果"已选中"的单选钮和未选中的长得一模一样（电价单一/分时那种二选一根本看不出选了哪个） */
QRadioButton::indicator {{ border-radius: 7px; }}
QRadioButton::indicator:checked {{
    border: 4px solid {accent};
    background: {bg_input};
}}

QToolTip {{
    background: {bg_card};
    color: {text};
    border: 1px solid {accent};
    padding: 4px 6px;
}}
QFrame#controlPanel {{
    background: {bg_panel};
    border: 1px solid {accent};
    border-radius: 8px;
}}
"""


def build_stylesheet(appearance: Appearance) -> str:
    """按外观设置生成全局样式表。"""
    values = appearance.palette.to_dict()
    values.pop("name", None)
    return _TEMPLATE.format(
        font_family=appearance.font_family,
        font_size=appearance.font_size,
        title_size=appearance.font_size + 3,
        # 内边距按密度缩放
        toolbar_pad=appearance.px(6),
        toolbar_spacing=appearance.px(6),
        title_pad=appearance.px(8),
        nav_top=appearance.px(8),
        nav_item_v=appearance.px(11),
        nav_item_h=appearance.px(16),
        btn_v=appearance.px(6),
        btn_h=appearance.px(14),
        key_v=appearance.px(10),
        key_h=appearance.px(16),
        group_margin=appearance.px(14),
        group_pad=appearance.px(10),
        input_v=appearance.px(4),
        input_h=appearance.px(8),
        header_v=appearance.px(6),
        header_h=appearance.px(8),
        tab_v=appearance.px(7),
        tab_h=appearance.px(16),
        **values,
    )


# --------------------------------------------------------------- 活动外观
# 下面是模块级常量，apply() 会就地更新，兼容 `theme.TEXT` 的老写法。

_appearance = Appearance()
#: 用户选的那份（可能开着"跟随系统"）；``_appearance`` 是按系统偏好换算后真正生效的
_preference = Appearance()

TEXT = ""
TEXT_DIM = ""
BG = ""
BG_PANEL = ""
BG_CARD = ""
BG_INPUT = ""
BORDER = ""
ACCENT = ""
ACCENT_ALT = ""
QUALITY_GOOD = ""
QUALITY_BAD = ""
QUALITY_STALE = ""
QUALITY_UNCERTAIN = ""
ALARM_LEVELS: dict[str, str] = {}
SERIES_COLORS: tuple[str, ...] = ()
#: 绘图区颜色，供自绘画布使用（跟随主题）
CANVAS_BG = ""
GRID_MINOR = ""
GRID_MAJOR = ""
CURSOR_A = ""
CURSOR_B = ""
WIRE_AC = ""
WIRE_DC = ""
WIRE_SIGNAL = ""
LAMP_OFF = ""
NODE_BG = ""
STYLESHEET = ""


def _refresh_globals() -> None:
    """把活动外观里的颜色同步到模块级常量。"""
    global TEXT, TEXT_DIM, BG, BG_PANEL, BG_CARD, BG_INPUT, BORDER
    global ACCENT, ACCENT_ALT, STYLESHEET, SERIES_COLORS
    global QUALITY_GOOD, QUALITY_BAD, QUALITY_STALE, QUALITY_UNCERTAIN
    global CANVAS_BG, GRID_MINOR, GRID_MAJOR, CURSOR_A, CURSOR_B
    global WIRE_AC, WIRE_DC, WIRE_SIGNAL, LAMP_OFF, NODE_BG

    palette = _appearance.palette
    TEXT = palette.text
    TEXT_DIM = palette.text_dim
    BG = palette.bg
    BG_PANEL = palette.bg_panel
    BG_CARD = palette.bg_card
    BG_INPUT = palette.bg_input
    BORDER = palette.border
    ACCENT = palette.accent
    ACCENT_ALT = palette.accent_alt
    QUALITY_GOOD = palette.quality_good
    QUALITY_BAD = palette.quality_bad
    QUALITY_STALE = palette.quality_stale
    QUALITY_UNCERTAIN = palette.quality_uncertain
    SERIES_COLORS = palette.series
    CANVAS_BG = palette.canvas_bg
    GRID_MINOR = palette.grid_minor
    GRID_MAJOR = palette.grid_major
    CURSOR_A = palette.cursor_a
    CURSOR_B = palette.cursor_b
    WIRE_AC = palette.wire_ac
    WIRE_DC = palette.wire_dc
    WIRE_SIGNAL = palette.wire_signal
    LAMP_OFF = palette.lamp_off
    NODE_BG = palette.node_bg
    ALARM_LEVELS.clear()
    ALARM_LEVELS.update(
        {
            "info": palette.alarm_info,
            "warning": palette.alarm_warning,
            "critical": palette.alarm_critical,
        }
    )
    STYLESHEET = build_stylesheet(_appearance)


def appearance() -> Appearance:
    """当前**生效**的外观（只读用途；要改请用 :func:`apply`）。"""
    return _appearance


def preference() -> Appearance:
    """用户选的外观（含"跟随系统"开关与手选的那套配色）。"""
    return _preference


def apply(new_appearance: Appearance, strict: bool = False) -> list[str]:
    """套用外观，返回被修正过的字段名。

    ``strict`` 为真时遇到非法颜色直接抛异常（导入调色板时想要明确报错），
    否则静默修回默认值 —— 保证界面不会因为一个坏颜色而整片变黑。

    开了"跟随系统"时，真正生效的配色按系统深色/浅色偏好挑（见
    :func:`effective_appearance`），而 ``new_appearance.palette`` 里手选的那套仍然记着。
    """
    global _appearance, _preference
    palette = new_appearance.palette.copy()
    problems = palette.normalize()
    if problems and strict:
        raise ValueError(f"配色里有非法值：{', '.join(problems)}")
    new_appearance.palette = palette
    _preference = new_appearance
    _appearance = effective_appearance(new_appearance)
    _refresh_globals()
    return problems


def apply_follow_system(enabled: bool = True) -> list[str]:
    """开/关"跟随系统"，其余外观项（字体/字号/密度/手选配色）保持不变。"""
    current = _preference
    updated = Appearance(
        palette=current.palette.copy(),
        font_family=current.font_family,
        font_size=current.font_size,
        density=current.density,
        follow_system=bool(enabled),
    )
    return apply(updated)


def refresh_system_scheme() -> bool:
    """系统深色/浅色变了：跟着换配色，返回配色是否真的变过。"""
    if not _preference.follow_system:
        return False
    before = _appearance.palette.name
    apply(_preference)
    return _appearance.palette.name != before


def apply_preset(name: str) -> list[str]:
    """按名字套用预设主题，保留字号/密度/字体等其它设置。

    老名字（改名前存进 settings.json 的）也能用，见 :func:`resolve_preset`。
    手选预设等于不再跟随系统。
    """
    preset = PRESETS.get(resolve_preset(name))
    if preset is None:
        raise KeyError(f"没有预设主题「{name}」")
    updated = Appearance(
        palette=preset.copy(),
        font_family=_preference.font_family,
        font_size=_preference.font_size,
        density=_preference.density,
        follow_system=False,
    )
    return apply(updated)


# --------------------------------------------------------------- 系统偏好监听


_system_watchers: list[Any] = []
_scheme_watch_connected = False


def on_system_scheme_changed() -> bool:
    """系统的深色/浅色偏好变了（Qt 的信号最终走到这里）。

    返回是否真的重套了配色；只有开着"跟随系统"才做事。
    """
    if not refresh_system_scheme():
        return False
    alive = []
    for item in list(_system_watchers):
        callback = item() if isinstance(item, weakref.ref) else item
        if callback is None:  # 窗口已经销毁，顺手清掉
            continue
        alive.append(item)
        try:
            callback()
        except RuntimeError:  # 底层控件已释放
            continue
        except Exception:  # noqa: BLE001 - 一个回调出错不该影响界面重绘
            pass
    _system_watchers[:] = alive
    return True


def watch_system_scheme(callback=None) -> bool:
    """挂上"系统深色/浅色变了"的监听（只连一次），返回是否已连上。

    Qt 拿不到这个信号（Qt < 6.5 / 没有 GUI）时返回 False，此时"跟随系统"
    仍然按启动时读到的那一次偏好生效。

    外部传进来的回调按**弱引用**存：主窗口关了不该被这条全局监听拖着不放。
    """
    global _scheme_watch_connected
    if callback is not None and _watch_ref(callback) not in _system_watchers:
        _system_watchers.append(_watch_ref(callback))
    if _scheme_watch_connected:
        return True
    try:
        from PySide6.QtGui import QGuiApplication
    except ImportError:  # pragma: no cover
        return False
    app = QGuiApplication.instance()
    if app is None:
        return False
    hints = app.styleHints()
    signal = getattr(hints, "colorSchemeChanged", None)
    if signal is None:  # pragma: no cover - Qt < 6.5
        return False
    signal.connect(lambda _scheme: on_system_scheme_changed())
    _scheme_watch_connected = True
    return True


def _watch_ref(callback):
    """绑定方法用弱引用（窗口销毁后自动失效），普通函数/可调用对象原样存。"""
    try:
        return weakref.WeakMethod(callback)
    except TypeError:
        return callback


def unwatch_system_scheme(callback=None) -> None:
    """摘掉回调（放过 Qt 信号不管；测试里清干净用）。"""
    if callback is None:
        _system_watchers.clear()
        return
    target = _watch_ref(callback)
    for item in list(_system_watchers):
        if item is callback or item == target:
            _system_watchers.remove(item)


def preset_names() -> list[str]:
    return list(PRESETS)


def series_color(index: int) -> str:
    """第 index 条曲线的颜色（超出配色表就循环取）。"""
    colors = _appearance.palette.series or Palette().series
    return colors[index % len(colors)]


# --------------------------------------------------------------- 配色的导入导出

#: 配色文件的格式标记与版本（以后加字段时好兼容，也方便识别"这不是配色文件"）
THEME_FILE_FORMAT = "vvphmi-theme"
THEME_FILE_VERSION = 1


def theme_to_dict(source: Appearance | None = None) -> dict[str, Any]:
    """把一套外观打包成可写文件的字典（带格式标记）。"""
    appearance_obj = source or _appearance
    return {
        "format": THEME_FILE_FORMAT,
        "version": THEME_FILE_VERSION,
        "appearance": appearance_obj.to_dict(),
    }


def theme_to_text(source: Appearance | None = None) -> str:
    """配色文件的文本内容（人也能看懂，方便手改两个颜色再发给同事）。"""
    return json.dumps(theme_to_dict(source), ensure_ascii=False, indent=2) + "\n"


def save_theme(path: str, source: Appearance | None = None) -> str:
    """把当前（或指定的）外观存成文件，返回实际写出的路径。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(theme_to_text(source))
    return path


def parse_theme_text(text: str) -> tuple[Appearance, list[str]]:
    """解析配色文件内容，返回 ``(外观, 警告列表)``。

    宽容处理：多出来的颜色项忽略、少了的用默认值补、非法颜色报出来但不致命 ——
    配色文件是拿去手改和互相发的，格式太严反而没法用。
    """
    warnings: list[str] = []
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"不是合法的 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("配色文件的顶层应该是一个对象")

    marker = data.get("format")
    if marker not in (None, THEME_FILE_FORMAT):
        warnings.append(f"这份文件的格式标记是「{marker}」，可能不是本软件的配色文件，仍按配色解析")
    body = data.get("appearance") if isinstance(data.get("appearance"), dict) else data

    palette_data = body.get("palette") if isinstance(body.get("palette"), dict) else {}
    defaults = Palette()
    known = set(defaults.to_dict())
    unknown = [key for key in palette_data if key not in known]
    if unknown:
        warnings.append("忽略了不认识的配色项：" + "、".join(unknown[:8]))

    cleaned: dict[str, Any] = {}
    valid_colour = 0
    for key, value in palette_data.items():
        if key not in known:
            continue
        if key == "name":
            if isinstance(value, str) and value:
                cleaned[key] = value
            continue
        if key == "series":
            if isinstance(value, list):
                colors = [item for item in value if is_color(item)]
                if len(colors) != len(value):
                    warnings.append("曲线颜色里有不是颜色的值，已丢掉")
                if colors:
                    cleaned[key] = colors
            continue
        if is_color(value):
            cleaned[key] = value
            valid_colour += 1
        else:
            warnings.append(f"配色项「{key}」的值「{value}」不是颜色，已按默认值处理")

    appearance_obj = Appearance.from_dict(body)
    appearance_obj.palette = Palette.from_dict({**defaults.to_dict(), **cleaned})
    if not marker and not valid_colour and not palette_data:
        warnings.append("文件里没有找到配色内容，已保持默认配色")
    return appearance_obj, warnings


def load_theme_file(path: str) -> tuple[Appearance, list[str]]:
    """从文件读一套外观（读不了就抛 ValueError，界面直接显示原因）。"""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        raise ValueError(f"读不了这个文件：{exc}") from exc
    return parse_theme_text(text)


_refresh_globals()
