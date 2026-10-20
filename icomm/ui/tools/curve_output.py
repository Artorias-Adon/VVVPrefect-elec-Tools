"""曲线给定：让一个点位按随时间变化的曲线连续写值。

现场用途：
* 斜坡/阶跃试验 —— 给定从 0 爬到 100%，看设备响应、配合录波；
* 让功率/频率给定小幅正弦摆动，验证跟随与保护；
* 有脚本导出的曲线数据（CSV）时，按原速回放给设备。

安全上刻意做得"啰嗦"：开始前确认一次、只在采集运行时才发、到点自动停、
数值按点位的**小数位 / 整型 / 写值限幅**先量化再发；关窗立刻停。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core import curvegen, sheetio
from ...core.curvegen import CurvePlayer, CurveSpec
from .. import theme
from ..spin import no_arrows
from .analysis import CurvePlot
from .base import ToolContext, WindowTool, register_tool

_KIND_TIPS = {
    "sine": "正弦：offset ± amplitude，周期 period",
    "triangle": "三角波：线性上升再线性下降",
    "square": "方波：按占空比在高/低之间跳",
    "sawtooth": "锯齿：从 -A 线性升到 +A 然后回绕",
    "ramp": "斜坡：从 -A 线性升到 +A，走完一个周期就停",
    "replay": "回放：读 CSV（一列值，或「时间,值」两列）",
}


class CurveWindow(QWidget):
    """曲线给定的界面：参数 + 预览 + 开始/停止。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.player: CurvePlayer | None = None
        self._started_at = 0.0
        self._sent_points: list[tuple[float, float]] = []
        self._pid = ""
        self._curve_spec = CurveSpec()
        self._status_problem = False

        self.setWindowTitle(f"曲线给定 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(1000, 700)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        body = QHBoxLayout()
        body.setSpacing(10)
        body.addWidget(self._build_form())
        body.addLayout(self._build_view(), 1)
        root.addLayout(body, 1)
        root.addLayout(self._build_buttons())

        self.status = QLabel("就绪")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._tick)

        self._refresh_points()
        self._sync_kind()
        self.update_preview()

    # -------------------------------------------------- 参数

    def _build_form(self) -> QWidget:
        box = QGroupBox("曲线与目标")
        box.setMaximumWidth(400)
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.point_combo = QComboBox()
        self.point_combo.setEditable(True)
        self.point_combo.setInsertPolicy(QComboBox.NoInsert)
        self.point_combo.setToolTip("只列可写的数值点位（bool / 命令点位用按键或输入框更合适）")
        self.point_combo.currentIndexChanged.connect(lambda _index: self.update_preview())
        form.addRow("目标点位", self.point_combo)

        self.kind_combo = QComboBox()
        for key, label in curvegen.KINDS.items():
            self.kind_combo.addItem(label, key)
            self.kind_combo.setItemData(self.kind_combo.count() - 1, _KIND_TIPS.get(key, ""), Qt.ToolTipRole)
        self.kind_combo.currentIndexChanged.connect(lambda _index: self._sync_kind())
        form.addRow("曲线", self.kind_combo)

        self.amplitude_box = self._number(50.0, -1e9, 1e9, "", "幅度：曲线在偏置上下摆多少")
        self.offset_box = self._number(50.0, -1e9, 1e9, "", "偏置：曲线的中心值")
        self.period_box = self._number(10.0, 0.1, 86400.0, " s", "周期（斜坡就是「爬完用时」）")
        self.phase_box = self._number(0.0, -360.0, 360.0, " °", "起始相位")
        self.duty_box = self._number(50.0, 1.0, 99.0, " %", "方波占空比")
        form.addRow("幅度 ±", self.amplitude_box)
        form.addRow("偏置", self.offset_box)
        form.addRow("周期", self.period_box)
        form.addRow("相位", self.phase_box)
        form.addRow("占空比", self.duty_box)

        self.interval_box = self._number(
            0.5, curvegen.MIN_INTERVAL_S, 60.0, " s",
            "多久写一次。低于 0.2 秒可能挤占采集，一般 0.5~1 秒就够",
        )
        self.interval_box.setDecimals(2)
        self.duration_box = self._number(0.0, 0.0, 86400.0, " s", "持续多久自动停（0 = 一直发到手动停止）")
        self.duration_box.setDecimals(1)
        form.addRow("写值间隔", self.interval_box)
        form.addRow("持续时间", self.duration_box)

        self.loop_box = QCheckBox("回放时循环")
        self.loop_box.setChecked(True)
        self.loop_box.setToolTip("关掉就是「放一遍就停」，适合把一段录波原样回放给设备")
        form.addRow("", self.loop_box)

        row = QHBoxLayout()
        row.setSpacing(6)
        import_button = QPushButton("导入曲线…")
        import_button.setToolTip("CSV：一列数值，或「时间,值」两列")
        import_button.clicked.connect(self.import_curve)
        row.addWidget(import_button)
        self.replay_label = QLabel("尚未导入")
        self.replay_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        row.addWidget(self.replay_label, 1)
        form.addRow("", row)

        for widget in (self.amplitude_box, self.offset_box, self.period_box,
                       self.phase_box, self.duty_box, self.interval_box,
                       self.duration_box):
            widget.valueChanged.connect(lambda _value: self.update_preview())
        self.loop_box.toggled.connect(lambda _checked: self.update_preview())
        return box

    def _number(self, value: float, low: float, high: float, suffix: str = "", tip: str = "",
                decimals: int = 2):
        box = QDoubleSpinBox()
        box.setDecimals(decimals)
        box.setRange(low, high)
        box.setValue(value)
        box.setSuffix(suffix)
        if tip:
            box.setToolTip(tip)
        no_arrows(box)
        return box

    def _build_view(self) -> QVBoxLayout:
        layout = QVBoxLayout()
        layout.setSpacing(6)

        self.plot = CurvePlot()
        self.plot.set_axis(x_label="时间 (s)", y_label="给定值", zero_line=True)
        layout.addWidget(self.plot, 1)

        row = QHBoxLayout()
        row.setSpacing(12)
        self.value_label = QLabel("--")
        self.value_label.setStyleSheet(
            f"color: {theme.ACCENT}; font-size: 26px; font-weight: 600;"
        )
        row.addWidget(self.value_label)
        self.sent_label = QLabel("已发 0 次")
        self.sent_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        row.addWidget(self.sent_label)
        row.addStretch(1)
        self.elapsed_label = QLabel("0.0s")
        self.elapsed_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        row.addWidget(self.elapsed_label)
        layout.addLayout(row)
        return layout

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.start_button = QPushButton("开始按曲线写值")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(lambda _checked=False: self.start())
        row.addWidget(self.start_button)

        self.stop_button = QPushButton("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(lambda _checked=False: self.stop("已手动停止"))
        row.addWidget(self.stop_button)

        clear = QPushButton("清掉已发轨迹")
        clear.clicked.connect(lambda _checked=False: self._clear_trace())
        row.addWidget(clear)
        row.addStretch(1)

        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {theme.TEXT_DIM};")
        row.addWidget(self.summary)
        return row

    # -------------------------------------------------- 点位与曲线

    def _refresh_points(self) -> None:
        self.point_combo.blockSignals(True)
        self.point_combo.clear()
        for pid, title in self.context.writable_points():
            found = self.context.project.find_point(pid)
            if found is None:
                continue
            _device, point = found
            if point.data_type == "bool" or point.command_items():
                continue  # bool 与命令点位不适合按曲线连续写
            self.point_combo.addItem(title if title == pid else f"{title}（{pid}）", pid)
        self.point_combo.blockSignals(False)
        if self.point_combo.count():
            self.point_combo.setCurrentIndex(0)

    def current_point(self):
        pid = self.point_combo.currentData() or self.point_combo.currentText().strip()
        pid = str(pid or "")
        if "）" in pid and "（" in pid:
            pid = pid.rsplit("（", 1)[-1].rstrip("）")
        self._pid = pid
        return self.context.project.find_point(pid) if pid else None

    def _sync_kind(self) -> None:
        kind = str(self.kind_combo.currentData() or "sine")
        is_replay = kind == "replay"
        for widget in (self.amplitude_box, self.offset_box, self.phase_box):
            widget.setEnabled(not is_replay)
        self.period_box.setEnabled(not is_replay)
        self.duty_box.setEnabled(kind == "square")
        self.loop_box.setEnabled(is_replay)
        self.update_preview()

    def curve_spec(self) -> CurveSpec:
        kind = str(self.kind_combo.currentData() or "sine")
        spec = CurveSpec(
            kind=kind,
            amplitude=float(self.amplitude_box.value()),
            offset=float(self.offset_box.value()),
            period_s=float(self.period_box.value()),
            phase_deg=float(self.phase_box.value()),
            duty=float(self.duty_box.value()) / 100.0,
            samples=list(self._curve_spec.samples),
            sample_interval_s=self._curve_spec.sample_interval_s,
            loop=self.loop_box.isChecked(),
        )
        self._curve_spec = spec
        return spec

    def import_curve(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "导入回放曲线",
            "",
            sheetio.read_filter() + ";;曲线数据 (*.csv *.txt *.xlsx *.xls)",
        )
        if not path:
            return
        try:
            spec = curvegen.load_replay(path, loop=self.loop_box.isChecked())
        except ValueError as exc:
            QMessageBox.warning(self, "导入失败", str(exc))
            return
        self._curve_spec = spec
        index = self.kind_combo.findData("replay")
        self.kind_combo.blockSignals(True)
        self.kind_combo.setCurrentIndex(index)
        self.kind_combo.blockSignals(False)
        self.replay_label.setText(f"{len(spec.samples)} 点 / 间隔 {spec.sample_interval_s:.3g}s")
        self._sync_kind()
        self.status.setText(f"已导入回放曲线：{path}")

    # -------------------------------------------------- 预览

    def update_preview(self) -> None:
        spec = self.curve_spec()
        # 预览用独立的 player，绝不能碰正在跑的 self.player（会丢掉发送进度）
        previewer = CurvePlayer(spec, **self._player_options(self.current_point()))
        span = previewer.preview_span()
        duration = float(self.duration_box.value())
        if duration > 0:
            span = max(span, duration)
        points = previewer.preview(span)
        self.plot.clear()
        if points:
            self.plot.add("给定", points, theme.ACCENT, 2.0)
        if self._sent_points:
            self.plot.add("已发", list(self._sent_points), theme.QUALITY_GOOD, 2.0)
        self.plot.set_axis(x_label="时间 (s)", y_label="给定值", zero_line=True)
        self.summary.setText(spec.describe())
        if not points:
            self.status.setText(spec.validate() or "这条曲线没有可发的点")
            self._status_problem = True
        elif self._status_problem and not self._timer.isActive():
            self.status.setText("就绪")
            self._status_problem = False

    def _player_options(self, found) -> dict:
        point = found[1] if found else None
        integer = bool(point and point.data_type in ("uint8", "int8", "uint16", "int16",
                                                    "uint32", "int32", "coil"))
        return {
            "interval_s": float(self.interval_box.value()),
            "duration_s": float(self.duration_box.value()),
            "decimals": int(point.decimals) if point else 2,
            "low": point.write_min if point else None,
            "high": point.write_max if point else None,
            "integer": integer,
        }

    def _clear_trace(self) -> None:
        self._sent_points = []
        self.value_label.setText("--")
        self.sent_label.setText("已发 0 次")
        self.elapsed_label.setText("0.0s")
        self.update_preview()

    # -------------------------------------------------- 运行

    def start(self) -> None:
        if self.player is not None and not self.player.finished and self._timer.isActive():
            return  # 已经在跑
        found = self.current_point()
        if found is None:
            QMessageBox.information(self, "先选点位", "请选择一个可写的数值点位。")
            return
        device, point = found
        spec = self.curve_spec()
        problem = spec.validate()
        if problem:
            QMessageBox.warning(self, "曲线参数不对", problem)
            return
        if not self.context.engine_running():
            QMessageBox.information(self, "还没启动采集", "按曲线写值要经过采集引擎，请先「启动采集」。")
            return

        interval = float(self.interval_box.value())
        duration = float(self.duration_box.value())
        options = self._player_options(found)
        self.player = CurvePlayer(spec, **options)
        self.player.reset()
        self._sent_points = []

        limits = ""
        if point.write_min is not None or point.write_max is not None:
            limits = f"\n写值限幅：{point.write_limit_text()}（超出会被夹紧）"
        answer = QMessageBox.question(
            self,
            "确认按曲线写值",
            f"将对「{device.name}.{point.title}」每 {interval:g} 秒写一个值。\n"
            f"曲线：{spec.describe()}\n"
            f"持续：{'直到手动停止' if duration <= 0 else f'{duration:g} 秒'}{limits}\n\n"
            "确认开始？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return

        self._started_at = time.monotonic()
        self._set_running(True)
        self._timer.start(max(20, int(interval * 1000 / 4)))  # 内部 4 倍细粒度，保证按时发
        self.context.report(
            f"曲线给定已开始：{device.name}.{point.title} · {spec.describe()} · 每 {interval:g}s"
        )

    def stop(self, reason: str = "已停止") -> None:
        if self._timer.isActive():
            self._timer.stop()
        self._set_running(False)
        self.status.setText(reason)
        self.elapsed_label.setText(curvegen.format_seconds(self._elapsed()))
        self.update_preview()

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        for widget in (
            self.point_combo, self.kind_combo, self.amplitude_box, self.offset_box,
            self.period_box, self.phase_box, self.duty_box, self.interval_box,
            self.duration_box, self.loop_box,
        ):
            widget.setEnabled(not running)
        if running:
            self.status.setText("正在按曲线写值…")

    def _elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._started_at) if self._started_at else 0.0

    def _tick(self) -> None:
        player = self.player
        if player is None:
            return
        if not self.context.engine_running():
            self.stop("采集已停止，曲线给定自动停止")
            return
        elapsed = self._elapsed()
        value, finished = player.tick(elapsed)
        if value is not None:
            ok = self.context.send(self._pid, value)
            self.value_label.setText(f"{value:g}")
            self.sent_label.setText(f"已发 {player.sent} 次")
            self._sent_points.append((elapsed, value))
            if len(self._sent_points) > 2000:
                del self._sent_points[:500]
            if not ok:
                self.stop("写值通道不可用（采集未运行？）")
                return
            self.update_preview()
        self.elapsed_label.setText(curvegen.format_seconds(elapsed))
        if finished:
            reason = self._finish_reason()
            self.stop(reason)
            self.context.announce(
                "曲线给定完成",
                f"{self._pid} 共写 {player.sent} 次，用时 {curvegen.format_seconds(elapsed)}",
                "info",
            )

    def _finish_reason(self) -> str:
        """到点停了要说清是"时间到了"还是"曲线放完了"，现场看日志能分清。"""
        if float(self.duration_box.value()) > 0:
            return "按设定的持续时间停下来了"
        if self.curve_spec().one_shot:
            return "曲线走完，已自动停止"
        return "曲线给定已结束"

    def closeEvent(self, event) -> None:  # noqa: item2
        if self._timer.isActive():
            self._timer.stop()
            self.context.report(f"曲线给定已停止（关窗口）：{self._pid}")
        super().closeEvent(event)


@register_tool
class CurveOutputTool(WindowTool):
    name = "曲线给定"
    group = "自动化"
    order = 16
    description = "让点位按正弦/三角/方波/斜坡或导入的 CSV 曲线连续写值（做试验用）"

    def build(self, context: ToolContext) -> QWidget:
        return CurveWindow(context)


__all__ = ["CurveOutputTool", "CurveWindow"]
