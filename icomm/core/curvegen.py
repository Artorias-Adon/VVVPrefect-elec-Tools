"""曲线给定：让一个点位按"随时间变化的曲线"连续写值（纯逻辑，可单测）。

现场用它干什么：

* 做**斜坡/阶跃试验**（给定从 0 爬到 100%，看响应、看录波）；
* 让**功率/频率给定**按正弦小幅摆动，验证设备跟随与保护；
* 有测试脚本导出的**曲线数据**（CSV）时，按原速回放给设备。

所以这里的核心只有两件事：

1. :class:`CurveSpec` —— 曲线怎么算（正弦/三角/方波/锯齿/斜坡/回放）；
2. :class:`CurvePlayer` —— 什么时候该发下一个值、什么时候结束，
   并按点位的**小数位 / 整型 / 写值限幅**量化好，避免发出 0.1000000001 这种值。
"""

from __future__ import annotations

import csv
import io
import math
import os
from dataclasses import dataclass, field

from . import sheetio

#: 曲线种类与中文名（顺序即界面下拉框顺序）
KINDS: dict[str, str] = {
    "sine": "正弦",
    "triangle": "三角波",
    "square": "方波",
    "sawtooth": "锯齿（上升）",
    "ramp": "斜坡（一次，走完即停）",
    "replay": "回放（导入的曲线）",
}

#: 一个周期内相位/占空比的默认值
DEFAULT_PERIOD_S = 10.0
#: 最短写值间隔（秒）：再快就变成拿写报文刷总线了
MIN_INTERVAL_S = 0.1
#: 建议的最小间隔：低于它会提示可能挤占采集
COMFORT_INTERVAL_S = 0.2


def _cycle(t: float, period_s: float, phase_deg: float) -> float:
    """把时间折成 0~1 的周期相位。"""
    if period_s <= 0:
        return 0.0
    value = t / period_s + phase_deg / 360.0
    return value - math.floor(value)


@dataclass
class CurveSpec:
    """一条给定曲线：``value = offset + amplitude × 形状(相位)``。"""

    kind: str = "sine"
    #: 幅度（±多少；峰峰值 = 2×幅度）
    amplitude: float = 1.0
    offset: float = 0.0
    period_s: float = DEFAULT_PERIOD_S
    phase_deg: float = 0.0
    #: 方波占空比（0.01~0.99）
    duty: float = 0.5
    #: 回放用：采样值、采样间隔、是否循环
    samples: list[float] = field(default_factory=list)
    sample_interval_s: float = 0.5
    loop: bool = True

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            self.kind = "sine"
        try:
            self.duty = float(self.duty)
        except (TypeError, ValueError):
            self.duty = 0.5
        self.duty = min(0.99, max(0.01, self.duty))

    # -------------------------------------------------- 计算

    def value_at(self, t: float) -> float | None:
        """``t`` 秒时的给定值；返回 ``None`` 表示这条曲线走完了（一次性的）。"""
        t = max(0.0, float(t))
        kind = self.kind
        if kind == "replay":
            return self._replay_at(t)

        period = max(1e-6, float(self.period_s))
        if kind == "ramp":
            if t >= period:
                return None  # 斜坡走完就结束（保持最后那个值由调用方决定）
            shape = 2.0 * (t / period) - 1.0
        else:
            u = _cycle(t, period, self.phase_deg)
            if kind == "sine":
                shape = math.sin(2.0 * math.pi * u)
            elif kind == "triangle":
                shape = 4.0 * u - 1.0 if u < 0.5 else 3.0 - 4.0 * u
            elif kind == "square":
                shape = 1.0 if u < self.duty else -1.0
            else:  # sawtooth
                shape = 2.0 * u - 1.0
        return float(self.offset) + float(self.amplitude) * shape

    def _replay_at(self, t: float) -> float | None:
        if not self.samples:
            return None
        step = max(1e-6, float(self.sample_interval_s))
        index = int(t / step)
        if index >= len(self.samples):
            if not self.loop:
                return None
            index %= len(self.samples)
        return float(self.samples[index])

    @property
    def one_shot(self) -> bool:
        """走一遍就结束的曲线（斜坡 / 不循环的回放）。"""
        return self.kind == "ramp" or (self.kind == "replay" and not self.loop)

    def duration_s(self) -> float | None:
        """一次性曲线有多长（循环曲线返回 None）。"""
        if self.kind == "ramp":
            return max(1e-6, float(self.period_s))
        if self.kind == "replay" and not self.loop and self.samples:
            return len(self.samples) * max(1e-6, float(self.sample_interval_s))
        return None

    def validate(self) -> str:
        """参数检查，返回人话错误（空串 = 没问题）。"""
        if self.kind == "replay":
            if not self.samples:
                return "还没导入回放曲线（点「导入曲线…」选一个 CSV）"
            if self.sample_interval_s <= 0:
                return "回放采样间隔要大于 0"
            return ""
        if self.period_s <= 0:
            return "周期要大于 0 秒"
        if abs(float(self.amplitude)) <= 0:
            return "幅度不能是 0（那样曲线就是一条直线）"
        return ""

    def describe(self) -> str:
        if self.kind == "replay":
            span = len(self.samples) * self.sample_interval_s
            loop = "循环" if self.loop else "不循环"
            return f"回放 {len(self.samples)} 点 / {span:.2f}s（{loop}）"
        return (
            f"{KINDS.get(self.kind, self.kind)}：{self.offset:g} ± {self.amplitude:g}，"
            f"周期 {self.period_s:g}s，相位 {self.phase_deg:g}°"
        )


# --------------------------------------------------------------- 量化


