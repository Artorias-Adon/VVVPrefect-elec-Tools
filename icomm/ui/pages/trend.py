"""历史趋势页：实时滚动曲线 + 从数据库回放历史。"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...core.models import Project, point_id
from .. import theme
from ..widgets import SeriesPicker, TrendChart

WINDOWS = [("1 分钟", 60), ("5 分钟", 300), ("15 分钟", 900), ("1 小时", 3600)]
HISTORY_SPANS = [("最近 10 分钟", 600), ("最近 1 小时", 3600), ("最近 6 小时", 21600), ("最近 24 小时", 86400)]


class TrendPage(QWidget):
    """多点位趋势分析。"""

    def __init__(self, project: Project, storage=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.storage = storage

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        toolbar.addWidget(QLabel("时间窗口"))
        self.window_combo = QComboBox()
        for label, seconds in WINDOWS:
            self.window_combo.addItem(label, seconds)
        self.window_combo.setCurrentIndex(1)
        self.window_combo.currentIndexChanged.connect(self._apply_window)
        toolbar.addWidget(self.window_combo)

        toolbar.addWidget(QLabel("历史区间"))
        self.span_combo = QComboBox()
        for label, seconds in HISTORY_SPANS:
            self.span_combo.addItem(label, seconds)
        toolbar.addWidget(self.span_combo)

        load_button = QPushButton("加载历史")
        load_button.clicked.connect(self.load_history)
        toolbar.addWidget(load_button)

        clear_button = QPushButton("清空曲线")
        clear_button.clicked.connect(self._clear)
        toolbar.addWidget(clear_button)

        toolbar.addStretch(1)
        self.info = QLabel("")
        self.info.setStyleSheet(f"color: {theme.TEXT_DIM};")
        toolbar.addWidget(self.info)
        root.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        picker_box = QGroupBox("选择点位")
        picker_layout = QVBoxLayout(picker_box)
        self.picker = SeriesPicker()
        self.picker.changed.connect(self._on_pick)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.picker)
        picker_layout.addWidget(scroll)
        splitter.addWidget(picker_box)

        chart_box = QGroupBox("趋势曲线")
        chart_layout = QVBoxLayout(chart_box)
        self.chart = TrendChart()
        chart_layout.addWidget(self.chart)
        splitter.addWidget(chart_box)
        splitter.setSizes([260, 800])

        self.set_project(project)
        self._apply_window()

    # -------------------------------------------------- 配置

    def set_project(self, project: Project) -> None:
        self.project = project
        entries = [
            (point_id(device.name, point.name), f"{device.name} · {point.title}")
            for device, point in project.iter_points()
        ]
        self.picker.set_points(entries)
        self.chart.clear()
        for pid in self.chart.series_ids:
            self.chart.remove_series(pid)
        self.info.setText(f"共 {len(entries)} 个可选点位")

    def set_storage(self, storage) -> None:
        self.storage = storage

    def _apply_window(self) -> None:
        self.chart.set_window(float(self.window_combo.currentData()))

    def _on_pick(self, pids: list[str]) -> None:
        for pid in list(self.chart.series_ids):
            if pid not in pids:
                self.chart.remove_series(pid)
        for pid in pids:
            if pid in self.chart.series_ids:
                continue
            found = self.project.find_point(pid)
            if found is None:
                continue
            device, point = found
            self.chart.add_series(pid, f"{device.name}.{point.title}", point.unit)
        self.info.setText(f"已选 {len(pids)} 条曲线")

    def _clear(self) -> None:
        self.chart.clear()

    # -------------------------------------------------- 数据

    def on_tags(self, values: dict) -> None:
        if not self.chart.series_ids:
            return
        now = time.time()
        for pid in self.chart.series_ids:
            tag = values.get(pid)
            if tag is not None and tag.value is not None:
                self.chart.append(pid, tag.timestamp or now, tag.value)

    def load_history(self) -> None:
        if self.storage is None:
            self.info.setText("未启用历史归档")
            return
        pids = self.chart.series_ids
        if not pids:
            self.info.setText("请先选择点位")
            return
        span = float(self.span_combo.currentData())
        end = time.time()
        data = self.storage.query_history(pids, end - span, end, limit=50000)
        total = 0
        for pid, samples in data.items():
            self.chart.load_history(pid, samples)
            total += len(samples)
        # 历史区间通常远大于实时窗口，自动放宽横轴
        self.chart.set_window(max(span, float(self.window_combo.currentData())))
        self.info.setText(f"已载入 {total} 个历史点")
