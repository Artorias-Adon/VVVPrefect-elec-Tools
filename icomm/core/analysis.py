"""电力电子/电气分析用的计算（纯逻辑，可单测）。

四块现场最常用的东西：

* :func:`bode` —— 按 R/L/C 参数算滤波器的幅频/相频（伯德图）；
* :func:`phase_track` —— 从一段采样波形里锁相（频率、相位角）；
* :func:`power_factor` —— 功率因数、功率角与无功；
* :func:`symmetrical_components` —— 三相向量的正序/负序/零序分解。

都是纯计算，不依赖 Qt、不碰工程 —— 所以能拿已知答案的算例直接验。
"""

from __future__ import annotations

import cmath
import csv
import math
from dataclasses import dataclass, field

from . import sheetio

#: 伯德图的默认频段（Hz）与点数
BODE_F_MIN = 1.0
BODE_F_MAX = 100_000.0
BODE_POINTS = 240


# --------------------------------------------------------------- 伯德图


#: 支持的滤波器结构：``(键, 名字, 说明)``
FILTER_TOPOLOGIES: tuple[tuple[str, str, str], ...] = (
    ("rc", "RC 低通", "一级 RC，-20 dB/十倍频"),
    ("rl", "RL 低通", "R 串联、L 并联，-20 dB/十倍频"),
    ("lc", "LC 低通", "L 串联 + C 并联，二阶、-40 dB/十倍频"),
    ("lcl", "LCL 滤波", "L1 串 + C 并 + L2 串，三阶、谐振点更靠前"),
    ("rlc_series", "RLC 串联", "R、L、C 串联，谐振点处增益最高"),
)


@dataclass
class BodePoint:
    freq: float
    gain_db: float
    phase_deg: float


@dataclass
class BodeResult:
    topology: str
    points: list[BodePoint] = field(default_factory=list)
    #: 谐振频率（算得出来才有）
    resonance_hz: float | None = None
    #: -3 dB 截止频率
    cutoff_hz: float | None = None
    #: 工频处的增益/相位（设计滤波器时最关心这个）
    gain_at_50_db: float | None = None
    phase_at_50_deg: float | None = None
    note: str = ""

    def gain_range(self) -> tuple[float, float]:
        if not self.points:
            return 0.0, 0.0
        gains = [point.gain_db for point in self.points]
        return min(gains), max(gains)


def transfer(topology: str, r: float, l: float, c: float, l2: float, freq: float) -> complex:
    """滤波器在 ``freq`` 处的复增益（输出电压/输入电压）。

    单位按现场习惯：R 用 Ω、L（和 l2）用 mH、C 用 μF。
    用复数直接算传递函数，比逐点数积分稳，也不会在谐振点炸掉。
    """
    omega = 2.0 * math.pi * max(0.0, freq)
    s = 1j * omega
    ohm = max(0.0, float(r))
    henry = max(0.0, float(l)) * 1e-3
    henry2 = max(0.0, float(l2)) * 1e-3
    farad = max(0.0, float(c)) * 1e-6

    if topology == "rc":
        return 1.0 / (1.0 + s * ohm * farad) if farad > 0 else complex(1.0)
    if topology == "rl":
        denom = ohm + s * henry
        return ohm / denom if denom != 0 else complex(1.0)
    if topology == "rlc_series":
        denom = 1.0 + s * ohm * farad + s * s * henry * farad
        return 1.0 / denom
    if topology == "lcl":
        if henry <= 0 or farad <= 0:
            return complex(1.0)
        if henry2 <= 0:
            return 1.0 / (s * s * henry * farad + s * ohm * farad + 1.0)
        # L1（含串联电阻）- C 并联 - L2 串联，输出侧开路
        z1 = s * henry + ohm
        zc = 1.0 / (s * farad)
        return zc / (z1 + zc + s * henry2)
    # 默认 LC 低通：L 串联（含等效串联电阻 r）、C 并联
    if henry <= 0 or farad <= 0:
        return complex(1.0)
    return 1.0 / (s * s * henry * farad + s * ohm * farad + 1.0)


