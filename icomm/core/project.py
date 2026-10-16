"""工程文件读写与内置示例工程。"""

from __future__ import annotations

import json
import os

from . import samples
from .models import (
    AlarmDef,
    ChannelDef,
    DeviceDef,
    DiagramDef,
    DiagramHotspot,
    DiagramLink,
    DiagramNode,
    PointDef,
    Project,
    ProjectStorage,
    default_channel_options,
)

PROJECT_VERSION = 1

#: 默认工程目录
DEFAULT_DIR = "data"
DEFAULT_PROJECT_FILE = os.path.join(DEFAULT_DIR, "project.json")
DEFAULT_DB_FILE = os.path.join(DEFAULT_DIR, "icomm.db")


def save_project(project: Project, path: str = DEFAULT_PROJECT_FILE) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    payload = {"version": PROJECT_VERSION, **project.to_dict()}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def load_project(path: str = DEFAULT_PROJECT_FILE) -> Project:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return Project.from_dict(data)


def load_or_create_sample(path: str = DEFAULT_PROJECT_FILE) -> tuple[Project, bool]:
    """工程存在则载入，否则生成示例工程并落盘。返回 (工程, 是否新建)。"""
    if os.path.exists(path):
        try:
            return load_project(path), False
        except (OSError, ValueError, KeyError):
            pass
    project = sample_project()
    save_project(project, path)
    return project, True


def load_or_create(repo_root: str = ".") -> tuple[Project, bool]:
    return load_or_create_sample(os.path.join(repo_root, DEFAULT_PROJECT_FILE))


# --------------------------------------------------------------- 示例工程


def _alarm(level: str = "warning", **kwargs) -> AlarmDef:
    return AlarmDef(enabled=True, level=level, **kwargs)


