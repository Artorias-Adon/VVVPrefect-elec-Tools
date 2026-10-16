"""自定义控件：数值卡片、仪表、指示灯、趋势图、状态徽标。

全部用 QPainter 自绘，不引入 QtCharts（其许可为 GPL/商业，自绘无此顾虑）。
"""

from __future__ import annotations

import math
import time
from collections import deque

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.codec import format_value
from . import theme


def _quality_color(quality: str) -> str:
    return {
        "good": theme.QUALITY_GOOD,
        "bad": theme.QUALITY_BAD,
        "stale": theme.QUALITY_STALE,
        "uncertain": theme.QUALITY_UNCERTAIN,
    }.get(quality, theme.QUALITY_BAD)


class ElidedLabel(QLabel):
    """说明文字用：窄了就把尾巴省略成「…」（或换行），**绝不因为文字长而把窗口撑宽**。

    为什么需要它：普通 QLabel 的 minimumSizeHint 就是整段文字的长度，
    一句话（状态栏里的完整工程路径、示波器页那行"没有波形点位…"提示）
    就能把窗口的最小宽度顶到一千多像素 —— 小屏上窗口直接超出屏幕。

    * ``wrap=False``（默认）：单行 + 超长省略号，全文在 tooltip 里；
    * ``wrap=True``：允许折行显示（重要提示用这个，窄窗口下也能读全）。

    ``text()`` 仍返回**完整**文字（显示用的那截另存），这样断言、日志拿到的都不失真。
    """

    #: 最小宽度（像素）：够显示省略号即可，不参与"谁把窗口撑宽"的竞争
    MIN_WIDTH = 48

    def __init__(
        self, text: str = "", parent: QWidget | None = None, wrap: bool = False
    ) -> None:
        super().__init__(parent)
        self._full_text = ""
        self._wrap = wrap
        self.setWordWrap(wrap)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(self.MIN_WIDTH)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: item2 - Qt 命名
        self._full_text = text or ""
        self.setToolTip(self._full_text)
        self._refresh()
        self.update()

    def text(self) -> str:  # noqa: A003 - 想和 QLabel 同名，调用方不用改
        """返回完整文字（显示上可能被省略，但逻辑上的文字是完整的）。"""
        return self._full_text

    def fullText(self) -> str:  # noqa: item2
        return self._full_text

    def resizeEvent(self, event) -> None:  # noqa: item2
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        if self._wrap or not self._full_text:
            super().setText(self._full_text)
            return
        super().setText(
            self.fontMetrics().elidedText(
                self._full_text, Qt.TextElideMode.ElideRight, max(self.width(), self.MIN_WIDTH)
            )
        )

    def minimumSizeHint(self):  # noqa: item2
        hint = super().minimumSizeHint()
        height = hint.height()
        if self._wrap and self.width() > 0:
            # 折行模式下按当前宽度算需要几行，免得高度按单行给导致文字被裁
            height = max(
                height, self.fontMetrics().boundingRect(0, 0, self.width(), 0, 0, self._full_text).height()
            )
        return hint.__class__(self.MIN_WIDTH, max(height, self.fontMetrics().height()))

    def sizeHint(self):  # noqa: item2
        hint = super().sizeHint()
        return hint.__class__(min(hint.width(), 320), hint.height())


class PointWidget(QFrame):
    """所有点位控件的基类：统一的鼠标交互。

    * **双击** -> 打开点位参数对话框（改值、单发、持续发送）
    * **右键** -> 同一套操作的快捷菜单（单发 / 持续发送 / 停止发送 / 添加到示波器 …）

    交互本身不在这里实现，而是抛信号给页面、由主窗口统一处理：
    菜单项需要用到采集引擎、数据库、示波器页，只有主窗口都拿得到。
    这样"全部点位都能添加到示波器"只需要在一处实现。
    """

    #: 双击，请求打开点位参数对话框
    editRequested = Signal(str)
    #: 右键，``(点号, 全局坐标)``；菜单由主窗口弹出
    contextRequested = Signal(str, object)

    def __init__(self, pid: str, point, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pid = pid
        self.point = point
        self.setFrameShape(QFrame.NoFrame)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.contextRequested.emit(self.pid, self.mapToGlobal(pos))
        )

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: item2
        self.editRequested.emit(self.pid)
        event.accept()


