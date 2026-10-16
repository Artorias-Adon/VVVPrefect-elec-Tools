"""故障录波：报警或自选变量一触发，就把**前后**一段波形留下来。

现场最有用的场景是"抓现场"：某个量越限的瞬间往往就是几毫秒到几秒的事，
等看到报警再去点录波，前面那段已经没了。所以这里一直在往回记（环形缓冲），
触发时把"触发前 pre 秒"的快照 + "触发后 post 秒"继续采完，拼成一段落盘。

两类点位都支持，语义按现场习惯分开：

* **标量点位**（母线电压、电流有效值…）：按喂数据的节奏采样，一段就是一条曲线；
* **数组/块点位**（DSP 波形缓冲区）：每一帧本身就是一段波形，缓冲区里通常带着
  触发前的数据，所以"前"这一侧要连**正在采集的那一帧**一起留下。

落盘走 :meth:`Storage.add_waveform`，也就是干脆存进现有的录波表 ——
这样录到的波形能直接用示波器页的录波回放看，不用另做一个查看器。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

#: 单条曲线最多存多少个采样点（防止有人把间隔设成 0.001 秒然后把内存吃满）
MAX_POINTS = 200_000
#: 单次记录最多保留的秒数上限（前后之和）
MAX_SPAN_S = 600.0


@dataclass
class FaultConfig:
    """录波配置。"""

    #: 监视哪些点位
    pids: list[str] = field(default_factory=list)
    #: 触发前保留多久
    pre_s: float = 1.0
    #: 触发后继续录多久
    post_s: float = 1.0
    #: 保存目录（空则不导出 CSV，只入库）
    csv_dir: str = ""
    #: 是否同时导出 CSV（现场常要发给别人看）
    export_csv: bool = True

    def span(self) -> float:
        return max(0.0, float(self.pre_s)) + max(0.0, float(self.post_s))

    def normalized(self) -> "FaultConfig":
        """把明显不合理的数值夹到可用范围（界面允许手输）。"""
        return FaultConfig(
            pids=list(dict.fromkeys(self.pids)),
            pre_s=min(MAX_SPAN_S, max(0.0, _number(self.pre_s, 1.0))),
            post_s=min(MAX_SPAN_S, max(0.0, _number(self.post_s, 1.0))),
            csv_dir=self.csv_dir,
            export_csv=bool(self.export_csv),
        )


@dataclass
class FaultCapture:
    """一次录波的结果：``{点号: 采样值列表}`` 加元信息。"""

    reason: str
    triggered_at: float
    pre_s: float
    post_s: float
    series: dict[str, list[float]] = field(default_factory=dict)
    #: ``{点号: 采样间隔微秒}``
    intervals: dict[str, int] = field(default_factory=dict)
    csv_path: str = ""

    @property
    def point_count(self) -> int:
        return sum(len(values) for values in self.series.values())

    def summary(self) -> str:
        return (
            f"{time.strftime('%H:%M:%S', time.localtime(self.triggered_at))}"
            f"　{len(self.series)} 条曲线 / {self.point_count} 点"
            f"　（前 {self.pre_s:g}s + 后 {self.post_s:g}s）"
        )


@dataclass
class _Series:
    """一个点位的环形缓冲。"""

    pid: str
    is_array: bool = False
    #: 标量：(时间戳, 值)；数组：(时间戳, [值…])
    samples: deque = field(default_factory=deque)
    interval_us: int = 0

    def trim(self, now: float, keep_s: float, max_points: int = MAX_POINTS) -> None:
        oldest = now - max(0.0, keep_s)
        while self.samples and self.samples[0][0] < oldest:
            self.samples.popleft()
        while len(self.samples) > max_points:
            self.samples.popleft()

    def window(self, start: float, end: float) -> list:
        return [values for ts, values in self.samples if start <= ts <= end]


class FaultRecorder:
    """常驻的"往回记"缓冲 + 触发后成段。

    使用方式（主窗口每个采集批次喂一次）::

        recorder.feed(values, now)              # 一直记着 pre 窗口
        recorder.trigger("母线欠压", now)        # 报警/自选变量触发
        capture = recorder.tick(now)            # post 时间到就拿到成品
    """

    def __init__(self, config: FaultConfig | None = None, project: Any = None) -> None:
        self.config = (config or FaultConfig()).normalized()
        self._series: dict[str, _Series] = {}
        self._armed_at: float | None = None
        self._reason = ""
        #: 已录完的次数（界面显示用）
        self.fired = 0
        self.last: FaultCapture | None = None
        #: 谁在用它。缓冲是**共享**的（主窗口建一个，所有「故障录波」窗口都拿同一个），
        #: 所以必须记下"是哪个窗口配的"：否则开两个窗口时两边都会去 tick()，
        #: 谁先跑到谁收尾 —— 录到的段就落进另一个窗口的目录里了。
        self.owner: Any = None
        if project is not None:
            self.configure(self.config, project)

    # -------------------------------------------------- 配置

    def configure(self, config: FaultConfig, project: Any = None) -> None:
        """换一套配置。给了工程就顺手把每个点位的类型/采样间隔记下来。

        调用方会通过 :attr:`owner` 认领这个缓冲（见 :meth:`is_owner`）。
        """
        self.config = config.normalized()
        wanted = set(self.config.pids)
        # 换了一批监视点位就把与它无关的缓冲丢掉，别让内存慢慢长
        for pid in list(self._series):
            if pid not in wanted:
                self._series.pop(pid, None)
        if project is None:
            return
        for pid in self.config.pids:
            found = project.find_point(pid)
            if found is None:
                continue
            _device, point = found
            series = self._series.get(pid)
            if series is None:
                series = _Series(pid=pid)
                self._series[pid] = series
            series.is_array = bool(point.is_array)
            series.interval_us = int(getattr(point, "sample_interval_us", 0) or 0)

    @property
    def pids(self) -> list[str]:
        return list(self.config.pids)

    @property
    def recording(self) -> bool:
        return self._armed_at is not None

    @property
    def armed_at(self) -> float | None:
        return self._armed_at

    def recording_for(self, now: float | None = None) -> float:
        """已经开始录多久了（没在录返回 0）。界面用它判断"该收尾了"。"""
        if self._armed_at is None:
            return 0.0
        now = time.time() if now is None else now
        return max(0.0, now - self._armed_at)

    @property
    def armed_reason(self) -> str:
        return self._reason if self.recording else ""

    def buffered(self) -> dict[str, int]:
        """``{点号: 缓冲里的采样个数}``（界面显示"记住了多少"）。"""
        return {pid: len(series.samples) for pid, series in self._series.items()}

    # -------------------------------------------------- 归属

    def claim(self, owner: Any) -> None:
        """认领这个缓冲（配置它的窗口调一下）。"""
        self.owner = owner

    def is_owner(self, who: Any) -> bool:
        """``who`` 是不是当前在用这个缓冲的人。

        没人认领（``owner is None``，例如单测里直接构造）时一律算"是"，
        这样老代码和单测不用改。
        """
        return self.owner is None or self.owner is who

    def release(self, owner: Any) -> None:
        """放开认领（停止监视的窗口调一下）。"""
        if self.owner is owner:
            self.owner = None

    # -------------------------------------------------- 采集

    def feed(self, values: dict[str, Any], now: float | None = None) -> None:
        """喂一批实时值。只关心配置里的点位，其它一律忽略。"""
        if not self.config.pids:
            return
        now = time.time() if now is None else now
        keep = max(self.config.pre_s, 1.0) * 1.2 + 0.2
        for pid in self.config.pids:
            tag = values.get(pid)
            if tag is None:
                continue
            value = tag.value if hasattr(tag, "value") else tag
            if value is None:
                continue
            quality = getattr(tag, "quality", "good")
            if quality not in ("good", "stale"):
                continue
            series = self._series.get(pid)
            if series is None:
                series = _Series(pid=pid)
                self._series[pid] = series
            if isinstance(value, (list, tuple)):
                series.is_array = True
                series.samples.append((now, [float(v) for v in value]))
            elif isinstance(value, (int, float, bool)):
                series.samples.append((now, float(value)))
            else:
                continue
            series.trim(now, keep if not self.recording else keep + self.config.post_s)

    def trigger(self, reason: str, now: float | None = None) -> bool:
        """开始一次录波。已经在录的话忽略（一次要录完再录下一次）。"""
        if self.recording or not self._series:
            return False
        self._armed_at = now if now is not None else time.time()
        self._reason = reason
        return True

    def tick(self, now: float | None = None) -> FaultCapture | None:
        """到点就把这一段的成品交出来（没在录或还没到时间返回 None）。"""
        if not self.recording:
            return None
        now = time.time() if now is None else now
        assert self._armed_at is not None
        if now - self._armed_at < self.config.post_s:
            return None
        capture = self._build(self._armed_at, now)
        self._armed_at = None
        self._reason = ""
        self.fired += 1
        self.last = capture
        return capture

    def flush(self, now: float | None = None) -> FaultCapture | None:
        """不等 post 时间，立刻收尾（用户手动停止或退出时用）。"""
        if not self.recording:
            return None
        now = time.time() if now is None else now
        assert self._armed_at is not None
        capture = self._build(self._armed_at, now)
        self._armed_at = None
        self._reason = ""
        self.fired += 1
        self.last = capture
        return capture

    def clear(self) -> None:
        for series in self._series.values():
            series.samples.clear()
        self._armed_at = None
        self._reason = ""

    # -------------------------------------------------- 成品

    def _build(self, armed_at: float, now: float) -> FaultCapture:
        capture = FaultCapture(
            reason=self._reason or "手动",
            triggered_at=armed_at,
            pre_s=self.config.pre_s,
            post_s=self.config.post_s,
        )
        start = armed_at - self.config.pre_s
        for pid, series in self._series.items():
            if not series.samples:
                continue
            values: list[float] = []
            if series.is_array:
                # 数组点位：每一帧本身就是一段波形。要连"跨越触发时刻的那一帧"
                # 一起留下 —— 触发前的数据通常就在那一帧里。
                for frame in series.window(start, now):
                    values.extend(frame)
            else:
                for value in series.window(start, now):
                    values.append(value)
            values = values[:MAX_POINTS]
            if values:
                capture.series[pid] = values
                capture.intervals[pid] = (
                    series.interval_us
                    if series.is_array
                    else _scalar_interval_us(series, start, now)
                )
        return capture


def _scalar_interval_us(series: _Series, start: float, end: float) -> int:
    """标量曲线用实际采样节奏当间隔（喂数据的节奏就是采集周期）。"""
    stamps = [ts for ts, _ in series.samples if start <= ts <= end]
    if len(stamps) < 2:
        return 0
    span = stamps[-1] - stamps[0]
    if span <= 0:
        return 0
    return int(span / (len(stamps) - 1) * 1_000_000)


def _number(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------- 落盘


def save_capture(
    capture: FaultCapture,
    storage: Any = None,
    exporter: Callable[[FaultCapture, str], str] | None = None,
) -> int:
    """把一次录波存进录波表（有 storage 时），返回入库的曲线条数。

    CSV 导出交给 ``exporter``（由界面提供，因为要写文件、要拼路径），
    这样核心层不碰文件系统，方便单测。
    """
    saved = 0
    if storage is None:
        return saved
    for pid, values in capture.series.items():
        try:
            storage.add_waveform(
                pid, values, dt_us=capture.intervals.get(pid, 0), ts=capture.triggered_at
            )
        except Exception:  # noqa: BLE001 - 存不进去也不能影响采集
            continue
        saved += 1
    if exporter is not None:
        try:
            capture.csv_path = exporter(capture, capture.reason)
        except OSError:
            capture.csv_path = ""
    return saved


def describe(capture: FaultCapture, title_of: Callable[[str], str] | None = None) -> str:
    """一段录波的一句话说明（列表里显示）。"""
    names: Iterable[str] = capture.series.keys()
    if title_of is not None:
        names = [title_of(pid) for pid in names]
    text = "、".join(list(names)[:3])
    if len(capture.series) > 3:
        text += f" 等 {len(capture.series)} 条"
    return f"{capture.reason}　{text}"