def sample_project() -> Project:
    """内置示例：一条 Modbus TCP 通道挂两台从站，点位覆盖四类寄存器。

    与 ``tools/modbus_sim.py`` 仿真器的数据区一一对应，可无硬件直接联调。
    """
    project = Project(
        name="示例工程 - 配电柜监控",
        folder_name="cabinet-monitor",
        history_interval_s=5,
        history_retention_days=30,
    )

    project.channels.append(
        ChannelDef(
            name="配电柜网关",
            driver="modbus",
            link="tcp",
            description="示例 TCP 通道，指向本地仿真器 127.0.0.1:5020",
            options={
                **default_channel_options("tcp"),
                "host": "127.0.0.1",
                "port": 5020,
                "timeout": 1.0,
                "max_regs_per_read": 120,
                "max_gap": 8,
                "retry": 1,
            },
        )
    )

    meter = DeviceDef(
        name="1号进线柜",
        channel="配电柜网关",
        unit_id=1,
        poll_interval_ms=1000,
        description="多功能电力仪表（示例 unit=1）",
    )
    meter.points = [
        PointDef(
            name="变压器温度",
            register="holding",
            address=0,
            data_type="float32",
            unit="℃",
            decimals=1,
            label="变压器温度",
            display="gauge",
            gauge_min=0,
            gauge_max=100,
            group="温度",
            alarm=_alarm("critical", hi_hi=75, hi=60, lo=5),
            description="32 位浮点，占 2 个寄存器",
        ),
        PointDef(
            name="A相电压",
            register="holding",
            address=2,
            data_type="uint16",
            scale=0.1,
            unit="V",
            decimals=1,
            label="A相电压",
            display="gauge",
            gauge_min=0,
            gauge_max=300,
            group="电参量",
            alarm=_alarm(hi_hi=250, hi=240, lo=198, lo_lo=190),
        ),
        PointDef(
            name="A相电流",
            register="holding",
            address=3,
            data_type="uint16",
            scale=0.01,
            unit="A",
            decimals=2,
            label="A相电流",
            display="card",
            group="电参量",
            alarm=_alarm(hi=15),
        ),
        PointDef(
            name="电网频率",
            register="holding",
            address=4,
            data_type="uint16",
            scale=0.01,
            unit="Hz",
            decimals=2,
            label="电网频率",
            display="card",
            group="电参量",
            alarm=_alarm(hi_hi=50.5, hi=50.2, lo=49.8, lo_lo=49.5),
        ),
        PointDef(
            name="有功电能",
            register="holding",
            address=5,
            data_type="uint32",
            unit="kWh",
            decimals=1,
            label="有功电能",
            display="card",
            group="电参量",
            archive=False,
            description="32 位无符号整数，占 2 个寄存器",
        ),
        PointDef(
            name="功率因数",
            register="holding",
            address=7,
            data_type="uint16",
            scale=0.001,
            decimals=3,
            label="功率因数",
            display="card",
            group="电参量",
        ),
        PointDef(
            name="温度设定值",
            register="holding",
            address=8,
            data_type="uint16",
            scale=0.1,
            unit="℃",
            decimals=1,
            writable=True,
            label="温度设定值",
            display="card",
            group="控制",
            archive=False,
        ),
        PointDef(
            name="主开关",
            register="coil",
            address=0,
            data_type="bool",
            writable=True,
            label="主开关",
            display="lamp",
            group="开关量",
            archive=False,
        ),
        PointDef(
            name="运行指示",
            register="coil",
            address=1,
            data_type="bool",
            label="运行指示",
            display="lamp",
            group="开关量",
        ),
        PointDef(
            name="柜门状态",
            register="discrete",
            address=0,
            data_type="bool",
            label="柜门状态",
            display="lamp",
            group="开关量",
        ),
        PointDef(
            name="设备状态字",
            register="input",
            address=0,
            data_type="uint16",
            label="设备状态字",
            display="card",
            group="状态",
            archive=False,
        ),
    ]
    project.devices.append(meter)

    pump = DeviceDef(
        name="2号水泵",
        channel="配电柜网关",
        unit_id=2,
        poll_interval_ms=1000,
        description="同一通道下的第二台从站（示例 unit=2）",
    )
    pump.points = [
        PointDef(
            name="泵转速",
            register="holding",
            address=0,
            data_type="uint16",
            unit="rpm",
            decimals=0,
            label="泵转速",
            display="gauge",
            gauge_min=0,
            gauge_max=3000,
            group="水泵",
            alarm=_alarm(hi=2800),
        ),
        PointDef(
            name="泵运行",
            register="coil",
            address=0,
            data_type="bool",
            writable=True,
            label="泵运行",
            display="lamp",
            group="水泵",
            archive=False,
        ),
    ]
    project.devices.append(pump)

    project.diagram = cabinet_diagram()
    return project


# --------------------------------------------------------------- 配电柜示意图


