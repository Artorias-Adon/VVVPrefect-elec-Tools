"""工程数据模型：通道 / 设备 / 点位 / 报警。

本模块为纯数据类，不依赖 Qt，也不依赖任何协议库，
以便单元测试、命令行工具和后续替换 UI 框架。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any

from . import serial_ports

# --------------------------------------------------------------- 取值范围

LINK_TYPES = ("tcp", "rtu", "rtu_over_tcp")
REGISTER_TYPES = ("coil", "discrete", "holding", "input")
#: uint8/int8 是给按字节寻址的协议用的（西门子 S7 的 ``DB1.DBB4`` / ``MB10``）。
#: Modbus 里这两个类型也收，占用 1 个寄存器的低字节（高字节不用）。
DATA_TYPES = (
    "bool",
    "uint8",
    "int8",
    "uint16",
    "int16",
    "uint32",
    "int32",
    "float32",
    "float64",
)
WORD_ORDERS = ("big", "little")
DISPLAY_TYPES = ("none", "card", "gauge", "lamp", "wave")
ALARM_LEVELS = ("info", "warning", "critical")

#: 允许的显示进制：值 -> 中文名（顺序就是界面上的顺序）
DISPLAY_BASES = {10: "十进制", 16: "十六进制", 2: "二进制", 8: "八进制"}

#: 进制写法的别名（导入 CSV / 手填配置时用）
BASE_ALIASES = {
    "dec": 10, "decimal": 10, "10": 10, "十进制": 10,
    "hex": 16, "0x": 16, "16": 16, "十六进制": 16,
    "bin": 2, "binary": 2, "2": 2, "二进制": 2,
    "oct": 8, "8": 8, "八进制": 8,
}


def normalize_base(base) -> int:
    """把各种写法的进制收敛到 10 / 16 / 2 / 8（认不出来就当十进制）。"""
    if isinstance(base, str):
        number = BASE_ALIASES.get(base.strip().lower())
        if number is not None:
            return number
    try:
        number = int(base)
    except (TypeError, ValueError):
        return 10
    return number if number in DISPLAY_BASES else 10

#: 点位种类
#:   scalar 标量：一次读 1 个元素（普通遥测/遥控点）
#:   array  数组：一次读 count 个元素，用于录波（DSP 波形缓冲区整段读回）
POINT_KINDS = ("scalar", "array")

#: 示意图图元库
DIAGRAM_SYMBOLS = (
    "grid",        # 电网 / 电源
    "transformer", # 变压器
    "breaker",     # 断路器 / 开关
    "bus",         # 母线
    "rectifier",   # 整流（AC/DC）
    "dcdc",        # DC/DC 变换器
    "inverter",    # 逆变（DC/AC）
    "inductor",    # 电感 / 滤波电抗
    "capacitor",   # 电容 / 直流支撑
    "load",        # 负载
    "meter",       # 仪表 / 测量点
    "dsp",         # 控制器 / DSP 板卡
    "text",        # 纯文字标注
)

#: 示意图连线类型
DIAGRAM_LINK_STYLES = ("ac", "dc", "signal")

# 读位类型（线圈/离散输入）
BIT_REGISTERS = ("coil", "discrete")
# 可写类型（线圈/保持寄存器）
WRITABLE_REGISTERS = ("coil", "holding")

#: 各数据类型占用的寄存器数量（字），bool 走位操作，这里的 1 仅作占位
WORD_COUNT: dict[str, int] = {
    "bool": 1,
    "uint8": 1,
    "int8": 1,
    "uint16": 1,
    "int16": 1,
    "uint32": 2,
    "int32": 2,
    "float32": 2,
    "float64": 4,
}


def is_bit_type(data_type: str) -> bool:
    """该数据类型是否按位寻址（线圈/离散输入）。"""
    return data_type == "bool"


def _pretty_number(value: float) -> str:
    """把数值写成人类友好形式（整数不带小数点）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:g}"


def word_count(data_type: str) -> int:
    return WORD_COUNT.get(data_type, 1)


