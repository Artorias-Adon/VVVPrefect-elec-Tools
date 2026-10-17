"""右上角通知中心：报警、故障、功能结束都在这里提醒。

现场盯屏幕时最容易漏掉的是"刚才发生了什么"：某个量报警了、设备掉线了、
数据记录/录音/录波跑完存盘了。这里在菜单栏右上角（运行状态旁边）放一个铃铛：

* 有未读通知 → 右上角一个红点；
* **有重要（故障/报警）** 未读 → 红点**闪烁**，一眼能看见；
* 点铃铛 → 弹出小窗，列出最近的通知（时间 + 标题 + 一句话说明），
  点哪条就标记已读；面板里还能「全部已读」「清空」。

这里只管"收集 + 显示"，不判断业务：谁想通知就调 ``center.add(level, title, text)``。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme

#: 通知级别（越靠后越重要）
LEVELS = ("info", "warning", "critical")

#: 级别对应的颜色与文字标签
LEVEL_STYLE = {
    "info": (theme.ACCENT, "提示"),
    "warning": (theme.QUALITY_STALE, "报警"),
    "critical": (theme.QUALITY_BAD, "故障"),
}

#: 最多留多少条（内存里，不落盘）
MAX_NOTICES = 200

#: 弹出面板里最多显示多少条
POPUP_ROWS = 30


@dataclass
class Notice:
    """一条通知。"""

    level: str
    title: str
    text: str = ""
    at: float = field(default_factory=time.time)
    read: bool = False

    @property
    def important(self) -> bool:
        return self.level == "critical"

    @property
    def time_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.at))

    @property
    def level_text(self) -> str:
        return LEVEL_STYLE.get(self.level, LEVEL_STYLE["info"])[1]

    @property
    def color(self) -> str:
        return LEVEL_STYLE.get(self.level, LEVEL_STYLE["info"])[0]


class NotificationButton(QAbstractButton):
    """铃铛按钮：未读有红点，重要未读时红点闪烁。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(QSize(30, 26))
        self._unread = 0
        self._important = 0
        self._blink_on = True
        self._timer = QTimer(self)
        self._timer.setInterval(520)
        self._timer.timeout.connect(self._toggle_blink)
        self.setToolTip("通知")
        self.setAccessibleName("通知")

    # -------------------------------------------------- 状态

    def set_counts(self, unread: int, important: int) -> None:
        self._unread = max(0, int(unread))
        self._important = max(0, int(important))
        if self._important > 0:
            if not self._timer.isActive():
                self._blink_on = True
                self._timer.start()
        else:
            self._timer.stop()
            self._blink_on = True
        tip = "通知" if not self._unread else f"通知：{self._unread} 条未读"
        if self._important:
            tip += f"（其中 {self._important} 条故障/报警）"
        tip += "\n点击查看最近的通知"
        self.setToolTip(tip)
        self.update()

    def is_blinking(self) -> bool:
        return self._timer.isActive()

    def _toggle_blink(self) -> None:
        self._blink_on = not self._blink_on
        self.update()

    # -------------------------------------------------- 绘制

    def paintEvent(self, event) -> None:  # noqa: item2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())

        # 铃铛：默认暗色，有未读时用主题文字色，重要未读时按闪烁节奏变红
        color = QColor(theme.TEXT_DIM)
        if self._unread:
            color = QColor(theme.TEXT)
        if self._important and self._blink_on:
            color = QColor(theme.QUALITY_BAD)

        line_width = 1.5
        painter.setPen(QPen(color, line_width))
        painter.setBrush(Qt.NoBrush)

        # 铃身：上窄下宽的钟形，下面一条横边，再下面是铃舌
        width = rect.width()
        height = rect.height()
        centre = width / 2.0
        bottom = height - 7.0
        half = width * 0.22
        path = QPainterPath()
        path.moveTo(centre - half, bottom - 2.0)
        path.cubicTo(
            centre - half * 0.92, bottom - height * 0.34,
            centre - half * 0.62, bottom - height * 0.52,
            centre, bottom - height * 0.55,
        )
        path.cubicTo(
            centre + half * 0.62, bottom - height * 0.52,
            centre + half * 0.92, bottom - height * 0.34,
            centre + half, bottom - 2.0,
        )
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(
            QPointF(centre - half - 2.0, bottom - 2.0),
            QPointF(centre + half + 2.0, bottom - 2.0),
        )
        # 铃舌
        painter.setBrush(color)
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(centre, bottom + 1.6), 1.7, 1.7)
        painter.setBrush(Qt.NoBrush)
        # 顶上的小挂钩
        painter.setPen(QPen(color, 1.2))
        painter.drawArc(
            QRectF(centre - 2.6, bottom - height * 0.62, 5.2, 5.2), 0, 180 * 16
        )

        if self._unread:
            radius = 4.0 if self._important and self._blink_on else 3.4
            if self._important and not self._blink_on:
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor(theme.QUALITY_BAD), 1.2))
            else:
                painter.setBrush(QColor(theme.QUALITY_BAD))
                painter.setPen(Qt.NoPen)
            dot = QPointF(rect.right() - 5.0, rect.top() + 5.0)
            painter.drawEllipse(dot, radius, radius)