def cabinet_diagram() -> DiagramDef:
    """配电柜监控示例的主回路示意图。

    10kV 进线 → 进线断路器 → 配电变压器 → 0.4kV 母线 → 进线柜（计量）/ 水泵馈线。
    带 action 的节点在界面上可点击，直接跳到对应设备详情页；
    叠了 value_point 的节点在主回路上直接显示实时值。
    """
    nodes = [
        DiagramNode(
            id="n_grid", symbol="grid", label="10kV 进线", sub_label="三相交流 · 市电",
            x=0.01, y=0.34, w=0.14, h=0.20,
        ),
        DiagramNode(
            id="n_qf", symbol="breaker", label="QF1", sub_label="进线断路器",
            x=0.18, y=0.39, w=0.10, h=0.13,
        ),
        DiagramNode(
            id="n_tr", symbol="transformer", label="T1 配电变压器", sub_label="10/0.4kV",
            x=0.31, y=0.33, w=0.15, h=0.19,
            value_point="1号进线柜.变压器温度", unit="℃", decimals=1,
            description="变压器温度由进线柜的多功能电力仪表上传（从站 1）",
        ),
        DiagramNode(
            id="n_bus", symbol="bus", label="0.4kV 母线", sub_label="单母线",
            x=0.50, y=0.34, w=0.14, h=0.18,
        ),
        DiagramNode(
            id="n_meter", symbol="meter", label="1号进线柜", sub_label="多功能电力仪表 · 从站1",
            x=0.68, y=0.16, w=0.15, h=0.18,
            action="device", target="1号进线柜",
            value_point="1号进线柜.A相电压", unit="V", decimals=1,
            description="进线柜：变压器温度、三相电压电流、频率、功率因数、有功电能",
        ),
        DiagramNode(
            id="n_pump_qs", symbol="switch", label="QS1", sub_label="水泵馈线开关",
            x=0.68, y=0.55, w=0.10, h=0.13,
        ),
        DiagramNode(
            id="n_pump", symbol="motor", label="2号水泵", sub_label="从站2",
            x=0.84, y=0.53, w=0.15, h=0.18,
            action="device", target="2号水泵",
            value_point="2号水泵.泵转速", unit="rpm", decimals=0,
            description="泵转速、泵运行（unit=2）",
        ),
        DiagramNode(
            id="n_note", symbol="text", label="柜门 / 运行指示", sub_label="开关量与状态字",
            x=0.68, y=0.79, w=0.20, h=0.13,
            description="主开关、运行指示、柜门状态、设备状态字（本示例只作示意）",
        ),
    ]
    links = [
        DiagramLink(source="n_grid", target="n_qf", style="ac", label="10kV"),
        DiagramLink(source="n_qf", target="n_tr", style="ac"),
        DiagramLink(source="n_tr", target="n_bus", style="ac", label="0.4kV"),
        DiagramLink(source="n_bus", target="n_meter", style="ac", label="进线"),
        DiagramLink(source="n_bus", target="n_pump_qs", style="ac", label="水泵馈线"),
        DiagramLink(source="n_pump_qs", target="n_pump", style="ac"),
        DiagramLink(source="n_meter", target="n_note", style="signal"),
    ]
    return DiagramDef(title="配电柜监控主回路", nodes=nodes, links=links)


# --------------------------------------------------------------- SST 示例工程


def sst_diagram() -> DiagramDef:
    """SST（固态变压器）主回路示意图。

    10kV 电网 → 进线断路器 → 级联整流 → 750V 直流母线 → DC/DC → 直流输出 → 计量。
    带 action 的节点在界面上可点击，直接跳到对应设备详情页。

    这里的电压等级（10kV / 750V）取的是**行业常见值**，阈值也只是示意，
    与任何真实装置无关 —— 照着自家图纸改就行。
    """
    nodes = [
        DiagramNode(
            id="grid", symbol="grid", label="10kV 电网", sub_label="三相交流",
            x=0.01, y=0.28, w=0.11, h=0.20,
        ),
        DiagramNode(
            id="qf", symbol="breaker", label="QF1", sub_label="进线断路器",
            x=0.15, y=0.33, w=0.07, h=0.11,
        ),
        DiagramNode(
            id="rect", symbol="rectifier", label="级联整流", sub_label="H桥级联 · DSP 控制",
            x=0.25, y=0.24, w=0.18, h=0.28,
            action="device", target="级联整流DSP",
            description="Modbus 从站 11，现场为 RS485/Modbus RTU，本示例走 TCP 便于无硬件演示",
        ),
        DiagramNode(
            id="dcbus", symbol="capacitor", label="直流母线", sub_label="",
            x=0.47, y=0.32, w=0.09, h=0.13,
            value_point="级联整流DSP.直流母线电压", unit="V", decimals=0,
        ),
        DiagramNode(
            id="dcdc", symbol="dcdc", label="DC/DC", sub_label="隔离变换 · DSP 控制",
            x=0.60, y=0.24, w=0.18, h=0.28,
            action="device", target="DCDC DSP",
            description="Modbus 从站 12，输出 750V 直流",
        ),
        DiagramNode(
            id="out", symbol="bus", label="750V 直流输出", sub_label="",
            x=0.82, y=0.30, w=0.17, h=0.15,
            value_point="DCDC DSP.输出电压", unit="V", decimals=1,
        ),
        DiagramNode(
            id="meter", symbol="meter", label="计量单元", sub_label="Modbus 从站 13",
            x=0.82, y=0.54, w=0.17, h=0.13,
            action="device", target="计量单元",
        ),
        DiagramNode(
            id="load", symbol="load", label="直流负载 / 充电桩", sub_label="",
            x=0.82, y=0.76, w=0.17, h=0.13,
        ),
    ]
    links = [
        DiagramLink(source="grid", target="qf", style="ac"),
        DiagramLink(source="qf", target="rect", style="ac", label="10kV"),
        DiagramLink(source="rect", target="dcbus", style="dc"),
        DiagramLink(source="dcbus", target="dcdc", style="dc"),
        DiagramLink(source="dcdc", target="out", style="dc", label="750V"),
        DiagramLink(source="out", target="meter", style="signal"),
        DiagramLink(source="out", target="load", style="dc"),
    ]
    return DiagramDef(title="SST 固态变压器主回路", nodes=nodes, links=links)