class ValueCard(PointWidget):
    """数值卡片：标题 + 大号数值 + 单位 + 质量色条；可写点位带写入控件。"""

    writeRequested = Signal(str, object)

    def __init__(self, pid: str, point, parent: QWidget | None = None) -> None:
        super().__init__(pid, point, parent)
        self._quality = "bad"
        self.setObjectName("valueCard")
        self.setMinimumWidth(190)
        self.setStyleSheet(
            f"QFrame#valueCard {{ background: {theme.BG_CARD}; border: 1px solid {theme.BORDER};"
            f" border-radius: 6px; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(4)

        self.title_label = QLabel(point.title)
        self.title_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        root.addWidget(self.title_label)

        value_row = QHBoxLayout()
        value_row.setSpacing(4)
        self.value_label = QLabel("--")
        self._value_font_size = 20
        font = QFont()
        font.setPointSize(self._value_font_size)
        font.setBold(True)
        self.value_label.setFont(font)
        value_row.addWidget(self.value_label)
        self.unit_label = QLabel(point.unit or "")
        self.unit_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.unit_label.setAlignment(Qt.AlignBottom | Qt.AlignLeft)
        value_row.addWidget(self.unit_label)
        value_row.addStretch(1)
        root.addLayout(value_row)

        self.sub_label = QLabel(point.description or f"{point.register} #{point.address}")
        self.sub_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.sub_label)

        self.write_box: QDoubleSpinBox | None = None
        self.command_buttons: list[QPushButton] = []
        self._bit_labels = point.bit_labels()
        self._bits_mode = bool(self._bit_labels)
        self._bit_active: list[str] | None = None

        commands = point.command_items()
        if commands:
            self._build_command_buttons(root, commands)
        elif point.writable:
            row = QHBoxLayout()
            row.setSpacing(6)
            self.write_box = QDoubleSpinBox()
            self.write_box.setDecimals(max(0, point.decimals))
            self.write_box.setRange(-1e9, 1e9)
            self.write_box.setSingleStep(1.0)
            self.write_box.setKeyboardTracking(False)
            row.addWidget(self.write_box, 1)
            btn = QPushButton("写入")
            btn.setFixedWidth(56)
            btn.clicked.connect(self._emit_write)
            row.addWidget(btn)
            root.addLayout(row)

    def _build_command_buttons(self, root: QVBoxLayout, commands: list[tuple[int, str]]) -> None:
        """命令点位：给一排按钮，点一下就把对应命令码写下去。

        控制字这类"写一次即命令"的点位，用输入框写数值既别扭又容易误操作。
        """
        grid = QGridLayout()
        grid.setContentsMargins(0, 2, 0, 0)
        grid.setSpacing(4)
        for index, (value, label) in enumerate(commands):
            button = QPushButton(label)
            button.setMinimumHeight(22)
            button.setToolTip(f"写入 {value}（0x{value:X}）")
            button.clicked.connect(
                lambda _checked=False, v=value: self.writeRequested.emit(self.pid, v)
            )
            grid.addWidget(button, index // 3, index % 3)
            self.command_buttons.append(button)
        root.addLayout(grid)

    def _set_bit_headline(self, active: list[str]) -> None:
        """状态字的标题文字。

        多个状态可以同时置位（如「就绪」+「运行」），能并排显示就并排；
        名字太长（连最小字号都排不下）或置位太多时，退化为「N / M 置位」计数，
        宁可显示计数也不显示被裁掉一半的名字。
        """
        self._bit_active = list(active)
        if active and len(active) <= 3:
            if self._fit_bit_headline(" / ".join(active)):
                return
        if active:
            self._fit_bit_headline(f"{len(active)} / {len(self._bit_labels)} 置位")
        else:
            self._fit_bit_headline("无")

    def _fit_bit_headline(self, text: str) -> bool:
        """按卡片可用宽度挑最大字号。返回文字是否真的放得下。"""
        available = max(60, self.width() - 26)
        font = QFont(self.value_label.font())
        font.setBold(True)
        size = 11
        for size in (20, 17, 15, 13, 12, 11):
            font.setPointSize(size)
            if QFontMetrics(font).horizontalAdvance(text) <= available:
                break
        self.value_label.setText(text)
        self.value_label.setFont(font)
        self._value_font_size = size
        self.value_label.setStyleSheet(
            f"color: {theme.TEXT if self._bit_active else theme.TEXT_DIM};"
        )
        return QFontMetrics(font).horizontalAdvance(text) <= available

    def _bit_row_html(self) -> str:
        """状态字下方位名行。

        全部位名放得下时显示完整「状态图」（置位的高亮），否则只列置位的位名，
        放不下的用 ``+N`` 收口；完整明细始终在卡片的悬停提示里。
        """
        metrics = QFontMetrics(self.sub_label.font())
        available = max(80, self.width() - 26)
        active = set(self._bit_active)

        full: list[tuple[str, bool]] = [
            (label, label in active) for _index, label in self._bit_labels
        ]
        total = sum(metrics.horizontalAdvance(f"{label} ") for label, _ in full)
        entries = full if total <= available else [(label, True) for label in self._bit_active]

        parts: list[str] = []
        used = 0.0
        hidden = 0
        for position, (label, is_active) in enumerate(entries):
            piece = f"{label} " if parts else label
            width = metrics.horizontalAdvance(piece)
            if parts and used + width > available:
                hidden = len(entries) - position
                break
            colour = theme.ACCENT if is_active else theme.BORDER
            parts.append(f"<span style='color:{colour}'>{label}</span>")
            used += width
        if hidden:
            parts.append(f"<span style='color:{theme.TEXT_DIM}'>+{hidden}</span>")
        return " ".join(parts)

    def _refresh_bit_tooltip(self) -> None:
        active = set(self._bit_active)
        detail = "　".join(
            f"{'●' if label in active else '○'} {label}"
            for _index, label in self._bit_labels
        )
        hint = "　".join(f"{value} = {label}" for value, label in self.point.command_items())
        text = f"{self.point.title}：{detail}"
        if hint:
            text += f"\n可写命令：{hint}"
        self.setToolTip(text)

    def resizeEvent(self, event) -> None:  # noqa: item2
        super().resizeEvent(event)
        if self._bits_mode and self._bit_active is not None:
            self._set_bit_headline(self._bit_active)
            self.sub_label.setTextFormat(Qt.RichText)
            self.sub_label.setText(self._bit_row_html())

    def _emit_write(self) -> None:
        if self.write_box is not None:
            self.writeRequested.emit(self.pid, self.write_box.value())

    def set_value(self, value, quality: str = "good", error: str = "") -> None:
        self._quality = quality
        if quality == "good" and self._bits_mode:
            active = self.point.decode_bits(value, self._bit_labels)
            self._set_bit_headline(active)
            self._refresh_bit_tooltip()
            self.sub_label.setTextFormat(Qt.RichText)
            self.sub_label.setText(self._bit_row_html())
            self.update()
            return

        if quality == "good":
            self.value_label.setText(format_value(value, self.point.decimals))
            self.value_label.setStyleSheet(f"color: {theme.TEXT};")
            self.sub_label.setTextFormat(Qt.PlainText)
            self.sub_label.setText(
                self.point.description or f"{self.point.register} #{self.point.address}"
            )
        else:
            self.value_label.setText("--")
            self.value_label.setStyleSheet(f"color: {theme.QUALITY_BAD};")
            self.sub_label.setTextFormat(Qt.PlainText)
            self.sub_label.setText(error or "通信异常")
        self.update()

    def paintEvent(self, event) -> None:  # noqa: item2
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = _quality_color(self._quality)
        if self._quality == "good":
            pen = QPen(QColor(theme.BORDER))
        else:
            pen = QPen(QColor(color), 2)
        painter.setPen(pen)
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)
        # 左上角质量色块
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(color)))
        painter.drawRect(QRectF(1.5, 1.5, 3, 10))


