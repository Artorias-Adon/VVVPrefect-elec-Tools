"""计时器与定时赋值。

现场很常见的两件事：
1. **对时间**：屏幕上得有个当前时间，做试验时还要能掐表、打时刻标记
   （"启动到故障用了 1.7 秒"这种结论，光看波形是量不准的）；
2. **按时动作**：开始计时后第 N 秒写一个值（阶跃响应），或者每 N 秒写一次（周期激励），
   或者让写入由某个变量触发（复用自动化核心）。

计时用 :class:`icomm.core.timing.Stopwatch`（基于 ``time.monotonic``，
调系统时间不会把计时搞乱），执行计划用 :class:`icomm.core.timing.Schedule`。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.automation import Task
from ...core.timing import (
    STEP_MODE_LABELS,
    Schedule,
    Step,
    StepMode,
    Stopwatch,
    format_seconds,
)
from .. import theme
from .base import ToolContext, WindowTool, register_tool
from .variable_trigger import (
    AUTOMATION_SERVICE,
    as_enum,
    build_trigger_fields,
    read_trigger_fields,
    trigger_summary,
)

#: 界面刷新间隔（毫秒）。掐表显示要跟手，所以比自动化那 300ms 快一些
TICK_MS = 100

COLUMNS = ["方式", "时刻 / 条件", "点位", "值", "已执行", "启用"]


class TriggerWriteDialog(QDialog):
    """给"变量触发写值"定条件。"""

    def __init__(self, context: ToolContext, step: Step, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.setWindowTitle("变量触发写值")
        self.setMinimumWidth(520)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        hint = QLabel(
            "条件成立时给目标点位写一次值。判断交给自动化核心（支持边沿、去抖、冷却）。"
        )
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.fields = build_trigger_fields(context)
        form.addRow("", QLabel(f"<b>目标</b>：{step.pid}　←　{step.value:g}"))
        for label, widget in self.fields.rows():
            form.addRow(label, widget)
        root.addLayout(form)

        buttons = QDialogButtonBox()
        buttons.addButton("确定", QDialogButtonBox.AcceptRole).clicked.connect(self._accept)
        buttons.addButton("取消", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        root.addWidget(buttons)

    def _accept(self) -> None:
        try:
            read_trigger_fields(self.fields)
        except ValueError as exc:
            self.fields.preview.setText(f"<span style='color:{theme.QUALITY_BAD}'>{exc}</span>")
            return
        self.accept()

    def trigger_of(self):
        return read_trigger_fields(self.fields)


class TimingWindow(QWidget):
    """时钟 + 秒表 + 定时赋值。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.service = context.service(AUTOMATION_SERVICE)
        self.watch = Stopwatch()
        self.schedule = Schedule()
        self._counter = 0

        self.setWindowTitle(f"计时器与定时赋值 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(940, 660)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        root.addWidget(self._build_clock())
        root.addWidget(self._build_marks())
        root.addWidget(self._build_schedule(), 1)
        root.addWidget(self._build_adder())

        self.status = QLabel("就绪")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

    # -------------------------------------------------- 时钟与秒表

    def _build_clock(self) -> QWidget:
        box = QGroupBox("时钟")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        self.clock = QLabel("")
        self.clock.setStyleSheet(
            f"color: {theme.TEXT}; font-size: 30px; font-weight: 600;"
        )
        layout.addWidget(self.clock)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.watch_label = QLabel("00:00:00.0")
        self.watch_label.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: 24px; font-weight: 600;"
        )
        row.addWidget(QLabel("计时"))
        row.addWidget(self.watch_label)
        row.addStretch(1)

        self.toggle_button = QPushButton("开始计时")
        self.toggle_button.setObjectName("primary")
        self.toggle_button.clicked.connect(lambda _checked=False: self._toggle_watch())
        row.addWidget(self.toggle_button)

        clear = QPushButton("清零")
        clear.setToolTip("清零会同时重置定时动作的计时")
        clear.clicked.connect(lambda _checked=False: self._reset_watch())
        row.addWidget(clear)

        mark = QPushButton("打时刻标记")
        mark.setToolTip("把当前计数值记下来（做试验时随手按一下）")
        mark.clicked.connect(lambda _checked=False: self._mark())
        row.addWidget(mark)
        layout.addLayout(row)

        date_row = QHBoxLayout()
        self.date_label = QLabel("")
        self.date_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        date_row.addWidget(self.date_label)
        date_row.addStretch(1)
        layout.addLayout(date_row)
        return box

    def _toggle_watch(self) -> None:
        self.watch.toggle()
        self.toggle_button.setText("暂停" if self.watch.running else "开始计时")
        self.status.setText("计时中" if self.watch.running else "已暂停")
        if not self.watch.running:
            return
        # 清零过、或者刚从头开始，就把计划表重新对齐到 0
        if self.watch.elapsed() < 0.2:
            self.schedule.reset()

    def _reset_watch(self) -> None:
        self.watch.reset()
        self.schedule.reset()
        self.toggle_button.setText("开始计时")
        self.watch_label.setText(format_seconds(0))
        self.status.setText("已清零")
        self.refresh()

    def _mark(self) -> None:
        value = self.watch.mark()
        self.marks_list.addItem(f"{format_seconds(value)}　　{time.strftime('%H:%M:%S')}")
        self.status.setText(f"已记录时刻 {format_seconds(value)}")

    def _build_marks(self) -> QWidget:
        box = QGroupBox("时刻标记")
        layout = QHBoxLayout(box)
        self.marks_list = QListWidget()
        self.marks_list.setMaximumHeight(96)
        layout.addWidget(self.marks_list, 1)
        clear = QPushButton("清空标记")
        clear.clicked.connect(lambda _checked=False: self._clear_marks())
        layout.addWidget(clear)
        return box

    def _clear_marks(self) -> None:
        self.marks_list.clear()
        self.watch.marks.clear()

    # -------------------------------------------------- 计划表

    def _build_schedule(self) -> QWidget:
        box = QGroupBox("定时动作（到点就给点位写值）")
        layout = QVBoxLayout(box)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_menu)
        for index, width in enumerate((90, 190, 230, 90, 80, 60)):
            self.table.setColumnWidth(index, width)
        layout.addWidget(self.table, 1)

        hint = QLabel(
            "提示：右键某一行可以「立即执行一次」「启用/停用」「删除」。"
            "需要在运行监视页看到效果的点位，先在左侧勾好。"
        )
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return box

    def _build_adder(self) -> QWidget:
        box = QGroupBox("添加动作")
        layout = QHBoxLayout(box)
        layout.setSpacing(6)

        self.mode_combo = QComboBox()
        for mode, label in STEP_MODE_LABELS.items():
            self.mode_combo.addItem(label, mode)
        self.mode_combo.currentIndexChanged.connect(lambda _index=0: self._sync_mode())
        layout.addWidget(self.mode_combo)

        self.seconds_box = QDoubleSpinBox()
        self.seconds_box.setRange(0.0, 86400.0)
        self.seconds_box.setDecimals(2)
        self.seconds_box.setValue(1.0)
        self.seconds_box.setSuffix(" 秒")
        self.seconds_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        layout.addWidget(self.seconds_box)

        self.pid_combo = QComboBox()
        for pid, label in self.context.writable_points():
            self.pid_combo.addItem(label, pid)
        self.pid_combo.setEditable(True)
        self.pid_combo.setInsertPolicy(QComboBox.NoInsert)
        self.pid_combo.setMinimumWidth(220)
        layout.addWidget(self.pid_combo, 1)

        self.value_box = QDoubleSpinBox()
        self.value_box.setRange(-1e9, 1e9)
        self.value_box.setDecimals(3)
        self.value_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        layout.addWidget(self.value_box)

        add = QPushButton("添加")
        add.setObjectName("primary")
        add.clicked.connect(lambda _checked=False: self.add_step())
        layout.addWidget(add)

        self.condition_hint = QLabel("")
        self.condition_hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(self.condition_hint)
        self._sync_mode()
        return box

    def _sync_mode(self) -> None:
        timing = self.selected_mode() is not StepMode.TRIGGER
        self.seconds_box.setEnabled(timing)
        self.condition_hint.setText("" if timing else "添加时会让你定条件")

    def selected_mode(self) -> StepMode:
        return as_enum(StepMode, self.mode_combo.currentData(), StepMode.AT)

    def add_step(self) -> None:
        pid = self.pid_combo.currentData()
        if not pid:
            self.status.setText("请先选择要写值的点位（可写点位才有）")
            return
        step = Step(
            pid=str(pid),
            value=float(self.value_box.value()),
            mode=self.selected_mode(),
            seconds=float(self.seconds_box.value()),
        )
        if step.mode is StepMode.TRIGGER:
            dialog = TriggerWriteDialog(self.context, step, parent=self)
            if dialog.exec() != QDialog.Accepted:
                return
            self._register_trigger_step(step, dialog.trigger_of())
        self.schedule.add(step)
        self.refresh()
        self.status.setText(f"已添加：{step.label()} → {self.context.title_of(step.pid)}")

    def _register_trigger_step(self, step: Step, trigger) -> None:
        """变量触发的动作交给自动化核心，这样去抖/冷却/边沿都自动有了。"""
        if self.service is None:
            self.status.setText("自动化核心不可用，变量触发不会生效")
            return
        self._counter += 1
        name = f"定时赋值 #{self._counter}：{trigger_summary(trigger, self.context)}"
        step.task_name = name
        self.service.add(
            Task(
                name=name,
                trigger=trigger,
                action=lambda task, _values, s=step: self._fire(s),
            )
        )

    def _show_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0 or row >= len(self.schedule.steps):
            return
        step = self.schedule.steps[row]
        menu = QMenu(self)
        run = menu.addAction("立即执行一次")
        run.triggered.connect(lambda: self._fire(step))
        toggle = menu.addAction("停用" if step.enabled else "启用")
        toggle.triggered.connect(lambda: self._toggle_step(row))
        menu.addSeparator()
        remove = menu.addAction("删除")
        remove.triggered.connect(lambda: self._remove_step(row))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _toggle_step(self, row: int) -> None:
        step = self.schedule.steps[row]
        step.enabled = not step.enabled
        if step.task_name and self.service is not None:
            task = self.service.find(step.task_name)
            if task is not None:
                task.enabled = step.enabled
        self.refresh()

    def _remove_step(self, row: int) -> None:
        step = self.schedule.steps[row]
        if step.task_name and self.service is not None:
            self.service.remove(step.task_name)
        self.schedule.remove(row)
        self.refresh()
        self.status.setText("已删除一条动作")

    # -------------------------------------------------- 运行

    def _fire(self, step: Step) -> None:
        """真正写值。走 ToolContext，引擎没起来时会提示先启动采集。"""
        if not self.context.engine_running():
            self.status.setText("未启动采集，写值没有下发（先点工具栏「启动采集」）")
            return
        ok = self.context.send(step.pid, step.value)
        if ok:
            self.status.setText(f"{format_seconds(self.watch.elapsed())}　写好 "
                                f"{self.context.title_of(step.pid)} = {step.value:g}")

    def _tick(self) -> None:
        now = time.localtime()
        self.clock.setText(time.strftime("%H:%M:%S", now))
        self.date_label.setText(time.strftime("%Y-%m-%d　%A", now))
        self.watch_label.setText(self.watch.text())
        if self.watch.running:
            for step in self.schedule.tick(self.watch.elapsed()):
                self._fire(step)
            self.refresh_counts()

    def refresh_counts(self) -> None:
        for row, step in enumerate(self.schedule.steps):
            item = self.table.item(row, 4)
            if item is not None:
                item.setText(str(step.fired))

    def refresh(self) -> None:
        self.table.setRowCount(len(self.schedule.steps))
        for row, step in enumerate(self.schedule.steps):
            where = step.label() if step.mode is not StepMode.TRIGGER else (
                f"{step.task_name or '变量触发'}"
            )
            cells = [
                STEP_MODE_LABELS.get(step.mode, step.mode.value),
                where,
                self.context.title_of(step.pid),
                f"{step.value:g}",
                str(step.fired),
                "是" if step.enabled else "否",
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column in (4, 5):
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, column, item)

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        # 关窗就把变量触发的任务摘掉：没人看着还在往设备写值更危险
        if self.service is not None:
            for step in self.schedule.steps:
                if step.task_name:
                    self.service.remove(step.task_name)
        super().closeEvent(event)


@register_tool
class TimingTool(WindowTool):
    name = "计时器与定时赋值"
    group = "自动化"
    order = 30
    description = "看点时间、掐表打标记，并可按时间或变量给点位写值"

    def build(self, context: ToolContext) -> QWidget:
        return TimingWindow(context)