def _wave_point(name: str, label: str, address: int, count: int, unit: str, scale: float,
                interval_us: int, color: str, group: str = "波形") -> PointDef:
    """一个数组/块点位：一次读回整段波形，供示波器绘制。"""
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
        archive=False,          # 波形不按标量方式归档，走 waveforms 表
        description=f"{count} 点，采样间隔 {interval_us}us",
    )


def sst_project() -> Project:
    """SST 固态变压器示例工程。

    结构：10kV 电网 → 级联整流（DSP, 从站11）→ 750V 直流母线 → DC/DC（DSP, 从站12）
          → 直流输出 → 计量单元（从站13）

    现场整流与 DCDC 都是 Modbus RTU 的 DSP 板卡；本示例为便于无硬件演示，
    通道走 TCP 指向内置仿真器（tools/modbus_sim.py 的 unit 11/12/13）。
    上现场时只需把通道的链路类型改成 rtu、串口号改成实际 COM 口即可。

    点数与量程都是**示意值**（10kV / 750V 取的是行业常见等级），照着自家图纸改。
    """
    project = Project(
        name="SST 固态变压器",
        folder_name="sst-transformer",
        history_interval_s=5,
        history_retention_days=30,
        storage=ProjectStorage(),
    )

    project.channels.append(
        ChannelDef(
            name="SST控制链",
            driver="modbus",
            link="tcp",
            description="示例走 TCP 指向内置仿真器；现场改为 rtu + 实际串口（如 COM3, 9600, 8N1）",
            options={
                **default_channel_options("tcp"),
                "host": "127.0.0.1",
                "port": 5020,
                "timeout": 1.0,
                "max_regs_per_read": 120,
                "max_gap": 8,
                "retry": 1,
            },
        )
    )

    # ------------------------------------------------ 级联整流 DSP（从站 11）
    rectifier = DeviceDef(
        name="级联整流DSP",
        channel="SST控制链",
        unit_id=11,
        poll_interval_ms=1000,
        description="级联 H 桥整流，DSP 控制板（现场为 RS485 Modbus RTU）",
    )
    rectifier.points = [
        PointDef(
            name="电网线电压", label="电网线电压", register="holding", address=0,
            data_type="uint16", scale=0.1, unit="kV", decimals=2,
            group="整流", display="gauge", gauge_min=0, gauge_max=12,
            alarm=_alarm("critical", hi_hi=11.0, hi=10.5, lo=9.5, lo_lo=9.0),
        ),
        PointDef(
            name="直流母线电压", label="直流母线电压", register="holding", address=1,
            data_type="uint16", unit="V", decimals=0,
            group="整流", display="card",
            alarm=_alarm(hi_hi=800, hi=780, lo=700, lo_lo=660),
        ),
        PointDef(
            name="整流功率", label="整流功率", register="holding", address=2,
            data_type="uint16", scale=0.1, unit="kW", decimals=1,
            group="整流", display="card",
        ),
        PointDef(
            name="电网电流", label="电网电流", register="holding", address=4,
            data_type="uint16", scale=0.1, unit="A", decimals=1,
            group="整流", display="card",
        ),
        PointDef(
            name="功率因数", label="功率因数", register="holding", address=5,
            data_type="uint16", scale=0.001, decimals=3,
            group="整流", display="card",
        ),
        PointDef(
            name="模块温度", label="模块温度", register="holding", address=3,
            data_type="uint16", scale=0.1, unit="℃", decimals=1,
            group="整流", display="gauge", gauge_min=0, gauge_max=100,
            alarm=_alarm("critical", hi_hi=80, hi=65),
        ),
        PointDef(
            name="整流运行", label="整流运行", register="coil", address=0,
            data_type="bool", writable=True, display="lamp",
            group="整流", archive=False,
        ),
        PointDef(
            name="整流状态字", label="整流状态字", register="input", address=0,
            data_type="uint16", display="card", group="整流", archive=False,
        ),
        _wave_point("A相电压波形", "A相电压波形", 10, 200, "V", 0.5, 100, "#3d8bfd"),
        _wave_point("B相电压波形", "B相电压波形", 210, 200, "V", 0.5, 100, "#2f9e6b"),
        _wave_point("C相电压波形", "C相电压波形", 410, 200, "V", 0.5, 100, "#c9a227"),
    ]
    project.devices.append(rectifier)

    # ------------------------------------------------ DC/DC DSP（从站 12）
    dcdc = DeviceDef(
        name="DCDC DSP",
        channel="SST控制链",
        unit_id=12,
        poll_interval_ms=1000,
        description="隔离型 DC/DC，DSP 控制板（现场为 RS485 Modbus RTU）",
    )
    dcdc.points = [
        PointDef(
            name="输入电压", label="输入电压", register="holding", address=0,
            data_type="uint16", scale=0.1, unit="V", decimals=1,
            group="DC/DC", display="card",
        ),
        PointDef(
            name="输出电压", label="输出电压", register="holding", address=1,
            data_type="uint16", scale=0.1, unit="V", decimals=1,
            group="DC/DC", display="gauge", gauge_min=650, gauge_max=850,
            alarm=_alarm("critical", hi_hi=800, hi=780, lo=700, lo_lo=660),
        ),
        PointDef(
            name="输出电流", label="输出电流", register="holding", address=2,
            data_type="uint16", scale=0.01, unit="A", decimals=2,
            group="DC/DC", display="card",
        ),
        PointDef(
            name="开关频率", label="开关频率", register="holding", address=3,
            data_type="uint16", scale=0.1, unit="kHz", decimals=1,
            group="DC/DC", display="card",
        ),
        PointDef(
            name="变压器温度", label="变压器温度", register="holding", address=4,
            data_type="uint16", scale=0.1, unit="℃", decimals=1,
            group="DC/DC", display="gauge", gauge_min=0, gauge_max=120,
            alarm=_alarm("critical", hi_hi=95, hi=80),
        ),
        PointDef(
            name="DCDC使能", label="DCDC 使能", register="coil", address=0,
            data_type="bool", writable=True, display="lamp",
            group="DC/DC", archive=False,
        ),
        _wave_point("电感电流波形", "电感电流波形", 10, 128, "A", 0.01, 20, "#a86ede"),
    ]
    project.devices.append(dcdc)

    # ------------------------------------------------ 计量单元（从站 13）
    meter = DeviceDef(
        name="计量单元",
        channel="SST控制链",
        unit_id=13,
        poll_interval_ms=2000,
        description="输出侧计量单元",
    )
    meter.points = [
        PointDef(
            name="输出有功功率", label="输出有功功率", register="holding", address=0,
            data_type="uint16", scale=0.1, unit="kW", decimals=1,
            group="计量", display="card",
        ),
        PointDef(
            name="整机效率", label="整机效率", register="holding", address=1,
            data_type="uint16", scale=0.01, unit="%", decimals=2,
            group="计量", display="gauge", gauge_min=80, gauge_max=100,
            alarm=_alarm("warning", lo=94.0),
        ),
        PointDef(
            name="累计输出电量", label="累计输出电量", register="holding", address=2,
            data_type="uint32", unit="kWh", decimals=1,
            group="计量", display="card", archive=False,
        ),
    ]
    project.devices.append(meter)

    project.diagram = sst_diagram()
    return project