def quantize_value(
    value: float,
    *,
    decimals: int = 0,
    low: float | None = None,
    high: float | None = None,
    integer: bool = False,
) -> float:
    """按点位的小数位 / 整型 / 写值限幅规整，返回真正要写下去的值。"""
    number = float(value)
    if low is not None and high is not None and float(low) > float(high):
        low = high = None
    if low is not None and number < float(low):
        number = float(low)
    if high is not None and number > float(high):
        number = float(high)
    if integer:
        return float(int(round(number)))
    places = max(0, min(6, int(decimals)))
    return round(number, places)


# --------------------------------------------------------------- 播放器


@dataclass
class CurvePlayer:
    """把曲线"按间隔"喂给点位：谁该发、发多少、什么时候停，都在这里算。"""

    spec: CurveSpec
    interval_s: float = 0.5
    #: 持续多少秒后自动停（0 = 一直发，直到手动停止）
    duration_s: float = 0.0
    decimals: int = 0
    low: float | None = None
    high: float | None = None
    integer: bool = False
    finished: bool = False
    sent: int = 0
    last_value: float | None = None
    _next_at: float = 0.0

    def __post_init__(self) -> None:
        self.interval_s = max(MIN_INTERVAL_S, float(self.interval_s))

    def reset(self) -> None:
        self.finished = False
        self.sent = 0
        self.last_value = None
        self._next_at = 0.0

    def stop(self) -> None:
        self.finished = True

    def tick(self, elapsed_s: float) -> tuple[float | None, bool]:
        """到点了就返回一个值。``(值, 是否刚刚结束)``，值为 None 表示这次不发。"""
        elapsed = max(0.0, float(elapsed_s))
        if self.finished:
            return None, True
        if self.duration_s > 0 and elapsed >= float(self.duration_s):
            self.finished = True
            return None, True
        if elapsed + 1e-9 < self._next_at:
            return None, False

        raw = self.spec.value_at(elapsed)
        if raw is None:
            self.finished = True
            return None, True

        value = quantize_value(
            raw,
            decimals=self.decimals,
            low=self.low,
            high=self.high,
            integer=self.integer,
        )
        self._next_at = elapsed + self.interval_s
        self.sent += 1
        self.last_value = value
        return value, False

    def preview(self, span_s: float | None = None, count: int = 240) -> list[tuple[float, float]]:
        """给界面画预览用的 ``[(t, 值)]``（已经量化过，画出来就是真要发的东西）。"""
        count = max(2, int(count))
        total = span_s if span_s and span_s > 0 else self.preview_span()
        points: list[tuple[float, float]] = []
        for index in range(count):
            t = total * index / (count - 1)
            raw = self.spec.value_at(t)
            if raw is None:
                break
            points.append(
                (
                    t,
                    quantize_value(
                        raw,
                        decimals=self.decimals,
                        low=self.low,
                        high=self.high,
                        integer=self.integer,
                    ),
                )
            )
        return points

    def preview_span(self) -> float:
        """默认预览多长：循环曲线看两个周期，一次性曲线看它全长。"""
        if self.spec.kind == "replay":
            span = len(self.spec.samples) * max(1e-6, self.spec.sample_interval_s)
            return span if span > 0 else 1.0
        return max(1e-6, float(self.spec.period_s)) * 2.0


# --------------------------------------------------------------- CSV 回放


def read_replay_csv(path: str) -> tuple[list[float], float]:
    """读回放曲线：``值`` 一列，或 ``时间,值`` 两列（CSV / TXT / Excel 都行）。

    两列时用相邻时间差的中位数当采样间隔（脚本导出的数据多半不是等间隔的）。
    """
    try:
        rows = sheetio.read_rows(path)
    except sheetio.SheetError as exc:
        raise ValueError(str(exc)) from exc
    except OSError as exc:
        raise ValueError(f"读不了这个文件：{exc}") from exc

    values: list[float] = []
    times: list[float] = []
    for row in rows:
        cells = [cell.strip() for cell in row if str(cell).strip()]
        if not cells:
            continue
        try:
            if len(cells) >= 2:
                times.append(float(cells[0]))
                values.append(float(cells[1]))
            else:
                values.append(float(cells[0]))
        except ValueError:
            continue  # 表头/注释行跳过
    if not values:
        raise ValueError("这个文件里没读到数值（要么是一列值，要么是「时间,值」两列）")

    interval = 0.5
    if len(times) >= 2:
        deltas = sorted(
            later - earlier for earlier, later in zip(times, times[1:]) if later > earlier
        )
        if deltas:
            interval = max(1e-3, deltas[len(deltas) // 2])
    return values, interval


def load_replay(path: str, *, loop: bool = True) -> CurveSpec:
    values, interval = read_replay_csv(path)
    return CurveSpec(
        kind="replay",
        samples=values,
        sample_interval_s=interval,
        loop=loop,
    )


def format_seconds(seconds: float) -> str:
    total = max(0.0, float(seconds))
    minutes, rest = divmod(total, 60.0)
    if minutes >= 1:
        return f"{int(minutes)}:{rest:04.1f}"
    return f"{rest:.1f}s"


def write_replay_csv(path: str, values: list[float], interval_s: float) -> str:
    """把一个数组存成"时间,值"表（CSV 或 XLSX，自测/给现场做回放文件用）。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    rows = [["time_s", "value"]]
    for index, value in enumerate(values):
        rows.append([f"{index * interval_s:.4f}", f"{float(value):.6g}"])
    sheetio.write_rows(path, rows, sheet_title="回放曲线")
    return path


__all__ = [
    "COMFORT_INTERVAL_S",
    "CurvePlayer",
    "CurveSpec",
    "DEFAULT_PERIOD_S",
    "KINDS",
    "MIN_INTERVAL_S",
    "format_seconds",
    "load_replay",
    "quantize_value",
    "read_replay_csv",
    "write_replay_csv",
]
