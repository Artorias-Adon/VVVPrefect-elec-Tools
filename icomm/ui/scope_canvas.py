"""示波器画布（QPainter 自绘）。

两种工作模式
------------
**录波模式（array）** —— 画数组/块点位一次读回来的一整段波形，这是"真示波器"的用法：
    * 时基由点数和采样间隔决定；
    * 支持上升/下降沿触发 + 触发电平，触发点对齐到固定位置，波形才稳得住；
    * 双光标测量 Δt 与 ΔV。

**趋势模式（scalar）** —— 画标量点位的滚动趋势（低频量，如有效值、温度），
    按"秒/格"的时间窗口滚动，支持双光标。

不引入 QtCharts（其许可是 GPL/商业），全部自绘，避免许可问题。
"""

from __future__ import annotations

import bisect
import math
import time
from collections import deque
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from . import theme

#: 屏幕分格数（横向 10 格、纵向 8 格，与真实示波器习惯一致）
DIV_X = 10
DIV_Y = 8
MAX_ROLLING_SAMPLES = 4096
WaveFrame = tuple[float, float, list[float]]  # 接收时间、采样间隔（秒）、样本
WaveSegment = tuple[float, float, list[float], int, int]  # 首点时间、间隔、样本、可见索引范围
RollingStats = tuple[list[WaveSegment], int, float, float]

TRIGGER_MODES = ("auto", "normal", "single")
TRIGGER_EDGES = ("rising", "falling")


def format_seconds(value: float) -> str:
    """把秒格式化成好看的时间刻度（µs / ms / s）。"""
    magnitude = abs(value)
    if magnitude < 1e-3:
        return f"{value * 1e6:.1f}µs"
    if magnitude < 1.0:
        return f"{value * 1e3:.3f}ms"
    return f"{value:.3f}s"

def _wave_indices(values: list[float], first: int, last: int, pixels: float) -> range | list[int]:
    """密集波形每列最多画两点，保留桶内极值和原始顺序。"""
    columns = max(1, int(pixels))
    count = last - first + 1
    if count <= 2 * columns:
        return range(first, last + 1)
    indices = [first]
    for offset in range(columns):
        start = first + count * offset // columns
        stop = first + count * (offset + 1) // columns
        low = high = start
        for index in range(start + 1, stop):
            if values[index] < values[low]:
                low = index
            if values[index] > values[high]:
                high = index
        for index in sorted((low, high)):
            if index > indices[-1]:
                indices.append(index)
    if last > indices[-1]:
        indices.append(last)
    return indices

@dataclass
class ScopeChannel:
    """一条示波器通道。"""

    pid: str
    name: str
    unit: str = ""
    color: str = ""
    visible: bool = True
    kind: str = "array"           # array 录波 | scalar 趋势
    decimals: int = 2
    #: array 模式下相邻采样点间隔（微秒），0 表示未知，按点序号显示
    sample_interval_us: int = 0
    #: 最近一次数据
    values: list[float] = field(default_factory=list)
    samples: list[tuple[float, float]] = field(default_factory=list)  # scalar: (ts, value)
    max_samples: int = 6000
    last_value: float | None = None
    frames: deque[WaveFrame] = field(default_factory=deque)
    buffered_samples: int = 0

    def buffer_wave(self, ts: float) -> None:
        if not self.values or not self.sample_interval_us or (self.frames and ts <= self.frames[-1][0]):
            return
        values = self.values
        if len(values) > MAX_ROLLING_SAMPLES:
            values = values[-MAX_ROLLING_SAMPLES:]
        self.frames.append((ts, self.sample_interval_us / 1e6, values))
        self.buffered_samples += len(values)
        while self.buffered_samples > MAX_ROLLING_SAMPLES:
            self.buffered_samples -= len(self.frames.popleft()[2])

    def clear_frames(self) -> None:
        self.frames.clear()
        self.buffered_samples = 0

    @property
    def is_array(self) -> bool:
        return self.kind == "array"


