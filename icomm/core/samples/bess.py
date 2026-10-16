"""内置示例工程：储能电站（BESS）。

面向一台**集装箱式储能电站**的典型 Modbus 接口：储能变流器（PCS）+ 电池簇 BMS，
交流侧经升压变压器并入 10kV 电网，直流侧接若干电池簇，另带消防/温控信号。

**这不是任何厂商的真实点表**：只按教科书上通用的量测范围与工程习惯编了一套接口，
地址、scale、告警门限都是"圆整"的通用值，方便对照学习与二次修改。
仿真器（``tools/modbus_sim.py``）按本模块的 :func:`simulate` 刷新 unit 31 的寄存器，
所以**点表与仿真必须成对维护**：地址与 scale 统一放在模块级 ``_REG`` 表里，
``build()`` 与 ``simulate()`` 都从那里取，只写一份，不会两边对不上。

通信约定
--------
* 一条 Modbus TCP 通道指向内置仿真器 ``127.0.0.1:5020``，从站号 ``UNIT = 31``，
  采集周期 ``POLL_INTERVAL_MS = 200``（波形点要成块读，轮询太快没意义）。
* PCS 与 BMS 是同一个从站里的两段寄存器区，做成两台"设备"，
  界面上分成两个详情页，便于分开监视。
* 标量全用保持寄存器（holding），只有"并网开关"用线圈（coil 0）。
* 32 位量（日/累计电量）占两个连续寄存器，高字在前（``word_order="big"``）。
* 波形点是一次成块读回的数组点位：200 点 × 100us = 20ms = 一个工频周期。

寄存器映射（保持寄存器 holding，单位见"说明"）
----------------------------------------------
储能变流器 PCS（0~26 + 波形区）::

    地址  点位名           类型      scale  说明
    0     直流母线电压     uint16    0.1    V，1100V 直流母线
    1     直流电流         int16     0.1    A，+放电 / −充电
    2     直流功率         int16     0.1    kW，+放电 / −充电
    3     直流母线纹波     uint16    0.01   V
    4~6   A/B/C 相电压     uint16    0.1    V，相电压 231V
    7~9   A/B/C 相电流     uint16    0.01   A
    10    有功功率         int16     0.1    kW，+放电（上网）/ −充电
    11    无功功率         int16     0.1    kVar
    12    功率因数         int16     0.001  四象限，充电为负
    13    电网频率         uint16    0.01   Hz
    14    运行状态字       uint16    —      按位置位（只置一位）：bit0 停机 / bit1 待机 / bit2 充电 / bit3 放电 / bit4 故障
    15    故障字           uint16    —      位定义，见模块常量 _FAULT_BITS
    16    告警字           uint16    —      位定义，见模块常量 _WARN_BITS
    17    控制字           uint16    —      命令码，可写：0x01 开机/0x02 关机/0x04 充电/0x08 放电/0x10 复位
    18    有功功率给定     int16     0.1    kW，可写 −500~+500，0 = 自动
    19    无功功率给定     int16     0.1    kVar，可写 −300~+300
    20    功能配置字       uint16    —      位定义，可写：0 低电压穿越/1 无功调节/2 防孤岛/3 限功率/4 故障录波/5 测试模式/6 定值远方/7 黑启动
    21    功率模块温度     int16     0.1    ℃（仿真里周期性越过 70℃ 告警门限）
    22    转换效率         uint16    0.01   %
    23-24 日充电量         uint32    0.1    kWh
    25-26 日放电量         uint32    0.1    kWh
    200~399  电池电流波形     int16×200  0.1  A，100us/点
    400~599  并网 A 相电流波形 int16×200  0.1  A，100us/点
    600~799  并网 B 相电流波形 int16×200  0.1  A，100us/点
    800~999  并网 C 相电流波形 int16×200  0.1  A，100us/点
    coil 0   并网开关          bool       —      可写，分闸后 PCS 进停机

电池管理系统 BMS（100~123）::

    地址  点位名           类型      scale  说明
    100   电池簇电压       uint16    0.1    V
    101   电池簇电流       int16     0.1    A，+放电 / −充电
    102   SOC              uint16    0.1    %
    103   SOH              uint16    0.1    %
    104   最高单体电压     uint16    0.001  V
    105   最低单体电压     uint16    0.001  V
    106   单体压差         uint16    0.001  V
    107   最高单体温度     int16     0.1    ℃
    108   最低单体温度     int16     0.1    ℃
    109   电池舱温度       int16     0.1    ℃（仿真里周期性越限，演示报警）
    110   舱内湿度         uint16    0.1    %
    111   绝缘电阻         uint16    1      kΩ（仿真里周期性低于 150kΩ 告警）
    112   电池状态字       uint16    —      位定义
    113   SOC 上限设定     uint16    0.1    %，可写 50~100
    114   SOC 下限设定     uint16    0.1    %，可写 0~80
    115   环境温度         int16     0.1    ℃
    116-117 累计充电量       uint32    0.1    kWh
    118-119 累计放电量       uint32    0.1    kWh
    120   循环次数         uint16    1      次
    122   允许充电电流     uint16    0.1    A
    123   允许放电电流     uint16    0.1    A

仿真的取舍
----------
* 默认（控制字 0、给定 0）走**自动充放曲线**：±320kW 正弦，SOC 与之同相缓慢摆动；
  写了控制字或给定值之后按命令走：开机→待机（0kW）/充电→取负值/放电→取正值。
* 温度、绝缘电阻用 ``sin`` 缓慢漂移，保证演示时**必然能看到越限报警**；
  告警字/故障字由这些越限位实时拼出来，不是假数据。
* ``t`` 用的是"仿真运行时间"，所以日/累计电量按 ``t`` 线性累加（只在演示里有意义）。
* SOC 上下限设定：0 视为"未初始化"，仿真会补成默认 20%/90%（这样没人写值时画面也正常）；
  同时仿真的 SOC 会被夹在设定区间内，改设定立刻能看到效果。
"""

