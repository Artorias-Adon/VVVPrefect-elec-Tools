"""自动化核心：**触发条件** + **到期动作**。

一批需求背后是同一个机制：

* 截屏辅助 —— 某通道达到某值时截屏；
* 数据记录 —— 满足条件开始按间隔记表，另一个条件结束；
* 录音 / 故障录波 —— 触发后记录一段时间；
* 计时器 —— 定时给变量赋值、定时动作。

所以先把这套做出来，各功能只实现"条件怎么判断"和"到点干什么"，
不必各自重造定时器与线程安全。

设计要点：

* **触发条件**（:class:`Trigger`）是纯函数：给它一份实时值快照，返回 True/False。
  支持边沿触发（上升沿/下降沿）与电平触发，还有"手动触发"。
* **动作**（:class:`Action`）由使用者提供回调。调度器不关心动作内容。
* :class:`AutomationService` 由一个 QTimer 驱动（跑在界面线程），
  这样动作可以放心碰界面；需要耗时的动作自己丢线程池。
* 所有注册项都有 ``name``，便于在界面上列出与删除；这是"留好扩展接口"的部分：
  新功能只要注册一个 Trigger + 一个回调即可。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .tags import TagValue

# --------------------------------------------------------------- 触发条件


class TriggerMode(str, Enum):
    """触发方式。"""

    #: 手动：只能由用户按按钮触发
    MANUAL = "manual"
    #: 电平：条件成立就一直认为"在触发状态"
    LEVEL = "level"
    #: 上升沿：条件由不成立变为成立的那一刻触发一次
    RISING = "rising"
    #: 下降沿：条件由成立变为不成立的那一刻触发一次
    FALLING = "falling"


class Compare(str, Enum):
    """比较方式。"""

    GT = ">"
    GE = ">="
    LT = "<"
    LE = "<="
    EQ = "=="
    NE = "!="
    CHANGE = "变化"
    BIT_SET = "位为1"
    BIT_CLEAR = "位为0"


#: 比较方式 -> 显示名（界面下拉框用）
COMPARE_LABELS: dict[Compare, str] = {
    Compare.GT: "大于",
    Compare.GE: "大于等于",
    Compare.LT: "小于",
    Compare.LE: "小于等于",
    Compare.EQ: "等于",
    Compare.NE: "不等于",
    Compare.CHANGE: "发生变化",
    Compare.BIT_SET: "某位为 1",
    Compare.BIT_CLEAR: "某位为 0",
}

TRIGGER_MODE_LABELS: dict[TriggerMode, str] = {
    TriggerMode.MANUAL: "手动",
    TriggerMode.LEVEL: "电平（持续成立）",
    TriggerMode.RISING: "上升沿（成立的那一刻）",
    TriggerMode.FALLING: "下降沿（失效的那一刻）",
}


def compare_values(compare: Compare, value: Any, threshold: float, bit: int = 0) -> bool:
    """执行一次比较。

    ``CHANGE`` / 位操作不参与数值比较，由 :class:`Trigger` 自己处理，
    这里只做数值型比较，非数值一律 False（避免现场把字符串点位配进来后崩）。
    """
    if value is None:
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    if compare is Compare.GT:
        return number > threshold
    if compare is Compare.GE:
        return number >= threshold
    if compare is Compare.LT:
        return number < threshold
    if compare is Compare.LE:
        return number <= threshold
    if compare is Compare.EQ:
        return number == threshold
    if compare is Compare.NE:
        return number != threshold
    return False


@dataclass
class Trigger:
    """一个触发条件。"""

    #: 监视的点号（MANUAL 模式可为空）
    pid: str = ""
    compare: Compare = Compare.GE
    threshold: float = 0.0
    #: BIT_SET / BIT_CLEAR 用的位序号
    bit: int = 0
    mode: TriggerMode = TriggerMode.RISING
    #: 连续满足多少次才算触发（防止毛刺误触发，现场很需要）
    hold_count: int = 1
    #: 触发后多少秒内不重复触发（限流，避免刷屏/刷文件）
    cooldown_s: float = 0.0

    #: 运行状态
    _active: bool = field(default=False, repr=False)
    _held: int = field(default=0, repr=False)
    _last_value: Any = field(default=None, repr=False)
    _last_fire: float = field(default=0.0, repr=False)

    def raw_state(self, values: dict[str, TagValue]) -> tuple[bool, Any]:
        """当前"条件是否成立"，以及用到的值。"""
        if self.mode is TriggerMode.MANUAL or not self.pid:
            return False, None
        tag = values.get(self.pid)
        value = tag.value if tag is not None else None
        if self.compare is Compare.CHANGE:
            changed = value is not None and self._last_value is not None and value != self._last_value
            return changed, value
        if self.compare in (Compare.BIT_SET, Compare.BIT_CLEAR):
            try:
                number = int(value)
            except (TypeError, ValueError):
                return False, value
            mask = 1 << max(0, self.bit)
            flag = bool(number & mask)
            return flag if self.compare is Compare.BIT_SET else not flag, value
        return compare_values(self.compare, value, self.threshold), value

    def update(self, values: dict[str, TagValue], now: float | None = None) -> bool:
        """喂一次快照，返回**本次是否应该触发动作**。"""
        if self.mode is TriggerMode.MANUAL:
            return False
        now = time.monotonic() if now is None else now
        state, value = self.raw_state(values)

        # 连续满足计数：只有连够 hold_count 次才认为"真的成立"
        if state:
            self._held += 1
        else:
            self._held = 0
        confirmed = self._held >= max(1, self.hold_count)

        fire = False
        if self.mode is TriggerMode.LEVEL:
            fire = confirmed and not self._active
        elif self.mode is TriggerMode.RISING:
            fire = confirmed and not self._active
        elif self.mode is TriggerMode.FALLING:
            fire = (not confirmed) and self._active
        self._active = confirmed
        self._last_value = value

        if not fire:
            return False
        if self.cooldown_s > 0 and (now - self._last_fire) < self.cooldown_s:
            return False
        self._last_fire = now
        return True

    @property
    def active(self) -> bool:
        return self._active

    def reset(self) -> None:
        self._active = False
        self._held = 0
        self._last_value = None
        self._last_fire = 0.0

    def describe(self, title_of: Callable[[str], str] | None = None) -> str:
        """一句话说明这个条件（界面上列出来用）。"""
        if self.mode is TriggerMode.MANUAL or not self.pid:
            return "手动触发"
        name = title_of(self.pid) if title_of is not None else self.pid
        if self.compare is Compare.CHANGE:
            cond = "发生变化"
        elif self.compare in (Compare.BIT_SET, Compare.BIT_CLEAR):
            cond = f"bit{self.bit} {'为 1' if self.compare is Compare.BIT_SET else '为 0'}"
        else:
            cond = f"{COMPARE_LABELS.get(self.compare, self.compare.value)} {self.threshold:g}"
        text = f"{name} {cond}"
        if self.hold_count > 1:
            text += f"（连续 {self.hold_count} 次）"
        if self.mode in (TriggerMode.FALLING,):
            text += "　失效时"
        return text


# --------------------------------------------------------------- 任务


@dataclass
class Task:
    """一个"触发后要做的事"。

    ``action`` 的签名是 ``(task, values) -> None``；调度器只负责在正确的时机调用它。
    """

    name: str
    action: Callable[["Task", dict[str, TagValue]], None]
    trigger: Trigger = field(default_factory=Trigger)
    #: 是否启用（用户可以临时关掉而不删配置）
    enabled: bool = True
    #: 最多触发几次（0 = 不限）
    max_fires: int = 0
    #: 已经触发几次
    fires: int = 0
    #: 最近一次触发时刻与错误
    last_fire_at: float = 0.0
    last_error: str = ""
    #: 使用者附加的数据（例如截屏工具的保存目录）
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def exhausted(self) -> bool:
        return self.max_fires > 0 and self.fires >= self.max_fires

    def status_text(self) -> str:
        if not self.enabled:
            return "已停用"
        if self.exhausted:
            return f"已完成（触发 {self.fires} 次）"
        if self.last_error:
            return f"出错：{self.last_error}"
        if self.fires:
            return f"已触发 {self.fires} 次"
        return "等待触发"


# --------------------------------------------------------------- 服务


class AutomationService:
    """触发与任务的调度中心。

    界面层用 QTimer 定时调 :meth:`tick`；这样动作跑在界面线程，
    要碰界面（截屏、弹提示）都安全。耗时动作请自己在 :class:`Task.action` 里丢线程。
    """

    def __init__(self) -> None:
        self.tasks: list[Task] = []
        self._listeners: list[Callable[[Task], None]] = []
        #: 已经因为"触发过"而需要后续收尾的任务（例如采完一段再保存）
        self.tick_count = 0

    # -------------------------------------------------- 注册

    def add(self, task: Task) -> Task:
        self.tasks.append(task)
        return task

    def remove(self, name: str) -> bool:
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t.name != name]
        return len(self.tasks) != before

    def find(self, name: str) -> Task | None:
        for task in self.tasks:
            if task.name == name:
                return task
        return None

    def clear(self) -> None:
        self.tasks.clear()

    def on_fire(self, listener: Callable[[Task], None]) -> None:
        """注册触发回调（界面用它刷新列表）。"""
        self._listeners.append(listener)

    # -------------------------------------------------- 运行

    def tick(self, values: dict[str, TagValue]) -> list[Task]:
        """喂一次实时值快照，返回本次触发的任务。"""
        self.tick_count += 1
        fired: list[Task] = []
        for task in list(self.tasks):
            if not task.enabled or task.exhausted:
                continue
            try:
                should = task.trigger.update(values)
            except Exception as exc:  # 触发条件本身出错不能拖垮采集显示
                task.last_error = f"触发判断失败：{exc}"
                continue
            if not should:
                continue
            self.fire(task, values)
            fired.append(task)
        return fired

    def fire(self, task: Task, values: dict[str, TagValue] | None = None) -> None:
        """立刻执行一个任务（手动触发也走这里）。"""
        try:
            task.action(task, values or {})
        except Exception as exc:
            task.last_error = f"动作执行失败：{exc}"
            return
        task.fires += 1
        task.last_fire_at = time.time()
        task.last_error = ""
        for listener in self._listeners:
            try:
                listener(task)
            except Exception:
                pass

    def manual_trigger(self, name: str, values: dict[str, TagValue] | None = None) -> bool:
        task = self.find(name)
        if task is None:
            return False
        self.fire(task, values)
        return True

    def status_rows(self) -> list[tuple[str, str, str]]:
        """``[(任务名, 条件, 状态)]``，给界面列表用。"""
        return [
            (task.name, task.trigger.describe(), task.status_text()) for task in self.tasks
        ]
