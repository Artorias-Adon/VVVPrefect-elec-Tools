"""分析工具（4 个页签，放「工具 → 分析工具」）。

都是现场拧螺丝时真要算的东西，不用再开 Matlab：

1. **滤波器伯德图**：给 R/L/C 与结构，画幅频/相频，直接读谐振点、-3dB、工频增益；
2. **锁相**：拿一路电压波形的当前采样，算频率与相位角，画角度趋势（可选择参考相位）；
3. **功率因数 / 功率角**：选电压/电流/有功（或设备直接报的 PF），实时算 PF、φ、无功并留趋势；
4. **三相向量**：三个幅值点 + 三个相角（点或手输），画向量图并做正序/负序/零序分解。

算的部分全在 :mod:`icomm.core.analysis`（纯函数，有算例测试），这里只管界面与取数。
"""

from __future__ import annotations

import math
import os

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import analysis as an, sheetio
from .. import theme
from ..spin import no_arrows
from .base import ToolContext, WindowTool, register_tool

#: 三相向量图里三相的颜色
PHASE_COLORS = ("#ffcc44", "#4aa8ff", "#ff6b6b")


class CurvePlot(QWidget):
    """通用多曲线绘图（支持对数 X 轴）—— 伯德图/角度趋势/功率趋势共用。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.series: list[dict] = []
        self.log_x = False
        self.x_label = ""
        self.y_label = ""
        self.y_min: float | None = None
        self.y_max: float | None = None
        self.zero_line = False
        #: True = 画成柱状（谐波谱这种离散谱线适合柱状，折线会连出假斜率）
        self.bars = False
        self.setMinimumHeight(240)

    def clear(self) -> None:
        self.series = []
        self.update()

    def add(self, name: str, points: list[tuple[float, float]], color: str, width: float = 1.6) -> None:
        self.series.append({"name": name, "points": list(points), "color": color, "width": width})
        self.update()

    def set_axis(self, *, log_x: bool = False, x_label: str = "", y_label: str = "",
                 y_min: float | None = None, y_max: float | None = None,
                 zero_line: bool = False) -> None:
        self.log_x = log_x
        self.x_label = x_label
        self.y_label = y_label
        self.y_min = y_min
        self.y_max = y_max
        self.zero_line = zero_line
        self.update()

    def _bounds(self) -> tuple[float, float, float, float] | None:
        xs = [x for item in self.series for x, _y in item["points"]]
        ys = [y for item in self.series for _x, y in item["points"]]
        if not xs or not ys:
            return None
        x_min, x_max = min(xs), max(xs)
        if x_max <= x_min:
            x_max = x_min + 1.0
        y_low = min(ys) if self.y_min is None else self.y_min
        y_high = max(ys) if self.y_max is None else self.y_max
        if y_high <= y_low:
            y_high = y_low + 1.0
        return x_min, x_max, y_low, y_high

    def paintEvent(self, event) -> None:  # noqa: item2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.CANVAS_BG))
        bounds = self._bounds()
        if bounds is None:
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            painter.drawText(self.rect(), Qt.AlignCenter, "还没有数据")
            return

        x_min, x_max, y_low, y_high = bounds
        # 有纵轴标题时左边要留出旋转文字的位置，否则刻度数字会被压在标题上
        left = 70.0 if self.y_label else 54.0
        top, right, bottom = 16.0, 12.0, 34.0
        plot = QRectF(
            left, top, max(10.0, self.width() - left - right),
            max(10.0, self.height() - top - bottom),
        )

        def to_x(value: float) -> float:
            if self.log_x:
                low = math.log10(max(1e-6, x_min))
                high = math.log10(max(1e-5, x_max))
                span = high - low if high > low else 1.0
                return plot.left() + (math.log10(max(1e-6, value)) - low) / span * plot.width()
            return plot.left() + (value - x_min) / (x_max - x_min) * plot.width()

        def to_y(value: float) -> float:
            return plot.bottom() - (value - y_low) / (y_high - y_low) * plot.height()

        painter.setPen(QPen(QColor(theme.GRID_MINOR), 1.0))
        for step in range(5):
            y = plot.top() + plot.height() * step / 4
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            painter.drawText(
                QRectF(20, y - 8, left - 24, 16),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{y_high - (y_high - y_low) * step / 4:.4g}",
            )
            painter.setPen(QPen(QColor(theme.GRID_MINOR), 1.0))
        for step in range(5):
            x = plot.left() + plot.width() * step / 4
            painter.setPen(QPen(QColor(theme.GRID_MINOR), 1.0))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            value = (
                x_min * (x_max / x_min) ** (step / 4) if self.log_x
                else x_min + (x_max - x_min) * step / 4
            )
            # 柱状图的横轴是离散的谐波次数，半次、5.75 次这种刻度没有意义
            text = f"{value:.0f}" if self.bars else f"{value:.4g}"
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            painter.drawText(QRectF(x - 38, plot.bottom() + 4, 76, 16), Qt.AlignCenter, text)

        if self.zero_line and y_low < 0.0 < y_high:
            painter.setPen(QPen(QColor(theme.GRID_MAJOR), 1.0, Qt.DashLine))
            y = to_y(0.0)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        for item in self.series:
            points = item["points"]
            if not points:
                continue
            painter.setPen(QPen(QColor(item["color"]), item["width"]))
            if self.bars:
                width = max(2.0, plot.width() / max(1, len(points)) * 0.55)
                painter.setPen(QPen(QColor(item["color"]), 1.0))
                painter.setBrush(QColor(item["color"]))
                baseline = to_y(max(y_low, 0.0) if y_low <= 0.0 else y_low)
                # 贴边的柱子会向外溢出、盖住纵轴刻度数字，所以限制在绘图区内
                painter.save()
                painter.setClipRect(plot)
                for x, y in points:
                    centre = to_x(x)
                    top = min(to_y(y), baseline)
                    painter.drawRect(QRectF(centre - width / 2, top, width, abs(baseline - top)))
                painter.restore()
                painter.setBrush(Qt.NoBrush)
                continue
            if len(points) < 2:
                continue
            polygon = QPolygonF(
                [QPointF(to_x(x), to_y(y)) for x, y in points]
            )
            painter.drawPolyline(polygon)

        # 图例
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        x = plot.left() + 8
        for item in self.series:
            painter.setPen(QPen(QColor(item["color"]), 2.0))
            painter.drawLine(QPointF(x, plot.top() + 10), QPointF(x + 18, plot.top() + 10))
            painter.setPen(QPen(QColor(theme.TEXT)))
            text = item["name"]
            painter.drawText(
                QRectF(x + 22, plot.top() + 2, 190, 16), Qt.AlignLeft | Qt.AlignVCenter, text
            )
            x += 26 + painter.fontMetrics().horizontalAdvance(text) + 18

        if self.x_label:
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            painter.drawText(
                QRectF(plot.left(), self.height() - 18, plot.width(), 16),
                Qt.AlignCenter,
                self.x_label,
            )
        if self.y_label:
            painter.save()
            painter.translate(12, plot.center().y())
            painter.rotate(-90)
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            painter.drawText(QRectF(-70, -10, 140, 16), Qt.AlignCenter, self.y_label)
            painter.restore()


class PhasorPlot(QWidget):
    """三相向量图（极坐标）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.phasors: list[tuple[str, an.Phasor]] = []
        self.reference: an.Phasor | None = None
        self.setMinimumHeight(260)

    def set_data(self, phasors: list[tuple[str, an.Phasor]], reference: an.Phasor | None = None) -> None:
        self.phasors = phasors
        self.reference = reference
        self.update()

    def paintEvent(self, event) -> None:  # noqa: item2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.CANVAS_BG))

        area = QRectF(self.rect()).adjusted(24, 16, -24, -16)
        radius = min(area.width(), area.height()) / 2 - 10
        center = area.center()
        if radius < 20:
            return

        painter.setPen(QPen(QColor(theme.GRID_MINOR), 1.0))
        for ratio in (0.25, 0.5, 0.75, 1.0):
            painter.drawEllipse(center, radius * ratio, radius * ratio)
        painter.drawLine(
            QPointF(center.x() - radius, center.y()), QPointF(center.x() + radius, center.y())
        )
        painter.drawLine(
            QPointF(center.x(), center.y() - radius), QPointF(center.x(), center.y() + radius)
        )
        painter.setPen(QPen(QColor(theme.TEXT_DIM)))
        painter.drawText(
            QRectF(center.x() + 4, center.y() - 18, 60, 16), Qt.AlignLeft, "0°"
        )
        painter.drawText(
            QRectF(center.x() - radius - 4, center.y() - radius - 16, 60, 16),
            Qt.AlignLeft,
            "90°",
        )

        magnitudes = [phasor.magnitude for _name, phasor in self.phasors] + (
            [self.reference.magnitude] if self.reference else []
        )
        scale = max(magnitudes) if magnitudes else 1.0
        if scale <= 0:
            return
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)

        for index, (name, phasor) in enumerate(self.phasors):
            color = QColor(PHASE_COLORS[index % len(PHASE_COLORS)])
            painter.setPen(QPen(color, 2.2))
            angle = math.radians(phasor.angle_deg)
            length = radius * (phasor.magnitude / scale)
            # 数学角度逆时针为正，屏幕 y 向下所以取负
            end = QPointF(center.x() + length * math.cos(angle), center.y() - length * math.sin(angle))
            painter.drawLine(center, end)
            painter.setBrush(color)
            painter.drawEllipse(end, 3.0, 3.0)
            painter.setBrush(Qt.NoBrush)
            painter.drawText(
                QRectF(end.x() + 6, end.y() - 18, 150, 16),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{name} {phasor.text()}",
            )

        if self.reference is not None:
            painter.setPen(QPen(QColor(theme.TEXT_DIM), 1.4, Qt.DashLine))
            angle = math.radians(self.reference.angle_deg)
            length = radius * (self.reference.magnitude / scale)
            end = QPointF(
                center.x() + length * math.cos(angle), center.y() - length * math.sin(angle)
            )
            painter.drawLine(center, end)