from __future__ import annotations

import math
from typing import Any

from ..models import (
    ChannelDef,
    DeviceDef,
    Project,
    ProjectStorage,
    default_channel_options,
)
from ._common import alarm, diagram, link, node, scalar, wave_point
from ._sim import put_i16, put_u16, put_u32, store_registers

__all__ = [
    "FOLDER_NAME",
    "NAME",
    "POLL_INTERVAL_MS",
    "UNIT",
    "UNITS",
    "build",
    "simulate",
]

# --------------------------------------------------------------- 模块接口

#: 界面上显示的工程名
NAME = "储能电站（BESS）"
#: 磁盘文件夹名（必须英文 ASCII）
FOLDER_NAME = "bess-storage"
#: 本示例用到的 Modbus 从站号
UNIT = 31
#: ``samples`` 包按这个元组给仿真器建数据区（没有 ``UNITS`` 时才回落到 ``UNIT``）
UNITS = (UNIT,)
#: 采集周期（毫秒）
POLL_INTERVAL_MS = 200

CHANNEL_NAME = "储能EMS链"
PCS_DEVICE = "储能变流器PCS"
BMS_DEVICE = "电池管理系统BMS"

# --------------------------------------------------------------- 状态字/控制字

#: 运行状态字：**一位一个状态**（同一时刻只置一位，与界面按位解码的机制一致）
_STATE_BITS = "0:停机,1:待机,2:充电,3:放电,4:故障,5:维护"

#: 告警字（警告级：越"告警门限"置位）
_WARN_BITS = (
    "0:交流过压,1:交流欠压,2:交流过流,3:频率异常,4:直流过压,5:直流欠压,"
    "6:模块过温,7:电池舱过温,8:SOC偏低,9:SOC偏高,10:绝缘偏低,11:单体压差大,"
    "12:SOH偏低,13:通信异常,14:空调故障,15:BMS告警"
)

#: 故障字（严重级：越"故障门限"置位）
_FAULT_BITS = (
    "0:交流过流跳闸,1:直流过压跳闸,2:功率模块过温,3:电池保护,4:绝缘故障,"
    "5:急停,6:硬件故障,7:通信中断,8:母线故障,9:风扇故障"
)

#: 控制字：写命令码，不是位组合（界面会画成一排按钮）
_CTRL_COMMANDS = "0x01:开机,0x02:关机,0x04:充电,0x08:放电,0x10:复位"

#: 功能配置字：位掩码，各位独立开关（可写）
_FUNC_BITS = (
    "0:使能低电压穿越,1:使能无功调节,2:使能防孤岛,3:使能限功率,"
    "4:使能故障录波,5:使能测试模式,6:定值远方,7:使能黑启动"
)

#: 电池状态字
_BATTERY_BITS = "0:正常,1:待机,2:充电,3:放电,4:告警,5:保护,6:故障,7:绝缘低,8:高温"

_CTRL_START = 0x01
_CTRL_STOP = 0x02
_CTRL_CHARGE = 0x04
_CTRL_DISCHARGE = 0x08
_CTRL_RESET = 0x10

STATE_STOP = 0
STATE_READY = 1
STATE_CHARGE = 2
STATE_DISCHARGE = 3

#: 自动充放曲线的周期（秒）与峰值（kW）
AUTO_CYCLE_S = 300.0
AUTO_PEAK_KW = 320.0
#: 手动充/放模式下 SOC 走完一个量程的时间（秒）
SOC_RAMP_S = 600.0
#: 电池簇额定电压与并网点线电压
CLUSTER_RATED_V = 1060.0
GRID_LINE_V = 400.0
GRID_PHASE_V = 231.0

#: 波形点：点数与采样间隔（200 点 × 100us = 20ms = 一个工频周期）
WAVE_COUNT = 200
WAVE_SAMPLE_US = 100
WAVE_SAMPLES_PER_CYCLE = 200

#: 并网开关用的线圈地址
COIL_GRID_SWITCH = 0

# --------------------------------------------------------------- 地址与 scale 表
#
# ``键 -> (地址, scale, 编码)``；编码 ``u16/i16/u32`` 是仿真写回时用的编码，
# ``wave`` 是波形数组，``rw`` 表示只读回来判断状态、仿真不写
# （控制字、两个给定与功能配置字）。