class WaveCard(PointWidget):
    """波形缩略卡片：数组/块点位在运行监视页上的呈现。

    双击进示波器看这段波形（波形卡的重点是波形，不是改值），
    右键菜单里另有「打开参数对话框」。
    """

    doubleClicked = Signal(str)

    def __init__(self, pid: str, point, parent: QWidget | None = None) -> None:
        super().__init__(pid, point, parent)
        self._values: list[float] = []
        self._quality = "bad"
        self.setObjectName("waveCard")
        self.setMinimumSize(240, 132)
        self.setStyleSheet(
            f"QFrame#waveCard {{ background: {theme.BG_CARD}; border: 1px solid {theme.BORDER};"
            f" border-radius: 6px; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 9, 12, 9)
        root.setSpacing(2)

        self.title_label = QLabel(point.title)
        self.title_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        root.addWidget(self.title_label)

        self.info_label = QLabel("等待数据…")
        self.info_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.info_label)
        root.addStretch(1)

    def set_value(self, value, quality: str = "good", error: str = "") -> None:
        self._quality = quality
        if quality == "good" and isinstance(value, (list, tuple)):
            self._values = [float(v) for v in value]
        else:
            self._values = []
        if self._values:
            peak = max(self._values) - min(self._values)
            interval = self.point.sample_interval_us
            span = (
                f"，{len(self._values) * interval / 1000.0:.2f} ms"
                if interval
                else ""
            )
            self.info_label.setText(
                f"{len(self._values)} 点{span}　峰峰 {peak:.{max(0, self.point.decimals)}f} "
                f"{self.point.unit}".strip()
            )
        else:
            self.info_label.setText(error or "无数据")
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: item2
        self.doubleClicked.emit(self.pid)

    def paintEvent(self, event) -> None:  # noqa: item2
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        border = QColor(theme.BORDER if self._quality == "good" else theme.QUALITY_BAD)
        painter.setPen(QPen(border, 2 if self._quality != "good" else 1))
        painter.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 6, 6)

        area = QRectF(10, self.height() * 0.48, self.width() - 20, self.height() * 0.44)
        if not self._values:
            return
        low, high = min(self._values), max(self._values)
        span = (high - low) or 1.0
        count = max(1, len(self._values) - 1)
        points = [
            QPointF(
                area.left() + area.width() * index / count,
                area.bottom() - area.height() * (value - low) / span,
            )
            for index, value in enumerate(self._values)
        ]
        painter.setPen(QPen(QColor(self.point.scope_color or theme.ACCENT_ALT), 1.4))
        painter.drawPolyline(QPolygonF(points))


