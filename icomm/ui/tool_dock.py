"""把浮动工具窗口「钉」进左侧栏（导航栏）当页面用。

复杂一点的工具（记事本、分析工具、故障录波……）开成小窗口时，跟数据页面来回切很麻烦。
这里给每个非模态工具窗口顶上挂一条**拖动条**，按住拖到左边「主页 / 运行监视」那一栏，
松手就把它变成那一栏里的一个页面，之后点导航就能直接看。

实现分两块：

* :class:`ToolPageGrip` —— 窗口顶上那条"⠿ 拖到左侧栏"，负责发起拖动
  （拖不动也能直接点右边的「钉住」按钮）；
* :class:`NavList` —— 左侧导航栏，接受这种拖动，拖进来时高亮，松手发信号。

MIME 里只放工具名（字符串），不跨进程，所以不用担心序列化安全问题。
"""

from __future__ import annotations

from PySide6.QtCore import QMimeData, QPoint, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import theme

#: 拖动时携带的数据类型（内容是工具名）
TOOL_MIME = "application/x-vvphmi-tool-page"

#: 导航栏里工具页的编号前缀（只是显示用，不影响数据）
TOOL_PAGE_PREFIX = "🔧 "


class ToolPageGrip(QFrame):
    """工具窗口顶上那条「拖到左侧栏」。

    按住条本身拖动即可；右侧还有个「钉住」按钮，不想拖的时候一点就行。
    """

    #: 用户点了「钉住」（参数是工具名）
    pinRequested = Signal(str)

    def __init__(self, tool_name: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tool_name = tool_name
        self._press_pos: QPoint | None = None
        self.setObjectName("toolPageGrip")
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip(
            "按住这条拖动，松手在左侧栏（主页 / 运行监视 那一栏）就钉成一个页面；\n"
            "也可以直接点右边的「钉住」。"
        )
        self.setStyleSheet(
            f"#toolPageGrip {{ background: {theme.BG_INPUT};"
            f" border: 1px solid {theme.BORDER}; border-radius: 4px; }}"
        )

        row = QHBoxLayout(self)
        row.setContentsMargins(8, 2, 4, 2)
        row.setSpacing(6)

        hint = QLabel("⠿ 拖到左侧栏可钉成页面")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        row.addWidget(hint)
        row.addStretch(1)

        pin = QPushButton("钉住")
        pin.setToolTip("把这个工具放到左侧栏，跟「主页 / 运行监视」一样点着看")
        pin.setCursor(Qt.PointingHandCursor)
        pin.setFlat(True)
        pin.clicked.connect(lambda: self.pinRequested.emit(self.tool_name))
        row.addWidget(pin)

    # -------------------------------------------------- 拖动

    def mousePressEvent(self, event) -> None:  # noqa: item2
        if event.button() == Qt.LeftButton:
            self._press_pos = event.position().toPoint()
            self.setCursor(Qt.ClosedHandCursor)
        else:
            self._press_pos = None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: item2
        self._press_pos = None
        self.setCursor(Qt.OpenHandCursor)
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: item2
        if self._press_pos is None or not (event.buttons() & Qt.LeftButton):
            return
        moved = (event.position().toPoint() - self._press_pos).manhattanLength()
        if moved < 8:
            return
        self._press_pos = None
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(TOOL_MIME, self.tool_name.encode("utf-8"))
        mime.setText(self.tool_name)
        drag.setMimeData(mime)
        drag.setPixmap(self.grab())
        drag.exec(Qt.LinkAction)


def install_page_grip(window: QWidget, tool_name: str, *, on_pin=None) -> ToolPageGrip | None:
    """给工具窗口顶上装一条拖动条（根布局不是竖向就不装，返回 None）。

    ``on_pin`` 收到点击「钉住」时的工具名；不给就把信号留在返回对象上。
    """
    layout = window.layout()
    if not isinstance(layout, QVBoxLayout):
        return None
    for index in range(layout.count()):
        existing = layout.itemAt(index).widget()
        if isinstance(existing, ToolPageGrip):
            return existing  # 已经装过，别重复
    grip = ToolPageGrip(tool_name, window)
    if on_pin is not None:
        grip.pinRequested.connect(on_pin)
    layout.insertWidget(0, grip)
    return grip


class NavList(QListWidget):
    """左侧导航栏：除点选页面外，还接受把工具拖进来钉成页面。"""

    #: 有工具被拖进来（参数是工具名）
    toolDropped = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DropOnly)
        self._highlight = False

    # -------------------------------------------------- 拖动判定

    @staticmethod
    def tool_name_from(mime: QMimeData | None) -> str:
        """从拖动数据里取工具名（不是我们的类型就返回空串）。"""
        if mime is None or not mime.hasFormat(TOOL_MIME):
            return ""
        try:
            return bytes(mime.data(TOOL_MIME)).decode("utf-8").strip()
        except (UnicodeDecodeError, ValueError):
            return ""

    def _set_highlight(self, on: bool) -> None:
        if self._highlight == on:
            return
        self._highlight = on
        self.setStyleSheet(
            f"#nav {{ border: 2px solid {theme.ACCENT}; }}" if on else ""
        )

    # -------------------------------------------------- 事件

    def dragEnterEvent(self, event) -> None:  # noqa: item2
        if self.tool_name_from(event.mimeData()):
            self._set_highlight(True)
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: item2
        if self.tool_name_from(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event) -> None:  # noqa: item2
        self._set_highlight(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: item2
        name = self.tool_name_from(event.mimeData())
        self._set_highlight(False)
        if name:
            event.acceptProposedAction()
            self.toolDropped.emit(name)
            return
        super().dropEvent(event)