#: 位宽不等于"寄存器数 × 16"的类型
_BIT_WIDTHS: dict[str, int] = {"bool": 1, "uint8": 8, "int8": 8}


def bit_width(data_type: str) -> int:
    """该数据类型的位宽。位域点位用它筛掉超出范围的位定义。"""
    if data_type in _BIT_WIDTHS:
        return _BIT_WIDTHS[data_type]
    return word_count(data_type) * 16


def point_id(device_name: str, tag_name: str) -> str:
    """点位全局唯一标识：``设备名.点号``。"""
    return f"{device_name}.{tag_name}"


def parse_point_id(pid: str) -> tuple[str, str]:
    device, _, tag = pid.partition(".")
    return device, tag


def default_channel_options(link: str) -> dict[str, Any]:
    """按链路类型给出连接的默认参数。

    串口默认给 **9600 8E1（偶校验）** —— Modbus RTU 现场最常用的组合；
    改成别的组合在设置里两处都能改。
    """
    if link in ("tcp", "rtu_over_tcp"):
        return {"host": "127.0.0.1", "port": 502}
    return {
        "port": serial_ports.DEFAULT_PORT,
        "baudrate": serial_ports.DEFAULT_BAUDRATE,
        "bytesize": serial_ports.DEFAULT_BYTESIZE,
        "parity": serial_ports.DEFAULT_PARITY,
        "stopbits": serial_ports.DEFAULT_STOPBITS,
    }


# --------------------------------------------------------------- 位域 / 命令解析


