"""主页示意图。

用工程里的 ``DiagramDef`` 数据渲染电气主回路图：
* 节点用内置图元库画（电网/断路器/变压器/整流/DC-DC/母线/电容/负载/仪表/DSP…）；
* 连线的 ac / dc / signal 三种线型区分交直流与信号；
* 节点可点击 -> 跳到对应设备详情或页面；
* 节点可以绑定实时点号，直接把当前值叠在图上（母线电压这类最实用）；
* 也可以挂一张 SVG 底图（现场 CAD 导出的图），再在 ``hotspots`` 里标可点击区域。

坐标全部是 0~1 归一化，换分辨率/缩放都不用改图。
"""

from __future__ import annotations

import os

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.models import DiagramDef, DiagramHotspot, DiagramNode, Project
from .. import theme
from ..widgets import ElidedLabel

try:  # SVG 底图是可选功能，缺 QtSvg 也不该影响程序启动
    from PySide6.QtSvg import QSvgRenderer

    _HAS_SVG = True
except Exception:  # pragma: no cover
    QSvgRenderer = None  # type: ignore[assignment]
    _HAS_SVG = False


class DiagramCanvas(QWidget):
    """示意图绘制区（支持缩放、拖拽、点击、悬停高亮）。"""

    nodeActivated = Signal(str, str)  # (action, target)
    nodeMoved = Signal(str, float, float)  # (节点 id, x, y)　仅编辑模式下发出

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.diagram = DiagramDef()
        self.values: dict[str, tuple] = {}   # 点号 -> (值, 质量)
        self.scale = 1.0
        self.offset = QPointF(0.0, 0.0)
        self._drag_from: QPointF | None = None
        self._hover: str = ""
        self._svg = None
        self._svg_path = ""
        #: 编辑模式：拖动节点会直接改坐标并发 nodeMoved（主页面保持只读）
        self.editable = False
        self._moving: str = ""
        self.setMinimumHeight(420)
        self.setMouseTracking(True)

    # -------------------------------------------------- 数据

    def set_diagram(self, diagram: DiagramDef, base_dir: str = "") -> None:
        self.diagram = diagram or DiagramDef()
        self._svg = None
        self._svg_path = ""
        if self.diagram.svg_background:
            path = self.diagram.svg_background
            if not os.path.isabs(path) and base_dir:
                path = os.path.join(base_dir, path)
            if os.path.exists(path) and _HAS_SVG:
                renderer = QSvgRenderer(path)
                if renderer.isValid():
                    self._svg = renderer
                    self._svg_path = path
        self.scale = 1.0
        self.offset = QPointF(0.0, 0.0)
        self._moving = ""
        self.update()

    def set_values(self, values: dict) -> None:
        self.values.update(values)
        if self.diagram.nodes:
            self.update()

    # -------------------------------------------------- 几何

    def _content_rect(self, rect: QRectF) -> QRectF:
        """把 0~1 归一化坐标映射到控件矩形。"""
        margin = 16.0
        base = QRectF(
            rect.left() + margin,
            rect.top() + margin,
            max(10.0, rect.width() - margin * 2),
            max(10.0, rect.height() - margin * 2),
        )
        return base

    def _node_rect(self, node: DiagramNode, rect: QRectF) -> QRectF:
        origin, width, height = self._content_metrics(rect)
        return QRectF(
            origin.x() + node.x * width,
            origin.y() + node.y * height,
            max(24.0, node.w * width),
            max(20.0, node.h * height),
        )

    def _content_metrics(self, rect: QRectF) -> tuple[QPointF, float, float]:
        """返回（内容原点, 宽, 高）：把 0~1 归一化坐标映射到控件上的那套换算。"""
        base = self._content_rect(rect)
        width = base.width() * self.scale
        height = base.height() * self.scale
        origin = QPointF(
            base.left() + self.offset.x() + (base.width() - width) / 2,
            base.top() + self.offset.y() + (base.height() - height) / 2,
        )
        return origin, width, height

    def normalized(self, position: QPointF) -> tuple[float, float]:
        """控件坐标 -> 0~1 归一化内容坐标（编辑时用来算新位置）。"""
        origin, width, height = self._content_metrics(QRectF(self.rect()))
        return (
            (position.x() - origin.x()) / max(1.0, width),
            (position.y() - origin.y()) / max(1.0, height),
        )

    def move_node(self, node: DiagramNode, ratio_x: float, ratio_y: float) -> None:
        """按"节点中心落在 (ratio_x, ratio_y)"放置节点，并夹在画面内。"""
        node.x = round(min(max(0.0, ratio_x - node.w / 2), max(0.0, 1.0 - node.w)), 4)
        node.y = round(min(max(0.0, ratio_y - node.h / 2), max(0.0, 1.0 - node.h)), 4)
        self.update()
        self.nodeMoved.emit(node.id, node.x, node.y)

    def _node_center(self, node_id: str, rect: QRectF) -> QPointF | None:
        node = self.diagram.node(node_id)
        if node is None:
            return None
        return self._node_rect(node, rect).center()

    # -------------------------------------------------- 交互

    def mousePressEvent(self, event) -> None:  # noqa: item2
        if self.editable and event.buttons() & Qt.LeftButton:
            node_id = self._hit_test(event.position())
            if node_id:
                # 编辑模式下抓住节点 = 移动它，而不是平移画面
                self._moving = node_id
                self._drag_from = None
                self.setCursor(Qt.SizeAllCursor)
                return
        self._drag_from = event.position()

    def mouseMoveEvent(self, event) -> None:  # noqa: item2
        if self._moving:
            node = self.diagram.node(self._moving)
            if node is not None and event.buttons() & Qt.LeftButton:
                ratio_x, ratio_y = self.normalized(event.position())
                self.move_node(node, ratio_x, ratio_y)
            return
        if self._drag_from is not None and event.buttons() & Qt.LeftButton:
            delta = event.position() - self._drag_from
            self.offset += delta
            self._drag_from = event.position()
            self.update()
            return
        hovered = self._hit_test(event.position())
        if hovered != self._hover:
            self._hover = hovered
            cursor = Qt.ArrowCursor
            if hovered:
                cursor = Qt.SizeAllCursor if self.editable else Qt.PointingHandCursor
            self.setCursor(cursor)
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: item2
        if self._moving:
            self._moving = ""
            self.setCursor(Qt.ArrowCursor if not self.editable else Qt.SizeAllCursor)
            return
        moved = (
            self._drag_from is not None
            and (event.position() - self._drag_from).manhattanLength() > 4
        )
        self._drag_from = None
        if moved:
            return
        node_id = self._hit_test(event.position())
        node = self.diagram.node(node_id) if node_id else None
        if node is not None and node.action and node.action != "none" and node.target:
            self.nodeActivated.emit(node.action, node.target)
            return
        hotspot = self._hit_hotspot(event.position())
        if hotspot is not None and hotspot.action != "none" and hotspot.target:
            self.nodeActivated.emit(hotspot.action, hotspot.target)

    def wheelEvent(self, event) -> None:  # noqa: item2
        factor = 1.1 if event.angleDelta().y() > 0 else 1 / 1.1
        self.scale = min(3.0, max(0.4, self.scale * factor))
        self.update()

    def reset_view(self) -> None:
        self.scale = 1.0
        self.offset = QPointF(0.0, 0.0)
        self.update()

    def _hit_test(self, position: QPointF) -> str:
        rect = QRectF(self.rect())
        for node in reversed(self.diagram.nodes):
            if self._node_rect(node, rect).contains(position):
                return node.id
        return ""

    def _hit_hotspot(self, position: QPointF) -> DiagramHotspot | None:
        base = self._content_rect(QRectF(self.rect()))
        for hotspot in self.diagram.hotspots:
            area = QRectF(
                base.left() + hotspot.x * base.width(),
                base.top() + hotspot.y * base.height(),
                hotspot.w * base.width(),
                hotspot.h * base.height(),
            )
            if area.contains(position):
                return hotspot
        return None

    # -------------------------------------------------- 绘制

    def paintEvent(self, event) -> None:  # noqa: item2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(theme.CANVAS_BG))

        rect = QRectF(self.rect())
        if self._svg is not None:
            self._svg.render(painter, rect)

        if not self.diagram.nodes and self._svg is None:
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            font = QFont()
            font.setPointSize(11)
            painter.setFont(font)
            painter.drawText(
                rect,
                Qt.AlignCenter,
                "当前工程没有配置示意图。\n可在工程文件里配置 diagram.nodes，或载入内置 SST 示例工程查看效果。",
            )
            return

        self._draw_links(painter, rect)
        for node in self.diagram.nodes:
            self._draw_node(painter, node, rect)

    def _draw_links(self, painter: QPainter, rect: QRectF) -> None:
        for link in self.diagram.links:
            start = self._node_center(link.source, rect)
            end = self._node_center(link.target, rect)
            if start is None or end is None:
                continue
            style = {
                "ac": (theme.WIRE_AC, Qt.SolidLine, 2.4),
                "dc": (theme.WIRE_DC, Qt.SolidLine, 2.4),
                "signal": (theme.WIRE_SIGNAL, Qt.DotLine, 1.4),
            }.get(link.style, (theme.WIRE_SIGNAL, Qt.SolidLine, 2.0))
            pen = QPen(QColor(style[0]), style[2], style[1])
            painter.setPen(pen)
            painter.drawLine(start, end)
            self._draw_arrow(painter, start, end, QColor(style[0]))

            if link.label:
                mid = QPointF((start.x() + end.x()) / 2, (start.y() + end.y()) / 2)
                painter.setPen(QPen(QColor(theme.TEXT_DIM)))
                font = QFont()
                font.setPointSize(8)
                painter.setFont(font)
                painter.drawText(QRectF(mid.x() - 40, mid.y() - 18, 80, 16), Qt.AlignCenter, link.label)

    @staticmethod
    def _draw_arrow(painter: QPainter, start: QPointF, end: QPointF, color: QColor) -> None:
        import math

        angle = math.atan2(end.y() - start.y(), end.x() - start.x())
        size = 9.0
        tip = end
        left = QPointF(
            tip.x() - size * math.cos(angle - math.pi / 6),
            tip.y() - size * math.sin(angle - math.pi / 6),
        )
        right = QPointF(
            tip.x() - size * math.cos(angle + math.pi / 6),
            tip.y() - size * math.sin(angle + math.pi / 6),
        )
        painter.setBrush(QBrush(color))
        painter.setPen(Qt.NoPen)
        painter.drawPolygon(QPolygonF([tip, left, right]))

    def _draw_node(self, painter: QPainter, node: DiagramNode, rect: QRectF) -> None:
        area = self._node_rect(node, rect)
        color = QColor(node.color or theme.ACCENT)
        hovered = node.id == self._hover
        clickable = node.action and node.action != "none"

        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.setBrush(QBrush(QColor(theme.NODE_BG)))
        painter.drawRoundedRect(area, 6, 6)

        if hovered and clickable:
            painter.setPen(QPen(color.lighter(140), 2))
            painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(area.adjusted(1, 1, -1, -1), 6, 6)

        self._draw_symbol(painter, area.adjusted(8, 6, -8, -6), node.symbol, color)

        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(theme.TEXT)))
        painter.drawText(
            QRectF(area.left(), area.bottom() - 32, area.width(), 16),
            Qt.AlignCenter,
            node.label,
        )

        if node.sub_label:
            font.setPointSize(7)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QPen(QColor(theme.TEXT_DIM)))
            painter.drawText(
                QRectF(area.left(), area.bottom() - 17, area.width(), 14),
                Qt.AlignCenter,
                node.sub_label,
            )

        # 叠加实时值
        if node.value_point:
            value, quality = self.values.get(node.value_point, (None, "bad"))
            font.setPointSize(10)
            font.setBold(True)
            painter.setFont(font)
            if value is None:
                painter.setPen(QPen(QColor(theme.QUALITY_BAD)))
                text = "--"
            else:
                painter.setPen(
                    QPen(QColor(theme.TEXT if quality == "good" else theme.QUALITY_BAD))
                )
                try:
                    text = f"{float(value):.{max(0, node.decimals)}f}"
                except (TypeError, ValueError):
                    text = str(value)
                if node.unit:
                    text += f" {node.unit}"
            band = QRectF(area.left(), area.top() + 2, area.width(), 16)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(QColor(0, 0, 0, 120)))
            painter.drawRoundedRect(band, 3, 3)
            painter.setPen(QPen(QColor(theme.TEXT if value is not None else theme.QUALITY_BAD)))
            painter.drawText(band, Qt.AlignCenter, text)

    def _draw_symbol(self, painter: QPainter, area: QRectF, symbol: str, color: QColor) -> None:
        """内置图元库。形状只求一眼能认出来，不追求电气制图标准的精细化。"""
        painter.setPen(QPen(color, 1.8))
        painter.setBrush(Qt.NoBrush)
        cx, cy = area.center().x(), area.center().y() - 6
        width = min(area.width(), 74.0)
        height = min(area.height() - 14, 44.0)

        if symbol == "grid":
            painter.drawEllipse(QPointF(cx, cy), width * 0.3, width * 0.3)
            path = QPolygonF(
                [
                    QPointF(cx - width * 0.18, cy),
                    QPointF(cx - width * 0.09, cy - height * 0.18),
                    QPointF(cx, cy + height * 0.18),
                    QPointF(cx + width * 0.09, cy - height * 0.18),
                    QPointF(cx + width * 0.18, cy),
                ]
            )
            painter.drawPolyline(path)
        elif symbol == "transformer":
            radius = width * 0.16
            painter.drawEllipse(QPointF(cx - radius * 0.8, cy), radius, radius)
            painter.drawEllipse(QPointF(cx + radius * 0.8, cy), radius, radius)
            painter.drawLine(
                QPointF(cx - radius * 0.8, cy - radius), QPointF(cx - radius * 0.8, cy - radius - 8)
            )
            painter.drawLine(
                QPointF(cx + radius * 0.8, cy - radius), QPointF(cx + radius * 0.8, cy - radius - 8)
            )
        elif symbol == "breaker":
            painter.drawRect(QRectF(cx - width * 0.18, cy - height * 0.2, width * 0.36, height * 0.4))
            painter.drawLine(
                QPointF(cx - width * 0.18, cy + height * 0.2),
                QPointF(cx + width * 0.2, cy - height * 0.34),
            )
        elif symbol == "bus":
            painter.setPen(QPen(color, 4.0))
            painter.drawLine(
                QPointF(cx - width * 0.36, cy), QPointF(cx + width * 0.36, cy)
            )
            painter.setPen(QPen(color, 1.6))
            for offset in (-0.24, 0.0, 0.24):
                painter.drawLine(
                    QPointF(cx + width * offset, cy), QPointF(cx + width * offset, cy + height * 0.3)
                )
        elif symbol == "inductor":
            # 电感：四个半圆串在一条线上
            radius = max(4.0, min(width * 0.09, 9.0))
            start = cx - radius * 2
            painter.drawLine(
                QPointF(cx - width * 0.32, cy), QPointF(start, cy)
            )
            painter.drawLine(
                QPointF(start + radius * 4, cy), QPointF(cx + width * 0.32, cy)
            )
            for index in range(4):
                painter.drawArc(
                    QRectF(start + index * radius * 2, cy - radius, radius * 2, radius * 2),
                    0,
                    180 * 16,
                )
        elif symbol == "capacitor":
            gap = 6.0
            painter.setPen(QPen(color, 3.0))
            painter.drawLine(
                QPointF(cx - gap, cy - height * 0.28), QPointF(cx - gap, cy + height * 0.28)
            )
            painter.drawLine(
                QPointF(cx + gap, cy - height * 0.28), QPointF(cx + gap, cy + height * 0.28)
            )
            painter.setPen(QPen(color, 1.6))
            painter.drawLine(QPointF(cx, cy + height * 0.28), QPointF(cx, cy + height * 0.45))
        elif symbol in ("rectifier", "inverter", "dcdc"):
            painter.drawRect(QRectF(cx - width * 0.26, cy - height * 0.26, width * 0.52, height * 0.52))
            size = min(width * 0.16, 10.0)
            if symbol == "rectifier":
                painter.drawLine(QPointF(cx - size, cy - size), QPointF(cx - size, cy + size))
                painter.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(cx + size * 0.2, cy - size),
                            QPointF(cx + size * 0.2, cy + size),
                            QPointF(cx + size * 1.4, cy),
                        ]
                    )
                )
            elif symbol == "inverter":
                painter.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(cx - size * 1.4, cy - size),
                            QPointF(cx - size * 1.4, cy + size),
                            QPointF(cx - size * 0.2, cy),
                        ]
                    )
                )
                painter.drawLine(QPointF(cx + size, cy - size), QPointF(cx + size, cy + size))
            else:  # dcdc：两个相对三角
                painter.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(cx - size * 1.3, cy - size),
                            QPointF(cx - size * 1.3, cy + size),
                            QPointF(cx - size * 0.2, cy),
                        ]
                    )
                )
                painter.drawPolygon(
                    QPolygonF(
                        [
                            QPointF(cx + size * 1.3, cy - size),
                            QPointF(cx + size * 1.3, cy + size),
                            QPointF(cx + size * 0.2, cy),
                        ]
                    )
                )
        elif symbol == "battery":
            # 电池：长短线交替（长线为正）
            painter.setPen(QPen(color, 2.2))
            count = 3
            step = height * 0.16
            top = cy - step * (count - 1) / 2
            for index in range(count):
                y = top + index * step
                painter.drawLine(
                    QPointF(cx - width * 0.16, y), QPointF(cx + width * 0.16, y)
                )
                painter.setPen(QPen(color, 1.4))
                painter.drawLine(
                    QPointF(cx - width * 0.07, y + step * 0.3),
                    QPointF(cx + width * 0.07, y + step * 0.3),
                )
                painter.setPen(QPen(color, 2.2))
        elif symbol == "pv":
            # 光伏：斜放的面板 + 几条栅线
            panel = QPolygonF(
                [
                    QPointF(cx - width * 0.22, cy - height * 0.2),
                    QPointF(cx + width * 0.24, cy - height * 0.26),
                    QPointF(cx + width * 0.22, cy + height * 0.2),
                    QPointF(cx - width * 0.24, cy + height * 0.26),
                ]
            )
            painter.drawPolygon(panel)
            painter.setPen(QPen(color, 1.0))
            for ratio in (0.33, 0.66):
                left = QPointF(cx - width * 0.23, cy - height * 0.2 + height * 0.4 * ratio)
                right = QPointF(cx + width * 0.23, cy - height * 0.26 + height * 0.46 * ratio)
                painter.drawLine(left, right)
            painter.drawLine(QPointF(cx, cy - height * 0.23), QPointF(cx, cy + height * 0.23))
        elif symbol == "switch":
            # 隔离开关：一静一动的刀片
            painter.drawEllipse(QPointF(cx - width * 0.2, cy + height * 0.2), 2.4, 2.4)
            painter.drawEllipse(QPointF(cx + width * 0.2, cy + height * 0.2), 2.4, 2.4)
            painter.setPen(QPen(color, 2.0))
            painter.drawLine(
                QPointF(cx - width * 0.2, cy + height * 0.2),
                QPointF(cx + width * 0.12, cy - height * 0.16),
            )
        elif symbol == "fuse":
            painter.drawRect(
                QRectF(cx - width * 0.24, cy - height * 0.16, width * 0.48, height * 0.32)
            )
            painter.setPen(QPen(color, 1.4))
            painter.drawLine(
                QPointF(cx - width * 0.24, cy), QPointF(cx + width * 0.24, cy)
            )
        elif symbol == "filter":
            # LC 梯形滤波
            painter.setPen(QPen(color, 1.6))
            painter.drawLine(QPointF(cx - width * 0.26, cy), QPointF(cx - width * 0.14, cy))
            for index in range(3):
                x = cx - width * 0.14 + index * width * 0.14
                painter.drawArc(
                    QRectF(x, cy - height * 0.12, width * 0.14, height * 0.24), 0, 180 * 16
                )
            painter.drawLine(QPointF(cx + width * 0.12, cy), QPointF(cx + width * 0.26, cy))
            painter.drawLine(
                QPointF(cx, cy + height * 0.12), QPointF(cx, cy + height * 0.26)
            )
            painter.drawLine(
                QPointF(cx - width * 0.1, cy + height * 0.26),
                QPointF(cx + width * 0.1, cy + height * 0.26),
            )
        elif symbol == "resistor":
            painter.drawRect(
                QRectF(cx - width * 0.22, cy - height * 0.1, width * 0.44, height * 0.2)
            )
            painter.setPen(QPen(color, 1.4))
            painter.drawLine(QPointF(cx - width * 0.3, cy), QPointF(cx - width * 0.22, cy))
            painter.drawLine(QPointF(cx + width * 0.22, cy), QPointF(cx + width * 0.3, cy))
        elif symbol == "motor":
            painter.drawEllipse(QPointF(cx, cy), width * 0.22, width * 0.22)
            font = QFont()
            font.setPointSize(9)
            font.setBold(True)
            painter.setFont(font)
            painter.drawText(
                QRectF(cx - width * 0.22, cy - 9, width * 0.44, 18), Qt.AlignCenter, "M"
            )
        elif symbol == "load":
            painter.drawRect(QRectF(cx - width * 0.16, cy - height * 0.26, width * 0.32, height * 0.52))
            painter.drawLine(QPointF(cx - width * 0.2, cy), QPointF(cx + width * 0.2, cy))
        elif symbol == "meter":
            painter.drawEllipse(QPointF(cx, cy), width * 0.22, width * 0.22)
            painter.drawLine(QPointF(cx, cy + width * 0.2), QPointF(cx + width * 0.14, cy - width * 0.12))
        elif symbol == "dsp":
            painter.drawRect(QRectF(cx - width * 0.2, cy - height * 0.24, width * 0.4, height * 0.48))
            painter.setPen(QPen(color, 1.2))
            for index in range(4):
                y = cy - height * 0.18 + index * height * 0.12
                painter.drawLine(QPointF(cx - width * 0.28, y), QPointF(cx - width * 0.2, y))
                painter.drawLine(QPointF(cx + width * 0.2, y), QPointF(cx + width * 0.28, y))
            font = QFont()
            font.setPointSize(7)
            painter.setFont(font)
            painter.drawText(
                QRectF(cx - width * 0.2, cy - 7, width * 0.4, 14), Qt.AlignCenter, "DSP"
            )
        else:  # text 或未知图元
            font = QFont()
            font.setPointSize(8)
            painter.setFont(font)
            painter.drawText(area, Qt.AlignCenter, symbol)


