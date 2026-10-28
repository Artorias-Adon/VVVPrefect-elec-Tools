"""参数记录：把调试好的下发参数存下来，导到同型号的下一台设备。

现场流程：在一台装置上把参数试到位 → 「记录当前值」（或直接看自动记下的那些）→
**保存为 JSON**（也可选 CSV / TXT）→ 换到下一台设备 → 「导入…」按点位名对上 → 一次下发。

自动记录由主窗口在**写值成功**之后喂进来（:class:`icomm.core.params.ParamRecorder`），
所以单发、持续发送、按键控制台、自动化写出来的值都会被记上；命令点位（启动/停机这种**动作**）
不算参数，不记。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import params, sheetio
from .. import theme
from ..spin import no_arrows
from .base import ToolContext, WindowTool, register_tool

#: 表格刷新间隔（自动记录随时会来）
REFRESH_MS = 500

COLUMNS = ("点号", "显示名", "下发值", "单位", "地址", "时间")

#: 下发历史表的列（时间在最前：现场就是按时间查的）
HISTORY_COLUMNS = ("下发时间", "设备", "点号", "显示名", "值", "单位", "来源")

#: 历史表最多显示多少条（再多也没人看，导出的才是全量）
HISTORY_SHOW_LIMIT = 2000


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
            "「最新值」是这台设备现在这套值，「下发历史」记着每一次下发的时间与来源；"
            "历史可以导出成表格（Excel / CSV）去查、去对，换到同型号的另一台设备"
            "再导入，按每个参数的<b>最后一次</b>自动下发。"
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

        self.tabs = QTabWidget()
        latest_page = QWidget()
        latest_layout = QVBoxLayout(latest_page)
        latest_layout.setContentsMargins(0, 6, 0, 0)
        latest_layout.addWidget(self.table, 1)
        self.tabs.addTab(latest_page, "最新值")
        self.tabs.addTab(self._build_history_page(), "下发历史")
        root.addWidget(self.tabs, 1)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT};")
        root.addWidget(self.status)

        self._history_signature: tuple | None = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(REFRESH_MS)

        self.reload_devices()

    # -------------------------------------------------- 下发历史（界面）

    def _build_history_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.history_all_check = QCheckBox("所有设备")
        self.history_all_check.setToolTip("取消勾选就只看上面选中的那台设备")
        self.history_all_check.setChecked(True)
        self.history_all_check.toggled.connect(lambda _checked: self.refresh_history())
        row.addWidget(self.history_all_check)
        row.addWidget(QLabel("筛选"))
        self.history_filter = QLineEdit()
        self.history_filter.setPlaceholderText("按点号 / 显示名筛选")
        self.history_filter.setClearButtonEnabled(True)
        self.history_filter.textChanged.connect(lambda _text: self.refresh_history())
        row.addWidget(self.history_filter, 1)
        row.addWidget(QLabel("最多显示"))
        self.history_limit = QSpinBox()
        self.history_limit.setRange(50, params.HISTORY_LIMIT)
        self.history_limit.setSingleStep(50)
        self.history_limit.setValue(200)
        self.history_limit.setToolTip("只是表里显示多少条；导出永远是全部")
        self.history_limit.valueChanged.connect(lambda _value: self.refresh_history())
        no_arrows(self.history_limit)
        row.addWidget(self.history_limit)
        row.addStretch(1)
        layout.addLayout(row)

        self.history_table = QTableWidget(0, len(HISTORY_COLUMNS))
        self.history_table.setHorizontalHeaderLabels(HISTORY_COLUMNS)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.history_table.setAlternatingRowColors(True)
        header = self.history_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.history_table, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.export_history_button = QPushButton("导出历史…")
        self.export_history_button.setToolTip(
            "把每一次下发（设备 / 点号 / 值 / 时间 / 来源）存成 Excel 或 CSV，回去慢慢查"
        )
        self.export_history_button.clicked.connect(self.export_history)
        buttons.addWidget(self.export_history_button)
        self.import_history_button = QPushButton("导入（按最后一次下发）…")
        self.import_history_button.setToolTip(
            "读一份历史表格，按**每个参数的最后一次值**下发到上面选中的设备"
        )
        self.import_history_button.clicked.connect(self.import_history)
        buttons.addWidget(self.import_history_button)
        self.clear_history_button = QPushButton("清空历史")
        self.clear_history_button.setToolTip("只清历史（最新值不动），不影响已导出的文件")
        self.clear_history_button.clicked.connect(self.clear_history)
        buttons.addWidget(self.clear_history_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.history_status = QLabel("")
        self.history_status.setWordWrap(True)
        self.history_status.setStyleSheet(f"color: {theme.TEXT};")
        layout.addWidget(self.history_status)
        return page

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
            self.refresh_history()
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
                self.refresh_history()
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
        self.refresh_history()

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
        if not path.lower().endswith(
            tuple(f".{name}" for name in params.READABLE_FORMATS + (params.FORMAT_TXT,))
        ):
            # 用户没写扩展名：按当前选中的过滤器补上
            fmt = params.format_from_filter(selected)
            path = f"{path}.{fmt}"
        try:
            saved = params.save(path, record, fmt)
        except (OSError, ValueError) as exc:
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

    # -------------------------------------------------- 下发历史

    def history_events(self) -> list[params.ParamEvent]:
        """按界面上的条件取历史（最新的在前）：范围、关键字、显示条数。"""
        device = "" if self.history_all_check.isChecked() else self.device_name()
        events = self.recorder.events(device_name=device or None)
        text = self.history_filter.text().strip().lower()
        if text:
            events = [
                event
                for event in events
                if text in event.point.lower() or text in event.label.lower()
            ]
        limit = self.history_limit.value()
        return events[:limit] if limit and limit > 0 else events

    def refresh_history(self) -> None:
        """重画历史表；内容没变就只更新一行状态（免得抢走选中行与滚动位置）。"""
        events = self.history_events()
        signature = (
            len(events),
            events[0].time if events else "",
            events[-1].time if events else "",
            events[0].value if events else None,
            events[0].point if events else "",
        )
        if signature == self._history_signature:
            self._update_history_status(events)
            return
        self._history_signature = signature
        self.history_table.setRowCount(len(events))
        for row, event in enumerate(events):
            values = (
                event.time,
                event.device,
                event.point,
                event.label,
                "" if event.value is None else str(event.value),
                event.unit,
                event.source,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 4:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.history_table.setItem(row, column, item)
        self._update_history_status(events)

    def _update_history_status(self, events: list[params.ParamEvent]) -> None:
        total = self.recorder.history_count()
        if not events:
            self.history_status.setText(
                "还没有下发记录：手动写一个参数就会记下来（只读点位和命令键不算）。"
            )
            return
        newest = events[0].time or "（无时间）"
        scope = "全部设备" if self.history_all_check.isChecked() else self.device_name()
        self.history_status.setText(
            f"{scope}：显示 {len(events)} 条（共记 {total} 条），最近一次 {newest}。"
        )

    def export_history(self) -> str:
        """按界面当前条件导出（选文件）。返回路径（空 = 取消）。"""
        events = self.history_events()
        saved = export_history_dialog(
            self, self.context, self.recorder, events=events
        )
        if saved:
            self.history_status.setText(f"已导出 {len(events)} 条到：{saved}")
        return saved

    def export_history_to(self, path: str) -> str:
        """导出到指定路径（不弹对话框，测试与「文件 → 保存为…」共用）。"""
        events = self.history_events()
        saved = params.save_history(path, events)
        self.history_status.setText(f"已导出 {len(events)} 条到：{saved}")
        return saved

    def import_history(self) -> "HistoryImportResult":
        """选文件导入历史，按每个参数的最后一次值下发到当前设备。"""
        result = import_history_dialog(
            self, self.context, self.recorder, self.device_name()
        )
        self._finish_history_import(result)
        return result

    def import_history_from(self, path: str) -> "HistoryImportResult":
        """从指定文件导入（不弹对话框，测试与「文件 → 保存为…」共用）。"""
        result = import_history_file(
            self.context, self.recorder, self.device_name(), path
        )
        self._finish_history_import(result)
        return result

    def _finish_history_import(self, result: "HistoryImportResult") -> None:
        if result.message:
            self.history_status.setText(result.message)
        self.refresh()
        self.refresh_history()

    def clear_history(self) -> None:
        device = "" if self.history_all_check.isChecked() else self.device_name()
        self.recorder.clear_history(device or None)
        self._history_signature = None
        self.refresh_history()
        self.history_status.setText(
            f"已清空{'全部设备' if not device else f'「{device}」'}的下发历史"
            "（最新值和已导出的文件都没动）。"
        )

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        super().closeEvent(event)


# --------------------------------------------------------------- 导出 / 导入（界面无关）


@dataclass
class HistoryImportResult:
    """一次「按最后一次下发」的结果：给界面和状态栏看的都是它。"""

    count: int = 0
    errors: list[str] = field(default_factory=list)
    message: str = ""

    def __bool__(self) -> bool:
        return bool(self.count) and not self.errors


def default_history_name(device_name: str = "") -> str:
    """默认文件名：带日期，现场一眼能看出是哪天导的。"""
    import time as _time

    stamp = _time.strftime("%Y%m%d")
    safe = "".join(ch for ch in device_name if ch not in '<>:"/\\|?*').strip()
    return f"参数下发历史_{safe + '_' if safe else ''}{stamp}"


def export_history_dialog(
    parent,
    context: ToolContext,
    recorder: params.ParamRecorder,
    device_name: str = "",
    events: list[params.ParamEvent] | None = None,
) -> str:
    """选文件 → 保存下发历史（表格优先）。返回路径（空 = 取消 / 没有记录）。"""
    chosen = list(recorder.events(device_name=device_name or None) if events is None else events)
    if not chosen:
        QMessageBox.information(
            parent,
            "还没有下发记录",
            "先手动写几个参数（或点「记录当前值」），再来导出历史。",
        )
        return ""
    suffix = "xlsx" if sheetio.excel_ready() else "csv"
    start = context.output_path("params", f"{default_history_name(device_name)}.{suffix}")
    path, selected = QFileDialog.getSaveFileName(
        parent, "保存参数下发历史", start, params.history_file_filter()
    )
    if not path:
        return ""
    known = tuple(f".{name}" for name in (*params.READABLE_FORMATS, params.FORMAT_TXT))
    if not path.lower().endswith(known):
        path = f"{path}.{params.format_from_filter(selected)}"
    try:
        saved = params.save_history(path, chosen)
    except (OSError, ValueError, sheetio.SheetError) as exc:
        QMessageBox.warning(parent, "存不下来", str(exc))
        return ""
    context.announce("参数下发历史已保存", f"{len(chosen)} 条 → {saved}", "info")
    return saved


def import_history_file(
    context: ToolContext,
    recorder: params.ParamRecorder,
    device_name: str,
    path: str,
) -> HistoryImportResult:
    """读一份历史文件 → 每个点位取最后一次 → 下发到 ``device_name``。"""
    if not device_name:
        return HistoryImportResult(message="先选一台设备。")
    try:
        events = params.load_history(path)
    except (ValueError, OSError) as exc:
        return HistoryImportResult(message=f"读不了这份下发历史：{exc}")
    try:
        plan = params.plan_history_import(events, context.project, device_name)
    except ValueError as exc:
        return HistoryImportResult(message=str(exc))
    if not plan.entries:
        return HistoryImportResult(
            message=(
                f"这份历史里没有能对到「{device_name}」的参数（{plan.summary()}）。"
                "同型号设备点号一样才能套用。"
            )
        )
    if not context.engine_running():
        return HistoryImportResult(
            errors=["未启动采集"],
            message="还没启动采集，写值没法下发 —— 先启动采集再导入。",
        )

    def remember(_pid: str, event: params.ParamEvent) -> None:
        device = context.project.device(plan.device)
        point = device.point(event.point) if device is not None else None
        if point is not None:
            recorder.record(
                plan.device, point, event.value, source=params.SOURCE_IMPORT
            )

    errors = params.apply_history_plan(plan, context.send, on_written=remember)
    if errors:
        message = (
            f"按最后一次下发 {len(plan.entries)} 项，{len(errors)} 项出错：\n"
            + "\n".join(errors[:5])
        )
    else:
        message = (
            f"已按每个参数的最后一次值向「{device_name}」下发 "
            f"{len(plan.entries)} 项（{plan.summary()}）。"
        )
        context.announce(
            "参数下发历史已导入", f"{device_name}：{len(plan.entries)} 项", "info"
        )
    return HistoryImportResult(count=len(plan.entries), errors=errors, message=message)


def import_history_dialog(
    parent,
    context: ToolContext,
    recorder: params.ParamRecorder,
    device_name: str,
) -> HistoryImportResult:
    """选文件 → 按最后一次下发。取消返回空结果。"""
    if not device_name:
        return HistoryImportResult(message="先选一台设备。")
    start = context.report_subdir("params")
    path, _selected = QFileDialog.getOpenFileName(
        parent, "导入参数下发历史（按每个参数的最后一次下发）", start,
        params.history_file_filter(),
    )
    if not path:
        return HistoryImportResult()
    return import_history_file(context, recorder, device_name, path)



@register_tool
class ParamRecordTool(WindowTool):
    """「工具 → 参数记录」。"""

    name = "参数记录"
    group = "工程"
    order = 30
    description = "记录下发过的参数与下发历史（可导出表格查询），按最后一次值导到同型号的另一台设备"

    def build(self, context: ToolContext) -> QWidget:
        return ParamRecordWindow(context)
