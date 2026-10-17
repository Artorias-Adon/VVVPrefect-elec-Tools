"""运行监视画面。

「变量配置即出画面」：仪表盘完全由工程里点位的 display 字段生成，
不需要单独设计画面文件。
    display = none   不出现在画面
    display = card   数值卡片（可写点位自带写入框）
    display = gauge  弧形仪表
    display = lamp   布尔指示灯
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..widgets import GaugeWidget, LampWidget, StatusBadge, ValueCard, WaveCard

#: 每个分组内的列数（独占一行时）
COLUMNS = 4
#: 一行最多并排放几个分组
MAX_GROUPS_PER_ROW = 2
#: 拖动分组时携带的数据类型（内容是分组名）
GROUP_MIME = "application/x-vvphmi-group"


class _GroupBoard(QWidget):
    """分组容器：接住"把分组拖进来"的拖动，落点是左/右/上/下四种。"""

    def __init__(self, page: "DashboardPage") -> None:
        super().__init__()
        self.page = page
        self.setAcceptDrops(True)
        self._highlight: QGroupBox | None = None
        self._zone = ""

    # -------------------------------------------------- 落点

    def group_box_at(self, pos: QPoint) -> QGroupBox | None:
        widget = self.childAt(pos)
        while widget is not None and widget is not self:
            if isinstance(widget, QGroupBox):
                return widget
            widget = widget.parentWidget()
        return None

    @staticmethod
    def zone_for(box: QGroupBox, pos: QPoint) -> str:
        """鼠标在目标分组的哪一侧：left / right / above / below。"""
        local = box.mapFromParent(pos) if box.parentWidget() is not None else pos
        if local.x() < box.width() * 0.3:
            return "left"
        if local.x() > box.width() * 0.7:
            return "right"
        return "above" if local.y() < box.height() * 0.5 else "below"

    @staticmethod
    def _group_name(mime: QMimeData | None) -> str:
        if mime is None or not mime.hasFormat(GROUP_MIME):
            return ""
        try:
            return bytes(mime.data(GROUP_MIME)).decode("utf-8").strip()
        except (UnicodeDecodeError, ValueError):
            return ""

    def _set_highlight(self, box: QGroupBox | None, zone: str) -> None:
        if self._highlight is box and self._zone == zone:
            return
        self._clear_highlight()
        self._highlight, self._zone = box, zone
        if box is None:
            return
        # 用描边提示落点：左右是压边，上下是上/下边框
        edges = {
            "left": f"border-left: 3px solid {theme.ACCENT};",
            "right": f"border-right: 3px solid {theme.ACCENT};",
            "above": f"border-top: 3px solid {theme.ACCENT};",
            "below": f"border-bottom: 3px solid {theme.ACCENT};",
        }
        box.setStyleSheet(
            f"QGroupBox {{ {edges.get(zone, '')} background: {theme.BG_PANEL}; }}"
        )

    def _clear_highlight(self) -> None:
        if self._highlight is not None:
            try:
                self._highlight.setStyleSheet("")
            except RuntimeError:
                pass
        self._highlight, self._zone = None, ""

    # -------------------------------------------------- 拖动事件

    def dragEnterEvent(self, event) -> None:  # noqa: item2
        if self._group_name(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: item2
        name = self._group_name(event.mimeData())
        if not name:
            super().dragMoveEvent(event)
            return
        pos = event.position().toPoint()
        target = self.group_box_at(pos)
        if target is None or target is self.page._groups.get(name):
            self._clear_highlight()
            event.ignore()
            return
        self._set_highlight(target, self.zone_for(target, pos))
        event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:  # noqa: item2
        self._clear_highlight()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: item2
        name = self._group_name(event.mimeData())
        pos = event.position().toPoint()
        target = self.group_box_at(pos)
        zone = self.zone_for(target, pos) if target is not None else ""
        self._clear_highlight()
        if not name or target is None:
            event.ignore()
            return
        event.acceptProposedAction()
        self.page.move_group(name, self.page._group_name_of(target), zone)


class _GroupDragFilter(QObject):
    """按住分组标题 / 边框就能把整组拖走（拖到别的分组左/右/上/下）。"""

    def __init__(self, page: "DashboardPage", box: QGroupBox) -> None:
        super().__init__(box)
        self.page = page
        self.box = box
        self._press: QPoint | None = None

    @staticmethod
    def is_handle(box: QGroupBox, pos: QPoint, margin: int = 10, title: int = 26) -> bool:
        return (
            pos.x() <= margin
            or pos.y() <= title
            or pos.x() >= box.width() - margin
            or pos.y() >= box.height() - margin
        )

    def eventFilter(self, obj, event) -> bool:  # noqa: item2
        kind = event.type()
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            self._press = pos if self.is_handle(self.box, pos) else None
            return False
        if kind == QEvent.MouseMove and self._press is not None:
            if not (event.buttons() & Qt.LeftButton):
                self._press = None
                return False
            if (event.position().toPoint() - self._press).manhattanLength() < 8:
                return False
            self._press = None
            drag = QDrag(self.box)
            mime = QMimeData()
            mime.setData(GROUP_MIME, self.page._group_name_of(self.box).encode("utf-8"))
            drag.setMimeData(mime)
            drag.setPixmap(self.box.grab().scaledToWidth(max(120, self.box.width() // 3)))
            drag.exec(Qt.MoveAction)
            return True
        if kind == QEvent.MouseButtonRelease:
            self._press = None
        return False


class DashboardPage(QWidget):
    """固定布局仪表盘（按分组自动排布）。"""

    writeRequested = Signal(str, object)
    #: 双击波形卡片 -> 希望到示波器里看这段波形
    waveActivated = Signal(str)
    #: 双击点位控件 -> 打开点位参数对话框
    editRequested = Signal(str)
    #: 在点位控件上右键 -> 弹出操作菜单
    contextRequested = Signal(str, object)
    #: 中键拖动调整顺序（分组名, 新的点号顺序）
    orderChanged = Signal(str, list)
    #: 分组整体位置变了（新的「行 -> 分组名」列表，一行最多并排两个）
    groupLayoutChanged = Signal(list)

    def __init__(self, project, order: dict | None = None,
                 group_rows: list | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self._items: dict[str, QWidget] = {}
        self._badges: dict[str, StatusBadge] = {}
        #: 分组名 -> 点号顺序（用户用中键拖出来的，存在软件设置里）
        self._order: dict[str, list[str]] = order if order is not None else {}
        #: 分组整体怎么摆：[[分组名, ...], ...]，一行最多两个（并排）
        self._group_rows: list[list[str]] = [
            [str(name) for name in row] for row in (group_rows or []) if row
        ]
        #: 每个分组当前的显示顺序（拖动时直接改这个再重排）
        self._group_entries: dict[str, list[str]] = {}
        self._groups: dict[str, QGroupBox] = {}
        #: 每个分组内部用几列（独占一行 4 列，并排时减半）
        self._group_columns: dict[str, int] = {}
        self._drag_pid: str | None = None
        #: 中键拖动：在卡片上装事件过滤器（卡片自己会吃掉按下事件）
        self._drag_filter = _CardDragFilter(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 顶部设备状态条
        self.status_bar_widget = QWidget()
        self.status_bar_widget.setStyleSheet(
            f"background: {theme.BG_PANEL}; border-bottom: 1px solid {theme.BORDER};"
        )
        self.status_layout = QHBoxLayout(self.status_bar_widget)
        self.status_layout.setContentsMargins(12, 6, 12, 6)
        self.status_layout.setSpacing(18)
        outer.addWidget(self.status_bar_widget)

        # 滚动区
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = _GroupBoard(self)
        self.container.setObjectName("dashboardBoard")
        self.container_layout = QGridLayout(self.container)
        self.container_layout.setContentsMargins(14, 12, 14, 12)
        self.container_layout.setHorizontalSpacing(12)
        self.container_layout.setVerticalSpacing(12)
        self.container_layout.setColumnStretch(0, 1)
        self.container_layout.setColumnStretch(1, 1)
        self.scroll.setWidget(self.container)
        outer.addWidget(self.scroll, 1)

        self.rebuild(project)

    # -------------------------------------------------- 构建

    def rebuild(self, project) -> None:
        self.project = project
        self._items.clear()
        self._badges.clear()
        self._group_entries.clear()
        self._groups.clear()
        self._group_columns.clear()
        self._drag_pid = None

        while self.container_layout.count():
            item = self.container_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        while self.status_layout.count():
            item = self.status_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for device in project.devices:
            badge = StatusBadge(device.name)
            self._badges[device.name] = badge
            self.status_layout.addWidget(badge)
        self.status_layout.addStretch(1)

        groups: dict[str, list] = {}
        for device, point in project.display_points():
            groups.setdefault(point.group or "默认", []).append((device, point))

        if not groups:
            hint = QLabel("当前工程没有任何点位的 display 字段被设为 card / gauge / lamp，\n"
                          "请到「设备与点位」页面配置后重新生成画面。")
            hint.setAlignment(Qt.AlignCenter)
            hint.setStyleSheet(f"color: {theme.TEXT_DIM}; padding: 40px;")
            self.container_layout.addWidget(hint, 0, 0, 1, MAX_GROUPS_PER_ROW)
            self._group_rows = []
            return

        for group_name, entries in groups.items():
            self._build_group(group_name, entries)
        self._normalize_group_rows(list(groups))
        self._apply_group_rows()

    def _build_group(self, name: str, entries: list) -> QGroupBox:
        box = QGroupBox(name)
        box.setToolTip(
            "按住这一组的标题或边框拖动，可以把它挪到别的分组左/右/上/下 ——\n"
            "松手在另一组的左半边就是并排到它左边（比如状态量在左、控制量在右）。"
        )
        grid = QGridLayout(box)
        grid.setContentsMargins(12, 16, 12, 12)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        order = self._sorted_entries(name, entries)
        self._group_entries[name] = order
        self._groups[name] = box
        widgets: dict[str, QWidget] = {}
        for device, point in entries:
            pid = f"{device.name}.{point.name}"
            widget = self._make_widget(pid, point)
            self._items[pid] = widget
            widgets[pid] = widget
            # 中键拖动要在卡片自己处理鼠标事件之前拦下来
            widget.installEventFilter(self._drag_filter)

        self._fill_grid(grid, [widgets[pid] for pid in order], COLUMNS)
        for column in range(COLUMNS):
            grid.setColumnStretch(column, 1)
        self._group_columns[name] = COLUMNS
        # 按住标题/边框把整组拖走
        box.installEventFilter(_GroupDragFilter(self, box))
        return box

    # -------------------------------------------------- 分组位置（拖整组）

    def _group_name_of(self, box: QGroupBox) -> str:
        for name, candidate in self._groups.items():
            if candidate is box:
                return name
        return ""

    def group_rows(self) -> list[list[str]]:
        """当前分组怎么摆（一行一个列表，最多两个并排）。"""
        return [list(row) for row in self._group_rows]

    def _normalize_group_rows(self, names: list[str]) -> None:
        """把记住的分组位置收敛到"只含现有分组、一行最多两个、不重复"。"""
        valid = set(names)
        rows: list[list[str]] = []
        seen: set[str] = set()
        for row in self._group_rows:
            kept = [name for name in row if name in valid and name not in seen]
            kept = kept[:MAX_GROUPS_PER_ROW]
            for name in kept:
                seen.add(name)
            if kept:
                rows.append(kept)
        for name in names:
            if name not in seen:
                rows.append([name])
                seen.add(name)
        self._group_rows = rows

    def _apply_group_rows(self) -> None:
        """按 ``_group_rows`` 重新摆分组：独占一行占两列，并排各占一列。"""
        for box in self._groups.values():
            self.container_layout.removeWidget(box)
        while self.container_layout.count():
            item = self.container_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        for row_index, row in enumerate(self._group_rows):
            span = max(1, MAX_GROUPS_PER_ROW // max(1, len(row)))
            columns = COLUMNS if len(row) == 1 else max(1, COLUMNS // 2)
            for column, name in enumerate(row):
                box = self._groups.get(name)
                if box is None:
                    continue
                self.container_layout.addWidget(
                    box, row_index, column * span, 1, span
                )
                self._set_group_columns(name, columns)
        # 底部留白，别让最后一行被拉满整屏
        self.container_layout.setRowStretch(len(self._group_rows), 1)

    def _set_group_columns(self, name: str, columns: int) -> None:
        if self._group_columns.get(name) == columns:
            return
        box = self._groups.get(name)
        if box is None:
            return
        self._group_columns[name] = columns
        grid = box.layout()
        if not isinstance(grid, QGridLayout):
            return
        pids = self._group_entries.get(name) or []
        self._fill_grid(grid, [self._items[pid] for pid in pids if pid in self._items], columns)
        for column in range(COLUMNS):
            grid.setColumnStretch(column, 1 if column < columns else 0)

    def move_group(self, name: str, target: str, zone: str) -> bool:
        """把整个分组挪到 ``target`` 的某一侧。

        ``zone``：``left`` / ``right`` 并排（一行最多两个），``above`` / ``below`` 单独占一行。
        返回位置是否真的变了。
        """
        if not name or not target or name == target:
            return False
        if name not in self._groups or target not in self._groups:
            return False

        rows = [[item for item in row if item != name] for row in self._group_rows]
        rows = [row for row in rows if row]
        index = next((i for i, row in enumerate(rows) if target in row), None)
        if index is None:
            return False

        if zone in ("left", "right"):
            row = rows[index]
            row.insert(0 if zone == "left" else len(row), name)
            if len(row) > MAX_GROUPS_PER_ROW:
                # 满了就把被挤出来的那个挪到下一行，别丢
                overflow = row.pop(MAX_GROUPS_PER_ROW)
                rows.insert(index + 1, [overflow])
        else:
            rows.insert(index if zone == "above" else index + 1, [name])

        if rows == self._group_rows:
            return False
        self._group_rows = rows
        self._apply_group_rows()
        self.groupLayoutChanged.emit(self.group_rows())
        return True

    # -------------------------------------------------- 顺序（中键拖动）

    def _sorted_entries(self, name: str, entries: list) -> list[str]:
        """按用户拖出来的顺序排点号；没记过的排在后面（保持工程里的原顺序）。"""
        pids = [f"{device.name}.{point.name}" for device, point in entries]
        wanted = self._order.get(name) or []
        seen = {pid for pid in wanted}
        head = [pid for pid in wanted if pid in pids]
        tail = [pid for pid in pids if pid not in seen]
        return head + tail

    @staticmethod
    def _fill_grid(grid: QGridLayout, widgets: list[QWidget], columns: int = COLUMNS) -> None:
        """把控件按顺序重新摆进网格（重新 addWidget 就会挪位置）。"""
        columns = max(1, columns)
        for index, widget in enumerate(widgets):
            grid.addWidget(widget, index // columns, index % columns)

    def group_order(self, name: str) -> list[str]:
        """某个分组当前的显示顺序（点号列表）。"""
        return list(self._group_entries.get(name, []))

    def group_of(self, pid: str) -> str:
        for name, pids in self._group_entries.items():
            if pid in pids:
                return name
        return ""

    def move_pid(self, pid: str, target_pid: str) -> bool:
        """把 pid 挪到 target_pid 的位置（只在同一分组内生效）。

        ``pid`` 是拖起来的那块，``target_pid`` 是松手时鼠标下面那块；
        返回顺序是否真的变了。
        """
        if not pid or not target_pid or pid == target_pid:
            return False
        group = self.group_of(pid)
        if not group or group != self.group_of(target_pid):
            return False
        order = self._group_entries[group]
        if pid not in order or target_pid not in order:
            return False
        # 先记住落点的下标再抽走自己：这样"拖到哪一格就占哪一格"，
        # 中间那些块整体串位（在网格上就是被拖的那块顶了目标的位置）
        target_index = order.index(target_pid)
        order.remove(pid)
        order.insert(target_index, pid)
        self._order[group] = list(order)
        self._fill_grid(
            self._groups[group].layout(),
            [self._items[p] for p in order],
            self._group_columns.get(group, COLUMNS),
        )
        self.orderChanged.emit(group, list(order))
        return True

    def reset_order(self) -> None:
        """清掉拖出来的顺序，恢复工程里的默认排列。"""
        self._order.clear()
        self.rebuild(self.project)

    def _pid_at(self, global_pos) -> str:
        """全局坐标下面那一块是哪个点位（拖动时找落点用）。"""
        for pid, widget in self._items.items():
            if widget.isVisible() and widget.rect().contains(widget.mapFromGlobal(global_pos)):
                return pid
        return ""

    def _make_widget(self, pid: str, point) -> QWidget:
        if point.display == "gauge" and not point.is_array:
            widget = GaugeWidget(pid, point)
        elif point.display == "lamp" and not point.is_array:
            widget = LampWidget(pid, point)
        elif point.is_array or point.display == "wave":
            card = WaveCard(pid, point)
            card.doubleClicked.connect(self.waveActivated.emit)
            widget = card
        else:
            card = ValueCard(pid, point)
            card.writeRequested.connect(self.writeRequested.emit)
            widget = card
        # 所有点位控件都支持：双击打开参数对话框、右键弹操作菜单
        # （含「添加到示波器」，所以任意点位都能进示波器，不限于标了示波器=是的）
        widget.editRequested.connect(self.editRequested.emit)
        widget.contextRequested.connect(self.contextRequested.emit)
        return widget

    # -------------------------------------------------- 数据刷新

    def _pid_for_widget(self, widget) -> str:
        for pid, item in self._items.items():
            if item is widget:
                return pid
        return ""

    def on_tags(self, values: dict) -> None:
        for pid, tag in values.items():
            widget = self._items.get(pid)
            if widget is None:
                continue
            widget.set_value(tag.value, tag.quality, tag.error)

    def on_device_state(self, status) -> None:
        badge = self._badges.get(status.name)
        if badge is not None:
            badge.set_status(status.connected, status.state, status.error)

    def fill_from_table(self, tag_table) -> None:
        """启动前用实时表里的缓存值刷新一次。"""
        snapshot = tag_table.snapshot()
        if snapshot:
            self.on_tags(snapshot)


class _CardDragFilter(QObject):
    """把卡片上的中键拖动接到运行监视页上。

    卡片自己会处理鼠标按下（右键菜单），所以用事件过滤器先拦一道；
    中键事件一律吃掉，免得漏给卡片当成别的操作。

    注意：这个类必须放在 ``DashboardPage`` **之后**，否则会把页面的
    后半段方法（on_tags / on_device_state / fill_from_table）吞进自己的类体里。
    """

    def __init__(self, page: "DashboardPage") -> None:
        super().__init__(page)
        self.page = page

    def eventFilter(self, obj, event) -> bool:  # noqa: item2
        kind = event.type()
        if kind == QEvent.MouseButtonPress and event.button() == Qt.MiddleButton:
            pid = self.page._pid_for_widget(obj)
            if pid:
                self.page._drag_pid = pid
                obj.setCursor(Qt.ClosedHandCursor)
                return True
            return False
        if kind == QEvent.MouseMove and self.page._drag_pid is not None:
            if event.buttons() & Qt.MiddleButton:
                target = self.page._pid_at(event.globalPosition().toPoint())
                if target:
                    self.page.move_pid(self.page._drag_pid, target)
                return True
            return False
        if kind == QEvent.MouseButtonRelease and event.button() == Qt.MiddleButton:
            if self.page._drag_pid is not None:
                self.page._drag_pid = None
                obj.setCursor(Qt.ArrowCursor)
                return True
            return False
        return False
