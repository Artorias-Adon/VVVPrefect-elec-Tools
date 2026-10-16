"""数据记录：指定一些量，按设定间隔记到表格里，可导出 CSV。

比"历史归档"更贴合现场临时取证的需求：

* 自己选要记哪几个量（不一定要进历史库的那些）；
* 自己设记录间隔（不受归档间隔限制）；
* **起始触发**可以是手动，也可以是某个变量满足条件；**结束**可以设时长、
  次数或另一个条件。

触发判断复用 :mod:`icomm.core.automation`，所以去抖、冷却这些都不用重写。
"""

from __future__ import annotations

import csv
import os
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.automation import Compare, Task, Trigger, TriggerMode
from ...core.codec import format_value
from .. import theme
from ..spin import no_arrows
from .base import ToolContext, WindowTool, register_tool
from .variable_trigger import (
    build_trigger_fields,
    read_trigger_fields,
    trigger_summary,
)

#: 表格里最多显示多少行（数据本身不截断，只是别把界面拖死）
MAX_TABLE_ROWS = 5000


class DataLogWindow(QWidget):
    """数据记录窗口（非模态，可以一边记一边看画面）。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.service = context.service("automation")
        self._timer = None
        self._rows: list[list[str]] = []
        self._pids: list[str] = []
        self._recording = False
        self._started_at = 0.0
        self._stop_at = 0.0
        self._task_name = ""
        self._end_task_name = ""

        self.setWindowTitle(f"数据记录 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(1080, 700)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # 触发控件要在搭界面之前建好：_build_config() 里就要用它们
        self._start_trigger = build_trigger_fields(
            context, default_compare=Compare.GE, default_mode=TriggerMode.RISING
        )
        self._record_trigger = build_trigger_fields(
            context, default_compare=Compare.LE, default_mode=TriggerMode.RISING
        )

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_config())
        splitter.addWidget(self._build_data())
        splitter.setSizes([420, 660])
        root.addWidget(splitter, 1)

        self.status = QLabel("就绪")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.status)

        from PySide6.QtCore import QTimer

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._fill_points()

    # -------------------------------------------------- 界面

    def _build_config(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        points_box = QGroupBox("记录哪些量")
        points_layout = QVBoxLayout(points_box)
        self.points_list = QListWidget()
        self.points_list.setSelectionMode(QListWidget.ExtendedSelection)
        points_layout.addWidget(self.points_list, 1)
        row = QHBoxLayout()
        row.setSpacing(4)
        for text, slot in (
            ("全选", lambda: self._check_all(True)),
            ("全不选", lambda: self._check_all(False)),
            ("只选监视页的", self._check_displayed),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        points_layout.addLayout(row)
        layout.addWidget(points_box, 1)

        start_box = QGroupBox("记录参数")
        form = QFormLayout(start_box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.interval_box = QDoubleSpinBox()
        self.interval_box.setRange(0.05, 3600.0)
        self.interval_box.setDecimals(2)
        self.interval_box.setValue(1.0)
        self.interval_box.setSuffix(" 秒")
        self.interval_box.setToolTip("记录间隔，与历史归档间隔无关")
        no_arrows(self.interval_box)
        form.addRow("记录间隔", self.interval_box)

        self.start_mode_combo = QComboBox()
        self.start_mode_combo.addItem("手动开始", "manual")
        self.start_mode_combo.addItem("变量触发开始", "variable")
        self.start_mode_combo.currentIndexChanged.connect(self._sync_start_mode)
        form.addRow("起始方式", self.start_mode_combo)

        self.start_trigger_host = QWidget()
        host_layout = QFormLayout(self.start_trigger_host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for label, widget in self._start_rows():
            host_layout.addRow(label, widget)
        form.addRow("", self.start_trigger_host)

        self.duration_box = QDoubleSpinBox()
        self.duration_box.setRange(0.0, 86400.0)
        self.duration_box.setDecimals(1)
        self.duration_box.setValue(60.0)
        self.duration_box.setSuffix(" 秒")
        self.duration_box.setToolTip("0 = 不限时长，需手动或按结束条件停止")
        no_arrows(self.duration_box)
        form.addRow("持续时长", self.duration_box)

        self.end_mode_combo = QComboBox()
        self.end_mode_combo.addItem("按持续时长 / 手动停止", "duration")
        self.end_mode_combo.addItem("变量触发停止", "variable")
        self.end_mode_combo.currentIndexChanged.connect(self._sync_end_mode)
        form.addRow("结束方式", self.end_mode_combo)

        self.end_trigger_host = QWidget()
        end_layout = QFormLayout(self.end_trigger_host)
        end_layout.setContentsMargins(0, 0, 0, 0)
        end_layout.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for label, widget in self._end_rows():
            end_layout.addRow(label, widget)
        form.addRow("", self.end_trigger_host)

        self.max_rows_box = QSpinBox()
        self.max_rows_box.setRange(0, 1_000_000)
        self.max_rows_box.setValue(0)
        self.max_rows_box.setSuffix(" 行")
        self.max_rows_box.setToolTip("0 = 不限（小心把内存吃满）")
        no_arrows(self.max_rows_box)
        form.addRow("最多记录", self.max_rows_box)

        self.record_meta = QCheckBox("时间列记到毫秒")
        self.record_meta.setChecked(True)
        form.addRow("", self.record_meta)
        layout.addWidget(start_box)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.start_button = QPushButton("开始记录")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(lambda _checked=False: self.start_recording())
        buttons.addWidget(self.start_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("danger")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(lambda _checked=False: self.stop_recording())
        buttons.addWidget(self.stop_button)
        export = QPushButton("导出 CSV")
        export.clicked.connect(self.export_csv)
        buttons.addWidget(export)
        clear = QPushButton("清空")
        clear.clicked.connect(self.clear_rows)
        buttons.addWidget(clear)
        layout.addLayout(buttons)

        self._sync_start_mode()
        self._sync_end_mode()
        return panel

    def _start_rows(self) -> list[tuple]:
        """起始触发的控件行（由共享的触发控件提供，支持多个变量组合）。"""
        return [
            ("", self._start_trigger.conditions_widget()),
            ("", self._start_trigger.preview),
        ]

    def _end_rows(self) -> list[tuple]:
        """结束触发的控件行。"""
        return [
            ("", self._record_trigger.conditions_widget()),
            ("", self._record_trigger.preview),
        ]

    def _build_data(self) -> QWidget:
        box = QGroupBox("记录数据")
        layout = QVBoxLayout(box)
        self.table = QTableWidget(0, 1)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        layout.addWidget(self.table)
        return box

    def _fill_points(self) -> None:
        self.points_list.clear()
        for pid, label in self.context.all_points():
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, pid)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.points_list.addItem(item)

    def _check_all(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for index in range(self.points_list.count()):
            self.points_list.item(index).setCheckState(state)

    def _check_displayed(self) -> None:
        """只勾"运行监视页上看得见"的点位（最常用的一批）。"""
        displayed = {
            f"{device.name}.{point.name}"
            for device, point in self.context.project.display_points()
        }
        for index in range(self.points_list.count()):
            item = self.points_list.item(index)
            item.setCheckState(
                Qt.Checked if item.data(Qt.UserRole) in displayed else Qt.Unchecked
            )

    def _sync_start_mode(self) -> None:
        variable = self.start_mode_combo.currentData() == "variable"
        self.start_trigger_host.setEnabled(variable)

    def _sync_end_mode(self) -> None:
        variable = self.end_mode_combo.currentData() == "variable"
        self.end_trigger_host.setEnabled(variable)

    # -------------------------------------------------- 记录

    def selected_pids(self) -> list[str]:
        return [
            self.points_list.item(index).data(Qt.UserRole)
            for index in range(self.points_list.count())
            if self.points_list.item(index).checkState() == Qt.Checked
        ]

    def start_recording(self) -> None:
        if self._recording:
            return
        pids = self.selected_pids()
        if not pids:
            QMessageBox.information(self, "提示", "请先勾选要记录的量。")
            return

        self._pids = pids
        if not self._rows:
            self._setup_columns(pids)

        self._started_at = time.time()
        duration = float(self.duration_box.value())
        self._stop_at = self._started_at + duration if duration > 0 else 0.0
        self._recording = True
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.points_list.setEnabled(False)

        # 变量触发开始：注册一个一次性任务，触发时才真正开跑
        if self.start_mode_combo.currentData() == "variable":
            try:
                trigger = read_trigger_fields(self._start_trigger)
            except ValueError as exc:
                QMessageBox.warning(self, "触发条件不完整", str(exc))
                self._reset_buttons()
                return
            if self.service is None:
                QMessageBox.warning(self, "不可用", "自动化核心未接入，请从主窗口打开本工具。")
                self._reset_buttons()
                return
            name = f"数据记录启动：{trigger_summary(trigger, self.context)}"
            self.service.remove(name)
            self.service.add(
                Task(
                    name=name,
                    trigger=trigger,
                    max_fires=1,
                    action=lambda task, _values: self._arm(),
                )
            )
            self.status.setText(f"等待触发：{trigger.describe()}")
            self._task_name = name
        else:
            self._arm()

        self._timer.start()

    def _arm(self) -> None:
        """真正开始采样（变量触发走这里）。"""
        self._started_at = time.time()
        duration = float(self.duration_box.value())
        self._stop_at = self._started_at + duration if duration > 0 else 0.0
        if self.end_mode_combo.currentData() == "variable":
            self._arm_end_trigger()
        self.status.setText(f"记录中：{len(self._pids)} 个量，每 {self.interval_box.value():g} 秒一行")

    def _arm_end_trigger(self) -> None:
        """变量触发结束：登记一个一次性任务，满足条件就停。"""
        if self.service is None:
            return
        try:
            trigger = read_trigger_fields(self._record_trigger)
        except ValueError as exc:
            QMessageBox.warning(self, "结束条件不完整", str(exc))
            return
        name = f"数据记录停止：{trigger_summary(trigger, self.context)}"
        self.service.remove(name)
        self.service.add(
            Task(
                name=name,
                trigger=trigger,
                max_fires=1,
                action=lambda task, _values: self.stop_recording("变量触发停止"),
            )
        )
        self._end_task_name = name

    def stop_recording(self, reason: str = "手动停止") -> None:
        if not self._recording:
            return
        self._recording = False
        self._timer.stop()
        self._reset_buttons()
        if self.service is not None:
            for name in (self._task_name, self._end_task_name):
                if name:
                    self.service.remove(name)
        self._task_name = ""
        self._end_task_name = ""
        self.status.setText(f"{reason}，共 {len(self._rows)} 行")
        self.context.report(f"数据记录结束（{reason}），共 {len(self._rows)} 行")

    def _reset_buttons(self) -> None:
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.points_list.setEnabled(True)

    def _tick(self) -> None:
        """定时器回调：按间隔采一行，并检查结束条件。"""
        if not self._recording:
            return
        now = time.time()
        if self._stop_at and now >= self._stop_at:
            self.stop_recording(f"到达设定时长（{self.duration_box.value():g} 秒）")
            return

        limit = self.max_rows_box.value()
        if limit and len(self._rows) >= limit:
            self.stop_recording(f"达到行数上限（{limit}）")
            return

        interval = float(self.interval_box.value())
        if self._rows and now - self._last_row_at() < interval:
            return
        self.append_row(now)

    def _last_row_at(self) -> float:
        """上一行的时间（用行首的时间列反推，避免另存一份状态）。"""
        if not self._rows:
            return 0.0
        try:
            return float(self._rows[-1][-1])  # 最后一列藏了原始时间戳
        except (TypeError, ValueError):
            return 0.0

    def append_row(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        if self.record_meta.isChecked():
            stamp += f".{int((now % 1) * 1000):03d}"
        row = [stamp]
        for pid in self._pids:
            tag = self.context.tag_of(pid)
            if tag.quality != "good" or tag.value is None:
                row.append(tag.error or "通信异常")
            else:
                point = self.context.project.find_point(pid)
                decimals = point[1].decimals if point else 2
                row.append(format_value(tag.value, decimals))
        row.append(f"{now:.6f}")  # 内部用，导出时不写
        self._rows.append(row)
        self._append_table(row)

    def _setup_columns(self, pids: list[str]) -> None:
        headers = ["时间"]
        for pid in pids:
            found = self.context.project.find_point(pid)
            if found is None:
                headers.append(pid)
                continue
            device, point = found
            unit = f"（{point.unit}）" if point.unit else ""
            headers.append(f"{device.name}.{point.title}{unit}")
        self.table.setColumnCount(len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        for index in range(len(headers)):
            self.table.setColumnWidth(index, 150 if index else 190)

    def _append_table(self, row: list[str]) -> None:
        if self.table.rowCount() >= MAX_TABLE_ROWS:
            return  # 表格只是给人看，数据本身留在 _rows 里
        index = self.table.rowCount()
        self.table.insertRow(index)
        for column, text in enumerate(row[:-1]):
            item = QTableWidgetItem(text)
            if column == 0:
                item.setForeground(QColor(theme.TEXT_DIM))
            self.table.setItem(index, column, item)
        if index % 20 == 0:
            self.table.scrollToBottom()
        self.status.setText(
            f"记录中：{len(self._rows)} 行"
            + (f"（表格只显示前 {MAX_TABLE_ROWS} 行）" if len(self._rows) > MAX_TABLE_ROWS else "")
        )

    def clear_rows(self) -> None:
        self._rows.clear()
        self.table.setRowCount(0)
        self.status.setText("已清空")

    def export_csv(self) -> None:
        if not self._rows:
            QMessageBox.information(self, "提示", "还没有记录到数据。")
            return
        default = os.path.join(
            self.context.report_subdir("datalog"),
            time.strftime("datalog_%Y%m%d_%H%M%S.csv"),
        )
        path, _ = QFileDialog.getSaveFileName(self, "导出记录", default, "CSV (*.csv)")
        if not path:
            return
        if not path.lower().endswith(".csv"):
            path += ".csv"
        headers = [
            self.table.horizontalHeaderItem(i).text() for i in range(self.table.columnCount())
        ]
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as fh:
                writer = csv.writer(fh)
                writer.writerow(headers)
                for row in self._rows:
                    writer.writerow(row[:-1])
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(
            self, "导出完成", f"已导出 {len(self._rows)} 行到：\n{path}"
        )
        self.context.report(f"数据记录已导出 {len(self._rows)} 行 -> {path}")

    def closeEvent(self, event) -> None:  # noqa: item2
        self.stop_recording("窗口关闭")
        if self._timer is not None:
            self._timer.stop()
        super().closeEvent(event)


@register_tool
class DataLogTool(WindowTool):
    name = "数据记录"
    group = "自动化"
    order = 20
    description = "选量、设间隔、按触发起止，记录到表格并可导出"

    def build(self, context: ToolContext) -> QWidget:
        return DataLogWindow(context)