def parse_bit_labels(text: str) -> list[tuple[int, str]]:
    """解析位定义文本。

    ``"0:INIT,1:自检,5:运行"`` -> ``[(0, "INIT"), (1, "自检"), (5, "运行")]``

    分隔符可用逗号或分号，空白会被忽略，写错的行直接跳过（不影响其它位）。
    """
    items: list[tuple[int, str]] = []
    for chunk in (text or "").replace("；", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        index_text, _, label = chunk.partition(":")
        try:
            index = int(index_text.strip(), 0)
        except ValueError:
            continue
        if 0 <= index <= 63:
            items.append((index, label.strip()))
    return items


def parse_commands(text: str) -> list[tuple[int, str]]:
    """解析命令按钮文本。

    ``"0x04:停机,0x20:启动,0x80:复位"`` -> ``[(4, "停机"), (32, "启动"), (128, "复位")]``

    值支持十进制与十六进制（``0x`` 前缀）。
    """
    items: list[tuple[int, str]] = []
    for chunk in (text or "").replace("；", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        value_text, _, label = chunk.partition(":")
        try:
            value = int(value_text.strip(), 0)
        except ValueError:
            continue
        items.append((value, label.strip()))
    return items


def parse_code_labels(text: str) -> list[tuple[int, str]]:
    """解析"值 -> 名称"表（状态码）。

    ``"0:停机,1:充电,2:放电"`` -> ``[(0, "停机"), (1, "充电"), (2, "放电")]``

    写法与 :func:`parse_bit_labels` 一样（``值:名称``，逗号分隔），值支持十进制与十六进制。
    **含义不同**：位定义说的是"每一位什么意思"，状态码说的是"整个值什么意思"。
    有些装置的运行状态就是一个编号（0/1/2…）而不是位掩码，这时该用 ``codes``。
    """
    items: list[tuple[int, str]] = []
    for chunk in (text or "").replace("；", ",").replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        value_text, _, label = chunk.partition(":")
        try:
            value = int(value_text.strip(), 0)
        except ValueError:
            continue
        if 0 <= value <= 0xFFFF:
            items.append((value, label.strip()))
    return items


# --------------------------------------------------------------- 数据类


@dataclass
class AlarmDef:
    """点位报警阈值定义。"""

    enabled: bool = False
    level: str = "warning"
    hi_hi: float | None = None
    hi: float | None = None
    lo: float | None = None
    lo_lo: float | None = None
    deadband: float = 0.0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AlarmDef":
        return _build(cls, data)


@dataclass
class PointDef:
    """单个采集点位（变量）。

    ``kind == "array"`` 时表示数组/块点位：一次读回 ``count`` 个元素，
    用于把 DSP 的波形缓冲区整段读回来画示波器。
    """

    name: str
    register: str = "holding"
    #: 协议路径。Modbus 不用（地址在 address 里）；其它协议按各自写法填：
    #:   IEC 61850 —— 对象引用，如 ``MEAS/MMXU1.Hz.mag.f``（FC 写在 register 里）
    #:   CAN 原始帧 —— 帧号，如 ``0x181``（字节偏移写在 address 里）
    #:   CANopen —— ``索引:子索引``，如 ``0x6041:0``（节点号取设备的从站号）
    path: str = ""
    address: int = 0
    data_type: str = "uint16"
    word_order: str = "big"
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""
    decimals: int = 2
    #: 显示进制：10 十进制（默认）/ 16 十六进制 / 2 二进制 / 8 八进制。
    #: 只影响界面显示，读到/写下去的原始值不变。
    display_base: int = 10
    writable: bool = False
    archive: bool = True
    deadband: float = 0.0
    label: str = ""
    display: str = "card"
    gauge_min: float = 0.0
    gauge_max: float = 100.0
    group: str = "默认"
    description: str = ""
    alarm: AlarmDef = field(default_factory=AlarmDef)
    # ---- 数组/块点位与示波器 ----
    kind: str = "scalar"
    count: int = 1
    #: kind=array 时 DSP 侧相邻采样点的时间间隔（微秒）；0 表示未知，示波器按点序号显示
    sample_interval_us: int = 0
    #: 是否把整段录波写入数据库（数组点位数据量大，默认关闭，按需开启）
    record: bool = False
    #: 是否默认出现在示波器通道列表里
    scope: bool = False
    #: 曲线颜色，留空则自动分配
    scope_color: str = ""
    #: 位域点位的位定义，如 "0:INIT,1:自检,5:运行"；设置后界面显示位名而不是数字
    bits: str = ""
    #: 命令点位的按钮定义，如 "0x04:停机,0x20:启动"；设置后界面显示按钮，点击即写入该值
    commands: str = ""
    #: 状态码表，如 "0:停机,1:充电,2:放电"；设置后界面把整个值翻译成名称显示。
    #: 与 ``bits`` 的区别：``bits`` 是"每一位什么意思"（位掩码），
    #: ``codes`` 是"整个值什么意思"（编号）。两个都填时以 ``bits`` 为准。
    codes: str = ""
    #: 写值下限 / 上限（NaN 表示不限）。现场常见的需求：给定值不许超过某个范围，
    #: 手滑把 700 打成 70000 可能直接炸设备。引擎在**单发与持续发送两条路径**上都会夹紧。
    write_min: float | None = None
    write_max: float | None = None
    #: 夹紧后是否提示（默认提示；批量脚本可以关掉免得刷屏）
    write_limit_warn: bool = True

    def clamp_write(self, value: Any) -> tuple[Any, str]:
        """按写值上下限夹紧，返回 ``(夹紧后的值, 说明)``。

        说明为空表示没夹紧。区间为空（下限 > 上限）时按"不设限"处理，
        免得把点位锁死成写不进去。
        """
        if value is None or self.data_type == "bool":
            return value, ""
        low, high = self.write_min, self.write_max
        if low is not None and high is not None and low > high:
            return value, ""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value, ""
        clamped = number
        note = ""
        if low is not None and number < low:
            clamped = low
            note = f"低于下限 {low}"
        elif high is not None and number > high:
            clamped = high
            note = f"超过上限 {high}"
        if not note:
            return value, ""
        if self.data_type.startswith(("int", "uint")):
            clamped = int(clamped)
        return clamped, note

    @property
    def has_write_limit(self) -> bool:
        return self.write_min is not None or self.write_max is not None

    def write_limit_text(self) -> str:
        low = "-∞" if self.write_min is None else _pretty_number(self.write_min)
        high = "+∞" if self.write_max is None else _pretty_number(self.write_max)
        unit = f" {self.unit}" if self.unit else ""
        return f"{low} ~ {high}{unit}"

    @property
    def title(self) -> str:
        return self.label or self.name

    def bit_labels(self) -> list[tuple[int, str]]:
        """位定义，已按本点位的数据类型裁掉越界的位序号。

        与 ``decode_bits`` 配合时可直接把 ``bit_labels()`` 传进去，
        不必在外面再判断数据类型。
        """
        limit = bit_width(self.data_type)
        return [(index, label) for index, label in parse_bit_labels(self.bits) if index < limit]

    def command_items(self) -> list[tuple[int, str]]:
        return parse_commands(self.commands)
    def code_labels(self) -> list[tuple[int, str]]:
        """状态码表（整个值 -> 名称）。"""
        return parse_code_labels(self.codes)

    def code_label(self, value: Any) -> str:
        """把数值翻译成状态名；表里没有这个值就返回空串（界面回退显示数字）。"""
        if value is None:
            return ""
        try:
            number = int(value)
        except (TypeError, ValueError):
            return ""
        for code, label in self.code_labels():
            if code == number:
                return label
        return ""

    @staticmethod
    def decode_bits(value: Any, labels: list[tuple[int, str]]) -> list[str]:
        """把数值翻译成当前置位的位名。"""
        if value is None or not labels:
            return []
        try:
            number = int(value)
        except (TypeError, ValueError):
            return []
        return [label for index, label in labels if number & (1 << index)]

    @property
    def is_array(self) -> bool:
        return self.kind == "array"

    @property
    def element_width(self) -> int:
        """单个元素占用的寄存器数。"""
        return word_count(self.data_type) if not is_bit_type(self.data_type) else 1

    @property
    def span(self) -> int:
        """该点位占用的寄存器/位总数。"""
        return max(1, self.count) * self.element_width if self.is_array else self.element_width

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["alarm"] = self.alarm.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PointDef":
        data = dict(data)
        alarm = data.pop("alarm", None)
        point = _build(cls, data)
        point.alarm = AlarmDef.from_dict(alarm)
        if point.kind not in POINT_KINDS:
            point.kind = "scalar"
        if point.is_array:
            point.count = max(1, int(point.count or 1))
        point.display_base = normalize_base(point.display_base)
        return point


@dataclass
class DeviceDef:
    """一台从站设备：隶属于某个通道，带一组点位。"""

    name: str
    channel: str = ""
    protocol: str = "modbus"
    unit_id: int = 1
    poll_interval_ms: int = 1000
    timeout_ms: int = 1000
    retries: int = 1
    enabled: bool = True
    description: str = ""
    points: list[PointDef] = field(default_factory=list)

    def point(self, name: str) -> PointDef | None:
        for p in self.points:
            if p.name == name:
                return p
        return None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["points"] = [p.to_dict() for p in self.points]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeviceDef":
        data = dict(data)
        points = data.pop("points", []) or []
        device = _build(cls, data)
        device.points = [PointDef.from_dict(p) for p in points]
        return device


@dataclass
class ChannelDef:
    """一条物理链路（一个 TCP 连接或一个串口）。

    同一通道下的多台设备共用连接，符合 Modbus 总线/多从站的实际拓扑。
    """

    name: str
    driver: str = "modbus"
    link: str = "tcp"
    enabled: bool = True
    description: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    #: 每种链路类型各自记住的连接参数。
    #: 现场调试时常要在 RTU（插串口）与 TCP（接网关/仿真器）之间来回切，
    #: 若切换时把参数重置掉，切回来就得重新填串口号或端口号。
    options_by_link: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChannelDef":
        channel = _build(cls, data)
        if not channel.options:
            channel.options = default_channel_options(channel.link)
        # 旧工程没有 options_by_link：把当前这套参数归到当前链路名下，切走再切回来仍在
        channel.options_by_link.setdefault(channel.link, dict(channel.options))
        return channel

    def remember_options(self) -> None:
        """把当前链路的参数记进 ``options_by_link``。"""
        self.options_by_link[self.link] = dict(self.options)

    def switch_link(self, link: str, defaults: dict[str, Any] | None = None) -> None:
        """切换链路类型，并恢复该链路上次用过的参数。

        ``defaults`` 由界面层按驱动声明的参数规格传入（驱动层与模型层不能互相 import）。
        """
        if link == self.link and self.options:
            return
        self.remember_options()
        self.link = link
        remembered = self.options_by_link.get(link)
        if remembered:
            self.options = dict(remembered)
        elif defaults:
            self.options = dict(defaults)
        else:
            self.options = default_channel_options(link)


# --------------------------------------------------------------- 示意图


@dataclass
class DiagramValue:
    """图元下面挂的一个实时量（电压 / 电流 / 功率 / 电度…）。

    一个图元最多挂几个（界面按行显示），比只有一个 ``value_point`` 更贴近现场：
    主回路图上"这台变压器下面要有 6.3 kV、120 A、800 kVA"是常态。
    """

    point: str = ""
    #: 显示名（留空就用点位标题）
    label: str = ""
    unit: str = ""
    decimals: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Any) -> "DiagramValue | None":
        if isinstance(data, str):  # 兼容"只写了点号"的简写
            return cls(point=data.strip()) if data.strip() else None
        if not isinstance(data, dict):
            return None
        point = str(data.get("point") or "").strip()
        if not point:
            return None
        try:
            decimals = int(data.get("decimals", 1))
        except (TypeError, ValueError):
            decimals = 1
        return cls(
            point=point,
            label=str(data.get("label") or ""),
            unit=str(data.get("unit") or ""),
            decimals=max(0, min(6, decimals)),
        )


@dataclass
class DiagramNode:
    """示意图上的一个节点（图元）。

    坐标用 0~1 归一化，绘制时按画面尺寸缩放，换分辨率不用改图。
    """

    id: str
    symbol: str = "text"
    label: str = ""
    sub_label: str = ""
    x: float = 0.0
    y: float = 0.0
    w: float = 0.14
    h: float = 0.10
    color: str = ""
    #: 点击行为：none | device（打开设备详情）| point（趋势）| page（切页面）| url
    action: str = "none"
    #: 行为目标：设备名 / 点号（多个用 | 分隔）/ 页面名 / 链接
    target: str = ""
    #: 节点上直接叠加显示的实时点号，留空不显示
    value_point: str = ""
    unit: str = ""
    decimals: int = 1
    description: str = ""
    #: 模块下面另外要显示的量（电压/电流/功率…，右键里加）
    values: list[DiagramValue] = field(default_factory=list)
    #: 开关量操作点（可写的线圈/保持寄存器），右键菜单据此给出合闸/分闸
    switch_point: str = ""
    switch_on: float = 1.0
    switch_off: float = 0.0
    #: 开关状态点（只读）：读回 0/1 用来把图元画成合/分，右键里也能看当前状态
    state_point: str = ""
    state_on: float = 1.0
    #: 状态点是个位域（遥信字）时取第几位判断合位；-1 = 不按位，直接和 ``state_on`` 比
    state_bit: int = -1

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["values"] = [value.to_dict() for value in self.values]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiagramNode":
        node = _build(cls, {k: v for k, v in (data or {}).items() if k != "values"})
        raw = (data or {}).get("values")
        if isinstance(raw, list):
            for item in raw:
                value = DiagramValue.from_dict(item)
                if value is not None:
                    node.values.append(value)
        return node

    # -------------------------------------------------- 便捷

    @property
    def bound_points(self) -> list[str]:
        """这个图元引用到的所有点号（含叠加值、开关点、状态点），去重保序。"""
        seen: list[str] = []
        for pid in [self.value_point, self.state_point, self.switch_point]:
            if pid and pid not in seen:
                seen.append(pid)
        for value in self.values:
            if value.point and value.point not in seen:
                seen.append(value.point)
        return seen

    @property
    def devices(self) -> list[str]:
        """图元关联到的设备名：点击行为里的设备 + 各点号前缀里的设备。"""
        names: list[str] = []
        if self.action == "device" and self.target:
            for part in str(self.target).split("|"):
                if part.strip() and part.strip() not in names:
                    names.append(part.strip())
        for pid in self.bound_points:
            device = str(pid).split(".", 1)[0] if "." in str(pid) else ""
            if device and device not in names:
                names.append(device)
        return names


@dataclass
class DiagramLink:
    """节点之间的连线。"""

    source: str
    target: str
    style: str = "ac"
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiagramLink":
        return _build(cls, data)


@dataclass
class DiagramHotspot:
    """SVG 底图上的可点击区域（归一化矩形）。"""

    id: str
    x: float = 0.0
    y: float = 0.0
    w: float = 0.1
    h: float = 0.1
    label: str = ""
    action: str = "none"
    target: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiagramHotspot":
        return _build(cls, data)


@dataclass
class DiagramDef:
    """一张示意图。

    ``nodes`` 非空时用内置图元库绘制；也可以只挂一张 SVG 底图（``svg_background``）
    再在 ``hotspots`` 里标注可点击区域，两种方式可同时使用（图元叠在底图上）。
    """

    title: str = ""
    svg_background: str = ""
    nodes: list[DiagramNode] = field(default_factory=list)
    links: list[DiagramLink] = field(default_factory=list)
    hotspots: list[DiagramHotspot] = field(default_factory=list)

    def node(self, node_id: str) -> DiagramNode | None:
        for item in self.nodes:
            if item.id == node_id:
                return item
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "svg_background": self.svg_background,
            "nodes": [n.to_dict() for n in self.nodes],
            "links": [l.to_dict() for l in self.links],
            "hotspots": [h.to_dict() for h in self.hotspots],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DiagramDef":
        if not data:
            return cls()
        diagram = cls(
            title=data.get("title", ""), svg_background=data.get("svg_background", "")
        )
        diagram.nodes = [DiagramNode.from_dict(n) for n in data.get("nodes", [])]
        diagram.links = [DiagramLink.from_dict(l) for l in data.get("links", [])]
        diagram.hotspots = [DiagramHotspot.from_dict(h) for h in data.get("hotspots", [])]
        return diagram


# --------------------------------------------------------------- 存储位置


@dataclass
class ProjectStorage:
    """数据库与参数表的存放位置。

    * 两者都放工程目录内 => 工程目录模式（默认，可整体打包为 .iprj）
    * 任一项指向自定义路径 => 松散模式（例如库放网络盘、参数表放共享目录给同事编辑）
    """

    db_in_project: bool = True
    db_path: str = "history.db"
    csv_in_project: bool = True
    csv_path: str = "parameters.csv"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProjectStorage":
        return _build(cls, data)


# --------------------------------------------------------------- 工程


@dataclass
class PeriodicWriteDef:
    """持续发送（周期写值）的一条配置。

    现场调试时经常需要给某个给定值持续发同一个数（例如把母线电压给定钉在 700 V，
    或周期性地给心跳寄存器写 1）。这里只保存"发什么、多久发一次"，
    运行状态（已发次数、最近错误）由采集引擎持有。
    """

    pid: str
    value: Any = 0
    interval_ms: int = 1000

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "PeriodicWriteDef":
        return _build(cls, data)


@dataclass
class Project:
    """一个工程 = 通道 + 设备 + 点位 + 示意图 + 存储设置。"""

    name: str = "未命名工程"
    #: 磁盘上的文件夹名（英文）。留空 = 按显示名自动生成。
    #: 为什么分开：磁盘目录一律 ASCII（命令行/ZIP/英文系统上不出乱码），
    #: 而界面上显示的中文工程名照旧。在「设置 → 工程设置」里可以改。
    folder_name: str = ""
    channels: list[ChannelDef] = field(default_factory=list)
    devices: list[DeviceDef] = field(default_factory=list)
    history_interval_s: int = 10
    history_retention_days: int = 30
    storage: ProjectStorage = field(default_factory=ProjectStorage)
    diagram: DiagramDef = field(default_factory=DiagramDef)
    #: 持续发送（周期写值）配置。只记配置不记运行状态：
    #: 打开工程时不会自动开始发送，必须手动点开始。
    periodic_writes: list[PeriodicWriteDef] = field(default_factory=list)
    #: 记事本内容（工具 →「记事本」）。跟着工程走，打包 .iprj 时一起带走。
    notes: str = ""

    # -------------------------------------------------- 查询辅助

    def channel(self, name: str) -> ChannelDef | None:
        for c in self.channels:
            if c.name == name:
                return c
        return None

    def device(self, name: str) -> DeviceDef | None:
        for d in self.devices:
            if d.name == name:
                return d
        return None

    def iter_points(self) -> list[tuple[DeviceDef, PointDef]]:
        return [(d, p) for d in self.devices for p in d.points]

    def all_point_ids(self) -> list[str]:
        return [point_id(d.name, p.name) for d, p in self.iter_points()]

    def find_point(self, pid: str) -> tuple[DeviceDef, PointDef] | None:
        device_name, tag = parse_point_id(pid)
        device = self.device(device_name)
        if device is None:
            return None
        point = device.point(tag)
        if point is None:
            return None
        return device, point

    def display_points(self) -> list[tuple[DeviceDef, PointDef]]:
        """需要在仪表盘上显示的点位，按分组和顺序排列。"""
        return [
            (d, p)
            for d, p in self.iter_points()
            if p.display != "none" and d.enabled
        ]

    # -------------------------------------------------- 序列化

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "folder_name": self.folder_name,
            "history_interval_s": self.history_interval_s,
            "history_retention_days": self.history_retention_days,
            "storage": self.storage.to_dict(),
            "channels": [c.to_dict() for c in self.channels],
            "devices": [d.to_dict() for d in self.devices],
            "diagram": self.diagram.to_dict(),
            "periodic_writes": [p.to_dict() for p in self.periodic_writes],
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Project":
        project = cls(
            name=data.get("name", "未命名工程"),
            history_interval_s=int(data.get("history_interval_s", 10)),
            history_retention_days=int(data.get("history_retention_days", 30)),
            storage=ProjectStorage.from_dict(data.get("storage")),
            diagram=DiagramDef.from_dict(data.get("diagram")),
        )
        declared = data.get("folder_name")
        project.folder_name = declared.strip() if isinstance(declared, str) else ""
        project.channels = [ChannelDef.from_dict(c) for c in data.get("channels", [])]
        project.devices = [DeviceDef.from_dict(d) for d in data.get("devices", [])]
        project.periodic_writes = [
            PeriodicWriteDef.from_dict(p) for p in data.get("periodic_writes", [])
        ]
        notes = data.get("notes")
        project.notes = notes if isinstance(notes, str) else ""
        return project

    # -------------------------------------------------- 持续发送

    def periodic_write(self, pid: str) -> "PeriodicWriteDef | None":
        for entry in self.periodic_writes:
            if entry.pid == pid:
                return entry
        return None

    def set_periodic_write(self, pid: str, value: Any, interval_ms: int = 1000) -> "PeriodicWriteDef":
        """登记/更新一条持续发送配置（同一点位只保留一条）。"""
        interval_ms = max(50, int(interval_ms))
        self.periodic_writes = [p for p in self.periodic_writes if p.pid != pid]
        entry = PeriodicWriteDef(pid=pid, value=value, interval_ms=interval_ms)
        self.periodic_writes.append(entry)
        return entry

    def clear_periodic_write(self, pid: str) -> bool:
        before = len(self.periodic_writes)
        self.periodic_writes = [p for p in self.periodic_writes if p.pid != pid]
        return len(self.periodic_writes) != before

    def waveform_points(self) -> list[tuple[DeviceDef, PointDef]]:
        """所有数组/块点位（录波用）。"""
        return [(d, p) for d, p in self.iter_points() if p.is_array]

    def scope_points(self) -> list[tuple[DeviceDef, PointDef]]:
        """示波器可选点位：显式标记的，或所有数组点位。"""
        return [
            (d, p)
            for d, p in self.iter_points()
            if p.scope or p.is_array or p.display == "wave"
        ]


# --------------------------------------------------------------- 内部工具


def _build(cls: type, data: dict[str, Any] | None):
    """按字段名过滤后构造数据类，忽略多余键以兼容旧工程文件。"""
    if not data:
        return cls()
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in allowed})