def bode(
    topology: str = "lc",
    r: float = 1.0,
    l: float = 1.0,
    c: float = 100.0,
    f_min: float = BODE_F_MIN,
    f_max: float = BODE_F_MAX,
    points: int = BODE_POINTS,
    l2: float = 0.0,
) -> BodeResult:
    """算幅频/相频曲线（对数频率上均匀取点）。"""
    f_min = max(1e-3, float(f_min))
    f_max = max(f_min * 1.0001, float(f_max))
    points = max(8, int(points))
    result = BodeResult(topology=topology)

    ratio = (f_max / f_min) ** (1.0 / (points - 1))
    freq = f_min
    for _index in range(points):
        gain = transfer(topology, r, l, c, l2, freq)
        magnitude = abs(gain)
        result.points.append(
            BodePoint(
                freq=freq,
                gain_db=20.0 * math.log10(magnitude) if magnitude > 1e-12 else -240.0,
                phase_deg=math.degrees(cmath.phase(gain)),
            )
        )
        freq *= ratio

    result.resonance_hz = resonance_hz(topology, l, c, l2)
    result.cutoff_hz = _cutoff(result.points)
    at_50 = _interpolate(result.points, 50.0)
    if at_50 is not None:
        result.gain_at_50_db = at_50.gain_db
        result.phase_at_50_deg = at_50.phase_deg
    result.note = topology_note(topology)
    return result


def resonance_hz(topology: str, l: float, c: float, l2: float = 0.0) -> float | None:
    """解析算谐振频率：``1/(2π√(LC))``；LCL 用 L1∥L2 与 C 谐振。"""
    henry = max(0.0, float(l)) * 1e-3
    farad = max(0.0, float(c)) * 1e-6
    henry2 = max(0.0, float(l2)) * 1e-3
    if henry <= 0 or farad <= 0:
        return None
    if topology == "lcl" and henry2 > 0:
        parallel = (henry * henry2) / (henry + henry2)
        return 1.0 / (2.0 * math.pi * math.sqrt(parallel * farad))
    if topology in ("lc", "lcl", "rlc_series"):
        return 1.0 / (2.0 * math.pi * math.sqrt(henry * farad))
    return None


def topology_note(topology: str) -> str:
    for key, _name, note in FILTER_TOPOLOGIES:
        if key == topology:
            return note
    return ""


def _cutoff(points: list[BodePoint]) -> float | None:
    """-3 dB 截止频率：找第一处向下穿过 -3 dB 的位置（对数频率上线性插值）。"""
    for previous, current in zip(points, points[1:]):
        if previous.gain_db > -3.0 >= current.gain_db:
            span = previous.gain_db - current.gain_db
            ratio = 0.0 if span == 0 else (previous.gain_db + 3.0) / span
            log_f = math.log10(previous.freq) + ratio * (
                math.log10(current.freq) - math.log10(previous.freq)
            )
            return 10.0 ** log_f
    return None


def _interpolate(points: list[BodePoint], freq: float) -> BodePoint | None:
    if not points:
        return None
    for previous, current in zip(points, points[1:]):
        if previous.freq <= freq <= current.freq:
            span = current.freq - previous.freq
            ratio = 0.0 if span == 0 else (freq - previous.freq) / span
            return BodePoint(
                freq=freq,
                gain_db=previous.gain_db + ratio * (current.gain_db - previous.gain_db),
                phase_deg=previous.phase_deg + ratio * (current.phase_deg - previous.phase_deg),
            )
    return None


def key_points(result: BodeResult) -> list[tuple[str, str]]:
    """给人看的关键点（界面上列出来，比看曲线快）。"""
    rows: list[tuple[str, str]] = []
    if result.resonance_hz:
        rows.append(("谐振频率", f"{result.resonance_hz:,.1f} Hz"))
    if result.cutoff_hz:
        rows.append(("-3 dB 截止", f"{result.cutoff_hz:,.1f} Hz"))
    if result.gain_at_50_db is not None:
        rows.append(("工频 50 Hz 增益", f"{result.gain_at_50_db:.2f} dB"))
    if result.phase_at_50_deg is not None:
        rows.append(("工频 50 Hz 相移", f"{result.phase_at_50_deg:.1f}°"))
    low, high = result.gain_range()
    rows.append(("增益范围", f"{low:.1f} ~ {high:.1f} dB"))
    return rows


# --------------------------------------------------------------- 谐波


@dataclass
class HarmonicComponent:
    order: int
    rms: float
    percent_of_fundamental: float


