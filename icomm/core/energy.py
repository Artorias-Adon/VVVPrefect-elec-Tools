"""电能与电费：把功率积成电量，再按电价算钱（纯逻辑，不依赖 Qt）。

现场要回答的问题只有三个：**现在多少 kW**、**用了多少 kWh**、**折多少钱**。
前两个装置一般自己就有，但"这个月电费多少""峰段用了多少"通常没人算，
只能翻电表箱或者月末等供电局单子。这里的做法是：

* **两种取电量的方式**
  - ``power`` 功率积分：读一个有功功率点（kW），按采样间隔积分成 kWh。
    适合没有电量寄存器、或者只有功率的点表（DSP、PCS、变频器常见）。
  - ``reading`` 电量示数：直接读装置的累计电量（kWh）做差值。
    最准 —— 电表本身就是计量器具，积分只是估算。
* **电价可以是单一价，也可以是分时（尖/峰/平/谷）**：按采样时刻落到哪个时段，
  这段电量就按那个单价计费；分时电量也分时段累计，月末对账能直接看。
* **累计要能扛重启**：状态（电量、电费、上次采样时刻）由
  :mod:`icomm.ui.energy_service` 存成 JSON，软件重启后接着算。
* **不凭空造电量**：采样间隔超过 :data:`MAX_STEP_S`（采集停过、电脑睡过）
  那一段直接不计，只把时间戳往后推 —— 宁可少算，也不要给出一个假的大数字。

反向电量（发电倒送）单独累计、**不计电费**：正向按用电计费，反向只能等上网电价，
那套规则各地说法不一，硬算会算错钱。
"""

from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable

#: 默认电价（元/kWh）：工业一般 0.6~0.8，取中间值当初始值，用户自己改
DEFAULT_PRICE = 0.65

#: 采样间隔超过这么多秒就当作"没在采集"，那一段不计电量（采集停过、电脑休眠过）
MAX_STEP_S = 300.0

#: 电量示数倒退多少以上算"表清零/换表"（不是真的倒送）
RESET_TOLERANCE = 0.5

#: 示数型表推"当前功率"要求的最小采样间隔（秒）。
#: 示数差 ÷ 间隔 出来的是平均功率，间隔只有一两秒时，读数一个跳变就能推出
#: 几十万 kW 的假峰值（装置的累计量是攒着跳的，不是每秒均匀涨），所以短间隔只记电量、
#: 不给功率。
MIN_IMPLIED_SPAN_S = 30.0

MODE_POWER = "power"
MODE_READING = "reading"
MODES = {MODE_POWER: "功率积分（读 kW）", MODE_READING: "电量示数（读 kWh）"}
#: 表格里用的短标签（列宽的代价）
MODE_SHORT = {MODE_POWER: "功率积分", MODE_READING: "电量示数"}

SCORES = ("total", "day", "month")
SCOPE_LABELS = {"total": "累计", "day": "今日", "month": "本月"}

#: 分时电价的默认时段（尖峰 / 高峰 / 平段 / 低谷），覆盖全天且不重叠
TOU_SLOTS: tuple[tuple[str, str, str, float], ...] = (
    ("尖峰", "10:00", "11:00", 1.20),
    ("尖峰", "19:00", "21:00", 1.20),
    ("高峰", "08:00", "10:00", 0.95),
    ("高峰", "11:00", "19:00", 0.95),
    ("高峰", "21:00", "22:00", 0.95),
    ("平段", "07:00", "08:00", 0.65),
    ("平段", "22:00", "23:00", 0.65),
    ("低谷", "23:00", "07:00", 0.35),
)


# --------------------------------------------------------------- 小工具


def _minutes(text: str) -> int:
    """``"08:30"`` → 510。格式不对抛 ValueError（调用方会转成人话）。"""
    body = (text or "").strip()
    if ":" not in body:
        raise ValueError(f"「{body}」不是 HH:MM 格式的时间")
    hour_text, minute_text = body.split(":", 1)
    hour, minute = int(hour_text), int(minute_text)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"「{body}」不是合法时间")
    return hour * 60 + minute


def _hhmm(minute_of_day: int) -> str:
    minute = int(minute_of_day) % (24 * 60)
    return f"{minute // 60:02d}:{minute % 60:02d}"


def format_kwh(value: float) -> str:
    """电量：小电量两位小数，上万以后只留一位（现场看的是量级）。"""
    value = float(value)
    if abs(value) >= 10000:
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def format_money(value: float) -> str:
    return f"¥{float(value):,.2f}"