class GaugeWidget(PointWidget):
    """弧形仪表。"""

    def __init__(self, pid: str, point, parent: QWidget | None = None) -> None:
        super().__init__(pid, point, parent)
        self.minimum = float(point.gauge_min)
        self.maximum = float(point.gauge_max) if point.gauge_max != point.gauge_min else 1.0
        self._value = None
        self._quality = "bad"
        self.setMinimumSize(180, 172)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def set_value(self, value, quality: str = "good", error: str = "") -> None:
        self._quality = quality
        if quality == "good":
            try:
                self._value = float(value)
            except (TypeError, ValueError):
                self._value = None
        else:
            self._value = None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: item2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        width = self.width()
        height = self.height()
        # 表盘中心放在偏下位置：240° 圆弧的缺口正好在正下方，用来放数值文字，
        # 指针长度限制在 0.7r 以内就不会扫过文字。
        center = QPointF(width / 2, height * 0.66)
        radius = max(28.0, min(width / 2, height * 0.66) - 18)

        start_angle = 210 * 16
        span_angle = -240 * 16

        # 底盘
        painter.setPen(QPen(QColor(theme.BORDER), 10, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(
            QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2),
            start_angle,
            span_angle,
        )

        ratio = 0.0
        if self._value is not None and self.maximum != self.minimum:
            ratio = (self._value - self.minimum) / (self.maximum - self.minimum)
            ratio = max(0.0, min(1.0, ratio))

        # 值弧
        color = theme.ACCENT if self._quality == "good" else theme.QUALITY_BAD
        painter.setPen(QPen(QColor(color), 10, Qt.SolidLine, Qt.RoundCap))
        painter.drawArc(
            QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2),
            start_angle,
            int(span_angle * ratio),
        )

        # 指针
        angle = math.radians(210 - 240 * ratio)
        tip = QPointF(
            center.x() + radius * 0.70 * math.cos(angle),
            center.y() - radius * 0.70 * math.sin(angle),
        )
        painter.setPen(QPen(QColor(theme.TEXT), 2))
        painter.drawLine(center, tip)

        # 量程刻度：贴在圆弧两个端点下方
        painter.setPen(QPen(QColor(theme.TEXT_DIM)))
        font = QFont()
        font.setPointSize(8)
        font.setBold(False)
        painter.setFont(font)
        end_y = center.y() + radius * 0.5 + 2
        painter.drawText(
            QRectF(center.x() - radius * 0.866 - 22, end_y, 44, 14),
            Qt.AlignCenter,
            f"{self.minimum:g}",
        )
        painter.drawText(
            QRectF(center.x() + radius * 0.866 - 22, end_y, 44, 14),
            Qt.AlignCenter,
            f"{self.maximum:g}",
        )

        # 数值与标题：放在圆弧缺口的中下部
        text = format_value(self._value, self.point.decimals) if self._value is not None else "--"
        font.setPointSize(16)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT if self._value is not None else theme.QUALITY_BAD)))
        painter.drawText(QRectF(0, center.y() + 2, width, 24), Qt.AlignCenter, text)

        font.setPointSize(9)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT_DIM)))
        painter.drawText(
            QRectF(0, center.y() + 26, width, 16),
            Qt.AlignCenter,
            f"{self.point.title}  {self.point.unit}".strip(),
        )