@dataclass
class HarmonicResult:
    fundamental_hz: float
    sample_rate_hz: float
    cycles: int
    components: list[HarmonicComponent] = field(default_factory=list)
    thd_percent: float = 0.0


def read_waveform_csv(path: str) -> list[float]:
    """Load waveform samples from the first column of a CSV / TXT / Excel table."""
    try:
        rows = sheetio.read_rows(path)
    except sheetio.SheetError as exc:
        raise ValueError(str(exc)) from exc
    values: list[float] = []
    skipped_header = False
    for line_number, row in enumerate(rows, 1):
        if not row or not str(row[0]).strip() or str(row[0]).lstrip().startswith("#"):
            continue
        try:
            value = float(str(row[0]).strip())
        except ValueError as exc:
            if not values and not skipped_header:
                skipped_header = True
                continue
            raise ValueError(f"表格第 {line_number} 行不是数值：{row[0]}") from exc
        if not math.isfinite(value):
            raise ValueError(f"表格第 {line_number} 行不是有限数值")
        values.append(value)
    if len(values) < 8:
        raise ValueError("至少需要 8 个有效采样值")
    return values


def harmonic_analysis(
    values: list[float],
    sample_interval_us: float,
    fundamental_hz: float = 50.0,
    max_order: int = 20,
) -> HarmonicResult:
    """Calculate RMS harmonic content using complete fundamental cycles.

    Samples must be uniformly spaced. The calculation removes DC, keeps the
    largest whole number of nominal cycles in the sample window, and evaluates
    the Fourier coefficients at integer multiples of the fundamental.
    """
    if len(values) < 8:
        raise ValueError("至少需要 8 个采样点")
    dt = float(sample_interval_us) / 1_000_000.0
    fundamental = float(fundamental_hz)
    order_limit = int(max_order)
    if not math.isfinite(dt) or dt <= 0:
        raise ValueError("采样间隔必须大于 0")
    if not math.isfinite(fundamental) or fundamental <= 0:
        raise ValueError("基波频率必须大于 0")
    if order_limit < 1 or order_limit > 100:
        raise ValueError("谐波次数必须在 1 到 100 之间")
    samples = [float(value) for value in values]
    if any(not math.isfinite(value) for value in samples):
        raise ValueError("波形包含非有限数值")

    sample_rate = 1.0 / dt
    cycles = int(len(samples) * dt * fundamental + 1e-9)
    if cycles < 1:
        raise ValueError("波形不足一个完整基波周期")
    segment_size = int(round(cycles / (fundamental * dt)))
    if segment_size < 8 or segment_size > len(samples):
        raise ValueError("波形采样点不足以截取完整周期")
    max_nyquist_order = int((sample_rate / 2.0 - 1e-9) / fundamental)
    if order_limit > max_nyquist_order:
        raise ValueError(
            f"采样率 {sample_rate:g} Hz 下，第 {order_limit} 次谐波超过奈奎斯特频率"
        )
    # 每个谐波次数对应 DFT 的第 ``order * cycles`` 根谱线，超过半长会混叠
    if order_limit * cycles > segment_size // 2:
        raise ValueError(
            f"{segment_size} 个采样点（{cycles} 个周波）最多能分辨到第 "
            f"{max(1, segment_size // (2 * cycles))} 次谐波"
        )

    segment = samples[:segment_size]
    mean = sum(segment) / segment_size
    centered = [value - mean for value in segment]
    fundamental_rms: float | None = None
    components: list[HarmonicComponent] = []
    for order in range(1, order_limit + 1):
        cosine = 0.0
        sine = 0.0
        for index, value in enumerate(centered):
            angle = 2.0 * math.pi * order * cycles * index / segment_size
            cosine += value * math.cos(angle)
            sine += value * math.sin(angle)
        peak = 2.0 * math.hypot(cosine, sine) / segment_size
        rms = peak / math.sqrt(2.0)
        if order == 1:
            fundamental_rms = rms
        components.append(HarmonicComponent(order, rms, 0.0))

    if fundamental_rms is None or fundamental_rms <= 1e-15:
        raise ValueError("波形基波幅值为 0，无法计算 THD")
    for component in components:
        component.percent_of_fundamental = component.rms / fundamental_rms * 100.0
    thd = math.sqrt(sum(item.rms * item.rms for item in components[1:])) / fundamental_rms
    return HarmonicResult(
        fundamental_hz=fundamental,
        sample_rate_hz=sample_rate,
        cycles=cycles,
        components=components,
        thd_percent=thd * 100.0,
    )


