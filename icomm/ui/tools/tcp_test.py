"""TCP 连接测试：现场判断"网关通不通、端口开没开"。

比 ping 更有用的地方在于：ping 只说明 IP 通，而现场的问题常常是
**端口没开 / 被防火墙挡 / 网关把 Modbus 端口映射错了**。这里直接做 TCP 连接，
并且能手工发一串字节看回什么（Modbus 网关、串口服务器都能这么试）。

逻辑与界面分开：连接测试的核心在 :mod:`.core.net_check`（可单测、可命令行复用），
这里只做界面。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.net_check import ConnectionTester, ProbeResult, parse_hex_payload
from .. import theme
from .base import DialogTool, ToolContext, register_tool


class _ProbeWorker(QObject):
    """把连接测试放到后台线程，免得界面卡住（连接超时可能有几秒）。"""

    finished = Signal(object)  # ProbeResult

    def __init__(self, tester: ConnectionTester) -> None:
        super().__init__()
        self.tester = tester

    def run(self) -> None:
        self.finished.emit(self.tester.run())


class TcpTestDialog(QDialog):
    """TCP 连接 / 收发字节测试。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self._thread: QThread | None = None
        self._worker: _ProbeWorker | None = None

        self.setWindowTitle("TCP 连接测试")
        self.setMinimumSize(680, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        root.addWidget(self._build_target())
        root.addWidget(self._build_payload())
        root.addWidget(self._build_result(), 1)

        buttons = QDialogButtonBox()
        self.run_button = buttons.addButton("开始测试", QDialogButtonBox.ActionRole)
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.start_test)
        buttons.addButton("关闭", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        root.addWidget(buttons)

        self._prefill()

    # -------------------------------------------------- 界面

    def _build_target(self) -> QWidget:
        box = QGroupBox("目标")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.host_edit = QLineEdit("127.0.0.1")
        self.host_edit.setPlaceholderText("IP 或主机名")
        row.addWidget(self.host_edit, 1)

        row.addWidget(QLabel("端口"))
        self.port_box = QSpinBox()
        self.port_box.setRange(1, 65535)
        self.port_box.setValue(502)
        self.port_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        row.addWidget(self.port_box)
        wrapper = QWidget()
        wrapper.setLayout(row)
        form.addRow("地址", wrapper)

        timeout_row = QHBoxLayout()
        timeout_row.setSpacing(6)
        self.timeout_box = QSpinBox()
        self.timeout_box.setRange(1, 60)
        self.timeout_box.setValue(3)
        self.timeout_box.setSuffix(" 秒")
        self.timeout_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        timeout_row.addWidget(self.timeout_box)
        self.repeat_box = QSpinBox()
        self.repeat_box.setRange(1, 20)
        self.repeat_box.setValue(1)
        self.repeat_box.setPrefix("连 ")
        self.repeat_box.setSuffix(" 次")
        self.repeat_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.repeat_box.setToolTip("连多次可以看出抖动：偶发失败现场很常见")
        timeout_row.addWidget(self.repeat_box)
        timeout_row.addStretch(1)

        self.from_channel = QCheckBox("用工程里的通道参数")
        self.from_channel.setToolTip("选一条已有 TCP 通道，自动填上它的地址与端口")
        self.from_channel.toggled.connect(self._prefill)
        timeout_row.addWidget(self.from_channel)

        self.channel_combo = None
        from PySide6.QtWidgets import QComboBox

        self.channel_combo = QComboBox()
        for channel in context_channels(self.context):
            self.channel_combo.addItem(channel[0], channel[1])
        timeout_row.addWidget(self.channel_combo)
        timeout_wrapper = QWidget()
        timeout_wrapper.setLayout(timeout_row)
        form.addRow("超时 / 次数", timeout_wrapper)
        return box

    def _build_payload(self) -> QWidget:
        box = QGroupBox("发送内容（可留空：只测连接）")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        hint = QLabel(
            "十六进制，可带空格或 0x。留空则只做 TCP 连接测试。\n"
            "例（读保持寄存器，带 CRC）：01 03 00 00 00 0A C5 CD"
        )
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(hint)

        self.payload_edit = QPlainTextEdit()
        self.payload_edit.setPlaceholderText("留空 = 只测连接")
        self.payload_edit.setFixedHeight(72)
        layout.addWidget(self.payload_edit)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.wait_box = QCheckBox("发送后等回应")
        self.wait_box.setChecked(True)
        row.addWidget(self.wait_box)
        row.addWidget(QLabel("最多等"))
        self.read_timeout_box = QSpinBox()
        self.read_timeout_box.setRange(1, 60)
        self.read_timeout_box.setValue(3)
        self.read_timeout_box.setSuffix(" 秒")
        self.read_timeout_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        row.addWidget(self.read_timeout_box)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _build_result(self) -> QWidget:
        box = QGroupBox("结果")
        layout = QVBoxLayout(box)
        self.result_label = QLabel("尚未测试")
        self.result_label.setWordWrap(True)
        self.result_label.setTextFormat(Qt.RichText)
        layout.addWidget(self.result_label)

        self.detail_edit = QPlainTextEdit()
        self.detail_edit.setReadOnly(True)
        self.detail_edit.setPlaceholderText("测试明细会显示在这里")
        layout.addWidget(self.detail_edit, 1)
        return box

    # -------------------------------------------------- 逻辑

    def _prefill(self) -> None:
        """勾上「用工程里的通道参数」就把选中通道的地址端口填进来。"""
        if not self.from_channel.isChecked() or self.channel_combo is None:
            return
        options = self.channel_combo.currentData() or {}
        host = str(options.get("host", "") or "")
        port = options.get("port", 502)
        if host:
            self.host_edit.setText(host)
        try:
            self.port_box.setValue(int(port))
        except (TypeError, ValueError):
            pass

    def _tester(self) -> ConnectionTester:
        payload, error = parse_hex_payload(self.payload_edit.toPlainText())
        if error:
            self.result_label.setText(f"<span style='color:{theme.QUALITY_BAD}'>{error}</span>")
            raise ValueError(error)
        return ConnectionTester(
            host=self.host_edit.text().strip(),
            port=self.port_box.value(),
            timeout_s=float(self.timeout_box.value()),
            attempts=self.repeat_box.value(),
            payload=payload,
            wait_response=self.wait_box.isChecked(),
            read_timeout_s=float(self.read_timeout_box.value()),
        )

    def start_test(self) -> None:
        if self._thread is not None:
            return  # 上一次还在跑
        try:
            tester = self._tester()
        except ValueError:
            return

        self.run_button.setEnabled(False)
        self.result_label.setText("测试中…")
        self.detail_edit.clear()

        self._thread = QThread(self)
        self._worker = _ProbeWorker(tester)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def _on_finished(self, result: ProbeResult) -> None:
        colour = theme.QUALITY_GOOD if result.ok else theme.QUALITY_BAD
        self.result_label.setText(f"<span style='color:{colour}'><b>{result.headline}</b></span>")
        self.detail_edit.setPlainText(result.detail())
        self.context.report(f"TCP 测试 {result.host}:{result.port} —— {result.headline}")
        self._teardown()

    def _teardown(self) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
        self._worker = None
        self.run_button.setEnabled(True)

    def done(self, code: int) -> None:  # noqa: D102
        self._teardown()
        super().done(code)


def context_channels(context: ToolContext) -> list[tuple[str, dict]]:
    """工程里可用的 TCP 通道 ``[(显示名, options)]``。"""
    entries: list[tuple[str, dict]] = []
    for channel in context.project.channels:
        if channel.link in ("tcp", "rtu_over_tcp"):
            label = f"{channel.name}（{channel.options.get('host', '-')}:{channel.options.get('port', '-')}）"
            entries.append((label, dict(channel.options)))
    return entries


@register_tool
class TcpTestTool(DialogTool):
    name = "TCP 连接测试"
    group = "通信"
    order = 10
    description = "测端口通不通，可手工发字节看回应"

    def build(self, context: ToolContext) -> QDialog:
        return TcpTestDialog(context)