class LampWidget(PointWidget):
    """指示灯：布尔量 / 状态显示。"""

    def __init__(self, pid: str, point, parent: QWidget | None = None) -> None:
        super().__init__(pid, point, parent)
        self._value = None
        self._quality = "bad"
        self.setMinimumSize(120, 84)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

    def set_value(self, value, quality: str = "good", error: str = "") -> None:
        self._quality = quality
        self._value = bool(value) if quality == "good" and value is not None else None
        self.update()

    def paintEvent(self, event) -> None:  # noqa: item2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        width = self.width()
        diameter = min(width * 0.42, self.height() * 0.55)
        cx = width / 2
        cy = diameter / 2 + 8

        if self._quality != "good":
            color = QColor(theme.QUALITY_BAD)
        elif self._value:
            color = QColor(theme.ACCENT)
        else:
            color = QColor(theme.LAMP_OFF)

        # 光晕
        if self._value and self._quality == "good":
            glow = QColor(color)
            glow.setAlpha(60)
            painter.setBrush(QBrush(glow))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(cx, cy), diameter * 0.78, diameter * 0.78)

        painter.setBrush(QBrush(color))
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawEllipse(QPointF(cx, cy), diameter / 2, diameter / 2)

        painter.setPen(QPen(QColor(theme.TEXT_DIM)))
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        painter.drawText(
            QRectF(0, cy + diameter / 2 + 4, width, 30),
            Qt.AlignHCenter | Qt.AlignTop,
            self.point.title,
        )


