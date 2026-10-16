"""自动化任务一览。

所有"到条件就做点什么"的功能（截屏、数据记录、录音、故障录波、定时赋值）
都注册到同一个 :class:`icomm.core.automation.AutomationService`，
这里把它们列出来，可以手动触发、启停或删除。

集中一处的好处：现场要排查"到底有哪些自动化在跑、有没有互相打架"时一眼就能看到。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.automation import AutomationService
from .. import theme

COLUMNS = ["任务", "触发条件", "状态", "次数", "最近触发"]

#: 状态刷新间隔（毫秒）
REFRESH_MS = 700


class AutomationPanel(QDialog):
    """自动化任务管理。"""

    def __init__(self, service: AutomationService, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.changed = False

        self.setWindowTitle("自动化任务")
        self.setMinimumSize(820, 460)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        hint = QLabel(
            "这里是所有「满足条件就做点什么」的任务：截屏、数据记录、录音、故障录波、定时赋值……\n"
            "条件判断在界面线程执行，所以动作可以安全地操作界面。"
        )
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        for index, width in enumerate((230, 300, 170, 70, 170)):
            self.table.setColumnWidth(index, width)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        manual = QPushButton("手动触发选中")
        manual.setToolTip("不等条件，立刻执行一次")
        manual.clicked.connect(self._manual)
        row.addWidget(manual)
        toggle = QPushButton("启用 / 停用")
        toggle.clicked.connect(self._toggle)
        row.addWidget(toggle)
        reset = QPushButton("重置触发状态")
        reset.setToolTip("清掉边沿检测的记忆，让它重新等待一次上升沿")
        reset.clicked.connect(self._reset)
        row.addWidget(reset)
        row.addStretch(1)
        remove = QPushButton("删除选中")
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove)
        row.addWidget(remove)
        root.addLayout(row)

        buttons = QDialogButtonBox()
        buttons.addButton("关闭", QDialogButtonBox.RejectRole).clicked.connect(self.accept)
        root.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # -------------------------------------------------- 列表

    def refresh(self) -> None:
        tasks = self.service.tasks
        self.table.setRowCount(len(tasks))
        for index, task in enumerate(tasks):
            import time as _time

            when = (
                _time.strftime("%H:%M:%S", _time.localtime(task.last_fire_at))
                if task.last_fire_at
                else "--"
            )
            cells = [task.name, task.trigger.describe(), task.status_text(), str(task.fires), when]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.UserRole, task.name)
                if column == 2:
                    if task.last_error:
                        item.setForeground(QColor(theme.QUALITY_BAD))
                    elif not task.enabled:
                        item.setForeground(QColor(theme.TEXT_DIM))
                self.table.setItem(index, column, item)

    def _selected_name(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        data = item.data(Qt.UserRole) if item is not None else ""
        return str(data or "")

    # -------------------------------------------------- 操作

    def _manual(self) -> None:
        name = self._selected_name()
        if name:
            self.service.manual_trigger(name)
            self.changed = True
            self.refresh()

    def _toggle(self) -> None:
        task = self.service.find(self._selected_name())
        if task is not None:
            task.enabled = not task.enabled
            self.changed = True
            self.refresh()

    def _reset(self) -> None:
        task = self.service.find(self._selected_name())
        if task is not None:
            task.trigger.reset()
            task.fires = 0
            task.last_error = ""
            self.changed = True
            self.refresh()

    def _remove(self) -> None:
        name = self._selected_name()
        if name and self.service.remove(name):
            self.changed = True
            self.refresh()

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        super().closeEvent(event)
