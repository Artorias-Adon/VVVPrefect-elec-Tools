"""参数记录：把调试好的下发参数存下来，导到同型号的下一台设备。

现场流程：在一台装置上把参数试到位 → 「记录当前值」（或直接看自动记下的那些）→
**保存为 JSON**（也可选 CSV / TXT）→ 换到下一台设备 → 「导入…」按点位名对上 → 一次下发。

自动记录由主窗口在**写值成功**之后喂进来（:class:`icomm.core.params.ParamRecorder`），
所以单发、持续发送、按键控制台、自动化写出来的值都会被记上；命令点位（启动/停机这种**动作**）
不算参数，不记。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import params
from .. import theme
from .base import ToolContext, WindowTool, register_tool

#: 表格刷新间隔（自动记录随时会来）
REFRESH_MS = 500

COLUMNS = ("点号", "显示名", "下发值", "单位", "地址", "时间")


def _value_text(entry: params.ParamEntry) -> str:
    """表格里的"值"列：只放数字，单位单独一列（免得一列里又重复一遍单位）。"""
    return "" if entry.value is None else str(entry.value)


class ParamRecordWindow(QWidget):
    """参数记录窗口。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        # 注意别写成 `getattr(...) or ParamRecorder()`：记录本实现了 __len__，
        # 空的时候是"假值"，那样会悄悄换成另一个新本子（记录就丢了）。
        recorder = getattr(context, "param_recorder", None)
        self.recorder: params.ParamRecorder = recorder if recorder is not None else params.ParamRecorder()

        self.setWindowTitle(f"参数记录 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(760, 520)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        info = QLabel(
            "写下去的参数会自动记在这里（命令按键那种<b>动作</b>不算参数）。"
            "改完一台设备就「保存为…」（默认 JSON，也能存 CSV / TXT），"
            "换到同型号的下一台再「导入…」，按<b>点号</b>自动对上。"
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(info)

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(QLabel("设备"))
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(180)
        self.device_combo.currentIndexChanged.connect(lambda _index: self.refresh())
        head.addWidget(self.device_combo)

        self.snapshot_button = QPushButton("记录当前值")
        self.snapshot_button.setToolTip(
            "把这台设备所有可写设定项的**当前值**记一遍\n"
            "（参数是在装置面板上改的，或者刚打开软件还没写过值，就用这个）"
        )
        self.snapshot_button.clicked.connect(self.take_snapshot)
        head.addWidget(self.snapshot_button)

        self.save_button = QPushButton("保存为…")
        self.save_button.setToolTip("默认存 JSON；也可以选 CSV（Excel 编辑）或 TXT（查看）")
        self.save_button.clicked.connect(self.save_as)
        head.addWidget(self.save_button)

        self.import_button = QPushButton("导入到这台设备…")
        self.import_button.setToolTip("读一份记录，按点号对到当前选中的设备上一次下发")
        self.import_button.clicked.connect(self.import_into_device)
        head.addWidget(self.import_button)

        self.clear_button = QPushButton("清空")
        self.clear_button.setToolTip("只清这台设备记下的内容，不影响文件")
        self.clear_button.clicked.connect(self.clear_device)
        head.addWidget(self.clear_button)
        head.addStretch(1)
        root.addLayout(head)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        root.addWidget(self.table, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT};")
        root.addWidget(self.status)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(REFRESH_MS)

        self.reload_devices()

    # -------------------------------------------------- 设备与表格

    def reload_devices(self) -> None:
        """设备下拉：工程里所有有"设定项"的设备（记录可能来自别的设备）。"""
        current = self.device_combo.currentText()
        names = [
            device.name
            for device in self.context.project.devices
            if any(params.is_setting(point) for point in device.points)
        ]
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for name in names:
            self.device_combo.addItem(name)
        self.device_combo.blockSignals(False)
        if current in names:
            self.device_combo.setCurrentIndex(names.index(current))
        self.refresh()

    def device_name(self) -> str:
        return self.device_combo.currentText()

    def _tick(self) -> None:
        """记录随时会变（写值后自动记），有变化才重画表格，免得抢走选中行。"""
        self.refresh()

    def refresh(self) -> None:
        name = self.device_name()
        if not name:
            self.table.setRowCount(0)
            self.status.setText("这个工程里没有可写设定项的设备。")
            self._set_buttons_enabled(False)
            return
        device = self.context.project.device(name)
        order = [point.name for point in device.points] if device is not None else None
        record = self.recorder.record_for(name, self.context.project)
        entries = record.sorted_entries(order)
        self._set_buttons_enabled(True)

        if len(entries) == self.table.rowCount():
            same = all(
                self.table.item(row, 0) is not None
                and self.table.item(row, 0).text() == entry.point
                and self.table.item(row, 2).text() == _value_text(entry)
                for row, entry in enumerate(entries)
            )
            if same:
                self._update_status(len(entries))
                return

        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = (
                entry.point,
                entry.label,
                _value_text(entry),
                entry.unit,
                entry.address,
                entry.time,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column >= 2:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(row, column, item)
        self._update_status(len(entries))

    def _update_status(self, count: int) -> None:
        if count:
            self.status.setText(f"「{self.device_name()}」已记 {count} 项参数。")
        else:
            self.status.setText(
                f"「{self.device_name()}」还没记下参数：写一次值，或点「记录当前值」补录。"
            )

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for button in (
            self.snapshot_button,
            self.save_button,
            self.import_button,
            self.clear_button,
        ):
            button.setEnabled(enabled)

    # -------------------------------------------------- 记录

    def take_snapshot(self) -> int:
        """把当前值全记一遍（装置面板上改过的参数也能收进来）。"""
        name = self.device_name()
        if not name:
            return 0

        def value_of(pid: str):
            return self.context.tag_of(pid) if self.context.tag_of else None

        try:
            count = self.recorder.snapshot(self.context.project, name, value_of)
        except ValueError as exc:
            self.status.setText(str(exc))
            return 0
        self.refresh()
        if count:
            self.status.setText(f"已记录「{name}」当前值 {count} 项。")
        else:
            self.status.setText(
                f"「{name}」当前没有可记的值（可能还没启动采集，读不到数）。"
            )
        return count

    def clear_device(self) -> None:
        self.recorder.clear(self.device_name())
        self.refresh()
        self.status.setText(f"已清空「{self.device_name()}」的记录（文件没动）。")

    # -------------------------------------------------- 保存

    def default_file_name(self) -> str:
        name = self.device_name() or "参数"
        safe = "".join(ch for ch in name if ch not in '<>:"/\\|?*').strip() or "params"
        return f"参数记录_{safe}.json"

    def save_as(self) -> str:
        """存文件：默认 JSON，可在对话框里改成 CSV / TXT。返回路径（空 = 取消）。"""
        record = self.recorder.record_for(self.device_name(), self.context.project)
        if not record.entries:
            self.status.setText("还没有可存的参数，先写值或点「记录当前值」。")
            return ""
        start = self.context.output_path("params", self.default_file_name())
        path, selected = QFileDialog.getSaveFileName(
            self, "保存参数记录", start, params.file_filter()
        )
        if not path:
            return ""
        fmt = params.format_for_path(path)
        if not path.lower().endswith(tuple(f".{name}" for name in params.FORMATS)):
            # 用户没写扩展名：按当前选中的过滤器补上
            fmt = params.format_from_filter(selected)
            path = f"{path}.{fmt}"
        try:
            saved = params.save(path, record, fmt)
        except OSError as exc:
            self.status.setText(f"存不下来：{exc}")
            return ""
        self.status.setText(f"已保存 {len(record.entries)} 项{params.FORMATS[fmt][:4]}到：{saved}")
        self.context.announce("参数记录已保存", f"{saved}", "info")
        return saved

    # -------------------------------------------------- 导入

    def import_into_device(self) -> list[str]:
        """读一份记录，按点号对到当前设备并下发。返回出错说明。"""
        name = self.device_name()
        if not name:
            return []
        start = self.context.report_subdir("params")
        path, _selected = QFileDialog.getOpenFileName(
            self, "导入参数记录", start, params.file_filter()
        )
        if not path:
            return []
        try:
            record = params.load(path)
        except ValueError as exc:
            self.refresh()
            self.status.setText(f"读不了这份记录：{exc}")
            return []

        plan = params.plan_import(record, self.context.project, name)
        if not plan.writes:
            self.refresh()
            self.status.setText(
                f"这份记录里没有能对到「{name}」的参数（{plan.summary()}）。"
                "同型号的设备点号一样才能套用。"
            )
            return []
        if not self.context.engine_running():
            self.status.setText("还没启动采集，写值没法下发 —— 先启动采集再导入。")
            return ["未启动采集"]

        source = record.device or os.path.basename(path)
        errors = self.apply_plan_to_device(plan)
        if errors:
            message = (
                f"下发 {len(plan.writes)} 项，{len(errors)} 项出错：\n"
                + "\n".join(errors[:5])
            )
        else:
            message = f"已把「{source}」的 {len(plan.writes)} 项参数下发到「{name}」。"
            self.context.announce("参数已导入", f"{name}：{len(plan.writes)} 项", "info")
        self.refresh()
        self.status.setText(message)
        return errors

    def apply_plan_to_device(self, plan: params.ImportPlan) -> list[str]:
        """真正下发（单独拆出来便于测试：不用弹文件对话框）。"""
        def write(pid: str, value) -> bool:
            return self.context.send(pid, value)

        def remember(_pid: str, point_name: str, value) -> None:
            device = self.context.project.device(plan.device)
            point = device.point(point_name) if device is not None else None
            if point is not None:
                self.recorder.record(plan.device, point, value)

        return params.apply_plan(plan, write, on_written=remember)

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        super().closeEvent(event)


@register_tool
class ParamRecordTool(WindowTool):
    """「工具 → 参数记录」。"""

    name = "参数记录"
    group = "工程"
    order = 30
    description = "记录下发过的参数，存成 JSON/CSV 并导入到同型号的另一台设备"

    def build(self, context: ToolContext) -> QWidget:
        return ParamRecordWindow(context)
