"""持续发送管理面板。

工具栏「持续发送」按钮打开。集中看两件事：

* **正在发**的任务（发送中 / 已暂停），带发包计数与最近错误；
* 工程里**记着但没在发**的配置 —— 打开工程时按安全策略不会自动开跑，
  在这里手动启动。

面板本身不动引擎，只把用户的选择收集成三个列表交给主窗口执行，
这样"谁在写设备"这件事始终只有主窗口一个出口。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import theme

COLUMNS = ["状态", "点位", "写入值", "周期", "已发", "失败", "最近错误"]
ROW_HEIGHT_PADDING = 12


class PeriodicPanel(QDialog):
    """持续发送一览与批量操作。"""

    def __init__(
        self,
        project,
        tasks: list,
        engine_running: bool,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.project = project
        self._running = {task.pid: task for task in tasks}
        self._engine_running = engine_running
        #: 交给主窗口执行的动作
        self.started: list[tuple[str, object, int]] = []
        self.paused: list[tuple[str, bool]] = []
        self.stopped: list[str] = []

        self.setWindowTitle("持续发送")
        self.setMinimumWidth(760)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        note = QLabel(
            "持续发送 = 按周期反复往设备写同一个值，会一直发到手动停止。\n"
            "工程里记住的配置在打开工程时不会自动开始，需要在这里手动启动。"
        )
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        note.setWordWrap(True)
        root.addWidget(note)

        if not engine_running:
            warn = QLabel("采集未运行：写值需要通道在线，请先启动采集。")
            warn.setStyleSheet(f"color: {theme.QUALITY_STALE}; font-size: 11px;")
            root.addWidget(warn)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        for column, width in enumerate((80, 210, 90, 90, 70, 60, 220)):
            self.table.setColumnWidth(column, width)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        start_button = QPushButton("启动选中")
        start_button.setToolTip("按工程里记住的值与周期开始发送")
        start_button.clicked.connect(self._start_selected)
        row.addWidget(start_button)

        self.pause_button = QPushButton("暂停选中")
        self.pause_button.setToolTip("暂停后配置保留，随时可恢复")
        self.pause_button.clicked.connect(lambda: self._set_paused(True))
        row.addWidget(self.pause_button)

        self.resume_button = QPushButton("恢复选中")
        self.resume_button.clicked.connect(lambda: self._set_paused(False))
        row.addWidget(self.resume_button)

        stop_button = QPushButton("停止选中")
        stop_button.setToolTip("停止发送，并从工程配置里移除")
        stop_button.clicked.connect(self._stop_selected)
        row.addWidget(stop_button)

        stop_all_button = QPushButton("全部停止")
        stop_all_button.clicked.connect(self._stop_all)
        row.addWidget(stop_all_button)

        row.addStretch(1)
        close_button = QPushButton("关闭")
        close_button.setDefault(True)
        close_button.clicked.connect(self.accept)
        row.addWidget(close_button)
        root.addLayout(row)

        self._fill()

    # -------------------------------------------------- 列表

    def _fill(self) -> None:
        """列出运行中的任务 + 工程里记着但没在发的配置。"""
        rows: list[tuple[str, object]] = []
        for task in self._running.values():
            rows.append(("sending", task))
        for entry in self.project.periodic_writes:
            if entry.pid not in self._running:
                rows.append(("idle", entry))

        self.table.setRowCount(len(rows))
        for index, (kind, item) in enumerate(rows):
            status = "未启动"
            if kind == "sending":
                status = "发送中" if item.enabled else "已暂停"
            cells = [
                status,
                item.pid,
                str(item.value),
                f"{item.interval_ms} ms",
                str(getattr(item, "sent", "")) if kind == "sending" else "",
                str(getattr(item, "failed", "")) if kind == "sending" else "",
                getattr(item, "last_error", "") if kind == "sending" else "",
            ]
            for column, text in enumerate(cells):
                cell = QTableWidgetItem(str(text))
                if column == 0:
                    cell.setForeground(
                        QColor(Qt.GlobalColor.green)
                        if status == "发送中"
                        else QColor(theme.QUALITY_STALE if status == "已暂停" else theme.TEXT_DIM)
                    )
                    cell.setTextAlignment(Qt.AlignCenter)
                if column == 6 and text:
                    cell.setForeground(QColor(theme.QUALITY_BAD))
                cell.setData(Qt.UserRole, item.pid)
                cell.setData(Qt.UserRole + 1, kind)
                self.table.setItem(index, column, cell)
            self.table.setRowHeight(index, self.table.rowHeight(index) + ROW_HEIGHT_PADDING)

        if not rows:
            self.table.setRowCount(1)
            item = QTableWidgetItem("（没有持续发送配置）")
            item.setForeground(QColor(theme.TEXT_DIM))
            item.setFlags(Qt.NoItemFlags)
            self.table.setItem(0, 0, item)
            self.table.setSpan(0, 0, 1, len(COLUMNS))

    def _selected(self) -> list[tuple[str, str]]:
        """选中的 ``[(点号, 运行/未启动)]``。"""
        picked: list[tuple[str, str]] = []
        seen: set[str] = set()
        for index in self.table.selectionModel().selectedRows() if self.table.selectionModel() else []:
            cell = self.table.item(index.row(), 0)
            if cell is None:
                continue
            pid = cell.data(Qt.UserRole)
            if pid and pid not in seen:
                seen.add(pid)
                picked.append((pid, cell.data(Qt.UserRole + 1)))
        return picked

    # -------------------------------------------------- 动作

    def _start_selected(self) -> None:
        for pid, kind in self._selected():
            if kind != "idle":
                continue
            entry = self.project.periodic_write(pid)
            if entry is None:
                continue
            self.started.append((pid, entry.value, entry.interval_ms))
        self.accept()

    def _set_paused(self, paused: bool) -> None:
        for pid, kind in self._selected():
            if kind == "sending":
                self.paused.append((pid, not paused))
        self.accept()

    def _stop_selected(self) -> None:
        for pid, _kind in self._selected():
            self.stopped.append(pid)
        self.accept()

    def _stop_all(self) -> None:
        self.stopped.extend(task.pid for task in self._running.values())
        self.stopped.extend(entry.pid for entry in self.project.periodic_writes)
        self.stopped = list(dict.fromkeys(self.stopped))
        self.accept()
