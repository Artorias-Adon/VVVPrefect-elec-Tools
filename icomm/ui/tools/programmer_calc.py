"""程序员计算器：进制 / 浮点 / 位段 / 定标换算。

面向电气、电力电子工程师 —— 他们要一边盯着实时数据，一边对着 DSP 手册算
寄存器。所以这里做成 :class:`~icomm.ui.tools.base.WindowTool`
（**非模态浮动窗口**），算的时候不用把主界面关掉。

* 进制页：HEX / DEC / OCT / BIN 四栏联动 + 可点击的位网格 + 位段提取；
  位宽（8/16/32/64）与有符号/无符号可切，改动立刻反映到所有显示上。
* 浮点页：浮点值 → IEEE-754 位模式（32/64 位同时给出），
  以及位模式（例如寄存器读到的 ``0x43AF0000``）→ 浮点值；
  顺带给出「该精度能不能精确表示」的误差，定标精度够不够一目了然。
* 定标页：``工程量 = 原始值 × 缩放 + 偏移`` 双向换算，外加 2 的幂速查表。

界面里所有颜色都取自 :mod:`icomm.ui.theme`（换主题时跟着变），
数值格式化统一交给 :mod:`icomm.core.programmer_calc`，本模块只管摆放控件。
"""

from __future__ import annotations

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import programmer_calc as pc
from .. import theme
from .base import ToolContext, WindowTool, register_tool

#: 位方格的尺寸：固定大小才能保证 8 位一行排得整齐、不随文字长度抖动
BIT_CELL_W = 28
BIT_CELL_H = 24

#: 位网格左侧「字节」标签的宽度
AXIS_WIDTH = 74

#: 进制栏的标题宽度
BASE_LABEL_WIDTH = 46

#: 等宽字体（取主题里那个等宽候选）：十六进制、二进制按位对齐才好读
MONO_FONT = theme.FONT_FAMILIES[3]


def _dim_label(text: str = "") -> QLabel:
    """次要说明文字。"""
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.TEXT_DIM};")
    return label


def _value_label(text: str = "--", mono: bool = True) -> QLabel:
    """结果文字：可用鼠标选中复制（工程师常要把结果贴进手册/脚本）。"""
    label = QLabel(text)
    style = f"color: {theme.TEXT};"
    if mono:
        style += f" font-family: {MONO_FONT};"
    label.setStyleSheet(style)
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def _set_message(label: QLabel, text: str, error: bool = False) -> None:
    """统一的提示行：正常时是暗色说明，出错时变红。

    两种状态都占同样的高度，切换时布局不会上下跳。
    """
    label.setText(text)
    label.setStyleSheet(f"color: {theme.QUALITY_BAD if error else theme.TEXT_DIM};")


def _fmt_number(value: float) -> str:
    """工程量这类结果用 12 位有效数字显示。

    100 × 0.1 在双精度里是 10.000000000000002，直接甩给工程师会被当成
    「定标算错了」；这里只保留 12 位有效数字，看着还是 10。
    需要逐位精确的地方（误差）走 :func:`programmer_calc.number_text`。
    """
    if math.isnan(value) or math.isinf(value):
        return pc.number_text(value)
    return f"{value:.12g}"