def format_power(value: float) -> str:
    return f"{float(value):,.1f}"


# --------------------------------------------------------------- 电价


@dataclass
class TariffSlot:
    """一个计费时段。``start > end`` 表示跨零点（例如 23:00–07:00 低谷）。"""

    name: str
    start: str
    end: str
    price: float

    @property
    def start_minute(self) -> int:
        return _minutes(self.start)

    @property
    def end_minute(self) -> int:
        return _minutes(self.end)

    def covers(self, minute_of_day: int) -> bool:
        start, end = self.start_minute, self.end_minute
        if start == end:  # 全天
            return True
        if start < end:
            return start <= minute_of_day < end
        return minute_of_day >= start or minute_of_day < end

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "start": self.start, "end": self.end, "price": float(self.price)}

    @classmethod
    def from_dict(cls, data: Any) -> "TariffSlot | None":
        if not isinstance(data, dict):
            return None
        try:
            start, end = str(data.get("start", "")), str(data.get("end", ""))
            _minutes(start)  # 校验格式
            _minutes(end)
            return cls(
                name=str(data.get("name") or "平段"),
                start=start,
                end=end,
                price=max(0.0, float(data.get("price", 0.0))),
            )
        except (TypeError, ValueError):
            return None


@dataclass
class Tariff:
    """电价方案：没有时段就是单一电价，有时段就按时段走分时电价。"""

    price: float = DEFAULT_PRICE
    #: 没有命中任何时段时用的名字（分时电价里就是"平段"）
    base_name: str = "平段"
    slots: list[TariffSlot] = field(default_factory=list)

    @property
    def segmented(self) -> bool:
        return bool(self.slots)

    @property
    def label(self) -> str:
        return "分时电价" if self.segmented else "单一电价"

    def slot_at(self, when: datetime) -> TariffSlot | None:
        minute = when.hour * 60 + when.minute
        for slot in self.slots:
            if slot.covers(minute):
                return slot
        return None

    def price_at(self, when: datetime) -> tuple[str, float]:
        """这一刻属于哪个时段、单价多少。"""
        slot = self.slot_at(when)
        if slot is not None:
            return slot.name, float(slot.price)
        if self.segmented:
            return self.base_name, float(self.price)
        return "单一电价", float(self.price)

    def describe(self) -> str:
        if not self.segmented:
            return f"单一电价 {self.price:.4g} 元/kWh"
        parts = [f"{slot.name} {slot.start}-{slot.end} {slot.price:.4g}" for slot in self.slots]
        return f"分时电价（{self.base_name} {self.price:.4g} 元/kWh）：" + "，".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "price": float(self.price),
            "base_name": self.base_name,
            "slots": [slot.to_dict() for slot in self.slots],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Tariff":
        if not isinstance(data, dict):
            return single_tariff()
        try:
            price = max(0.0, float(data.get("price", DEFAULT_PRICE)))
        except (TypeError, ValueError):
            price = DEFAULT_PRICE
        slots: list[TariffSlot] = []
        raw = data.get("slots")
        if isinstance(raw, list):
            for item in raw:
                slot = TariffSlot.from_dict(item)
                if slot is not None:
                    slots.append(slot)
        return cls(price=price, base_name=str(data.get("base_name") or "平段"), slots=slots)


def single_tariff(price: float = DEFAULT_PRICE) -> Tariff:
    return Tariff(price=max(0.0, float(price)), slots=[])


def tou_tariff(price: float = DEFAULT_PRICE) -> Tariff:
    """默认的尖/峰/平/谷方案（各厂各省不一样，这里给一套能直接用的起点）。"""
    return Tariff(
        price=max(0.0, float(price)),
        slots=[TariffSlot(name, start, end, rate) for name, start, end, rate in TOU_SLOTS],
    )


# --------------------------------------------------------------- 电表


