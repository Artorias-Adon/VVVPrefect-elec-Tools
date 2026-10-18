"""主界面右键菜单用到的小对话框：模块下面显示哪些量、开关操作点、故障信息。

这些对话框改的是**工程里的图元定义**（``DiagramNode``），所以都在"确定"里
直接回写 dataclass 字段，由调用方负责标记工程已修改。
"""

from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.models import DiagramNode, DiagramValue
from .. import theme
from ..spin import no_arrows

#: 一个图元下面最多挂几个量（再多就压到别的图元上了）
MAX_VALUES = 4


class NodeValuesDialog(QDialog):
    """模块下面显示哪几个实时量（电压 / 电流 / 功率 / 电度…）。"""

    def __init__(
        self,
        node: DiagramNode,
        points: Iterable[tuple[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"模块下面显示什么 · {node.label or node.id}")
        self.resize(760, 380)
        self._points = list(points)

        root = QVBoxLayout(self)
        hint = QLabel(
            "在这里挑要点亮显示的量（最多 %d 个）。显示名留空就用点位名；"
            "单位和小数位只影响这一行怎么显示，不改点位定义。" % MAX_VALUES
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(hint)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["点位", "显示名", "单位", "小数位"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        add_button = QPushButton("加一行")
        add_button.clicked.connect(lambda: self._add_row(DiagramValue()))
        remove_button = QPushButton("删掉选中")
        remove_button.clicked.connect(self._remove_selected)
        row.addWidget(add_button)
        row.addWidget(remove_button)
        row.addStretch(1)
        root.addLayout(row)

        self.empty_hint = QLabel("")
        self.empty_hint.setWordWrap(True)
        self.empty_hint.setStyleSheet(f"color: {theme.QUALITY_UNCERTAIN}; font-size: 11px;")
        root.addWidget(self.empty_hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for value in node.values:
            self._add_row(value)

    # -------------------------------------------------- 表格

    def _point_combo(self, current: str) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)  # 点号也能手打（点还没建好、或拼别的设备的点）
        combo.setInsertPolicy(QComboBox.NoInsert)
        combo.addItem("", "")
        for pid, title in self._points:
            combo.addItem(f"{title}（{pid}）", pid)
        if current:
            index = combo.findData(current)
            if index >= 0:
                combo.setCurrentIndex(index)
            else:
                combo.addItem(current, current)
                combo.setCurrentIndex(combo.count() - 1)
        return combo

    def _add_row(self, value: DiagramValue) -> None:
        if self.table.rowCount() >= MAX_VALUES:
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setCellWidget(row, 0, self._point_combo(value.point))

        label_edit = QLineEdit(value.label)
        label_edit.setPlaceholderText("留空 = 用点位名")
        self.table.setCellWidget(row, 1, label_edit)

        unit_edit = QLineEdit(value.unit)
        unit_edit.setPlaceholderText("kV / A / kW")
        unit_edit.setMaximumWidth(90)
        self.table.setCellWidget(row, 2, unit_edit)

        decimals = QSpinBox()
        decimals.setRange(0, 6)
        decimals.setValue(max(0, min(6, int(value.decimals))))
        decimals.setMaximumWidth(70)
        no_arrows(decimals)
        self.table.setCellWidget(row, 3, decimals)

    def _remove_selected(self) -> None:
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    # -------------------------------------------------- 结果

    def values(self) -> list[DiagramValue]:
        out: list[DiagramValue] = []
        for row in range(self.table.rowCount()):
            combo = self.table.cellWidget(row, 0)
            if combo is None:
                continue
            point = combo.currentData() or combo.currentText().strip()
            point = str(point or "").strip()
            if "）" in point and "（" in point:  # 手打整串「标题（点号）」
                point = point.rsplit("（", 1)[-1].rstrip("）")
            if not point:
                continue
            label_widget = self.table.cellWidget(row, 1)
            unit_widget = self.table.cellWidget(row, 2)
            decimals_widget = self.table.cellWidget(row, 3)
            out.append(
                DiagramValue(
                    point=point,
                    label=label_widget.text().strip() if isinstance(label_widget, QLineEdit) else "",
                    unit=unit_widget.text().strip() if isinstance(unit_widget, QLineEdit) else "",
                    decimals=decimals_widget.value() if isinstance(decimals_widget, QSpinBox) else 1,
                )
            )
        return out

    def _accept(self) -> None:
        values = self.values()
        if self.table.rowCount() and not values:
            QMessageBox.warning(self, "没选点位", "每一行都要选一个点位，或者把空行删掉。")
            return
        self.accept()


class NodeSwitchDialog(QDialog):
    """这个图元是不是开关：合闸/分闸写哪个点、状态从哪个点读回来。"""

    def __init__(
        self,
        node: DiagramNode,
        points: Iterable[tuple[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"开关操作点 · {node.label or node.id}")
        self.setMinimumWidth(620)
        self._points = [("", "（不用）")] + list(points)

        root = QVBoxLayout(self)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.switch_combo = QComboBox()
        self.switch_combo.setEditable(True)
        self.switch_combo.setInsertPolicy(QComboBox.NoInsert)
        for pid, title in self._points:
            self.switch_combo.addItem(title if not pid else f"{title}（{pid}）", pid)
        self._select(self.switch_combo, node.switch_point)
        self.switch_combo.setToolTip(
            "右键菜单里的「合闸 / 分闸」就写这个点。必须是可写的线圈或保持寄存器"
        )
        form.addRow("开关操作点", self.switch_combo)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.on_box = QDoubleSpinBox()
        self.on_box.setDecimals(0)
        self.on_box.setRange(-1e6, 1e6)
        self.on_box.setValue(float(node.switch_on))
        no_arrows(self.on_box)
        self.off_box = QDoubleSpinBox()
        self.off_box.setDecimals(0)
        self.off_box.setRange(-1e6, 1e6)
        self.off_box.setValue(float(node.switch_off))
        no_arrows(self.off_box)
        row.addWidget(QLabel("合闸写"))
        row.addWidget(self.on_box)
        row.addWidget(QLabel("分闸写"))
        row.addWidget(self.off_box)
        row.addStretch(1)
        form.addRow("写入值", row)

        self.state_combo = QComboBox()
        self.state_combo.setEditable(True)
        self.state_combo.setInsertPolicy(QComboBox.NoInsert)
        for pid, title in self._points:
            self.state_combo.addItem(title if not pid else f"{title}（{pid}）", pid)
        self._select(self.state_combo, node.state_point)
        self.state_combo.setToolTip(
            "读回来的状态（0/1）：配了它，图元合闸时画绿色、分闸画灰色，右键也能看当前状态"
        )
        form.addRow("开关状态点", self.state_combo)

        self.state_on_box = QDoubleSpinBox()
        self.state_on_box.setDecimals(0)
        self.state_on_box.setRange(-1e6, 1e6)
        self.state_on_box.setValue(float(node.state_on))
        no_arrows(self.state_on_box)
        form.addRow("状态=合闸当值", self.state_on_box)

        self.state_bit_box = QSpinBox()
        self.state_bit_box.setRange(-1, 31)
        self.state_bit_box.setValue(int(node.state_bit))
        self.state_bit_box.setSpecialValueText("（按值比较）")
        self.state_bit_box.setToolTip(
            "状态点是位域（遥信字）时填第几位算合位，例如断路器位置字里 bit0=合位、bit1=分位就填 0；\n"
            "填 -1 表示直接拿整值和上面的「状态=合闸当值」比"
        )
        no_arrows(self.state_bit_box)
        form.addRow("状态位", self.state_bit_box)

        root.addLayout(form)

        hint = QLabel(
            "提示：写入仍然走软件的写值通道（会检查采集是否在运行、点位是否可写、限幅），"
            "不是绕过安全直接发报文。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(hint)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _select(combo: QComboBox, pid: str) -> None:
        if not pid:
            combo.setCurrentIndex(0)
            return
        index = combo.findData(pid)
        if index >= 0:
            combo.setCurrentIndex(index)
        else:
            combo.addItem(pid, pid)
            combo.setCurrentIndex(combo.count() - 1)

    def apply_to(self, node: DiagramNode) -> None:
        node.switch_point = _point_of(self.switch_combo)
        node.state_point = _point_of(self.state_combo)
        node.switch_on = float(self.on_box.value())
        node.switch_off = float(self.off_box.value())
        node.state_on = float(self.state_on_box.value())
        node.state_bit = int(self.state_bit_box.value())


def _point_of(combo: QComboBox) -> str:
    """下拉里显示的是「标题（点号）」，这里取回真正的点号（占位项返回空串）。"""
    data = combo.currentData()
    point = str(data).strip() if data is not None else combo.currentText().strip()
    if not point:
        return ""
    if "）" in point and "（" in point:
        point = point.rsplit("（", 1)[-1].rstrip("）")
    return point


FAULT_COLUMNS = ("时间", "设备", "点号", "级别", "描述", "数值", "状态")
_LEVEL_LABELS = {"critical": "故障", "warning": "报警", "info": "提示"}


class FaultInfoDialog(QDialog):
    """某个模块相关的故障 / 报警 / 通讯状态。"""

    def __init__(
        self,
        title: str,
        rows: list[dict[str, Any]],
        devices: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"故障信息 · {title}")
        self.resize(860, 380)

        root = QVBoxLayout(self)
        subtitle = f"相关设备：{'、'.join(devices)}" if devices else "未关联设备（显示全部未复位报警）"
        self.headline = QLabel("")
        self.headline.setWordWrap(True)
        root.addWidget(self.headline)
        hint = QLabel(subtitle)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.table = QTableWidget(len(rows), len(FAULT_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(FAULT_COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        for column, width in ((0, 150), (1, 120), (2, 150), (3, 70), (5, 90), (6, 80)):
            self.table.setColumnWidth(column, width)
        for row_index, row in enumerate(rows):
            level = str(row.get("level", ""))
            cells = [
                str(row.get("time", "")),
                str(row.get("device", "")),
                str(row.get("tag", "")),
                _LEVEL_LABELS.get(level, level),
                str(row.get("message", "")),
                "" if row.get("value") is None else str(row.get("value")),
                str(row.get("state", "")),
            ]
            colour = theme.ALARM_LEVELS.get(level, theme.TEXT)
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setForeground(QColor(colour))
                if column in (3, 5, 6):
                    item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row_index, column, item)
        root.addWidget(self.table, 1)

        if rows:
            self.headline.setText(f"<b>共 {len(rows)} 条</b>")
            self.headline.setStyleSheet(f"color: {theme.QUALITY_BAD};")
        else:
            self.headline.setText("<b>没有未复位的故障 / 报警，通讯也正常</b>")
            self.headline.setStyleSheet(f"color: {theme.QUALITY_GOOD};")

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(lambda _button: self.accept())
        root.addWidget(buttons)


__all__ = [
    "FAULT_COLUMNS",
    "FaultInfoDialog",
    "MAX_VALUES",
    "NodeSwitchDialog",
    "NodeValuesDialog",
]
