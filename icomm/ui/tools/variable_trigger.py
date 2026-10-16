"""触发条件的界面控件（截图、数据记录、录音、故障录波、计时器共用）。

把"选点位 + 选比较方式 + 填阈值 + 选触发方式 + 去抖/冷却"这一套抽出来，
免得每个自动化工具各写一遍、还各写得不一样。

**支持多个变量**：现场的条件很少只有一个 —— "电压够 **且** 电流够 **且** 无故障"
才是真正想抓的时刻。所以这里是一串条件行（每行一个变量），再选"全部满足 / 任一满足"：

    ┌ 监视条件 ─────────────────────────────────────────────┐
    │ [储能变流器.母线电压 ▾] [大于等于 ▾] [700]       [－] │
    │ [储能变流器.输出电流 ▾] [大于     ▾] [250]       [－] │
    │ 组合方式 [全部满足（且）▾]         [＋ 添加条件]      │
    └───────────────────────────────────────────────────────┘

用法::

    fields = build_trigger_fields(context)
    for row in fields.rows():
        form.addRow(*row)
    trigger = read_trigger_fields(fields)   # -> automation.Trigger

老代码按 ``fields.pid_combo`` / ``fields.compare_combo`` / ``fields.threshold_box``
拿控件的地方照旧能用 —— 那几个名字指的是**第一行**的控件（单条件场景写起来最短）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...core.automation import (
    COMPARE_LABELS,
    LOGIC_LABELS,
    TRIGGER_MODE_LABELS,
    Compare,
    Condition,
    Logic,
    Trigger,
    TriggerMode,
)
from .. import theme
from ..spin import no_arrows
from .base import ToolContext

#: 自动化服务在 ToolContext.services 里的键
AUTOMATION_SERVICE = "automation"
#: 截屏工具单独用这个键（它要给动作传"谁在截屏"）
CAPTURE_SERVICE = AUTOMATION_SERVICE

#: 最多能加几条条件（再多窗口就放不下了，现场也用不到）
MAX_CONDITIONS = 6


def as_enum(enum_type, data, default):
    """把下拉框里的 data 还原成枚举成员。

    坑：``Compare`` / ``TriggerMode`` / ``Logic`` / ``StepMode`` 都是 ``str, Enum``，
    PySide6 存进 QComboBox 的 itemData 时会退化成普通 ``str``，取出来再
    ``isinstance`` 就 False 了 —— 不还原的话用户选了"下降沿 / 变量触发"，实际会被当成
    默认的"上升沿 / 第 N 秒一次"，属于不报错但行为错的那种 bug。
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


class ConditionRow:
    """一行条件：一个变量 + 一种比较 + 阈值/位 + 一个删除按钮。"""

    def __init__(self, context: ToolContext, on_change, on_remove) -> None:
        self.context = context
        self._on_change = on_change
        self._on_remove = on_remove

        self.pid_combo = build_pid_combo(context)
        self.pid_combo.setMinimumWidth(150)

        self.compare_combo = QComboBox()
        for compare, label in COMPARE_LABELS.items():
            self.compare_combo.addItem(label, compare)
        self.compare_combo.setToolTip("条件：大于 / 小于 / 某位为 1…（有些条件不用填阈值）")

        self.threshold_box = QDoubleSpinBox()
        self.threshold_box.setRange(-1e12, 1e12)
        self.threshold_box.setDecimals(3)
        self.threshold_box.setValue(0.0)
        self.threshold_box.setMinimumWidth(90)
        no_arrows(self.threshold_box)

        self.bit_box = QSpinBox()
        self.bit_box.setRange(0, 63)
        self.bit_box.setValue(0)
        self.bit_box.setToolTip("第几位（从 0 开始）")
        no_arrows(self.bit_box)

        self.remove_button = QToolButton()
        self.remove_button.setText("－")
        self.remove_button.setToolTip("删掉这条条件")
        self.remove_button.setAutoRaise(True)
        self.remove_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.remove_button.clicked.connect(lambda _checked=False: self._on_remove(self))

        self.widget = self._build_widget()
        self.pid_combo.currentIndexChanged.connect(self._changed)
        self.pid_combo.editTextChanged.connect(self._changed)
        self.compare_combo.currentIndexChanged.connect(self._changed)
        self.threshold_box.valueChanged.connect(self._changed)
        self.bit_box.valueChanged.connect(self._changed)
        self.sync_visibility()

    # -------------------------------------------------- 界面

    def _build_widget(self) -> QWidget:
        """一行：[点位] [条件] [阈值/位] [－]。"""
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.pid_combo, 1)
        layout.addWidget(self.compare_combo)
        layout.addWidget(self.threshold_box)
        layout.addWidget(self.bit_box)
        layout.addWidget(self.remove_button)
        return box

    def _changed(self, *_args) -> None:
        self.sync_visibility()
        self._on_change()

    def sync_visibility(self) -> None:
        """按比较方式显示阈值框或位框（用不上的直接收起来，别占地方）。"""
        compare = self.selected_compare()
        needs_bit = compare in (Compare.BIT_SET, Compare.BIT_CLEAR)
        needs_value = compare is not Compare.CHANGE and not needs_bit
        self.threshold_box.setVisible(needs_value)
        self.bit_box.setVisible(needs_bit)

    # -------------------------------------------------- 读写

    def selected_compare(self) -> Compare:
        return as_enum(Compare, self.compare_combo.currentData(), Compare.GE)

    @property
    def pid(self) -> str:
        return str(self.pid_combo.currentData() or "").strip()

    def set_pid(self, pid: str) -> None:
        """选中某个点号（找不到就补进下拉框，别让用户自己去翻）。"""
        index = self.pid_combo.findData(pid)
        if index < 0 and pid:
            title = self.context.title_of(pid) if self.context.title_of else pid
            self.pid_combo.addItem(title or pid, pid)
            index = self.pid_combo.count() - 1
        if index >= 0:
            self.pid_combo.setCurrentIndex(index)

    def read(self) -> Condition | None:
        """读成一条条件；没选点位就返回 None（空行不算条件）。"""
        pid = self.pid
        if not pid:
            return None
        return Condition(
            pid=pid,
            compare=self.selected_compare(),
            threshold=float(self.threshold_box.value()),
            bit=int(self.bit_box.value()),
        )


