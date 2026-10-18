"""电能与电费：实时功率、累计电量、按电价算电费。

界面只是显示器 —— 记账在 :class:`icomm.ui.energy_service.EnergyService` 里
（主窗口持有，跟着采集跑），所以**关掉这个窗口电费照样在算**；
这里也支持被钉进左侧栏（工具框架自带），现场可以一直挂着看。

电价支持两种：
* **单一电价**：一个单价，最常用；
* **分时电价**（尖/峰/平/谷）：按时段单价计费，分时电量也分着累，
  月末跟供电局的单子对账能对得上。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import energy
from ...core.energy import MeterConfig, Tariff, TariffSlot
from .. import theme
from ..dialogs.sizing import fit_to_available_screen
from ..spin import no_arrows
from .base import ToolContext, WindowTool, register_tool

_COLUMNS = (
    ("name", "电表"),
    ("mode", "方式"),
    ("source", "来源点"),
    ("power_kw", "当前功率 kW"),
    ("forward_total", "累计电量 kWh"),
    ("forward_day", "今日 kWh"),
    ("forward_month", "本月 kWh"),
    ("period", "当前时段"),
    ("price", "单价"),
    ("cost_day", "今日电费"),
    ("cost_month", "本月电费"),
    ("cost_total", "累计电费"),
    ("peak_kw", "峰值 kW"),
)

_NUMERIC = {
    "power_kw", "forward_total", "forward_day", "forward_month",
    "cost_day", "cost_month", "cost_total", "peak_kw",
}


class MeterDialog(QDialog):
    """一块电表：叫什么、读哪个点、倍率多少。"""

    def __init__(
        self,
        context: ToolContext,
        config: MeterConfig | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent or context.window)
        self.setWindowTitle("电表设置" if config else "添加电表")
        self.setMinimumWidth(560)
        self._context = context
        config = config or MeterConfig()

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.name_edit = QLineEdit(config.name)
        self.name_edit.setPlaceholderText("例如：1号进线柜 / 总表")
        form.addRow("名称", self.name_edit)

        self.mode_combo = QComboBox()
        for key, label in energy.MODES.items():
            self.mode_combo.addItem(label, key)
        index = self.mode_combo.findData(config.mode)
        self.mode_combo.setCurrentIndex(max(0, index))
        self.mode_combo.setToolTip(
            "功率积分：读一个 kW 点，按采样间隔积分（没有电量寄存器时用）\n"
            "电量示数：直接读装置的累计 kWh 做差值（最准，推荐）"
        )
        form.addRow("取电量的方式", self.mode_combo)

        self.point_combo = QComboBox()
        self.point_combo.setEditable(True)  # 点号也能手打（比如点还没建好）
        self.point_combo.setInsertPolicy(QComboBox.NoInsert)
        for pid, title in context.all_points():
            self.point_combo.addItem(f"{title}（{pid}）", pid)
        form.addRow("点位", self.point_combo)

        self.ratio_box = QDoubleSpinBox()
        self.ratio_box.setDecimals(3)
        self.ratio_box.setRange(0.001, 100000.0)
        self.ratio_box.setValue(config.ratio)
        self.ratio_box.setToolTip("倍率：CT/PT 变比，或装置单位没换算时（例如报 0.1kWh）填 0.1")
        no_arrows(self.ratio_box)
        form.addRow("倍率", self.ratio_box)

        self.note_edit = QLineEdit(config.note)
        self.note_edit.setPlaceholderText("备注（可空）")
        form.addRow("备注", self.note_edit)

        root.addLayout(form)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._initial = config
        #: 每种方式各自记着选了哪个点，切来切去不丢
        self._points: dict[str, str] = {
            energy.MODE_POWER: config.power_point,
            energy.MODE_READING: config.reading_point,
        }
        if config.source:
            self._points[config.mode] = config.source
        self.mode_combo.currentIndexChanged.connect(lambda _i: self._sync_mode())
        self._sync_mode()

    def _current_point(self) -> str:
        point = self.point_combo.currentData() or self.point_combo.currentText().strip()
        point = str(point or "")
        if "）" in point and "（" in point:
            point = point.rsplit("（", 1)[-1].rstrip("）")
        return point

    def _sync_mode(self) -> None:
        mode = str(self.mode_combo.currentData() or energy.MODE_POWER)
        previous = getattr(self, "_shown_mode", None)
        if previous and previous != mode:
            # 切走之前先把当前选的点记在旧方式名下，切回来还能还原
            self._points[previous] = self._current_point()
        self._shown_mode = mode
        if mode == energy.MODE_READING:
            self.hint.setText("读装置的累计电量（kWh）做差值：装置换表/清零时会自动重新起算。")
        else:
            self.hint.setText("读有功功率（kW）并按采样间隔积分；采集停着的时间不计入电量。")
        preset = self._points.get(mode, "")
        index = self.point_combo.findData(preset) if preset else -1
        if index >= 0:
            self.point_combo.setCurrentIndex(index)
        elif preset:
            self.point_combo.setCurrentText(preset)

    def config(self) -> MeterConfig:
        mode = str(self.mode_combo.currentData() or energy.MODE_POWER)
        point = self._current_point()
        self._points[mode] = point
        return MeterConfig(
            name=self.name_edit.text().strip() or "电表",
            mode=mode,
            power_point=point if mode == energy.MODE_POWER else "",
            reading_point=point if mode == energy.MODE_READING else "",
            ratio=self.ratio_box.value(),
            note=self.note_edit.text().strip(),
        )

    def _accept(self) -> None:
        config = self.config()
        if not config.source:
            QMessageBox.warning(self, "还差一个点位", "请选择或填写要点位的点号。")
            return
        self.accept()


class TariffDialog(QDialog):
    """电价：单一价，或者按时段分时计价。"""

    def __init__(self, tariff: Tariff, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("电价设置")
        self.resize(680, 460)

        root = QVBoxLayout(self)

        price_row = QHBoxLayout()
        price_row.addWidget(QLabel("基础单价（元/kWh）"))
        self.price_box = QDoubleSpinBox()
        self.price_box.setDecimals(3)
        self.price_box.setRange(0.0, 100.0)
        self.price_box.setValue(tariff.price)
        self.price_box.setMinimumWidth(140)
        self.price_box.setToolTip("没有命中任何分时时段时用的单价；单一电价就是它")
        price_row.addWidget(self.price_box)
        price_row.addStretch(1)
        root.addLayout(price_row)

        mode_row = QHBoxLayout()
        self.single_radio = QRadioButton("单一电价（不分时段）")
        self.tou_radio = QRadioButton("分时电价（按下面的时段算）")
        (self.tou_radio if tariff.segmented else self.single_radio).setChecked(True)
        mode_row.addWidget(self.single_radio)
        mode_row.addWidget(self.tou_radio)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        box = QGroupBox("时段表（开始 > 结束 表示跨零点，如 23:00–07:00）")
        box_layout = QVBoxLayout(box)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["时段名", "开始 HH:MM", "结束 HH:MM", "单价 元/kWh"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        box_layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        add_button = QPushButton("加一行")
        add_button.clicked.connect(lambda: self._add_row(TariffSlot("平段", "00:00", "00:00", 0.0)))
        remove_button = QPushButton("删掉选中")
        remove_button.clicked.connect(self._remove_selected)
        preset_button = QPushButton("用默认尖峰平谷")
        preset_button.clicked.connect(self._load_preset)
        row.addWidget(add_button)
        row.addWidget(remove_button)
        row.addWidget(preset_button)
        row.addStretch(1)
        box_layout.addLayout(row)
        root.addWidget(box, 1)

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for slot in (tariff.slots or tou_tariff_slots()):
            self._add_row(slot)
        self._sync_mode()

    # -------------------------------------------------- 表格

    def _add_row(self, slot: TariffSlot) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(slot.name))
        self.table.setItem(row, 1, QTableWidgetItem(slot.start))
        self.table.setItem(row, 2, QTableWidgetItem(slot.end))
        item = QTableWidgetItem(f"{slot.price:g}")
        item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row, 3, item)

    def _remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def _load_preset(self) -> None:
        self.table.setRowCount(0)
        for slot in tou_tariff_slots():
            self._add_row(slot)
        self.tou_radio.setChecked(True)
        self._sync_mode()

    def _sync_mode(self) -> None:
        enabled = self.tou_radio.isChecked()
        self.table.setEnabled(enabled)
        self.hint.setText(
            "分时电价：落在哪个时段就按那个单价计费，分时电量也会分别累计（月末对账用）。"
            if enabled
            else "单一电价：所有电量按基础单价计费。"
        )

    # -------------------------------------------------- 结果

    def tariff(self) -> Tariff:
        base = float(self.price_box.value())
        if not self.tou_radio.isChecked():
            return energy.single_tariff(base)
        slots: list[TariffSlot] = []
        for row in range(self.table.rowCount()):
            def text(col: int) -> str:
                item = self.table.item(row, col)
                return (item.text() if item else "").strip()

            name = text(0) or "平段"
            start, end = text(1) or "00:00", text(2) or "00:00"
            try:
                price = float(text(3) or 0.0)
            except ValueError:
                price = 0.0
            slots.append(TariffSlot(name, start, end, max(0.0, price)))
        return Tariff(price=base, slots=slots)

    def _accept(self) -> None:
        tariff = self.tariff()
        for slot in tariff.slots:
            try:
                slot.start_minute
                slot.end_minute
            except ValueError as exc:
                QMessageBox.warning(self, "时间格式不对", f"{exc}（要写成 HH:MM，例如 08:30）")
                return
        if self.tou_radio.isChecked() and not tariff.slots:
            QMessageBox.warning(self, "没有时段", "分时电价至少要有一个时段，或者改成单一电价。")
            return
        self.accept()


def tou_tariff_slots() -> list[TariffSlot]:
    return [TariffSlot(name, start, end, price) for name, start, end, price in energy.TOU_SLOTS]


class EnergyWindow(QWidget):
    """电表清单 + 电价 + 电费。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.setWindowTitle(f"电能与电费 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(1180, 620)

        # 主窗口会给一个共享的记账服务；单独打开工具时（测试/命令行）自己造一个
        self._own_service = context.service("energy") is None
        if self._own_service:
            from ..energy_service import EnergyService

            self.service = EnergyService(
                project=context.project,
                data_dir=getattr(context, "data_dir", "") or context.base_dir,
                base_dir=context.base_dir,
                tag_of=context.tag_of,
                engine_running=context.engine_running,
                notify=context.notify,
                parent=self,
            )
            self.service.start()
        else:
            self.service = context.service("energy")

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        root.addWidget(self._build_tariff_row())
        root.addWidget(self._build_table(), 1)
        root.addLayout(self._build_buttons())
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.status)

        self.service.changed.connect(self.refresh)
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()
        self._update_status()

    # -------------------------------------------------- 界面

    def _build_tariff_row(self) -> QWidget:
        box = QGroupBox("电价")
        row = QHBoxLayout(box)
        row.setSpacing(8)
        self.tariff_label = QLabel("")
        self.tariff_label.setWordWrap(True)
        row.addWidget(self.tariff_label, 1)
        edit_button = QPushButton("设置电价…")
        edit_button.clicked.connect(self.edit_tariff)
        row.addWidget(edit_button)
        return box

    def _build_table(self) -> QWidget:
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels([label for _key, label in _COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(lambda _index: self.edit_meter())
        return self.table

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        add_button = QPushButton("添加电表…")
        add_button.clicked.connect(self.add_meter)
        edit_button = QPushButton("编辑…")
        edit_button.clicked.connect(self.edit_meter)
        remove_button = QPushButton("删除")
        remove_button.clicked.connect(self.remove_meter)
        reset_button = QPushButton("清零今日")
        reset_button.setToolTip("只清「今日」这一栏（不影响累计与本月）")
        reset_button.clicked.connect(lambda: self.reset("day"))
        reset_month = QPushButton("清零本月")
        reset_month.clicked.connect(lambda: self.reset("month"))
        reset_all = QPushButton("全部清零")
        reset_all.setToolTip("累计电量、电费、峰值一起清（换表/新装时用）")
        reset_all.clicked.connect(lambda: self.reset("total"))
        export_button = QPushButton("导出 CSV…")
        export_button.clicked.connect(self.export_csv)
        save_button = QPushButton("立即保存")
        save_button.clicked.connect(self.save_now)
        for widget in (add_button, edit_button, remove_button):
            row.addWidget(widget)
        row.addSpacing(12)
        for widget in (reset_button, reset_month, reset_all):
            row.addWidget(widget)
        row.addStretch(1)
        row.addWidget(export_button)
        row.addWidget(save_button)
        return row

    # -------------------------------------------------- 刷新

    def refresh(self) -> None:
        try:
            rows = self.service.rows()
            tariff_label = self.service.book.tariff.describe()
            totals = self.service.totals()
        except RuntimeError:  # 窗口正在销毁
            return
        self.tariff_label.setText(tariff_label)

        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, (key, _label) in enumerate(_COLUMNS):
                if key in _NUMERIC:
                    raw = row.get(key)
                    if raw is None:
                        text = "--"
                    elif key in ("cost_day", "cost_month", "cost_total"):
                        text = energy.format_money(raw)
                    elif key in ("peak_kw", "power_kw"):
                        text = energy.format_power(raw)
                    elif key == "price":
                        text = f"{raw:.4g}"
                    else:
                        text = energy.format_kwh(raw)
                else:
                    text = str(row.get(key, ""))
                item = QTableWidgetItem(text)
                if key in _NUMERIC:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                elif key == "mode":
                    item.setToolTip(str(row.get("mode_name", text)))
                elif text:
                    item.setToolTip(text)  # 列被挤窄时悬停能看全
                self.table.setItem(row_index, column, item)

        if rows:
            self._summary = (
                "合计："
                f"当前 {energy.format_power(totals['power_kw'])} kW　"
                f"今日 {energy.format_kwh(totals['forward_day'])} kWh"
                f"（{energy.format_money(totals['cost_day'])}）　"
                f"本月 {energy.format_kwh(totals['forward_month'])} kWh"
                f"（{energy.format_money(totals['cost_month'])}）　"
                f"累计 {energy.format_kwh(totals['forward_total'])} kWh"
                f"（{energy.format_money(totals['cost_total'])}）"
                f"　｜　账本：{self.service.path or '未保存'}"
            )
        else:
            self._summary = (
                "还没有电表：点「添加电表…」挑一个功率点（kW）或累计电量点（kWh）。"
                f"　账本：{self.service.path or '未保存'}"
            )
        self._update_status()

    def _update_status(self) -> None:
        hint = "" if self.service.engine_running() else "　（采集未运行，电费不增长）"
        self.status.setText(f"{getattr(self, '_summary', '')}{hint}")
        if self.service.book.tariff.segmented:
            period, price = self.service.book.tariff.price_at(datetime.now())
            self.tariff_label.setToolTip(f"当前时段 {period}：{price:g} 元/kWh")

    # -------------------------------------------------- 操作

    def _selected(self) -> int:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else -1

    def add_meter(self) -> None:
        suggestion = energy.suggest_meter(self.context.project)
        dialog = MeterDialog(self.context, suggestion, self)
        fit_to_available_screen(dialog)
        if dialog.exec() != QDialog.Accepted:
            return
        meter = self.service.add_meter(dialog.config())
        self.context.report(f"已添加电表：{meter.config.name}（{meter.config.source}）")
        self.refresh()

    def edit_meter(self) -> None:
        index = self._selected()
        if index < 0:
            QMessageBox.information(self, "先选一块表", "请在列表里选中要编辑的电表。")
            return
        meter = self.service.book.meters[index]
        dialog = MeterDialog(self.context, meter.config, self)
        fit_to_available_screen(dialog)
        if dialog.exec() != QDialog.Accepted:
            return
        self.service.update_meter(index, dialog.config())
        self.refresh()

    def remove_meter(self) -> None:
        index = self._selected()
        if index < 0:
            QMessageBox.information(self, "先选一块表", "请在列表里选中要删除的电表。")
            return
        name = self.service.book.meters[index].config.name
        answer = QMessageBox.question(
            self, "删除电表", f"删除「{name}」？它的累计电量与电费记录也会一起删除。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.service.remove_meter(index)
        self.refresh()

    def reset(self, scope: str) -> None:
        label = energy.SCOPE_LABELS.get(scope, scope)
        answer = QMessageBox.question(
            self, f"清零{label}",
            f"把{label}电量与电费清零？（建议先导出 CSV 留档）",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        count = self.service.reset(scope)
        self.context.report(f"已清零{label}：{count} 块表")
        self.refresh()

    def edit_tariff(self) -> None:
        dialog = TariffDialog(self.service.book.tariff, self)
        fit_to_available_screen(dialog)
        if dialog.exec() != QDialog.Accepted:
            return
        self.service.set_tariff(dialog.tariff())
        self.context.report(f"电价已更新：{self.service.book.tariff.describe()}")
        self.refresh()

    def export_csv(self) -> None:
        default = self.context.output_path(
            "energy", f"电能电费_{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
        )
        path, _filter = QFileDialog.getSaveFileName(
            self, "导出电能电费表", default, "CSV 表格 (*.csv);;所有文件 (*.*)"
        )
        if not path:
            return
        try:
            saved = self.service.export_csv(path)
        except OSError as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
            return
        self.context.report(f"电能电费表已导出：{saved}")
        QMessageBox.information(self, "已导出", saved)

    def save_now(self) -> None:
        path = self.service.save(force=True)
        self.context.report(f"电能账本已保存：{path}")
        self.refresh()

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        self.service.changed.disconnect(self.refresh)
        if self._own_service:
            self.service.stop()
        super().closeEvent(event)


@register_tool
class EnergyTool(WindowTool):
    name = "电能与电费"
    group = "计算"
    order = 12
    description = "实时功率、累计电量，按单一或分时电价算电费"

    def build(self, context: ToolContext) -> QWidget:
        return EnergyWindow(context)


__all__ = [
    "EnergyTool",
    "EnergyWindow",
    "MeterDialog",
    "TariffDialog",
    "tou_tariff_slots",
]