@dataclass
class MeterConfig:
    """一块"电表"要读什么。"""

    name: str = "电表"
    mode: str = MODE_POWER
    #: 有功功率点（kW），``mode=power`` 时用
    power_point: str = ""
    #: 累计电量点（kWh），``mode=reading`` 时用
    reading_point: str = ""
    #: 倍率：CT/PT 变化、或装置报的是 W/0.1kWh 而单位没换算时用
    ratio: float = 1.0
    #: 备注（现场写"#1 进线柜"之类）
    note: str = ""

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            self.mode = MODE_POWER
        try:
            self.ratio = float(self.ratio)
        except (TypeError, ValueError):
            self.ratio = 1.0
        if self.ratio <= 0:
            self.ratio = 1.0

    @property
    def source(self) -> str:
        return self.power_point if self.mode == MODE_POWER else self.reading_point

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "mode": self.mode,
            "power_point": self.power_point,
            "reading_point": self.reading_point,
            "ratio": float(self.ratio),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "MeterConfig | None":
        if not isinstance(data, dict):
            return None
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        return cls(
            name=name,
            mode=str(data.get("mode") or MODE_POWER),
            power_point=str(data.get("power_point") or ""),
            reading_point=str(data.get("reading_point") or ""),
            ratio=data.get("ratio", 1.0),
            note=str(data.get("note") or ""),
        )


