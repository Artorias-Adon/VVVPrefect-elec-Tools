"""连通性测试：每种通信方式都真连一次（TCP / Modbus / CAN / 61850 / S7）。

界面只负责填参数、跑测试、显示结论；怎么连、怎么判断通不通都在
:mod:`.core.connectivity` 里（不依赖 Qt，可单测）。

两个现场常用的便利：

* **从工程通道填入** —— 选一条工程里已经配好的通道，参数、通信方式、从站号
  自动带过来，不用照着设备页再抄一遍（抄错一个波特率能查半天）。
* **保存结果** —— 结果按工程的输出位置存成文本，下次回现场前还能翻出上次是什么样。
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core import connectivity
from ...core.connectivity import CheckResult
from ...core.net_check import parse_hex_payload
from .. import theme
from ..dialogs.sizing import scrollable_page
from ..option_fields import make_option_widget, options_from_widgets
from ..spin import no_arrows
from .base import DialogTool, ToolContext, register_tool

_HINT_STYLE = f"color: {theme.TEXT_DIM}; font-size: 11px;"


class _CheckWorker(QObject):
    """把测试放到后台线程：连接超时可能几秒，界面不能卡住。"""

    finished = Signal(object)  # CheckResult

    def __init__(self, key: str, kwargs: dict) -> None:
        super().__init__()
        self.key = key
        self.kwargs = kwargs
        self.stop = threading.Event()

    def run(self) -> None:
        try:
            result = connectivity.run_check(
                self.key, should_stop=self.stop.is_set, **self.kwargs
            )
        except Exception as exc:  # 兜住意外异常，别让线程静默死掉
            result = CheckResult(
                key=self.key,
                target="",
                steps=[
                    connectivity.CheckStep(
                        "执行测试", connectivity.STEP_FAIL, f"{exc.__class__.__name__}: {exc}"
                    )
                ],
            )
        self.finished.emit(result)


class ConnectivityDialog(QDialog):
    """按通信方式测连通性（TCP 端口 / Modbus / CAN / 61850 / S7 …）。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self._thread: QThread | None = None
        self._worker: _CheckWorker | None = None
        #: 每种通信方式上次填的值 ``{协议: {参数: 值}}``，切来切去不丢
        self._values: dict[str, dict] = {}
        self._field_widgets: dict[str, QWidget] = {}
        #: 当前这组控件是按哪种通信方式造出来的（切协议时信号先到、重建在后，
        #: 必须用它去读控件，用新的协议去读会张冠李戴）
        self._built_key: str | None = None
        self._result: CheckResult | None = None

        self.setWindowTitle("连通性测试")
        self.setMinimumSize(700, 560)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        page = QWidget()
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 6, 0)
        page_layout.setSpacing(10)
        page_layout.addWidget(self._build_protocol())
        page_layout.addWidget(self._build_source())
        page_layout.addWidget(self._build_params())
        page_layout.addWidget(self._build_payload())
        page_layout.addWidget(self._build_result())
        # 小屏幕上参数表单很长，整页可滚动才点得到下面的按钮
        self._page = scrollable_page(page)
        root.addWidget(self._page, 1)

        buttons = QDialogButtonBox()
        self.run_button = buttons.addButton("开始测试", QDialogButtonBox.ActionRole)
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self.start_test)
        self.save_button = buttons.addButton("保存结果", QDialogButtonBox.ActionRole)
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_result)
        buttons.addButton("关闭", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        root.addWidget(buttons)

        self._rebuild_params()

    # -------------------------------------------------- 界面

    def _build_protocol(self) -> QWidget:
        box = QGroupBox("测什么")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(QLabel("通信方式"))
        self.protocol_combo = QComboBox()
        for spec in connectivity.PROTOCOLS:
            self.protocol_combo.addItem(spec.label, spec.key)
            self.protocol_combo.setItemData(
                self.protocol_combo.count() - 1, spec.hint, Qt.ToolTipRole
            )
        self.protocol_combo.currentIndexChanged.connect(self._on_protocol_changed)
        row.addWidget(self.protocol_combo, 1)
        layout.addLayout(row)

        self.protocol_hint = QLabel()
        self.protocol_hint.setWordWrap(True)
        self.protocol_hint.setStyleSheet(_HINT_STYLE)
        layout.addWidget(self.protocol_hint)
        return box

    def _build_source(self) -> QWidget:
        box = QGroupBox("从工程通道填入（可选）")
        row = QHBoxLayout(box)
        row.setSpacing(6)
        self.channel_combo = QComboBox()
        self.channel_combo.addItem("（手工填写参数）", None)
        for label, key, options, unit in self._channel_entries():
            self.channel_combo.addItem(label, (key, options, unit))
        self.channel_combo.currentIndexChanged.connect(self._on_channel_picked)
        row.addWidget(self.channel_combo, 1)
        return box

    def _build_params(self) -> QWidget:
        self.params_box = QGroupBox("连接参数")
        self.params_layout = QVBoxLayout(self.params_box)
        self.params_layout.setContentsMargins(9, 6, 9, 6)
        return self.params_box

    def _build_payload(self) -> QWidget:
        self.payload_box = QGroupBox("发送内容（可留空：只测连接）")
        layout = QVBoxLayout(self.payload_box)
        layout.setSpacing(6)

        hint = QLabel(
            "十六进制，可带空格或 0x。例（读保持寄存器，带 CRC）：01 03 00 00 00 0A C5 CD"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(_HINT_STYLE)
        layout.addWidget(hint)

        self.payload_edit = QPlainTextEdit()
        self.payload_edit.setPlaceholderText("留空 = 只测端口通不通")
        self.payload_edit.setFixedHeight(64)
        layout.addWidget(self.payload_edit)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.wait_box = QCheckBox("发送后等回应")
        self.wait_box.setChecked(True)
        row.addWidget(self.wait_box)

        row.addWidget(QLabel("等"))
        self.read_timeout_box = QSpinBox()
        self.read_timeout_box.setRange(1, 60)
        self.read_timeout_box.setValue(3)
        self.read_timeout_box.setSuffix(" 秒")
        no_arrows(self.read_timeout_box)
        row.addWidget(self.read_timeout_box)

        row.addWidget(QLabel("重复"))
        self.repeat_box = QSpinBox()
        self.repeat_box.setRange(1, 20)
        self.repeat_box.setValue(1)
        self.repeat_box.setSuffix(" 次")
        self.repeat_box.setToolTip("连多次能看出抖动：偶发失败现场很常见")
        no_arrows(self.repeat_box)
        row.addWidget(self.repeat_box)
        row.addStretch(1)
        layout.addLayout(row)
        return self.payload_box

    def _build_result(self) -> QWidget:
        box = QGroupBox("结果")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)
        self.result_label = QLabel("尚未测试")
        self.result_label.setWordWrap(True)
        self.result_label.setTextFormat(Qt.RichText)
        layout.addWidget(self.result_label)

        self.detail_edit = QPlainTextEdit()
        self.detail_edit.setReadOnly(True)
        self.detail_edit.setMinimumHeight(170)
        self.detail_edit.setPlaceholderText("测试明细会显示在这里")
        layout.addWidget(self.detail_edit, 1)
        return box

    # -------------------------------------------------- 参数区

    def current_key(self) -> str:
        return str(self.protocol_combo.currentData() or connectivity.PROTOCOLS[0].key)

    def _values_for(self, key: str) -> dict:
        values = self._values.get(key)
        if values is None:
            values = connectivity.default_options_for(key)
            self._values[key] = values
        return values

    def _rebuild_params(self) -> None:
        key = self.current_key()
        spec = connectivity.protocol_spec(key)
        values = self._values_for(key)

        while self.params_layout.count():
            item = self.params_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        self._field_widgets = {}
        self.unit_box: QSpinBox | None = None
        holder = QWidget()
        form = QFormLayout(holder)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for field in connectivity.option_fields(key):
            widget = make_option_widget(field, values.get(field.key))
            self._field_widgets[field.key] = widget
            form.addRow(field.label, widget)

        if spec.needs_unit:
            self.unit_box = QSpinBox()
            self.unit_box.setRange(1, 247)
            self.unit_box.setValue(int(values.get("unit_id", 1) or 1))
            self.unit_box.setToolTip("从站号：和仪表/设备里设的站号一致，错了会一直没数据")
            no_arrows(self.unit_box)
            form.addRow("从站号", self.unit_box)

        self.params_layout.addWidget(holder)

        self._built_key = key
        self.protocol_hint.setText(spec.hint)
        self.payload_box.setVisible(not spec.driver)
        self.result_label.setText(f"尚未测试（{spec.label}）")
        self.detail_edit.clear()
        self._result = None
        self.save_button.setEnabled(False)

    def _snapshot(self) -> None:
        """把当前界面上的值记进 ``_values``，切协议再切回来不丢。"""
        key = self._built_key or self.current_key()
        if not self._field_widgets:
            return
        spec = connectivity.protocol_spec(key)
        schema = connectivity.option_fields(key)
        values = options_from_widgets(self._field_widgets, schema)
        if spec.needs_unit and self.unit_box is not None:
            values["unit_id"] = self.unit_box.value()
        self._values[key] = values

    def _on_protocol_changed(self) -> None:
        if self._field_widgets:
            self._snapshot()
        self._rebuild_params()

    def _channel_entries(self) -> list[tuple[str, str, dict, int]]:
        """工程里能对应的通道 ``[(显示名, 协议, 参数, 从站号)]``。"""
        entries: list[tuple[str, str, dict, int]] = []
        for channel in self.context.project.channels:
            spec = connectivity.spec_for_channel(channel.driver, channel.link)
            if spec is None:
                continue
            unit = next(
                (
                    device.unit_id
                    for device in self.context.project.devices
                    if device.channel == channel.name
                ),
                1,
            )
            label = f"{channel.name}（{spec.label} {connectivity.target_text(spec.key, channel.options)}）"
            entries.append((label, spec.key, dict(channel.options), unit))
        return entries

    def _on_channel_picked(self) -> None:
        data = self.channel_combo.currentData()
        if not data:
            return
        key, options, unit = data
        spec = connectivity.protocol_spec(key)
        index = self.protocol_combo.findData(key)
        if index >= 0 and index != self.protocol_combo.currentIndex():
            if self._field_widgets:
                self._snapshot()
            self.protocol_combo.blockSignals(True)
            self.protocol_combo.setCurrentIndex(index)
            self.protocol_combo.blockSignals(False)
            self._rebuild_params()

        values = self._values_for(key)
        values.update({k: v for k, v in options.items() if k in {f.key for f in connectivity.option_fields(key)}})
        if spec.needs_unit:
            values["unit_id"] = int(unit or 1)
        self._values[key] = values
        self._fill_widgets(key, values)

    def _fill_widgets(self, key: str, values: dict) -> None:
        for field in connectivity.option_fields(key):
            widget = self._field_widgets.get(field.key)
            if widget is None or field.key not in values:
                continue
            _set_widget_value(widget, field, values[field.key])
        if self.unit_box is not None:
            self.unit_box.setValue(int(values.get("unit_id", 1) or 1))

    # -------------------------------------------------- 测试

    def test_kwargs(self) -> dict:
        """按当前通信方式凑出 :func:`connectivity.run_check` 的参数。"""
        key = self.current_key()
        spec = connectivity.protocol_spec(key)
        self._snapshot()
        values = dict(self._values_for(key))
        unit_id = int(values.pop("unit_id", 1) or 1)
        kwargs: dict = {"options": values}
        if spec.needs_unit:
            kwargs["unit_id"] = unit_id
        if not spec.driver:
            payload, error = parse_hex_payload(self.payload_edit.toPlainText())
            if error:
                raise ValueError(error)
            kwargs["payload"] = payload
            kwargs["wait_response"] = self.wait_box.isChecked()
            kwargs["attempts"] = self.repeat_box.value()
            kwargs["read_timeout_s"] = float(self.read_timeout_box.value())
        return kwargs

    def start_test(self) -> None:
        if self._thread is not None:
            return  # 上一次还在跑
        try:
            kwargs = self.test_kwargs()
        except ValueError as exc:
            self.result_label.setText(
                f"<span style='color:{theme.QUALITY_BAD}'>{exc}</span>"
            )
            return

        self.run_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.result_label.setText("测试中…")
        self.detail_edit.clear()

        self._thread = QThread(self)
        self._worker = _CheckWorker(self.current_key(), kwargs)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()

    def _on_finished(self, result: CheckResult) -> None:
        self._result = result
        self._teardown()
        colour = theme.QUALITY_GOOD if result.ok else theme.QUALITY_BAD
        if result.ok and result.warnings:
            colour = theme.QUALITY_UNCERTAIN
        self.result_label.setText(f"<span style='color:{colour}'><b>{result.headline}</b></span>")
        self.detail_edit.setPlainText(result.detail())
        self.save_button.setEnabled(True)
        self._scroll_to_result()
        level = "info" if result.ok else "warning"
        self.context.report(f"连通性测试 {result.label} {result.target} —— {result.headline}", level)
        if self.context.settings is not None and not result.ok:
            self.context.announce("连通性测试未通过", result.headline, "warning")

    def _teardown(self) -> None:
        if self._worker is not None:
            self._worker.stop.set()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(6000)
            self._thread = None
        self._worker = None
        self.run_button.setEnabled(True)

    def _scroll_to_result(self) -> None:
        """参数表单长的时候，测完自动滚到结果那一段，不用自己找。"""
        bar = self._page.verticalScrollBar()
        bar.setValue(bar.maximum())

    def save_result(self) -> None:
        if self._result is None:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"连通性测试_{self._result.label}_{stamp}.txt"
        try:
            path = self.context.output_path("connectivity", name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self._result.as_text())
        except OSError as exc:
            self.result_label.setText(
                f"<span style='color:{theme.QUALITY_BAD}'>保存失败：{exc}</span>"
            )
            return
        self.context.report(f"连通性测试结果已保存：{path}")
        self.result_label.setText(
            f"{self.result_label.text()}<br><span style='color:{theme.TEXT_DIM}'>已保存：{path}</span>"
        )

    def done(self, code: int) -> None:  # noqa: D102
        self._teardown()
        super().done(code)


def _set_widget_value(widget: QWidget, field, value) -> None:
    """把值填进 :func:`make_option_widget` 造出来的控件。"""
    try:
        if field.type == "bool":
            widget.setChecked(bool(value))
        elif field.type in ("int", "float"):
            widget.setValue(type(widget.value())(value))
        elif field.type == "choice":
            index = widget.findData(value)
            if index >= 0:
                widget.setCurrentIndex(index)
        elif field.type == "port":
            widget.setCurrentText(str(value))
        else:
            widget.setText(str(value))
    except (TypeError, ValueError):  # 值不合类型就保留默认，不打断用户
        pass


@register_tool
class ConnectivityTool(DialogTool):
    name = "连通性测试"
    group = "通信"
    order = 10
    description = "TCP / Modbus / CAN / 61850 / S7 每种通信方式都能测通不通"

    def build(self, context: ToolContext) -> QDialog:
        return ConnectivityDialog(context)