_REG: dict[str, tuple[int, float, str]] = {
    # ---- PCS：直流侧
    "pcs_dc_bus_voltage": (0, 0.1, "u16"),
    "pcs_dc_current": (1, 0.1, "i16"),
    "pcs_dc_power": (2, 0.1, "i16"),
    "pcs_dc_ripple": (3, 0.01, "u16"),
    # ---- PCS：交流侧
    "pcs_voltage_a": (4, 0.1, "u16"),
    "pcs_voltage_b": (5, 0.1, "u16"),
    "pcs_voltage_c": (6, 0.1, "u16"),
    "pcs_current_a": (7, 0.01, "u16"),
    "pcs_current_b": (8, 0.01, "u16"),
    "pcs_current_c": (9, 0.01, "u16"),
    "pcs_active_power": (10, 0.1, "i16"),
    "pcs_reactive_power": (11, 0.1, "i16"),
    "pcs_power_factor": (12, 0.001, "i16"),
    "pcs_frequency": (13, 0.01, "u16"),
    # ---- PCS：状态与控制
    "pcs_state_word": (14, 1.0, "u16"),
    "pcs_fault_word": (15, 1.0, "u16"),
    "pcs_alarm_word": (16, 1.0, "u16"),
    "pcs_control_word": (17, 1.0, "rw"),
    "pcs_active_setpoint": (18, 0.1, "rw"),
    "pcs_reactive_setpoint": (19, 0.1, "rw"),
    "pcs_function_config": (20, 1.0, "rw"),
    # ---- PCS：温度与统计
    "pcs_module_temperature": (21, 0.1, "i16"),
    "pcs_efficiency": (22, 0.01, "u16"),
    "pcs_daily_charge": (23, 0.1, "u32"),      # 23-24
    "pcs_daily_discharge": (25, 0.1, "u32"),   # 25-26
    # ---- BMS：电池
    "bms_cluster_voltage": (100, 0.1, "u16"),
    "bms_cluster_current": (101, 0.1, "i16"),
    "bms_soc": (102, 0.1, "u16"),
    "bms_soh": (103, 0.1, "u16"),
    "bms_cell_voltage_max": (104, 0.001, "u16"),
    "bms_cell_voltage_min": (105, 0.001, "u16"),
    "bms_cell_voltage_delta": (106, 0.001, "u16"),
    "bms_cell_temp_max": (107, 0.1, "i16"),
    "bms_cell_temp_min": (108, 0.1, "i16"),
    "bms_cabin_temperature": (109, 0.1, "i16"),
    "bms_cabin_humidity": (110, 0.1, "u16"),
    "bms_insulation": (111, 1.0, "u16"),
    "bms_state_word": (112, 1.0, "u16"),
    "bms_soc_high_limit": (113, 0.1, "rw"),
    "bms_soc_low_limit": (114, 0.1, "rw"),
    "bms_ambient_temperature": (115, 0.1, "i16"),
    "bms_allow_charge_current": (122, 0.1, "u16"),
    "bms_allow_discharge_current": (123, 0.1, "u16"),
    # ---- BMS：统计
    "bms_total_charge": (116, 0.1, "u32"),      # 116-117
    "bms_total_discharge": (118, 0.1, "u32"),   # 118-119
    "bms_cycle_count": (120, 1.0, "u16"),
    # ---- 波形数组（各占 200 个寄存器）
    "wave_battery_current": (200, 0.1, "wave"),
    "wave_grid_current_a": (400, 0.1, "wave"),
    "wave_grid_current_b": (600, 0.1, "wave"),
    "wave_grid_current_c": (800, 0.1, "wave"),
}

#: 默认 SOC 上下限设定（%）—— 没人写过值时用
DEFAULT_SOC_HIGH = 90.0
DEFAULT_SOC_LOW = 20.0


def _p(key: str, name: str, unit: str = "", label: str = "", **kwargs):
    """按 ``_REG`` 取 ``(地址, scale)`` 建一个标量点位。"""
    address, scale, _kind = _REG[key]
    return scalar(name, label or name, address, unit=unit, scale=scale, **kwargs)


def _w(key: str, name: str, unit: str, color: str, label: str = ""):
    """按 ``_REG`` 取地址/scale 建一个波形数组点位。"""
    address, scale, _kind = _REG[key]
    return wave_point(
        name,
        label or name,
        address,
        WAVE_COUNT,
        unit,
        scale,
        WAVE_SAMPLE_US,
        color,
        group="波形",
    )


# --------------------------------------------------------------- 点表