class OverviewPage(QWidget):
    """主页：示意图 + 简要说明。"""

    nodeActivated = Signal(str, str)

    def __init__(self, project: Project, base_dir: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.base_dir = base_dir

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.title = QLabel("")
        self.title.setStyleSheet(f"color: {theme.TEXT}; font-size: 14px; font-weight: 600;")
        bar.addWidget(self.title)
        bar.addStretch(1)
        hint = QLabel("滚轮缩放 · 按住左键拖动 · 点击带高亮的方框进入对应页面")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        bar.addWidget(hint)
        reset = QPushButton("复位视图")
        reset.clicked.connect(lambda: self.canvas.reset_view())
        bar.addWidget(reset)
        root.addLayout(bar)

        self.canvas = DiagramCanvas()
        self.canvas.nodeActivated.connect(self._on_node)
        root.addWidget(self.canvas, 1)

        self.legend = ElidedLabel("")
        self.legend.setStyleSheet(f"color: {theme.TEXT_DIM};")
        root.addWidget(self.legend)

        self.set_project(project, base_dir)

    def set_project(self, project: Project, base_dir: str = "") -> None:
        self.project = project
        if base_dir:
            self.base_dir = base_dir
        self.canvas.set_diagram(project.diagram, self.base_dir)
        self.title.setText(project.diagram.title or "主回路示意图")
        clickable = [
            n.label for n in project.diagram.nodes if n.action and n.action != "none"
        ]
        self.legend.setText(
            "线型：黄=交流、蓝=直流、灰虚线=信号"
            + (f"　｜　可点击：{'、'.join(clickable)}" if clickable else "")
        )

    def set_values(self, values: dict) -> None:
        payload = {}
        for pid, tag in values.items():
            payload[pid] = (tag.value, tag.quality)
        self.canvas.set_values(payload)

    def _on_node(self, action: str, target: str) -> None:
        self.nodeActivated.emit(action, target)