class NotificationPopup(QFrame):
    """点铃铛弹出来的小面板：最近的通知列表 + 全部已读 / 清空。"""

    def __init__(self, center: "NotificationCenter") -> None:
        super().__init__(center.window(), Qt.Popup | Qt.FramelessWindowHint)
        self.center = center
        self.setObjectName("noticePopup")
        self.setStyleSheet(
            f"#noticePopup {{ background: {theme.BG_PANEL};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
        )
        self.setFixedWidth(380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("通知")
        title.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
        header.addWidget(title)
        self.count_label = QLabel("")
        self.count_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        header.addWidget(self.count_label)
        header.addStretch(1)
        read_all = QPushButton("全部已读")
        read_all.setFlat(True)
        read_all.clicked.connect(self.center.mark_all_read)
        header.addWidget(read_all)
        clear = QPushButton("清空")
        clear.setFlat(True)
        clear.clicked.connect(self.center.clear)
        header.addWidget(clear)
        layout.addLayout(header)

        self.list = QListWidget()
        self.list.setObjectName("noticeList")
        self.list.setMaximumHeight(320)
        self.list.itemClicked.connect(self._on_item_clicked)
        layout.addWidget(self.list, 1)

        self.empty = QLabel("还没有通知。报警、设备掉线、数据记录/录音/录波结束时会在这里提示。")
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(self.empty)

        self.refresh()

    def refresh(self) -> None:
        notices = self.center.notices(POPUP_ROWS)
        self.list.clear()
        for notice in notices:
            item = QListWidgetItem(f"{notice.time_text}　{notice.title}\n{notice.text or notice.level_text}")
            item.setData(Qt.UserRole, id(notice))
            font = QFont()
            font.setBold(not notice.read)
            item.setFont(font)
            item.setForeground(QColor(notice.color))
            item.setToolTip(notice.text or notice.title)
            self.list.addItem(item)
        self.empty.setVisible(not notices)
        self.count_label.setText(
            f"（{self.center.unread_count()} 条未读）" if self.center.unread_count() else ""
        )
        self.adjustSize()

    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        self.center.mark_read(id_of=item.data(Qt.UserRole))
        self.refresh()

    def show_below(self, widget: QWidget) -> None:
        """贴着按钮显示：右对齐、在按钮下方。"""
        self.refresh()
        corner = widget.mapToGlobal(QPoint(widget.width(), widget.height()))
        width = self.width()
        self.move(corner.x() - width, corner.y() + 2)
        self.show()


class NotificationCenter(QWidget):
    """通知中心：内存里存最近的若干条，按钮 + 弹出面板。"""

    #: 通知有变化（新增 / 已读 / 清空）
    changed = Signal()

    def __init__(self, parent: QWidget | None = None, limit: int = MAX_NOTICES) -> None:
        super().__init__(parent)
        self._notices: list[Notice] = []
        self._limit = max(10, int(limit))

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.button = NotificationButton(self)
        self.button.clicked.connect(self.toggle_popup)
        layout.addWidget(self.button)

        self._popup: NotificationPopup | None = None
        self.changed.connect(self._refresh)

    # -------------------------------------------------- 数据

    def notices(self, limit: int | None = None) -> list[Notice]:
        """最近的若干条（新的在前）。"""
        return self._notices if limit is None else self._notices[:limit]

    def unread_count(self) -> int:
        return sum(1 for notice in self._notices if not notice.read)

    def important_count(self) -> int:
        """未读里"故障/报警"级的条数（决定要不要闪）。"""
        return sum(1 for notice in self._notices if not notice.read and notice.important)

    def add(self, level: str, title: str, text: str = "") -> Notice:
        """加一条通知（级别不在 LEVELS 里按 info 处理）。"""
        notice = Notice(
            level=level if level in LEVELS else "info",
            title=str(title),
            text=str(text or ""),
        )
        self._notices.insert(0, notice)
        del self._notices[self._limit :]
        self.changed.emit()
        return notice

    def mark_read(self, id_of: int | None = None, notice: Notice | None = None) -> None:
        if notice is None and id_of is not None:
            notice = next((item for item in self._notices if id(item) == id_of), None)
        if notice is None or notice.read:
            return
        notice.read = True
        self.changed.emit()

    def mark_all_read(self) -> None:
        changed = False
        for notice in self._notices:
            if not notice.read:
                notice.read = True
                changed = True
        if changed:
            self.changed.emit()

    def clear(self) -> None:
        if not self._notices:
            return
        self._notices.clear()
        self.changed.emit()

    # -------------------------------------------------- 面板

    def _refresh(self) -> None:
        self.button.set_counts(self.unread_count(), self.important_count())
        if self._popup is not None and self._popup.isVisible():
            self._popup.refresh()

    def popup(self) -> NotificationPopup:
        if self._popup is None:
            self._popup = NotificationPopup(self)
        return self._popup

    def toggle_popup(self) -> None:
        panel = self.popup()
        if panel.isVisible():
            panel.hide()
            return
        panel.show_below(self.button)