def pcs_points() -> list:
    """储能变流器 PCS 的点位（直流侧 / 交流侧 / 状态与控制 / 温度 / 统计 / 波形）。"""
    return [
        # ------------------------------------------------ 直流侧
        _p(
            "pcs_dc_bus_voltage", "直流母线电压", unit="V", decimals=1,
            group="直流侧", display="gauge", gauge_min=0, gauge_max=1300,
            alarm_def=alarm("critical", hi_hi=1250, hi=1200, lo=900, lo_lo=850),
            description="1100V 直流母线，PCS 与电池簇之间的公共直流环节",
        ),
        _p(
            "pcs_dc_current", "直流电流", unit="A", decimals=1, group="直流侧",
            data_type="int16",
            description="正值放电、负值充电",
        ),
        _p(
            "pcs_dc_power", "直流功率", unit="kW", decimals=1, group="直流侧",
            data_type="int16",
            description="电池侧功率：正值放电、负值充电",
        ),
        _p(
            "pcs_dc_ripple", "直流母线纹波", unit="V", decimals=2, group="直流侧",
            alarm_def=alarm("warning", hi=5, hi_hi=8),
            description="母线电压交流分量峰峰值",
        ),
        # ------------------------------------------------ 交流侧
        _p(
            "pcs_voltage_a", "A相电压", unit="V", decimals=1, group="交流侧",
            alarm_def=alarm("critical", hi_hi=253, hi=245, lo=207, lo_lo=200),
        ),
        _p(
            "pcs_voltage_b", "B相电压", unit="V", decimals=1, group="交流侧",
            alarm_def=alarm("critical", hi_hi=253, hi=245, lo=207, lo_lo=200),
        ),
        _p(
            "pcs_voltage_c", "C相电压", unit="V", decimals=1, group="交流侧",
            alarm_def=alarm("critical", hi_hi=253, hi=245, lo=207, lo_lo=200),
        ),
        _p(
            "pcs_current_a", "A相电流", unit="A", decimals=2, group="交流侧",
            alarm_def=alarm("critical", hi_hi=700, hi=600),
        ),
        _p(
            "pcs_current_b", "B相电流", unit="A", decimals=2, group="交流侧",
            alarm_def=alarm("critical", hi_hi=700, hi=600),
        ),
        _p(
            "pcs_current_c", "C相电流", unit="A", decimals=2, group="交流侧",
            alarm_def=alarm("critical", hi_hi=700, hi=600),
        ),
        _p(
            "pcs_active_power", "有功功率", unit="kW", decimals=1, group="交流侧",
            data_type="int16",
            description="并网点有功：正值放电上网、负值充电",
        ),
        _p(
            "pcs_reactive_power", "无功功率", unit="kVar", decimals=1, group="交流侧",
            data_type="int16",
        ),
        _p(
            "pcs_power_factor", "功率因数", decimals=3, group="交流侧",
            data_type="int16",
            alarm_def=alarm("warning", lo=0.90, lo_lo=0.85),
            description="四象限测量，充电时为负",
        ),
        _p(
            "pcs_frequency", "电网频率", unit="Hz", decimals=2, group="交流侧",
            display="gauge", gauge_min=45, gauge_max=55,
            alarm_def=alarm("critical", hi_hi=50.5, hi=50.2, lo=49.8, lo_lo=49.5),
        ),
        # ------------------------------------------------ 状态与控制
        _p(
            "pcs_state_word", "运行状态字", group="状态与控制", display="card",
            bits=_STATE_BITS, archive=False,
            description="按位置位（同一时刻只置一位）：bit0 停机 / bit1 待机 / bit2 充电 / bit3 放电 / bit4 故障",
        ),
        _p(
            "pcs_fault_word", "故障字", group="状态与控制", display="card",
            bits=_FAULT_BITS, archive=False,
            description="严重级保护动作（跳闸类）",
        ),
        _p(
            "pcs_alarm_word", "告警字", group="状态与控制", display="card",
            bits=_WARN_BITS, archive=False,
            description="警告级越限，暂时不影响运行",
        ),
        _p(
            "pcs_control_word", "控制字", group="状态与控制", display="card",
            writable=True, commands=_CTRL_COMMANDS, archive=False,
            description="写命令码：0x01 开机、0x02 关机、0x04 充电、0x08 放电、0x10 复位",
        ),
        _p(
            "pcs_active_setpoint", "有功功率给定", unit="kW", decimals=1,
            data_type="int16", group="状态与控制", writable=True,
            write_min=-500.0, write_max=500.0, archive=False,
            description="0 = 按内置曲线自动充放；写值后按给定值运行（负值=充电）",
        ),
        _p(
            "pcs_reactive_setpoint", "无功功率给定", unit="kVar", decimals=1,
            data_type="int16", group="状态与控制", writable=True,
            write_min=-300.0, write_max=300.0, archive=False,
        ),
        _p(
            "pcs_function_config", "功能配置字", decimals=0, group="状态与控制",
            writable=True, bits=_FUNC_BITS, archive=False,
            description="按位下发：0 低电压穿越 / 1 无功调节 / 2 防孤岛 / 3 限功率 / "
                        "4 故障录波 / 5 测试模式 / 6 定值远方 / 7 黑启动",
        ),
        scalar(
            "并网开关", "并网开关", COIL_GRID_SWITCH, register="coil", data_type="bool",
            writable=True, display="lamp", group="状态与控制", archive=False,
            description="分闸后 PCS 进入停机；写 1 合闸",
        ),
        # ------------------------------------------------ 温度
        _p(
            "pcs_module_temperature", "功率模块温度", unit="℃", decimals=1,
            data_type="int16", group="温度", display="gauge", gauge_min=0, gauge_max=120,
            alarm_def=alarm("critical", hi=70, hi_hi=80),
            description="仿真中周期性越过 70℃ 告警门限，用来看报警链路",
        ),
        # ------------------------------------------------ 统计
        _p(
            "pcs_efficiency", "转换效率", unit="%", decimals=2, group="统计",
            display="gauge", gauge_min=90, gauge_max=100,
            alarm_def=alarm("warning", lo=95),
            description="交流侧输出 / 直流侧输入",
        ),
        _p(
            "pcs_daily_charge", "日充电量", unit="kWh", decimals=1, group="统计",
            data_type="uint32",
        ),
        _p(
            "pcs_daily_discharge", "日放电量", unit="kWh", decimals=1, group="统计",
            data_type="uint32",
        ),
        # ------------------------------------------------ 波形
        _w("wave_battery_current", "电池电流波形", "A", "#a86ede"),
        _w("wave_grid_current_a", "并网A相电流波形", "A", "#3d8bfd"),
        _w("wave_grid_current_b", "并网B相电流波形", "A", "#2f9e6b"),
        _w("wave_grid_current_c", "并网C相电流波形", "A", "#c9a227"),
    ]