class ScopeCanvas(QWidget):
    """示波器绘制区。"""

    cursorMoved = Signal(float, float)  # (cursor_a_x, cursor_b_x) 归一化 0..1
    timebaseChanged = Signal(float)  # 当前"秒/格"（滚轮缩放后同步回下拉）

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.channels: list[ScopeChannel] = []
        #: 秒/格（趋势模式用）
        self.sec_per_div = 20.0
        self.trigger_mode = "auto"
        self.trigger_edge = "rising"
        self.trigger_level = 0.0
        self.trigger_pid = ""
        self.y_min: float | None = None
        self.y_max: float | None = None
        self.cursor_a = 0.25
        self.cursor_b = 0.75
        self.show_cursors = True
        self.rolling = False
        #: 录波模式的横向平移：窗口里第一个采样点的序号（"归位"时清零）
        self.pan_samples = 0
        #: 正在拖哪个光标（"A"/"B"），松开鼠标清零
        self._dragging: str | None = None
        #: 中键平移的锚点
        self._pan_anchor = 0.0
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    # -------------------------------------------------- 通道

    def set_channels(self, channels: list[ScopeChannel]) -> None:
        self.channels = channels
        if channels and not self.trigger_pid:
            self.trigger_pid = channels[0].pid
        self.update()

    def clear_channels(self) -> None:
        """清空通道（工程切换时用，避免残留上一个工程的曲线）。"""
        self.channels = []
        self.trigger_pid = ""
        self.update()

    def visible_channels(self) -> list[ScopeChannel]:
        return [c for c in self.channels if c.visible]

    def channel(self, pid: str) -> ScopeChannel | None:
        for item in self.channels:
            if item.pid == pid:
                return item
        return None

    @property
    def mode(self) -> str:
        visible = self.visible_channels()
        if not visible:
            return "array"
        return "array" if visible[0].is_array else "scalar"

    # -------------------------------------------------- 数据

    def append_scalar(self, pid: str, ts: float, value: float) -> None:
        channel = self.channel(pid)
        if channel is None:
            return
        channel.samples.append((ts, value))
        if len(channel.samples) > channel.max_samples:
            del channel.samples[: len(channel.samples) - channel.max_samples]
        channel.last_value = value
        self.update()

    def set_waveform(
        self, pid: str, values: list[float], sample_interval_us: int = 0, timestamp: float = 0
    ) -> None:
        channel = self.channel(pid)
        if channel is None:
            return
        channel.values = values
        if sample_interval_us:
            channel.sample_interval_us = sample_interval_us
        channel.last_value = values[0] if values else None
        if self.rolling and channel.visible and timestamp:
            channel.buffer_wave(timestamp)
        self.update()

    def set_rolling(self, enabled: bool) -> None:
        if self.rolling == enabled:
            return
        self.rolling = enabled
        self.pan_samples = 0
        if not enabled:
            for channel in self.channels:
                channel.clear_frames()
        self.update()

    def clear_data(self) -> None:
        for channel in self.channels:
            channel.values = []
            channel.samples = []
            channel.clear_frames()
        self.update()

    # -------------------------------------------------- 参数

    def set_timebase(self, sec_per_div: float) -> None:
        # 下限放到 1ns：录波放大到"每格 1 个采样点"时不能卡在 1ms
        self.sec_per_div = max(1e-9, sec_per_div)
        self.timebaseChanged.emit(self.sec_per_div)
        self.update()

    def set_y_range(self, low: float | None, high: float | None) -> None:
        self.y_min, self.y_max = low, high
        self.update()

    def set_trigger(self, mode: str, edge: str, level: float, pid: str) -> None:
        self.trigger_mode = mode if mode in TRIGGER_MODES else "auto"
        self.trigger_edge = edge if edge in TRIGGER_EDGES else "rising"
        self.trigger_level = level
        if pid:
            self.trigger_pid = pid
        self.update()

    def set_cursors(self, a: float, b: float) -> None:
        self.cursor_a = min(max(a, 0.0), 1.0)
        self.cursor_b = min(max(b, 0.0), 1.0)
        self.update()

    # -------------------------------------------------- 交互

    def _ratio_at(self, x: float) -> float:
        """像素 x -> 0..1 的横向比例（超出绘图区就夹到边上）。"""
        plot = self._plot_rect(self._margins())
        if plot.width() <= 1:
            return 0.0
        return min(max((x - plot.left()) / plot.width(), 0.0), 1.0)

    def _cursor_hit(self, ratio: float) -> str:
        """拖动时该抓哪一个光标：离得近的那个。

        以前是"左键动 A、右键动 B" —— 可右键被上下文菜单占了，而且拖拽过程中
        ``event.button()`` 恒为 NoButton，于是永远只动 A，B 根本拖不了。
        """
        return "A" if abs(ratio - self.cursor_a) <= abs(ratio - self.cursor_b) else "B"

    def set_cursor(self, which: str, ratio: float) -> None:
        """单独设某个光标（给外部/脚本用，也供拖动调用）。"""
        ratio = min(max(ratio, 0.0), 1.0)
        if which == "B":
            self.cursor_b = ratio
        else:
            self.cursor_a = ratio
        self.cursorMoved.emit(self.cursor_a, self.cursor_b)
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: item2
        if event.button() == Qt.MiddleButton:
            self._pan_anchor = event.position().x()
            return
        if event.button() != Qt.LeftButton:
            # 右键留给上下文菜单，不再拿它放光标
            return
        ratio = self._ratio_at(event.position().x())
        self._dragging = self._cursor_hit(ratio)
        self.set_cursor(self._dragging, ratio)

    def mouseMoveEvent(self, event) -> None:  # noqa: item2
        if event.buttons() & Qt.MiddleButton:
            # 录波模式中键左右平移（放大以后要挪着看）
            self.pan_by_pixels(event.position().x() - self._pan_anchor)
            self._pan_anchor = event.position().x()
            return
        if event.buttons() & Qt.LeftButton:
            ratio = self._ratio_at(event.position().x())
            if self._dragging is None:
                self._dragging = self._cursor_hit(ratio)
            self.set_cursor(self._dragging, ratio)
            return
        # 悬停在光标附近换个鼠标形状，提示"这个能拖"
        ratio = self._ratio_at(event.position().x())
        near = min(abs(ratio - self.cursor_a), abs(ratio - self.cursor_b))
        self.setCursor(Qt.SizeHorCursor if near < 0.012 else Qt.ArrowCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: item2
        self._dragging = None

    def wheelEvent(self, event) -> None:  # noqa: item2
        """滚轮缩放时基：向上放大（每格时间变短），向下缩小。"""
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        self.set_timebase(self.sec_per_div * factor)
        self.clamp_pan()

    # -------------------------------------------------- 录波窗口（缩放 / 平移）

    def _array_view(self, count: int, dt: float) -> tuple[int, int, float]:
        """录波模式的显示窗口：``(首点号, 末点号, 窗口秒数)``。

        整段放得下就整段显示（和以前一样）；时基调短就是放大，最短可到
        "每格 1 个采样点"。``dt <= 0``（没标采样间隔）时不缩放，按点序号铺满。
        """
        if count <= 1 or dt <= 0:
            return 0, max(0, count - 1), 0.0
        window = self.sec_per_div * DIV_X
        total = (count - 1) * dt
        if window >= total:
            return 0, count - 1, total
        points = max(1, int(math.ceil(window / dt)))
        first = max(0, min(int(self.pan_samples), count - 1 - points))
        return first, min(count - 1, first + points), window

    def _array_meta(self, visible: list["ScopeChannel"]) -> tuple[int, float]:
        """录波模式画的是多少个点、每点多少秒（触发模式下从触发点算起）。"""
        channel = next((c for c in visible if c.is_array), None)
        if channel is None or not channel.values:
            return 0, 0.0
        count = len(channel.values)
        if self.trigger_mode in ("normal", "single") and count > 1:
            offset = self._trigger_index(channel.values, self.trigger_level, self.trigger_edge)
            count = max(1, count - offset)
        return count, (channel.sample_interval_us or 0) / 1e6

    def array_sample_count(self) -> int:
        """当前可见波形点位的点数（没有波形点位时 0）。"""
        for channel in self.visible_channels():
            if channel.is_array and channel.values:
                return len(channel.values)
        return 0

    def array_span_seconds(self) -> float:
        """当前可见波形整段的时长（秒）；不知道采样间隔时 0。"""
        for channel in self.visible_channels():
            if channel.is_array and channel.values and channel.sample_interval_us:
                return max(1, len(channel.values) - 1) * channel.sample_interval_us / 1e6
        return 0.0

    def array_full_timebase(self) -> float:
        """看完整段波形所需的"秒/格"（= 整段时长 / 横向格数）。"""
        return self.array_span_seconds() / DIV_X

    def clear_pan(self) -> None:
        """横向平移归零（"归位"用）。"""
        self.pan_samples = 0
        self.update()

    def clamp_pan(self) -> None:
        """把平移量夹回有效范围，免得拖到记录外面。"""
        channel = next((c for c in self.visible_channels() if c.is_array), None)
        if channel is None:
            self.pan_samples = 0
            self.update()
            return
        dt = (channel.sample_interval_us or 0) / 1e6
        first, _last, _window = self._array_view(len(channel.values), dt)
        self.pan_samples = first
        self.update()

    def pan_by_pixels(self, dx: float) -> None:
        """按拖动的像素数平移（换算成采样点）。"""
        if self.rolling:
            return
        channel = next((c for c in self.visible_channels() if c.is_array), None)
        if channel is None or not channel.sample_interval_us:
            return
        plot = self._plot_rect(self._margins())
        dt = channel.sample_interval_us / 1e6
        _first, _last, window = self._array_view(len(channel.values), dt)
        if plot.width() <= 1 or dt <= 0 or window <= 0:
            return
        self.pan_samples += -dx * window / plot.width() / dt
        self.clamp_pan()

    # -------------------------------------------------- 绘制

    @staticmethod
    def _margins() -> tuple[int, int, int, int]:
        return 72, 18, 16, 30  # left, right, top, bottom

    def _plot_rect(self, margin) -> QRectF:
        left, right, top, bottom = margin
        return QRectF(
            left,
            top,
            max(10, self.width() - left - right),
            max(10, self.height() - top - bottom),
        )

    def _trigger_index(self, values: list[float], level: float, edge: str) -> int:
        """在波形里找触发起点：第一个跨越触发电平的采样点。"""
        if len(values) < 2:
            return 0
        for index in range(1, len(values)):
            prev, cur = values[index - 1], values[index]
            if edge == "rising" and prev < level <= cur:
                return index
            if edge == "falling" and prev > level >= cur:
                return index
        return 0

    def _rolling_end(self, visible: list[ScopeChannel]) -> float:
        return max((ch.frames[-1][0] for ch in visible if ch.frames), default=0.0)

    @staticmethod
    def _rolling_segments(channel: ScopeChannel, end: float, window: float) -> list[WaveSegment]:
        segments: list[WaveSegment] = []
        for timestamp, step, values in reversed(channel.frames):
            relative_end = timestamp - end
            if relative_end < -window:
                break
            origin = relative_end - (len(values) - 1) * step
            first = max(0, math.ceil((-window - origin) / step - 1e-6))
            if first < len(values):
                segments.append((origin, step, values, first, len(values) - 1))
        return segments

    def _rolling_data(self, visible: list[ScopeChannel]) -> dict[str, RollingStats]:
        end = self._rolling_end(visible)
        window = self.seg_per_div_total()
        data = {}
        for channel in visible:
            segments = self._rolling_segments(channel, end, window)
            samples = []
            for _, _, values, first, last in segments:
                samples.extend(values if first == 0 else values[first:last + 1])
            if samples:
                data[channel.pid] = (segments, len(samples), min(samples), max(samples))
        return data

    def paintEvent(self, event) -> None:  # noqa: item2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.CANVAS_BG))

        margin = self._margins()
        plot = self._plot_rect(margin)
        visible = self.visible_channels()

        if self.rolling:
            visible = [channel for channel in visible if channel.is_array]
        rolling_data = self._rolling_data(visible) if self.rolling else {}
        extrema = [(low, high) for _, _, low, high in rolling_data.values()] if self.rolling else None
        y_range = self._y_range(visible, extrema)
        self._draw_grid(painter, plot, visible, y_range)
        if visible:
            if self.rolling:
                self._draw_rolling(painter, plot, visible, rolling_data, y_range)
            elif self.mode == "array":
                self._draw_waveforms(painter, plot, visible, y_range)
            else:
                self._draw_trends(painter, plot, visible, y_range)
        else:
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            font = QFont()
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(plot, Qt.AlignCenter, "请在左侧勾选要显示的通道")

        if self.show_cursors:
            self._draw_cursors(painter, plot, visible)
        self._draw_legend(painter, plot, visible, rolling_data)

    # -------------------------------------------------- 网格与坐标

    def _y_range(self, visible: list[ScopeChannel], extrema=None) -> tuple[float, float]:
        if self.y_min is not None and self.y_max is not None and self.y_max > self.y_min:
            return self.y_min, self.y_max
        if extrema is None:
            series = (c.values if c.is_array else [v for _t, v in c.samples] for c in visible)
            extrema = [(min(data), max(data)) for data in series if data]
        if not extrema:
            return -1.0, 1.0
        low = min(pair[0] for pair in extrema)
        high = max(pair[1] for pair in extrema)
        # 手动只给了单边时，另一边自动
        if self.y_min is not None:
            low = self.y_min
        if self.y_max is not None:
            high = self.y_max
        if high - low < 1e-9:
            low, high = low - 0.5, high + 0.5
        pad = (high - low) * 0.08
        if self.y_min is None:
            low -= pad
        if self.y_max is None:
            high += pad
        return low, high

    def _x_window(self) -> tuple[float, float]:
        """趋势模式的横轴时间窗口 [start, end]，右端跟随最新数据时间。"""
        now = 0.0
        for channel in self.channels:
            if channel.samples:
                now = max(now, channel.samples[-1][0])
        if now <= 0:
            return 0.0, self.sec_per_div * DIV_X
        return now - self.sec_per_div * DIV_X, now

    def _draw_grid(self, painter: QPainter, plot: QRectF, visible: list[ScopeChannel], y_range) -> None:
        font = QFont()
        font.setPointSize(8)
        painter.setFont(font)

        pen_minor = QPen(QColor(theme.GRID_MINOR))
        pen_major = QPen(QColor(theme.GRID_MAJOR))
        for index in range(DIV_X + 1):
            x = plot.left() + plot.width() * index / DIV_X
            painter.setPen(pen_major if index % 5 == 0 else pen_minor)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
        for index in range(DIV_Y + 1):
            y = plot.top() + plot.height() * index / DIV_Y
            painter.setPen(pen_major if index % 4 == 0 else pen_minor)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))

        low, high = y_range
        painter.setPen(QPen(QColor(theme.TEXT_DIM)))
        for index in range(DIV_Y + 1):
            y = plot.top() + plot.height() * index / DIV_Y
            value = high - (high - low) * index / DIV_Y
            painter.drawText(
                QRectF(4, y - 9, self._margins()[0] - 12, 18),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{value:g}",
            )

        # 横向刻度：录波模式显示时间（若知道采样间隔），否则显示点序号
        if self.rolling:
            for index in range(DIV_X + 1):
                x = plot.left() + plot.width() * index / DIV_X
                label = format_seconds((index - DIV_X) * self.sec_per_div) if index < DIV_X else "最新"
                painter.drawText(QRectF(x - 34, plot.bottom() + 4, 68, 16), Qt.AlignCenter, label)
        elif self.mode == "array":
            count, dt = self._array_meta(visible)
            first, last, window = self._array_view(count, dt)
            for index in range(DIV_X + 1):
                x = plot.left() + plot.width() * index / DIV_X
                painter.setPen(QPen(QColor(theme.TEXT_DIM)))
                if dt > 0 and window > 0:
                    # 刻度跟着缩放窗口走，放大后才知道自己是几微秒
                    text = format_seconds(first * dt + window * index / DIV_X)
                elif count:
                    text = f"{first + (last - first) * index // DIV_X}"
                else:
                    text = "-"
                painter.drawText(QRectF(x - 34, plot.bottom() + 4, 68, 16), Qt.AlignCenter, text)
        else:
            start, end = self._x_window()
            for index in range(DIV_X + 1):
                x = plot.left() + plot.width() * index / DIV_X
                ts = start + (end - start) * index / DIV_X
                painter.setPen(QPen(QColor(theme.TEXT_DIM)))
                text = "现在" if index == DIV_X else time.strftime("%H:%M:%S", time.localtime(ts))
                painter.drawText(QRectF(x - 30, plot.bottom() + 4, 60, 16), Qt.AlignCenter, text)

        # 触发电平线
        if not self.rolling and self.mode == "array" and visible and self.trigger_mode != "auto":
            y = plot.bottom() - plot.height() * (self.trigger_level - low) / (high - low)
            if plot.top() <= y <= plot.bottom():
                pen = QPen(QColor(theme.CURSOR_A), 1, Qt.DashLine)
                painter.setPen(pen)
                painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
                painter.setPen(QPen(QColor(theme.CURSOR_A)))
                painter.drawText(QPointF(plot.left() + 6, y - 4), "T")

    # -------------------------------------------------- 曲线

    def _draw_rolling(
        self, painter: QPainter, plot: QRectF, visible: list[ScopeChannel],
        rolling_data: dict[str, RollingStats], y_range: tuple[float, float],
    ) -> None:
        window = self.seg_per_div_total()
        low, high = y_range
        x_scale = plot.width() / window
        y_scale = plot.height() / (high - low)
        right, bottom = plot.right(), plot.bottom()
        painter.save()
        painter.setClipRect(plot)
        painter.setRenderHint(QPainter.Antialiasing, False)
        for channel in visible:
            entry = rolling_data.get(channel.pid)
            if entry is None:
                continue
            painter.setPen(QPen(QColor(channel.color), 1))
            for origin, step, values, first, last in entry[0]:
                indices = _wave_indices(values, first, last, (last - first) * step * x_scale)
                points = QPolygonF([
                    QPointF(right + (origin + index * step) * x_scale,
                            bottom - (values[index] - low) * y_scale)
                    for index in indices
                ])
                if len(points) == 1:
                    painter.drawPoint(points[0])
                else:
                    painter.drawPolyline(points)
        painter.restore()

    def _draw_waveforms(self, painter: QPainter, plot: QRectF, visible: list[ScopeChannel], y_range) -> None:
        low, high = y_range
        span = high - low or 1.0

        for channel in visible:
            values = channel.values
            if len(values) < 2:
                continue
            triggered = self.trigger_mode in ("normal", "single")
            if triggered:
                start = self._trigger_index(values, self.trigger_level, self.trigger_edge)
                values = values[start:]
                if len(values) < 2:
                    continue
            dt = (channel.sample_interval_us or 0) / 1e6
            first, last, window = self._array_view(len(values), dt)
            if window > 0:
                t0 = first * dt
                step = dt
                scale = window
            else:
                # 不知道采样间隔：退回"按点序号铺满"的老算法
                t0 = float(first)
                step = 1.0
                scale = float(max(1, last - first))
            spacing = plot.width() * step / scale
            indices = _wave_indices(values, first, last, (last - first) * spacing)
            points = [
                QPointF(
                    plot.left() + plot.width() * (index * step - t0) / scale,
                    plot.bottom() - plot.height() * (values[index] - low) / span,
                )
                for index in indices
            ]
            painter.setPen(QPen(QColor(channel.color), 1.6))
            painter.drawPolyline(QPolygonF(points))

            # 放大到点间距够大时把采样点画出来 —— 量跳变沿时要能看清每一个点
            if spacing >= 6.0 and len(points) <= 4000:
                painter.setBrush(QBrush(QColor(channel.color)))
                for point in points:
                    painter.drawEllipse(point, 2.4, 2.4)
                painter.setBrush(Qt.NoBrush)

            if triggered and first == 0 and points:
                # 标出触发点
                painter.setPen(QPen(QColor(theme.CURSOR_A), 1))
                painter.setBrush(QBrush(QColor(theme.CURSOR_A)))
                painter.drawEllipse(points[0], 3.0, 3.0)
                painter.setBrush(Qt.NoBrush)

    def _draw_trends(self, painter: QPainter, plot: QRectF, visible: list[ScopeChannel], y_range) -> None:
        low, high = y_range
        span = high - low or 1.0
        start, end = self._x_window()
        width = max(1e-6, end - start)

        for channel in visible:
            samples = [(ts, v) for ts, v in channel.samples if ts >= start]
            if len(samples) < 2:
                continue
            points = [
                QPointF(
                    plot.left() + plot.width() * (ts - start) / width,
                    plot.bottom() - plot.height() * (value - low) / span,
                )
                for ts, value in samples
            ]
            painter.setPen(QPen(QColor(channel.color), 1.6))
            painter.drawPolyline(QPolygonF(points))

    # -------------------------------------------------- 光标与图例

    def _sample_at(self, channel: ScopeChannel, ratio: float) -> float | None:
        if self.rolling:
            end = self._rolling_end(self.visible_channels())
            target = (ratio - 1.0) * self.seg_per_div_total()
            for ts, step, values in reversed(channel.frames):
                position = (target - ((ts - end) - (len(values) - 1) * step)) / step
                if -1e-6 <= position <= len(values) - 1 + 1e-6:
                    return values[round(position)]
            return None
        if channel.is_array:
            if not channel.values:
                return None
            values = channel.values
            offset = 0
            if self.trigger_mode in ("normal", "single") and len(values) > 1:
                offset = self._trigger_index(values, self.trigger_level, self.trigger_edge)
            dt = (channel.sample_interval_us or 0) / 1e6
            first, last, window = self._array_view(max(1, len(values) - offset), dt)
            if window > 0 and dt > 0:
                index = offset + int(round((first * dt + window * ratio) / dt))
            elif offset or first:
                index = offset + int(round(first + (last - first) * ratio))
            else:
                index = int(round(ratio * (len(values) - 1)))
            index = min(max(index, 0), len(values) - 1)
            return values[index]
        if not channel.samples:
            return None
        start, end = self._x_window()
        target = start + (end - start) * ratio
        times = [ts for ts, _v in channel.samples]
        position = bisect.bisect_left(times, target)
        position = min(max(position, 0), len(channel.samples) - 1)
        return channel.samples[position][1]

    def _draw_cursors(self, painter: QPainter, plot: QRectF, visible: list[ScopeChannel]) -> None:
        for ratio, label, color in (
            (self.cursor_a, "A", theme.CURSOR_A),
            (self.cursor_b, "B", theme.CURSOR_B),
        ):
            x = plot.left() + plot.width() * ratio
            painter.setPen(QPen(QColor(color), 1, Qt.DashLine))
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(QPen(QColor(color)))
            painter.drawText(QPointF(x + 3, plot.top() + 12), label)

        # Δt / ΔV 读数
        channel = visible[0] if visible else None
        if channel is None:
            return
        delta_ratio = abs(self.cursor_b - self.cursor_a)
        if self.rolling:
            delta_text = f"Δt ≈ {format_seconds(delta_ratio * self.seg_per_div_total())}"
        elif channel.is_array and channel.values:
            count = len(channel.values)
            dt = (channel.sample_interval_us or 0) / 1e6
            _first, _last, window = self._array_view(count, dt)
            if dt > 0 and window > 0:
                # 按当前缩放窗口算，放大以后量沿才准
                delta_text = f"Δt = {format_seconds(delta_ratio * window)}"
            else:
                delta_text = f"Δ点 = {delta_ratio * max(1, count - 1):.1f}"
        else:
            delta_text = f"Δt = {delta_ratio * self.seg_per_div_total():.3f} s"

        value_a = self._sample_at(channel, self.cursor_a)
        value_b = self._sample_at(channel, self.cursor_b)
        if value_a is not None and value_b is not None:
            delta_text += f"   ΔV = {value_b - value_a:+.3f} {channel.unit}"
        elif self.rolling:
            delta_text += "   光标处无采样"

        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        box = QRectF(plot.left() + 8, plot.top() + 8, 300, 20)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(0, 0, 0, 160)))
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QPen(QColor(theme.TEXT)))
        painter.drawText(box.adjusted(8, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, delta_text)

    def seg_per_div_total(self) -> float:
        return self.sec_per_div * DIV_X

    def _draw_legend(
        self, painter: QPainter, plot: QRectF, visible: list[ScopeChannel],
        rolling_data: dict[str, RollingStats],
    ) -> None:
        font = QFont()
        font.setPointSize(9)
        painter.setFont(font)
        y = plot.top() + 34
        for channel in visible:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(channel.color)))
            painter.drawRect(QRectF(plot.left() + 10, y - 6, 12, 3))

            if self.rolling:
                stats = rolling_data.get(channel.pid)
                latest = f"{stats[1]} 点  峰峰值 {stats[3] - stats[2]:.1f} {channel.unit}" if stats else "无数据"
            elif channel.is_array:
                latest = f"{len(channel.values)} 点" if channel.values else "无数据"
                if channel.values:
                    latest += f"  峰峰值 {max(channel.values) - min(channel.values):.1f} {channel.unit}"
            else:
                latest = (
                    f"{channel.last_value:.{channel.decimals}f} {channel.unit}"
                    if channel.last_value is not None
                    else "无数据"
                )
            painter.setPen(QPen(QColor(theme.TEXT)))
            painter.drawText(
                QRectF(plot.left() + 28, y - 12, plot.width() - 40, 18),
                Qt.AlignLeft | Qt.AlignVCenter,
                f"{channel.name}  {latest}",
            )
            y += 16

        # 右下角标出当前时基/采样间隔（画在图内右上角，避免与 X 轴刻度重叠）
        painter.setPen(QPen(QColor(theme.TEXT_DIM)))
        if self.rolling:
            text = "帧间按接收时间估算"
        elif self.mode == "array":
            channel = visible[0] if visible else None
            if channel and channel.sample_interval_us:
                text = f"采样间隔 {channel.sample_interval_us}us / 点"
            else:
                text = "按点序号显示（未配置采样间隔）"
        else:
            text = f"时基 {self.sec_per_div:g} 秒/格"
        painter.drawText(
            QRectF(plot.left(), plot.top() + 4, plot.width() - 6, 16),
            Qt.AlignRight | Qt.AlignVCenter,
            text,
        )