@dataclass
class Meter:
    """一块电表的配置 + 累计状态。"""

    config: MeterConfig
    tariff: Tariff = field(default_factory=single_tariff)

    # 正向（用电）/ 反向（发电倒送）电量，分三个口径
    forward_total: float = 0.0
    forward_day: float = 0.0
    forward_month: float = 0.0
    reverse_total: float = 0.0
    reverse_day: float = 0.0
    reverse_month: float = 0.0
    # 电费（只对正向电量计费）
    cost_total: float = 0.0
    cost_day: float = 0.0
    cost_month: float = 0.0
    #: 分时电量：``{时段名: kWh}``（累计口径，月末对账用）
    period_kwh: dict[str, float] = field(default_factory=dict)
    #: 峰值功率（kW）
    peak_kw: float = 0.0
    #: 现在这一刻的功率（界面显示用，不落盘）
    power_kw: float | None = None
    last_power_kw: float | None = None
    #: 上次采样时刻
    last_ts: datetime | None = None
    #: 电量示数模式用：上次读数、起始读数
    reading_last: float | None = None
    reading_base: float | None = None
    #: 计数（透明用）：采样次数、跳过的间隔、示数清零次数
    samples: int = 0
    skipped_gaps: int = 0
    reading_resets: int = 0
    day_key: str = ""
    month_key: str = ""

    # -------------------------------------------------- 累计

    def reset(self, scope: str = "total") -> None:
        """清零。``day`` / ``month`` 只清当期，``total`` 连电费一起清。"""
        if scope == "day":
            self.forward_day = 0.0
            self.reverse_day = 0.0
            self.cost_day = 0.0
            self.peak_kw = 0.0
            return
        if scope == "month":
            self.forward_month = 0.0
            self.reverse_month = 0.0
            self.cost_month = 0.0
            return
        self.forward_total = 0.0
        self.forward_day = 0.0
        self.forward_month = 0.0
        self.reverse_total = 0.0
        self.reverse_day = 0.0
        self.reverse_month = 0.0
        self.cost_total = 0.0
        self.cost_day = 0.0
        self.cost_month = 0.0
        self.period_kwh.clear()
        self.peak_kw = 0.0
        self.samples = 0
        self.skipped_gaps = 0
        self.reading_resets = 0
        self.reading_base = self.reading_last

    def _roll(self, when: datetime) -> None:
        """跨天 / 跨月把"当期"清零（累计与时段电量不动）。"""
        day = when.strftime("%Y-%m-%d")
        month = when.strftime("%Y-%m")
        if day != self.day_key:
            self.day_key = day
            self.forward_day = 0.0
            self.reverse_day = 0.0
            self.cost_day = 0.0
            self.peak_kw = 0.0
        if month != self.month_key:
            self.month_key = month
            self.forward_month = 0.0
            self.reverse_month = 0.0
            self.cost_month = 0.0

    def _add_energy(self, kwh: float, when: datetime) -> float:
        """记一笔电量（正=用电，负=倒送），返回这笔的电费。"""
        if not kwh:
            return 0.0
        cost = 0.0
        if kwh > 0:
            self.forward_total += kwh
            self.forward_day += kwh
            self.forward_month += kwh
            period, price = self.tariff.price_at(when)
            cost = kwh * price
            self.cost_total += cost
            self.cost_day += cost
            self.cost_month += cost
            self.period_kwh[period] = self.period_kwh.get(period, 0.0) + kwh
        else:
            back = -kwh
            self.reverse_total += back
            self.reverse_day += back
            self.reverse_month += back
        return cost

    # -------------------------------------------------- 两种输入

    def accrue(self, power_kw: float, when: datetime) -> float:
        """功率积分：按上次采样到现在的时间差积成电量，返回本次电量（kWh）。"""
        self._roll(when)
        self.power_kw = float(power_kw)
        self.last_power_kw = float(power_kw)
        if power_kw > self.peak_kw:
            self.peak_kw = float(power_kw)

        last = self.last_ts
        self.last_ts = when
        if last is None:
            return 0.0
        delta_s = (when - last).total_seconds()
        if delta_s <= 0:
            return 0.0
        if delta_s > MAX_STEP_S:
            # 这一段没在正常采集（停过采集、电脑睡过）：不凭空造电量
            self.skipped_gaps += 1
            return 0.0

        kwh = float(power_kw) * self.config.ratio * delta_s / 3600.0
        self.samples += 1
        self._add_energy(kwh, when)
        return kwh

    def apply_reading(self, reading: float, when: datetime, *, tolerance: float = RESET_TOLERANCE) -> float:
        """电量示数模式：读累计电量做差值，返回本次电量（kWh）。

        顺带按"这段电量 ÷ 这段时长"推一个平均功率出来，界面上一样能看实时 kW
        （示数型表没有功率寄存器时，这就是唯一能给的功率）。
        """
        self._roll(when)
        last = self.last_ts
        self.last_ts = when
        value = float(reading)
        if self.reading_last is None:
            self.reading_base = value
            self.reading_last = value
            return 0.0
        delta = value - self.reading_last
        self.reading_last = value
        if delta < -tolerance:
            # 表清零 / 换表：重新起算，别把倒退当成巨大的反向电量
            self.reading_base = value
            self.reading_resets += 1
            return 0.0
        if delta < 0:
            delta = 0.0
        kwh = delta * self.config.ratio
        self.samples += 1
        self._add_energy(kwh, when)
        # 示数差是计量器具量出来的，**不受采样间隔上限约束**（间隔多久都得记）；
        # 但"由示数推功率"只有间隔够长才有意义，否则装置的攒着跳变会推出假峰值
        if last is not None:
            span = (when - last).total_seconds()
            if span >= MIN_IMPLIED_SPAN_S and kwh >= 0:
                self.power_kw = kwh * 3600.0 / span
                if self.power_kw > self.peak_kw:
                    self.peak_kw = self.power_kw
            else:
                self.power_kw = None
        return kwh

    # -------------------------------------------------- 展示

    def period_of(self, when: datetime) -> tuple[str, float]:
        return self.tariff.price_at(when)

    def summary(self, when: datetime | None = None) -> dict[str, Any]:
        """一行给人看的数据（界面表格直接用它）。"""
        when = when or datetime.now()
        period, price = self.tariff.price_at(when)
        return {
            "name": self.config.name,
            "mode": MODE_SHORT.get(self.config.mode, self.config.mode),
            "mode_name": MODES.get(self.config.mode, self.config.mode),
            "source": self.config.source,
            "power_kw": self.power_kw,
            "forward_total": self.forward_total,
            "forward_day": self.forward_day,
            "forward_month": self.forward_month,
            "reverse_total": self.reverse_total,
            "cost_total": self.cost_total,
            "cost_day": self.cost_day,
            "cost_month": self.cost_month,
            "peak_kw": self.peak_kw,
            "period": period,
            "price": price,
            "samples": self.samples,
        }

    # -------------------------------------------------- 落盘

    def to_dict(self) -> dict[str, Any]:
        return {
            "config": self.config.to_dict(),
            "forward_total": self.forward_total,
            "forward_day": self.forward_day,
            "forward_month": self.forward_month,
            "reverse_total": self.reverse_total,
            "reverse_day": self.reverse_day,
            "reverse_month": self.reverse_month,
            "cost_total": self.cost_total,
            "cost_day": self.cost_day,
            "cost_month": self.cost_month,
            "period_kwh": dict(self.period_kwh),
            "peak_kw": self.peak_kw,
            "last_power_kw": self.last_power_kw,
            "last_ts": self.last_ts.isoformat() if self.last_ts else "",
            "reading_last": self.reading_last,
            "reading_base": self.reading_base,
            "samples": self.samples,
            "skipped_gaps": self.skipped_gaps,
            "reading_resets": self.reading_resets,
            "day_key": self.day_key,
            "month_key": self.month_key,
        }

    @classmethod
    def from_dict(cls, data: Any, tariff: Tariff) -> "Meter | None":
        if not isinstance(data, dict):
            return None
        config = MeterConfig.from_dict(data.get("config"))
        if config is None:
            return None
        meter = cls(config=config, tariff=tariff)

        def number(key: str) -> float:
            try:
                return float(data.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        for key in ("forward_total", "forward_day", "forward_month",
                    "reverse_total", "reverse_day", "reverse_month",
                    "cost_total", "cost_day", "cost_month", "peak_kw"):
            setattr(meter, key, number(key))
        periods = data.get("period_kwh")
        if isinstance(periods, dict):
            meter.period_kwh = {
                str(name): float(value)
                for name, value in periods.items()
                if isinstance(value, (int, float))
            }
        last_power = data.get("last_power_kw")
        meter.last_power_kw = float(last_power) if isinstance(last_power, (int, float)) else None
        stamp = str(data.get("last_ts") or "")
        if stamp:
            try:
                meter.last_ts = datetime.fromisoformat(stamp)
            except ValueError:
                meter.last_ts = None
        for key in ("reading_last", "reading_base"):
            raw = data.get(key)
            setattr(meter, key, float(raw) if isinstance(raw, (int, float)) else None)
        for key in ("samples", "skipped_gaps", "reading_resets"):
            try:
                setattr(meter, key, int(data.get(key) or 0))
            except (TypeError, ValueError):
                setattr(meter, key, 0)
        meter.day_key = str(data.get("day_key") or "")
        meter.month_key = str(data.get("month_key") or "")
        return meter


# --------------------------------------------------------------- 电表本子


FORMAT_VERSION = 1


@dataclass
class EnergyBook:
    """电价 + 若干块电表。存成 JSON，重启接着算。"""

    tariff: Tariff = field(default_factory=single_tariff)
    meters: list[Meter] = field(default_factory=list)

    # -------------------------------------------------- 电表

    def add(self, config: MeterConfig) -> Meter:
        meter = Meter(config=config, tariff=self.tariff)
        self.meters.append(meter)
        return meter

    def remove(self, index: int) -> bool:
        if 0 <= index < len(self.meters):
            del self.meters[index]
            return True
        return False

    def set_tariff(self, tariff: Tariff) -> None:
        self.tariff = tariff
        for meter in self.meters:
            meter.tariff = tariff

    def find(self, name: str) -> Meter | None:
        for meter in self.meters:
            if meter.config.name == name:
                return meter
        return None

    # -------------------------------------------------- 采样

    def tick(self, now: datetime, value_of: Callable[[str], float | None]) -> bool:
        """采一轮：每块表按自己的方式取一个值。返回是否有任何一块表动过。"""
        moved = False
        for meter in self.meters:
            source = meter.config.source
            if not source:
                continue
            value = value_of(source)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if meter.config.mode == MODE_POWER:
                meter.accrue(number, now)
            else:
                meter.apply_reading(number, now)
            moved = True
        return moved

    def reset(self, scope: str = "total", names: Iterable[str] | None = None) -> int:
        """清零，返回清了几块表。``names`` 为空表示全部。"""
        wanted = set(names or [])
        count = 0
        for meter in self.meters:
            if wanted and meter.config.name not in wanted:
                continue
            meter.reset(scope)
            count += 1
        return count

    # -------------------------------------------------- 落盘

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": FORMAT_VERSION,
            "tariff": self.tariff.to_dict(),
            "meters": [meter.to_dict() for meter in self.meters],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "EnergyBook":
        if not isinstance(data, dict):
            return cls()
        tariff = Tariff.from_dict(data.get("tariff"))
        book = cls(tariff=tariff)
        raw = data.get("meters")
        if isinstance(raw, list):
            for item in raw:
                meter = Meter.from_dict(item, tariff)
                if meter is not None:
                    book.meters.append(meter)
        return book

    def save(self, path: str) -> str:
        """原子写：先写临时文件再替换，断电不会留下半个 JSON。"""
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        text = _json_text(self.to_dict())
        temp = f"{path}.tmp"
        with open(temp, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp, path)
        return path

    @classmethod
    def load(cls, path: str) -> "EnergyBook":
        """读不出来就当空本子（坏文件不会让软件起不来）。"""
        try:
            with open(path, encoding="utf-8-sig") as handle:
                text = handle.read()
        except OSError:
            return cls()
        import json

        try:
            return cls.from_dict(json.loads(text))
        except (ValueError, TypeError):
            return cls()

    # -------------------------------------------------- 导出

    def export_csv(self, path: str, now: datetime | None = None) -> str:
        """导出对账表（Excel 直接打开）。"""
        now = now or datetime.now()
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([f"电价：{self.tariff.describe()}"])
        writer.writerow([f"导出时间：{now.strftime('%Y-%m-%d %H:%M:%S')}"])
        writer.writerow([])
        writer.writerow([
            "电表", "方式", "来源点", "当前功率(kW)", "正向总电量(kWh)", "反向总电量(kWh)",
            "今日电量(kWh)", "本月电量(kWh)", "累计电费(元)", "今日电费(元)", "本月电费(元)",
            "峰值功率(kW)", "当前时段", "单价(元/kWh)",
        ])
        for meter in self.meters:
            row = meter.summary(now)
            writer.writerow([
                row["name"], row["mode"], row["source"],
                "" if row["power_kw"] is None else f"{row['power_kw']:.3f}",
                f"{row['forward_total']:.3f}", f"{row['reverse_total']:.3f}",
                f"{row['forward_day']:.3f}", f"{row['forward_month']:.3f}",
                f"{row['cost_total']:.2f}", f"{row['cost_day']:.2f}", f"{row['cost_month']:.2f}",
                f"{row['peak_kw']:.3f}", row["period"], f"{row['price']:.4g}",
            ])
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        # utf-8-sig：Excel 打开中文表头不乱码
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write(buffer.getvalue())
        return path

    def totals(self, now: datetime | None = None) -> dict[str, float]:
        """所有表加起来（界面上"合计"那一行、也方便做日报）。"""
        now = now or datetime.now()
        rows = [meter.summary(now) for meter in self.meters]
        return {
            "power_kw": sum(row["power_kw"] or 0.0 for row in rows),
            "forward_total": sum(row["forward_total"] for row in rows),
            "forward_day": sum(row["forward_day"] for row in rows),
            "forward_month": sum(row["forward_month"] for row in rows),
            "reverse_total": sum(row["reverse_total"] for row in rows),
            "cost_total": sum(row["cost_total"] for row in rows),
            "cost_day": sum(row["cost_day"] for row in rows),
            "cost_month": sum(row["cost_month"] for row in rows),
        }


def _json_text(data: dict[str, Any]) -> str:
    import json

    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


# --------------------------------------------------------------- 工程里找点位


def suggest_meter(project) -> MeterConfig | None:
    """从工程里挑一个最像"总电表"的配置。

    优先"累计/总 电量"点（示数型最准），其次有功功率点（能算电费、有实时 kW），
    再其次任意电量点。都找不到返回 None（让用户自己选点）。
    """
    power: tuple[str, str] | None = None
    readings: list[tuple[str, str]] = []
    for device, point in project.iter_points():
        if point.is_array:
            continue
        unit = (point.unit or "").strip().lower()
        title = point.title or point.name
        pid = f"{device.name}.{point.name}"
        if unit in ("kw", "千瓦") and power is None:
            power = (pid, title)
        elif not unit and "有功" in title and "功率" in title and power is None:
            power = (pid, title)
        elif unit in ("kwh", "千瓦时", "度", "kw·h"):
            readings.append((pid, title))

    total_readings = [
        item for item in readings if any(word in item[1] for word in ("累计", "总电量", "总计"))
    ]
    if total_readings:
        pid, title = total_readings[0]
        return MeterConfig(name=title, mode=MODE_READING, reading_point=pid)
    if power is not None:
        pid, title = power
        return MeterConfig(name=title, mode=MODE_POWER, power_point=pid)
    if readings:
        pid, title = readings[0]
        return MeterConfig(name=title, mode=MODE_READING, reading_point=pid)
    return None


__all__ = [
    "DEFAULT_PRICE",
    "EnergyBook",
    "FORMAT_VERSION",
    "MAX_STEP_S",
    "Meter",
    "MeterConfig",
    "MODES",
    "MODE_POWER",
    "MODE_READING",
    "RESET_TOLERANCE",
    "SCOPE_LABELS",
    "Tariff",
    "TariffSlot",
    "TOU_SLOTS",
    "format_kwh",
    "format_money",
    "format_power",
    "single_tariff",
    "suggest_meter",
    "tou_tariff",
]
