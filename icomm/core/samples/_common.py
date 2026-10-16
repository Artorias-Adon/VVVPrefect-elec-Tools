"""示例工程共用的构造小工具。

内置示例一个不能少：每个示例（储能 / 光伏 / SST / 电力系统 / 配电柜）都要
建点位、配报警、画主回路图，写法要一致。这里放这些共用的写法，
示例模块只管"这台设备有哪些量、地址是多少、画成什么样"。

**依赖方向**：本包只依赖 ``..models``，**不要** import ``..project`` ——
``project.py`` 要 import 本包来注册内置示例，反过来引就成环了。
"""

from __future__ import annotations

from ..models import AlarmDef, DiagramDef, DiagramLink, DiagramNode, PointDef

__all__ = [
    "alarm",
    "diagram",
    "link",
    "node",
    "scalar",
    "wave_point",
]


def alarm(
    level: str = "warning",
    hi_hi: float | None = None,
    hi: float | None = None,
    lo: float | None = None,
    lo_lo: float | None = None,
    deadband: float = 0.0,
    message: str = "",
) -> AlarmDef:
    """启用一个报警阈值（只写用得到的档位）。"""
    return AlarmDef(
        enabled=True,
        level=level,
        hi_hi=hi_hi,
        hi=hi,
        lo=lo,
        lo_lo=lo_lo,
        deadband=deadband,
        message=message,
    )


def scalar(
    name: str,
    label: str,
    address: int,
    unit: str = "",
    scale: float = 1.0,
    decimals: int = 2,
    data_type: str = "uint16",
    register: str = "holding",
    writable: bool = False,
    group: str = "遥测",
    display: str = "card",
    description: str = "",
    alarm_def: AlarmDef | None = None,
    bits: str = "",
    commands: str = "",
    write_min: float | None = None,
    write_max: float | None = None,
    archive: bool = True,
    gauge_min: float = 0.0,
    gauge_max: float = 100.0,
) -> PointDef:
    """一个标量点位。``scale`` 用于把寄存器原始值换算成工程量。

    例：电压 0.1V/LSB 就填 ``scale=0.1``，寄存器里存 2200 读出来是 220.0。
    """
    return PointDef(
        name=name,
        label=label,
        register=register,
        address=address,
        data_type=data_type,
        scale=scale,
        unit=unit,
        decimals=decimals,
        writable=writable,
        archive=archive,
        display=display,
        group=group,
        description=description,
        alarm=alarm_def or AlarmDef(),
        bits=bits,
        commands=commands,
        write_min=write_min,
        write_max=write_max,
        gauge_min=gauge_min,
        gauge_max=gauge_max,
    )


def wave_point(
    name: str,
    label: str,
    address: int,
    count: int,
    unit: str,
    scale: float,
    interval_us: int,
    color: str,
    group: str = "波形",
) -> PointDef:
    """一个数组/块点位：一次读回整段波形，供示波器绘制。

    DSP 把一段波形放在连续的保持寄存器里，上位机成块读回来即可画真实波形 ——
    这是工业上唯一靠谱的做法（标量轮询画不出 50Hz 正弦，会严重混叠）。
    """
    return PointDef(
        name=name,
        label=label,
        register="holding",
        address=address,
        data_type="int16",
        scale=scale,
        unit=unit,
        decimals=1,
        kind="array",
        count=count,
        sample_interval_us=interval_us,
        record=True,
        scope=True,
        scope_color=color,
        display="wave",
        group=group,
        archive=False,  # 波形不按标量方式归档，走 waveforms 表
        description=f"{count} 点，采样间隔 {interval_us}us",
    )


def node(
    node_id: str,
    symbol: str,
    label: str,
    x: float,
    y: float,
    w: float = 0.14,
    h: float = 0.12,
    sub_label: str = "",
    action: str = "none",
    target: str = "",
    value_point: str = "",
    unit: str = "",
    decimals: int = 1,
    description: str = "",
) -> DiagramNode:
    """主回路图上的一个图元（坐标是 0~1 归一化）。

    ``symbol`` 取值见 :mod:`icomm.core.diagram_lib` 的图元库
    （grid / breaker / transformer / rectifier / inverter / dcdc / battery /
    solar / capacitor / bus / load / motor / meter / dsp / filter / …）。
    """
    return DiagramNode(
        id=node_id,
        symbol=symbol,
        label=label,
        sub_label=sub_label,
        x=x,
        y=y,
        w=w,
        h=h,
        action=action,
        target=target,
        value_point=value_point,
        unit=unit,
        decimals=decimals,
        description=description,
    )


def link(source: str, target: str, style: str = "ac", label: str = "") -> DiagramLink:
    """一条连线。``style``：ac 交流（黄）/ dc 直流（蓝）/ signal 信号（灰虚线）。"""
    return DiagramLink(source=source, target=target, style=style, label=label)


def diagram(title: str, nodes: list[DiagramNode], links: list[DiagramLink]) -> DiagramDef:
    return DiagramDef(title=title, nodes=nodes, links=links)