class TrendChart(QWidget):
    """滚动趋势图：自绘折线 + 网格 + 图例。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.window_s = 300.0
        self.max_points = 4000
        self._series: dict[str, dict] = {}
        self.setMinimumHeight(260)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # ------------------------------------------------ 数据接口

    def clear(self) -> None:
        for entry in self._series.values():
            entry["points"].clear()
        self.update()

    def add_series(self, pid: str, name: str, unit: str = "", color: str | None = None) -> None:
        if pid in self._series:
            return
        index = len(self._series)
        self._series[pid] = {
            "name": name,
            "unit": unit,
            "color": QColor(color or theme.series_color(index)),
            "points": deque(maxlen=self.max_points),
        }
        self.update()

    def remove_series(self, pid: str) -> None:
        self._series.pop(pid, None)
        self.update()

    @property
    def series_ids(self) -> list[str]:
        return list(self._series)

    def append(self, pid: str, ts: float, value) -> None:
        entry = self._series.get(pid)
        if entry is None or value is None:
            return
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        entry["points"].append((ts, numeric))
        self.update()

    def load_history(self, pid: str, samples: list[tuple[float, float]]) -> None:
        entry = self._series.get(pid)
        if entry is None:
            return
        entry["points"] = deque(samples[-self.max_points :], maxlen=self.max_points)
        self.update()

    def set_window(self, seconds: float) -> None:
        self.window_s = max(5.0, seconds)
        self.update()

    # ------------------------------------------------ 绘制

    def paintEvent(self, event) -> None:  # noqa: item2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.BG_PANEL))

        margin_left, margin_right, margin_top, margin_bottom = 62, 16, 30, 26
        plot = QRectF(
            margin_left,
            margin_top,
            max(10, self.width() - margin_left - margin_right),
            max(10, self.height() - margin_top - margin_bottom),
        )
        now = time.time()
        t_start = now - self.window_s

        # 收集显示范围内的数据并计算 Y 轴范围
        visible: list[tuple[dict, list[tuple[float, float]]]] = []
        y_min = None
        y_max = None
        for entry in self._series.values():
            pts = [(ts, v) for ts, v in entry["points"] if ts >= t_start]
            visible.append((entry, pts))
            for _ts, v in pts:
                y_min = v if y_min is None else min(y_min, v)
                y_max = v if y_max is None else max(y_max, v)

        if y_min is None:
            y_min, y_max = 0.0, 1.0
        if y_max - y_min < 1e-9:
            y_min, y_max = y_min - 0.5, y_max + 0.5
        span = y_max - y_min
        y_min -= span * 0.08
        y_max += span * 0.08

        # 网格与 Y 轴刻度
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)
        grid_pen = QPen(QColor(theme.BORDER))
        grid_pen.setStyle(Qt.DotLine)
        for i in range(5):
            y = plot.top() + plot.height() * i / 4
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            value = y_max - (y_max - y_min) * i / 4
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            painter.drawText(
                QRectF(4, y - 9, margin_left - 10, 18),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{value:.2f}",
            )

        # X 轴刻度
        for i in range(5):
            x = plot.left() + plot.width() * i / 4
            painter.setPen(grid_pen)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            seconds_ago = self.window_s * (1 - i / 4)
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            label = "现在" if i == 4 else f"-{seconds_ago:.0f}s"
            painter.drawText(QRectF(x - 26, plot.bottom() + 3, 52, 18), Qt.AlignCenter, label)

        painter.setPen(QPen(QColor(theme.BORDER)))
        painter.drawLine(QPointF(plot.left(), plot.bottom()), QPointF(plot.right(), plot.bottom()))

        def to_screen(ts: float, value: float) -> QPointF:
            x = plot.left() + plot.width() * (ts - t_start) / self.window_s
            y = plot.bottom() - plot.height() * (value - y_min) / (y_max - y_min)
            return QPointF(x, y)

        # 曲线
        for entry, pts in visible:
            if len(pts) < 2:
                continue
            painter.setPen(QPen(entry["color"], 2))
            polygon = QPolygonF([to_screen(ts, v) for ts, v in pts])
            painter.drawPolyline(polygon)

        # 图例
        painter.setPen(Qt.NoPen)
        x = plot.left()
        y = 12.0
        for entry, pts in visible:
            painter.setBrush(QBrush(entry["color"]))
            painter.drawRect(QRectF(x, y - 5, 10, 10))
            latest = f"{pts[-1][1]:.2f}" if pts else "--"
            painter.setPen(QPen(QColor(theme.TEXT)))
            painter.drawText(
                QRectF(x + 14, y - 9, 190, 18), Qt.AlignLeft | Qt.AlignVCenter, f"{entry['name']} = {latest}"
            )
            painter.setPen(Qt.NoPen)
            x += 210
            if x > plot.right() - 180:
                x = plot.left()
                y += 16
                if y > plot.top() + 20:
                    break


class StatusBadge(QWidget):
    """设备通信状态徽标。"""

    def __init__(self, name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = name
        self._connected = False
        self._error = ""
        self._state = "未启动"
        self.setFixedHeight(24)

    def set_status(self, connected: bool, state: str, error: str = "") -> None:
        self._connected = connected
        self._state = state
        self._error = error
        self.setToolTip(error or state)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: item2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(theme.ACCENT if self._connected else theme.QUALITY_BAD)
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(8, self.height() / 2), 5, 5)

        painter.setPen(QPen(QColor(theme.TEXT)))
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        painter.drawText(
            QRectF(20, 0, self.width() - 22, self.height()),
            Qt.AlignLeft | Qt.AlignVCenter,
            f"{self._name}  {self._state}",
        )


class SeriesPicker(QWidget):
    """趋势点位选择器。"""

    changed = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._boxes: list[QCheckBox] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(2)
        self._pid_map: dict[QCheckBox, str] = {}

    def set_points(self, entries: list[tuple[str, str]]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._boxes.clear()
        self._pid_map.clear()
        for pid, label in entries:
            box = QCheckBox(label)
            box.toggled.connect(self._emit)
            self._layout.addWidget(box)
            self._boxes.append(box)
            self._pid_map[box] = pid
        self._layout.addStretch(1)

    def _emit(self) -> None:
        self.changed.emit(self.selected())

    def selected(self) -> list[str]:
        return [self._pid_map[b] for b in self._boxes if b.isChecked()]