# --------------------------------------------------------------- 锁相


@dataclass
class PhaseSample:
    time: float
    angle_deg: float
    freq_hz: float


@dataclass
class PhaseResult:
    freq_hz: float = 0.0
    angle_deg: float = 0.0
    samples: list[PhaseSample] = field(default_factory=list)
    cycles: int = 0
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.freq_hz > 0.0


def zero_crossings(values: list[float]) -> list[int]:
    """上升沿过零点的下标（从负到正的跨越点）。"""
    return [
        index
        for index in range(1, len(values))
        if values[index - 1] < 0.0 <= values[index]
    ]


def detrend(values: list[float]) -> tuple[list[float], float]:
    """扣掉直流分量（现场波形常带偏置，不扣会找不到过零点）。"""
    items = [float(value) for value in values]
    average = sum(items) / len(items) if items else 0.0
    return [value - average for value in items], average


def phase_track(values: list[float], dt_s: float, nominal_hz: float = 50.0) -> PhaseResult:
    """从一段波形估算频率与相位角（扣均值 + 过零 + 线性插值）。

    为什么不用复杂 PLL：现场要的是"周波对不对、相位差多少"，
    过零法对谐波和偏置反而更皮实，而且结果能一眼核对
    （一个周期多少个采样点，现场工程师自己心算得出来）。
    """
    result = PhaseResult()
    if len(values) < 4 or dt_s <= 0:
        result.note = "数据太少：至少 4 个采样点，且采样间隔要大于 0"
        return result

    centered, average = detrend(list(values))
    result.note = f"已扣除直流分量 {average:.3f}"

    crossings = zero_crossings(centered)
    if len(crossings) < 2:
        result.note += "；没有找到两个上升沿过零点，无法锁相"
        return result

    times = [_crossing_time(centered, index, dt_s) for index in crossings]
    periods = [second - first for first, second in zip(times, times[1:]) if second > first]
    if not periods:
        result.note += "；过零点间隔异常"
        return result

    mean_period = sum(periods) / len(periods)
    result.freq_hz = 1.0 / mean_period
    result.cycles = len(crossings) - 1

    start = times[0]
    for index in range(len(centered)):
        t = index * dt_s
        angle = ((t - start) / mean_period) % 1.0 * 360.0
        if angle > 359.995:
            # 360° 就是 0° —— 别让界面上出现"相位 360.0°"这种看着像 bug 的值
            angle = 0.0
        result.samples.append(
            PhaseSample(time=t, angle_deg=angle, freq_hz=result.freq_hz)
        )
    result.angle_deg = result.samples[0].angle_deg

    drift = abs(result.freq_hz - nominal_hz) / max(1e-9, nominal_hz)
    if drift > 0.2:
        result.note += f"；频率 {result.freq_hz:.2f} Hz 与额定 {nominal_hz:g} Hz 差得较多"
    spread = max(periods) - min(periods)
    if spread / mean_period > 0.05:
        result.note += f"；各周波差异较大（{spread * 1000:.1f} ms），波形畸变或变频"
    return result


def _crossing_time(values: list[float], index: int, dt_s: float) -> float:
    """过零点的时间（在 ``index-1`` 与 ``index`` 之间线性插值）。"""
    previous = values[index - 1]
    current = values[index]
    span = current - previous
    ratio = 0.0 if span == 0 else (-previous) / span
    return (index - 1 + ratio) * dt_s


def phase_from_waveform(values: list[float], sample_interval_us: int,
                        nominal_hz: float = 50.0) -> PhaseResult:
    """按参数表里的采样间隔（微秒）锁相 —— 界面直接调这个。"""
    if sample_interval_us:
        return phase_track(list(values), max(1e-9, float(sample_interval_us) / 1e6), nominal_hz)
    guess = 1.0 / (nominal_hz * 100.0)
    result = phase_track(list(values), guess, nominal_hz)
    result.note = (
        f"点位没配「采样间隔us」，按 {nominal_hz:g} Hz、每周期 100 点估算；" + result.note
    )
    return result


