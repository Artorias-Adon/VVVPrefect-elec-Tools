"""示波器页面。

左侧管通道，右侧是画布，底部是录波历史（从数据库回看之前录下的波形）。

两种数据来源，按点位类型自动切换：
* **数组/块点位** -> 录波模式：画整段波形，带触发电平（波形才稳得住），时基由点数和采样间隔决定；
* **标量点位** -> 趋势模式：按"秒/格"滚动，用来看有效值、温度这类低频量。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.models import Project, point_id
from .. import theme
from ..scope_canvas import ScopeCanvas, ScopeChannel
from ..widgets import ElidedLabel

TIMEBASES = [("1 秒/格", 1.0), ("2 秒/格", 2.0), ("5 秒/格", 5.0),
             ("10 秒/格", 10.0), ("30 秒/格", 30.0), ("2 分/格", 120.0), ("5 分/格", 300.0)]
#: 下拉里只写短名，括号里的说明放 tooltip —— 下拉越宽，整页最小宽度跟着涨
#: 趋势模式默认选中的档位下标（2 秒/格）
TREND_DEFAULT_INDEX = 1
#: 录波模式的时基档位：横向一格铺开几个采样点（1 点/格最短，一个点一格）
ARRAY_POINTS_PER_DIV = (1, 2, 5, 10, 20, 50, 100)

TRIGGER_MODES = [("自动", "auto"), ("正常", "normal"), ("单次", "single")]
TRIGGER_MODE_TIPS = {
    "auto": "自动：不触发也一直画（默认）",
    "normal": "正常：等触发条件满足才画",
    "single": "单次：抓到一次就停住，适合截一次波形",
}
TRIGGER_EDGES = [("上升沿", "rising"), ("下降沿", "falling")]


class ScopePage(QWidget):
    """示波器 / 趋势分析。"""

    def __init__(self, project: Project, storage=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.storage = storage
        self._channels: dict[str, ScopeChannel] = {}
        self._frozen = False  # 单次触发后冻结显示
        #: 通道列表是否列出工程里的全部点位（否则只列标了"示波器=是"的）
        self._show_all = False
        self._preferences = None
        #: 用户正在看历史录波时暂停自动刷新，免得列表在眼皮底下跳
        self._history_paused = False
        #: 时基下拉是按哪套档位填的（模式/点数/采样间隔），变了才重建
        self._timebase_signature = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # ---------------------------------------------- 左：通道列表
        left = QGroupBox("通道")
        left_layout = QVBoxLayout(left)
        self.show_all_box = QCheckBox("显示全部点位")
        self.show_all_box.setToolTip(
            "勾上后列出工程里所有点位（含只读遥测）；\n"
            "不勾则只列参数表里标了「示波器=是」的点位"
        )
        self.show_all_box.toggled.connect(self._on_show_all_toggled)
        left_layout.addWidget(self.show_all_box)

        self.channel_list = QListWidget()
        self.channel_list.itemChanged.connect(self._on_channel_toggled)
        left_layout.addWidget(self.channel_list, 1)

        row = QHBoxLayout()
        row.setSpacing(4)
        for text, slot in (
            ("全选", lambda: self._set_all(True)),
            ("全不选", lambda: self._set_all(False)),
            ("仅波形", lambda: self._select_kind("array")),
            ("仅标量", lambda: self._select_kind("scalar")),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            row.addWidget(button)
        left_layout.addLayout(row)
        splitter.addWidget(left)

        # ---------------------------------------------- 右：画布 + 控制
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        right_layout.addWidget(self._build_controls())
        right_layout.addWidget(self._build_hint_row())

        self.canvas = ScopeCanvas()
        self.canvas.timebaseChanged.connect(self._on_timebase_changed)
        right_layout.addWidget(self.canvas, 1)
        splitter.addWidget(right)
        # 左边列表允许被压窄（不然通道名一长，整页最小宽度就跟着涨）
        left.setMinimumWidth(180)
        splitter.setSizes([250, 950])

        # ---------------------------------------------- 下：录波历史
        root.addWidget(self._build_history())

        self.set_project(project)

        # 录波会持续产生，页面可见时定时刷新历史列表，
        # 否则用户会看到"没有录波数据"却不知道要点刷新
        self._history_timer = QTimer(self)
        self._history_timer.timeout.connect(self._auto_reload_history)
        self._history_timer.start(5000)

    def _auto_reload_history(self) -> None:
        if self.isVisible() and not self._history_paused:
            self.reload_history()

    def showEvent(self, event) -> None:  # noqa: item2
        """切换到本页时立即刷一次录波历史，保证看到的是最新数据。"""
        super().showEvent(event)
        if not self._history_paused:
            self.reload_history()

    # -------------------------------------------------- 控制栏

    def _build_controls(self) -> QWidget:
        """控制栏排成两行。

        控件多（时基 / Y 轴 / 触发方式 / 触发沿 / 触发电平 / 触发源 / 光标 / 清空），
        挤在一行时最小宽度会到 1200 像素左右 —— 加上左侧列表与右侧画布，
        整页最小宽度就把窗口顶到 1400 以上，小屏上窗口会超出屏幕。排两行就下来了。
        """
        bar = QWidget()
        outer = QVBoxLayout(bar)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(6)
        row1.addWidget(QLabel("时基"))
        self.timebase_combo = QComboBox()
        for label, seconds in TIMEBASES:
            self.timebase_combo.addItem(label, seconds)
        self.timebase_combo.setCurrentIndex(1)
        self.timebase_combo.currentIndexChanged.connect(
            lambda: self.canvas.set_timebase(float(self.timebase_combo.currentData()))
        )
        row1.addWidget(self.timebase_combo)

        row1.addSpacing(8)
        row1.addWidget(QLabel("Y 轴"))
        self.auto_y = QCheckBox("自动量程")
        self.auto_y.setChecked(True)
        self.auto_y.toggled.connect(self._apply_y_range)
        row1.addWidget(self.auto_y)
        self.y_min = self._make_spin(-1e6, 1e6, 0.0)
        self.y_max = self._make_spin(-1e6, 1e6, 100.0)
        self.y_min.valueChanged.connect(self._apply_y_range)
        self.y_max.valueChanged.connect(self._apply_y_range)
        row1.addWidget(self.y_min)
        row1.addWidget(self.y_max)

        row1.addSpacing(8)
        self.cursors = QCheckBox("光标")
        self.cursors.setChecked(True)
        self.cursors.toggled.connect(self._apply_cursors)
        row1.addWidget(self.cursors)

        clear_button = QPushButton("清空曲线")
        clear_button.clicked.connect(self._clear)
        row1.addWidget(clear_button)

        reset_button = QPushButton("归位")
        reset_button.setToolTip(
            "把时基、平移、Y 轴、触发、光标一次恢复默认（调乱了很省事）"
        )
        reset_button.clicked.connect(self._reset_view)
        row1.addWidget(reset_button)
        row1.addStretch(1)
        outer.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)
        row2.addWidget(QLabel("触发"))
        self.trigger_mode = QComboBox()
        for label, value in TRIGGER_MODES:
            self.trigger_mode.addItem(label, value)
            self.trigger_mode.setItemData(
                self.trigger_mode.count() - 1,
                TRIGGER_MODE_TIPS.get(value, ""),
                Qt.ToolTipRole,
            )
        row2.addWidget(self.trigger_mode)
        self.trigger_edge = QComboBox()
        for label, value in TRIGGER_EDGES:
            self.trigger_edge.addItem(label, value)
        row2.addWidget(self.trigger_edge)
        self.trigger_level = self._make_spin(-1e6, 1e6, 0.0)
        row2.addWidget(self.trigger_level)
        self.trigger_source = QComboBox()
        self.trigger_source.setMinimumWidth(120)
        row2.addWidget(self.trigger_source)
        for widget in (self.trigger_mode, self.trigger_edge, self.trigger_source):
            widget.currentIndexChanged.connect(self._apply_trigger)
        self.trigger_level.valueChanged.connect(self._apply_trigger)
        row2.addStretch(1)
        outer.addLayout(row2)
        return bar

    def _build_hint_row(self) -> QWidget:
        """提示单独占一行并允许折行。

        这行字挺长（"没有波形点位：按瞬时值画趋势（每个采集周期一个点，约 200 ms/点）…"），
        放在控制栏那一行会把窗口最小宽度顶到两千多像素 —— 小屏上窗口直接超出屏幕。
        """
        self.hint = ElidedLabel("", wrap=True)
        self.hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        return self.hint

    @staticmethod
    def _make_spin(low: float, high: float, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(low, high)
        spin.setDecimals(2)
        spin.setValue(value)
        spin.setFixedWidth(96)
        spin.setKeyboardTracking(False)
        return spin

    # -------------------------------------------------- 录波历史

    def _build_history(self) -> QWidget:
        box = QGroupBox("录波历史（双击载入到画布）")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(6)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._manual_refresh)
        row.addWidget(refresh)
        load = QPushButton("载入选中")
        load.clicked.connect(self._load_selected_history)
        row.addWidget(load)
        export = QPushButton("导出选中为 CSV")
        export.clicked.connect(self._export_selected_history)
        row.addWidget(export)
        row.addStretch(1)
        self.history_hint = ElidedLabel("没有录波数据：请给数组点位勾上「录波」并启动采集")
        self.history_hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        row.addWidget(self.history_hint)
        layout.addLayout(row)

        self.history_table = QTableWidget(0, 5)
        self.history_table.setHorizontalHeaderLabels(["时间", "点号", "点数", "采样间隔", "记录 ID"])
        self.history_table.setAlternatingRowColors(True)
        self.history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.history_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setColumnWidth(0, 160)
        self.history_table.setColumnWidth(1, 240)
        self.history_table.setColumnWidth(2, 80)
        self.history_table.setColumnWidth(3, 100)
        self.history_table.setColumnWidth(4, 90)
        self.history_table.doubleClicked.connect(self._load_selected_history)
        self.history_table.setMaximumHeight(150)
        layout.addWidget(self.history_table)
        return box

    # -------------------------------------------------- 工程与通道

    def set_project(self, project: Project) -> None:
        self.project = project
        self._build_channels()
        self.reload_history()

    def set_storage(self, storage) -> None:
        self.storage = storage
        self.reload_history()

    def set_preferences(self, settings) -> None:
        """套用软件设置里的示波器默认项（时基、是否列出全部点位）。"""
        self._preferences = settings
        index = self.timebase_combo.findData(float(settings.scope_timebase_s))
        if index >= 0:
            self.timebase_combo.setCurrentIndex(index)
        self._show_all = bool(settings.scope_show_all_points)
        self.show_all_box.blockSignals(True)
        self.show_all_box.setChecked(self._show_all)
        self.show_all_box.blockSignals(False)
        self._build_channels()

    def _on_show_all_toggled(self, checked: bool) -> None:
        self._show_all = checked
        self._build_channels()

    def _make_channel(self, device, point, index: int) -> ScopeChannel:
        return ScopeChannel(
            pid=point_id(device.name, point.name),
            name=f"{device.name}.{point.title}",
            unit=point.unit,
            color=point.scope_color or theme.series_color(index),
            visible=bool(point.scope or point.is_array),
            kind="array" if point.is_array else "scalar",
            decimals=point.decimals,
            sample_interval_us=point.sample_interval_us,
        )

    def _make_item(self, device, point, channel: ScopeChannel) -> QListWidgetItem:
        item = QListWidgetItem(f"{device.name}.{point.title}")
        item.setData(Qt.UserRole, channel.pid)
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(Qt.Checked if channel.visible else Qt.Unchecked)
        item.setForeground(QColor(channel.color))
        if point.is_array:
            item.setToolTip(
                f"{point.count} 点数组点位，采样间隔 {point.sample_interval_us or '未知'}us"
            )
        else:
            item.setToolTip(
                f"{point.register} #{point.address}　{point.data_type}"
                + ("　可写" if point.writable else "　只读")
            )
        return item

    def _entries_for_list(self) -> tuple[list[tuple], bool]:
        """返回 ``(可选点位列表, 是否用了"全列出来"的兜底)``。

        「显示全部点位」打开时列全部；否则只列标了「示波器=是」的。
        **一个都没标时兜底列全部** —— 现场最常见的设备只有一批有效值/计算量，
        没有波形缓冲区，这时列表空着会让人以为示波器坏了。
        """
        if self._show_all:
            return [(d, p) for d, p in self.project.iter_points() if d.enabled], False
        marked = self.project.scope_points()
        if marked:
            return marked, False
        return [(d, p) for d, p in self.project.iter_points() if d.enabled], True

    def _poll_text(self) -> str:
        """当前采集节奏（一个采集周期一个点，所以这个数直接决定趋势密度）。"""
        intervals = [
            device.poll_interval_ms
            for device in self.project.devices
            if device.enabled and device.poll_interval_ms
        ]
        if not intervals:
            return ""
        return f"约 {min(intervals)} ms/点"

    def _display_hint(self, entries: list[tuple], visible: int, fallback: bool) -> str:
        """说清楚现在是"波形"还是"瞬时值"，以及为什么这么显示。"""
        arrays = sum(1 for _device, point in entries if point.is_array)
        scope = "全部点位" if (self._show_all or fallback) else "已标记点位"
        parts = [f"{scope} {len(entries)} 个可选，已选 {visible} 个"]
        if arrays:
            parts.append(f"含 {arrays} 个波形点位：选波形点位画整段波形")
        else:
            parts.append(
                "工程里没有波形点位：按瞬时值画趋势（每个采集周期一个点"
                + (f"，{self._poll_text()}" if self._poll_text() else "")
                + "）"
            )
            # 趋势模式下"每格几个点"直接决定曲线好不好看，太稀就得提醒一句
            cheap = self._trend_points_per_division()
            if cheap and cheap < 4:
                parts.append(
                    f"注意：每格只有约 {cheap:.0f} 个点，曲线会很稀 —— "
                    "把设备的轮询周期调小，或把时基调大"
                )
        if fallback:
            parts.append("没有点位标「示波器」，先按全部点位列出")
        return "　｜　".join(parts)

    def _trend_points_per_division(self) -> float:
        """趋势模式下每格大约几个采样点（0 = 算不出来）。"""
        intervals = [
            device.poll_interval_ms
            for device in self.project.devices
            if device.enabled and device.poll_interval_ms
        ]
        if not intervals:
            return 0.0
        try:
            per_div = float(self.timebase_combo.currentData() or 5.0)
        except (TypeError, ValueError):
            return 0.0
        return per_div / (max(1, min(intervals)) / 1000.0)

    def _build_channels(self) -> None:
        """重建通道列表。

        重建时尽量保留用户已经勾选的通道，免得切一下开关就把选好的曲线清空。
        """
        keep = {pid for pid, ch in self._channels.items() if ch.visible}

        self.channel_list.blockSignals(True)
        self.channel_list.clear()
        for pid in [pid for pid in self._channels if pid not in keep]:
            self._channels.pop(pid, None)

        entries, fallback = self._entries_for_list()
        seen: set[str] = set()
        for index, (device, point) in enumerate(entries):
            pid = point_id(device.name, point.name)
            seen.add(pid)
            channel = self._channels.get(pid) or self._make_channel(device, point, index)
            if pid in keep:
                channel.visible = True
            self._channels[pid] = channel
            self.channel_list.addItem(self._make_item(device, point, channel))

        self.channel_list.blockSignals(False)
        self.canvas.set_channels(list(self._channels.values()))
        self._rebuild_timebase_items()
        self._refresh_trigger_sources()
        self._apply_trigger()
        self._apply_y_range()
        if self._preferences is not None:
            index = self.timebase_combo.findData(float(self._preferences.scope_timebase_s))
            if index >= 0:
                self.timebase_combo.setCurrentIndex(index)
        self.canvas.set_timebase(float(self.timebase_combo.currentData()))
        visible = sum(1 for channel in self._channels.values() if channel.visible)
        self.hint.setText(self._display_hint(entries, visible, fallback))

    def add_point(self, pid: str) -> bool:
        """把任意点位加进示波器并显示。

        不限于工程里标了「示波器=是」的点位 —— 现场临时想看某个量是常事。
        返回是否成功（工程里没有这个点位则失败）。
        """
        channel = self._channels.get(pid)
        if channel is None:
            found = self.project.find_point(pid)
            if found is None:
                return False
            device, point = found
            channel = self._make_channel(device, point, len(self._channels))
            self._channels[pid] = channel
            self.channel_list.blockSignals(True)
            self.channel_list.addItem(self._make_item(device, point, channel))
            self.channel_list.blockSignals(False)
            self.canvas.set_channels(list(self._channels.values()))
        channel.visible = True
        self._sync_channel_checks()
        for row in range(self.channel_list.count()):
            if self.channel_list.item(row).data(Qt.UserRole) == pid:
                self.channel_list.setCurrentRow(row)
                break
        self._rebuild_timebase_items()
        self._refresh_trigger_sources()
        self.canvas.update()
        self.hint.setText(f"已添加 {channel.name} 到示波器")
        return True

    def _on_channel_toggled(self, item: QListWidgetItem) -> None:
        pid = item.data(Qt.UserRole)
        channel = self._channels.get(pid)
        if channel is None:
            return
        channel.visible = item.checkState() == Qt.Checked
        self._rebuild_timebase_items()
        self._refresh_trigger_sources()
        self.canvas.update()

    def _set_all(self, visible: bool) -> None:
        for index in range(self.channel_list.count()):
            item = self.channel_list.item(index)
            item.setCheckState(Qt.Checked if visible else Qt.Unchecked)

    def _select_kind(self, kind: str) -> None:
        for index in range(self.channel_list.count()):
            item = self.channel_list.item(index)
            pid = item.data(Qt.UserRole)
            channel = self._channels.get(pid)
            item.setCheckState(
                Qt.Checked if channel and channel.kind == kind else Qt.Unchecked
            )

    def _refresh_trigger_sources(self) -> None:
        current = self.trigger_source.currentData()
        self.trigger_source.blockSignals(True)
        self.trigger_source.clear()
        for channel in self.canvas.visible_channels():
            self.trigger_source.addItem(channel.name, channel.pid)
        index = self.trigger_source.findData(current)
        if index >= 0:
            self.trigger_source.setCurrentIndex(index)
        self.trigger_source.blockSignals(False)

    # -------------------------------------------------- 时基（按模式切档位）

    def _mode_for_timebase(self) -> str:
        """时基下拉按哪种模式填：有可见波形点位就"每格几个点"，否则"秒/格"。

        和画布 `mode` 判定保持一致（画布也是只看首个可见通道），免得下拉
        跟画布画的不是一回事。
        """
        if not self.canvas.visible_channels():
            return "scalar"
        return self.canvas.mode

    def _array_interval_us(self) -> int:
        """当前可见波形点位的采样间隔（us），取不到返回 0。"""
        for channel in self.canvas.visible_channels():
            if channel.is_array and channel.sample_interval_us:
                return int(channel.sample_interval_us)
        return 0

    def _array_timebase_items(self) -> list[tuple[str, float]]:
        """录波模式的档位：一格几个采样点。

        整段数组已经采好了，说"秒/格"没意义 —— 有意义的是横向一格能摊开几个点。
        1 点/格就是最短：一个采样点占满一格，跳变沿 / 响应时间才量得准。
        """
        count = self.canvas.array_sample_count()
        total = self.canvas.array_span_seconds()
        full = self.canvas.array_full_timebase()
        if not count:
            # 波形还没到（刚开机/刚连上）：先按"秒/格"填，
            # 否则下拉里只剩一个数据为 0 的死档位，用户存的偏好也对不上
            return list(TIMEBASES)
        items: list[tuple[str, float]] = [(f"全长（{count} 点）", full)]
        interval = self._array_interval_us() / 1e6
        if not interval or not count:
            return items
        for points in ARRAY_POINTS_PER_DIV:
            seconds = points * interval
            # 比"全长"还长的档位没意义（画布会直接整段铺满，跟全长重复）
            if points < count and (not full or seconds < full):
                items.append((f"{points} 点/格", seconds))
        return items

    def _timebase_labels(self) -> list[str]:
        """当前下拉里的档位名（测试和提示都用得上）。"""
        return [
            self.timebase_combo.itemText(index)
            for index in range(self.timebase_combo.count())
        ]

    def _rebuild_timebase_items(self) -> None:
        """按当前模式重建时基下拉（点数/采样间隔没变就不动，免得把缩放重置掉）。"""
        mode = self._mode_for_timebase()
        signature = (mode, self.canvas.array_sample_count(), self._array_interval_us())
        if signature == self._timebase_signature:
            return
        self._timebase_signature = signature
        previous = self.timebase_combo.currentData()
        items = self._array_timebase_items() if mode == "array" else TIMEBASES

        self.timebase_combo.blockSignals(True)
        self.timebase_combo.clear()
        for label, seconds in items:
            self.timebase_combo.addItem(label, seconds)
        index = self.timebase_combo.findData(previous) if previous is not None else -1
        if index < 0:
            index = 0 if mode == "array" else TREND_DEFAULT_INDEX
        self.timebase_combo.setCurrentIndex(min(index, self.timebase_combo.count() - 1))
        self.timebase_combo.blockSignals(False)
        self.canvas.set_timebase(float(self.timebase_combo.currentData() or 1.0))
        self.canvas.clamp_pan()

    def _nearest_timebase_index(self, seconds: float) -> int:
        """下拉里跟给定"秒/格"最接近的一档（滚轮缩放后对齐用）。"""
        best_index, best_delta = -1, None
        for index in range(self.timebase_combo.count()):
            data = self.timebase_combo.itemData(index)
            if data is None:
                continue
            delta = abs(float(data) - float(seconds))
            if best_delta is None or delta < best_delta:
                best_index, best_delta = index, delta
        return best_index

    def _on_timebase_changed(self, seconds: float) -> None:
        """画布滚轮缩放后把下拉对齐到最接近的档位。

        不对齐的话下拉显示"2 秒/格"而画布早就是别的值了，读数会骗人。
        这里故意不屏蔽信号：下拉变更会再回写一次画布，一轮就收敛。
        """
        index = self._nearest_timebase_index(seconds)
        if index >= 0 and index != self.timebase_combo.currentIndex():
            self.timebase_combo.setCurrentIndex(index)

    def _reset_view(self) -> None:
        """归位：时基 / 平移 / Y 轴 / 触发 / 光标一次恢复默认。

        时基一格格调乱以后很难改回去，看波形又常常要先看个全景。
        """
        self._timebase_signature = None
        self.auto_y.setChecked(True)
        self.cursors.setChecked(True)
        self.trigger_mode.setCurrentIndex(0)
        self.trigger_edge.setCurrentIndex(0)
        self.trigger_level.setValue(0.0)
        if self.trigger_source.count():
            self.trigger_source.setCurrentIndex(0)
        self.canvas.clear_pan()
        self._rebuild_timebase_items()
        self.timebase_combo.setCurrentIndex(
            0 if self._mode_for_timebase() == "array" else TREND_DEFAULT_INDEX
        )
        self.canvas.set_cursors(0.25, 0.75)
        self.canvas.set_timebase(float(self.timebase_combo.currentData() or 1.0))
        self._frozen = False
        self.canvas.update()
        self.hint.setText("视图已归位（时基 / 平移 / Y 轴 / 触发 / 光标）")


    # -------------------------------------------------- 参数应用

    def _apply_y_range(self) -> None:
        if self.auto_y.isChecked():
            self.canvas.set_y_range(None, None)
        else:
            self.canvas.set_y_range(self.y_min.value(), self.y_max.value())

    def _apply_trigger(self) -> None:
        self.canvas.set_trigger(
            self.trigger_mode.currentData(),
            self.trigger_edge.currentData(),
            self.trigger_level.value(),
            self.trigger_source.currentData() or "",
        )
        self._frozen = False

    def _apply_cursors(self) -> None:
        self.canvas.show_cursors = self.cursors.isChecked()
        self.canvas.update()

    def _clear(self) -> None:
        self.canvas.clear_data()
        self._frozen = False

    # -------------------------------------------------- 数据

    def on_tags(self, values: dict) -> None:
        if not values or self._frozen:
            return
        changed = False
        for pid, channel in self._channels.items():
            tag = values.get(pid)
            if tag is None or tag.value is None:
                continue
            if channel.is_array and isinstance(tag.value, (list, tuple)):
                self.canvas.set_waveform(
                    pid, [float(v) for v in tag.value], channel.sample_interval_us
                )
                changed = True
            elif not channel.is_array and not isinstance(tag.value, (list, tuple)):
                try:
                    self.canvas.append_scalar(pid, tag.timestamp or time.time(), float(tag.value))
                    changed = True
                except (TypeError, ValueError):
                    continue
        if changed:
            self._rebuild_timebase_items()
        if changed and self.trigger_mode.currentData() == "single" and self.canvas.mode == "array":
            # 单次触发：抓到一段就停住，便于细看
            self._frozen = True
            self.hint.setText("单次触发已捕获，点「清空曲线」或改触发设置继续")

    # -------------------------------------------------- 录波历史

    def _manual_refresh(self) -> None:
        """手动刷新：恢复自动刷新并立即拉一次。"""
        self._history_paused = False
        self.reload_history()

    def reload_history(self) -> None:
        self.history_table.setRowCount(0)
        if self.storage is None:
            return
        try:
            rows = self.storage.query_waveform_index(None, limit=200)
        except Exception as exc:
            self.history_hint.setText(f"读取录波失败：{exc}")
            return
        for row in rows:
            position = self.history_table.rowCount()
            self.history_table.insertRow(position)
            interval = f"{row['dt_us']} us" if row.get("dt_us") else "未知"
            cells = [
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["ts"] or 0)),
                row["pid"],
                str(row["n"]),
                interval,
                str(row["id"]),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column in (2, 3, 4):
                    item.setTextAlignment(Qt.AlignCenter)
                self.history_table.setItem(position, column, item)
        self.history_hint.setText(
            f"共 {len(rows)} 段录波"
            if rows
            else (
                "没有录波数据：当前工程没有波形点位（录波只对数组/块点位有效）"
                if not any(point.is_array for _d, point in self.project.iter_points())
                else "没有录波数据：请给数组点位勾上「录波」并启动采集"
            )
        )

    def _selected_history_id(self) -> int | None:
        row = self.history_table.currentRow()
        if row < 0:
            return None
        item = self.history_table.item(row, 4)
        return int(item.text()) if item else None

    def _load_selected_history(self) -> None:
        record = self._selected_history_id()
        if record is None:
            QMessageBox.information(self, "提示", "请先在录波历史里选一段")
            return
        if self.storage is None:
            return
        # 正在回看历史时暂停自动刷新，免得列表在眼皮底下跳
        self._history_paused = True
        pid_item = self.history_table.item(self.history_table.currentRow(), 1)
        pid = pid_item.text() if pid_item else ""
        values = self.storage.query_waveform(record)
        if not values:
            QMessageBox.warning(self, "提示", "这段录波数据为空")
            return

        channel = self._channels.get(pid)
        if channel is None:
            # 该点位不在当前通道列表里（比如工程换了），临时建一个只读通道
            channel = ScopeChannel(pid=pid, name=pid, kind="array")
            self._channels[pid] = channel
            self.canvas.set_channels(list(self._channels.values()))
        channel.visible = True
        self.canvas.set_waveform(pid, values)
        self.hint.setText(f"已载入录波 #{record}（{len(values)} 点）")
        self._sync_channel_checks()

    def _sync_channel_checks(self) -> None:
        self.channel_list.blockSignals(True)
        for index in range(self.channel_list.count()):
            item = self.channel_list.item(index)
            channel = self._channels.get(item.data(Qt.UserRole))
            item.setCheckState(Qt.Checked if channel and channel.visible else Qt.Unchecked)
        self.channel_list.blockSignals(False)

    def focus_point(self, pid: str) -> None:
        """把某个点位设为可见（从仪表盘波形卡片或示意图跳过来时用）。"""
        channel = self._channels.get(pid)
        if channel is None:
            self.hint.setText(f"示波器里没有点位 {pid}（可在参数表里把它标记为 示波器=是）")
            return
        channel.visible = True
        self._sync_channel_checks()
        index = self.channel_list.currentRow()
        for row in range(self.channel_list.count()):
            if self.channel_list.item(row).data(Qt.UserRole) == pid:
                index = row
                break
        if index >= 0:
            self.channel_list.setCurrentRow(index)
        self.canvas.update()
        self.hint.setText(f"已定位到 {channel.name}")

    def _export_selected_history(self) -> None:
        record = self._selected_history_id()
        if record is None or self.storage is None:
            QMessageBox.information(self, "提示", "请先在录波历史里选一段")
            return
        row = self.history_table.currentRow()
        pid = self.history_table.item(row, 1).text()
        values = self.storage.query_waveform(record)
        if not values:
            return
        default = f"waveform_{record}.csv"
        path, _ = QFileDialog.getSaveFileName(self, "导出录波", default, "CSV (*.csv)")
        if not path:
            return
        dt_us = 0
        try:
            dt_us = int(self.history_table.item(row, 3).text().split()[0])
        except (ValueError, IndexError):
            dt_us = 0
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            import csv as _csv

            writer = _csv.writer(fh)
            writer.writerow(["序号", "时间(ms)" if dt_us else "序号", "数值"])
            for index, value in enumerate(values):
                writer.writerow(
                    [index, f"{index * dt_us / 1000.0:.4f}" if dt_us else index, value]
                )
        QMessageBox.information(self, "导出完成", f"已导出 {len(values)} 点：\n{path}")