class TriggerFields:
    """一组触发条件控件（若干条件 + 组合方式 + 触发方式 + 去抖/冷却）。"""

    def __init__(
        self,
        context: ToolContext,
        default_compare: Compare = Compare.GE,
        default_mode: TriggerMode = TriggerMode.RISING,
        default_logic: Logic = Logic.AND,
        max_conditions: int = MAX_CONDITIONS,
    ) -> None:
        self.context = context
        self.max_conditions = max(1, int(max_conditions))
        self._rows: list[ConditionRow] = []

        self.conditions_host = QWidget()
        self._conditions_layout = QVBoxLayout(self.conditions_host)
        self._conditions_layout.setContentsMargins(0, 0, 0, 0)
        self._conditions_layout.setSpacing(4)

        self.logic_combo = QComboBox()
        for logic, label in LOGIC_LABELS.items():
            self.logic_combo.addItem(label, logic)
        index = self.logic_combo.findData(default_logic)
        if index >= 0:
            self.logic_combo.setCurrentIndex(index)
        self.logic_combo.currentIndexChanged.connect(lambda _index=0: self.sync_visibility())

        self.add_button = QPushButton("＋ 添加条件")
        self.add_button.setToolTip(
            f"再加一个变量（最多 {self.max_conditions} 条）：例如「电压够 且 电流够 且 无故障」"
        )
        self.add_button.clicked.connect(lambda _checked=False: self.add_condition())

        self.mode_combo = QComboBox()
        for mode, label in TRIGGER_MODE_LABELS.items():
            self.mode_combo.addItem(label, mode)
        index = self.mode_combo.findData(default_mode)
        if index >= 0:
            self.mode_combo.setCurrentIndex(index)

        self.hold_box = QSpinBox()
        self.hold_box.setRange(1, 999)
        self.hold_box.setValue(1)
        self.hold_box.setSuffix(" 次")
        self.hold_box.setToolTip("要连续满足这么多次才算触发，用来滤掉毛刺")
        no_arrows(self.hold_box)

        self.cooldown_box = QDoubleSpinBox()
        self.cooldown_box.setRange(0.0, 3600.0)
        self.cooldown_box.setDecimals(1)
        self.cooldown_box.setValue(0.0)
        self.cooldown_box.setSuffix(" 秒")
        self.cooldown_box.setToolTip("触发后这段时间内不再重复触发，避免刷屏／刷文件")
        no_arrows(self.cooldown_box)

        self.preview = QLabel("")
        self.preview.setWordWrap(True)
        self.preview.setTextFormat(Qt.TextFormat.RichText)
        self.preview.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

        self._body = self._build_body()

        # 先摆一条条件：既要能直接用，也要让"第一行的控件"这个兼容入口是活的
        first = self.add_condition(default_compare)
        if first is not None:
            index = first.compare_combo.findData(default_compare)
            if index >= 0:
                first.compare_combo.setCurrentIndex(index)
        self.sync_visibility()

    # -------------------------------------------------- 兼容入口

    @property
    def pid_combo(self) -> QComboBox:
        """第一条条件的点位下拉框（老代码/探针按这个名字拿控件）。"""
        return self._rows[0].pid_combo

    @property
    def compare_combo(self) -> QComboBox:
        return self._rows[0].compare_combo

    @property
    def threshold_box(self) -> QDoubleSpinBox:
        return self._rows[0].threshold_box

    @property
    def bit_box(self) -> QSpinBox:
        return self._rows[0].bit_box

    # -------------------------------------------------- 条件增删

    @property
    def condition_rows(self) -> list[ConditionRow]:
        return list(self._rows)

    def add_condition(
        self, default_compare: Compare | None = None, pid: str = ""
    ) -> ConditionRow | None:
        """加一条条件（返回新行；到上限就不加）。"""
        if len(self._rows) >= self.max_conditions:
            return None
        row = ConditionRow(self.context, self._on_any_change, self.remove_condition)
        self._rows.append(row)
        self._conditions_layout.addWidget(row.widget)
        compare = default_compare or Compare.GE
        index = row.compare_combo.findData(compare)
        if index >= 0:
            row.compare_combo.setCurrentIndex(index)
        if pid:
            row.set_pid(pid)
        self.sync_visibility()
        return row

    def remove_condition(self, row: ConditionRow) -> None:
        """删掉一条条件；至少留一条（不然用户会找不到输入的地方）。"""
        if row not in self._rows or len(self._rows) <= 1:
            return
        self._rows.remove(row)
        self._conditions_layout.removeWidget(row.widget)
        row.widget.setParent(None)
        row.widget.deleteLater()
        self.sync_visibility()

    # -------------------------------------------------- 界面组装

    def _build_body(self) -> QWidget:
        """条件列表 + 底下的"组合方式 / 添加条件"。"""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.conditions_host)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(6)
        footer_layout.addWidget(QLabel("组合方式"))
        footer_layout.addWidget(self.logic_combo)
        footer_layout.addWidget(self.add_button)
        footer_layout.addStretch(1)
        layout.addWidget(footer)
        return box

    def conditions_widget(self) -> QWidget:
        """整块条件编辑器（要自己排版的地方直接把这块塞进去）。"""
        return self._body

    def rows(self) -> list[tuple]:
        """``[(label, widget), ...]``，直接喂给 QFormLayout.addRow。"""
        return [
            ("监视条件", self._body),
            ("触发方式", self.mode_combo),
            ("连续次数", self.hold_box),
            ("冷却时间", self.cooldown_box),
            ("", self.preview),
        ]

    # -------------------------------------------------- 读写

    def selected_logic(self) -> Logic:
        return as_enum(Logic, self.logic_combo.currentData(), Logic.AND)

    def selected_mode(self) -> TriggerMode:
        return as_enum(TriggerMode, self.mode_combo.currentData(), TriggerMode.RISING)

    def read_conditions(self) -> list[Condition]:
        """所有填了点位的条件（空行自动跳过）。"""
        return [
            condition
            for condition in (row.read() for row in self._rows)
            if condition is not None
        ]

    def _on_any_change(self) -> None:
        self.sync_visibility()

    def sync_visibility(self) -> None:
        """条件条数决定"组合方式 / 删除按钮"能不能用，并刷新条件预览。"""
        multiple = len(self._rows) > 1
        self.logic_combo.setEnabled(multiple)
        self.logic_combo.setToolTip(
            "多个条件之间是「且」还是「或」"
            if multiple
            else "只有一个条件时用不到组合方式"
        )
        for row in self._rows:
            row.remove_button.setEnabled(multiple)
        self.add_button.setEnabled(len(self._rows) < self.max_conditions)
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
    return TriggerFields(context, default_compare, default_mode)


def read_trigger_fields(fields: TriggerFields, strict: bool = True) -> Trigger:
    """把控件读成 :class:`Trigger`。

    ``strict`` 时一条条件都没填要抛错。单条件时会顺带把 ``pid/compare/threshold/bit``
    也填上（老代码直接读 ``trigger.pid`` 的地方照旧能用）。
    """
    conditions = fields.read_conditions()
    if strict and not conditions:
        raise ValueError("请先选择至少一个监视点位（工程里还没有点位？）")
    trigger = Trigger(
        conditions=conditions,
        logic=fields.selected_logic(),
        mode=fields.selected_mode(),
        hold_count=int(fields.hold_box.value()),
        cooldown_s=float(fields.cooldown_box.value()),
    )
    if len(conditions) == 1:
        first = conditions[0]
        trigger.pid = first.pid
        trigger.compare = first.compare
        trigger.threshold = first.threshold
        trigger.bit = first.bit
    return trigger


def trigger_summary(trigger: Trigger, context: ToolContext) -> str:
    """给任务起名字用的一句话条件描述。"""

    def title_of(pid: str) -> str:
        found = context.project.find_point(pid)
        if found is None:
            return pid
        device, point = found
        return f"{device.name}.{point.title}"

    return trigger.describe(title_of)
