"""连接参数表单控件（驱动 ``option_schema`` -> Qt 控件）。

抽出来是因为**两处**都要编辑通道的连接参数：
「设备与点位」页的通道表单，与设置里的「通信」页。
两边共用同一套实现，免得加一个协议参数要改两个地方，
也保证两处对参数的默认值、类型转换、非法值回退行为完全一致。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from ..core import serial_ports
from ..core.drivers import OptionField


class SerialPortCombo(QComboBox):
    """串口号输入框：可以下拉选（每次打开下拉都重新枚举），也可以手打。

    为什么不用「下拉框 + 检测按钮」：现场插拔 USB 转串口很频繁，
    点开下拉自动重扫最省事，而且手打的串口名（有些板子的虚拟串口枚举不出来）
    也不会被下次重扫冲掉。
    """

    def __init__(self, current: str = "") -> None:
        super().__init__()
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.NoInsert)
        self._fallback = (current or "").strip()
        self.setMinimumWidth(130)
        self.refresh()

    def refresh(self) -> None:
        """重新枚举串口，并保留当前填的值。"""
        typed = self.currentText().strip() or self._fallback
        ports = serial_ports.list_serial_ports()
        self.blockSignals(True)
        self.clear()
        for port in ports:
            self.addItem(port.device, port.device)
            self.setItemData(
                self.count() - 1,
                f"{port.device}　{port.description}" if port.description else port.device,
                Qt.ToolTipRole,
            )
        if typed and self.findText(typed) < 0:
            # 手打的、或当前工程配的串口（可能设备没插）也要保留，别被重扫冲掉
            self.addItem(typed, typed)
            self.setItemData(
                self.count() - 1,
                f"{typed}（系统当前没枚举到这个串口，仍按填写值使用）",
                Qt.ToolTipRole,
            )
        self.blockSignals(False)
        if typed:
            # 索引要跟着文本走：索引和文本不一致时，用户从下拉里选中当前索引那一行
            # 不会发出任何信号（索引没变），那次选择就被吞掉了。
            index = self.findText(typed)
            if index >= 0:
                self.setCurrentIndex(index)
            else:  # pragma: no cover - 上面的分支一定会把 typed 加进下拉
                self.setCurrentText(typed)
        elif ports:
            self.setCurrentIndex(0)

    def showPopup(self) -> None:  # noqa: item2 - Qt 命名
        self.refresh()
        super().showPopup()

    def setToolTipExtra(self, text: str) -> None:
        ports = serial_ports.list_serial_ports()
        detail = "、".join(port.device for port in ports) if ports else "没有枚举到串口"
        self.setToolTip(f"{text}\n当前系统：{detail}\n点开下拉会重新检测，也可以直接手打串口号")


def make_option_widget(field: OptionField, current: Any = None) -> QWidget:
    """按参数规格造一个编辑控件。"""
    value = field.default if current is None else current

    if field.type == "choice":
        widget = QComboBox()
        labels = field.choice_labels
        for index, option in enumerate(field.choices):
            label = labels[index] if index < len(labels) else str(option)
            widget.addItem(label, option)
        index = widget.findData(value)
        if index < 0:
            index = widget.findData(field.default)
        widget.setCurrentIndex(index if index >= 0 else 0)
    elif field.type == "port":
        widget = SerialPortCombo(str(value or field.default))
    elif field.type in ("int", "float"):
        widget = QSpinBox() if field.type == "int" else QDoubleSpinBox()
        if field.type == "float":
            widget.setDecimals(3)
        low = int(field.minimum) if field.minimum is not None else -1_000_000
        high = int(field.maximum) if field.maximum is not None else 1_000_000
        widget.setRange(low, high)
        try:
            widget.setValue(type(value)(value))
        except (TypeError, ValueError):
            widget.setValue(type(field.default)(field.default))
    elif field.type == "bool":
        widget = QCheckBox()
        widget.setChecked(bool(value))
    else:
        widget = QLineEdit(str(value))

    if field.hint:
        widget.setToolTip(field.hint)
        if isinstance(widget, SerialPortCombo):
            widget.setToolTipExtra(field.hint)
    return widget


def apply_signals(widget: QWidget) -> list:
    """编辑这个控件后应当触发一次「应用」的信号。

    选择类（下拉 / 勾选）选完就生效；文本框在编辑结束（回车或失焦）时生效。
    可手打的串口下拉**两个都要接**：从下拉里选会改索引，手打不改索引，
    只接 ``currentIndexChanged`` 的话手打的串口号永远写不进工程。
    """
    if isinstance(widget, SerialPortCombo):
        line = widget.lineEdit()
        signals = [widget.currentIndexChanged]
        if line is not None:
            signals.append(line.editingFinished)
        return signals
    if isinstance(widget, QComboBox):
        return [widget.currentIndexChanged]
    if isinstance(widget, QCheckBox):
        return [widget.toggled]
    return [widget.editingFinished]


def read_option_widget(widget: QWidget, field: OptionField) -> Any:
    """从控件读出原始值。"""
    if field.type == "bool":
        return widget.isChecked()
    if field.type in ("int", "float"):
        return widget.value()
    if field.type == "choice":
        return widget.currentData()
    if field.type == "port":
        # 串口号允许手打，所以取文本框里的内容
        return widget.currentText().strip()
    return widget.text()


def coerce_option(value: Any, field: OptionField) -> Any:
    """把值转成参数规格声明的类型，转不了就退回默认值。

    读文件/表单都可能拿到不合类型的值（例如手改工程文件把端口写成字符串），
    这里兜住，避免驱动拿到怪类型后在现场报看不懂的错。
    """
    if value is None:
        return field.default
    try:
        if field.type == "float":
            return float(value)
        if field.type == "int":
            return int(value)
        if field.type == "bool":
            return bool(value)
        if field.type in ("str", "port"):
            return str(value)
        return type(field.default)(value) if field.default not in ("", None) else str(value)
    except (TypeError, ValueError):
        return field.default


def options_from_widgets(
    widgets: dict[str, QWidget], schema: list[OptionField]
) -> dict[str, Any]:
    """把一组控件读成 ``options`` 字典。"""
    options: dict[str, Any] = {}
    for field in schema:
        widget = widgets.get(field.key)
        if widget is None:
            options[field.key] = field.default
            continue
        options[field.key] = coerce_option(read_option_widget(widget, field), field)
    return options
