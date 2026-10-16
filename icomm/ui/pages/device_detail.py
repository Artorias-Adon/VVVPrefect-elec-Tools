"""设备详情页。

从主页示意图点一个节点（比如那块 Modbus RTU 的整流 DSP），
跳到这里看它的"主要信息"：挂在哪个通道、链路与连接参数、从站号、采集周期、
通信状态（在线/故障、成功失败次数、最近耗时），以及该设备全部点位的实时值。
可写点位可以直接下发。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.models import DeviceDef, Project, point_id
from .. import theme

COLUMNS = ["点号", "显示名称", "寄存器区", "地址", "类型", "当前值", "单位", "质量", "更新时间", ""]


def connection_text(channel) -> str:
    """把通道的连接参数拼成人能读的一行。"""
    if channel is None:
        return "（通道不存在）"
    options = channel.options or {}
    if channel.link == "rtu":
        return (
            f"Modbus RTU　串口 {options.get('port', '-')}，"
            f"{options.get('baudrate', '-')}，{options.get('bytesize', 8)}"
            f"{options.get('parity', 'N')}{options.get('stopbits', 1)}"
        )
    if channel.link == "rtu_over_tcp":
        return f"RTU over TCP　{options.get('host', '-')}:{options.get('port', '-')}"
    return f"Modbus TCP　{options.get('host', '-')}:{options.get('port', '-')}"


class DeviceDetailPage(QWidget):
    """单台设备详情。"""

    backRequested = Signal()
    writeRequested = Signal(str, object)
    #: 双击点位行 / 点「参数」按钮，请求打开点位参数对话框
    editRequested = Signal(str)
    #: 在点位行上右键，``(点号, 全局坐标)``
    contextRequested = Signal(str, object)
    #: 请求调出这台设备的控制台小窗口
    controlRequested = Signal(str)

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.device_name = ""
        self._row_of: dict[str, int] = {}
        self._pid_of: dict[int, str] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # ------------------------------------------ 标题栏
        bar = QHBoxLayout()
        bar.setSpacing(10)
        back = QPushButton("← 返回示意图")
        back.clicked.connect(self.backRequested.emit)
        bar.addWidget(back)

        self.title = QLabel("设备详情")
        self.title.setStyleSheet(f"color: {theme.TEXT}; font-size: 16px; font-weight: 600;")
        bar.addWidget(self.title)

        self.control_button = QPushButton("调出控制台")
        self.control_button.setObjectName("primary")
        self.control_button.setToolTip(
            "打开这台设备的按键控制小窗口（非模态，可一边看数据一边按）\n"
            "按键来自参数表里该设备的「命令」列与「位定义」列"
        )
        self.control_button.clicked.connect(
            lambda: self.controlRequested.emit(self.device_name)
        )
        bar.addWidget(self.control_button)

        self.state_label = QLabel("")
        self.state_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 12px;")
        bar.addWidget(self.state_label)
        bar.addStretch(1)
        root.addLayout(bar)

        # ------------------------------------------ 信息区
        info_row = QHBoxLayout()
        info_row.setSpacing(10)

        self.info_box = QGroupBox("基本信息")
        info_layout = QVBoxLayout(self.info_box)
        self.info_label = QLabel("")
        self.info_label.setTextFormat(Qt.RichText)
        self.info_label.setStyleSheet(f"color: {theme.TEXT}; line-height: 160%;")
        info_layout.addWidget(self.info_label)
        info_layout.addStretch(1)
        info_row.addWidget(self.info_box, 3)

        self.status_box = QGroupBox("通信状态")
        status_layout = QVBoxLayout(self.status_box)
        self.status_label = QLabel("")
        self.status_label.setTextFormat(Qt.RichText)
        self.status_label.setStyleSheet(f"color: {theme.TEXT}; line-height: 160%;")
        status_layout.addWidget(self.status_label)
        status_layout.addStretch(1)
        info_row.addWidget(self.status_box, 2)

        root.addLayout(info_row)

        # ------------------------------------------ 点位表
        points_box = QGroupBox("点位实时值")
        points_layout = QVBoxLayout(points_box)
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        for column, width in enumerate((150, 150, 90, 80, 90, 130, 70, 70, 150, 80)):
            self.table.setColumnWidth(column, width)
        # 双击行 = 打开点位参数对话框；右键 = 单发 / 持续发送 / 添加到示波器
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)
        points_layout.addWidget(self.table)
        root.addWidget(points_box, 1)

    # -------------------------------------------------- 点位交互

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        pid = self._pid_of.get(row)
        if pid:
            self.editRequested.emit(pid)

    def _on_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        pid = self._pid_of.get(row)
        if pid:
            self.table.selectRow(row)
            self.contextRequested.emit(pid, self.table.viewport().mapToGlobal(pos))

    # -------------------------------------------------- 切换设备

    def set_project(self, project: Project) -> None:
        self.project = project
        if self.device_name:
            self.show_device(self.device_name)

    def show_device(self, name: str) -> None:
        self.device_name = name
        device = self.project.device(name)
        if device is None:
            self.title.setText("设备不存在")
            self.info_label.setText(f"工程里找不到设备「{name}」。")
            self.status_label.setText("")
            self.table.setRowCount(0)
            return

        channel = self.project.channel(device.channel)
        self.title.setText(device.name)
        self.info_label.setText(
            "<table cellspacing='4'>"
            f"<tr><td>协议驱动</td><td><b>{channel.driver if channel else '-'}</b></td></tr>"
            f"<tr><td>所属通道</td><td><b>{device.channel or '-'}</b></td></tr>"
            f"<tr><td>链路 / 连接</td><td><b>{connection_text(channel)}</b></td></tr>"
            f"<tr><td>从站号</td><td><b>{device.unit_id}</b></td></tr>"
            f"<tr><td>采集周期</td><td><b>{device.poll_interval_ms} ms</b></td></tr>"
            f"<tr><td>超时 / 重试</td><td><b>{device.timeout_ms} ms / {device.retries} 次</b></td></tr>"
            f"<tr><td>启用</td><td><b>{'是' if device.enabled else '否'}</b></td></tr>"
            f"<tr><td>点位数量</td><td><b>{len(device.points)}</b>（其中波形 {sum(1 for p in device.points if p.is_array)} 个）</td></tr>"
            f"<tr><td>备注</td><td>{device.description or '-'}</td></tr>"
            "</table>"
        )
        self._fill_points(device)
        self._update_status(None)

    def _fill_points(self, device: DeviceDef) -> None:
        self.table.setRowCount(0)
        self._row_of.clear()
        self._pid_of.clear()
        for point in device.points:
            row = self.table.rowCount()
            self.table.insertRow(row)
            pid = point_id(device.name, point.name)
            self._row_of[pid] = row
            self._pid_of[row] = pid
            cells = [
                point.name,
                point.title,
                point.register,
                str(point.address),
                point.data_type if not point.is_array else f"{point.data_type}×{point.count}",
                "--",
                point.unit,
                "坏",
                "--",
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column in (2, 3, 4, 7):
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, column, item)
            button = QPushButton("参数")
            button.setFixedWidth(64)
            button.setToolTip("打开点位参数对话框：单发 / 持续发送 / 看位状态")
            button.clicked.connect(
                lambda _checked=False, i=pid: self.editRequested.emit(i)
            )
            self.table.setCellWidget(row, len(COLUMNS) - 1, button)
            self.table.setItem(row, 7, QTableWidgetItem("坏"))

    # -------------------------------------------------- 实时刷新

    def on_tags(self, values: dict) -> None:
        if not values:
            return
        device = self.project.device(self.device_name)
        if device is None:
            return
        for point in device.points:
            pid = point_id(device.name, point.name)
            tag = values.get(pid)
            row = self._row_of.get(pid)
            if tag is None or row is None:
                continue
            if tag.value is None:
                text = "--"
            elif point.is_array and isinstance(tag.value, (list, tuple)):
                waves = list(tag.value)
                if waves:
                    text = f"{len(waves)} 点，峰峰 {max(waves) - min(waves):.2f}"
                else:
                    text = "空"
            else:
                try:
                    text = f"{float(tag.value):.{max(0, point.decimals)}f}"
                except (TypeError, ValueError):
                    text = str(tag.value)

            item = self.table.item(row, 5)
            if item is not None:
                item.setText(text)
                item.setForeground(
                    QColor(theme.TEXT if tag.quality == "good" else theme.QUALITY_BAD)
                )

            quality_item = self.table.item(row, 7)
            if quality_item is not None:
                quality_item.setText(
                    {"good": "好", "bad": "坏", "stale": "陈旧", "uncertain": "可疑"}.get(
                        tag.quality, tag.quality
                    )
                )
                quality_item.setForeground(
                    QColor(theme.ACCENT if tag.quality == "good" else theme.QUALITY_BAD)
                )

            time_item = self.table.item(row, 8)
            if time_item is not None:
                time_item.setText(
                    time.strftime("%H:%M:%S", time.localtime(tag.timestamp))
                    if tag.timestamp
                    else "--"
                )

    def on_device_state(self, status) -> None:
        if status.name == self.device_name:
            self._update_status(status)

    def _update_status(self, status) -> None:
        if status is None:
            self.status_label.setText(
                "<span style='color:#8b97a5'>尚未启动采集，暂无通信状态。</span>"
            )
            self.state_label.setText("")
            return
        color = theme.ACCENT if status.connected else theme.QUALITY_BAD
        self.state_label.setText(f"　● {status.state}")
        self.state_label.setStyleSheet(f"color: {color}; font-size: 12px;")
        detail = (
            "<table cellspacing='4'>"
            f"<tr><td>状态</td><td><b style='color:{color}'>{status.state}</b></td></tr>"
            f"<tr><td>成功次数</td><td><b>{status.ok_count}</b></td></tr>"
            f"<tr><td>失败次数</td><td><b>{status.error_count}</b></td></tr>"
            f"<tr><td>最近耗时</td><td><b>{status.last_duration_ms:.0f} ms</b></td></tr>"
            f"<tr><td>最近成功</td><td>{_fmt_time(status.last_ok_ts)}</td></tr>"
            f"<tr><td>最近失败</td><td>{_fmt_time(status.last_error_ts)}</td></tr>"
            f"<tr><td>最近错误</td><td>{status.error or '-'}</td></tr>"
            "</table>"
        )
        self.status_label.setText(detail)


def _fmt_time(ts: float) -> str:
    if not ts:
        return "--"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