class AnalysisWindow(QWidget):
    """4 个页签住在一起：伯德图 / 锁相 / 功率 / 三相向量。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context

        self.setWindowTitle(f"分析工具 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(1120, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        # 状态行先建好：各页签在构造时就可能写状态（例如谐波页没有波形数据），
        # 放到 tabs 之后建会让 _build_* 里拿到还没定义的属性
        self.status = QLabel("就绪")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.status.setWordWrap(True)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_bode(), "滤波器伯德图")
        self.tabs.addTab(self._build_pll(), "锁相")
        self.tabs.addTab(self._build_power(), "功率因数 / 功率角")
        self.tabs.addTab(self._build_phasor(), "三相向量")
        self.tabs.addTab(self._build_harmonics(), "谐波分析")
        self.tabs.currentChanged.connect(lambda _index: self.refresh())
        root.addWidget(self.tabs, 1)
        root.addWidget(self.status)

        # 实时值每秒刷一次就够了（这些量本身变得不快），别去挤采集线程
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        super().closeEvent(event)

    # -------------------------------------------------- 1 伯德图

    def _build_bode(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setSpacing(10)

        box = QGroupBox("滤波器参数")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.topology_combo = QComboBox()
        for key, name, _note in an.FILTER_TOPOLOGIES:
            self.topology_combo.addItem(name, key)
        # 默认给最常用的 LC（电力电子滤波器的基本形态），RC 是一阶的、没有谐振点
        lc_index = self.topology_combo.findData("lc")
        if lc_index >= 0:
            self.topology_combo.setCurrentIndex(lc_index)
        self.topology_combo.currentIndexChanged.connect(lambda _index: self._update_bode())
        form.addRow("结构", self.topology_combo)

        self.r_box = self._number(0.5, 0.0, 1e6, " Ω", "电阻（含电感等效串联电阻）")
        self.l_box = self._number(1.0, 0.0001, 1e6, " mH", "电感 L1")
        self.c_box = self._number(100.0, 0.0001, 1e6, " μF", "电容")
        self.l2_box = self._number(0.5, 0.0, 1e6, " mH", "LCL 的第二只电感（其它结构忽略）")
        for label, widget in (
            ("电阻 R", self.r_box), ("电感 L", self.l_box),
            ("电容 C", self.c_box), ("电感 L2", self.l2_box),
        ):
            form.addRow(label, widget)

        self.f_min_box = self._number(1.0, 0.001, 1e7, " Hz")
        self.f_max_box = self._number(100000.0, 0.01, 1e9, " Hz")
        form.addRow("起始频率", self.f_min_box)
        form.addRow("终止频率", self.f_max_box)
        for widget in (self.r_box, self.l_box, self.c_box, self.l2_box,
                       self.f_min_box, self.f_max_box):
            widget.valueChanged.connect(lambda _value: self._update_bode())
        layout.addWidget(box)

        right = QVBoxLayout()
        self.bode_plot = CurvePlot()
        self.bode_plot.set_axis(log_x=True, x_label="频率 (Hz)", y_label="幅值 (dB)")
        right.addWidget(self.bode_plot, 3)
        self.bode_phase_plot = CurvePlot()
        self.bode_phase_plot.set_axis(log_x=True, x_label="频率 (Hz)", y_label="相位 (°)", zero_line=True)
        right.addWidget(self.bode_phase_plot, 2)
        self.bode_text = QLabel("")
        self.bode_text.setWordWrap(True)
        self.bode_text.setStyleSheet(f"color: {theme.TEXT}; font-size: 12px;")
        right.addWidget(self.bode_text)
        layout.addLayout(right, 1)
        return page

    @staticmethod
    def _number(value: float, low: float, high: float, suffix: str = "",
                tip: str = "") -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(low, high)
        box.setDecimals(4)
        box.setValue(value)
        box.setSuffix(suffix)
        box.setToolTip(tip)
        no_arrows(box)
        return box

    def _update_bode(self) -> None:
        result = an.bode(
            topology=str(self.topology_combo.currentData() or "lc"),
            r=self.r_box.value(),
            l=self.l_box.value(),
            c=self.c_box.value(),
            l2=self.l2_box.value(),
            f_min=self.f_min_box.value(),
            f_max=self.f_max_box.value(),
        )
        self.bode_plot.clear()
        self.bode_plot.add("幅频", [(point.freq, point.gain_db) for point in result.points], theme.ACCENT)
        self.bode_phase_plot.clear()
        self.bode_phase_plot.add(
            "相频", [(point.freq, point.phase_deg) for point in result.points], theme.QUALITY_STALE
        )
        self.bode_text.setText(
            an.format_points(an.key_points(result)) + f"　｜　{result.note}"
        )
        self.status.setText(
            f"伯德图：{len(result.points)} 个频点，谐振 "
            + (f"{result.resonance_hz:,.1f} Hz" if result.resonance_hz else "无")
        )

    # -------------------------------------------------- 2 锁相

    def _build_pll(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setSpacing(10)

        box = QGroupBox("锁相")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.pll_combo = QComboBox()
        self._fill_points(self.pll_combo, wave_only=True, prefer=("电压", "线电压", "波形"))
        self.pll_combo.currentIndexChanged.connect(lambda _index: self._update_pll())
        form.addRow("电压波形点位", self.pll_combo)

        self.pll_ref_combo = QComboBox()
        self._fill_points(
            self.pll_ref_combo, wave_only=True, allow_empty="（不比较相位差）",
            prefer=("A相", "电流"),
        )
        form.addRow("参考波形点位", self.pll_ref_combo)

        self.pll_nominal = self._number(50.0, 1.0, 1000.0, " Hz", "额定频率（用来判断周波对不对）")
        form.addRow("额定频率", self.pll_nominal)

        readonly = QLabel("取的是当前采集到的那一段波形（数组点位）。\n"
                          "采样间隔取参数表里的「采样间隔us」。")
        readonly.setWordWrap(True)
        readonly.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        form.addRow("", readonly)
        layout.addWidget(box)

        right = QVBoxLayout()
        self.pll_plot = CurvePlot()
        self.pll_plot.set_axis(x_label="时间 (ms)", y_label="角度 (°)", y_min=0.0, y_max=360.0)
        right.addWidget(self.pll_plot, 1)
        self.pll_text = QLabel("")
        self.pll_text.setWordWrap(True)
        self.pll_text.setStyleSheet(f"color: {theme.TEXT}; font-size: 12px;")
        right.addWidget(self.pll_text)
        layout.addLayout(right, 1)
        return page

    def _update_pll(self) -> None:
        self.pll_plot.clear()
        pid = self.pll_combo.currentData()
        if not pid:
            self.pll_text.setText("工程里没有数组/波形点位：锁相需要有瞬时波形缓冲区。")
            return
        tag = self.context.tag_of(str(pid))
        values = tag.value if isinstance(tag.value, (list, tuple)) else None
        if not values:
            self.pll_text.setText(f"{self._title(str(pid))} 还没有波形数据（先启动采集）。")
            return
        interval = 0
        found = self.context.project.find_point(str(pid))
        if found is not None:
            interval = int(found[1].sample_interval_us or 0)
        result = an.phase_from_waveform([float(v) for v in values], interval, self.pll_nominal.value())
        if not result.ok:
            self.pll_text.setText(f"锁相失败：{result.note}")
            self.status.setText("锁相失败")
            return
        self.pll_plot.add(
            "相位角",
            [(sample.time * 1000.0, sample.angle_deg) for sample in result.samples],
            theme.ACCENT,
        )
        text = (
            f"频率 <b>{result.freq_hz:.2f} Hz</b>　"
            f"（额定 {self.pll_nominal.value():g} Hz，偏差 "
            f"{(result.freq_hz - self.pll_nominal.value()) / max(1e-9, self.pll_nominal.value()) * 100:+.2f}%）　"
            f"周波数 {result.cycles}　采样点 {len(values)}"
        )
        ref_pid = self.pll_ref_combo.currentData()
        if ref_pid:
            ref_tag = self.context.tag_of(str(ref_pid))
            ref_values = ref_tag.value if isinstance(ref_tag.value, (list, tuple)) else None
            if ref_values:
                dt = (interval or int(1e6 / (self.pll_nominal.value() * 100))) / 1e6
                delay = an.phase_differences([list(values), list(ref_values)], dt)
                if len(delay) >= 2:
                    text += (
                        f"　｜　相对「{self._title(str(ref_pid))}」相位差 "
                        f"<b>{delay[1]:.1f}°</b>"
                    )
        text += f"<br>{result.note}"
        self.pll_text.setText(text)
        self.status.setText(f"锁相：{result.freq_hz:.2f} Hz / {result.cycles} 个周波")

    # -------------------------------------------------- 3 功率

    def _build_power(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setSpacing(10)

        box = QGroupBox("取哪些点")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.volt_combo = QComboBox()
        self._fill_points(self.volt_combo, prefer=("线电压", "母线电压", "电压"))
        self.current_combo = QComboBox()
        self._fill_points(self.current_combo, prefer=("A相电流", "电流"))
        self.active_combo = QComboBox()
        self._fill_points(self.active_combo, prefer=("有功", "功率"))
        self.pf_combo = QComboBox()
        self._fill_points(self.pf_combo, allow_empty="（不用设备报的 PF，按电压电流算）",
                          prefer=("功率因数",))
        for label, widget in (
            ("电压（线电压）", self.volt_combo),
            ("电流", self.current_combo),
            ("有功功率", self.active_combo),
            ("设备报的 PF", self.pf_combo),
        ):
            form.addRow(label, widget)
            widget.currentIndexChanged.connect(lambda _index: self._update_power())
        self.power_auto = QCheckBox("每次刷新都记一点趋势（最多 600 点）")
        self.power_auto.setChecked(True)
        form.addRow("", self.power_auto)
        self.power_clear = QPushButton("清空趋势")
        self.power_clear.clicked.connect(lambda _checked=False: self._clear_power_history())
        form.addRow("", self.power_clear)
        layout.addWidget(box)

        right = QVBoxLayout()
        self.power_plot = CurvePlot()
        self.power_plot.set_axis(x_label="采样点", y_label="功率因数", y_min=0.0, y_max=1.05)
        right.addWidget(self.power_plot, 1)
        self.power_plot2 = CurvePlot()
        self.power_plot2.set_axis(x_label="采样点", y_label="功率角 (°)", y_min=0.0, y_max=90.0)
        right.addWidget(self.power_plot2, 1)
        self.power_text = QLabel("")
        self.power_text.setWordWrap(True)
        self.power_text.setStyleSheet(f"color: {theme.TEXT}; font-size: 12px;")
        right.addWidget(self.power_text)
        layout.addLayout(right, 1)

        self._power_history: list[tuple[float, float]] = []
        return page

    def _clear_power_history(self) -> None:
        self._power_history.clear()
        self.power_plot.clear()
        self.power_plot2.clear()
        self.status.setText("功率趋势已清空")

    def _update_power(self) -> None:
        voltage = self._value_of(self.volt_combo)
        current = self._value_of(self.current_combo)
        active = self._value_of(self.active_combo)
        pf_direct = self._value_of(self.pf_combo)
        if voltage is None or current is None or (active is None and pf_direct is None):
            self.power_text.setText("选好电压、电流与有功（或设备报的 PF）后再看结果。")
            return
        result = an.power_factor(voltage, current, active or 0.0, pf_direct)
        self.power_text.setText(
            f"功率因数 <b>{result.power_factor:.4f}</b>　"
            f"功率角 <b>{result.angle_deg:.2f}°</b>　{result.kind}　"
            f"视在 <b>{result.apparent / 1000:.2f} kVA</b>　无功 <b>{result.reactive / 1000:.2f} kvar</b>"
            + (f"<br>{result.note}" if result.note else "")
        )
        if self.power_auto.isChecked() and result.power_factor:
            self._power_history.append((result.power_factor, result.angle_deg))
            del self._power_history[:-600]
            self.power_plot.clear()
            self.power_plot.add(
                "功率因数",
                list(enumerate(value for value, _angle in self._power_history)),
                theme.ACCENT,
            )
            self.power_plot2.clear()
            self.power_plot2.add(
                "功率角",
                list(enumerate(angle for _value, angle in self._power_history)),
                theme.QUALITY_STALE,
            )
        self.status.setText(f"功率因数 {result.power_factor:.4f}（φ = {result.angle_deg:.2f}°）")

    # -------------------------------------------------- 4 三相向量

    def _build_phasor(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setSpacing(10)

        box = QGroupBox("三个相量")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.amp_combos: list[QComboBox] = []
        self.angle_combos: list[QComboBox] = []
        self.angle_boxes: list[QDoubleSpinBox] = []
        taken: list[str] = []
        for phase in ("A", "B", "C"):
            amp = QComboBox()
            # 三相必须是三个不同点位，否则"三相平衡"这个结论是假的
            self._fill_points(
                amp,
                prefer=(f"{phase}相电压", f"{phase}相电流", f"{phase}相", "电压"),
                avoid=tuple(taken),
            )
            amp.currentIndexChanged.connect(lambda _index: self._update_phasor())
            self.amp_combos.append(amp)
            taken.append(str(amp.currentData() or ""))
            form.addRow(f"{phase} 相幅值", amp)

            row = QHBoxLayout()
            row.setSpacing(4)
            source = QComboBox()
            self._fill_points(source, allow_empty="手动输入角度")
            source.currentIndexChanged.connect(
                lambda _index, index=len(self.angle_combos): self._sync_angle_row(index)
            )
            self.angle_combos.append(source)
            box_angle = self._number(0.0, -360.0, 360.0, " °", "手输相角")
            box_angle.setValue({"A": 0.0, "B": -120.0, "C": 120.0}[phase])
            self.angle_boxes.append(box_angle)
            row.addWidget(source, 1)
            row.addWidget(box_angle)
            form.addRow(f"{phase} 相相角", row)

        self.phasor_note = QLabel("")
        self.phasor_note.setWordWrap(True)
        self.phasor_note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        form.addRow("", self.phasor_note)
        layout.addWidget(box)

        right = QVBoxLayout()
        self.phasor_plot = PhasorPlot()
        right.addWidget(self.phasor_plot, 1)
        self.phasor_text = QLabel("")
        self.phasor_text.setWordWrap(True)
        self.phasor_text.setStyleSheet(f"color: {theme.TEXT}; font-size: 12px;")
        right.addWidget(self.phasor_text)
        layout.addLayout(right, 1)
        return page

    # -------------------------------------------------- 5 谐波

    def _build_harmonics(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setSpacing(10)

        controls = QGroupBox("波形与分析参数")
        form = QFormLayout(controls)
        self.harmonic_source_combo = QComboBox()
        self._fill_points(self.harmonic_source_combo, wave_only=True, prefer=("电压", "A相"))
        self.harmonic_source_combo.currentIndexChanged.connect(
            lambda _index: self._harmonic_source_changed()
        )
        form.addRow("实时波形点位", self.harmonic_source_combo)

        self.harmonic_interval = self._number(
            100.0, 0.001, 1e9, " μs", "CSV 需要填写采样间隔；实时波形会自动读参数表"
        )
        self.harmonic_interval.setDecimals(2)
        form.addRow("采样间隔", self.harmonic_interval)
        self.harmonic_frequency = self._number(50.0, 0.01, 10000.0, " Hz", "电网或变流器基波频率")
        form.addRow("基波频率", self.harmonic_frequency)
        self.harmonic_max_order = self._number(
            20, 1, 100, " 次", "最高计算到第几次谐波（不得超过奈奎斯特频率）"
        )
        self.harmonic_max_order.setDecimals(0)
        form.addRow("最高次数", self.harmonic_max_order)

        self.harmonic_import = QPushButton("导入 CSV 波形…")
        self.harmonic_import.clicked.connect(self._load_harmonic_csv)
        form.addRow("", self.harmonic_import)
        self.harmonic_live = QPushButton("改用实时波形")
        self.harmonic_live.clicked.connect(self._use_live_harmonic_source)
        form.addRow("", self.harmonic_live)
        note = QLabel(
            "CSV 读取第一列数值（可有表头）；采样间隔请按采集设备设置。"
            "分析截取整周期、去除直流分量，幅值按 RMS 展示。"
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        form.addRow("", note)
        layout.addWidget(controls)

        right = QVBoxLayout()
        self.harmonic_plot = CurvePlot()
        self.harmonic_plot.bars = True
        self.harmonic_plot.set_axis(
            x_label="谐波次数", y_label="RMS（基波 = 100%）", y_min=0.0, y_max=110.0
        )
        right.addWidget(self.harmonic_plot, 2)
        self.harmonic_table = QTableWidget(0, 3)
        self.harmonic_table.setHorizontalHeaderLabels(["次数", "RMS", "基波占比"])
        self.harmonic_table.horizontalHeader().setStretchLastSection(True)
        right.addWidget(self.harmonic_table, 1)
        self.harmonic_text = QLabel("选择波形点位，或导入 CSV 波形。")
        self.harmonic_text.setWordWrap(True)
        self.harmonic_text.setStyleSheet(f"color: {theme.TEXT}; font-size: 12px;")
        right.addWidget(self.harmonic_text)
        layout.addLayout(right, 1)

        self._harmonic_csv_values: list[float] | None = None
        self._harmonic_csv_name = ""
        self.harmonic_interval.valueChanged.connect(lambda _value: self._update_harmonics())
        self.harmonic_frequency.valueChanged.connect(lambda _value: self._update_harmonics())
        self.harmonic_max_order.valueChanged.connect(lambda _value: self._update_harmonics())
        self._harmonic_source_changed()
        return page

    def _harmonic_source_changed(self) -> None:
        pid = self.harmonic_source_combo.currentData()
        found = self.context.project.find_point(str(pid)) if pid else None
        if found is not None and found[1].sample_interval_us:
            self.harmonic_interval.setValue(float(found[1].sample_interval_us))
        self._update_harmonics()

    def _load_harmonic_csv(self) -> None:
        start = self.context.report_subdir("waveforms")
        path, _selected = QFileDialog.getOpenFileName(
            self, "导入波形表", start, sheetio.read_filter()
        )
        if not path:
            return
        try:
            values = an.read_waveform_csv(path)
        except (OSError, ValueError) as exc:
            self.harmonic_text.setText(f"CSV 读取失败：{exc}")
            return
        self._harmonic_csv_values = values
        self._harmonic_csv_name = os.path.basename(path)
        self._update_harmonics()

    def _use_live_harmonic_source(self) -> None:
        self._harmonic_csv_values = None
        self._harmonic_csv_name = ""
        self._harmonic_source_changed()

    def _update_harmonics(self) -> None:
        values = self._harmonic_csv_values
        label = self._harmonic_csv_name
        if values is None:
            pid = self.harmonic_source_combo.currentData()
            if not pid:
                self.harmonic_text.setText("工程里没有数组/波形点位；请导入 CSV 波形。")
                self.harmonic_plot.clear()
                self.harmonic_table.setRowCount(0)
                self.status.setText("谐波分析：没有可用的波形点位")
                return
            tag = self.context.tag_of(str(pid))
            values = tag.value if isinstance(tag.value, (list, tuple)) else None
            label = self._title(str(pid))
            if not values:
                self.harmonic_text.setText(f"{label} 暂无波形数据（先启动采集或导入 CSV）。")
                self.harmonic_plot.clear()
                self.harmonic_table.setRowCount(0)
                self.status.setText(f"谐波分析：{label} 暂无数据")
                return
            found = self.context.project.find_point(str(pid))
            interval = float(found[1].sample_interval_us or 0) if found else 0.0
            if interval <= 0:
                # 参数表没写采样间隔时，用界面上填的那个（否则只能报错）
                interval = float(self.harmonic_interval.value())
        else:
            interval = self.harmonic_interval.value()

        try:
            result = an.harmonic_analysis(
                [float(value) for value in values],
                interval,
                self.harmonic_frequency.value(),
                int(self.harmonic_max_order.value()),
            )
        except (TypeError, ValueError) as exc:
            self.harmonic_text.setText(f"无法分析：{exc}")
            self.harmonic_plot.clear()
            self.harmonic_table.setRowCount(0)
            self.status.setText(f"谐波分析失败：{exc}")
            return

        points = [
            (float(item.order), item.percent_of_fundamental)
            for item in result.components
        ]
        self.harmonic_plot.clear()
        self.harmonic_plot.add("相对基波", points, theme.ACCENT)
        self.harmonic_table.setRowCount(len(result.components))
        for row, item in enumerate(result.components):
            self.harmonic_table.setItem(row, 0, QTableWidgetItem(f"{item.order}"))
            self.harmonic_table.setItem(row, 1, QTableWidgetItem(f"{item.rms:.6g}"))
            self.harmonic_table.setItem(
                row, 2, QTableWidgetItem(f"{item.percent_of_fundamental:.3f}%")
            )
        self.harmonic_text.setText(
            f"数据：{label}　采样率 {result.sample_rate_hz:,.1f} Hz　"
            f"有效周波 {result.cycles}　基波 {result.fundamental_hz:g} Hz　"
            f"<b>THD = {result.thd_percent:.3f}%</b>"
        )
        self.status.setText(f"谐波分析：THD {result.thd_percent:.3f}%")

    def _sync_angle_row(self, index: int) -> None:
        pid = self.angle_combos[index].currentData()
        self.angle_boxes[index].setEnabled(not pid)
        self._update_phasor()

    def _update_phasor(self) -> None:
        phasors: list[an.Phasor] = []
        for index, name in enumerate(("A", "B", "C")):
            magnitude = self._value_of(self.amp_combos[index]) or 0.0
            pid = self.angle_combos[index].currentData()
            if pid:
                angle = self._value_of(self.angle_combos[index])
                angle = 0.0 if angle is None else angle
            else:
                angle = self.angle_boxes[index].value()
            phasors.append(an.Phasor(magnitude=float(magnitude), angle_deg=float(angle)))

        if all(phasor.magnitude <= 0 for phasor in phasors):
            self.phasor_text.setText("选好三相幅值（点位上要有实时值）再看结果。")
            self.phasor_plot.set_data([])
            return

        self.phasor_plot.set_data(list(zip(("A 相", "B 相", "C 相"), phasors)))
        result = an.symmetrical_components(*phasors)
        duplicates = [
            pid for pid in {str(c.currentData()) for c in self.amp_combos}
            if pid and sum(1 for c in self.amp_combos if str(c.currentData()) == pid) > 1
        ]
        warn = (
            "<br><b>注意：三相选到了同一个点位（"
            + "、".join(self._title(pid) for pid in duplicates)
            + "），这样算出来的「平衡」没有意义。</b>"
            if duplicates
            else ""
        )
        self.phasor_text.setText(
            f"正序 <b>{result.positive.text()}</b>　负序 <b>{result.negative.text()}</b>　"
            f"零序 <b>{result.zero.text()}</b><br>不平衡度 <b>{result.unbalance_percent():.2f}%</b>"
            f"　｜　{result.note}{warn}"
        )
        self.status.setText(f"三相向量：不平衡度 {result.unbalance_percent():.2f}%")

    # -------------------------------------------------- 通用

    def _title(self, pid: str) -> str:
        """点位显示名（宿主没给 ``title_of`` 时退回点号本身）。"""
        if self.context.title_of is None:
            return pid
        return self.context.title_of(pid)

    def _fill_points(self, combo: QComboBox, *, wave_only: bool = False,
                     allow_empty: str = "", prefer: tuple[str, ...] = (),
                     avoid: tuple[str, ...] = ()) -> None:
        """填点位下拉框。

        ``prefer`` 里的关键词命中就先选上（现场点表大体这么起名）；
        ``avoid`` 里的点号不参与预选 —— 三相幅值那种"必须选三个不同点"的地方靠它，
        否则三个框会一起落到同一个点位上，算出来永远是"三相平衡"，很误导人。
        """
        if allow_empty:
            combo.addItem(allow_empty, "")
        best = -1
        for pid, title in self.context.all_points():
            if wave_only:
                found = self.context.project.find_point(pid)
                if found is None or not found[1].is_array:
                    continue
            combo.addItem(title, pid)
            if best < 0 and pid not in avoid and prefer and any(word in title for word in prefer):
                best = combo.count() - 1
        if best < 0 and avoid:
            # 一个都没匹配上：至少别和已经选过的点位重复
            for index in range(combo.count()):
                if combo.itemData(index) not in avoid:
                    best = index
                    break
        if best >= 0:
            combo.setCurrentIndex(best)

    def _value_of(self, combo: QComboBox) -> float | None:
        pid = combo.currentData()
        if not pid:
            return None
        tag = self.context.tag_of(str(pid))
        if tag.value is None or tag.quality not in ("good", "stale"):
            return None
        if isinstance(tag.value, (list, tuple)):
            return float(tag.value[0]) if tag.value else None
        try:
            return float(tag.value)
        except (TypeError, ValueError):
            return None

    def refresh(self) -> None:
        """按当前页签重算（界面每秒调一次，实时性够用）。"""
        index = self.tabs.currentIndex()
        if index == 0:
            self._update_bode()
        elif index == 1:
            self._update_pll()
        elif index == 2:
            self._update_power()
        elif index == 3:
            self._update_phasor()
        else:
            self._update_harmonics()

    def select_tab(self, name: str) -> bool:
        """按名字切页签（探针/测试用）。"""
        for index in range(self.tabs.count()):
            if name in self.tabs.tabText(index):
                self.tabs.setCurrentIndex(index)
                return True
        return False


@register_tool
class AnalysisTool(WindowTool):
    name = "分析工具"
    group = "计算"
    order = 20
    description = "滤波器伯德图、锁相、功率因数/功率角、三相向量（都是现场拧螺丝时真要算的）"

    def build(self, context: ToolContext) -> QWidget:
        return AnalysisWindow(context)
