"""「Modbus 报文与 CRC」工具：拼帧、算 CRC、解析报文。

现场调试最常干两件事：

1. 拼一帧发给设备、或把帧发给厂家核对 —— 这里把 CRC 自动算好并**高亮**，
   同时把初值/多项式/传输字节序列出来，避免"到底是 CRC-16/ARC 还是
   CRC-16/MODBUS"扯不清；
2. 抓回来一串十六进制看不懂 —— 这里解析成"从站号 / 功能码 / 地址 / 数据 /
   CRC 对不对"加一句中文说明。

RTU 与 TCP 共用同一套构造器与解析器：切换模式时用同一份"功能码 + 数据段"
重新生成报文，所以"同一件事的两种报文"可以直接对照。

纯逻辑都在 :mod:`icomm.core.modbus_tools` 里，这个文件只管界面与交互。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QScrollArea,
)

from ...core import modbus_tools as mb
from .. import theme
from .base import DialogTool, ToolContext, register_tool

#: 构造器支持的功能码 -> 值输入控件在 QStackedWidget 里的页号
_VALUE_PAGES = {"count": 0, "coil": 1, "value": 2, "bits": 3, "registers": 4}

MODE_RTU = "rtu"
MODE_TCP = "tcp"

#: 报文与十六进制一律用等宽字体，位数好对齐（沿用主题里的等宽候选，不硬编码字体名）
MONO_FONT = theme.FONT_FAMILIES[3]


class ModbusFrameDialog(QDialog):
    """报文构造 + CRC 明细 + 报文解析，三块放在一个窗口里。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        #: 最近一次成功拼出的报文与它的 (功能码, 数据段)，模式切换时拿它重算
        self._frame = b""
        self._pdu: tuple[int, bytes] | None = None

        self.setWindowTitle("Modbus 报文与 CRC")
        self.setMinimumSize(0, 0)
        self.resize(900, 820)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        self.tabs.addTab(scroll, "报文与 CRC")

        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        root.addWidget(self._build_header())
        root.addWidget(self._build_mode_box())
        root.addWidget(self._build_builder_box())
        root.addWidget(self._build_frame_box())
        root.addWidget(self._build_parse_box(), 1)
        root.addWidget(self._build_footer())

        self._on_function_changed()
        self._on_mode_changed()

        # 从机调试页：延迟导入，避免和本模块的常量/类互相 import
        from .modbus_slave_panel import SlaveDebugPanel

        self.slave_panel = SlaveDebugPanel(
            frame_provider=self.frame_bytes, mode_provider=self.mode
        )
        slave_scroll = QScrollArea()
        slave_scroll.setWidgetResizable(True)
        slave_scroll.setFrameShape(QFrame.NoFrame)
        slave_scroll.setWidget(self.slave_panel)
        self.tabs.addTab(slave_scroll, "从机调试")

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        """关窗口一定要停从机服务，别留个端口在后台听着。"""
        panel = getattr(self, "slave_panel", None)
        if panel is not None:
            panel.shutdown()
        super().closeEvent(event)

    # -------------------------------------------------- 界面骨架

    def _build_header(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        title = QLabel("Modbus 报文与 CRC")
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 15px; font-weight: 600;")
        layout.addWidget(title)

        hint = QLabel(
            "RTU 帧 = 从站号 + 功能码 + 数据 + CRC（低字节在前）；"
            "TCP 帧 = MBAP 头 7 字节 + PDU（长度域 = 从站号 1 + PDU）。"
            "两个方向共用下面的模式开关。"
        )
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return box

    def _build_mode_box(self) -> QWidget:
        box = QGroupBox("报文模式")
        layout = QHBoxLayout(box)
        layout.setContentsMargins(theme_pad(), theme_pad(), theme_pad(), theme_pad())
        layout.setSpacing(12)

        self.mode_group = QButtonGroup(self)
        self.rtu_radio = QRadioButton("RTU（带 CRC16）")
        self.tcp_radio = QRadioButton("TCP（带 MBAP 头）")
        self.rtu_radio.setChecked(True)
        self.rtu_radio.setToolTip("串口/RTU over TCP：帧尾 2 字节 CRC，低字节在前")
        self.tcp_radio.setToolTip("Modbus TCP：7 字节 MBAP 头，报文本身不带 CRC")
        for index, radio in enumerate((self.rtu_radio, self.tcp_radio)):
            self.mode_group.addButton(radio, index)
            layout.addWidget(radio)
        self.mode_group.idToggled.connect(lambda _id, checked: checked and self._on_mode_changed())

        self.transaction_label = QLabel("事务号")
        self.transaction_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        layout.addWidget(self.transaction_label)
        self.transaction_box = QSpinBox()
        self.transaction_box.setRange(0, 0xFFFF)
        self.transaction_box.setValue(1)
        self.transaction_box.setToolTip("MBAP 头里的事务号，用来把请求和响应配对；RTU 模式下无效")
        self.transaction_box.valueChanged.connect(self._refresh_frame)
        layout.addWidget(self.transaction_box)

        hint = QLabel("切换模式会按当前功能码与数据重算报文")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(hint)
        layout.addStretch(1)
        return box

    def _build_builder_box(self) -> QWidget:
        box = QGroupBox("构造器：选功能码填参数，报文自动刷新")
        grid = QGridLayout(box)
        grid.setContentsMargins(theme_pad(), theme_pad(), theme_pad(), theme_pad())
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)

        self.slave_box = QSpinBox()
        self.slave_box.setRange(0, 255)
        self.slave_box.setValue(1)
        self.slave_box.setToolTip("从站号（RTU 从站地址 / TCP 单元号）")
        self.slave_box.valueChanged.connect(self._refresh_frame)
        grid.addWidget(_caption("从站号"), 0, 0)
        grid.addWidget(self.slave_box, 0, 1)

        self.function_combo = QComboBox()
        for code in sorted(mb.FUNCTION_FIELDS):
            self.function_combo.addItem(f"0x{code:02X}　{mb.function_name(code)}", code)
        self.function_combo.setToolTip("左起 8 个常用功能码；其它功能码在下面的解析器里认得")
        self.function_combo.currentIndexChanged.connect(self._on_function_changed)
        grid.addWidget(_caption("功能码"), 1, 0)
        grid.addWidget(self.function_combo, 1, 1)

        self.address_edit = QLineEdit("0x0000")
        self.address_edit.setPlaceholderText("如 0 或 0x0000（0x 前缀表示十六进制）")
        self.address_edit.textChanged.connect(self._refresh_frame)
        self.address_caption = _caption("起始地址")
        grid.addWidget(self.address_caption, 2, 0)
        grid.addWidget(self.address_edit, 2, 1)

        # 值/数量的输入方式随功能码变化，用堆叠页避免一堆控件挤在一起
        self.count_edit = QLineEdit("10")
        self.count_edit.setPlaceholderText("一次读多少个")
        self.coil_combo = QComboBox()
        self.coil_combo.addItem("ON（0xFF00）", 0xFF00)
        self.coil_combo.addItem("OFF（0x0000）", 0x0000)
        self.value_edit = QLineEdit("0")
        self.value_edit.setPlaceholderText("如 100 或 0x0064")
        self.bits_edit = QLineEdit("1, 0, 1, 1")
        self.bits_edit.setPlaceholderText("每个线圈 0/1，也可直接粘 1011 或 FF 0F")
        self.regs_edit = QLineEdit("0x0001, 0x0002")
        self.regs_edit.setPlaceholderText("每个寄存器 16 位，如 0x0001, 0x0002")

        self.value_stack = QStackedWidget()
        for widget in (
            self.count_edit,
            self.coil_combo,
            self.value_edit,
            self.bits_edit,
            self.regs_edit,
        ):
            self.value_stack.addWidget(widget)
            if isinstance(widget, QLineEdit):
                widget.textChanged.connect(self._refresh_frame)
        self.value_caption = _caption("数量")
        grid.addWidget(self.value_caption, 3, 0)
        grid.addWidget(self.value_stack, 3, 1)

        self.builder_hint = QLabel("")
        self.builder_hint.setStyleSheet(f"color: {theme.QUALITY_BAD}; font-size: 11px;")
        self.builder_hint.setWordWrap(True)
        grid.addWidget(self.builder_hint, 4, 0, 1, 2)
        return box

    def _build_frame_box(self) -> QWidget:
        box = QGroupBox("报文")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(theme_pad(), theme_pad(), theme_pad(), theme_pad())
        layout.setSpacing(6)

        self.frame_view = QTextEdit()
        self.frame_view.setReadOnly(True)
        self.frame_view.setFrameShape(QFrame.NoFrame)
        self.frame_view.setFixedHeight(46)
        self.frame_view.setStyleSheet(mono_area_style())
        self.frame_view.setToolTip("等宽字体显示；CRC 字节（RTU）或 MBAP 长度域（TCP）已高亮")
        layout.addWidget(self.frame_view)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.copy_button = QPushButton("复制报文")
        self.copy_button.setToolTip("复制纯十六进制文本，便于贴给别人或抓包工具对照")
        self.copy_button.clicked.connect(self._copy_frame)
        row.addWidget(self.copy_button)

        self.frame_summary = QLabel("")
        self.frame_summary.setStyleSheet(f"color: {theme.ACCENT};")
        self.frame_summary.setWordWrap(True)
        row.addWidget(self.frame_summary, 1)
        layout.addLayout(row)

        self.crc_label = QLabel("")
        self.crc_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.crc_label.setWordWrap(True)
        layout.addWidget(self.crc_label)
        return box

    def _build_parse_box(self) -> QWidget:
        box = QGroupBox("解析器：粘一帧十六进制，这里给出结论")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(theme_pad(), theme_pad(), theme_pad(), theme_pad())
        layout.setSpacing(6)

        self.parse_input = QPlainTextEdit()
        self.parse_input.setFrameShape(QFrame.NoFrame)
        self.parse_input.setPlaceholderText(
            "例如 01 03 00 00 00 0A C5 CD　（空格/逗号/换行/0x 前缀都认）"
        )
        self.parse_input.setFixedHeight(50)
        self.parse_input.setStyleSheet(mono_area_style())
        self.parse_input.textChanged.connect(self._on_parse_changed)
        layout.addWidget(self.parse_input)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.parse_summary = QLabel("")
        self.parse_summary.setStyleSheet(f"color: {theme.ACCENT};")
        self.parse_summary.setWordWrap(True)
        self.parse_summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row.addWidget(self.parse_summary, 1)
        self.detect_button = QPushButton("按当前报文填入")
        self.detect_button.setToolTip("把刚拼出来的报文放进解析框，用来对照解析结果")
        self.detect_button.clicked.connect(self._fill_parse_with_frame)
        row.addWidget(self.detect_button)
        layout.addLayout(row)

        self.parse_detail = QPlainTextEdit()
        self.parse_detail.setReadOnly(True)
        self.parse_detail.setFrameShape(QFrame.NoFrame)
        self.parse_detail.setMinimumHeight(112)
        self.parse_detail.setStyleSheet(mono_area_style())
        layout.addWidget(self.parse_detail, 1)
        return box

    def _build_footer(self) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        note = QLabel(
            "解析按上面的「报文模式」进行：粘错模式时会看到明确的错（如 MBAP 长度域不符、CRC 错误）"
        )
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        note.setWordWrap(True)
        layout.addWidget(note, 1)

        close = QPushButton("关闭")
        close.clicked.connect(self.close)
        layout.addWidget(close)
        return box

    # -------------------------------------------------- 构造器 -> 报文

    def mode(self) -> str:
        return MODE_RTU if self.rtu_radio.isChecked() else MODE_TCP

    def set_mode(self, mode: str) -> None:
        """供测试 / 外部调用：切模式（和点单选框等价）。"""
        (self.rtu_radio if mode == MODE_RTU else self.tcp_radio).setChecked(True)

    def set_function(self, code: int) -> None:
        index = self.function_combo.findData(code)
        if index >= 0:
            self.function_combo.setCurrentIndex(index)

    def frame_bytes(self) -> bytes:
        return self._frame

    def frame_hex(self) -> str:
        return mb.to_hex_text(self._frame)

    def parse_detail_text(self) -> str:
        return self.parse_detail.toPlainText()

    def parse_summary_text(self) -> str:
        return self.parse_summary.text()

    def _on_function_changed(self) -> None:
        code = self.function_combo.currentData()
        if code is None:
            return
        address_caption, value_caption, kind = mb.FUNCTION_FIELDS[code]
        self.address_caption.setText(address_caption)
        self.value_caption.setText(value_caption)
        self.value_stack.setCurrentIndex(_VALUE_PAGES[kind])
        if code == 0x05:
            self.count_edit.setPlaceholderText("一次读多少个")
        elif code in (0x01, 0x02):
            self.count_edit.setPlaceholderText(f"1~{mb.MAX_READ_BITS} 位")
        elif code in (0x03, 0x04):
            self.count_edit.setPlaceholderText(f"1~{mb.MAX_READ_REGISTERS} 个寄存器")
        self._refresh_frame()

    def _on_mode_changed(self) -> None:
        tcp = self.mode() == MODE_TCP
        self.transaction_box.setEnabled(tcp)
        self.transaction_label.setStyleSheet(
            f"color: {theme.TEXT if tcp else theme.TEXT_DIM};"
        )
        # 已有一帧时按"同一份功能码 + 数据段"换格式重算，做到 RTU <-> TCP 对照
        if self._pdu is None:
            self._refresh_frame()
        else:
            function, payload = self._pdu
            try:
                frame = self._compose(function, payload)
            except ValueError as exc:
                self._show_problem(str(exc))
                return
            self._publish(frame)
        self._on_parse_changed()

    def _compose(self, function: int, payload: bytes) -> bytes:
        unit = self.slave_box.value()
        if self.mode() == MODE_TCP:
            return mb.build_tcp(self.transaction_box.value(), unit, function, payload)
        return mb.build_rtu(unit, function, payload)

    def _refresh_frame(self) -> None:
        payload = self._build_payload()
        if payload is None:
            return
        function, data = payload
        try:
            frame = self._compose(function, data)
        except ValueError as exc:
            self._show_problem(str(exc))
            return
        self._pdu = (function, data)
        self._publish(frame)

    def _build_payload(self) -> tuple[int, bytes] | None:
        """按当前功能码取参数并拼出数据段；参数不合法时提示并返回 None。"""
        code = self.function_combo.currentData()
        if code is None:
            return None
        kind = mb.FUNCTION_FIELDS[code][2]
        try:
            address = mb.parse_int_text(self.address_edit.text(), "起始地址")
            if kind == "count":
                count = mb.parse_int_text(self.count_edit.text(), "数量")
                payload = mb.read_request(code, address, count)
            elif kind == "coil":
                payload = mb.write_single(code, address, self.coil_combo.currentData())
            elif kind == "value":
                payload = mb.write_single(code, address, mb.parse_int_text(self.value_edit.text(), "寄存器值"))
            elif kind == "bits":
                payload = mb.write_multiple(code, address, self.bits_edit.text())
            else:
                payload = mb.write_multiple(code, address, self.regs_edit.text())
        except ValueError as exc:
            self._show_problem(str(exc))
            return None
        return code, payload

    def _publish(self, frame: bytes) -> None:
        """把拼好的报文与它的说明一起显示出来。"""
        self._frame = frame
        self.builder_hint.setText("")
        self.frame_view.setHtml(self._frame_html(frame))
        info = mb.parse_frame(frame, self.mode())
        self.frame_summary.setText(info.summary)
        self.frame_summary.setStyleSheet(f"color: {theme.ACCENT};")
        self.frame_summary.setToolTip(info.error)
        self.crc_label.setText(self._crc_note(frame, info))

    def _show_problem(self, message: str) -> None:
        """参数不合法：不弹窗打断输入，就在原地写清楚哪里不对。"""
        self._frame = b""
        self._pdu = None
        self.frame_view.setHtml(
            f"<div style='font-family:{MONO_FONT}; color:{theme.TEXT_DIM};'>—</div>"
        )
        self.builder_hint.setText(message)
        self.frame_summary.setText("参数还没填对，报文本地拼不出来")
        self.frame_summary.setStyleSheet(f"color: {theme.QUALITY_BAD};")
        self.crc_label.setText("")

    def _frame_html(self, frame: bytes) -> str:
        if self.mode() == MODE_RTU and len(frame) >= 2:
            parts = [
                _span(mb.to_hex_text(frame[:-2]), theme.TEXT),
                _highlight(mb.to_hex_text(frame[-2:])),
            ]
        elif self.mode() == MODE_TCP and len(frame) >= mb.MBAP_HEADER_LEN:
            # MBAP 头拆开显示：长度域最容易被写错，单独高亮
            parts = [
                _span(mb.to_hex_text(frame[:4]), theme.TEXT_DIM),
                _highlight(mb.to_hex_text(frame[4:6])),
                _span(mb.to_hex_text(frame[6:7]), theme.TEXT_DIM),
                _span(mb.to_hex_text(frame[mb.MBAP_HEADER_LEN :]), theme.TEXT),
            ]
        else:
            parts = [_span(mb.to_hex_text(frame), theme.TEXT)]
        body = " ".join(part for part in parts if part)
        return (
            f"<div style='font-family:{MONO_FONT}; font-size:13px; "
            f"white-space:pre-wrap;'>{body}</div>"
        )

    def _crc_note(self, frame: bytes, info: mb.FrameInfo) -> str:
        if self.mode() == MODE_RTU and len(frame) > 2:
            return "\n".join(
                [
                    *mb.crc_detail(frame[:-2]).split("\n"),
                    f"帧内 CRC 字节：{mb.to_hex_text(frame[-2:])}"
                    f"　总帧长 {len(frame)} 字节 = 数据 {len(frame) - 2} + CRC 2",
                    "把帧尾两字节去掉再算，结果应等于 0x0000（校验通过的判据）"
                    if info.crc_ok
                    else "校验没通过：对照上面的帧内字节与计算值，通常是 CRC 字节序或帧内容被改动",
                ]
            )
        length = int.from_bytes(frame[4:6], "big") if len(frame) >= 6 else 0
        return "\n".join(
            [
                "TCP 报文不带 CRC（校验交给 TCP 链路层）",
                f"MBAP 头 7 字节：事务号 0x{info.transaction or 0:04X}＋协议号 0x0000"
                f"＋长度域 0x{length:04X}＋从站号 {info.unit}（从站号在最末一字节）",
                f"长度域校验：从站号 1 字节 + PDU {max(0, len(frame) - mb.MBAP_HEADER_LEN)} 字节"
                f" = {length}　总帧长 {len(frame)} 字节",
            ]
        )

    def _copy_frame(self) -> None:
        if not self._frame:
            self.frame_summary.setText("还没有可复制的报文")
            self.frame_summary.setStyleSheet(f"color: {theme.QUALITY_BAD};")
            return
        QApplication.clipboard().setText(self.frame_hex())
        self.frame_summary.setText(f"已复制 {len(self._frame)} 字节报文到剪贴板")
        self.frame_summary.setStyleSheet(f"color: {theme.ACCENT};")

    # -------------------------------------------------- 解析器

    def _on_parse_changed(self) -> None:
        text = self.parse_input.toPlainText()
        if not text.strip():
            self.parse_summary.setText("粘贴一帧十六进制报文，这里给出结论（按上面的报文模式解读）")
            self.parse_summary.setStyleSheet(f"color: {theme.TEXT_DIM};")
            self.parse_detail.setPlainText("")
            return
        data, reason = mb.parse_hex_text(text)
        if reason:
            self.parse_summary.setText(f"解析失败：{reason}")
            self.parse_summary.setStyleSheet(f"color: {theme.QUALITY_BAD};")
            self.parse_detail.setPlainText("")
            return
        info = mb.parse_frame(data, self.mode())
        self.parse_summary.setText(info.summary or info.error)
        # 异常响应是"合法但设备报错"：给琥珀色，避免和"帧本身有问题"混在一起
        if not info.is_ok:
            color = theme.QUALITY_BAD
        elif info.is_exception:
            color = theme.QUALITY_STALE
        else:
            color = theme.ACCENT
        self.parse_summary.setStyleSheet(f"color: {color};")
        self.parse_detail.setPlainText("\n".join(info.detail_lines()))

    def _fill_parse_with_frame(self) -> None:
        if not self._frame:
            self.parse_summary.setText("构造器还没拼出报文，先检查上面的参数")
            self.parse_summary.setStyleSheet(f"color: {theme.QUALITY_BAD};")
            return
        self.parse_input.setPlainText(self.frame_hex())


