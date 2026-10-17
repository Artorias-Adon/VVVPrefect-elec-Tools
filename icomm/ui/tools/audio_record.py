"""录音工具：某个通道到值就自动开录，录够时长自动停。

用的是 Windows 自带的 winmm（:mod:`icomm.core.audio`），不加任何第三方依赖。
录音期间界面照常能用 —— 采集在音频线程、落盘在后台线程，界面只显示进度。

现场最常见的用法是"抓故障时的声音"：继电器动作、放电、风机异响。
所以触发条件、去抖、冷却这些跟截屏/录波完全一样（共用自动化核心）。
"""

from __future__ import annotations

import os
import time
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import audio
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

#: 界面刷新间隔（毫秒）
TICK_MS = 200

RATES = (8000, 16000, 22050, 44100)
COLUMNS = ["时间", "文件", "时长", "大小"]
#: 现场录音默认 30 秒够用了，再长也没人愿意听
DEFAULT_MAX_SECONDS = 30.0

RecorderFactory = Callable[..., object]


class AudioRecordWindow(QWidget):
    """配置"到条件就录音"并管理录到的文件。"""

    def __init__(
        self,
        context: ToolContext,
        parent: QWidget | None = None,
        recorder_factory: RecorderFactory | None = None,
    ) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.service = context.service(AUTOMATION_SERVICE)
        self._factory = recorder_factory or audio.make_recorder
        self._recorder: object | None = None
        self._task_name = ""
        self._files: list[dict] = []
        self._reason = ""
        self._path = ""

        self.setWindowTitle(f"录音 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(820, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        root.addWidget(self._build_hint())
        root.addWidget(self._build_config())
        root.addWidget(self._build_files(), 1)
        root.addLayout(self._build_buttons())

        self.status = QLabel("就绪")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self.refresh_files()
        self._report_device()

    # -------------------------------------------------- 界面

    def _build_hint(self) -> QWidget:
        label = QLabel(
            "用系统自带的录音设备（Windows winmm，不装额外软件）。条件成立就开始录，"
            "到设定时长自动停；也可以手动录一段。\n"
            "录音期间界面照常可用：采样在音频线程、写盘在后台线程，界面只显示进度。"
        )
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        return label

    def _build_config(self) -> QWidget:
        box = QGroupBox("录音与触发")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.device_combo = QComboBox()
        self._devices = audio.devices()
        if self._devices:
            for device in self._devices:
                self.device_combo.addItem(f"{device.name}（{device.channels} 声道）", device.index)
        else:
            self.device_combo.addItem("（没有找到录音设备）", 0)
        form.addRow("录音设备", self.device_combo)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.rate_combo = QComboBox()
        for rate in RATES:
            self.rate_combo.addItem(f"{rate} Hz", rate)
        self.rate_combo.setCurrentIndex(RATES.index(audio.DEFAULT_RATE))
        row.addWidget(self.rate_combo)

        self.channels_combo = QComboBox()
        self.channels_combo.addItem("单声道", 1)
        self.channels_combo.addItem("双声道", 2)
        row.addWidget(self.channels_combo)

        self.max_box = QDoubleSpinBox()
        self.max_box.setRange(1.0, audio.MAX_DURATION_S)
        self.max_box.setDecimals(0)
        self.max_box.setValue(DEFAULT_MAX_SECONDS)
        self.max_box.setSuffix(" 秒")
        self.max_box.setToolTip("一次最长录多久，到点自动停（防止没人看着把磁盘写满）")
        no_arrows(self.max_box)
        row.addWidget(self.max_box)
        row.addStretch(1)
        form.addRow("采样 / 声道 / 最长", row)

        self.fields = build_trigger_fields(
            self.context, default_compare=Compare.GE, default_mode=TriggerMode.RISING
        )
        for label, widget in self.fields.rows():
            form.addRow(label, widget)

        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        self.dir_edit = QLineEdit(self.context.report_subdir("audio"))
        dir_row.addWidget(self.dir_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(lambda _checked=False: self._pick_dir())
        dir_row.addWidget(browse)
        form.addRow("保存目录", dir_row)
        return box

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择保存目录", self.dir_edit.text())
        if chosen:
            self.dir_edit.setText(chosen)

    def _build_files(self) -> QWidget:
        box = QGroupBox("已录到的文件")
        layout = QVBoxLayout(box)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for index, width in enumerate((150, 330, 90, 100)):
            self.table.setColumnWidth(index, width)
        self.table.doubleClicked.connect(lambda _index: self.play_selected())
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        play = QPushButton("播放选中的")
        play.clicked.connect(lambda _checked=False: self.play_selected())
        row.addWidget(play)
        reveal = QPushButton("打开目录")
        reveal.clicked.connect(lambda _checked=False: self.open_dir())
        row.addWidget(reveal)
        refresh = QPushButton("刷新清单")
        refresh.clicked.connect(lambda _checked=False: self.refresh_files())
        row.addWidget(refresh)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.start_button = QPushButton("开始监视 → 到条件就录音")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(lambda _checked=False: self.start())
        row.addWidget(self.start_button)

        self.stop_button = QPushButton("停止监视")
        self.stop_button.setObjectName("danger")
        self.stop_button.clicked.connect(lambda _checked=False: self.stop())
        row.addWidget(self.stop_button)

        manual = QPushButton("手动录一段")
        manual.setToolTip("立刻开录，点「结束本次录音」收尾；连续录请用触发条件")
        manual.clicked.connect(lambda _checked=False: self.manual_recording())
        row.addWidget(manual)

        self.cut_button = QPushButton("结束本次录音")
        self.cut_button.setEnabled(False)
        self.cut_button.clicked.connect(lambda _checked=False: self.finish_recording("手动结束"))
        row.addWidget(self.cut_button)
        row.addStretch(1)
        return row

    # -------------------------------------------------- 设备

    def _report_device(self) -> None:
        ok, reason = self._factory_available()
        if ok:
            names = "、".join(device.name for device in self._devices[:2])
            self.status.setText(f"可用录音设备：{names}" + ("…" if len(self._devices) > 2 else ""))
            return
        self.status.setText(f"⚠ {reason}：可以先配置好，等有设备时再开始监视")

    def _factory_available(self) -> tuple[bool, str]:
        """问录音实现能不能用（注入的假实现就不用问了）。"""
        if self._factory is not audio.make_recorder:
            return True, ""
        return audio.WinmmRecorder.available()

    # -------------------------------------------------- 录制

    def _new_recorder(self, path: str) -> object:
        return self._factory(
            path=path,
            rate=int(self.rate_combo.currentData() or audio.DEFAULT_RATE),
            channels=int(self.channels_combo.currentData() or 1),
            device_index=int(self.device_combo.currentData() or 0),
            max_seconds=float(self.max_box.value()),
        )

    def _next_path(self) -> str:
        directory = self.dir_edit.text().strip() or self.context.report_subdir("audio")
        os.makedirs(directory, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(directory, f"audio_{stamp}.wav")
        index = 2
        while os.path.exists(path):
            path = os.path.join(directory, f"audio_{stamp}_{index}.wav")
            index += 1
        return path

    def start(self) -> None:
        try:
            trigger = read_trigger_fields(self.fields)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        if self.service is None:
            self.status.setText("自动化核心不可用，触发不会生效")
            return
        ok, reason = self._factory_available()
        if not ok:
            self.status.setText(f"⚠ {reason}（先插上麦克风/声卡输入再开始监视）")
            return

        name = f"录音：{trigger_summary(trigger, self.context)}"
        self.service.remove(name)
        self.service.add(
            Task(
                name=name,
                trigger=trigger,
                action=lambda task, _values: self.begin_recording(trigger_summary(trigger, self.context)),
                # 每次触发都录一段，不限次数
                max_fires=0,
            )
        )
        self._task_name = name
        self.status.setText(f"监视中：等触发 {trigger.describe()}")
        self._sync_buttons()

    def stop(self) -> None:
        self.finish_recording("停止监视")
        if self._task_name and self.service is not None:
            self.service.remove(self._task_name)
        self._task_name = ""
        self.status.setText("已停止监视")
        self._sync_buttons()

    def begin_recording(self, reason: str = "手动") -> None:
        """开始录一段（触发回调与"手动录一段"都走这里）。"""
        if self._recorder is not None:
            return  # 正在录，不打断
        path = self._next_path()
        recorder = self._new_recorder(path)
        if not recorder.open():  # type: ignore[attr-defined]
            reason_text = getattr(recorder, "error", "") or "打开录音设备失败"
            self.status.setText(f"⚠ {reason_text}")
            return
        self._recorder = recorder
        self._reason = reason
        self._path = path
        self.status.setText(f"录音中（{reason}）…")
        self._sync_buttons()

    def finish_recording(self, reason: str = "结束") -> None:
        recorder = self._recorder
        if recorder is None:
            return
        self._recorder = None
        duration = float(getattr(recorder, "duration", 0.0))
        recorder.close()  # type: ignore[attr-defined]
        path = getattr(self, "_path", "")
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        self.status.setText(
            f"已保存 {os.path.basename(path)}（{reason}）：{duration:.1f} 秒 / {size / 1024:.0f} KB"
        )
        self.context.report(f"录音完成：{os.path.basename(path)}　{duration:.1f} 秒")
        self.context.announce(
            "录音完成", f"{os.path.basename(path)}　{duration:.1f} 秒", "info"
        )
        self.refresh_files()
        self._sync_buttons()

    def manual_recording(self) -> None:
        """手动开始录一段（再点「结束本次录音」收尾）。"""
        self.begin_recording("手动")

    def _tick(self) -> None:
        recorder = self._recorder
        if recorder is not None:
            duration = float(getattr(recorder, "duration", 0.0))
            size = float(getattr(recorder, "size", 0.0))
            self.status.setText(
                f"录音中（{getattr(self, '_reason', '')}）　{duration:.1f} 秒 / {size / 1024:.0f} KB"
            )
            stop_reason = recorder.should_stop()  # type: ignore[attr-defined]
            if stop_reason:
                self.finish_recording(stop_reason)
            return
        if self._task_name:
            self.status.setText(f"监视中：等触发（已录 {len(self._files)} 段）")

    def _sync_buttons(self) -> None:
        recording = self._recorder is not None
        self.cut_button.setEnabled(recording)
        self.stop_button.setEnabled(bool(self._task_name) or recording)
        self.start_button.setText("更新触发任务" if self._task_name else "开始监视 → 到条件就录音")

    # -------------------------------------------------- 文件

    def refresh_files(self) -> None:
        directory = self.dir_edit.text().strip()
        entries: list[dict] = []
        if directory and os.path.isdir(directory):
            for name in sorted(os.listdir(directory), reverse=True):
                if not name.lower().endswith(".wav"):
                    continue
                path = os.path.join(directory, name)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                # 16 位单/双声道：数据字节数可以还原时长，不用读 WAV 头
                block = 2 * max(1, int(self.channels_combo.currentData() or 1))
                rate = int(self.rate_combo.currentData() or audio.DEFAULT_RATE)
                seconds = max(0, stat.st_size - audio.WavWriter.HEADER_SIZE) / (rate * block)
                entries.append(
                    {
                        "path": path,
                        "name": name,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                        "size": stat.st_size,
                        "seconds": seconds,
                    }
                )
        self._files = entries
        self.table.setRowCount(len(entries))
        for index, entry in enumerate(entries):
            cells = [
                entry["time"],
                entry["name"],
                f"{entry['seconds']:.1f}s",
                f"{entry['size'] / 1024:.0f} KB",
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column >= 2:
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(index, column, item)

    def play_selected(self) -> None:
        row = self.table.currentRow()
        if not (0 <= row < len(self._files)):
            self.status.setText("先选一段录音")
            return
        path = self._files[row]["path"]
        try:
            os.startfile(path)  # noqa: S606 - 用户要求播放
        except OSError as exc:
            self.status.setText(f"打不开 {path}：{exc}")
            return
        self.status.setText(f"已用系统播放器打开 {os.path.basename(path)}")

    def open_dir(self) -> None:
        directory = self.dir_edit.text().strip()
        if not directory:
            self.status.setText("先选一个保存目录")
            return
        os.makedirs(directory, exist_ok=True)
        os.startfile(directory)  # noqa: S606
        self.status.setText(f"已打开 {directory}")

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        # 关窗就把触发任务摘掉，并结束正在录的那一段（不然 WAV 头是坏的）
        self.stop()
        super().closeEvent(event)


@register_tool
class AudioRecordTool(WindowTool):
    name = "录音"
    group = "自动化"
    order = 40
    description = "某个通道达到某个值时自动录音，录够时长自动停（系统自带 winmm，无需额外依赖）"

    def build(self, context: ToolContext) -> QWidget:
        return AudioRecordWindow(context)