def bms_points() -> list:
    """电池管理系统 BMS 的点位（电池 / 状态与控制 / 温度 / 统计）。"""
    return [
        _p(
            "bms_cluster_voltage", "电池簇电压", unit="V", decimals=1, group="电池",
            display="gauge", gauge_min=800, gauge_max=1300,
            alarm_def=alarm("critical", hi=1250, lo=950, lo_lo=900),
        ),
        _p(
            "bms_cluster_current", "电池簇电流", unit="A", decimals=1, group="电池",
            data_type="int16", description="正值放电、负值充电",
        ),
        _p(
            "bms_soc", "SOC", unit="%", decimals=1, group="电池",
            display="gauge", gauge_min=0, gauge_max=100,
            alarm_def=alarm("critical", hi_hi=95, hi=90, lo=20, lo_lo=10),
            description="荷电状态；仿真中被夹在 SOC 上限/下限设定之间",
        ),
        _p(
            "bms_soh", "SOH", unit="%", decimals=1, group="电池",
            display="gauge", gauge_min=0, gauge_max=100,
            alarm_def=alarm("warning", lo=85),
            description="健康度",
        ),
        _p(
            "bms_cell_voltage_max", "最高单体电压", unit="V", decimals=3, group="电池",
            alarm_def=alarm("critical", hi_hi=3.65, hi=3.60, lo=2.90, lo_lo=2.80),
            description="LFP 单体，量程 2.8~3.65V",
        ),
        _p(
            "bms_cell_voltage_min", "最低单体电压", unit="V", decimals=3, group="电池",
            alarm_def=alarm("critical", lo=2.90, lo_lo=2.80),
        ),
        _p(
            "bms_cell_voltage_delta", "单体压差", unit="V", decimals=3, group="电池",
            alarm_def=alarm("warning", hi=0.10, hi_hi=0.20),
            description="最高与最低单体电压之差，看均衡效果",
        ),
        _p(
            "bms_cell_temp_max", "最高单体温度", unit="℃", decimals=1, group="电池",
            data_type="int16", alarm_def=alarm("critical", hi=45, hi_hi=55),
        ),
        _p(
            "bms_cell_temp_min", "最低单体温度", unit="℃", decimals=1, group="电池",
            data_type="int16",
        ),
        _p(
            "bms_cabin_temperature", "电池舱温度", unit="℃", decimals=1, group="电池",
            data_type="int16", display="gauge", gauge_min=0, gauge_max=80,
            alarm_def=alarm("critical", hi=45, hi_hi=50),
            description="仿真中周期性超过 45/50℃，演示「先告警后故障」",
        ),
        _p(
            "bms_cabin_humidity", "舱内湿度", unit="%", decimals=1, group="电池",
            alarm_def=alarm("warning", hi=80),
        ),
        _p(
            "bms_insulation", "绝缘电阻", unit="kΩ", decimals=0, group="电池",
            alarm_def=alarm("critical", lo=150, lo_lo=80),
            description="直流侧对地绝缘；仿真中周期性跌破 150kΩ",
        ),
        _p(
            "bms_state_word", "电池状态字", group="电池", display="card",
            bits=_BATTERY_BITS, archive=False,
        ),
        _p(
            "bms_allow_charge_current", "允许充电电流", unit="A", decimals=1, group="电池",
        ),
        _p(
            "bms_allow_discharge_current", "允许放电电流", unit="A", decimals=1, group="电池",
        ),
        # ------------------------------------------------ 状态与控制
        _p(
            "bms_soc_high_limit", "SOC上限设定", unit="%", decimals=1,
            group="状态与控制", writable=True, write_min=50.0, write_max=100.0,
            archive=False, description="仿真的 SOC 不会超过这个值",
        ),
        _p(
            "bms_soc_low_limit", "SOC下限设定", unit="%", decimals=1,
            group="状态与控制", writable=True, write_min=0.0, write_max=80.0,
            archive=False, description="仿真的 SOC 不会低于这个值",
        ),
        # ------------------------------------------------ 温度
        _p(
            "bms_ambient_temperature", "环境温度", unit="℃", decimals=1,
            data_type="int16", group="温度", alarm_def=alarm("warning", hi=45),
        ),
        # ------------------------------------------------ 统计
        _p(
            "bms_total_charge", "累计充电量", unit="kWh", decimals=1, group="统计",
            data_type="uint32", archive=False,
        ),
        _p(
            "bms_total_discharge", "累计放电量", unit="kWh", decimals=1, group="统计",
            data_type="uint32", archive=False,
        ),
        _p(
            "bms_cycle_count", "循环次数", unit="次", decimals=0, group="统计",
            archive=False,
        ),
    ]


# --------------------------------------------------------------- 示意图