class BitGrid(QWidget):
    """位网格：每一位一个方格，点一下就翻转。

    高字节在上、字节内高位在左（``B3 · 31:24`` 那一行），跟看寄存器手册时
    的习惯一致；64 位也只是 8 行 × 8 列，不用滚动。
    """

    #: 翻转某位后的新位模式。用 object 承载：``Signal(int)`` 是 32 位，64 位会被截断
    valueChanged = Signal(object)

    def __init__(self, width: int = 8, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(3)
        self._grid.setVerticalSpacing(3)
        self._buttons: dict[int, QPushButton] = {}
        self._value = 0
        self._width = 0
        self._updating = False
        self.setStyleSheet(self._style())
        self.set_width(width)

    @staticmethod
    def _style() -> str:
        return f"""
QPushButton#bitCell {{
    background: {theme.BG_INPUT};
    border: 1px solid {theme.BORDER};
    border-radius: 3px;
    color: {theme.TEXT_DIM};
    font-family: {MONO_FONT};
    padding: 0;
}}
QPushButton#bitCell:checked {{
    background: {theme.ACCENT};
    border-color: {theme.ACCENT};
    color: #ffffff;   /* 跟着主题模板：强调色底上的文字一律用白色 */
    font-weight: 600;
}}
QPushButton#bitCell:hover {{ border-color: {theme.ACCENT_ALT}; }}
"""

    # -------------------------------------------------- 对外

    @property
    def value(self) -> int:
        return self._value

    @property
    def width(self) -> int:
        return self._width

    def button(self, index: int) -> QPushButton | None:
        """给测试/脚本用的取方格入口。"""
        return self._buttons.get(index)

    def set_width(self, width: int) -> None:
        """重建网格：位宽变了行列都要重排。"""
        pc.check_width(width)
        if width == self._width:
            return
        self._width = width
        self._clear_cells()
        # 列头：字节内的位号 7..0
        for column in range(8):
            header = _dim_label(str(7 - column))
            header.setAlignment(Qt.AlignCenter)
            header.setFixedWidth(BIT_CELL_W)
            self._grid.addWidget(header, 0, column + 1)
        # 数据行：高字节在上
        for row, byte_index in enumerate(range(width // 8 - 1, -1, -1)):
            caption = _dim_label(f"B{byte_index} · {byte_index * 8 + 7}:{byte_index * 8}")
            caption.setFixedWidth(AXIS_WIDTH)
            self._grid.addWidget(caption, row + 1, 0)
            for column in range(8):
                index = byte_index * 8 + (7 - column)
                button = QPushButton("0")
                button.setObjectName("bitCell")
                button.setCheckable(True)
                button.setFixedSize(BIT_CELL_W, BIT_CELL_H)
                button.setToolTip(f"bit{index}　（点击翻转）")
                button.toggled.connect(lambda _checked, i=index: self._on_toggled(i))
                self._grid.addWidget(button, row + 1, column + 1)
                self._buttons[index] = button
        self._refresh_cells()

    def set_value(self, value: int) -> None:
        """外部更新位模式（进制栏联动、位段计算都走这里）。"""
        previous = self._updating
        self._updating = True
        try:
            self._value = int(value) & ((1 << self._width) - 1)
        finally:
            self._updating = previous
        self._refresh_cells()

    def flip_bit(self, index: int) -> None:
        """翻转某一位（等价于点那个方格）。"""
        self._value = pc.toggle_bit(self._value, index, self._width)
        self._refresh_cells()
        self.valueChanged.emit(self._value)

    # -------------------------------------------------- 内部

    def _clear_cells(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._buttons.clear()

    def _refresh_cells(self) -> None:
        # 保存/还原而不是直接置 False：set_value 里已经置位过一次，嵌套调用不能把它清掉
        previous = self._updating
        self._updating = True
        try:
            for index, button in self._buttons.items():
                bit = self._value >> index & 1
                button.setChecked(bool(bit))
                button.setText("1" if bit else "0")
        finally:
            self._updating = previous

    def _on_toggled(self, index: int) -> None:
        # 程序改动也会触发 toggled，所以只在用户点击时走这条路径
        if self._updating:
            return
        self.flip_bit(index)


class FloatDetail(QGroupBox):
    """一块 IEEE-754 拆解结果：位模式 / 符号 / 指数 / 尾数 / 构成 / 误差。

    32 位、64 位、反推三处都用它，保证同一个数在哪儿显示都一样。
    """

    ROWS: tuple[str, ...] = (
        "位模式 (HEX)",
        "二进制",
        "符号位",
        "指数位",
        "尾数位",
        "指数真值",
        "尾数小数",
        "构成",
        "误差",
        "精确表示",
    )

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        grid = QGridLayout(self)
        grid.setContentsMargins(10, 12, 10, 8)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        self._labels: dict[str, QLabel] = {}
        for row, name in enumerate(self.ROWS):
            caption = _dim_label(name)
            value = _value_label("--")
            # 二进制（64 位有 79 个字符）与构成公式较长，允许折行，不要被裁掉
            value.setWordWrap(name in ("二进制", "构成"))
            grid.addWidget(caption, row, 0, Qt.AlignTop | Qt.AlignLeft)
            grid.addWidget(value, row, 1)
            self._labels[name] = value
        grid.setColumnStretch(1, 1)

    def text_of(self, name: str) -> str:
        """取某一行的文本（测试与脚本核对用）。"""
        label = self._labels.get(name)
        return label.text() if label is not None else ""

    def show_info(self, info: pc.FloatInfo | None) -> None:
        """显示一份拆解结果；``None`` 表示当前输入不合法（清空）。"""
        if info is None:
            for label in self._labels.values():
                label.setText("--")
            return

        if info.is_nan:
            error_text, exact_text = "NaN 不参与误差比较", "—"
        elif info.exact:
            error_text, exact_text = "0（该精度能精确表示）", "是"
        else:
            relative = info.relative_error
            extra = f"，相对误差 {pc.number_text(relative)}" if relative is not None else ""
            error_text = f"{pc.number_text(info.error)}{extra}"
            exact_text = "否"

        values = {
            "位模式 (HEX)": f"{info.hex_text}　（{info.bits}）",
            "二进制": info.binary_text,
            "符号位": f"{info.sign_text}（{'负' if info.is_negative else '正'}）",
            "指数位": f"{info.exponent_bits}　（{info.exponent_text}）",
            "尾数位": f"0x{info.mantissa_bits:X}",
            "指数真值": str(info.exponent),
            "尾数小数": pc.number_text(info.mantissa),
            "构成": info.formula,
            "误差": error_text,
            "精确表示": exact_text,
        }
        for name, label in self._labels.items():
            label.setText(values[name])


class ProgrammerCalcWindow(QWidget):
    """程序员计算器窗口。"""

    def __init__(self, context: ToolContext | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.context = context
        self._width = 32
        self._signed = False
        self._value = 0
        self._edits: dict[str, QLineEdit] = {}
        # 解析用的进制：DEC 用 0 = 按前缀自动判断，这样十进制栏里贴 0x2F400000 也认
        self._bases = {"HEX": 16, "DEC": 0, "OCT": 8, "BIN": 2}
        # 显示用的进制：十进制栏显示成十进制
        self._format_bases = {"HEX": 16, "DEC": 10, "OCT": 8, "BIN": 2}
        self._scale_edits: dict[str, QLineEdit] = {}

        self.setObjectName("programmerCalc")
        # 浮动工具窗：跟按键控制面板同一套路，置顶且不挡主窗口的其它部分
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.setWindowTitle("程序员计算器")
        self.resize(940, 780)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)
        root.addWidget(self._build_top())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scrolled(self._build_bits_page()), "进制与位")
        self.tabs.addTab(self._scrolled(self._build_float_page()), "浮点")
        self.tabs.addTab(self._scrolled(self._build_scale_page()), "定标")
        root.addWidget(self.tabs, 1)

        hint = _dim_label("四位进制栏任改一栏其余实时同步；位网格点一下即翻转该位；浮点页会算出能否被该精度精确表示。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.set_value(0)
        self._on_float_edited()
        self._on_reverse_edited()

    # -------------------------------------------------- 顶部

    def _build_top(self) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(QLabel("位宽"))
        self.width_combo = QComboBox()
        for width in pc.WIDTHS:
            self.width_combo.addItem(f"{width} 位", width)
        self.width_combo.setCurrentIndex(list(pc.WIDTHS).index(self._width))
        self.width_combo.currentIndexChanged.connect(self._on_width_changed)
        layout.addWidget(self.width_combo)

        layout.addWidget(QLabel("符号"))
        self.sign_combo = QComboBox()
        self.sign_combo.addItem("无符号", False)
        self.sign_combo.addItem("有符号", True)
        self.sign_combo.setToolTip("只影响十进制的显示与解析范围，位模式本身不变")
        self.sign_combo.currentIndexChanged.connect(self._on_sign_changed)
        layout.addWidget(self.sign_combo)

        layout.addStretch(1)
        layout.addWidget(_dim_label("位宽与符号影响进制显示、位网格、位段提取"))
        return box

    @staticmethod
    def _scrolled(inner: QWidget) -> QScrollArea:
        """页内容放进滚动区：窗口被拖小时靠滚动，而不是把控件挤到看不清。"""
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setWidget(inner)
        return area

    # -------------------------------------------------- 进制页

    def _build_bits_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        layout.addWidget(self._build_base_group())

        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self._build_grid_group(), 0)
        row.addWidget(self._build_extract_group(), 1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _build_base_group(self) -> QWidget:
        group = QGroupBox("进制联动（HEX / DEC / OCT / BIN）")
        grid = QGridLayout(group)
        grid.setContentsMargins(10, 14, 10, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        for row, (key, title, tooltip) in enumerate(
            (
                ("HEX", "HEX", "十六进制；可带 0x，也可直接写 3F800000"),
                ("DEC", "DEC", "十进制；也接受 0x/0b/0o 前缀与下划线分隔"),
                ("OCT", "OCT", "八进制；可带 0o 前缀"),
                ("BIN", "BIN", "二进制；空格分组只是好看，怎么写都认"),
            )
        ):
            caption = QLabel(title)
            caption.setFixedWidth(BASE_LABEL_WIDTH)
            edit = QLineEdit()
            edit.setToolTip(tooltip)
            edit.setMinimumWidth(260)
            edit.setStyleSheet(f"font-family: {MONO_FONT};")
            edit.textEdited.connect(lambda text, k=key: self._on_base_edited(k, text))
            # 失焦时把这一栏自己重新排版（补零、加大写前缀），输入过程中不动它，
            # 否则一边打字一边被改写，光标会乱跳
            edit.editingFinished.connect(lambda k=key: self._normalize_field(k))
            grid.addWidget(caption, row, 0)
            grid.addWidget(edit, row, 1)
            self._edits[key] = edit

        self.base_message = QLabel()
        self.base_message.setWordWrap(True)
        self.base_message.setMinimumHeight(18)
        _set_message(self.base_message, "改任意一栏，其余三栏实时同步。")
        grid.addWidget(self.base_message, len(self._edits), 0, 1, 2)
        grid.setColumnStretch(1, 1)

        # 具名引用：测试、脚本和其它工具要往某一栏填数时用得上
        self.hex_edit = self._edits["HEX"]
        self.dec_edit = self._edits["DEC"]
        self.oct_edit = self._edits["OCT"]
        self.bin_edit = self._edits["BIN"]
        return group

    def _build_grid_group(self) -> QWidget:
        group = QGroupBox("位网格（点击翻转）")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 14, 10, 10)
        self.bit_grid = BitGrid(self._width)
        self.bit_grid.valueChanged.connect(self._on_grid_changed)
        layout.addWidget(self.bit_grid)
        layout.addStretch(1)
        return group

    def _build_extract_group(self) -> QWidget:
        group = QGroupBox("位段提取（读 DSP 位域寄存器）")
        grid = QGridLayout(group)
        grid.setContentsMargins(10, 14, 10, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        grid.addWidget(QLabel("高位"), 0, 0)
        self.high_spin = QSpinBox()
        self.high_spin.setRange(0, self._width - 1)
        self.high_spin.setValue(self._width - 1)
        # 限宽：数字框拉满整行既难看又会让整页变宽，窄窗口就得横向滚动
        self.high_spin.setMaximumWidth(90)
        self.high_spin.valueChanged.connect(self._refresh_extract)
        grid.addWidget(self.high_spin, 0, 1, Qt.AlignLeft)

        grid.addWidget(QLabel("低位"), 1, 0)
        self.low_spin = QSpinBox()
        self.low_spin.setRange(0, self._width - 1)
        self.low_spin.setValue(0)
        self.low_spin.setMaximumWidth(90)
        self.low_spin.valueChanged.connect(self._refresh_extract)
        grid.addWidget(self.low_spin, 1, 1, Qt.AlignLeft)

        grid.addWidget(_dim_label("取值"), 2, 0, Qt.AlignTop | Qt.AlignLeft)
        self.extract_label = _value_label("--")
        grid.addWidget(self.extract_label, 2, 1)

        grid.addWidget(_dim_label("HEX"), 3, 0, Qt.AlignTop | Qt.AlignLeft)
        self.extract_hex_label = _value_label("--")
        grid.addWidget(self.extract_hex_label, 3, 1)

        grid.addWidget(_dim_label("位宽"), 4, 0, Qt.AlignTop | Qt.AlignLeft)
        self.extract_span_label = _value_label("--")
        grid.addWidget(self.extract_span_label, 4, 1)

        grid.addWidget(_dim_label("二进制"), 5, 0, Qt.AlignTop | Qt.AlignLeft)
        self.extract_bits_label = _value_label("--")
        # 一行放不下的长串允许折行：宁可换行，也不要撑宽整个窗口
        self.extract_bits_label.setWordWrap(True)
        grid.addWidget(self.extract_bits_label, 5, 1)

        hint = _dim_label("提示：高位必须 ≥ 低位，结果右对齐；超范围会直接报错。")
        # 长提示要能折行，否则它会顶宽整列，窗口一小就得横向滚动
        hint.setWordWrap(True)
        grid.addWidget(hint, 6, 0, 1, 2)
        grid.setColumnStretch(1, 1)
        return group

    # -------------------------------------------------- 浮点页

    def _build_float_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        layout.addWidget(self._build_float_forward())
        layout.addWidget(self._build_float_backward())
        layout.addStretch(1)
        return page

    def _build_float_forward(self) -> QWidget:
        group = QGroupBox("浮点值 → 位模式（IEEE-754）")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("浮点值"))
        self.float_input = QLineEdit()
        self.float_input.setPlaceholderText("例如 350 / -2.5 / 0.1 / 1e-3 / 0x10 / inf")
        self.float_input.setStyleSheet(f"font-family: {MONO_FONT};")
        self.float_input.textEdited.connect(self._on_float_edited)
        row.addWidget(self.float_input, 1)
        layout.addLayout(row)

        self.float_message = QLabel()
        self.float_message.setWordWrap(True)
        self.float_message.setMinimumHeight(18)
        layout.addWidget(self.float_message)

        both = QHBoxLayout()
        both.setSpacing(10)
        self.float32_detail = FloatDetail("32 位（单精度 / float32）")
        self.float64_detail = FloatDetail("64 位（双精度 / float64）")
        both.addWidget(self.float32_detail, 1)
        both.addWidget(self.float64_detail, 1)
        layout.addLayout(both)
        return group

    def _build_float_backward(self) -> QWidget:
        group = QGroupBox("位模式 → 浮点值（寄存器读数反推）")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 14, 10, 10)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("位宽"))
        self.reverse_combo = QComboBox()
        for width in pc.FLOAT_WIDTHS:
            self.reverse_combo.addItem(f"{width} 位", width)
        self.reverse_combo.currentIndexChanged.connect(self._on_reverse_edited)
        row.addWidget(self.reverse_combo)

        row.addWidget(QLabel("位模式"))
        self.reverse_input = QLineEdit()
        self.reverse_input.setPlaceholderText("HEX 或 DEC，例如 0x43AF0000")
        self.reverse_input.setStyleSheet(f"font-family: {MONO_FONT};")
        self.reverse_input.textEdited.connect(self._on_reverse_edited)
        row.addWidget(self.reverse_input, 1)

        # 算出来的位模式可以直接丢进制页继续抠位段 —— 两页之间来回填数很烦
        self.send_button = QPushButton("送到进制页")
        self.send_button.setToolTip("把这个位模式送到「进制与位」页继续算位段")
        self.send_button.clicked.connect(self._send_reverse_to_bits)
        row.addWidget(self.send_button)
        layout.addLayout(row)

        self.reverse_message = QLabel()
        self.reverse_message.setWordWrap(True)
        self.reverse_message.setMinimumHeight(18)
        layout.addWidget(self.reverse_message)

        self.reverse_detail = FloatDetail("拆解结果")
        layout.addWidget(self.reverse_detail)
        return group

    # -------------------------------------------------- 定标页

    def _build_scale_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)
        layout.addWidget(self._build_scale_group())
        # 速查表是拿来做速查的，给它伸缩权重，窗口拉高就能多看几行
        layout.addWidget(self._build_scale_table(), 1)
        return page

    def _build_scale_group(self) -> QWidget:
        group = QGroupBox("定标换算　工程量 = 原始值 × 缩放 + 偏移")
        grid = QGridLayout(group)
        grid.setContentsMargins(10, 14, 10, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        for row, (key, title, default, tooltip) in enumerate(
            (
                ("raw", "原始值", "0", "寄存器里的原始数；十进制或 0x 十六进制都行"),
                ("scale", "缩放", "1", "定标系数，例如 0.1 / 1/256 / 0x1（= 1.0）"),
                ("offset", "偏移", "0", "零点偏移"),
                ("engineering", "工程量", "", "改这一栏会反算原始值（缩放不能为 0）"),
            )
        ):
            caption = QLabel(title)
            caption.setFixedWidth(BASE_LABEL_WIDTH + 30)
            edit = QLineEdit(default)
            edit.setToolTip(tooltip)
            edit.setStyleSheet(f"font-family: {MONO_FONT};")
            edit.textEdited.connect(lambda text, k=key: self._on_scale_edited(k))
            grid.addWidget(caption, row, 0)
            grid.addWidget(edit, row, 1)
            self._scale_edits[key] = edit

        self.scale_message = QLabel()
        self.scale_message.setWordWrap(True)
        self.scale_message.setMinimumHeight(18)
        grid.addWidget(self.scale_message, len(self._scale_edits), 0, 1, 2)
        grid.setColumnStretch(1, 1)

        self.raw_edit = self._scale_edits["raw"]
        self.scale_edit = self._scale_edits["scale"]
        self.offset_edit = self._scale_edits["offset"]
        self.engineering_edit = self._scale_edits["engineering"]
        return group

    def _build_scale_table(self) -> QWidget:
        group = QGroupBox("定标速查（2 的幂次与常用系数，含 float32 精度）")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 14, 10, 10)

        self.scale_table = QTableWidget(0, 4)
        self.scale_table.setHorizontalHeaderLabels(["名称", "系数", "float32 位模式", "能否精确表示"])
        self.scale_table.verticalHeader().setVisible(False)
        self.scale_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.scale_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.scale_table.setAlternatingRowColors(True)
        self.scale_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.scale_table.setMinimumHeight(260)

        rows = pc.scale_table()
        self.scale_table.setRowCount(len(rows))
        for index, entry in enumerate(rows):
            cells = (
                entry.name,
                entry.text,
                entry.hex32,
                "精确" if entry.exact32 else "有误差",
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setForeground(QColor(theme.TEXT))
                elif column in (1, 2):
                    item.setFont(self._mono_font())
                    item.setForeground(QColor(theme.TEXT))
                else:
                    item.setForeground(
                        QColor(theme.QUALITY_GOOD if entry.exact32 else theme.QUALITY_STALE)
                    )
                self.scale_table.setItem(index, column, item)
        layout.addWidget(self.scale_table)
        return group

    @staticmethod
    def _mono_font() -> QFont:
        """表格单元格用的等宽字体：十六进制按位对齐才好核对。"""
        font = QFont()
        font.setFamily("Consolas")
        return font

    # -------------------------------------------------- 进制：交互

    def set_value(self, value: int) -> None:
        """外部把某个位模式送进来显示。

        别的工具（示波器光标、点位详情）要拿一个寄存器值过来算，调它即可。
        """
        self._value = int(value) & ((1 << self._width) - 1)
        self._refresh_fields()

    @property
    def value(self) -> int:
        return self._value

    def _on_base_edited(self, key: str, text: str) -> None:
        try:
            value = pc.parse_int(text, self._width, self._signed, base=self._bases[key])
        except pc.CalcError as exc:
            # 报错但不改动数值：宁可让人看到错在哪，也不要悄悄换成别的数
            _set_message(self.base_message, f"{key} 栏：{exc}", error=True)
            return
        _set_message(self.base_message, "改任意一栏，其余三栏实时同步。")
        self._value = value
        self._refresh_fields(skip=key)

    def _normalize_field(self, key: str) -> None:
        """失焦时把该栏自己排版规整（补零、统一前缀/大小写）。"""
        try:
            pc.parse_int(self._edits[key].text(), self._width, self._signed, base=self._bases[key])
        except pc.CalcError:
            return  # 不合法就留着原文，错误提示已经在下面显示
        self._refresh_fields()

    def _refresh_fields(self, skip: str | None = None) -> None:
        for key, base in self._format_bases.items():
            if key == skip:
                continue
            self._edits[key].setText(
                pc.format_value(self._value, self._width, base, self._signed)
            )
        self.bit_grid.set_value(self._value)
        self._refresh_extract()

    def _on_grid_changed(self, value: int) -> None:
        self._value = int(value)
        self._refresh_fields()
        _set_message(self.base_message, "改任意一栏，其余三栏实时同步。")

    def _refresh_extract(self, *_args) -> None:
        high, low = self.high_spin.value(), self.low_spin.value()
        try:
            field = pc.extract_bits(self._value, high, low, self._width)
        except pc.CalcError as exc:
            # 每个结果各占一行且都不长：长串会把整个窗口撑宽
            for label in (self.extract_label, self.extract_hex_label, self.extract_span_label):
                label.setText("--")
            self.extract_bits_label.setText(str(exc))
            return
        span = high - low + 1
        self.extract_label.setText(str(field))
        self.extract_hex_label.setText(pc.format_hex(field))
        self.extract_span_label.setText(f"{span} 位（bit{high}:{low}）")
        # 位段按 4 位补零显示，和手册里「0011 0101」那种写法对上
        self.extract_bits_label.setText(pc.format_binary(field, max(4, (span + 3) // 4 * 4)))

    # -------------------------------------------------- 位宽 / 符号

    def _on_width_changed(self, *_args) -> None:
        width = int(self.width_combo.currentData())
        if width == self._width:
            return
        previous = self._value
        self._width = width
        self.bit_grid.set_width(width)

        for spin in (self.high_spin, self.low_spin):
            spin.setMaximum(width - 1)
        self.high_spin.setValue(width - 1)
        self.low_spin.setValue(0)

        if previous >= 1 << width:
            # 不做静默截断：明确告诉用户放不下、已被清零
            self.set_value(0)
            _set_message(
                self.base_message,
                f"原值 {pc.format_hex(previous)} 超出 {width} 位位宽，已清零重新输入。",
                error=True,
            )
        else:
            self.set_value(previous)
            _set_message(self.base_message, "改任意一栏，其余三栏实时同步。")

    def _on_sign_changed(self, *_args) -> None:
        self._signed = bool(self.sign_combo.currentData())
        self._refresh_fields()

    # -------------------------------------------------- 浮点：交互

    def _on_float_edited(self, *_args) -> None:
        text = self.float_input.text().strip()
        if not text:
            self.float32_detail.show_info(None)
            self.float64_detail.show_info(None)
            _set_message(self.float_message, "输入一个浮点值，下面同时给出 32 位与 64 位的位模式。")
            return
        try:
            value = pc.parse_float(text)
        except pc.CalcError as exc:
            self.float32_detail.show_info(None)
            self.float64_detail.show_info(None)
            _set_message(self.float_message, str(exc), error=True)
            return

        infos: dict[int, pc.FloatInfo | None] = {}
        problems: list[str] = []
        for width in pc.FLOAT_WIDTHS:
            try:
                infos[width] = pc.decode_float(value, width)
            except pc.CalcError as exc:
                # 32 位放不下不代表 64 位也放不下，所以两种精度各算各的
                infos[width] = None
                problems.append(str(exc))
        self.float32_detail.show_info(infos[32])
        self.float64_detail.show_info(infos[64])
        if problems:
            _set_message(self.float_message, "；".join(problems), error=True)
        else:
            _set_message(
                self.float_message,
                "看「误差 / 精确表示」两行：不为 0 就说明这个数在该精度下存不下。",
            )

    def _on_reverse_edited(self, *_args) -> None:
        width = int(self.reverse_combo.currentData())
        text = self.reverse_input.text().strip()
        if not text:
            self.reverse_detail.show_info(None)
            _set_message(self.reverse_message, "填寄存器里的位模式（HEX 或 DEC），下面给出对应浮点值。")
            return
        try:
            bits = pc.parse_int(text, width, base=0)
            info = pc.decode_bits(bits, width)
        except pc.CalcError as exc:
            self.reverse_detail.show_info(None)
            _set_message(self.reverse_message, str(exc), error=True)
            return
        self.reverse_detail.show_info(info)
        _set_message(
            self.reverse_message,
            f"{pc.format_hex(bits, width)} → {pc.number_text(info.value)}",
        )

    def _send_reverse_to_bits(self) -> None:
        """把反推用的位模式送到进制页，接着抠位段。"""
        width = int(self.reverse_combo.currentData())
        try:
            bits = pc.parse_int(self.reverse_input.text(), width, base=0)
        except pc.CalcError as exc:
            _set_message(self.reverse_message, str(exc), error=True)
            return
        self.width_combo.setCurrentIndex(list(pc.WIDTHS).index(width))
        self.set_value(bits)
        self.tabs.setCurrentIndex(0)
        _set_message(self.base_message, f"已送入 {pc.format_hex(self._value, self._width)}。")

    # -------------------------------------------------- 定标：交互

    def _on_scale_edited(self, key: str) -> None:
        try:
            raw = pc.parse_float(self._scale_edits["raw"].text() or "0")
            scale = pc.parse_float(self._scale_edits["scale"].text() or "1")
            offset = pc.parse_float(self._scale_edits["offset"].text() or "0")
            if key == "engineering":
                engineering = pc.parse_float(self._scale_edits["engineering"].text())
                raw = pc.engineering_to_raw(engineering, scale, offset)
                self._scale_edits["raw"].setText(_fmt_number(raw))
            else:
                engineering = pc.raw_to_engineering(raw, scale, offset)
                self._scale_edits["engineering"].setText(_fmt_number(engineering))
        except pc.CalcError as exc:
            _set_message(self.scale_message, str(exc), error=True)
            return
        _set_message(
            self.scale_message,
            f"{_fmt_number(raw)} × {_fmt_number(scale)} + {_fmt_number(offset)}"
            f" = {_fmt_number(engineering)}",
        )


class ProgrammerCalcTool(WindowTool):
    """「程序员计算器」工具（非模态浮动窗口）。"""

    name = "程序员计算器"
    group = "计算"
    order = 10
    description = "进制/浮点/位段/定标换算"

    def build(self, context: ToolContext | None = None) -> QWidget:
        # 窗口 parent 给主窗口：主窗口一关，工具窗跟着关，不会留下孤零零的置顶窗口。
        # context 允许为 None：工具不碰工程数据，单独 fork 出来测/当脚本跑也行。
        parent = context.window if context is not None else None
        return ProgrammerCalcWindow(context, parent=parent)


register_tool(ProgrammerCalcTool())
