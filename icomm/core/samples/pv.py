"""光伏电站（组串式逆变器 + 汇流箱 + 环境监测仪）。

一个 500 kW 组串式光伏电站的示例点表，从站按现场"一个盒子一个从站"划分：

    32  光伏逆变器（PV Inverter）    直流量 / 交流量 / 状态控制 / 温度 / 统计 / 波形缓冲
    33  汇流箱（Combiner Box）       8 路组串电流、汇流箱温度、熔断器、绝缘电阻
    34  环境监测仪（Weather Station）总/斜面辐照度、组件与环境温度、风速风向、湿度气压

本模块同时是 ``tools/modbus_sim.py`` 的仿真来源：``simulate(store, t)`` 按
``store.unit_id`` 分派到 32/33/34 三套数据，所以**点表与仿真必须成对维护** ——
地址与 scale 统一放在模块级 ``_REG`` 表里，``build()`` 与 ``simulate()`` 都从那里取，
只写一份，不会两边对不上（scale 填错会出现"示波器幅值不对"这类很难查的问题）。

可写点（控制字、给定值、限功率设定、隔离开关）只在 :func:`init_defaults` 里写一次出厂值，
每轮的 ``simulate()`` 只刷只读遥测 —— 否则主站刚写进去的值下一拍就被仿真刷掉了。

寄存器映射（保持寄存器，scale = 工程量 / 原始值）

**unit 32 光伏逆变器**

| 地址 | 名称 | 类型 | 说明 |
|------|------|------|------|
| 0 | 光伏侧直流电压 | uint16 | scale 0.1 V，MPPT 工作点 |
| 1 | 光伏侧直流电流 | uint16 | scale 0.1 A |
| 2 | 直流输入功率 | uint16 | scale 0.1 kW |
| 3 | 直流母线纹波 | uint16 | scale 0.01 V，峰峰值 |
| 4~6 | 线电压 Uab/Ubc/Uca | uint16 | scale 0.1 V |
| 7~9 | A/B/C 相电流 | uint16 | scale 0.1 A |
| 10 | 有功功率 | uint16 | scale 0.1 kW |
| 11 | 无功功率 | int16 | scale 0.1 kVar，可正可负 |
| 12 | 电网频率 | uint16 | scale 0.01 Hz |
| 13 | 功率因数 | int16 | scale 0.001 |
| 14 | 转换效率 | uint16 | scale 0.01 %，0 = 未发电 |
| 15 | 三相电压不平衡度 | uint16 | scale 0.01 % |
| 16~17 | 散热器 / 机柜温度 | int16 | scale 0.1 ℃ |
| 18 | 状态字 | uint16 | ``bits`` 解码显示 |
| 19 | 故障字 | uint16 | ``bits`` 解码显示 |
| 20 | 告警字 | uint16 | ``bits`` 解码显示 |
| 21 | 控制字 | uint16 | **可写**，``commands`` 命令按钮 |
| 22 | 有功功率给定 | uint16 | **可写** scale 0.1 kW，0 = 自动 MPPT，限幅 0~550 |
| 23 | 无功功率给定 | int16 | **可写** scale 0.1 kVar，限幅 -200~200 |
| 24 | 功率因数给定 | uint16 | **可写** scale 0.001，限幅 0.8~1.0 |
| 25 | 限功率设定 | uint16 | **可写** scale 0.1 %，限幅 10~100 |
| 26 | 直流侧今日电量 | uint16 | scale 0.1 kWh |
| 27 | 今日发电量 | uint16 | scale 0.1 kWh |
| 28~29 | 累计发电量 | uint32 | scale 0.1 kWh，高字在前 |
| 30~31 | 累计运行时间 | uint32 | 1 h |
| 32 | 开机次数 | uint16 | |
| 33 | 故障次数 | uint16 | |
| 34~35 | 减排量 | uint32 | scale 0.1 kg，按 0.581 kg/kWh |
| 200~399 | 光伏侧电压波形 | int16[200] | scale 0.1 V，100us/点 |
| 400~599 | 光伏侧电流波形 | int16[200] | scale 0.1 A |
| 600~799 | 并网 A 相电压波形 | int16[200] | scale 0.1 V，瞬时值 |
| 800~999 | 并网 A 相电流波形 | int16[200] | scale 0.1 A，瞬时值 |

线圈：0 = 并网开关（**可写**），1 = 风扇运行（只读）。

**unit 33 汇流箱**

| 地址 | 名称 | 类型 | 说明 |
|------|------|------|------|
| 0~7 | 组串 1~8 电流 | uint16 | scale 0.1 A |
| 8 | 组串电压 | uint16 | scale 0.1 V |
| 9 | 组串总功率 | uint16 | scale 0.1 kW |
| 10 | 组串不平衡度 | uint16 | scale 0.1 %，报警 12 / 18 |
| 11 | 防反二极管温度 | int16 | scale 0.1 ℃ |
| 12 | 汇流箱温度 | int16 | scale 0.1 ℃，报警 60 / 70 |
| 13 | 绝缘电阻 | uint16 | scale 0.1 MΩ，低限 2.0 / 1.0 |
| 14 | 熔断器告警字 | uint16 | ``bits`` 解码显示 |
| 15 | 汇流箱状态字 | uint16 | ``bits`` 解码显示 |
| 16 | 最大组串电流 | uint16 | scale 0.1 A |
| 17 | 最小组串电流 | uint16 | scale 0.1 A |
| 18 | 组串告警字 | uint16 | ``bits`` 解码显示 |
| 19 | 投入组串数 | uint16 | 1 路 |
| 20 | 直流隔离开关 | uint16 | **可写**，0 = 分 1 = 合 |
| 21 | 柜门状态 | uint16 | 0 = 关 1 = 开 |

**unit 34 环境监测仪**

| 地址 | 名称 | 类型 | 说明 |
|------|------|------|------|
| 0 | 总辐照度 | uint16 | 1 W/m²，报警 1200 / 1400（本仿真峰值约 1000，不触发） |
| 1 | 倾斜面辐照度 | uint16 | 1 W/m² |
| 2 | 组件温度 | int16 | scale 0.1 ℃，报警 60 / 70 |
| 3 | 环境温度 | int16 | scale 0.1 ℃ |
| 4 | 风速 | uint16 | scale 0.1 m/s，报警 14 / 17 |
| 5 | 风向 | uint16 | 1 ° |
| 6 | 相对湿度 | uint16 | scale 0.1 % |
| 7 | 大气压 | uint16 | scale 0.1 hPa |
| 8 | 日降雨量 | uint16 | scale 0.1 mm |
| 9 | 背板温度 2 | int16 | scale 0.1 ℃ |
| 10 | 数据有效标志 | uint16 | 1 = 有效 |
| 11 | 辐照传感器状态 | uint16 | ``bits`` 解码显示 |
| 12 | 传感器供电电压 | uint16 | scale 0.01 V |
| 13 | 采样周期 | uint16 | 1 s |
| 14 | 通讯错误次数 | uint16 | |

仿真里会**周期性**出现的现象（用来演示报警、故障录波与趋势图，不必等现场）：

* 组件温度随"日"节律冲高：先越 60℃ 告警，再越 70℃ 触发过热故障（故障字 bit8）
* 绝缘电阻周期性跌到 2 MΩ 以下告警，最低跌到 1 MΩ 以下触发绝缘故障（故障字 bit7）
* 风速周期性冲到 14 m/s 以上、最高约 18 m/s（告警字 bit5）
* 4 号组串周期性被遮挡，组串不平衡度可越过 18%
* 直流母线纹波、散热器温度随辐照度起伏，正午前后越限

**仿真时间与真实时间**：仿真一个"日"只有 240 s（日出 t=0，日落 t=120，之后入夜），
所有周期都压缩到了秒级，方便坐在电脑前看完整过程。真实装置按天/小时计。

**汇流箱不做逆变器耦合**：汇流箱测的是阵列侧电流，只受自身直流隔离开关与辐照度控制。
现场逆变器停机时阵列电流也会接近 0，但那样两个从站就要互相知道对方状态了；
示例里刻意不这么做，好让三个从站各自独立（也方便单独替换其中一台设备）。

**依赖方向**：本模块只依赖 ``..models``，不要 import ``..project``（见包 docstring）。
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
from ._sim import put_i16, put_u16, put_u32, read_u16, store_registers

__all__ = [
    "FOLDER_NAME",
    "NAME",
    "POLL_INTERVAL_MS",
    "UNIT",
    "UNITS",
    "build",
    "init_defaults",
    "plant_state",
    "simulate",
]

# --------------------------------------------------------------- 模块接口

#: 界面上显示的工程名
NAME = "光伏电站（PV）"
#: 磁盘文件夹名（必须英文 ASCII）
FOLDER_NAME = "pv-plant"
#: 主从站号（逆变器）
UNIT = 32
#: 本示例用到的全部从站号
UNITS = (32, 33, 34)
#: 采集周期
POLL_INTERVAL_MS = 300

#: 通道名
CHANNEL_NAME = "光伏监控环网"
#: 设备名（示意图 action="device" 的 target 与 value_point 前缀都用它）
INVERTER_DEVICE = "光伏逆变器"
COMBINER_DEVICE = "汇流箱"
WEATHER_DEVICE = "环境监测仪"

# --------------------------------------------------------------- 位定义

#: 状态字位定义（按位解码，不是枚举值）
_STATE_BITS = (
    "0:停机,1:待机,2:发电运行,3:限功率运行,4:故障,5:夜间待机,6:并网,7:MPPT使能,"
    "8:风扇运行,9:降额运行,10:定值远方,11:通讯正常"
)
_FAULT_BITS = (
    "0:直流过压,1:直流欠压,2:交流过压,3:交流欠压,4:电网失压,5:过频,6:欠频,7:绝缘故障,"
    "8:过热,9:直流反接,10:熔断器熔断,11:组串故障,12:防雷器故障,13:急停"
)
_ALARM_BITS = (
    "0:辐照度偏高,1:组件温度高,2:散热器温度高,3:直流纹波偏大,4:三相电压不平衡,"
    "5:风速偏大,6:通讯超时,7:低效运行,8:限功率运行,9:绝缘偏低,10:熔断器告警,11:组串不平衡"
)
_FUSE_BITS = (
    "0:组串1熔断,1:组串2熔断,2:组串3熔断,3:组串4熔断,4:组串5熔断,5:组串6熔断,"
    "6:组串7熔断,7:组串8熔断,8:总熔断器熔断"
)
_COMBINER_BITS = "0:直流开关合位,1:直流开关分位,2:熔断器正常,3:防雷器正常,4:柜门关闭,5:通讯正常"
_STRING_ALARM_BITS = (
    "0:组串1偏低,1:组串2偏低,2:组串3偏低,3:组串4偏低,4:组串5偏低,5:组串6偏低,"
    "6:组串7偏低,7:组串8偏低,8:组串不平衡,9:汇流箱温度高,10:绝缘偏低,11:总电流偏低"
)
_PYRA_BITS = "0:辐照传感器正常,1:通讯超时,2:量程超限,3:加热开启,4:清洁提醒"

#: 控制字命令码（写一次执行一次，按边沿触发）
_CTRL_COMMANDS = "0x01:开机,0x02:关机,0x04:复位,0x08:限功率使能"
_CTRL_START = 0x01
_CTRL_STOP = 0x02
_CTRL_RESET = 0x04
_CTRL_LIMIT = 0x08

#: 状态字里"运行状态"那一位的取值
STATE_STOP = 0
STATE_READY = 1
STATE_RUN = 2
STATE_LIMIT = 3
STATE_FAULT = 4
STATE_NIGHT = 5

# --------------------------------------------------------------- 仿真常数

#: 逆变器额定有功（AC）
RATED_KW = 500.0
#: 直流侧额定输入
RATED_DC_KW = 520.0
#: 光伏侧 MPPT 工作电压
MPPT_VOLTAGE = 620.0
#: 并网相电压 RMS（线电压 400V）
GRID_PHASE_V = 231.0
#: 标准测试条件辐照度 W/m²
STC_IRRADIANCE = 1000.0
#: 组串失配与线损
ARRAY_LOSS = 0.98
#: 停机后阵列空载，电压抬到 MPPT 电压的这个倍数（接近开路电压）
OPEN_CIRCUIT_FACTOR = 1.12
#: 转换效率下限（辐照度足时再加 6%）
EFFICIENCY_MIN = 92.0
#: 仿真一个"日"的秒数（日出 t=0，日落 t=120，之后入夜）
DAY_CYCLE_S = 240.0
#: 组件温升周期（峰值刻意落在正午之后一点：先把额定功率跑出来，再进过热保护）
MODULE_T_CYCLE_S = 78.0
#: 组件温升峰值出现的时刻（仿真秒）
MODULE_T_PEAK_S = 80.0
#: 云遮周期与幅度
CLOUD_CYCLE_S = 37.0
CLOUD_DEPTH = 0.25
#: 起机门槛：辐照度低于它不发电
LOW_IRRADIANCE = 30.0
#: 组串路数
STRING_COUNT = 8
#: 4 号组串被遮挡的周期
SHADE_CYCLE_S = 53.0
#: 绝缘电阻的慢周期（周期性偏低，演示低限报警）
INSULATION_CYCLE_S = 137.0
#: 风速周期
WIND_CYCLE_S = 97.0
#: 一个仿真日的发电量（日落时爬到它，夜间保持，次日日出清零）
ENERGY_DAY_KWH = 2200.0
#: 累计发电量的起点
ENERGY_TOTAL_BASE = 1_250_000.0
#: 累计运行小时的起点（仿真按 24h/日 加速累积）
RUNNING_HOURS_BASE = 26_400
#: 统计起点
START_COUNT_BASE = 412
FAULT_COUNT_BASE = 17
#: 电网排放因子 kg/kWh，用于算减排量
CO2_FACTOR = 0.581
#: 波形点数与采样间隔（200 × 100us = 20ms = 50Hz 一个周波）
WAVE_COUNT = 200
WAVE_SAMPLE_US = 100

#: 线圈地址
COIL_GRID_SWITCH = 0
COIL_FAN = 1

# --------------------------------------------------------------- 寄存器表

#: ``{点号: (从站号, 地址, scale, 类型)}``。
#: 类型：``u16`` / ``i16`` / ``u32`` 每轮仿真写；``wave`` 是波形数组；
#: ``rw`` 是**可写点**（只用来取地址/scale，仿真不写，免得刷掉主站写进去的值）。
_REG: dict[str, tuple[int, int, float, str]] = {
    # ------------------------------------------------ unit 32 逆变器：直流侧
    "inv_dc_voltage": (32, 0, 0.1, "u16"),
    "inv_dc_current": (32, 1, 0.1, "u16"),
    "inv_dc_power": (32, 2, 0.1, "u16"),
    "inv_dc_ripple": (32, 3, 0.01, "u16"),
    # ------------------------------------------------ unit 32 逆变器：交流侧
    "inv_voltage_ab": (32, 4, 0.1, "u16"),
    "inv_voltage_bc": (32, 5, 0.1, "u16"),
    "inv_voltage_ca": (32, 6, 0.1, "u16"),
    "inv_current_a": (32, 7, 0.1, "u16"),
    "inv_current_b": (32, 8, 0.1, "u16"),
    "inv_current_c": (32, 9, 0.1, "u16"),
    "inv_active_power": (32, 10, 0.1, "u16"),
    "inv_reactive_power": (32, 11, 0.1, "i16"),
    "inv_frequency": (32, 12, 0.01, "u16"),
    "inv_power_factor": (32, 13, 0.001, "i16"),
    "inv_efficiency": (32, 14, 0.01, "u16"),
    "inv_voltage_unbalance": (32, 15, 0.01, "u16"),
    # ------------------------------------------------ unit 32 逆变器：温度与状态
    "inv_heatsink_temperature": (32, 16, 0.1, "i16"),
    "inv_cabinet_temperature": (32, 17, 0.1, "i16"),
    "inv_state_word": (32, 18, 1.0, "u16"),
    "inv_fault_word": (32, 19, 1.0, "u16"),
    "inv_alarm_word": (32, 20, 1.0, "u16"),
    # ------------------------------------------------ unit 32 逆变器：可写点
    "inv_control_word": (32, 21, 1.0, "rw"),
    "inv_active_setpoint": (32, 22, 0.1, "rw"),
    "inv_reactive_setpoint": (32, 23, 0.1, "rw"),
    "inv_pf_setpoint": (32, 24, 0.001, "rw"),
    "inv_limit_power_setpoint": (32, 25, 0.1, "rw"),
    # ------------------------------------------------ unit 32 逆变器：统计
    "inv_dc_energy_today": (32, 26, 0.1, "u16"),
    "inv_energy_today": (32, 27, 0.1, "u16"),
    "inv_energy_total": (32, 28, 0.1, "u32"),
    "inv_running_hours": (32, 30, 1.0, "u32"),
    "inv_start_count": (32, 32, 1.0, "u16"),
    "inv_fault_count": (32, 33, 1.0, "u16"),
    "inv_co2_reduction": (32, 34, 0.1, "u32"),
    # ------------------------------------------------ unit 32 逆变器：波形数组
    "wave_inv_dc_voltage": (32, 200, 0.1, "wave"),
    "wave_inv_dc_current": (32, 400, 0.1, "wave"),
    "wave_inv_voltage_a": (32, 600, 0.1, "wave"),
    "wave_inv_current_a": (32, 800, 0.1, "wave"),
    # ------------------------------------------------ unit 33 汇流箱
    "str1_current": (33, 0, 0.1, "u16"),
    "str2_current": (33, 1, 0.1, "u16"),
    "str3_current": (33, 2, 0.1, "u16"),
    "str4_current": (33, 3, 0.1, "u16"),
    "str5_current": (33, 4, 0.1, "u16"),
    "str6_current": (33, 5, 0.1, "u16"),
    "str7_current": (33, 6, 0.1, "u16"),
    "str8_current": (33, 7, 0.1, "u16"),
    "str_voltage": (33, 8, 0.1, "u16"),
    "str_power": (33, 9, 0.1, "u16"),
    "str_imbalance": (33, 10, 0.1, "u16"),
    "str_diode_temperature": (33, 11, 0.1, "i16"),
    "combiner_temperature": (33, 12, 0.1, "i16"),
    "str_insulation": (33, 13, 0.1, "u16"),
    "str_fuse_word": (33, 14, 1.0, "u16"),
    "combiner_state_word": (33, 15, 1.0, "u16"),
    "str_max_current": (33, 16, 0.1, "u16"),
    "str_min_current": (33, 17, 0.1, "u16"),
    "str_alarm_word": (33, 18, 1.0, "u16"),
    "str_online_count": (33, 19, 1.0, "u16"),
    "combiner_dc_switch": (33, 20, 1.0, "rw"),
    "combiner_door": (33, 21, 1.0, "u16"),
    # ------------------------------------------------ unit 34 环境监测仪
    "env_irradiance": (34, 0, 1.0, "u16"),
    "env_irradiance_tilt": (34, 1, 1.0, "u16"),
    "env_module_temperature": (34, 2, 0.1, "i16"),
    "env_ambient_temperature": (34, 3, 0.1, "i16"),
    "env_wind_speed": (34, 4, 0.1, "u16"),
    "env_wind_direction": (34, 5, 1.0, "u16"),
    "env_humidity": (34, 6, 0.1, "u16"),
    "env_pressure": (34, 7, 0.1, "u16"),
    "env_rainfall": (34, 8, 0.1, "u16"),
    "env_module_temperature_2": (34, 9, 0.1, "i16"),
    "env_data_valid": (34, 10, 1.0, "u16"),
    "env_pyranometer_status": (34, 11, 1.0, "u16"),
    "env_power_supply_voltage": (34, 12, 0.01, "u16"),
    "env_sample_interval": (34, 13, 1.0, "u16"),
    "env_comm_error_count": (34, 14, 1.0, "u16"),
}

#: 命令码的去重记忆（命令按边沿执行一次）
_LAST_COMMAND: dict[int, int] = {}

#: ``plant_state()`` 的结果缓存：同一轮仿真里 32/33/34 拿到的 ``t`` 相同
_STATE_CACHE: list[Any] = [None, None]


def _addr(key: str) -> int:
    """取某个点号的寄存器地址。"""
    return _REG[key][1]


def _scale(key: str) -> float:
    """取某个点号的 scale。"""
    return _REG[key][2]


# --------------------------------------------------------------- 构造助手


def _p(key: str, name: str, unit: str = "", label: str = "", **kwargs):
    """按 ``_REG`` 取 ``(地址, scale)`` 建一个标量点位。"""
    return scalar(name, label or name, _addr(key), unit=unit, scale=_scale(key), **kwargs)


def _w(key: str, name: str, unit: str, color: str, label: str = ""):
    """按 ``_REG`` 取地址/scale 建一个波形数组点位。"""
    return wave_point(
        name, label or name, _addr(key), WAVE_COUNT, unit, _scale(key), WAVE_SAMPLE_US, color
    )


# --------------------------------------------------------------- 点表


def inverter_points() -> list:
    """光伏逆变器点位（直流侧 / 交流侧 / 状态与控制 / 温度 / 统计 / 波形）。"""
    return [
        # ------------------------------------------------ 直流侧
        _p(
            "inv_dc_voltage", "光伏侧直流电压", unit="V", decimals=1,
            group="直流侧", display="gauge", gauge_min=0, gauge_max=900,
            alarm_def=alarm("warning", hi=800, hi_hi=850),
            description="组串汇流后的 MPPT 工作电压，正常 600~700V；停机时为空载电压",
        ),
        _p(
            "inv_dc_current", "光伏侧直流电流", unit="A", decimals=1, group="直流侧",
            display="gauge", gauge_min=0, gauge_max=1000,
            description="正值发电；夜间与停机时为 0",
        ),
        _p(
            "inv_dc_power", "直流输入功率", unit="kW", decimals=1, group="直流侧",
            display="gauge", gauge_min=0, gauge_max=600,
            alarm_def=alarm("warning", hi=540),
            description="阵列输出功率，只跟辐照度与温度有关",
        ),
        _p(
            "inv_dc_ripple", "直流母线纹波", unit="V", decimals=2, group="直流侧",
            alarm_def=alarm("warning", hi=4, hi_hi=6),
            description="直流母线电压交流分量峰峰值，以 6 倍频（300Hz）为主",
        ),
        # ------------------------------------------------ 交流侧
        _p(
            "inv_voltage_ab", "线电压 Uab", unit="V", decimals=1, group="交流侧",
            display="gauge", gauge_min=0, gauge_max=500,
            alarm_def=alarm("warning", hi=440, hi_hi=460, lo=340, lo_lo=320),
        ),
        _p(
            "inv_voltage_bc", "线电压 Ubc", unit="V", decimals=1, group="交流侧",
            alarm_def=alarm("warning", hi=440, hi_hi=460, lo=340, lo_lo=320),
        ),
        _p(
            "inv_voltage_ca", "线电压 Uca", unit="V", decimals=1, group="交流侧",
            alarm_def=alarm("warning", hi=440, hi_hi=460, lo=340, lo_lo=320),
        ),
        _p(
            "inv_current_a", "A 相电流", unit="A", decimals=1, group="交流侧",
            display="gauge", gauge_min=0, gauge_max=800,
        ),
        _p("inv_current_b", "B 相电流", unit="A", decimals=1, group="交流侧"),
        _p("inv_current_c", "C 相电流", unit="A", decimals=1, group="交流侧"),
        _p(
            "inv_active_power", "有功功率", unit="kW", decimals=1, group="交流侧",
            display="gauge", gauge_min=0, gauge_max=600,
            alarm_def=alarm("warning", hi=520),
            description="并网点有功，受限功率设定与有功给定约束",
        ),
        _p(
            "inv_reactive_power", "无功功率", unit="kVar", decimals=1, group="交流侧",
            description="由功率因数给定决定；给定 1.000 时只发有功",
        ),
        _p(
            "inv_frequency", "电网频率", unit="Hz", decimals=2, group="交流侧",
            alarm_def=alarm("critical", hi_hi=50.5, hi=50.2, lo=49.8, lo_lo=49.5),
        ),
        _p(
            "inv_power_factor", "功率因数", decimals=3, group="交流侧",
            description="1.000 = 纯有功；给定小于 1 时按给定发无功",
        ),
        _p(
            "inv_efficiency", "转换效率", unit="%", decimals=2, group="交流侧",
            display="gauge", gauge_min=80, gauge_max=100,
            description="0 = 未发电（停机/夜间）；发电时约 92~98%",
        ),
        _p(
            "inv_voltage_unbalance", "三相电压不平衡度", unit="%", decimals=2, group="交流侧",
            alarm_def=alarm("warning", hi=2, hi_hi=4),
        ),
        # ------------------------------------------------ 温度
        _p(
            "inv_heatsink_temperature", "散热器温度", unit="℃", decimals=1, group="温度",
            display="gauge", gauge_min=0, gauge_max=90,
            alarm_def=alarm("warning", hi=65, hi_hi=75),
        ),
        _p(
            "inv_cabinet_temperature", "机柜温度", unit="℃", decimals=1, group="温度",
            alarm_def=alarm("warning", hi=55),
        ),
        # ------------------------------------------------ 状态与控制
        _p(
            "inv_state_word", "状态字", group="状态与控制", decimals=0, bits=_STATE_BITS,
            archive=False, description="按位解码：停机/待机/发电/限功率/故障/夜间/并网…",
        ),
        _p(
            "inv_fault_word", "故障字", group="状态与控制", decimals=0, bits=_FAULT_BITS,
            archive=False,
            description="本仿真会置 bit7 绝缘故障、bit8 过热；其余位留给现场",
        ),
        _p(
            "inv_alarm_word", "告警字", group="状态与控制", decimals=0, bits=_ALARM_BITS,
            archive=False, description="越限类告警，不影响并网",
        ),
        _p(
            "inv_control_word", "控制字", group="状态与控制", decimals=0,
            writable=True, commands=_CTRL_COMMANDS, archive=False,
            description="写命令码：0x01 开机、0x02 关机、0x04 复位、0x08 限功率使能",
        ),
        _p(
            "inv_active_setpoint", "有功功率给定", unit="kW", decimals=1, group="状态与控制",
            writable=True, write_min=0.0, write_max=550.0, archive=False,
            description="0 = 自动 MPPT；写值后作为功率上限",
        ),
        _p(
            "inv_reactive_setpoint", "无功功率给定", unit="kVar", decimals=1, group="状态与控制",
            data_type="int16", writable=True, write_min=-200.0, write_max=200.0,
            archive=False, description="留给现场按调度下发无功；默认 0 时由功率因数给定决定",
        ),
        _p(
            "inv_pf_setpoint", "功率因数给定", decimals=3, group="状态与控制",
            writable=True, write_min=0.8, write_max=1.0, archive=False,
            description="1.000 = 只发有功；0.900 = 按 0.9 发无功",
        ),
        _p(
            "inv_limit_power_setpoint", "限功率设定", unit="%", decimals=1, group="状态与控制",
            writable=True, write_min=10.0, write_max=100.0, archive=False,
            description="控制字写 0x08 后按此百分比限功率；写 0x04 或 0 回自动",
        ),
        scalar(
            "并网开关", "并网开关", COIL_GRID_SWITCH, register="coil", data_type="bool",
            writable=True, display="lamp", group="状态与控制", archive=False,
            description="分闸后逆变器停机；写 1 合闸",
        ),
        scalar(
            "风扇运行", "风扇运行", COIL_FAN, register="coil", data_type="bool",
            display="lamp", group="状态与控制", archive=False,
            description="散热器超过 45℃ 且正在发电时启动",
        ),
        # ------------------------------------------------ 统计
        _p("inv_dc_energy_today", "直流侧今日电量", unit="kWh", decimals=1, group="统计"),
        _p("inv_energy_today", "今日发电量", unit="kWh", decimals=1, group="统计"),
        _p(
            "inv_energy_total", "累计发电量", unit="kWh", decimals=1, group="统计",
            data_type="uint32",
        ),
        _p(
            "inv_running_hours", "累计运行时间", unit="h", decimals=0, group="统计",
            data_type="uint32",
        ),
        _p("inv_start_count", "开机次数", unit="次", decimals=0, group="统计"),
        _p("inv_fault_count", "故障次数", unit="次", decimals=0, group="统计"),
        _p(
            "inv_co2_reduction", "减排量", unit="kg", decimals=1, group="统计",
            data_type="uint32", description=f"按 {CO2_FACTOR} kg/kWh 折算",
        ),
        # ------------------------------------------------ 波形
        _w("wave_inv_dc_voltage", "光伏侧电压波形", "V", "#38bdf8"),
        _w("wave_inv_dc_current", "光伏侧电流波形", "A", "#facc15"),
        _w("wave_inv_voltage_a", "并网 A 相电压（瞬时）", "V", "#22d3ee"),
        _w("wave_inv_current_a", "并网 A 相电流（瞬时）", "A", "#a78bfa"),
    ]


def combiner_points() -> list:
    """汇流箱点位（组串电流 / 汇流箱温度 / 绝缘 / 熔断器 / 状态）。"""
    points: list = []
    for index in range(STRING_COUNT):
        points.append(
            _p(
                f"str{index + 1}_current", f"组串 {index + 1} 电流", unit="A", decimals=1,
                group="组串电流",
                description="单路组串电流，同辐照度下各串应基本一致",
            )
        )
    points += [
        _p(
            "str_voltage", "组串电压", unit="V", decimals=1, group="组串电流",
            display="gauge", gauge_min=0, gauge_max=900,
        ),
        _p(
            "str_power", "组串总功率", unit="kW", decimals=1, group="组串电流",
            alarm_def=alarm("warning", hi=540),
        ),
        _p(
            "str_imbalance", "组串不平衡度", unit="%", decimals=1, group="组串电流",
            display="gauge", gauge_min=0, gauge_max=40,
            alarm_def=alarm("warning", hi=12, hi_hi=18),
            description="(最大 - 最小) / 平均 × 100%；4 号组串周期性被遮挡",
        ),
        _p("str_max_current", "最大组串电流", unit="A", decimals=1, group="组串电流"),
        _p("str_min_current", "最小组串电流", unit="A", decimals=1, group="组串电流"),
        _p("str_online_count", "投入组串数", unit="路", decimals=0, group="组串电流"),
        _p(
            "str_diode_temperature", "防反二极管温度", unit="℃", decimals=1, group="温度",
            alarm_def=alarm("warning", hi=75),
        ),
        _p(
            "combiner_temperature", "汇流箱温度", unit="℃", decimals=1, group="温度",
            display="gauge", gauge_min=0, gauge_max=90,
            alarm_def=alarm("warning", hi=60, hi_hi=70),
        ),
        _p(
            "str_insulation", "绝缘电阻", unit="MΩ", decimals=2, group="绝缘与保护",
            display="gauge", gauge_min=0, gauge_max=10,
            alarm_def=alarm("critical", lo=2.0, lo_lo=1.0),
            description="直流侧对地绝缘；低于 1MΩ 触发绝缘故障（故障字 bit7）",
        ),
        _p(
            "str_fuse_word", "熔断器告警字", group="绝缘与保护", decimals=0,
            bits=_FUSE_BITS, archive=False,
            description="本仿真无熔断器动作，留给现场对照",
        ),
        _p(
            "combiner_state_word", "汇流箱状态字", group="绝缘与保护", decimals=0,
            bits=_COMBINER_BITS, archive=False,
        ),
        _p(
            "str_alarm_word", "组串告警字", group="绝缘与保护", decimals=0,
            bits=_STRING_ALARM_BITS, archive=False,
        ),
        _p(
            "combiner_dc_switch", "直流隔离开关", group="绝缘与保护", decimals=0,
            writable=True, archive=False,
            description="0 = 分（组串电流归零）1 = 合",
        ),
        _p(
            "combiner_door", "柜门状态", group="绝缘与保护", decimals=0,
            description="0 = 关 1 = 开",
        ),
    ]
    return points


def weather_points() -> list:
    """环境监测仪点位（辐照度 / 温度 / 风速风向 / 湿度气压）。"""
    return [
        _p(
            "env_irradiance", "总辐照度", unit="W/m²", decimals=0, group="辐照",
            display="gauge", gauge_min=0, gauge_max=1400,
            alarm_def=alarm("warning", hi=1200, hi_hi=1400),
            description="水平面总辐照度；本仿真峰值约 1000 W/m²（高限只作配置示例）",
        ),
        _p(
            "env_irradiance_tilt", "倾斜面辐照度", unit="W/m²", decimals=0, group="辐照",
            description="组件倾角面上的辐照度，比水平面高约 8%",
        ),
        _p(
            "env_module_temperature", "组件温度", unit="℃", decimals=1, group="温度",
            display="gauge", gauge_min=0, gauge_max=90,
            alarm_def=alarm("critical", hi=60, hi_hi=70),
            description="仿真中每天先越 60℃ 告警、再越 70℃ 触发过热故障",
        ),
        _p("env_ambient_temperature", "环境温度", unit="℃", decimals=1, group="温度"),
        _p("env_module_temperature_2", "背板温度 2", unit="℃", decimals=1, group="温度"),
        _p(
            "env_wind_speed", "风速", unit="m/s", decimals=1, group="气象",
            display="gauge", gauge_min=0, gauge_max=30,
            alarm_def=alarm("warning", hi=14, hi_hi=17),
            description="周期性冲到 14 m/s 以上，最高约 18 m/s",
        ),
        _p(
            "env_wind_direction", "风向", unit="°", decimals=0, group="气象",
            description="0° = 正北，顺时针增加",
        ),
        _p("env_humidity", "相对湿度", unit="%", decimals=1, group="气象"),
        _p("env_pressure", "大气压", unit="hPa", decimals=1, group="气象"),
        _p("env_rainfall", "日降雨量", unit="mm", decimals=1, group="气象"),
        _p(
            "env_pyranometer_status", "辐照传感器状态", group="气象", decimals=0,
            bits=_PYRA_BITS, archive=False,
        ),
        _p(
            "env_data_valid", "数据有效标志", group="装置", decimals=0,
            description="1 = 本机数据有效",
        ),
        _p("env_power_supply_voltage", "传感器供电电压", unit="V", decimals=2, group="装置"),
        _p("env_sample_interval", "采样周期", unit="s", decimals=0, group="装置"),
        _p("env_comm_error_count", "通讯错误次数", unit="次", decimals=0, group="装置"),
    ]


# --------------------------------------------------------------- 示意图


def pv_diagram():
    """光伏电站主回路示意图。

    组串 → 汇流箱 → 组串式逆变器 → 升压变 → 并网计量 → 10kV 电网，
    另带逆变控制 DSP 与环境监测仪。带 ``action`` 的节点点击进设备详情页，
    ``value_point`` 的节点直接在主回路上显示实时值。
    """
    nodes = [
        node(
            "n_pv1", "pv", "光伏组串 ×8", 0.01, 0.08, 0.14, 0.15,
            sub_label=f"{STRING_COUNT} 路 · 620V DC",
            action="device", target=COMBINER_DEVICE,
            description="单晶组件串联，每路接入汇流箱的一路熔断器输入",
        ),
        node(
            "n_pv2", "pv", "光伏组串 ×8", 0.01, 0.42, 0.14, 0.15,
            sub_label=f"{STRING_COUNT} 路 · 620V DC",
            action="device", target=COMBINER_DEVICE,
        ),
        node(
            "n_comb", "bus", "汇流箱", 0.22, 0.18, 0.13, 0.30,
            sub_label="8 进 1 出 · 熔断器",
            action="device", target=COMBINER_DEVICE,
            value_point=f"{COMBINER_DEVICE}.组串不平衡度", unit="%", decimals=1,
            description="组串汇流 + 防反二极管 + 熔断器 + 绝缘监测（本示例 unit=33）",
        ),
        node(
            "n_inv", "inverter", "组串式逆变器", 0.42, 0.16, 0.18, 0.28,
            sub_label="500kW · 从站 32",
            action="device", target=INVERTER_DEVICE,
            value_point=f"{INVERTER_DEVICE}.有功功率", unit="kW", decimals=1,
            description="MPPT + 锁相 + PWM 逆变：直流 620V 逆变成 400V 三相交流",
        ),
        node(
            "n_mppt", "dcdc", "MPPT 升压", 0.42, 0.58, 0.18, 0.14,
            sub_label="Boost DC/DC",
            action="device", target=INVERTER_DEVICE,
            description="把组串电压跟踪到最大功率点，再抬到直流母线电压",
        ),
        node(
            "n_tr", "transformer", "T1 升压变", 0.66, 0.18, 0.13, 0.20,
            sub_label="0.4/10kV",
        ),
        node(
            "n_meter", "meter", "并网计量表", 0.66, 0.58, 0.13, 0.15,
            sub_label="双向计量",
            description="上网电量结算用",
        ),
        node(
            "n_grid", "grid", "10kV 电网", 0.85, 0.18, 0.14, 0.20,
            sub_label="三相交流 · 50Hz",
        ),
        node(
            "n_dsp", "dsp", "逆变控制 DSP", 0.22, 0.62, 0.16, 0.16,
            sub_label="MPPT + 锁相 + 保护",
            action="device", target=INVERTER_DEVICE,
            description="与逆变器同从站 32：控制字、状态字、故障字都在这块装置里",
        ),
        node(
            "n_env", "meter", "环境监测仪", 0.01, 0.62, 0.16, 0.16,
            sub_label="辐照 / 温度 / 风速",
            action="device", target=WEATHER_DEVICE,
            value_point=f"{WEATHER_DEVICE}.总辐照度", unit="W/m²", decimals=0,
            description="辐照度与组件温度是判断发电量是否正常的关键依据（unit=34）",
        ),
    ]
    links = [
        link("n_pv1", "n_comb", "dc", "620V"),
        link("n_pv2", "n_comb", "dc"),
        link("n_comb", "n_inv", "dc", "620V"),
        link("n_inv", "n_mppt", "signal"),
        link("n_inv", "n_tr", "ac", "0.4kV"),
        link("n_tr", "n_grid", "ac", "10kV"),
        link("n_tr", "n_meter", "signal"),
        link("n_dsp", "n_inv", "signal", "控制"),
        link("n_env", "n_comb", "signal", "环境"),
    ]
    return diagram("光伏电站主回路（组串 + 逆变器）", nodes, links)


# --------------------------------------------------------------- 工程


def build() -> Project:
    """建一台可直接采集的光伏电站示例工程（unit 32/33/34，TCP 指向内置仿真器）。"""
    project = Project(
        name=NAME,
        folder_name=FOLDER_NAME,
        history_interval_s=5,
        history_retention_days=30,
        storage=ProjectStorage(),
        notes=(
            "内置示例：500kW 组串式光伏电站（逆变器 + 汇流箱 + 环境监测仪）。\n"
            "三台设备分别走 32 / 33 / 34 号从站，都指向 tools/modbus_sim.py，无硬件即可联调；\n"
            "仿真一个“日”只要 240s，坐一会儿就能看全 告警 → 故障 → 恢复 的完整过程。\n"
            "上现场时把链路改成 rtu + 实际串口/网关地址即可。"
        ),
    )

    project.channels.append(
        ChannelDef(
            name=CHANNEL_NAME,
            driver="modbus",
            link="tcp",
            description="示例走 TCP 指向内置仿真器 127.0.0.1:5020（从站 32/33/34）",
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

    inverter = DeviceDef(
        name=INVERTER_DEVICE,
        channel=CHANNEL_NAME,
        unit_id=32,
        poll_interval_ms=POLL_INTERVAL_MS,
        description="组串式逆变器：直流量 / 交流量 / 状态控制 / 温度 / 统计 / 波形缓冲",
    )
    inverter.points = inverter_points()
    project.devices.append(inverter)

    combiner = DeviceDef(
        name=COMBINER_DEVICE,
        channel=CHANNEL_NAME,
        unit_id=33,
        poll_interval_ms=POLL_INTERVAL_MS,
        description="汇流箱：8 路组串电流、汇流箱温度、熔断器、绝缘电阻",
    )
    combiner.points = combiner_points()
    project.devices.append(combiner)

    weather = DeviceDef(
        name=WEATHER_DEVICE,
        channel=CHANNEL_NAME,
        unit_id=34,
        poll_interval_ms=POLL_INTERVAL_MS,
        description="环境监测仪：辐照度、组件/环境温度、风速风向、湿度气压",
    )
    weather.points = weather_points()
    project.devices.append(weather)

    project.diagram = pv_diagram()
    return project


# --------------------------------------------------------------- 可写点默认值


def init_defaults(store: Any) -> None:
    """给可写点位写一次出厂默认值（主站写过之后，每轮仿真不再碰这些地址）。

    ``tools/modbus_sim.py`` 建好数据区后、进入仿真循环前，对每个从站调一次。
    """
    unit = int(getattr(store, "unit_id", 0) or 0)
    regs = store_registers(store)
    with store.lock:
        if unit == 32:
            # 默认并网、功率因数 1.000、限功率 100%（= 不限）；
            # 控制字与功率给定留 0，即"自动 MPPT"
            store.coils[COIL_GRID_SWITCH] = True
            store.coils[COIL_FAN] = False
            put_u16(regs, _addr("inv_pf_setpoint"), 1.0, _scale("inv_pf_setpoint"))
            put_u16(
                regs,
                _addr("inv_limit_power_setpoint"),
                100.0,
                _scale("inv_limit_power_setpoint"),
            )
        elif unit == 33:
            # 直流隔离开关默认合上，否则组串电流一直是 0
            put_u16(regs, _addr("combiner_dc_switch"), 1.0, _scale("combiner_dc_switch"))


# --------------------------------------------------------------- 仿真：物理量


def plant_state(t: float) -> dict[str, Any]:
    """算出 ``t`` 时刻与命令状态**无关**的物理量（辐照度、温度、直流功率…）。

    返回的是中间量而不是点号：三个从站都从这里取数，再各自叠加自己的开关/命令状态，
    最后按 ``_REG`` 写寄存器。同一轮仿真里 32/33/34 拿到的 ``t`` 相同，所以结果按
    ``t`` 缓存一份，三台设备只算一遍。

    仿真时间刻度：``t`` 是仿真秒，一个"日" = :data:`DAY_CYCLE_S` 秒，
    日出在 ``t = 0``、日落在 ``t = DAY_CYCLE_S / 2``，之后入夜。
    """
    cached_t, cached = _STATE_CACHE
    if cached_t == t and cached is not None:
        return cached

    day_phase = (t % DAY_CYCLE_S) / DAY_CYCLE_S
    # 日照：日出 0 -> 正午 1 -> 日落 0，夜里恒为 0
    sun = max(0.0, math.sin(2.0 * math.pi * day_phase))
    # 云遮：慢起伏，让辐照度与功率曲线不是一条光滑正弦
    cloud = 1.0 - CLOUD_DEPTH * (0.5 + 0.5 * math.sin(2.0 * math.pi * t / CLOUD_CYCLE_S + 1.1))
    irradiance = STC_IRRADIANCE * sun * cloud
    tilt_irradiance = irradiance * 1.08

    ambient = 24.0 + 4.0 * math.sin(2.0 * math.pi * t / 720.0)
    # 组件温升周期：峰值放在正午之后一点，这样正午能先跑到额定功率，
    # 随后才越 60℃ 告警、再越 70℃ 过热停机 —— 演示过程更完整
    thermal = 0.5 + 0.5 * math.cos(
        2.0 * math.pi * (t - MODULE_T_PEAK_S) / MODULE_T_CYCLE_S
    )
    module_temp = ambient + 22.0 * (irradiance / STC_IRRADIANCE) + 30.0 * thermal

    wind = 5.0 + 13.0 * (0.5 + 0.5 * math.cos(2.0 * math.pi * t / WIND_CYCLE_S))
    humidity = 48.0 + 22.0 * (0.5 + 0.5 * math.sin(2.0 * math.pi * t / 83.0))
    pressure = 1013.0 + 3.0 * math.sin(2.0 * math.pi * t / 311.0)
    # 阵雨：平时接近 0，偶尔来一场
    rainfall = 4.5 * (0.5 + 0.5 * math.sin(2.0 * math.pi * t / 431.0)) ** 3

    # ---- 直流量（只跟辐照度有关，与逆变器开停机无关）
    dc_voltage = MPPT_VOLTAGE + 4.0 * math.sin(2.0 * math.pi * t / 23.0)
    dc_power = RATED_DC_KW * (irradiance / STC_IRRADIANCE) * ARRAY_LOSS
    dc_current = dc_power * 1000.0 / dc_voltage if dc_voltage else 0.0
    efficiency = EFFICIENCY_MIN + 6.0 * min(1.0, irradiance / 600.0)

    # ---- 绝缘电阻：慢周期跌到低限以下，演示"低限"报警
    insulation = 2.4 + 1.5 * math.cos(2.0 * math.pi * t / INSULATION_CYCLE_S)

    # ---- 组串电流：8 路基本一致，4 号周期性被遮挡
    strings: list[float] = []
    for index in range(STRING_COUNT):
        spread = 1.0 + 0.025 * math.sin(2.0 * math.pi * t / 11.0 + index * 1.9)
        if index == 3:
            spread -= 0.18 * (0.5 + 0.5 * math.sin(2.0 * math.pi * t / SHADE_CYCLE_S))
        strings.append(dc_current / STRING_COUNT * spread)
    string_max = max(strings)
    string_min = min(strings)
    string_mean = sum(strings) / STRING_COUNT
    imbalance = (string_max - string_min) / string_mean * 100.0 if string_mean > 1.0 else 0.0

    # ---- 发电量：日内按正弦功率积分（1-cos），日落以后保持当日累计，次日日出清零
    day_index = int(t / DAY_CYCLE_S)
    if day_phase <= 0.5:
        energy_today = ENERGY_DAY_KWH * (1.0 - math.cos(4.0 * math.pi * day_phase)) / 2.0
    else:
        energy_today = ENERGY_DAY_KWH
    energy_total = ENERGY_TOTAL_BASE + day_index * ENERGY_DAY_KWH + energy_today
    energy_dc_today = energy_today / 0.96  # 直流侧比交流侧多约 4%

    state: dict[str, Any] = {
        "irradiance": irradiance,
        "tilt_irradiance": tilt_irradiance,
        "ambient": ambient,
        "module_temp": module_temp,
        "thermal": thermal,
        "wind": wind,
        "humidity": humidity,
        "pressure": pressure,
        "rainfall": rainfall,
        "dc_voltage": dc_voltage,
        "dc_power": dc_power,
        "dc_current": dc_current,
        "efficiency": efficiency,
        "insulation": insulation,
        "strings": strings,
        "string_max": string_max,
        "string_min": string_min,
        "string_imbalance": imbalance,
        "day_index": day_index,
        "energy_today": energy_today,
        "energy_total": energy_total,
        "energy_dc_today": energy_dc_today,
        # 各类越限判断：三台设备的字位共用一套门限，免得各写一份
        "temp_warning": module_temp >= 60.0,
        "temp_fault": module_temp >= 70.0,
        "wind_warning": wind >= 14.0,
        "insulation_warning": insulation < 2.0,
        "insulation_fault": insulation < 1.0,
        "imbalance_warning": imbalance >= 12.0,
        "imbalance_fault": imbalance >= 18.0,
    }
    _STATE_CACHE[0] = t
    _STATE_CACHE[1] = state
    return state


# --------------------------------------------------------------- 仿真：unit 32


def _command_of(regs: list[int]) -> int:
    """读控制字（只取低字节，高位留给现场扩展）。"""
    return read_u16(regs, _addr("inv_control_word")) & 0x00FF


def _apply_command(store: Any, command: int) -> None:
    """命令码按**边沿**执行一次。

    跟真实装置一样：写一次命令动作一次。否则主站把控制字留着不清，就会每拍都重新
    合/分闸 —— 手动分掉的并网开关会被命令字反复刷回去。
    """
    unit = int(getattr(store, "unit_id", 0) or 0)
    if _LAST_COMMAND.get(unit, 0) == command:
        return
    _LAST_COMMAND[unit] = command
    if command == _CTRL_START:
        store.coils[COIL_GRID_SWITCH] = True
    elif command == _CTRL_STOP:
        store.coils[COIL_GRID_SWITCH] = False


def _clamp(value: float, low: float, high: float) -> float:
    """把给定值夹到限幅区间（点表的 write_min/write_max 只管界面，仿真也要自保）。"""
    return max(low, min(high, value))


def _setpoint(regs: list[int], key: str, low: float, high: float) -> float:
    """读一个可写给定值，按 scale 换算成工程量并夹到限幅区间。"""
    return _clamp(read_u16(regs, _addr(key)) * _scale(key), low, high)


def _update_inverter(store: Any, state: dict[str, Any], t: float) -> None:
    """刷新光伏逆变器（unit 32）这一拍的全部寄存器与线圈。

    物理量由 :func:`plant_state` 给，这里只叠加"命令字 + 并网开关 + 给定值"这层逻辑。
    """
    regs = store_registers(store)
    command = _command_of(regs)
    _apply_command(store, command)
    closed = bool(store.coils[COIL_GRID_SWITCH])

    irradiance = state["irradiance"]
    dc_voltage = state["dc_voltage"]
    dc_power = state["dc_power"]
    efficiency = state["efficiency"]
    thermal = state["thermal"]
    ratio = irradiance / STC_IRRADIANCE
    night = irradiance < LOW_IRRADIANCE

    # ---------------------------------------------------------- 故障字
    fault_word = 0
    if state["insulation_fault"]:
        fault_word |= 1 << 7  # 绝缘故障
    if state["temp_fault"]:
        fault_word |= 1 << 8  # 过热

    # ---------------------------------------------------------- 功率
    # 限功率使能：控制字停在 0x08 就按「限功率设定」限功率，写 0 / 0x04 即解除
    limit_enabled = command == _CTRL_LIMIT
    power_limit = RATED_KW * (_setpoint(regs, "inv_limit_power_setpoint", 0.0, 100.0) / 100.0)
    if not limit_enabled:
        power_limit = RATED_KW * 1.2
    active_setpoint = _setpoint(regs, "inv_active_setpoint", 0.0, 550.0)
    if active_setpoint > 0.0:
        power_limit = min(power_limit, active_setpoint)

    running = bool(closed) and fault_word == 0 and not night
    ac_available = dc_power * efficiency / 100.0
    limited = bool(running and ac_available > power_limit)
    ac_power = min(ac_available, power_limit) if running else 0.0
    if not running:
        limited = False

    if night:
        # 入夜：阵列无输出，直流电压也归零
        dc_voltage_out = 0.0
        dc_current_out = 0.0
        dc_power_out = 0.0
    elif not running:
        # 白天但停机/故障：直流侧空载，电压抬到接近开路电压，电流为 0
        dc_voltage_out = dc_voltage * OPEN_CIRCUIT_FACTOR
        dc_current_out = 0.0
        dc_power_out = 0.0
    else:
        dc_voltage_out = dc_voltage
        dc_current_out = state["dc_current"]
        dc_power_out = dc_power

    # ---------------------------------------------------------- 无功与并网点
    reactive_setpoint = _setpoint(regs, "inv_reactive_setpoint", -200.0, 200.0)
    pf_setpoint = _setpoint(regs, "inv_pf_setpoint", 0.8, 1.0)
    if abs(reactive_setpoint) > 0.1:
        # 无功给定优先（留给现场按调度下发的场合，默认 0 不生效）
        reactive_power = reactive_setpoint
        pf = ac_power / math.hypot(ac_power, reactive_power) if ac_power else 1.0
    elif pf_setpoint < 1.0 and ac_power > 0.0:
        pf = pf_setpoint
        reactive_power = ac_power * math.tan(math.acos(pf))
    else:
        pf = 1.0
        reactive_power = 0.0

    grid_phase = GRID_PHASE_V + 0.8 * math.sin(2.0 * math.pi * t / 19.0)
    voltage_ab = grid_phase * math.sqrt(3.0)
    unbalance = 0.3 + 0.4 * (0.5 + 0.5 * math.sin(2.0 * math.pi * t / 29.0))
    frequency = 50.0 + 0.02 * math.sin(2.0 * math.pi * t / 17.0)
    if ac_power > 0.0 and pf > 0.0:
        current_phase = ac_power * 1000.0 / (3.0 * grid_phase * pf)
    else:
        current_phase = 0.0
    current_a = current_phase * (1.0 + 0.012 * math.sin(2.0 * math.pi * t / 13.0))
    current_b = current_phase * (1.0 + 0.012 * math.sin(2.0 * math.pi * t / 13.0 - 2.0944))
    current_c = current_phase * (1.0 + 0.012 * math.sin(2.0 * math.pi * t / 13.0 + 2.0944))

    # ---------------------------------------------------------- 温度与风扇
    if running:
        heatsink = 34.0 + 30.0 * ratio + 12.0 * thermal
        cabinet = 28.0 + 12.0 * ratio + 4.0 * thermal
    else:
        heatsink = state["ambient"] + 3.0
        cabinet = state["ambient"] + 2.0
    fan = bool(running and heatsink >= 45.0)
    store.coils[COIL_FAN] = fan

    dc_ripple = (1.2 + 2.5 * ratio + 0.8 * thermal) if running else 0.0

    # ---------------------------------------------------------- 状态字 / 告警字
    state_word = 0
    if not running:
        if night:
            state_word |= 1 << STATE_NIGHT
        elif fault_word:
            state_word |= 1 << STATE_FAULT
        else:
            state_word |= 1 << STATE_STOP
    elif limited:
        state_word |= 1 << STATE_LIMIT
    elif ac_power > 1.0:
        state_word |= 1 << STATE_RUN
    else:
        state_word |= 1 << STATE_READY
    if closed:
        state_word |= 1 << 6  # 并网
    if running:
        state_word |= 1 << 7  # MPPT 使能
    if fan:
        state_word |= 1 << 8  # 风扇运行
    if limit_enabled:
        state_word |= 1 << 9  # 降额运行
    state_word |= 1 << 10  # 定值远方
    state_word |= 1 << 11  # 通讯正常

    alarm_word = 0
    if irradiance >= 1200.0:
        alarm_word |= 1 << 0
    if state["temp_warning"]:
        alarm_word |= 1 << 1
    if heatsink >= 65.0:
        alarm_word |= 1 << 2
    if dc_ripple >= 4.0:
        alarm_word |= 1 << 3
    if unbalance >= 2.0:
        alarm_word |= 1 << 4
    if state["wind_warning"]:
        alarm_word |= 1 << 5
    if running and efficiency < 93.0:
        alarm_word |= 1 << 7  # 低效运行
    if limited:
        alarm_word |= 1 << 8  # 限功率运行
    if state["insulation_warning"]:
        alarm_word |= 1 << 9
    if state["imbalance_warning"]:
        alarm_word |= 1 << 11

    # ---------------------------------------------------------- 统计
    values: dict[str, float] = {
        "inv_dc_voltage": dc_voltage_out,
        "inv_dc_current": dc_current_out,
        "inv_dc_power": dc_power_out,
        "inv_dc_ripple": dc_ripple,
        "inv_voltage_ab": voltage_ab,
        "inv_voltage_bc": voltage_ab,
        "inv_voltage_ca": voltage_ab,
        "inv_current_a": current_a,
        "inv_current_b": current_b,
        "inv_current_c": current_c,
        "inv_active_power": ac_power,
        "inv_reactive_power": reactive_power,
        "inv_frequency": frequency,
        "inv_power_factor": pf,
        "inv_efficiency": efficiency if running else 0.0,
        "inv_voltage_unbalance": unbalance,
        "inv_heatsink_temperature": heatsink,
        "inv_cabinet_temperature": cabinet,
        "inv_state_word": float(state_word),
        "inv_fault_word": float(fault_word),
        "inv_alarm_word": float(alarm_word),
        "inv_dc_energy_today": state["energy_dc_today"],
        "inv_energy_today": state["energy_today"],
        "inv_energy_total": state["energy_total"],
        "inv_running_hours": RUNNING_HOURS_BASE + t * (24.0 / DAY_CYCLE_S),
        "inv_start_count": float(START_COUNT_BASE + state["day_index"]),
        "inv_fault_count": float(
            FAULT_COUNT_BASE + state["day_index"] + int(t / INSULATION_CYCLE_S)
        ),
        "inv_co2_reduction": state["energy_total"] * CO2_FACTOR,
    }
    _write_values(32, regs, values)
    _write_waves(
        32,
        regs,
        _inverter_waves(dc_voltage_out, dc_current_out, grid_phase, current_phase, running),
    )


def _inverter_waves(
    dc_voltage: float,
    dc_current: float,
    grid_phase: float,
    current_phase: float,
    running: bool,
) -> dict[str, list[float]]:
    """四段波形缓冲区。

    直流侧画 6 倍频（300Hz）纹波 —— 三相桥整流的特征谐波，直流母线纹波就是它；
    交流侧画 50Hz 正弦并叠 5、7 次谐波，跟现场录到的并网电流形状一致。
    200 点 × 100us = 20ms，正好一个工频周波。
    """
    step = 2.0 * math.pi / WAVE_COUNT
    voltage_peak = grid_phase * math.sqrt(2.0)
    current_peak = current_phase * math.sqrt(2.0)
    load = dc_current / 800.0
    dc_wave_voltage: list[float] = []
    dc_wave_current: list[float] = []
    ac_wave_voltage: list[float] = []
    ac_wave_current: list[float] = []
    for index in range(WAVE_COUNT):
        phase = step * index
        if running:
            dc_wave_voltage.append(
                dc_voltage
                + load * 4.0 * math.sin(6.0 * phase)
                + load * 1.5 * math.sin(12.0 * phase + 0.8)
            )
            dc_wave_current.append(
                dc_current
                * (1.0 + 0.012 * math.sin(6.0 * phase + 0.7) + 0.006 * math.sin(12.0 * phase))
            )
            ac_wave_voltage.append(voltage_peak * math.sin(phase))
            ac_wave_current.append(
                current_peak
                * (math.sin(phase) + 0.05 * math.sin(5.0 * phase) + 0.03 * math.sin(7.0 * phase))
            )
        else:
            # 停机/夜间：直流侧只剩开路电压，交流侧无波形
            dc_wave_voltage.append(dc_voltage)
            dc_wave_current.append(0.0)
            ac_wave_voltage.append(0.0)
            ac_wave_current.append(0.0)
    return {
        "wave_inv_dc_voltage": dc_wave_voltage,
        "wave_inv_dc_current": dc_wave_current,
        "wave_inv_voltage_a": ac_wave_voltage,
        "wave_inv_current_a": ac_wave_current,
    }


# --------------------------------------------------------------- 仿真：unit 33


def _combiner_values(store: Any, state: dict[str, Any], t: float) -> dict[str, float]:
    """汇流箱（unit 33）这一拍的遥测。

    只受"辐照度 + 自身直流隔离开关"控制，不看逆变器状态（见模块 docstring）。
    """
    regs = store_registers(store)
    switch_on = read_u16(regs, _addr("combiner_dc_switch")) != 0
    night = state["irradiance"] < LOW_IRRADIANCE
    live = bool(switch_on) and not night

    strings = [float(value) for value in state["strings"]] if live else [0.0] * STRING_COUNT
    total_current = sum(strings)
    string_max = max(strings)
    string_min = min(strings)
    mean = total_current / STRING_COUNT
    imbalance = (string_max - string_min) / mean * 100.0 if mean > 1.0 else 0.0

    ratio = state["irradiance"] / STC_IRRADIANCE
    voltage = (state["dc_voltage"] - 0.8) if not night else 0.0
    power = total_current * voltage / 1000.0

    if live:
        combiner_temp = (
            32.0
            + 22.0 * ratio
            + 8.0 * state["thermal"]
            + 2.0 * math.sin(2.0 * math.pi * t / 67.0)
        )
        diode_temp = 30.0 + 25.0 * ratio + 8.0 * state["thermal"]
    else:
        combiner_temp = state["ambient"] + 2.0
        diode_temp = state["ambient"] + 1.0

    combiner_state = (1 << 0) if switch_on else (1 << 1)
    combiner_state |= 1 << 2  # 熔断器正常
    combiner_state |= 1 << 3  # 防雷器正常
    combiner_state |= 1 << 4  # 柜门关闭
    combiner_state |= 1 << 5  # 通讯正常

    alarm_word = 0
    if live:
        for index, current in enumerate(strings):
            if mean > 1.0 and current < mean * 0.85:
                alarm_word |= 1 << index
        if imbalance >= 12.0:
            alarm_word |= 1 << 8
        if total_current < 50.0:
            alarm_word |= 1 << 11
    if combiner_temp >= 60.0:
        alarm_word |= 1 << 9
    if state["insulation_warning"]:
        alarm_word |= 1 << 10

    values: dict[str, float] = {}
    for index, current in enumerate(strings):
        values[f"str{index + 1}_current"] = current
    values.update(
        {
            "str_voltage": voltage,
            "str_power": power,
            "str_imbalance": imbalance,
            "str_diode_temperature": diode_temp,
            "combiner_temperature": combiner_temp,
            "str_insulation": state["insulation"],
            "str_fuse_word": 0.0,  # 本仿真无熔断器动作
            "combiner_state_word": float(combiner_state),
            "str_max_current": string_max,
            "str_min_current": string_min,
            "str_alarm_word": float(alarm_word),
            "str_online_count": float(STRING_COUNT) if live else 0.0,
            "combiner_door": 0.0,
        }
    )
    return values


# --------------------------------------------------------------- 仿真：unit 34


def _weather_values(state: dict[str, Any], t: float) -> dict[str, float]:
    """环境监测仪（unit 34）这一拍的遥测。"""
    return {
        "env_irradiance": state["irradiance"],
        "env_irradiance_tilt": state["tilt_irradiance"],
        "env_module_temperature": state["module_temp"],
        "env_ambient_temperature": state["ambient"],
        "env_module_temperature_2": state["module_temp"] - 1.2,
        "env_wind_speed": state["wind"],
        "env_wind_direction": 180.0 + 60.0 * math.sin(2.0 * math.pi * t / 211.0),
        "env_humidity": state["humidity"],
        "env_pressure": state["pressure"],
        "env_rainfall": state["rainfall"],
        "env_data_valid": 1.0,
        "env_pyranometer_status": 1.0,  # bit0 辐照传感器正常
        "env_power_supply_voltage": 24.0 + 0.3 * math.sin(2.0 * math.pi * t / 47.0),
        "env_sample_interval": 5.0,
        "env_comm_error_count": float(int(t / 600.0)),
    }


# --------------------------------------------------------------- 仿真：写入


def _write_values(unit_id: int, regs: list[int], values: dict[str, float]) -> None:
    """按 ``_REG`` 的地址/scale 把遥测写进寄存器（``rw`` 可写点跳过）。"""
    for key, value in values.items():
        unit, address, scale, kind = _REG[key]
        if unit != unit_id or kind == "rw":
            continue
        if kind == "u16":
            put_u16(regs, address, value, scale)
        elif kind == "i16":
            put_i16(regs, address, value, scale)
        elif kind == "u32":
            put_u32(regs, address, int(round(value / scale)) if scale else int(value))


def _write_waves(unit_id: int, regs: list[int], waves: dict[str, list[float]]) -> None:
    """把波形缓冲区按 scale 写进连续寄存器（int16，超量程自动夹住）。"""
    for key, samples in waves.items():
        unit, address, scale, _kind = _REG[key]
        if unit != unit_id:
            continue
        for offset, value in enumerate(samples):
            put_i16(regs, address + offset, value, scale)


def simulate(store: Any, t: float) -> None:
    """按仿真时间 ``t``（秒）刷新本从站的寄存器区。

    ``tools/modbus_sim.py`` 对 32/33/34 各调一次，按 ``store.unit_id`` 分派。
    整个写入过程都在 ``store.lock`` 里，保证上位机读到的是一致的一拍数据。
    """
    unit = int(getattr(store, "unit_id", 0) or 0)
    if unit not in UNITS:
        return
    state = plant_state(t)
    with store.lock:
        if unit == 32:
            _update_inverter(store, state, t)
        elif unit == 33:
            _write_values(33, store_registers(store), _combiner_values(store, state, t))
        else:
            _write_values(34, store_registers(store), _weather_values(state, t))
