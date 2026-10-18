"""「从机调试」面板：当这台电脑/这段程序被当成从机时，怎么回主站的请求。

两块内容：

1. **报文级**：贴一帧主站请求（或直接从"报文与 CRC"页取刚拼好的那帧），
   立刻看到"一台守规矩的从机会怎么回"（含异常应答与不应答的情形）；
2. **真跑一个从机**：本地起一个 Modbus TCP 从机，主站（PLC / 上位机 / 现场网关）
   连上来轮询时按下面的寄存器表应答，收到的每一帧与回出去的每一帧都记在日志里。

寄存器表可以直接双击改值 —— 现场常用手法：把某个寄存器改成越限值，
看主站那边的报警/联锁动作对不对。从机改过的值会实时刷回表格（只刷动到的那几行）。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import modbus_slave as ms
from ...core import modbus_tools as mb
from .. import theme
from ..spin import no_arrows
from .modbus_frame import MONO_FONT, MODE_RTU, MODE_TCP

#: 表格列：地址 / 线圈 / 离散输入 / 保持寄存器 / 输入寄存器
COLUMNS = ("地址", "线圈 0x", "离散输入 1x", "保持寄存器 4x", "输入寄存器 3x")

#: 初值选项：显示名 -> SlaveModel.uniform 的 fill
FILLS: tuple[tuple[str, str], ...] = (
    ("全 0", "zero"),
    ("地址递增（0,1,2…）", "increment"),
    ("统一填一个值", "custom"),
)

#: 最多显示多少行（再大就让用户缩小数据区：几千行表格点着难受，也没必要）
MAX_TABLE_ROWS = 600


class SlaveDebugPanel(QWidget):
    """从机设置 + 报文级应答 + 本地从机服务 + 日志。"""

    #: 服务端线程里收到一帧 → 交给界面线程刷新（Qt 自动排队投递）
    exchangeReceived = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        frame_provider: Callable[[], bytes] | None = None,
        mode_provider: Callable[[], str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.model = ms.SlaveModel.uniform(unit_id=1, size=100, fill="zero")
        self.server: ms.TcpSlaveServer | None = None
        self.exchanges: list[ms.SlaveExchange] = []
        self._frame_provider = frame_provider
        self._mode_provider = mode_provider
        self._updating_table = False
        self._last_reply: ms.FrameReply | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        root.addWidget(self._build_intro())
        root.addWidget(self._build_model_box())
        root.addWidget(self._build_frame_box())
        root.addWidget(self._build_server_box())
        root.addWidget(self._build_log_box(), 1)

        self.exchangeReceived.connect(self._on_exchange)
        self._rebuild_model(ask=False)
        self._refresh_table()

    # -------------------------------------------------- 骨架

    def _build_intro(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        title = QLabel("从机调试")
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 15px; font-weight: 600;")
        layout.addWidget(title)
        hint = QLabel(
            "当对面是主站（PLC / 上位机 / 网关）来轮询我们这一侧时用这一页："
            "上面回答「这一帧从机会怎么回」，下面能真的起一个 TCP 从机让它连上来联调。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(hint)
        return box

    def _build_model_box(self) -> QWidget:
        box = QGroupBox("从机设置（数据区）")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(10, 8, 10, 8)
        outer.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("从站号"))
        self.unit_spin = QSpinBox()
        self.unit_spin.setRange(1, 247)
        self.unit_spin.setValue(1)
        self.unit_spin.setToolTip("和请求里的从站号不一致时，从机按规范不应答（主站会等到超时）")
        no_arrows(self.unit_spin)
        row.addWidget(self.unit_spin)

        row.addWidget(QLabel("数据区长度"))
        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 2000)
        self.size_spin.setValue(100)
        self.size_spin.setSuffix(" 个")
        self.size_spin.setToolTip("四个区（线圈/离散输入/保持/输入寄存器）用同一个长度，地址从 0 开始")
        no_arrows(self.size_spin)
        row.addWidget(self.size_spin)

        row.addWidget(QLabel("初值"))
        self.fill_combo = QComboBox()
        for label, _key in FILLS:
            self.fill_combo.addItem(label)
        self.fill_combo.setToolTip("重建数据区时怎么填（改长度会丢掉原来的值）")
        row.addWidget(self.fill_combo)

        self.fill_spin = QSpinBox()
        self.fill_spin.setRange(0, 65535)
        self.fill_spin.setValue(0)
        self.fill_spin.setEnabled(False)
        self.fill_spin.setToolTip("「统一填一个值」用这个值")
        no_arrows(self.fill_spin)
        row.addWidget(self.fill_spin)

        self.writable_box = QCheckBox("允许主站写")
        self.writable_box.setChecked(True)
        self.writable_box.setToolTip("关掉就当作只读设备：写请求一律回异常码 02")
        row.addWidget(self.writable_box)

        rebuild = QPushButton("重建数据区")
        rebuild.setToolTip("按上面的长度与初值重来一遍（会清空当前值）")
        rebuild.clicked.connect(lambda _checked=False: self._rebuild_model(ask=True))
        row.addWidget(rebuild)
        row.addStretch(1)
        outer.addLayout(row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.setMinimumHeight(180)
        outer.addWidget(self.table, 1)

        note = QLabel("双击「保持寄存器 / 输入寄存器」改数值，勾选框改线圈与离散输入 —— 用来造越限、造故障验证主站反应")
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        note.setWordWrap(True)
        outer.addWidget(note)

        self.model_state = QLabel("")
        self.model_state.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        outer.addWidget(self.model_state)
        return box

    def _build_frame_box(self) -> QWidget:
        box = QGroupBox("报文级：给一帧请求，看从机会怎么回")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(QLabel("报文模式"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("RTU（带 CRC16）", MODE_RTU)
        self.mode_combo.addItem("TCP（MBAP 头）", MODE_TCP)
        top.addWidget(self.mode_combo)
        top.addSpacing(12)
        take = QPushButton("取上一页那一帧")
        take.setToolTip("把「报文与 CRC」页当前拼出的请求帧拿过来（并跟着它的模式）")
        take.clicked.connect(lambda _checked=False: self.take_builder_frame())
        top.addWidget(take)
        top.addStretch(1)
        layout.addLayout(top)

        layout.addWidget(QLabel("请求帧（十六进制，可以粘厂家抓的包）"))
        self.request_edit = QPlainTextEdit()
        self.request_edit.setFixedHeight(52)
        self.request_edit.setPlaceholderText("例如：01 03 00 00 00 0A C5 CD")
        self.request_edit.setStyleSheet(f"font-family: '{MONO_FONT}';")
        layout.addWidget(self.request_edit)

        row = QHBoxLayout()
        row.setSpacing(8)
        generate = QPushButton("生成从机应答")
        generate.setObjectName("primary")
        generate.clicked.connect(lambda _checked=False: self.generate_reply())
        row.addWidget(generate)
        self.copy_reply_button = QPushButton("复制应答帧")
        self.copy_reply_button.setEnabled(False)
        self.copy_reply_button.clicked.connect(lambda _checked=False: self._copy_reply())
        row.addWidget(self.copy_reply_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.reply_label = QLabel("应答帧：--")
        self.reply_label.setStyleSheet(f"font-family: '{MONO_FONT}'; color: {theme.ACCENT};")
        self.reply_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reply_label.setWordWrap(True)
        layout.addWidget(self.reply_label)

        self.reply_detail = QPlainTextEdit()
        self.reply_detail.setReadOnly(True)
        self.reply_detail.setFixedHeight(96)
        self.reply_detail.setPlaceholderText("明细会显示在这里：地址范围、数量、异常码、为什么这样回…")
        layout.addWidget(self.reply_detail)
        return box

    def _build_server_box(self) -> QWidget:
        box = QGroupBox("本地从机服务（TCP）")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("监听地址"))
        self.host_edit = QLineEdit("127.0.0.1")
        self.host_edit.setToolTip("只给本机主站连就填 127.0.0.1；要给同网段设备连就填 0.0.0.0")
        self.host_edit.setMaximumWidth(130)
        row.addWidget(self.host_edit)

        row.addWidget(QLabel("端口"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(0, 65535)
        self.port_spin.setValue(1502)
        self.port_spin.setToolTip("标准 Modbus TCP 是 502；1502 这类高位端口不用管理员权限。填 0 让系统挑一个")
        no_arrows(self.port_spin)
        row.addWidget(self.port_spin)

        self.start_button = QPushButton("启动从机")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(lambda _checked=False: self.start_server())
        row.addWidget(self.start_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(lambda _checked=False: self.stop_server())
        row.addWidget(self.stop_button)

        self.server_state = QLabel("未启动")
        self.server_state.setStyleSheet(f"color: {theme.TEXT_DIM};")
        row.addWidget(self.server_state)
        row.addStretch(1)
        layout.addLayout(row)

        self.server_note = QLabel(
            "主站轮询本机时，请求会按上面的数据区应答。只做 TCP：串口（RTU）调试时串口通常被主站或串口助手占着。"
        )
        self.server_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.server_note.setWordWrap(True)
        layout.addWidget(self.server_note)
        return box

    def _build_log_box(self) -> QWidget:
        box = QGroupBox("请求 / 应答日志")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        self.log_table = QTableWidget(0, 5)
        self.log_table.setHorizontalHeaderLabels(["时间", "来源", "请求", "应答", "说明"])
        self.log_table.verticalHeader().setVisible(False)
        self.log_table.setAlternatingRowColors(True)
        self.log_table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.log_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        self.log_table.setMinimumHeight(160)
        layout.addWidget(self.log_table, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.counter_label = QLabel("已应答 0 次，异常 0 次")
        self.counter_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        row.addWidget(self.counter_label)
        row.addStretch(1)
        clear = QPushButton("清空日志")
        clear.clicked.connect(lambda _checked=False: self.clear_log())
        row.addWidget(clear)
        layout.addLayout(row)
        return box

    # -------------------------------------------------- 数据区

    def _fill_value(self) -> str:
        key = FILLS[max(0, self.fill_combo.currentIndex())][1]
        if key == "custom":
            return str(int(self.fill_spin.value()))
        return key

    def _fill_value_text(self) -> str:
        key = FILLS[max(0, self.fill_combo.currentIndex())][1]
        if key == "custom":
            return f"统一填 {int(self.fill_spin.value())}"
        return "全 0" if key == "zero" else "地址递增"

    def _rebuild_model(self, *, ask: bool) -> None:
        size = int(self.size_spin.value())
        self.model.unit_id = int(self.unit_spin.value())
        self.model.writable = self.writable_box.isChecked()
        self.model.resize(size, self._fill_value())
        self._refresh_table()
        if hasattr(self, "model_state"):
            # 不弹模态框（探针/测试里会卡住），只在数据区下面写一句
            prefix = "已重建：" if ask else "数据区："
            self.model_state.setText(
                f"{prefix}{size} 个地址（线圈/离散输入/保持寄存器/输入寄存器各一份），初值 {self._fill_value_text()}"
            )

    def _refresh_table(self) -> None:
        self._updating_table = True
        try:
            total = max(
                len(self.model.coils), len(self.model.discrete),
                len(self.model.holding), len(self.model.input),
            )
            rows = min(total, MAX_TABLE_ROWS)
            self.table.setRowCount(rows)
            for address in range(rows):
                self._fill_row(address)
        finally:
            self._updating_table = False

    def _fill_row(self, address: int) -> None:
        def cell(text: str) -> QTableWidgetItem:
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignCenter)
            return item

        current = self.table.item(address, 0)
        if current is None or current.text() != str(address):
            self.table.setItem(address, 0, cell(str(address)))
        for column, bank in ((1, self.model.coils), (2, self.model.discrete)):
            item = self.table.item(address, column)
            if item is None:
                item = QTableWidgetItem("")
                item.setTextAlignment(Qt.AlignCenter)
                item.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                self.table.setItem(address, column, item)
            item.setCheckState(Qt.Checked if address < len(bank) and bank[address] else Qt.Unchecked)
        for column, bank in ((3, self.model.holding), (4, self.model.input)):
            value = bank[address] if address < len(bank) else 0
            item = self.table.item(address, column)
            if item is None:
                item = QTableWidgetItem()
                item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(address, column, item)
            item.setText(str(value))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table:
            return
        address = item.row()
        column = item.column()
        banks = {
            1: self.model.coils,
            2: self.model.discrete,
            3: self.model.holding,
            4: self.model.input,
        }
        bank = banks.get(column)
        if bank is None or address >= len(bank):
            return
        self._updating_table = True
        try:
            if column in (1, 2):
                bank[address] = 1 if item.checkState() == Qt.Checked else 0
            else:
                try:
                    value = int(str(item.text()).strip() or "0", 0)
                except ValueError:
                    value = bank[address]
                bank[address] = max(0, min(0xFFFF, int(value)))
                item.setText(str(bank[address]))
        finally:
            self._updating_table = False

    def set_unit(self, unit: int) -> None:
        self.unit_spin.setValue(max(1, min(247, int(unit))))
        self.model.unit_id = int(self.unit_spin.value())

    # -------------------------------------------------- 报文级应答

    def take_builder_frame(self) -> None:
        """把「报文与 CRC」页刚拼好的帧拿过来（连同它的模式）。"""
        if self._frame_provider is None:
            return
        try:
            frame = self._frame_provider()
        except Exception:  # noqa: BLE001 - 上一页参数不合法时不要在这里炸
            frame = b""
        if not frame:
            self.reply_detail.setPlainText("上一页现在没有可用的报文（参数还没填对？）")
            return
        if self._mode_provider is not None:
            try:
                index = self.mode_combo.findData(self._mode_provider())
                if index >= 0:
                    self.mode_combo.setCurrentIndex(index)
            except Exception:  # noqa: BLE001
                pass
        self.request_edit.setPlainText(mb.to_hex_text(frame))
        self.generate_reply()

    def set_request_text(self, text: str) -> None:
        self.request_edit.setPlainText(text)

    def current_mode(self) -> str:
        return str(self.mode_combo.currentData() or MODE_RTU)

    def set_mode(self, mode: str) -> None:
        index = self.mode_combo.findData(mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

    def generate_reply(self) -> ms.FrameReply | None:
        """解析请求文本 → 生成应答，界面显示；返回结论对象（便于测试）。"""
        text = self.request_edit.toPlainText().strip()
        self._last_reply = None
        if not text:
            self._show_problem("先贴一帧请求（或点「取上一页那一帧」）")
            return None
        try:
            frame, error = mb.parse_hex_text(text)
        except ValueError as exc:  # pragma: no cover - parse_hex_text 自己不抛
            frame, error = b"", str(exc)
        if error:
            self._show_problem(error)
            return None
        result = ms.respond_frame(frame, self.current_mode(), self.model)
        self._last_reply = result
        self.copy_reply_button.setEnabled(bool(result.reply))
        if result.silent:
            self.reply_label.setText("应答帧：不应答（这是符合规范的行为，主站会超时）")
        elif result.error:
            self.reply_label.setText("应答帧：--")
        else:
            self.reply_label.setText(f"应答帧：{result.reply_hex}")
        lines: list[str] = []
        if result.summary:
            lines.append(result.summary)
        if result.error:
            lines.append(f"❌ {result.error}")
        lines.extend(result.detail)
        if not result.silent and result.reply:
            info = mb.parse_frame(result.reply, self.current_mode())
            lines.append("")
            lines.append("—— 这帧应答解析出来是 ——")
            lines.extend(info.detail_lines())
        self.reply_detail.setPlainText("\n".join(lines))
        self._refresh_table()
        return result

    def _show_problem(self, message: str) -> None:
        self.reply_label.setText("应答帧：--")
        self.copy_reply_button.setEnabled(False)
        self.reply_detail.setPlainText(f"❌ {message}")
        self._last_reply = None

    def _copy_reply(self) -> None:
        if self._last_reply is None or not self._last_reply.reply:
            return
        QApplication.clipboard().setText(self._last_reply.reply_hex)

    # -------------------------------------------------- 从机服务

    def start_server(self) -> int | None:
        """启动本地从机，返回真实端口（失败返回 None 并在界面上说明原因）。"""
        if self.server is not None and self.server.running:
            return self.server.actual_port
        self.model.unit_id = int(self.unit_spin.value())
        self.model.writable = self.writable_box.isChecked()
        server = ms.TcpSlaveServer(
            self.model,
            host=self.host_edit.text().strip() or "127.0.0.1",
            port=int(self.port_spin.value()),
            on_exchange=self.exchangeReceived.emit,
        )
        try:
            port = server.start()
        except OSError as error:
            self.server_state.setText(f"启动失败：{error}")
            self.server_state.setStyleSheet(f"color: {theme.QUALITY_BAD};")
            return None
        self.server = server
        self.server_state.setText(f"运行中：{server.host}:{port}（从站号 {self.model.unit_id}）")
        self.server_state.setStyleSheet(f"color: {theme.QUALITY_GOOD};")
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        return port

    def stop_server(self) -> None:
        server = self.server
        self.server = None
        if server is not None:
            server.stop()
        self.server_state.setText("未启动")
        self.server_state.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    # -------------------------------------------------- 日志

    def _on_exchange(self, exchange: ms.SlaveExchange) -> None:
        self.exchanges.append(exchange)
        self._append_log_row(exchange)
        if exchange.address is not None:
            for address in range(exchange.address, exchange.address + (exchange.quantity or 1)):
                if 0 <= address < self.table.rowCount():
                    self._updating_table = True
                    try:
                        self._fill_row(address)
                    finally:
                        self._updating_table = False
        server = self.server
        answered = server.requests if server is not None else len(self.exchanges)
        exceptions = server.exceptions if server is not None else 0
        self.counter_label.setText(f"已应答 {answered} 次，异常 {exceptions} 次")

    def _append_log_row(self, exchange: ms.SlaveExchange) -> None:
        row = self.log_table.rowCount()
        self.log_table.insertRow(row)
        cells = [
            exchange.time_text,
            exchange.peer,
            mb.to_hex_text(exchange.request),
            mb.to_hex_text(exchange.reply) if exchange.reply else "（不应答）",
            exchange.summary,
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column in (2, 3):
                item.setFont(self._mono_font())
            self.log_table.setItem(row, column, item)
        self.log_table.scrollToBottom()

    def _mono_font(self):
        from PySide6.QtGui import QFont

        font = QFont(MONO_FONT)
        font.setStyleHint(QFont.Monospace)
        return font

    def clear_log(self) -> None:
        self.exchanges.clear()
        self.log_table.setRowCount(0)
        self.counter_label.setText("已应答 0 次，异常 0 次")
        if self.server is not None:
            self.server.requests = 0
            self.server.exceptions = 0

    # -------------------------------------------------- 关窗

    def shutdown(self) -> None:
        """关窗口时一定要停服务，别留个端口在后台听着。"""
        self.stop_server()


__all__ = ["FILLS", "MAX_TABLE_ROWS", "SlaveDebugPanel"]
