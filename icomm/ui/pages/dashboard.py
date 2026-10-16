"""运行监视画面。

「变量配置即出画面」：仪表盘完全由工程里点位的 display 字段生成，
不需要单独设计画面文件。
    display = none   不出现在画面
    display = card   数值卡片（可写点位自带写入框）
    display = gauge  弧形仪表
    display = lamp   布尔指示灯
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
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

    def __init__(self, project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self._items: dict[str, QWidget] = {}
        self._badges: dict[str, StatusBadge] = {}

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

        for index, (device, point) in enumerate(entries):
            pid = f"{device.name}.{point.name}"
            widget = self._make_widget(pid, point)
            self._items[pid] = widget
            grid.addWidget(widget, index // COLUMNS, index % COLUMNS)

        for column in range(COLUMNS):
            grid.setColumnStretch(column, 1)
        return box

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