#: 内置示例工程（键 = 界面「新建 / 示例」菜单里的名字，顺序 = 菜单顺序）
#:
#: 储能 / 光伏 / 电力系统在 :mod:`icomm.core.samples` 里，一个示例一个模块
#: （点表与仿真行为成对维护，加示例不用改这个文件）；
#: SST 与配电柜因为历史原因还留在本文件里，以后也可以慢慢挪过去。
BUILTIN_SAMPLES = {
    **samples.REGISTRY,
    "SST 固态变压器": sst_project,
    "配电柜监控": sample_project,
}

#: 首启默认载入哪个示例（留空则用第一个）
DEFAULT_SAMPLE = next(iter(BUILTIN_SAMPLES))


# --------------------------------------------------------------- 按名取示例
#
# 测试与探针要一个"点位齐全"的工程当夹具（控制字命令、状态字位域、可写给定、
# 波形数组、周期报警都有），这里给每个示例留一个直白的入口，省得到处写中文键名。


def bess_project() -> Project:
    """内置储能示例（BESS）：测试夹具首选，控制/位域/给定/波形/报警都齐全。"""
    return samples.bess.build()


def pv_project() -> Project:
    """内置光伏示例（PV）：三台从站 + 四段波形 + 周期性报警。"""
    return samples.pv.build()