def bess_diagram():
    """储能电站主回路示意图。

    10kV 电网 → 并网断路器 → 升压变压器 → PCS（储能变流器）→ 直流母线 → 电池簇 BMS，
    另有消防/温控与关口计量。带 ``action`` 的节点可点击进设备详情页，
    ``value_point`` 的节点直接在主回路上显示实时值。
    """
    nodes = [
        node("n_grid", "grid", "10kV 电网", 0.01, 0.36, 0.13, 0.20, sub_label="三相交流 · 市电"),
        node("n_qf", "breaker", "QF1", 0.16, 0.40, 0.08, 0.13, sub_label="并网断路器"),
        node(
            "n_tr", "transformer", "T1 升压变", 0.27, 0.35, 0.13, 0.19,
            sub_label="0.4/10kV",
        ),
        node(
            "n_pcs", "inverter", "PCS 储能变流器", 0.43, 0.30, 0.18, 0.26,
            sub_label="500kW · Modbus 从站31",
            action="device", target=PCS_DEVICE,
            description="双向变流：充电时 AC/DC 整流，放电时 DC/AC 逆变（本示例 unit=31）",
        ),
        node(
            "n_bus", "bus", "直流母线", 0.64, 0.37, 0.14, 0.16,
            sub_label="1100V DC",
            value_point=f"{PCS_DEVICE}.直流母线电压", unit="V", decimals=1,
        ),
        node(
            "n_bat", "battery", "电池簇 ×8", 0.81, 0.30, 0.15, 0.18,
            sub_label="LFP · 1P416S",
            action="device", target=BMS_DEVICE,
            value_point=f"{BMS_DEVICE}.SOC", unit="%", decimals=1,
        ),
        node(
            "n_bms", "dsp", "BMS 主控", 0.81, 0.60, 0.15, 0.18,
            sub_label="与 PCS 同从站 31",
            action="device", target=BMS_DEVICE,
            description="电池簇信息采集与保护：SOC/单体电压/单体温度/绝缘",
        ),
        node(
            "n_meter", "meter", "关口计量表", 0.27, 0.68, 0.13, 0.15,
            sub_label="双向计量", description="充放电量结算用",
        ),
        node(
            "n_hvac", "text", "消防 / 温控", 0.55, 0.68, 0.18, 0.13,
            sub_label="可燃气体探测 + 空调",
            description="与电池舱温度、告警字联动（本示例只作示意）",
        ),
    ]
    links = [
        link("n_grid", "n_qf", "ac", "10kV"),
        link("n_qf", "n_tr", "ac"),
        link("n_tr", "n_pcs", "ac", "0.4kV"),
        link("n_pcs", "n_bus", "dc", "1100V"),
        link("n_bus", "n_bat", "dc"),
        link("n_bat", "n_bms", "signal", "BMS"),
        link("n_tr", "n_meter", "signal"),
        link("n_bat", "n_hvac", "signal", "温控"),
    ]
    return diagram("储能电站主回路（PCS + 电池簇）", nodes, links)


# --------------------------------------------------------------- 工程


