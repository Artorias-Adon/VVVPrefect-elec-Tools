"""通信监视页：原始报文与通信日志。

报文量很大，只保留在内存环形缓冲里，不落库；
通信日志（连接、重连、写入失败等）同时入库。
"""

from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import theme

MAX_FRAMES = 2000
MAX_LOGS = 2000

#: 「设备状态」页的列
STATUS_COLUMNS = [
    "设备",
    "通道",
    "状态",
    "请求",
    "无响应",
    "丢包率（累计）",
    "丢包率（最近 100 次）",
    "最近耗时",
    "最近错误",
]


def loss_colour(rate: float | None) -> str:
    """丢包率配色：<1% 正常、<5% 注意、再高就是问题（和设备详情页同一套阈值）。"""
    if rate is None:
        return theme.TEXT_DIM
    if rate < 0.01:
        return theme.QUALITY_GOOD
    if rate < 0.05:
        return theme.QUALITY_STALE
    return theme.QUALITY_BAD


class MonitorPage(QWidget):
    """报文监视 + 通信日志。"""

    def __init__(self, storage=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.storage = storage
        self._frames: deque[tuple[str, str, str, str]] = deque(maxlen=MAX_FRAMES)
        self._logs: deque[tuple[str, str, str, str]] = deque(maxlen=MAX_LOGS)
        self._frame_dirty = False
        self._log_dirty = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(QLabel("通道"))
        self.channel_combo = QComboBox()
        self.channel_combo.addItem("全部", "")
        self.channel_combo.currentIndexChanged.connect(self._refill)
        toolbar.addWidget(self.channel_combo)

        toolbar.addWidget(QLabel("方向"))
        self.direction_combo = QComboBox()
        self.direction_combo.addItem("全部", "")
        self.direction_combo.addItem("仅发送 TX", "TX")
        self.direction_combo.addItem("仅接收 RX", "RX")
        self.direction_combo.currentIndexChanged.connect(self._refill)
        toolbar.addWidget(self.direction_combo)

        self.pause_box = QCheckBox("暂停刷新")
        toolbar.addWidget(self.pause_box)

        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self._clear)
        toolbar.addWidget(clear_button)

        toolbar.addStretch(1)
        self.counter = QLabel("报文 0 条")
        self.counter.setStyleSheet(f"color: {theme.TEXT_DIM};")
        toolbar.addWidget(self.counter)
        root.addLayout(toolbar)

        self.tabs = QTabWidget()

        self.frame_table = self._make_table(["时间", "通道", "方向", "报文内容"])
        self.frame_table.setColumnWidth(0, 150)
        self.frame_table.setColumnWidth(1, 130)
        self.frame_table.setColumnWidth(2, 70)
        self.frame_table.setColumnWidth(3, 700)
        self.tabs.addTab(self.frame_table, "原始报文")

        self.log_table = self._make_table(["时间", "通道", "级别", "内容"])
        self.log_table.setColumnWidth(0, 150)
        self.log_table.setColumnWidth(1, 130)
        self.log_table.setColumnWidth(2, 80)
        self.log_table.setColumnWidth(3, 700)
        self.tabs.addTab(self.log_table, "通信日志")

        # 每台设备的链路质量：有多少轮没收到回应（丢包率）
        self.status_table = self._make_table(STATUS_COLUMNS)
        for column, width in enumerate((150, 130, 90, 70, 80, 120, 160, 100, 260)):
            self.status_table.setColumnWidth(column, width)
        self.tabs.addTab(self.status_table, "设备状态")
        self._statuses: dict[str, object] = {}
        self._row_of: dict[str, int] = {}

        root.addWidget(self.tabs, 1)

    @staticmethod
    def _make_table(headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.verticalHeader().setVisible(False)
        return table

    # -------------------------------------------------- 配置

    def set_storage(self, storage) -> None:
        self.storage = storage

    def set_channels(self, names: list[str]) -> None:
        current = self.channel_combo.currentData()
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        self.channel_combo.addItem("全部", "")
        for name in names:
            self.channel_combo.addItem(name, name)
        index = self.channel_combo.findData(current)
        self.channel_combo.setCurrentIndex(index if index >= 0 else 0)
        self.channel_combo.blockSignals(False)
        self._clear()

    # -------------------------------------------------- 接收

    def on_frame(self, channel: str, direction: str, text: str) -> None:
        self._frames.append((time.strftime("%H:%M:%S"), channel, direction, text))
        self._frame_dirty = True
        if not self.pause_box.isChecked():
            self._flush_frames()

    def on_log(self, channel: str, level: str, message: str) -> None:
        self._logs.append((time.strftime("%H:%M:%S"), channel, level, message))
        self._log_dirty = True
        if not self.pause_box.isChecked():
            self._flush_logs()

    # -------------------------------------------------- 设备状态（丢包率）

    def on_device_state(self, status, channel: str = "") -> None:
        """缓存一台设备的通信状态。

        每台设备**每次轮询**都会回调一次，所以这里只改它自己那一行
        （整表重画在几十台设备时会卡界面）。
        """
        self._statuses[status.name] = (status, channel)
        if not self.pause_box.isChecked():
            self._write_row(status, channel)

    def set_statuses(self, items) -> None:
        """整体替换（换工程 / 采集停了时用）。``items`` = [(DeviceStatus, 通道名)]"""
        self._statuses = {status.name: (status, channel) for status, channel in items}
        self._rebuild_status_table()

    def clear_statuses(self) -> None:
        self._statuses.clear()
        self._row_of.clear()
        self.status_table.setRowCount(0)

    def _rebuild_status_table(self) -> None:
        self._row_of.clear()
        names = sorted(self._statuses)
        self.status_table.setRowCount(len(names))
        for row, name in enumerate(names):
            status, channel = self._statuses[name]
            self._row_of[name] = row
            self._fill_row(row, status, channel)

    def _write_row(self, status, channel: str) -> None:
        row = self._row_of.get(status.name)
        if row is None:
            row = sorted(self._statuses).index(status.name)
            self.status_table.insertRow(row)
            for name, index in list(self._row_of.items()):
                if index >= row:
                    self._row_of[name] = index + 1
            self._row_of[status.name] = row
        self._fill_row(row, status, channel)

    def _fill_row(self, row: int, status, channel: str) -> None:
        # 状态对象可能来自别处的轻量实现（探针/测试），缺字段就当 0 / "--"
        requests = getattr(status, "request_count", 0)
        timeouts = getattr(status, "timeout_count", 0)
        loss_rate = getattr(status, "loss_rate", None)
        recent_rate = getattr(status, "recent_loss_rate", None)
        loss_text = getattr(status, "loss_text", lambda: "--")()
        recent_text = getattr(status, "recent_loss_text", lambda: "--")()
        duration = getattr(status, "last_duration_ms", 0.0)
        cells = [
            getattr(status, "name", ""),
            channel or "-",
            getattr(status, "state", "-"),
            str(requests),
            str(timeouts),
            loss_text,
            recent_text,
            f"{duration:.0f} ms",
            getattr(status, "error", "") or "-",
        ]
        for column, text in enumerate(cells):
            item = QTableWidgetItem(str(text))
            if column in (3, 4, 7):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if column == 2:
                item.setForeground(
                    QColor(theme.ACCENT if getattr(status, "connected", False) else theme.QUALITY_BAD)
                )
            if column in (5, 6):
                rate = loss_rate if column == 5 else recent_rate
                item.setForeground(QColor(loss_colour(rate)))
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.status_table.setItem(row, column, item)

    def status_row(self, device_name: str) -> list[str] | None:
        """某台设备在「设备状态」页里的那一行（测试/探针用）。"""
        row = self._row_of.get(device_name)
        if row is None:
            return None
        return [
            self.status_table.item(row, column).text()
            for column in range(self.status_table.columnCount())
        ]

    def _match(self, channel: str, direction: str | None) -> bool:
        wanted_channel = self.channel_combo.currentData()
        if wanted_channel and channel != wanted_channel:
            return False
        if direction is not None:
            wanted_direction = self.direction_combo.currentData()
            if wanted_direction and direction != wanted_direction:
                return False
        return True

    def _flush_frames(self) -> None:
        if not self._frame_dirty or self.pause_box.isChecked():
            return
        self._frame_dirty = False
        rows = [f for f in self._frames if self._match(f[1], f[2])]
        self._fill(self.frame_table, rows, direction_column=2)
        self.counter.setText(f"报文 {len(self._frames)} 条 / 显示 {len(rows)} 条")

    def _flush_logs(self) -> None:
        if not self._log_dirty or self.pause_box.isChecked():
            return
        self._log_dirty = False
        rows = [f for f in self._logs if self._match(f[1], None)]
        self._fill(self.log_table, rows, color_column=2)

    def _fill(self, table: QTableWidget, rows, direction_column: int | None = None, color_column: int | None = None) -> None:
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, text in enumerate(row):
                item = QTableWidgetItem(str(text))
                if direction_column is not None and column == direction_column:
                    item.setForeground(
                        QColor(theme.ACCENT_ALT if text == "TX" else theme.ACCENT)
                    )
                    item.setTextAlignment(Qt.AlignCenter)
                if color_column is not None and column == color_column:
                    item.setForeground(
                        QColor(
                            {
                                "error": theme.QUALITY_BAD,
                                "warning": theme.QUALITY_STALE,
                                "info": theme.TEXT,
                            }.get(str(text), theme.TEXT)
                        )
                    )
                table.setItem(row_index, column, item)

    def _refill(self) -> None:
        self._frame_dirty = True
        self._log_dirty = True
        self._flush_frames()
        self._flush_logs()

    def _clear(self) -> None:
        self._frames.clear()
        self._logs.clear()
        self.frame_table.setRowCount(0)
        self.log_table.setRowCount(0)
        self.counter.setText("报文 0 条")

    def load_logs_from_db(self, limit: int = 500) -> None:
        """从数据库把历史通信日志拉进内存视图。"""
        if self.storage is None:
            return
        for row in self.storage.query_logs(limit=limit):
            self._logs.appendleft(
                (
                    time.strftime("%H:%M:%S", time.localtime(row.get("ts") or 0)),
                    row.get("device") or "",
                    row.get("level") or "info",
                    row.get("message") or "",
                )
            )
        self._log_dirty = True
        self._flush_logs()
