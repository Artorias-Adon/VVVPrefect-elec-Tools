"""故障录波工具。

报警或自选变量一触发，自动把**前后**一段波形留档（前/后时长可选，默认各 1s）。
录到的波形进现有的录波表，所以能直接用示波器页的录波回放看，同时可选导出 CSV。

触发判断交给自动化核心（边沿、去抖、冷却、次数上限都自动有），
往回记的缓冲在 :class:`icomm.core.fault.FaultRecorder`，主窗口按采集批次喂它。
"""

from __future__ import annotations

import csv
import os
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
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
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import fault as fault_core
from ...core.automation import Compare, Task, TriggerMode
from .. import theme
from ..spin import no_arrows
from .base import ToolContext, WindowTool, register_tool
from .variable_trigger import (
    AUTOMATION_SERVICE,
    build_trigger_fields,
    read_trigger_fields,
    trigger_summary,
)

#: 录波服务的键（主窗口把自己的 FaultRecorder 放进 services）
FAULT_SERVICE = "fault"

COLUMNS = ["时间", "曲线", "点号", "点数", "采样间隔us"]


class FaultRecordWindow(QWidget):
    """配置并查看故障录波。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.service = context.service(AUTOMATION_SERVICE)
        self.recorder: fault_core.FaultRecorder | None = context.service(FAULT_SERVICE)
        self._task_name = ""

        self.setWindowTitle(f"故障录波 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(1060, 700)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        root.addWidget(self._build_hint())
        root.addWidget(self._build_config(), 1)
        root.addWidget(self._build_list(), 1)
        root.addLayout(self._build_buttons())

        self.status = QLabel("就绪")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._load_saved()
        self._sync_state()

    # -------------------------------------------------- 界面

    def _build_hint(self) -> QWidget:
        label = QLabel(
            "一直在往回记（环形缓冲），一旦触发就把「触发前」这段快照留下，再继续录完「触发后」，"
            "然后存进录波表。录到的波形可在「示波器」页的录波回放里看。\n"
            "数组/块点位（DSP 波形缓冲区）按帧记录，跨越触发时刻的那一帧会整帧保留。"
        )
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        return label

    def _build_config(self) -> QWidget:
        box = QGroupBox("录波配置")
        layout = QHBoxLayout(box)
        layout.setSpacing(10)

        left = QVBoxLayout()
        left.addWidget(QLabel("监视哪些量"))
        self.points_list = QListWidget()
        for pid, label in self.context.all_points():
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, pid)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.points_list.addItem(item)
        left.addWidget(self.points_list, 1)
        row = QHBoxLayout()
        row.setSpacing(4)
        for text, slot in (
            ("示波器里的", self._check_scope_points),
            ("全不选", lambda: self._check_all(False)),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, s=slot: s())
            row.addWidget(button)
        left.addLayout(row)
        layout.addLayout(left, 1)

        right = QVBoxLayout()
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.pre_box = self._seconds_box(1.0, "触发前留多久（默认 1 秒）")
        form.addRow("触发前", self.pre_box)
        self.post_box = self._seconds_box(1.0, "触发后继续录多久（默认 1 秒）")
        form.addRow("触发后", self.post_box)

        self.fields = build_trigger_fields(
            self.context, default_compare=Compare.GE, default_mode=TriggerMode.RISING
        )
        for label, widget in self.fields.rows():
            form.addRow(label, widget)
        right.addLayout(form)

        self.export_box = QCheckBox("同时导出 CSV（方便发给别人）")
        self.export_box.setChecked(True)
        right.addWidget(self.export_box)

        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        default_dir = self.context.report_subdir("faults")
        self.dir_edit = QLineEdit(default_dir)
        dir_row.addWidget(self.dir_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(lambda _checked=False: self._pick_dir())
        dir_row.addWidget(browse)
        right.addLayout(dir_row)

        self.max_box = QDoubleSpinBox()
        self.max_box.setRange(0.0, 600.0)
        self.max_box.setDecimals(0)
        self.max_box.setValue(60.0)
        self.max_box.setSuffix(" 秒")
        self.max_box.setToolTip("一条记录最长录多久，0 = 不限制（那就得手动停）")
        no_arrows(self.max_box)
        right.addWidget(self.max_box)
        right.addStretch(1)
        layout.addLayout(right, 1)
        return box

    @staticmethod
    def _seconds_box(value: float, tip: str) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0.0, 60.0)
        box.setDecimals(2)
        box.setValue(value)
        box.setSuffix(" 秒")
        box.setToolTip(tip)
        no_arrows(box)
        return box

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择保存目录", self.dir_edit.text())
        if chosen:
            self.dir_edit.setText(chosen)

    def _build_list(self) -> QWidget:
        box = QGroupBox("已录到的波形")
        layout = QVBoxLayout(box)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for index, width in enumerate((150, 330, 230, 90, 80)):
            self.table.setColumnWidth(index, width)
        self.table.doubleClicked.connect(lambda _index: self._open_csv())
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        open_csv = QPushButton("打开 CSV")
        open_csv.clicked.connect(lambda _checked=False: self._open_csv())
        row.addWidget(open_csv)
        open_dir = QPushButton("打开保存目录")
        open_dir.clicked.connect(lambda _checked=False: self._open_dir())
        row.addWidget(open_dir)
        reload_btn = QPushButton("刷新清单")
        reload_btn.clicked.connect(lambda _checked=False: self._load_saved())
        row.addWidget(reload_btn)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.start_button = QPushButton("开始监视 / 加为触发任务")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(lambda _checked=False: self.start())
        row.addWidget(self.start_button)

        self.stop_button = QPushButton("停止监视")
        self.stop_button.setObjectName("danger")
        self.stop_button.clicked.connect(lambda _checked=False: self.stop())
        row.addWidget(self.stop_button)

        manual = QPushButton("手动录一段")
        manual.setToolTip("不等触发条件，立刻按当前缓冲录一段（前段就是刚刚过去的这段时间）")
        manual.clicked.connect(lambda _checked=False: self.manual_capture())
        row.addWidget(manual)
        row.addStretch(1)
        return row

    # -------------------------------------------------- 选择

    def _check_all(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for index in range(self.points_list.count()):
            self.points_list.item(index).setCheckState(state)

    def _check_scope_points(self) -> None:
        """默认勾"示波器里的"：数组点位 + 参数表里标了示波器的量。"""
        wanted = {
            f"{device.name}.{point.name}"
            for device, point in self.context.project.iter_points()
            if point.scope or point.is_array or point.display == "wave"
        }
        for index in range(self.points_list.count()):
            item = self.points_list.item(index)
            item.setCheckState(
                Qt.Checked if item.data(Qt.UserRole) in wanted else Qt.Unchecked
            )

    def selected_pids(self) -> list[str]:
        return [
            self.points_list.item(index).data(Qt.UserRole)
            for index in range(self.points_list.count())
            if self.points_list.item(index).checkState() == Qt.Checked
        ]

    # -------------------------------------------------- 录制

    def _config(self) -> fault_core.FaultConfig:
        return fault_core.FaultConfig(
            pids=self.selected_pids(),
            pre_s=self.pre_box.value(),
            post_s=self.post_box.value(),
            csv_dir=self.dir_edit.text().strip(),
            export_csv=self.export_box.isChecked(),
        ).normalized()

    def start(self) -> None:
        if self.recorder is None:
            self.status.setText("录波缓冲不可用（应从主窗口打开本工具）")
            return
        pids = self.selected_pids()
        if not pids:
            self.status.setText("请先勾选要监视的量（可以点「示波器里的」）")
            return
        try:
            trigger = read_trigger_fields(self.fields)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        if self.service is None:
            self.status.setText("自动化核心不可用，触发不会生效")
            return

        self.recorder.configure(self._config(), self.context.project)
        self.recorder.claim(self)  # 缓冲是共享的，先认领再注册任务
        name = f"故障录波：{trigger_summary(trigger, self.context)}"
        self.service.remove(name)
        self.service.add(
            Task(
                name=name,
                trigger=trigger,
                action=lambda task, _values: self._on_trigger(trigger),
                # 录波是"再来一次就再录一段"，所以不设次数上限
                max_fires=0,
            )
        )
        self._task_name = name
        self.status.setText(
            f"监视中：{len(pids)} 个量，前 {self.pre_box.value():g}s + 后 {self.post_box.value():g}s"
            f"　等触发：{trigger.describe()}"
        )
        self._sync_state()

    def stop(self) -> None:
        if self._task_name and self.service is not None:
            self.service.remove(self._task_name)
        self._task_name = ""
        if self.recorder is not None:
            # 只有发起者才有权清缓冲，别把另一个窗口正在等的段清掉
            if self.recorder.is_owner(self):
                self.recorder.clear()
                self.recorder.release(self)
        self.status.setText("已停止监视")
        self._sync_state()

    def _on_trigger(self, trigger) -> None:
        if self.recorder is None or not self.recorder.is_owner(self):
            return
        if self.recorder.trigger(trigger_summary(trigger, self.context)):
            self.status.setText(f"已触发，正在录（触发后 {self.post_box.value():g} 秒）…")

    def manual_capture(self) -> None:
        if self.recorder is None:
            self.status.setText("录波缓冲不可用")
            return
        self.recorder.claim(self)
        if self.recorder.trigger("手动"):
            capture = self.recorder.flush()
            self._finish(capture)

    def _tick(self) -> None:
        if self.recorder is None:
            return
        # 缓冲是所有「故障录波」窗口共用的：谁认领的谁收尾。
        # 不设这个判断的话，第二个窗口会先跑到 tick() 把成品拿走，
        # 于是 CSV 落到那个窗口的目录里、状态也显示在那边。
        if not self.recorder.is_owner(self):
            if self.status.text().startswith("监视中"):
                self.status.setText("另一个「故障录波」窗口正在监视，本窗口空闲")
            return
        # 单条最长时长到了就收尾，别让一次故障把内存吃满
        limit = float(self.max_box.value())
        if self.recorder.recording and limit > 0 and self.recorder.recording_for() >= limit:
            self._finish(self.recorder.flush())
            return
        capture = self.recorder.tick()
        if capture is not None:
            self._finish(capture)
            return
        if self._task_name:
            counts = self.recorder.buffered()
            remembered = ", ".join(f"{self.context.title_of(pid)}:{n}" for pid, n in list(counts.items())[:3])
            self.status.setText(
                f"监视中（{len(counts)} 个量已入缓冲）　{remembered}"
                + (f"　已录 {self.recorder.fired} 次" if self.recorder.fired else "")
            )

    def _finish(self, capture: fault_core.FaultCapture | None) -> None:
        if capture is None or not capture.series:
            self.status.setText("这一段没有数据（触发太快，缓冲还没攒起来）")
            return
        if not self.recorder.is_owner(self):
            return  # 不是本窗口认领的这一段，交给认领它的窗口去存
        exporter = self._export_csv if self.export_box.isChecked() else None
        saved = fault_core.save_capture(capture, self.context.storage, exporter)
        self.status.setText(
            f"已录：{capture.summary()}　入库 {saved} 条"
            + (f"　CSV：{os.path.basename(capture.csv_path)}" if capture.csv_path else "")
        )
        self.context.report(f"故障录波完成：{capture.summary()}")
        self.context.announce("故障录波完成", capture.summary(), "info")
        self._load_saved()

    # -------------------------------------------------- 导出与清单

    def _export_csv(self, capture: fault_core.FaultCapture, reason: str) -> str:
        directory = self.dir_edit.text().strip() or "."
        os.makedirs(directory, exist_ok=True)
        safe = "".join(ch for ch in reason if ch not in '<>:"/\\|?*').strip() or "fault"
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(capture.triggered_at))
        path = os.path.join(directory, f"fault_{stamp}_{safe}.csv")
        index = 2
        while os.path.exists(path):
            path = os.path.join(directory, f"fault_{stamp}_{safe}_{index}.csv")
            index += 1

        pids = list(capture.series)
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["# 触发", reason])
            writer.writerow(["# 触发时间", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(capture.triggered_at))])
            writer.writerow(["# 前/后时长(秒)", capture.pre_s, capture.post_s])
            writer.writerow(["# 采样间隔(us)"] + [capture.intervals.get(pid, 0) for pid in pids])
            writer.writerow(["序号"] + [self.context.title_of(pid) for pid in pids])
            longest = max(len(capture.series[pid]) for pid in pids)
            for row in range(longest):
                line: list = [row]
                for pid in pids:
                    values = capture.series[pid]
                    line.append(f"{values[row]:g}" if row < len(values) else "")
                writer.writerow(line)
        return path

    def _load_saved(self) -> None:
        """列出录波表里最近的记录（也就是这个工具录到的 + 参数表里勾了「录波」的）。"""
        self.table.setRowCount(0)
        if self.context.storage is None:
            return
        try:
            rows = self.context.storage.query_waveform_index(limit=200)
        except Exception:  # noqa: BLE001 - 库读不出来不该影响界面
            return
        self._saved = rows
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row.get("ts") or 0))
            pid = row.get("pid", "")
            cells = [
                when,
                self.context.title_of(pid),
                pid.split(".")[-1],
                str(row.get("n", 0)),
                str(row.get("dt_us", 0)),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column >= 3:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(index, column, item)

    def _selected_row(self) -> dict | None:
        row = self.table.currentRow()
        saved = getattr(self, "_saved", [])
        if 0 <= row < len(saved):
            return saved[row]
        return None

    def _open_csv(self) -> None:
        directory = self.dir_edit.text().strip()
        if not directory or not os.path.isdir(directory):
            self.status.setText("还没有导出过 CSV")
            return
        files = sorted(
            (entry for entry in os.listdir(directory) if entry.lower().endswith(".csv")),
            reverse=True,
        )
        if not files:
            self.status.setText(f"{directory} 里还没有 CSV")
            return
        os.startfile(os.path.join(directory, files[0]))  # noqa: S606 - 用户要求打开文件
        self.status.setText(f"已打开 {files[0]}")

    def _open_dir(self) -> None:
        directory = self.dir_edit.text().strip()
        if not directory:
            self.status.setText("先选一个保存目录")
            return
        os.makedirs(directory, exist_ok=True)
        os.startfile(directory)  # noqa: S606
        self.status.setText(f"已打开 {directory}")

    def _sync_state(self) -> None:
        active = bool(self._task_name)
        self.stop_button.setEnabled(active)
        self.start_button.setText("更新触发任务" if active else "开始监视 / 加为触发任务")

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        # 关窗就停：没人看着还一直往回记波形没意义，占内存
        self.stop()
        super().closeEvent(event)


@register_tool
class FaultRecordTool(WindowTool):
    name = "故障录波"
    group = "自动化"
    order = 20
    description = "报警或自选变量触发时，自动记录前后一段波形（前后时长可选，默认各 1s）"

    def build(self, context: ToolContext) -> QWidget:
        return FaultRecordWindow(context)
