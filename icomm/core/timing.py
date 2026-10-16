"""计时与定时赋值（纯逻辑，可单测）。

现场用法：按「开始计时」后，第 N 秒给某个点位写一个值（例如做一次阶跃响应试验），
或者每 N 秒写一次（周期激励）；也可以让写入由某个变量触发（复用自动化核心）。

这里只放逻辑：秒表 + 计划表。界面在 :mod:`icomm.ui.tools.timing`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class StepMode(str, Enum):
    """一条定时动作在什么时候执行。"""

    #: 开始计时后第 N 秒执行一次
    AT = "at"
    #: 开始计时后每 N 秒执行一次
    EVERY = "every"
    #: 由变量触发（交给自动化核心判断）
    TRIGGER = "trigger"


STEP_MODE_LABELS = {
    StepMode.AT: "第 N 秒一次",
    StepMode.EVERY: "每 N 秒一次",
    StepMode.TRIGGER: "变量触发",
}


@dataclass
class Step:
    """一条"到点就写值"的动作。"""

    pid: str
    value: float = 0.0
    mode: StepMode = StepMode.AT
    #: AT / EVERY 用：开始计时后的第几秒（EVERY 表示第一次的偏移）
    seconds: float = 0.0
    enabled: bool = True
    #: 已经执行过几次（给界面看）
    fired: int = 0
    #: 变量触发模式下，注册到自动化核心的任务名（界面用来注销）
    task_name: str = ""

    def label(self) -> str:
        if self.mode is StepMode.AT:
            return f"第 {self.seconds:g} 秒"
        if self.mode is StepMode.EVERY:
            return f"每 {self.seconds:g} 秒"
        return "变量触发"

    def due_times(self, elapsed: float, last: float, limit: int = 10) -> int:
        """这一拍该执行几次。

        用**区间判断**而不是"相等判断"：界面卡一下、定时器拖到 1.2 秒才跑，
        精确比较会永远错过"第 1 秒"，定时赋值就时灵时不灵了。
        ``EVERY`` 跨过多个周期时按拍数补齐（``limit`` 防止卡死后一次性猛写）。
        """
        if not self.enabled or self.mode is StepMode.TRIGGER:
            return 0
        seconds = max(0.0, float(self.seconds))
        if seconds <= 0 or elapsed < seconds:
            return 0
        if self.mode is StepMode.AT:
            return 1 if last < seconds <= elapsed else 0
        # 第 seconds、2*seconds、3*seconds…… 各是一拍
        passed = int(elapsed // seconds) - int(last // seconds)
        return max(0, min(limit, passed))

    def due(self, elapsed: float, last: float) -> bool:
        return self.due_times(elapsed, last) > 0


class Stopwatch:
    """秒表：能开始、暂停、继续、清零，并记录时刻标记。

    用 ``time.monotonic()`` 而不是墙上时间 —— 现场调系统时间不能把计时搞乱。
    """

    def __init__(self) -> None:
        self._started_at: float | None = None
        self._accumulated = 0.0
        self.marks: list[float] = []

    # -------------------------------------------------- 状态

    @property
    def running(self) -> bool:
        return self._started_at is not None

    def elapsed(self, now: float | None = None) -> float:
        """从开始计时算起的秒数（暂停时是暂停时的值）。"""
        if self._started_at is None:
            return self._accumulated
        now = time.monotonic() if now is None else now
        return self._accumulated + max(0.0, now - self._started_at)

    # -------------------------------------------------- 操作

    def start(self, now: float | None = None) -> None:
        if self.running:
            return
        self._started_at = time.monotonic() if now is None else now

    def pause(self, now: float | None = None) -> None:
        if not self.running:
            return
        self._accumulated = self.elapsed(now)
        self._started_at = None

    def reset(self) -> None:
        self._started_at = None
        self._accumulated = 0.0
        self.marks.clear()

    def toggle(self, now: float | None = None) -> None:
        self.pause(now) if self.running else self.start(now)

    def mark(self, now: float | None = None) -> float:
        """记一个时刻（"就是这一下！"），返回当前的计数值。"""
        value = self.elapsed(now)
        self.marks.append(value)
        return value

    def text(self, now: float | None = None) -> str:
        """``00:00:00.0`` 形式，现场看着顺眼。"""
        return format_seconds(self.elapsed(now))


def format_seconds(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    tenths = int(round((seconds - int(seconds)) * 10)) % 10
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{tenths}"


@dataclass
class Schedule:
    """一堆定时动作。"""

    steps: list[Step] = field(default_factory=list)
    #: 每条动作上次执行时的计数值
    _last: list[float] = field(default_factory=list)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        self._last.append(0.0)
        return step

    def remove(self, index: int) -> None:
        if 0 <= index < len(self.steps):
            self.steps.pop(index)
            self._last.pop(index)

    def reset(self) -> None:
        """清零：所有计时类动作重新等一遍。"""
        self._last = [0.0] * len(self.steps)
        for step in self.steps:
            step.fired = 0

    def tick(self, elapsed: float) -> list[Step]:
        """推进到 ``elapsed`` 秒，返回这一拍该执行的动作（可能为空）。"""
        due: list[Step] = []
        while len(self._last) < len(self.steps):
            self._last.append(0.0)
        for index, step in enumerate(self.steps):
            times = step.due_times(elapsed, self._last[index])
            if times <= 0:
                continue
            # 记住"已经推进到 elapsed"，而不是"本该第几拍" —— 卡顿后不该补发一堆同样的值
            self._last[index] = elapsed
            step.fired += times
            due.extend([step] * times)
        return due

    def trigger_steps(self) -> list[Step]:
        """交给自动化核心的那部分动作。"""
        return [step for step in self.steps if step.mode is StepMode.TRIGGER]

    def summary(self) -> str:
        enabled = [step for step in self.steps if step.enabled]
        return f"{len(enabled)}/{len(self.steps)} 条启用"
