"""点位参数对话框。

在任意点位控件上**双击**即打开。一个窗口里把与这个点位有关的操作都收齐：

* 看当前值、质量、地址与数据类型；
* 改值后**单发**（写一次）或**持续发送**（按周期反复写）；
* 位域点位直接给命令按钮（控制字这种"写命令码"的语义）或按位勾选
  （功能配置字这种"位掩码"的语义）—— 这两种语义不一样，不能混着用。

对话框是非模态的：开着它也能继续看主界面的数据变化，方便边调边看。
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.codec import format_value
from ...core.settings import PERIODIC_MAX_MS, PERIODIC_MIN_MS
from .. import theme

#: 状态刷新间隔（毫秒）：用来看持续发送的发包计数与错误
REFRESH_MS = 400

QUALITY_TEXT = {
    "good": "正常",
    "bad": "通信异常",
    "stale": "数据陈旧",
    "uncertain": "不确定",
}


class PointDialog(QDialog):
    """点位参数 / 写值对话框。"""

    #: 单发写入 ``(点号, 值)``
    writeRequested = Signal(str, object)
    #: 持续发送 ``(点号, 值, 周期毫秒)``
    periodicRequested = Signal(str, object, int)
    #: 停止该点位的持续发送
    periodicStopRequested = Signal(str)
    #: 请求到「设备与点位」页编辑这个点位的定义
    definitionRequested = Signal(str)

    def __init__(
        self,
        pid: str,
        point,
        value_of: Callable[[str], Any],
        periodic_of: Callable[[str], Any],
        default_interval_ms: int = 1000,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.pid = pid
        self.point = point
        self._value_of = value_of
        self._periodic_of = periodic_of
        self._bit_labels = point.bit_labels()
        self._commands = point.command_items()
        self._bit_boxes: dict[int, QCheckBox] = {}
        #: 用户动过「按位设置」没有：没动过就让勾选跟着设备当前值走
        self._bits_dirty = False
        self._project_entry = None
        self.bool_box: QCheckBox | None = None
        self.value_box: QDoubleSpinBox | None = None
        self.command_buttons: list[QPushButton] = []
        self.interval_box: QSpinBox | None = None
        self.periodic_button: QPushButton | None = None
        self.stop_button: QPushButton | None = None

        self.setWindowTitle(f"点位参数 · {point.title}")
        self.setMinimumWidth(480)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        root.addWidget(self._build_header())
        if self._bit_labels:
            root.addWidget(self._build_bitmap())
        if self._commands:
            # 命令点位：只给命令按钮。手输命令码容易写错，周期发"启动"也没有意义。
            root.addWidget(self._build_commands())
        elif self._bit_labels and point.writable:
            # 位掩码点位：位勾选就是它的输入方式，再给数字框反而容易对不上
            root.addWidget(self._build_bit_toggles())
            root.addWidget(self._build_write(default_interval_ms, allow_value=False))
        elif point.writable:
            root.addWidget(self._build_write(default_interval_ms))
        else:
            hint = QLabel("该点位是只读遥测，不能写入。")
            hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
            root.addWidget(hint)
        root.addWidget(self._build_footer())

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # -------------------------------------------------- 各区块

    def _build_header(self) -> QWidget:
        point = self.point
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        title = QLabel(point.title)
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 15px; font-weight: 600;")
        layout.addWidget(title)

        meta = f"{self.pid}　·　{point.register} #{point.address}　·　{point.data_type}"
        if point.element_width > 1:
            meta += f"（{point.element_width} 个寄存器，字序 {point.word_order}）"
        if point.scale != 1.0 or point.offset:
            meta += f"　·　缩放 {point.scale} 偏移 {point.offset}"
        if point.unit:
            meta += f"　·　单位 {point.unit}"
        line = QLabel(meta)
        line.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        line.setWordWrap(True)
        layout.addWidget(line)

        self.current_label = QLabel("当前值：--")
        layout.addWidget(self.current_label)
        return box

    def _build_bitmap(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        caption = QLabel("位状态")
        caption.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(caption)

        self.bit_label = QLabel("--")
        self.bit_label.setWordWrap(True)
        self.bit_label.setTextFormat(Qt.RichText)
        layout.addWidget(self.bit_label)
        return box

    def _build_commands(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        caption = QLabel("命令（写命令码，点一下即单发）")
        caption.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(caption)

        grid = QGridLayout()
        grid.setSpacing(6)
        for index, (value, label) in enumerate(self._commands):
            button = QPushButton(f"{label}　0x{value:X}")
            button.setMinimumHeight(28)
            button.setToolTip(f"写入 {value}（0x{value:X}）")
            button.clicked.connect(
                lambda _checked=False, v=value: self.writeRequested.emit(self.pid, v)
            )
            grid.addWidget(button, index // 3, index % 3)
            self.command_buttons.append(button)
        layout.addLayout(grid)
        return box

    def _build_bit_toggles(self) -> QWidget:
        """位掩码点位（如功能配置字）：勾选各个位，凑出要写入的值。

        勾选状态默认跟「位状态」（设备当前值）同步：收到新值就跟着变，
        直到用户自己动过某个位为止；动过以后保留用户的改动，位状态里标出
        「待置位 / 待清零」，避免一边勾一边被刷新覆盖。
        """
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(5)
        caption = QLabel("按位设置（勾选后点「应用位设置」写入；勾选默认跟当前值同步）")
        caption.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(caption)

        grid = QGridLayout()
        grid.setSpacing(4)
        for index, (bit, label) in enumerate(self._bit_labels):
            check = QCheckBox(f"bit{bit}　{label}")
            check.toggled.connect(self._on_bit_toggled)
            self._bit_boxes[bit] = check
            grid.addWidget(check, index // 2, index % 2)
        layout.addLayout(grid)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.bit_preview = QLabel("将要写入：0（0x0）")
        self.bit_preview.setStyleSheet(f"color: {theme.TEXT_DIM};")
        row.addWidget(self.bit_preview)
        row.addStretch(1)
        self.reset_bits_button = QPushButton("按当前值重填")
        self.reset_bits_button.setToolTip("丢掉还没写入的勾选，重新按设备当前值勾一遍")
        self.reset_bits_button.clicked.connect(self.reset_bits)
        row.addWidget(self.reset_bits_button)
        apply_button = QPushButton("应用位设置")
        apply_button.clicked.connect(self._apply_bits)
        row.addWidget(apply_button)
        layout.addLayout(row)
        return box

    def _build_write(self, default_interval_ms: int, allow_value: bool = True) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        caption = QLabel("写入值" if allow_value else "持续发送")
        caption.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(caption)

        if allow_value:
            row = QHBoxLayout()
            row.setSpacing(6)
            if self.point.data_type == "bool":
                self.bool_box = QCheckBox("置位（勾选 = 1）")
                row.addWidget(self.bool_box)
            else:
                self.value_box = QDoubleSpinBox()
                integer = self.point.data_type.startswith(("int", "uint"))
                self.value_box.setDecimals(0 if integer else max(0, self.point.decimals))
                self.value_box.setRange(-1e12, 1e12)
                self.value_box.setSingleStep(1.0)
                self.value_box.setKeyboardTracking(False)
                if self.point.unit:
                    self.value_box.setSuffix(f" {self.point.unit}")
                row.addWidget(self.value_box, 1)
            use_current = QPushButton("取当前值")
            use_current.setToolTip("把当前值填进输入框，便于在此基础上微调")
            use_current.clicked.connect(self._fill_current)
            row.addWidget(use_current)
            layout.addLayout(row)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        if allow_value:
            single = QPushButton("单发写入")
            single.setToolTip("只写一次")
            single.clicked.connect(self._emit_single)
            actions.addWidget(single)
        else:
            hint = QLabel("按上面勾选的位持续下发")
            hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
            actions.addWidget(hint)

        actions.addWidget(QLabel("周期"))
        self.interval_box = QSpinBox()
        self.interval_box.setRange(PERIODIC_MIN_MS, PERIODIC_MAX_MS)
        self.interval_box.setSingleStep(100)
        self.interval_box.setValue(int(self._initial_interval(default_interval_ms)))
        self.interval_box.setSuffix(" ms")
        self.interval_box.setToolTip("持续发送的间隔；受通道轮询节奏限制，不会比轮询更快")
        actions.addWidget(self.interval_box)

        self.periodic_button = QPushButton("开始持续发送")
        self.periodic_button.clicked.connect(self._emit_periodic)
        actions.addWidget(self.periodic_button)

        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(lambda: self.periodicStopRequested.emit(self.pid))
        actions.addWidget(self.stop_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return box

    def _build_footer(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.periodic_label = QLabel("")
        self.periodic_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.periodic_label.setWordWrap(True)
        layout.addWidget(self.periodic_label)

        row = QHBoxLayout()
        row.setSpacing(6)
        edit_button = QPushButton("编辑点位定义")
        edit_button.setToolTip("到「设备与点位」页修改地址、类型、量程等")
        edit_button.clicked.connect(lambda: self.definitionRequested.emit(self.pid))
        row.addWidget(edit_button)
        row.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        row.addWidget(close_button)
        layout.addLayout(row)
        return box

    # -------------------------------------------------- 交互

    def _initial_interval(self, fallback: int) -> int:
        """周期默认值：优先用正在发送的周期，其次是工程里记住的，最后是软件设置。"""
        task = self._periodic_of(self.pid)
        if task is not None:
            return max(PERIODIC_MIN_MS, int(task.interval_ms))
        if self._project_entry is not None:
            return max(PERIODIC_MIN_MS, int(self._project_entry.interval_ms))
        return max(PERIODIC_MIN_MS, int(fallback))

    def _fill_current(self) -> None:
        tag = self._value_of(self.pid)
        if self.bool_box is not None:
            self.bool_box.setChecked(bool(tag.value))
            return
        if self.value_box is not None and tag.value is not None:
            try:
                self.value_box.setValue(float(tag.value))
            except (TypeError, ValueError):
                pass

    def _current_input(self) -> Any:
        """当前要写入的值。

        命令点位的值只能来自命令按钮（``_commands`` 非空时这里返回 None）；
        位掩码点位取勾选出的组合值。
        """
        if self._commands:
            return None
        if self._bit_boxes:
            return self._bits_value()
        if self.bool_box is not None:
            return self.bool_box.isChecked()
        if self.value_box is not None:
            value = self.value_box.value()
            return int(value) if self.point.data_type.startswith(("int", "uint")) else value
        return None

    def _emit_single(self) -> None:
        value = self._current_input()
        if value is not None:
            self.writeRequested.emit(self.pid, value)

    def _emit_periodic(self) -> None:
        value = self._current_input()
        if value is not None:
            self.periodicRequested.emit(self.pid, value, self.interval_box.value())

    def _on_bit_toggled(self, _checked: bool = False) -> None:
        self._bits_dirty = True
        self._sync_bit_preview()
        self._refresh_bit_states()

    def _bits_value(self) -> int:
        value = 0
        for bit, check in self._bit_boxes.items():
            if check.isChecked():
                value |= 1 << bit
        return value

    def _live_bits(self) -> int | None:
        """设备当前值换算成整数（没有值/不是数字时返回 None）。"""
        tag = self._value_of(self.pid)
        if tag.value is None:
            return None
        try:
            return int(tag.value)
        except (TypeError, ValueError):
            return None

    def _sync_bits_to_live(self, live: int) -> None:
        """把勾选调成设备当前的值（用户没动过的时候）。"""
        for bit, check in self._bit_boxes.items():
            wanted = bool(live & (1 << bit))
            if check.isChecked() != wanted:
                check.blockSignals(True)
                check.setChecked(wanted)
                check.blockSignals(False)

    def reset_bits(self) -> None:
        """「按当前值重填」：丢掉还没写入的勾选。"""
        live = self._live_bits()
        if live is None:
            return
        self._bits_dirty = False
        self._sync_bits_to_live(live)
        self._sync_bit_preview()
        self._refresh_bit_states()

    def _sync_bit_preview(self) -> None:
        pending = self._bits_value()
        live = self._live_bits()
        if live is None:
            text = f"将要写入：{pending}（0x{pending:X}）"
            colour = theme.TEXT_DIM
        elif pending == live:
            text = f"与当前值一致：{pending}（0x{pending:X}）"
            colour = theme.TEXT_DIM
        else:
            text = f"当前 {live}（0x{live:X}）　→　将要写入 {pending}（0x{pending:X}）"
            colour = theme.QUALITY_UNCERTAIN
        self.bit_preview.setText(text)
        self.bit_preview.setStyleSheet(f"color: {colour};")

    def _refresh_bit_states(self) -> None:
        """位状态：已置位高亮；用户动过勾选时，额外标出「待置位 / 待清零」。"""
        if not self._bit_labels or not hasattr(self, "bit_label"):
            return
        tag = self._value_of(self.pid)
        active = set(self.point.decode_bits(tag.value, self._bit_labels))
        dirty = bool(self._bit_boxes) and self._bits_dirty
        pending = {
            label
            for bit, label in self._bit_labels
            if bit in self._bit_boxes and self._bit_boxes[bit].isChecked()
        }
        parts: list[str] = []
        for bit, label in self._bit_labels:
            is_on = label in active
            want_on = label in pending if dirty else is_on
            mark = ""
            if not dirty or is_on == want_on:
                colour = theme.ACCENT if is_on else theme.BORDER
            elif want_on:
                colour = theme.QUALITY_UNCERTAIN
                mark = " ↑"
            else:
                colour = theme.QUALITY_STALE
                mark = " ↓"
            parts.append(f"<span style='color:{colour}'>bit{bit} {label}{mark}</span>")
        if dirty:
            parts.append(
                f"<span style='color:{theme.TEXT_DIM}'>（↑ 待置位 / ↓ 待清零）</span>"
            )
        self.bit_label.setText("　".join(parts))

    def _apply_bits(self) -> None:
        self.writeRequested.emit(self.pid, self._bits_value())
        self.bit_preview.setText(
            f"已下发 {self._bits_value()}（0x{self._bits_value():X}），等设备回读…"
        )

    # -------------------------------------------------- 刷新

    def refresh(self) -> None:
        """定时把当前值、位状态、持续发送进度刷新到界面上。"""
        tag = self._value_of(self.pid)
        text = (
            format_value(tag.value, self.point.decimals, self.point.display_base)
            if tag.value is not None
            else "--"
        )
        suffix = f" {self.point.unit}" if self.point.unit else ""
        quality = QUALITY_TEXT.get(tag.quality, tag.quality)
        colour = theme.TEXT if tag.quality == "good" else theme.QUALITY_BAD
        self.current_label.setText(f"当前值：{text}{suffix}　（{quality}）")
        self.current_label.setStyleSheet(f"color: {colour}; font-size: 13px;")

        if self._bit_labels:
            live = self._live_bits()
            if self._bit_boxes:
                pending = self._bits_value()
                # 设备已经回读成我们刚写的值：不再算"未写入的改动"
                if live is not None and self._bits_dirty and pending == live:
                    self._bits_dirty = False
                if not self._bits_dirty and live is not None:
                    self._sync_bits_to_live(live)
                self._sync_bit_preview()
            self._refresh_bit_states()
            active = set(self.point.decode_bits(tag.value, self._bit_labels))
            if not self._bit_boxes and not active and tag.value is None:
                self.bit_label.setText("--")

        if self.bool_box is not None and tag.value is not None:
            self.bool_box.setChecked(bool(tag.value))

        self._refresh_periodic()

    def _refresh_periodic(self) -> None:
        task = self._periodic_of(self.pid)
        has_write = self.point.writable and not self._commands
        if task is None:
            if self._project_entry is not None:
                self.periodic_label.setText(
                    f"工程里记着一条持续发送配置（{self._project_entry.value} / "
                    f"{self._project_entry.interval_ms} ms），当前未启动。"
                )
            else:
                self.periodic_label.setText("持续发送：未启用")
            if has_write:
                self.periodic_button.setText("开始持续发送")
                self.stop_button.setEnabled(False)
            return

        state = "发送中" if task.enabled else "已暂停"
        detail = (
            f"持续发送：{state}　每 {task.interval_ms} ms 写 {task.value}　已发 {task.sent} 次"
        )
        if task.failed:
            detail += f"　失败 {task.failed} 次"
        if task.last_error:
            detail += f"\n最近错误：{task.last_error}"
        self.periodic_label.setText(detail)
        self.periodic_label.setStyleSheet(
            f"color: {theme.QUALITY_BAD if task.last_error else theme.TEXT_DIM}; font-size: 11px;"
        )
        if has_write:
            self.periodic_button.setText("更新并继续发送")
            self.stop_button.setEnabled(True)

    # -------------------------------------------------- 收尾

    def closeEvent(self, event) -> None:  # noqa: item2
        """关闭时停掉刷新定时器。

        不停的话窗口关了它还在每 400ms 读实时表刷界面，纯浪费；
        而且对话框对象被释放后定时器还在跑更容易出问题。
        """
        self._timer.stop()
        super().closeEvent(event)

    # -------------------------------------------------- 外部注入

    def set_project_entry(self, entry) -> None:
        """把工程里记住的持续发送配置传进来（用于未启动时的显示与周期默认值）。"""
        self._project_entry = entry
        if entry is not None and self._periodic_of(self.pid) is None:
            self.interval_box.setValue(max(PERIODIC_MIN_MS, int(entry.interval_ms)))
        self.refresh()
