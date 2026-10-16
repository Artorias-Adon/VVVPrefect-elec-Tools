"""触发条件的界面控件（截图、数据记录、录音、故障录波共用）。

把"选点位 + 选比较方式 + 填阈值 + 选触发方式 + 去抖/冷却"这一套抽出来，
免得每个自动化工具各写一遍、还各写得不一样。

用法::

    fields = build_trigger_fields(context)
    form.addRow(*... )            # 或 fields.rows()
    trigger = read_trigger_fields(fields)   # -> automation.Trigger
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QSpinBox,
    QWidget,
)

from ...core.automation import (
    COMPARE_LABELS,
    TRIGGER_MODE_LABELS,
    Compare,
    Trigger,
    TriggerMode,
)
from .. import theme
from .base import ToolContext

#: 自动化服务在 ToolContext.services 里的键
AUTOMATION_SERVICE = "automation"
#: 截屏工具单独用这个键（它要给动作传"谁在截屏"）
CAPTURE_SERVICE = AUTOMATION_SERVICE


def as_enum(enum_type, data, default):
    """把下拉框里的 data 还原成枚举成员。

    坑：``Compare`` / ``TriggerMode`` / ``StepMode`` 都是 ``str, Enum``，
    PySide6 存进 QComboBox 的 itemData 时会退化成普通 ``str``，取出来再
    ``isinstance`` 就 False 了 —— 不还原的话用户选了"下降沿 / 变量触发"，
    实际会被当成默认的"上升沿 / 第 N 秒一次"，属于不报错但行为错的那种 bug。
    """
    if isinstance(data, enum_type):
        return data
    try:
        return enum_type(data)
    except (TypeError, ValueError):
        return default


def build_pid_combo(context: ToolContext, writable_only: bool = False) -> QComboBox:
    """点位下拉框（带显示名）。"""
    combo = QComboBox()
    entries = (
        context.writable_points() if writable_only else context.all_points()
    )
    for pid, label in entries:
        combo.addItem(label, pid)
    combo.setEditable(True)  # 点位多的时候可以手打过滤
    combo.setInsertPolicy(QComboBox.NoInsert)
    return combo


@dataclass
class TriggerFields:
    """一组触发条件控件。"""

    pid_combo: QComboBox
    compare_combo: QComboBox
    threshold_box: QDoubleSpinBox
    bit_box: QSpinBox
    mode_combo: QComboBox
    hold_box: QSpinBox
    cooldown_box: QDoubleSpinBox
    preview: QLabel
    #: 需要按比较方式显示/隐藏的控件（阈值 vs 位序号）
    _threshold_row: QWidget = field(default=None)  # type: ignore[assignment]
    _bit_row: QWidget = field(default=None)  # type: ignore[assignment]

    def rows(self) -> list[tuple]:
        """``[(label, widget), ...]``，直接喂给 QFormLayout.addRow。"""
        return [
            ("监视点位", self.pid_combo),
            ("条件", self.compare_combo),
            ("阈值 / 位", self._value_row()),
            ("触发方式", self.mode_combo),
            ("连续次数", self.hold_box),
            ("冷却时间", self.cooldown_box),
            ("", self.preview),
        ]

    def _value_row(self) -> QWidget:
        """阈值与位序号放同一行，按条件类型显示其中一个。"""
        from PySide6.QtWidgets import QHBoxLayout

        wrapper = QWidget()
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.threshold_box, 1)
        layout.addWidget(QLabel("bit"))
        layout.addWidget(self.bit_box)
        layout.addWidget(QLabel("（0 起）"))
        layout.addStretch(0)
        return wrapper

    def selected_compare(self) -> Compare:
        return as_enum(Compare, self.compare_combo.currentData(), Compare.GE)

    def sync_visibility(self) -> None:
        """按比较方式决定阈值框/位框是否可用，并刷新条件预览。"""
        compare = self.selected_compare()
        needs_bit = compare in (Compare.BIT_SET, Compare.BIT_CLEAR)
        needs_value = compare not in (Compare.CHANGE,) and not needs_bit
        self.threshold_box.setEnabled(needs_value)
        self.bit_box.setEnabled(needs_bit)
        self.preview.setText(
            f"<span style='color:{theme.TEXT_DIM}'>条件预览：{self.describe()}</span>"
        )

    def describe(self) -> str:
        try:
            return read_trigger_fields(self, strict=False).describe()
        except Exception:  # noqa: BLE001 - 预览不该因为半成品输入而报错
            return "（条件不完整）"


def build_trigger_fields(
    context: ToolContext,
    default_compare: Compare = Compare.GE,
    default_mode: TriggerMode = TriggerMode.RISING,
) -> TriggerFields:
    """造一组触发条件控件。"""
    pid_combo = build_pid_combo(context)

    compare_combo = QComboBox()
    for compare, label in COMPARE_LABELS.items():
        compare_combo.addItem(f"{label}（{compare.value}）", compare)

    threshold_box = QDoubleSpinBox()
    threshold_box.setRange(-1e12, 1e12)
    threshold_box.setDecimals(3)
    threshold_box.setValue(0.0)
    threshold_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    bit_box = QSpinBox()
    bit_box.setRange(0, 63)
    bit_box.setValue(0)
    bit_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    mode_combo = QComboBox()
    for mode, label in TRIGGER_MODE_LABELS.items():
        mode_combo.addItem(label, mode)

    hold_box = QSpinBox()
    hold_box.setRange(1, 999)
    hold_box.setValue(1)
    hold_box.setSuffix(" 次")
    hold_box.setToolTip("要连续满足这么多次才算触发，用来滤掉毛刺")
    hold_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    cooldown_box = QDoubleSpinBox()
    cooldown_box.setRange(0.0, 3600.0)
    cooldown_box.setDecimals(1)
    cooldown_box.setValue(0.0)
    cooldown_box.setSuffix(" 秒")
    cooldown_box.setToolTip("触发后这段时间内不再重复触发，避免刷屏／刷文件")
    cooldown_box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)

    preview = QLabel("")
    preview.setWordWrap(True)
    preview.setTextFormat(Qt.TextFormat.RichText)

    fields = TriggerFields(
        pid_combo=pid_combo,
        compare_combo=compare_combo,
        threshold_box=threshold_box,
        bit_box=bit_box,
        mode_combo=mode_combo,
        hold_box=hold_box,
        cooldown_box=cooldown_box,
        preview=preview,
    )

    index = compare_combo.findData(default_compare)
    if index >= 0:
        compare_combo.setCurrentIndex(index)
    index = mode_combo.findData(default_mode)
    if index >= 0:
        mode_combo.setCurrentIndex(index)

    compare_combo.currentIndexChanged.connect(fields.sync_visibility)
    pid_combo.currentIndexChanged.connect(fields.sync_visibility)
    threshold_box.valueChanged.connect(fields.sync_visibility)
    bit_box.valueChanged.connect(fields.sync_visibility)
    fields.sync_visibility()
    return fields


def read_trigger_fields(fields: TriggerFields, strict: bool = True) -> Trigger:
    """把控件读成 :class:`Trigger`。``strict`` 时点位为空要抛错。"""
    pid = fields.pid_combo.currentData()
    pid = str(pid or "").strip()
    if strict and not pid:
        raise ValueError("请先选择一个监视点位（工程里还没有点位？）")

    compare = fields.selected_compare()
    mode = as_enum(TriggerMode, fields.mode_combo.currentData(), TriggerMode.RISING)
    return Trigger(
        pid=pid,
        compare=compare,
        threshold=float(fields.threshold_box.value()),
        bit=int(fields.bit_box.value()),
        mode=mode,
        hold_count=int(fields.hold_box.value()),
        cooldown_s=float(fields.cooldown_box.value()),
    )


def trigger_summary(trigger: Trigger, context: ToolContext) -> str:
    """给任务起名字用的一句话条件描述。"""

    def title_of(pid: str) -> str:
        found = context.project.find_point(pid)
        if found is None:
            return pid
        device, point = found
        return f"{device.name}.{point.title}"

    return trigger.describe(title_of)
