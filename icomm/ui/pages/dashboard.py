"""运行监视画面。

「变量配置即出画面」：仪表盘完全由工程里点位的 display 字段生成，
不需要单独设计画面文件。
    display = none   不出现在画面
    display = card   数值卡片（可写点位自带写入框）
    display = gauge  弧形仪表
    display = lamp   布尔指示灯
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt, Signal
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

#: 每个分组内的列数
COLUMNS = 4


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

    def __init__(self, project, order: dict | None = None,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self._items: dict[str, QWidget] = {}
        self._badges: dict[str, StatusBadge] = {}
        #: 分组名 -> 点号顺序（用户用中键拖出来的，存在软件设置里）
        self._order: dict[str, list[str]] = order if order is not None else {}
        #: 每个分组当前的显示顺序（拖动时直接改这个再重排）
        self._group_entries: dict[str, list[str]] = {}
        self._groups: dict[str, QGroupBox] = {}
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
        self.container = QWidget()
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(14, 12, 14, 12)
        self.container_layout.setSpacing(12)
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
            self.container_layout.addWidget(hint)
            self.container_layout.addStretch(1)
            return

        for group_name, entries in groups.items():
            self.container_layout.addWidget(self._build_group(group_name, entries))
        self.container_layout.addStretch(1)

    def _build_group(self, name: str, entries: list) -> QGroupBox:
        box = QGroupBox(name)
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

        self._fill_grid(grid, [widgets[pid] for pid in order])
        for column in range(COLUMNS):
            grid.setColumnStretch(column, 1)
        return box

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
    def _fill_grid(grid: QGridLayout, widgets: list[QWidget]) -> None:
        """把控件按顺序重新摆进网格（重新 addWidget 就会挪位置）。"""
        for index, widget in enumerate(widgets):
            grid.addWidget(widget, index // COLUMNS, index % COLUMNS)

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
        self._fill_grid(self._groups[group].layout(), [self._items[p] for p in order])
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

class _CardDragFilter(QObject):
    """把卡片上的中键拖动接到运行监视页上。

    卡片自己会处理鼠标按下（右键菜单），所以用事件过滤器先拦一道；
    中键事件一律吃掉，免得漏给卡片当成别的操作。
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
