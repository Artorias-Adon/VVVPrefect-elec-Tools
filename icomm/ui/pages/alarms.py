"""报警页：实时报警列表 + 历史查询。"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme

COLUMNS = ["时间", "设备", "点号", "级别", "描述", "数值", "状态"]


class AlarmPage(QWidget):
    """报警记录。"""

    def __init__(self, storage=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.storage = storage
        self._active: dict[str, int] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.only_active = QCheckBox("只看未复位的报警")
        self.only_active.toggled.connect(self.reload)
        toolbar.addWidget(self.only_active)

        reload_button = QPushButton("刷新历史")
        reload_button.clicked.connect(self.reload)
        toolbar.addWidget(reload_button)

        clear_button = QPushButton("清空列表")
        clear_button.clicked.connect(self._clear_view)
        toolbar.addWidget(clear_button)

        toolbar.addStretch(1)
        self.counter = QLabel("当前报警：0")
        toolbar.addWidget(self.counter)
        root.addLayout(toolbar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setColumnWidth(0, 160)
        self.table.setColumnWidth(1, 120)
        self.table.setColumnWidth(2, 130)
        self.table.setColumnWidth(3, 80)
        self.table.setColumnWidth(4, 300)
        self.table.setColumnWidth(5, 90)
        root.addWidget(self.table, 1)

    # -------------------------------------------------- 实时

    def set_storage(self, storage) -> None:
        self.storage = storage
        self._clear_view()

    def on_alarm(self, event) -> None:
        self._insert(
            time_text=event.time_text,
            device=event.device,
            tag=event.tag,
            level=event.level,
            message=event.message,
            value="" if event.value is None else str(event.value),
            active=event.active,
        )
        if event.active:
            self._active[event.pid] = self._active.get(event.pid, 0) + 1
        else:
            self._active.pop(event.pid, None)
        self.counter.setText(f"当前报警：{len(self._active)}")

    def _insert(self, time_text, device, tag, level, message, value, active) -> None:
        self.table.insertRow(0)
        cells = [time_text, device, tag, level, message, value, "未复位" if active else "已复位"]
        color = theme.ALARM_LEVELS.get(level, theme.TEXT_DIM)
        for column, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if column == 3:
                item.setForeground(QColor(color))
            elif active:
                item.setForeground(QColor(color))
            else:
                item.setForeground(QColor(theme.TEXT_DIM))
            if column in (3, 5, 6):
                item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(0, column, item)
        while self.table.rowCount() > 2000:
            self.table.removeRow(self.table.rowCount() - 1)

    def _clear_view(self) -> None:
        self.table.setRowCount(0)
        self._active.clear()
        self.counter.setText("当前报警：0")

    # -------------------------------------------------- 历史

    def reload(self) -> None:
        if self.storage is None:
            return
        self.table.setRowCount(0)
        rows = self.storage.query_alarms(limit=1000, only_active=self.only_active.isChecked())
        for row in rows:
            self._insert(
                time_text=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row.get("ts") or 0)),
                device=row.get("device") or "",
                tag=row.get("tag") or "",
                level=row.get("level") or "",
                message=row.get("message") or "",
                value=row.get("value") or "",
                active=bool(row.get("active")),
            )
        self.counter.setText(f"已载入 {len(rows)} 条历史报警")
