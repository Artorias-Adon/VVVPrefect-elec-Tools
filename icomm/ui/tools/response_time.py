"""波形响应时间：从一段录波里算上升时间、建立时间、超调这些指标。

现场为什么要它：调环路、改参数以后最关心的就是"阶跃上去多久到位、冲多高"，
以前只能放大波形拿光标一点点量。这里直接把指标算出来，并把光标摆到
低/高阈值处 —— 光标之间那段 Δt 就是上升时间，可以互相印证。

数据来源有两处：
* 工程里的波形点位（数组点位）—— 直接用实时表里最新那段，改参数就能看变化；
* 录波历史里存下来的某一段 —— 故障录波抓到的那些。

指标计算在 :func:`icomm.core.analysis.step_response`，纯逻辑、可单测。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import analysis, sheetio
from ...core.models import point_id
from .. import theme
from ..scope_canvas import ScopeCanvas, ScopeChannel
from .base import ToolContext, WindowTool, register_tool

#: 跟随实时数据刷新的节奏（毫秒）
REFRESH_MS = 400


class ResponseTimeWindow(QWidget):
    """算波形响应时间的窗口。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self._sources: list[tuple[str, str]] = []  # (显示名, 键)
        self._wave: list[float] = []
        self._dt_s = 0.0
        self._result: analysis.StepResult | None = None

        self.setWindowTitle(f"波形响应时间 · {context.project.name}")
        # 有父窗口时 QWidget 默认是**子控件**（没有标题栏、关不掉）——
        # 必须显式声明成独立工具窗口，跟其它非模态工具保持一致
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(760, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 8, 10, 8)
        root.setSpacing(6)
        root.addLayout(self._build_bar())
        root.addLayout(self._build_params())

        # Esc 关窗（QWidget 不像 QDialog 那样自带）
        self._close_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self._close_shortcut.activated.connect(self.close)

        self.canvas = ScopeCanvas()
        self.canvas.setMinimumHeight(220)
        root.addWidget(self.canvas, 2)

        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["指标", "数值"])
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setMaximumHeight(240)
        root.addWidget(self.table, 1)

        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(REFRESH_MS)
        self.reload_sources()

    # -------------------------------------------------- 界面

    def _build_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addWidget(QLabel("数据来源"))
        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(280)
        self.source_combo.currentIndexChanged.connect(self.recompute)
        bar.addWidget(self.source_combo, 1)

        refresh = QPushButton("刷新列表")
        refresh.clicked.connect(self.reload_sources)
        bar.addWidget(refresh)

        self.auto_box = QCheckBox("跟随实时")
        self.auto_box.setChecked(True)
        self.auto_box.setToolTip("波形点位每次采回来就重算一遍；录波历史只需算一次")
        bar.addWidget(self.auto_box)

        bar.addStretch(1)
        self.format_combo = QComboBox()
        self.format_combo.addItem("CSV（.csv）", ".csv")
        self.format_combo.addItem("Excel（.xlsx）", ".xlsx")
        self.format_combo.setToolTip("导出格式；文件直接落到工程报表目录")
        bar.addWidget(self.format_combo)
        export = QPushButton("导出结果")
        export.clicked.connect(self.export_csv)
        bar.addWidget(export)

        # 显式关闭按钮：工具窗口在某些主题/远程桌面下没有标题栏 X，留一个总关得掉
        close = QPushButton("关闭")
        close.setToolTip("关闭这个窗口（也可以按 Esc）")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        return bar

    def _build_params(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)
        bar.addWidget(QLabel("低阈值"))
        self.low_box = self._ratio_spin(0.1)
        bar.addWidget(self.low_box)
        bar.addWidget(QLabel("高阈值"))
        self.high_box = self._ratio_spin(0.9)
        bar.addWidget(self.high_box)
        bar.addWidget(QLabel("建立误差带"))
        self.band_box = self._ratio_spin(0.02)
        bar.addWidget(self.band_box)
        bar.addStretch(1)
        note = QLabel("阈值是相对幅值的比例：10%/90% 就是常说的上升时间")
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        bar.addWidget(note)
        return bar

    def _ratio_spin(self, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 1.0)
        spin.setDecimals(3)
        spin.setSingleStep(0.01)
        spin.setValue(value)
        spin.setMaximumWidth(90)
        spin.setKeyboardTracking(False)
        spin.valueChanged.connect(self.recompute)
        return spin

    # -------------------------------------------------- 数据来源

    def reload_sources(self) -> None:
        """列出可选来源：工程里的波形点位 + 录波历史里存下来的片段。"""
        current = self.source_combo.currentData()
        self._sources = []
        for device, point in self.context.project.iter_points():
            if point.is_array:
                self._sources.append(
                    (f"实时 · {device.name}.{point.title}", f"live:{point_id(device.name, point.name)}")
                )
        storage = getattr(self.context, "storage", None)
        if storage is not None:
            try:
                rows = storage.query_waveform_index(None, limit=100)
            except Exception:  # noqa: BLE001 - 历史库读不出来不该让工具打不开
                rows = []
            for row in rows:
                stamps = time.strftime("%H:%M:%S", time.localtime(row["ts"]))
                self._sources.append(
                    (
                        f"录波 #{row['id']} · {row['pid']} · {stamps}（{row['n']} 点）",
                        f"wave:{row['id']}",
                    )
                )

        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        for label, key in self._sources:
            self.source_combo.addItem(label, key)
        index = self.source_combo.findData(current)
        self.source_combo.setCurrentIndex(index if index >= 0 else 0)
        self.source_combo.blockSignals(False)

        if not self._sources:
            self.hint.setText(
                "没有可用的波形点位 —— 需要工程里有「数组/波形」点位，或者已有录波记录"
            )
        self.recompute()

    def _fetch(self) -> tuple[list[float], float, str]:
        """按当前来源取一段波形：``(数据, 采样间隔秒, 说明)``。"""
        key = self.source_combo.currentData()
        if not key:
            return [], 0.0, ""
        kind, _, payload = str(key).partition(":")
        if kind == "live":
            found = self.context.project.find_point(payload)
            tag = self.context.tag_of(payload)
            if found is None or not isinstance(tag.value, (list, tuple)):
                return [], 0.0, "还没有采到波形数据（先启动采集）"
            _device, point = found
            return (
                [float(value) for value in tag.value],
                (point.sample_interval_us or 0) / 1e6,
                f"{payload}（采样间隔 {point.sample_interval_us or '未知'} µs）",
            )
        storage = getattr(self.context, "storage", None)
        if storage is None:
            return [], 0.0, "没有接上历史库"
        rows = {str(row["id"]): row for row in storage.query_waveform_index(None, limit=200)}
        row = rows.get(payload)
        if row is None:
            return [], 0.0, "这条录波已经不在了（可能被清理过）"
        values = storage.query_waveform(int(payload))
        return (
            values,
            (row.get("dt_us") or 0) / 1e6,
            f"录波 #{payload}（点号 {row['pid']}，采样间隔 {row.get('dt_us') or '未知'} µs）",
        )

    # -------------------------------------------------- 计算

    def _on_tick(self) -> None:
        if not self.auto_box.isChecked():
            return
        key = str(self.source_combo.currentData() or "")
        if key.startswith("live:"):
            self.recompute()

    def recompute(self) -> None:
        values, dt_s, note = self._fetch()
        self._wave = values
        self._dt_s = dt_s
        self._show_wave(values, dt_s)

        result = analysis.step_response(
            values,
            dt_s,
            low=self.low_box.value(),
            high=self.high_box.value(),
            settle_band=self.band_box.value(),
        )
        self._result = result
        self._fill_table(result)
        if result is None:
            self.hint.setText(
                (note + "　｜　" if note else "")
                + "算不出响应时间：需要一段有明显跳变的波形（幅值变化太小的纹波/直流不算）"
            )
            return
        self.hint.setText(
            (note + "　｜　" if note else "")
            + f"上升时间 {analysis.format_time(result.rise_time)}　"
            f"建立时间 {analysis.format_time(result.settle_time)}　"
            f"超调 {result.overshoot_pct:.2f}%"
        )
        self._place_cursors(result)

    def _show_wave(self, values: list[float], dt_s: float) -> None:
        """把这段波形画到画布上（复用示波器画布，滚动缩放/光标都能用）。"""
        channel = self.canvas.channel("响应")
        if channel is None:
            channel = ScopeChannel(
                pid="响应",
                name="响应",
                unit="",
                color=theme.ACCENT,
                visible=True,
                kind="array",
            )
            self.canvas.set_channels([channel])
        dt_us = int(round(dt_s * 1e6)) if dt_s else 0
        # 画布画的是"整段铺满"，时基按整段时长给，波形才不会被截掉
        self.canvas.set_timebase(max((max(2, len(values)) - 1) * dt_s / 10.0, 1e-9))
        self.canvas.set_waveform("响应", list(values), dt_us)
        self.canvas.update()

    def _place_cursors(self, result: analysis.StepResult) -> None:
        """把两个光标摆到低/高阈值时刻 —— 它们之间的 Δt 就是上升时间，可互相印证。"""
        duration = result.duration
        if duration <= 0:
            return
        low = result.delay_time or 0.0
        high = low + (result.rise_time or 0.0)
        self.canvas.set_cursors(
            min(max(low / duration, 0.0), 1.0), min(max(high / duration, 0.0), 1.0)
        )
        self.canvas.show_cursors = True

    def _fill_table(self, result: analysis.StepResult | None) -> None:
        rows = result.rows() if result is not None else [("（算不出来）", "波形没有明显跳变")]
        self.table.setRowCount(len(rows))
        for index, (name, text) in enumerate(rows):
            self.table.setItem(index, 0, QTableWidgetItem(name))
            self.table.setItem(index, 1, QTableWidgetItem(text))

    # -------------------------------------------------- 导出

    def export_csv(self, path: str = "") -> None:
        """导出结果（默认直接落到报表目录；扩展名按「导出格式」下拉）。

        这个工具以前就是"点了就出文件、不弹保存框"，保持这个手感；
        要 Excel 就把下拉切到 .xlsx。
        """
        result = self._result
        if result is None:
            QMessageBox.information(self, "没有结果", "先选一段有明显跳变的波形再来导出。")
            return
        suffix = self.format_combo.currentData() or ".csv"
        if not path:
            path = self.context.output_path(
                "response", f"响应时间_{time.strftime('%Y%m%d_%H%M%S')}{suffix}"
            )
        key = str(self.source_combo.currentData() or "")
        rows: list[list] = [["指标", "数值"], ["数据来源", key]]
        rows.append(
            ["阈值", f"{self.low_box.value():.3f}-{self.high_box.value():.3f}"
                     f"（误差带 ±{self.band_box.value():.3f}）"]
        )
        rows.extend([name, text] for name, text in result.rows())
        try:
            sheetio.write_rows(path, rows, sheet_title="响应时间")
        except (OSError, sheetio.SheetError) as exc:
            QMessageBox.critical(self, "导出失败", f"响应时间结果写入失败：\n{exc}")
            return
        self.hint.setText(f"已导出：{path}")

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        super().closeEvent(event)


@register_tool
class ResponseTimeTool(WindowTool):
    name = "波形响应时间"
    group = "计算"
    order = 40
    description = "从录波里算上升时间、建立时间、超调（可导出）"

    def build(self, context: ToolContext) -> QWidget:
        return ResponseTimeWindow(context)
