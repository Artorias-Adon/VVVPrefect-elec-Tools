"""外观编辑器：配色主题、字体、字号、密度，以及逐项自定义颜色。

界面自定义分两级：

* **常用**：选预设主题、改强调色、字号、密度、字体 —— 覆盖绝大多数需求；
* **高级**：逐项自定义 :class:`~icomm.ui.theme.Palette` 里的每一个颜色，
  每个颜色旁边就有色块预览，改错了可以据此判断。

改动**实时预览**（直接套用到正在运行的程序）；按「取消」会把进入时的外观还原，
所以用户可以放心乱调。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..theme import (
    DENSITIES,
    FONT_SIZE_MAX,
    FONT_SIZE_MIN,
    Appearance,
    Palette,
)

#: 高级页里颜色字段的显示名与分组（顺序即展示顺序）
COLOR_GROUPS: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "背景层次",
        [
            ("bg", "主背景"),
            ("bg_panel", "面板"),
            ("bg_card", "卡片/按钮"),
            ("bg_input", "输入框"),
            ("border", "边框"),
        ],
    ),
    ("文字", [("text", "正文"), ("text_dim", "次要文字")]),
    (
        "强调色",
        [("accent", "主强调"), ("accent_alt", "次强调"), ("accent_hover", "强调悬停")],
    ),
    (
        "控件",
        [
            ("button_hover", "按钮悬停"),
            ("button_pressed", "按钮按下"),
            ("button_disabled_text", "禁用文字"),
            ("button_disabled_border", "禁用边框"),
            ("danger", "危险操作"),
            ("danger_hover", "危险悬停"),
            ("header_bg", "表头"),
            ("input_button_bg", "数字框按钮"),
            ("input_button_hover", "数字框按钮悬停"),
            ("table_alt", "表格隔行"),
            ("table_selection", "表格选中"),
            ("scrollbar_handle", "滚动条"),
            ("scrollbar_hover", "滚动条悬停"),
        ],
    ),
    (
        "质量戳",
        [
            ("quality_good", "正常"),
            ("quality_bad", "异常"),
            ("quality_stale", "陈旧"),
            ("quality_uncertain", "不确定"),
        ],
    ),
    (
        "报警级别",
        [("alarm_info", "提示"), ("alarm_warning", "告警"), ("alarm_critical", "故障")],
    ),
    (
        "绘图区",
        [
            ("canvas_bg", "画布背景"),
            ("grid_minor", "网格细线"),
            ("grid_major", "网格粗线"),
            ("cursor_a", "光标 A"),
            ("cursor_b", "光标 B"),
            ("wire_ac", "交流连线"),
            ("wire_dc", "直流连线"),
            ("wire_signal", "信号连线"),
            ("node_bg", "图元底板"),
            ("lamp_off", "指示灯灭"),
        ],
    ),
]


class ColorButton(QPushButton):
    """一个带色块的颜色选择按钮。"""

    colorChanged = Signal(str)

    def __init__(self, field: str, colour: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.field = field
        self._colour = colour
        self.setFixedWidth(84)
        self.setToolTip(f"{field}　点击选择颜色")
        self.clicked.connect(self._pick)
        self._paint()

    @property
    def colour(self) -> str:
        return self._colour

    def set_colour(self, colour: str) -> None:
        self._colour = colour
        self._paint()

    def _paint(self) -> None:
        # 色块 + 色值文字；浅色文字用于深底色，反之亦然
        self.setText(self._colour.upper())
        text_colour = _readable_on(self._colour)
        self.setStyleSheet(
            f"QPushButton {{ background: {self._colour}; color: {text_colour}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 4px; "
            f"padding: 4px 6px; font-size: 11px; }}"
        )

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(
            _to_qcolor(self._colour), self, f"选择颜色 · {self.field}"
        )
        if chosen.isValid():
            self.set_colour(chosen.name())
            self.colorChanged.emit(self._colour)


def _to_qcolor(colour: str):
    from PySide6.QtGui import QColor

    color = QColor(colour)
    return color if color.isValid() else QColor("#808080")


def _readable_on(colour: str) -> str:
    """在给定底色上挑一个看得清的文字色。"""
    color = _to_qcolor(colour)
    luminance = (0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()) / 255
    return "#1b1f24" if luminance > 0.55 else "#ffffff"


class AppearanceEditor(QWidget):
    """界面外观设置（实时预览）。"""

    changed = Signal(object)

    def __init__(self, appearance: Appearance, parent: QWidget | None = None,
                 base_dir: str = "") -> None:
        super().__init__(parent)
        self._appearance = appearance.copy() if hasattr(appearance, "copy") else appearance
        self._loading = False
        self._base_dir = base_dir
        self._colour_buttons: dict[str, ColorButton] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        root.addWidget(self._build_common())

        self.color_tabs = QTabWidget()
        for group, entries in COLOR_GROUPS:
            self.color_tabs.addTab(self._build_colour_group(group, entries), group)
        root.addWidget(self.color_tabs, 1)

        bottom = QHBoxLayout()
        reset = QPushButton("还原本页为默认值")
        reset.clicked.connect(self.reset_defaults)
        bottom.addWidget(reset)
        bottom.addStretch(1)
        self.hint = QLabel("改动立即预览，按「取消」可整页还原。")
        self.hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        bottom.addWidget(self.hint)
        root.addLayout(bottom)

        self._load_values()

    # -------------------------------------------------- 常用项

    def _build_common(self) -> QWidget:
        box = QGroupBox("主题与排版")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(8)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.preset_combo = QComboBox()
        for name in theme.preset_names():
            self.preset_combo.addItem(name, name)
        self.preset_combo.addItem("自定义", "自定义")
        self.preset_combo.currentIndexChanged.connect(self._on_preset)
        row.addWidget(self.preset_combo, 1)
        self.preset_note = QLabel("整套配色一次换好")
        self.preset_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        row.addWidget(self.preset_note)
        wrapper = QWidget()
        wrapper.setLayout(row)
        form.addRow("配色主题", wrapper)

        font_row = QHBoxLayout()
        font_row.setSpacing(6)
        self.font_combo = QComboBox()
        for family in theme.FONT_FAMILIES:
            self.font_combo.addItem(family.split(",")[0].strip('" '), family)
        self.font_combo.currentIndexChanged.connect(self._on_font_family)
        font_row.addWidget(self.font_combo, 1)
        font_row.addWidget(QLabel("字号"))
        self.size_box = QSpinBox()
        self.size_box.setRange(FONT_SIZE_MIN, FONT_SIZE_MAX)
        self.size_box.setSuffix(" px")
        self.size_box.valueChanged.connect(self._on_font_size)
        font_row.addWidget(self.size_box)
        font_wrapper = QWidget()
        font_wrapper.setLayout(font_row)
        form.addRow("字体", font_wrapper)

        density_row = QHBoxLayout()
        density_row.setSpacing(6)
        self.density_combo = QComboBox()
        for name in DENSITIES:
            self.density_combo.addItem(name, name)
        self.density_combo.currentIndexChanged.connect(self._on_density)
        density_row.addWidget(self.density_combo)
        self.density_note = QLabel("紧凑适合小屏多看点，宽松适合触摸屏")
        self.density_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        density_row.addWidget(self.density_note, 1)
        density_wrapper = QWidget()
        density_wrapper.setLayout(density_row)
        form.addRow("界面密度", density_wrapper)

        file_row = QHBoxLayout()
        file_row.setSpacing(6)
        export = QPushButton("导出配色…")
        export.setToolTip("把当前这一套（颜色+字体+字号+密度）存成文件，可以发给同事或备份")
        export.clicked.connect(lambda _checked=False: self.export_theme())
        file_row.addWidget(export)
        load = QPushButton("导入配色…")
        load.setToolTip("读入别人给的配色文件，立刻预览")
        load.clicked.connect(lambda _checked=False: self.import_theme())
        file_row.addWidget(load)
        self.file_note = QLabel("配色是纯 JSON，可以手改两个颜色再发出去")
        self.file_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        file_row.addWidget(self.file_note, 1)
        file_wrapper = QWidget()
        file_wrapper.setLayout(file_row)
        form.addRow("配色文件", file_wrapper)
        return box

    # -------------------------------------------------- 配色的导入导出

    def default_theme_name(self) -> str:
        palette_name = self._appearance.palette.name or "自定义"
        safe = "".join(ch for ch in palette_name if ch not in '<>:"/\\|?*').strip() or "theme"
        return f"theme_{safe}.json"

    def export_theme(self) -> str:
        """导出当前配色，返回路径（空串 = 用户取消或失败）。"""
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出配色",
            self._theme_dir() + "/" + self.default_theme_name(),
            "配色文件 (*.json)",
        )
        if not path:
            return ""
        try:
            theme.save_theme(path, self._appearance)
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", f"写不了这个文件：\n{exc}")
            return ""
        self.file_note.setText(f"已导出 {path}")
        self.hint.setText("配色已导出，可以直接发给同事")
        return path

    def import_theme(self) -> bool:
        """导入配色文件并立即预览。返回是否成功。"""
        path, _ = QFileDialog.getOpenFileName(
            self, "导入配色", self._theme_dir(), "配色文件 (*.json);;所有文件 (*)"
        )
        if not path:
            return False
        try:
            loaded, warnings = theme.load_theme_file(path)
        except ValueError as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return False
        self._appearance = loaded
        self._load_values()
        self._emit()
        text = f"已导入 {path}"
        if warnings:
            text += f"（{len(warnings)} 项提示：" + "；".join(warnings[:3]) + "）"
        self.file_note.setText(text)
        self.hint.setText("配色已导入并预览，按「确定」保存")
        return True

    def _theme_dir(self) -> str:
        """默认目录：优先放在工程目录旁边（施工现场习惯把配置和图放一起）。"""
        base = getattr(self, "_base_dir", "")
        return base or "."

    def _build_colour_group(self, _group: str, entries: list[tuple[str, str]]) -> QWidget:
        """一组颜色按钮。条目多时放进滚动区，免得窗口被撑高。"""
        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(10, 10, 10, 10)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)

        for index, (field, label) in enumerate(entries):
            column = (index % 2) * 2
            name = QLabel(label)
            name.setStyleSheet(f"color: {theme.TEXT};")
            grid.addWidget(name, index // 2, column)

            button = ColorButton(field, getattr(self._appearance.palette, field))
            button.colorChanged.connect(
                lambda colour, key=field: self._on_colour(key, colour)
            )
            self._colour_buttons[field] = button
            grid.addWidget(button, index // 2, column + 1)
        grid.setColumnStretch(3, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        return scroll

    # -------------------------------------------------- 载入/回写

    def _load_values(self) -> None:
        self._loading = True
        try:
            palette = self._appearance.palette
            index = self.preset_combo.findData(palette.name)
            if index >= 0:
                self.preset_combo.setCurrentIndex(index)
            for field, button in self._colour_buttons.items():
                button.set_colour(getattr(palette, field))
            self.size_box.setValue(self._appearance.font_size)
            index = self.font_combo.findData(self._appearance.font_family)
            if index < 0:
                self.font_combo.addItem(self._appearance.font_family, self._appearance.font_family)
                index = self.font_combo.count() - 1
            self.font_combo.setCurrentIndex(index)
            density_index = self.density_combo.findData(self._appearance.density)
            self.density_combo.setCurrentIndex(density_index if density_index >= 0 else 1)
        finally:
            self._loading = False

    def current_appearance(self) -> Appearance:
        return self._appearance

    def _emit(self) -> None:
        if not self._loading:
            self.changed.emit(self._appearance)

    # -------------------------------------------------- 交互

    def _on_preset(self) -> None:
        if self._loading:
            return
        name = self.preset_combo.currentData()
        preset = theme.PRESETS.get(name)
        if preset is None:
            return
        kept_font = self._appearance.font_family
        kept_size = self._appearance.font_size
        kept_density = self._appearance.density
        self._appearance = Appearance(
            palette=preset.copy(),
            font_family=kept_font,
            font_size=kept_size,
            density=kept_density,
        )
        self._load_values()
        self._emit()

    def _on_font_family(self) -> None:
        if self._loading:
            return
        self._appearance.font_family = self.font_combo.currentData()
        self._emit()

    def _on_font_size(self, value: int) -> None:
        if self._loading:
            return
        self._appearance.font_size = int(value)
        self._emit()

    def _on_density(self) -> None:
        if self._loading:
            return
        self._appearance.density = self.density_combo.currentData()
        self._emit()

    def _on_colour(self, field: str, colour: str) -> None:
        if self._loading:
            return
        setattr(self._appearance.palette, field, colour)
        # 手改颜色后主题名不再准确，标成"自定义"，避免下拉框骗人
        self._appearance.palette.name = "自定义"
        self._loading = True
        try:
            index = self.preset_combo.findData("自定义")
            if index >= 0:
                self.preset_combo.setCurrentIndex(index)
        finally:
            self._loading = False
        self._emit()

    def reset_defaults(self) -> None:
        """把本页所有外观项还原成默认主题的默认值。"""
        kept_size = self._appearance.font_size
        default = theme.PRESETS.get(theme.DEFAULT_PRESET)
        self._appearance = Appearance(
            palette=(default.copy() if default is not None else Palette()),
            font_size=kept_size,
        )
        self._load_values()
        self._emit()