def build() -> Project:
    """建一台可直接采集的储能电站示例工程（unit 31，Modbus TCP 指向内置仿真器）。"""
    project = Project(
        name=NAME,
        folder_name=FOLDER_NAME,
        history_interval_s=5,
        history_retention_days=30,
        storage=ProjectStorage(),
        notes=(
            "内置示例：集装箱式储能电站（PCS + 电池簇 BMS）。\n"
            "通道指向 tools/modbus_sim.py 的 unit 31，无硬件即可联调；\n"
            "上现场时把链路改成 rtu + 实际串口即可。"
        ),
    )

    project.channels.append(
        ChannelDef(
            name=CHANNEL_NAME,
            driver="modbus",
            link="tcp",
            description="示例走 TCP 指向内置仿真器 127.0.0.1:5020（从站 31）",
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

    pcs = DeviceDef(
        name=PCS_DEVICE,
        channel=CHANNEL_NAME,
        unit_id=UNIT,
        poll_interval_ms=POLL_INTERVAL_MS,
        description="储能变流器（PCS）：直流侧 + 交流侧 + 状态控制 + 波形缓冲",
    )
    pcs.points = pcs_points()
    project.devices.append(pcs)

    bms = DeviceDef(
        name=BMS_DEVICE,
        channel=CHANNEL_NAME,
        unit_id=UNIT,
        poll_interval_ms=POLL_INTERVAL_MS,
        description="电池管理系统（BMS）：簇电压电流、SOC/SOH、单体极值、绝缘、统计",
    )
    bms.points = bms_points()
    project.devices.append(bms)

    project.diagram = bess_diagram()
    return project


# --------------------------------------------------------------- 仿真


def _raw_u(regs: list[int], key: str) -> int:
    """读一个寄存器原始值（无符号）。"""
    return int(regs[_REG[key][0]]) & 0xFFFF


def _raw_i(regs: list[int], key: str) -> int:
    """读一个寄存器原始值（按 int16 解释）。"""
    value = _raw_u(regs, key)
    return value - 0x10000 if value & 0x8000 else value


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _resolve_state(control: int, closed: bool) -> int | None:
    """由控制字与并网开关推出运行状态；``None`` 表示"自动模式"（按内置曲线跑）。"""
    if not closed:
        return STATE_STOP
    if control == _CTRL_STOP:
        return STATE_STOP
    if control == _CTRL_RESET:
        return STATE_READY
    if control == _CTRL_START:
        return STATE_READY
    if control == _CTRL_CHARGE:
        return STATE_CHARGE
    if control == _CTRL_DISCHARGE:
        return STATE_DISCHARGE
    return None


def _soc_target(t: float, state: int | None, soc_low: float, soc_high: float) -> float:
    """SOC 仿真值。

    自动模式与 PCS 的功率曲线同相（放电时 SOC 下降）；
    手动充/放模式则朝设定区间的另一端单调爬（方向一定对，便于演示）。
    """
    span = max(0.5, soc_high - soc_low)
    if state == STATE_CHARGE:
        return soc_low + span * ((t % SOC_RAMP_S) / SOC_RAMP_S)
    if state == STATE_DISCHARGE:
        return soc_high - span * ((t % SOC_RAMP_S) / SOC_RAMP_S)
    return 55.0 + 10.0 * math.cos(2.0 * math.pi * t / AUTO_CYCLE_S)


def _simulate_values(store: Any, t: float) -> tuple[dict[str, float], list[tuple[str, list[float]]]]:
    """算出这一拍的工程量（纯计算），返回 ``(标量, 波形)``。

    写成"先算值、再写寄存器"两段，是为了让点表与仿真共用同一张 ``_REG`` 表：
    这里只管工程量的物理关系，写回时按表里的地址/scale/编码统一编码。
    """
    regs = store_registers(store)

    # 上电首次（寄存器区还是全 0）：把并网开关合上、SOC 设定补成默认值，
    # 这样直接启动仿真器就能看到一台正常运行的电站，不用先点按钮。
    if not any(regs[:32]):
        store.coils[COIL_GRID_SWITCH] = True
    if regs[_REG["bms_soc_high_limit"][0]] == 0:
        put_u16(regs, _REG["bms_soc_high_limit"][0], DEFAULT_SOC_HIGH, 0.1)
    if regs[_REG["bms_soc_low_limit"][0]] == 0:
        put_u16(regs, _REG["bms_soc_low_limit"][0], DEFAULT_SOC_LOW, 0.1)

    control = _raw_u(regs, "pcs_control_word")
    if control in (_CTRL_START, _CTRL_CHARGE, _CTRL_DISCHARGE):
        store.coils[COIL_GRID_SWITCH] = True  # 收到开机/充放电命令就自动合闸
    closed = bool(store.coils[COIL_GRID_SWITCH])
    state = _resolve_state(control, closed)

    # ---------------------------------------------------------- 功率与 SOC
    setpoint = _raw_i(regs, "pcs_active_setpoint") * 0.1     # kW
    auto_power = AUTO_PEAK_KW * math.sin(2.0 * math.pi * t / AUTO_CYCLE_S)
    if state is None:
        active_power = auto_power
        state_value = STATE_CHARGE if active_power < 0 else STATE_DISCHARGE
    elif state == STATE_CHARGE:
        active_power = -abs(setpoint) if setpoint else -AUTO_PEAK_KW
        state_value = STATE_CHARGE
    elif state == STATE_DISCHARGE:
        active_power = abs(setpoint) if setpoint else AUTO_PEAK_KW
        state_value = STATE_DISCHARGE
    else:
        active_power = 0.0
        state_value = state

    soc_high = _clamp(_raw_u(regs, "bms_soc_high_limit") * 0.1, 10.0, 100.0)
    soc_low = _clamp(_raw_u(regs, "bms_soc_low_limit") * 0.1, 0.0, 90.0)
    if soc_high - soc_low < 5.0:
        soc_high = min(100.0, soc_low + 5.0)
    soc = _clamp(_soc_target(t, state, soc_low, soc_high), soc_low, soc_high)

    efficiency = 97.3 + 0.7 * math.sin(t / 33.0) - 1.2 * abs(active_power) / 500.0
    # 电池簇电压随 SOC 抬升（真实电池就是这样），再叠一点测量波动
    cluster_voltage = CLUSTER_RATED_V + 0.8 * soc + 12.0 * math.sin(t / 23.0)
    dc_power = active_power / max(0.5, efficiency / 100.0)          # kW，直流侧功率
    dc_current = dc_power * 1000.0 / max(1.0, cluster_voltage)      # A
    dc_bus_voltage = cluster_voltage + 12.0 * math.sin(t / 19.0)

    # ---------------------------------------------------------- 交流侧
    power_factor = (0.982 + 0.012 * math.sin(t / 13.0)) * (1.0 if active_power >= 0 else -1.0)
    line_current = abs(active_power) * 1000.0 / (
        math.sqrt(3.0) * GRID_LINE_V * max(0.5, abs(power_factor))
    )
    voltage_a = GRID_PHASE_V + 1.2 * math.sin(t / 11.0)
    voltage_b = GRID_PHASE_V + 1.2 * math.sin(t / 11.0 - 2.0944)
    voltage_c = GRID_PHASE_V + 1.2 * math.sin(t / 11.0 + 2.0944)

    # ---------------------------------------------------------- 温度与绝缘
    module_temperature = 52.0 + 18.0 * math.sin(t / 25.0) + 0.02 * abs(active_power)
    cabin_temperature = 32.0 + 20.0 * math.sin(t / 20.0)
    cell_temp_max = 26.0 + 5.0 * math.sin(t / 70.0) + 4.0 * abs(active_power) / AUTO_PEAK_KW
    cell_temp_min = cell_temp_max - 2.5 - 1.0 * math.sin(t / 31.0)
    ambient_temperature = 18.0 + 8.0 * math.sin(t / 90.0)
    humidity = 45.0 + 15.0 * math.sin(t / 55.0)
    insulation = 320.0 + 260.0 * math.sin(t / 70.0)

    # ---------------------------------------------------------- 单体电压
    cell_voltage_max = 2.90 + 0.0075 * soc + 0.004 * math.sin(t / 37.0)
    cell_voltage_delta = 0.030 + 0.004 * math.sin(t / 13.0)
    cell_voltage_min = cell_voltage_max - cell_voltage_delta

    # ---------------------------------------------------------- 状态字
    warn = 0
    fault = 0
    if module_temperature >= 70.0:
        warn |= 1 << 6
    if cabin_temperature >= 45.0:
        warn |= 1 << 7
    if insulation <= 150.0:
        warn |= 1 << 10
    if cell_voltage_delta >= 0.10:
        warn |= 1 << 11
    if cabin_temperature >= 50.0:
        fault |= 1 << 3        # 电池保护
    if insulation <= 80.0:
        fault |= 1 << 4        # 绝缘故障
    if module_temperature >= 80.0:
        fault |= 1 << 2        # 模块过温跳闸

    battery_state = 0
    if not warn and not fault:
        battery_state |= 1 << 0
    if state_value == STATE_READY:
        battery_state |= 1 << 1
    if state_value == STATE_CHARGE:
        battery_state |= 1 << 2
    if state_value == STATE_DISCHARGE:
        battery_state |= 1 << 3
    if warn:
        battery_state |= 1 << 4
    if fault:
        battery_state |= 1 << 5
    if insulation <= 150.0:
        battery_state |= 1 << 7
    if cabin_temperature >= 45.0:
        battery_state |= 1 << 8

    values: dict[str, float] = {
        # PCS
        "pcs_dc_bus_voltage": dc_bus_voltage,
        "pcs_dc_current": dc_current,
        "pcs_dc_power": dc_power,
        "pcs_dc_ripple": 2.4 + 0.9 * math.sin(t / 6.0),
        "pcs_voltage_a": voltage_a,
        "pcs_voltage_b": voltage_b,
        "pcs_voltage_c": voltage_c,
        "pcs_current_a": line_current * 1.004,
        "pcs_current_b": line_current * 0.998,
        "pcs_current_c": line_current * 0.999,
        "pcs_active_power": active_power,
        "pcs_reactive_power": 10.0 + 40.0 * math.sin(t / 47.0),
        "pcs_power_factor": power_factor,
        "pcs_frequency": 50.0 + 0.02 * math.sin(t / 17.0),
        "pcs_state_word": float(1 << state_value),
        "pcs_fault_word": float(fault),
        "pcs_alarm_word": float(warn),
        "pcs_module_temperature": module_temperature,
        "pcs_efficiency": efficiency,
        "pcs_daily_charge": 186.0 + 0.03 * t,
        "pcs_daily_discharge": 174.0 + 0.035 * t,
        # BMS
        "bms_cluster_voltage": cluster_voltage,
        "bms_cluster_current": dc_current,
        "bms_soc": soc,
        "bms_soh": 96.5 - 0.2 * math.sin(t / 95.0),
        "bms_cell_voltage_max": cell_voltage_max,
        "bms_cell_voltage_min": cell_voltage_min,
        "bms_cell_voltage_delta": cell_voltage_delta,
        "bms_cell_temp_max": cell_temp_max,
        "bms_cell_temp_min": cell_temp_min,
        "bms_cabin_temperature": cabin_temperature,
        "bms_cabin_humidity": humidity,
        "bms_insulation": insulation,
        "bms_state_word": float(battery_state),
        "bms_ambient_temperature": ambient_temperature,
        "bms_total_charge": 186500.0 + 0.03 * t,
        "bms_total_discharge": 172300.0 + 0.035 * t,
        "bms_cycle_count": 1287 + int(t / 1800.0),
        "bms_allow_charge_current": 260.0 + 40.0 * math.sin(t / 41.0),
        "bms_allow_discharge_current": 280.0 + 40.0 * math.sin(t / 37.0),
    }

    # ---------------------------------------------------------- 波形缓冲区
    #
    # 200 点 / 100us = 20ms = 一个工频周期；相位随时间缓慢累加，画出来就是"在动的正弦"。
    phase = 2.0 * math.pi * ((t * 0.02) % 1.0)
    peak = line_current * math.sqrt(2.0)

    def sine_samples(offset: float) -> list[float]:
        samples: list[float] = []
        for index in range(WAVE_COUNT):
            angle = 2.0 * math.pi * index / WAVE_SAMPLES_PER_CYCLE + phase + offset
            # 叠一点 5 次谐波，看起来像真实的并网电流，不是纯正弦
            samples.append(peak * math.sin(angle) + 0.04 * peak * math.sin(5.0 * angle))
        return samples

    battery_samples = [
        dc_current
        + 25.0 * math.sin(2.0 * math.pi * index / 7.0)
        + 12.0 * math.sin(2.0 * math.pi * index / 50.0 + phase)
        for index in range(WAVE_COUNT)
    ]
    waves = [
        ("wave_battery_current", battery_samples),
        ("wave_grid_current_a", sine_samples(0.0)),
        ("wave_grid_current_b", sine_samples(-2.0944)),
        ("wave_grid_current_c", sine_samples(2.0944)),
    ]
    return values, waves


def _write_scalars(regs: list[int], values: dict[str, float]) -> None:
    """把工程量按 ``_REG`` 里的地址/scale/编码写进寄存器。"""
    for key, value in values.items():
        address, scale, kind = _REG[key]
        if kind == "u16":
            put_u16(regs, address, value, scale)
        elif kind == "i16":
            put_i16(regs, address, value, scale)
        elif kind == "u32":
            put_u32(regs, address, int(round(value / scale)) if scale else int(value))


def _write_wave(regs: list[int], address: int, scale: float, samples: list[float]) -> None:
    """把一段波形工程量按 ``scale`` 写进连续寄存器（int16，超出量程就夹住）。"""
    for offset, value in enumerate(samples):
        put_i16(regs, address + offset, value, scale)


def simulate(store: Any, t: float) -> None:
    """按仿真时间 ``t``（秒）刷新从站 31 的寄存器。

    与 ``tools/modbus_sim.py`` 的其它从站一样：整个写入过程都在 ``store.lock`` 里，
    保证上位机读到的是一致的一拍数据。
    """
    regs = store_registers(store)
    with store.lock:
        values, waves = _simulate_values(store, t)
        _write_scalars(regs, values)
        for key, samples in waves:
            address, scale, _kind = _REG[key]
            _write_wave(regs, address, scale, samples)