# --------------------------------------------------------------- 小工具

def theme_pad() -> int:
    """分组框内边距跟着界面密度走（和别的对话框保持一致的呼吸感）。"""
    return max(8, theme.appearance().px(10))


def mono_area_style() -> str:
    """文本区样式：等宽字体 + 和 QLineEdit 同一套描边。

    这里必须自己描边：默认边框在深色主题下会露出一条浅色内线，
    看着像"控件没对齐"。字体沿用主题里的等宽候选，不硬编码字体名。
    """
    return (
        f"font-family: {MONO_FONT};"
        f" background: {theme.BG_INPUT};"
        f" border: 1px solid {theme.BORDER};"
        " border-radius: 4px; padding: 4px 8px;"
    )


def _caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setStyleSheet(f"color: {theme.TEXT_DIM};")
    return label


def _span(text: str, color: str) -> str:
    return f'<span style="color:{color};">{text}</span>' if text else ""


def _highlight(text: str) -> str:
    """高亮块：CRC 字节（RTU）与长度域（TCP）都靠它一眼看出来。"""
    if not text:
        return ""
    return (
        f'<span style="background:{theme.ACCENT}; color:#ffffff;'
        f' font-weight:600; padding:0 4px;">{text}</span>'
    )


# --------------------------------------------------------------- 注册

class ModbusFrameTool(DialogTool):
    """菜单里的「Modbus 报文与 CRC」。"""

    name = "Modbus 报文与 CRC"
    group = "Modbus"
    order = 10
    description = "拼帧、算 CRC、解析报文；另带「从机调试」（当从机时怎么应答、可起本地 TCP 从机）"

    def build(self, context: ToolContext | None = None) -> QDialog:
        # 工具不碰工程数据，只用宿主窗口当父窗口，所以 fork 出去单测也能跑
        parent = context.window if context is not None else None
        return ModbusFrameDialog(parent)


register_tool(ModbusFrameTool())