# --------------------------------------------------------------- 功率


def power_angle(power_factor: float) -> float:
    """功率角 φ = arccos(|PF|)，单位度。"""
    return math.degrees(math.acos(max(-1.0, min(1.0, abs(float(power_factor))))))


def pf_kind(power_factor: float, active_power: float | None = None) -> str:
    """感性/容性（滞后/超前）判断：优先看有功功率方向，否则看 PF 符号。"""
    if active_power is not None and float(active_power) < 0:
        return "容性（电流超前）"
    if float(power_factor) < 0:
        return "容性（电流超前）"
    return "感性（电流滞后）"


@dataclass
class PowerResult:
    power_factor: float = 0.0
    angle_deg: float = 0.0
    apparent: float = 0.0
    reactive: float = 0.0
    kind: str = ""
    note: str = ""


def power_factor(voltage: float, current: float, active: float,
                 pf_direct: float | None = None) -> PowerResult:
    """由电压/电流/有功算功率因数、功率角与无功。

    ``pf_direct`` 给了就直接用它（有些设备本身报 PF），电压电流只用来算视在功率。
    """
    result = PowerResult()
    apparent = abs(float(voltage)) * abs(float(current))
    result.apparent = apparent
    if pf_direct is not None:
        factor = max(-1.0, min(1.0, float(pf_direct)))
        result.note = "功率因数取自设备本身报的值"
    else:
        if apparent <= 0:
            result.note = "电压或电流为 0，算不出功率因数"
            return result
        factor = max(-1.0, min(1.0, float(active) / apparent))
    result.power_factor = factor
    result.kind = pf_kind(factor, active)
    result.angle_deg = power_angle(factor)
    result.reactive = abs(apparent * math.sin(math.radians(result.angle_deg)))
    return result


# --------------------------------------------------------------- 三相向量


@dataclass
class Phasor:
    """一个相量：幅值（有效值）+ 相角（度）。"""

    magnitude: float
    angle_deg: float

    def complex(self) -> complex:
        return cmath.rect(self.magnitude, math.radians(self.angle_deg))

    def text(self) -> str:
        return f"{self.magnitude:.2f} ∠ {self.angle_deg:.1f}°"


@dataclass
class SequenceResult:
    """对称分量分解结果。"""

    positive: Phasor = field(default_factory=lambda: Phasor(0.0, 0.0))
    negative: Phasor = field(default_factory=lambda: Phasor(0.0, 0.0))
    zero: Phasor = field(default_factory=lambda: Phasor(0.0, 0.0))
    note: str = ""

    def unbalance_percent(self) -> float:
        """不平衡度（负序/正序，百分数）。"""
        if self.positive.magnitude <= 0:
            return 0.0
        return self.negative.magnitude / self.positive.magnitude * 100.0


ALPHA = cmath.rect(1.0, math.radians(120.0))  # 旋转因子 a = e^{j120°}


def symmetrical_components(a_phase: Phasor, b_phase: Phasor, c_phase: Phasor) -> SequenceResult:
    """三相 -> 正序/负序/零序（Fortescue 变换）。

    平衡三相应当只有正序；出现负序说明相角/幅值不平衡，出现零序多半是接地问题。
    """
    a = a_phase.complex()
    b = b_phase.complex()
    c = c_phase.complex()
    alpha = ALPHA
    result = SequenceResult(
        positive=_to_phasor((a + alpha * b + alpha * alpha * c) / 3.0),
        negative=_to_phasor((a + alpha * alpha * b + alpha * c) / 3.0),
        zero=_to_phasor((a + b + c) / 3.0),
    )
    unbalanced = result.unbalance_percent()
    if unbalanced < 2.0:
        result.note = f"三相基本平衡（不平衡度 {unbalanced:.2f}%）"
    elif unbalanced < 5.0:
        result.note = f"不平衡度 {unbalanced:.2f}%，略偏大"
    else:
        result.note = (
            f"不平衡度 {unbalanced:.2f}%，明显不平衡（负序会让电机/变压器发热）"
        )
    if result.zero.magnitude > 0.05 * max(result.positive.magnitude, 1e-9):
        result.note += "；零序分量较大，检查是否单相接地或中线问题"
    return result


