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