def grid_project() -> Project:
    """内置电力系统示例（配电网）：三台从站 + 遥控/定值 + 位域状态字。"""
    return samples.grid.build()


def sample_diagram_for(project: Project) -> DiagramDef:
    """按工程名找一份内置示例主回路图（主回路图编辑器的「载入示例」用）。

    先按工程名精确匹配内置示例；匹配不上就按设备名的关键词猜一个
    （用户把自己的工程改名后，设备名往往还留着）；
    都没有就回落到 :data:`DEFAULT_SAMPLE` —— 「载入示例图」要给的是一张能改的
    起点图，空白工程点了也不该什么都不给。
    """
    builder = BUILTIN_SAMPLES.get(project.name)
    if builder is not None:
        diagram = builder().diagram
        if diagram is not None and diagram.nodes:
            return diagram

    names = " ".join([project.name, *(device.name for device in project.devices)])
    for keyword, sample_name in (
        ("储能", "储能电站（BESS）"),
        ("电池", "储能电站（BESS）"),
        ("PCS", "储能电站（BESS）"),
        ("光伏", "光伏电站（PV）"),
        ("逆变", "光伏电站（PV）"),
        ("组串", "光伏电站（PV）"),
        ("SST", "SST 固态变压器"),
        ("固态变压", "SST 固态变压器"),
        ("馈线", "电力系统（配电网）"),
        ("配变", "电力系统（配电网）"),
        ("母线", "电力系统（配电网）"),
        ("配电柜", "配电柜监控"),
    ):
        if keyword.lower() in names.lower():
            fallback = BUILTIN_SAMPLES.get(sample_name)
            if fallback is not None:
                diagram = fallback().diagram
                if diagram is not None and diagram.nodes:
                    return diagram
    return BUILTIN_SAMPLES[DEFAULT_SAMPLE]().diagram


def blank_project(name: str = "新建工程", folder_name: str = "") -> Project:
    """空白工程：一条 TCP 通道 + 一台设备 + 一个点位，便于从零开始。"""
    project = Project(name=name, folder_name=folder_name)
    project.channels.append(
        ChannelDef(
            name="通道1",
            driver="modbus",
            link="tcp",
            options={**default_channel_options("tcp"), "host": "127.0.0.1", "port": 502},
        )
    )
    project.devices.append(
        DeviceDef(
            name="设备1",
            channel="通道1",
            unit_id=1,
            points=[PointDef(name="变量1", label="变量1", address=0)],
        )
    )
    return project