def _to_phasor(value: complex) -> Phasor:
    return Phasor(magnitude=abs(value), angle_deg=math.degrees(cmath.phase(value)))


def phase_differences(waves: list[list[float]], dt_s: float) -> list[float]:
    """三路（或多路）波形相对**第一路**的相位差，单位度（0~360）。"""
    if not waves:
        return []
    starts: list[float] = []
    periods: list[float] = []
    for wave in waves:
        centered, _average = detrend(list(wave))
        crossings = zero_crossings(centered)
        if len(crossings) < 2:
            starts.append(0.0)
            periods.append(0.0)
            continue
        times = [_crossing_time(centered, index, dt_s) for index in crossings]
        starts.append(times[0])
        gaps = [second - first for first, second in zip(times, times[1:]) if second > first]
        periods.append(sum(gaps) / len(gaps) if gaps else 0.0)

    base = starts[0]
    period = next((value for value in periods if value > 0), 0.0)
    if period <= 0:
        return [0.0 for _ in waves]
    return [((start - base) / period) % 1.0 * 360.0 for start in starts]


def format_points(rows: list[tuple[str, str]]) -> str:
    """``[(名字, 值), …]`` -> 一行文本（界面里显示关键点）。"""
    return "　｜　".join(f"{name}：{value}" for name, value in rows) if rows else "（无）"


# --------------------------------------------------------------- 阶跃响应


#: 少于此点数就没法判断"哪一段是基线、哪一段是稳态"
STEP_MIN_POINTS = 8


@dataclass
class StepResult:
    """阶跃（或突变）响应的几个时间指标。时间单位一律是秒。

    约定：``baseline`` 是波形开头那一段的平均值，``final`` 是结尾那一段的平均值；
    上升沿取 ``y = (v - baseline) / (final - baseline)``，所以下跳（final 更低）
    也照样成立 —— 那时"超调"指往下冲得比稳态更低。
    """

    baseline: float
    final: float
    span: float
    peak: float
    peak_time: float
    delay_time: float | None
    rise_time: float | None
    settle_time: float | None
    overshoot_pct: float
    tail_ripple: float
    dt_s: float
    points: int

    @property
    def duration(self) -> float:
        """整段波形的时长。"""
        return max(0, self.points - 1) * self.dt_s

    def rows(self) -> list[tuple[str, str]]:
        """``[(指标, 文本), …]``，直接给界面表格用。"""
        return [
            ("起始值（基线）", f"{self.baseline:.4g}"),
            ("稳态值", f"{self.final:.4g}"),
            ("幅值变化", f"{self.span:.4g}"),
            ("峰值", f"{self.peak:.4g}"),
            ("峰值时间", format_time(self.peak_time)),
            ("延迟时间（到低阈值）", _opt_time(self.delay_time)),
            ("上升时间", _opt_time(self.rise_time)),
            ("建立时间", _opt_time(self.settle_time)),
            ("超调量", f"{self.overshoot_pct:.2f} %"),
            ("稳态波动（半峰峰）", f"{self.tail_ripple:.4g}"),
        ]


def _opt_time(value: float | None) -> str:
    return "未测到" if value is None else format_time(value)


def format_time(seconds: float) -> str:
    """按量级把秒写成 µs / ms / s —— 阶跃响应动辄微秒级，统一写秒看不清。"""
    if seconds is None:
        return "--"
    magnitude = abs(seconds)
    if magnitude < 1e-3:
        return f"{seconds * 1e6:.1f} µs"
    if magnitude < 1.0:
        return f"{seconds * 1e3:.3f} ms"
    return f"{seconds:.4f} s"


def _median(values: list[float]) -> float:
    """中位数 —— 比平均值抗毛刺，现场波形头上带一两根尖刺很常见。"""
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _crossing_ratio(y: list[float], level: float, dt_s: float) -> float | None:
    """归一化波形第一次穿过 ``level`` 的时刻（在两点之间线性插值）。"""
    for index in range(1, len(y)):
        previous, current = y[index - 1], y[index]
        if previous < level <= current:
            gap = current - previous
            fraction = 0.0 if gap <= 0 else (level - previous) / gap
            return (index - 1 + fraction) * dt_s
    return None


def step_response(
    values: list[float],
    dt_s: float,
    low: float = 0.1,
    high: float = 0.9,
    settle_band: float = 0.02,
    head_ratio: float = 0.1,
    tail_ratio: float = 0.1,
) -> StepResult | None:
    """从一段采样波形里算阶跃响应的指标（上升/建立时间、超调…）。

    参数都是"相对幅值"的比例：``low``/``high`` 是上升时间的两个阈值（默认 10% / 90%），
    ``settle_band`` 是建立时间用的误差带（默认 ±2%），
    ``head_ratio``/``tail_ratio`` 决定拿头尾多少比例的平均当基线/稳态。

    波形没有明显变化（幅值几乎是 0）或点数太少时返回 ``None`` —— 让界面去提示，
    不要在这儿硬编个数字出来。
    """
    if not values or dt_s is None or dt_s <= 0 or len(values) < STEP_MIN_POINTS:
        return None
    wave = [float(value) for value in values]
    head = max(1, int(len(wave) * max(0.01, min(0.45, head_ratio))))
    tail = max(1, int(len(wave) * max(0.01, min(0.45, tail_ratio))))
    final = _median(wave[-tail:])

    # 基线只取"过渡开始之前"的样本：先用头尾均值粗判过渡在哪（穿越一半的位置），
    # 之前的样本才算基线。录波大多能录到阶跃前的稳态；万一波形一开始就在爬
    # （记录从阶跃中途开始），就退回第一个点 —— 这时测得的时间偏大是必然的。
    coarse_base = _median(wave[:head])
    coarse_span = final - coarse_base
    baseline = wave[0]
    onset = 0.0
    if abs(coarse_span) > 1e-12:
        # 从第一个点开始，找出波形**第一次明显离开起始电平**的位置（偏离 5% 幅值）；
        # 这之前的样本才算基线。比"穿越一半"更准：指数上升前段也有一小段贴地。
        # 这个位置同时当作"突变起点"，后面几个时间指标都从它算起 ——
        # 否则录波前面留了一段稳态，延迟时间会把那段静默也算进去。
        reference = wave[0]
        tolerance = 0.05 * abs(coarse_span)
        leave = len(wave) - 1
        for index, value in enumerate(wave):
            if abs(value - reference) > tolerance:
                leave = index
                break
        before = wave[: max(1, leave)]
        baseline = _median(before) if len(before) >= 3 else reference
        onset = max(0, leave - 1) * dt_s
    span = final - baseline
    if abs(span) <= 1e-12:
        return None
    # 头尾的差要有意义：整段峰峰值远大于这个差时，说明波形只是纹波/噪声，没有阶跃
    peak_to_peak = max(wave) - min(wave)
    if peak_to_peak > 0 and abs(span) < 0.2 * peak_to_peak:
        return None

    y = [(value - baseline) / span for value in wave]
    peak_index = max(range(len(y)), key=lambda index: y[index])
    low_time = _crossing_ratio(y, low, dt_s)
    high_time = _crossing_ratio(y, high, dt_s)
    if low_time is not None:
        low_time = max(0.0, low_time - onset)
    if high_time is not None:
        high_time = max(0.0, high_time - onset)
    rise = (high_time - low_time) if (low_time is not None and high_time is not None) else None
    if rise is not None and rise < 0:
        rise = None

    # 建立时间：最后一次跑出误差带之后的那一刻，后面就一直待在带内
    settle: float | None = None
    outside = [
        index for index, value in enumerate(y) if abs(value - 1.0) > max(1e-9, settle_band)
    ]
    if outside:
        last = outside[-1]
        if last < len(y) - 1:
            settle = max(0.0, (last + 1) * dt_s - onset)
    else:
        settle = 0.0

    tail_values = wave[-tail:]
    ripple = (max(tail_values) - min(tail_values)) / 2.0
    if ripple > 0.5 * abs(span):
        # 尾巴自己就在大幅摆动（幅值和"阶跃"一个量级）—— 这不是阶跃响应
        return None
    return StepResult(
        baseline=baseline,
        final=final,
        span=span,
        peak=wave[peak_index],
        peak_time=max(0.0, peak_index * dt_s - onset),
        delay_time=low_time,
        rise_time=rise,
        settle_time=settle,
        overshoot_pct=(y[peak_index] - 1.0) * 100.0,
        tail_ripple=ripple,
        dt_s=dt_s,
        points=len(wave),
    )
