"""内置示例工程：电力系统（配电网监控）。

一条 485/Modbus 总线上挂三台装置，模拟现场最常见的配电网监控拓扑：

    PC（主站）── Modbus TCP/RTU ──┬─ 从站 41 变电站测控装置（10kV 进线）
                                   ├─ 从站 42 馈线保护测控（馈线 1）
                                   └─ 从站 43 配电变压器监测终端 TTU（配变）

现场三台装置都在同一条 RS485 上（Modbus RTU，9600 8E1），本示例为便于无硬件
演示，通道走 TCP 指向内置仿真器（127.0.0.1:5020）的 unit 41/42/43。
上现场时把通道链路类型改成 rtu、填实际 COM 口即可，点表不用动。

**点位取值范围是通用教科书式的工程常识值，不照抄任何厂商的真实寄存器映射** ——
地址按"每台从站各自从 0 编址"的原则排布，改点表只改本文件。

--------------------------------------------------------------------------
寄存器映射（每台从站各自独立编址，范围 0~1023；32 位值占 2 个寄存器，高字在前）
--------------------------------------------------------------------------

从站 41 —— 变电站测控装置（10kV 进线）

    | 地址 | 名称             | 类型     | 说明                        |
    |------|------------------|----------|-----------------------------|
    |  0/1/2 | A/B/C 相电压   | uint16   | scale=0.01 kV（相电压）     |
    |  3/4/5 | AB/BC/CA 线电压| uint16   | scale=0.01 kV               |
    |  6/7/8 | A/B/C 相电流   | uint16   | scale=0.01 A                |
    |  9     | 有功功率 P     | uint16   | scale=0.1 kW（本示例 P≥0）   |
    | 10     | 无功功率 Q     | uint16   | scale=0.1 kvar              |
    | 11     | 视在功率 S     | uint16   | scale=0.1 kVA               |
    | 12     | 功率因数       | uint16   | scale=0.001                 |
    | 13     | 频率           | uint16   | scale=0.01 Hz               |
    | 14     | 零序电压 3U0   | uint16   | scale=0.1 V                 |
    | 15     | 母线电压不平衡度| uint16  | scale=0.01 %（周期性越限报警）|
    | 16     | 15 分钟有功需量 | uint16  | scale=0.1 kW                |
    | 17     | 断路器位置     | uint16   | bits= 位域                  |
    | 18     | 隔离开关位置   | uint16   | bits= 位域                  |
    | 19     | 保护动作字     | uint16   | bits= 位域                  |
    | 20     | 装置状态字     | uint16   | bits= 位域                  |
    | 21     | 直流母线电压   | uint16   | scale=0.1 V（220V 直流屏）  |
    | 22     | 遥控命令字     | uint16   | 可写，commands= 命令按钮    |
    | 23     | 保护定值区号   | uint16   | 可写，限幅 1~8              |
    | 24     | 速断电流定值   | uint16   | 可写，scale=0.01 A，1~200   |
    | 25     | 过流Ⅱ段时限    | uint16   | 可写，scale=0.01 s，0.1~10  |
    | input 0| 开入量状态字   | uint16   | bits= 位域                  |
    | coil 0 | 断路器出口     | bool     | 可写，出口继电器            |
    | discr 0| 断路器辅助接点 | bool     | 只读，辅助接点              |
    |100..299| 10kV 母线 A 相电压波形 | int16 | 200 点 / 100us，scale=0.01 kV |
    |300..499| 进线 A 相电流波形      | int16 | 200 点 / 100us，scale=0.01 A  |

从站 42 —— 馈线保护测控（馈线 1）

    | 地址 | 名称           | 类型   | 说明                          |
    |------|----------------|--------|-------------------------------|
    |  0/1/2 | A/B/C 相电流 | uint16 | scale=0.01 A                  |
    |  3     | 零序电流 3I0 | uint16 | scale=0.001 A                 |
    |  4     | 有功功率     | uint16 | scale=0.1 kW                  |
    |  5     | 无功功率     | uint16 | scale=0.1 kvar                |
    |  6     | 功率因数     | uint16 | scale=0.001                   |
    |  7     | 频率         | uint16 | scale=0.01 Hz                 |
    |  8     | 故障电流     | uint16 | scale=0.1 A（最近一次故障值）  |
    |  9     | 重合闸次数   | uint16 | scale=1                       |
    | 10     | 保护定值区号 | uint16 | 可写，限幅 1~8                |
    | 11     | 速断电流定值 | uint16 | 可写，scale=0.01 A，1~200     |
    | 12     | 零序电流定值 | uint16 | 可写，scale=0.001 A，0.02~5   |
    | 13     | 开关位置     | uint16 | bits= 位域                    |
    | 14     | 刀闸位置     | uint16 | bits= 位域                    |
    | 15     | 保护动作字   | uint16 | bits= 位域                    |
    | 16     | 重合闸充电时间| uint16| scale=0.1 s                   |
    | 17     | 零序电压 3U0 | uint16 | scale=0.1 V                   |
    | 18     | 电流不平衡度 | uint16 | scale=0.01 %（周期性越限报警） |
    | 19     | 负荷率       | uint16 | scale=0.1 %                   |
    | input 0| 开入量状态字 | uint16 | bits= 位域                    |
    | coil 0 | 保护跳闸出口 | bool   | 可写，置 1 模拟保护跳闸        |
    | coil 1 | 断路器合闸出口| bool  | 可写，置 1 合闸               |
    | discr 0| 断路器辅助接点| bool  | 只读                          |
    |300..499| 馈线 A 相电流波形 | int16 | 200 点 / 100us，scale=0.01 A |

从站 43 —— 配电变压器监测终端（TTU，配变 10/0.4kV）

    | 地址 | 名称             | 类型   | 说明                          |
    |------|------------------|--------|-------------------------------|
    |  0/1/2 | A/B/C 相电压   | uint16 | scale=0.1 V（低压侧相电压）    |
    |  3/4/5 | A/B/C 相电流   | uint16 | scale=0.1 A                   |
    |  6     | 有功功率       | uint16 | scale=0.01 kW                 |
    |  7     | 无功功率       | uint16 | scale=0.01 kvar               |
    |  8     | 功率因数       | uint16 | scale=0.001                   |
    |  9     | 频率           | uint16 | scale=0.01 Hz                 |
    | 10     | 油温           | int16  | scale=0.1 ℃（周期性越限报警）  |
    | 11     | 绕组温度       | int16  | scale=0.1 ℃                   |
    | 12     | 负载率         | uint16 | scale=0.1 %                   |
    | 13     | 电流不平衡度   | uint16 | scale=0.01 %                  |
    | 14     | 电压不平衡度   | uint16 | scale=0.01 %（周期性越限报警） |
    | 15/16  | 日电量         | uint32 | scale=0.1 kWh                 |
    | 17/18  | 月电量         | uint32 | scale=0.1 kWh                 |
    | 19     | 运行状态字     | uint16 | bits= 位域                    |
    | 20     | 告警字         | uint16 | bits= 位域                    |
    | 21     | 电容器投切档位 | uint16 | 可写，限幅 0~5                |
    | 22     | 配变容量       | uint16 | scale=1 kVA（仅展示）          |
    | 23     | 电流谐波总畸变率| uint16| scale=0.01 %                  |
    | 24     | 油温告警定值   | uint16 | 可写，scale=0.1 ℃，60~110     |
    | input 0| 开入量状态字   | uint16 | bits= 位域                    |
    | coil 0 | 电容器组投入   | bool   | 可写                          |
    | discr 0| 柜门状态       | bool   | 只读                          |

波形点位一律 `count=200, interval_us=100`（200 × 100us = 20ms = 一个 50Hz 工频周期），
按 :func:`_write_wave` 用正弦生成 —— 标量轮询画不出 50Hz 正弦，必须整块读回。
"""

from __future__ import annotations

import math
from typing import Any

from ..models import ChannelDef, DeviceDef, Project, ProjectStorage, default_channel_options
from ._common import alarm, diagram, link, node, scalar, wave_point
from ._sim import put_i16, put_u16, put_u32, read_u16, store_registers

NAME = "电力系统（配电网）"
FOLDER_NAME = "power-grid"
UNITS = (41, 42, 43)
POLL_INTERVAL_MS = 500

#: 通道名 / 三台设备名（示意图的 action、value_point 按这个名字指过来）
CHANNEL = "配网监控总线"
SUBSTATION = "变电站测控装置"
FEEDER = "馈线保护测控"
TTU = "配电变压器TTU"

# --------------------------------------------------------------- 位定义

#: 41 断路器位置
_QF_BITS = "0:合位,1:分位,2:储能已好,3:就地,4:远方,5:弹簧未储能"
#: 41 隔离开关位置
_QS_BITS = "0:1母刀闸合,1:1母刀闸分,2:2母刀闸合,3:2母刀闸分,4:接地刀闸合"
#: 41/42 保护动作字
_PROTECT_BITS = (
    "0:速断,1:过流Ⅰ段,2:过流Ⅱ段,3:零序过流,4:过负荷,5:低电压,6:过电压,"
    "7:重合闸动作,8:TV断线,9:控制回路断线,10:装置告警,11:装置异常"
)
#: 41 装置状态字
_DEVICE_STATE_BITS = "0:运行,1:告警,2:闭锁,3:检修,4:装置故障"
#: 41 开入量状态字
_DIN_BITS_41 = "0:断路器合位开入,1:断路器分位开入,2:远方位置,3:弹簧未储能,4:SF6气压低,5:装置告警,6:对时正常"
#: 41 遥控命令（写命令码，不是设值：点一下按钮就写对应值下去）
_CTRL_COMMANDS = "0x01:合闸,0x02:分闸,0x04:复归"

#: 42 开关位置
_SW_BITS = "0:合位,1:分位,2:未储能,3:就地,4:远方,5:故障跳闸"
#: 42 刀闸位置
_SW_QS_BITS = "0:甲刀闸合,1:甲刀闸分,2:乙刀闸合,3:乙刀闸分"
#: 42 保护动作字（与 41 不同：馈线保护的动作类型）
_FEEDER_PROTECT_BITS = (
    "0:过流Ⅰ段,1:过流Ⅱ段,2:过流Ⅲ段,3:零序过流,4:重合闸动作,5:加速跳闸,"
    "6:低周减载,7:小电流接地选线,8:跳位异常,9:控制回路断线"
)
#: 42 开入量状态字
_DIN_BITS_42 = "0:断路器合位开入,1:断路器分位开入,2:远方位置,3:重合闸充电完成,4:重合闸闭锁,5:控制回路断线,6:装置告警"

#: 43 运行状态字
_TTU_STATE_BITS = "0:运行,1:轻载,2:过载,3:油温高,4:缺相,5:停电,6:柜门开,7:通信正常"
#: 43 告警字
_TTU_ALARM_BITS = "0:过载,1:油温高,2:油温过高,3:电流不平衡,4:电压不平衡,5:缺相,6:装置故障,7:停电"
#: 43 开入量状态字
_DIN_BITS_43 = "0:运行,1:柜门开,2:轻载,3:过载,4:停电,5:缺相"

# --------------------------------------------------------------- 仿真参数

#: 波形缓冲区的换算系数，必须与 wave_point() 里填的 scale 一致
_WAVE_V_SCALE = 0.01      # 41 母线电压波形：0.01 kV/LSB
_WAVE_I_SCALE = 0.01      # 41/42 电流波形：0.01 A/LSB

#: 波形缓冲区地址（41 与 42 各自独立编址，互不影响）
_WAVE_ADDR_41_U = 100
_WAVE_ADDR_41_I = 300
_WAVE_ADDR_42_I = 300


def _ensure_u16(regs: list[int], address: int, scale: float, default: float, minimum: float) -> None:
    """可写定值"主站还没写过"时填一次默认值。

    判据：寄存器还是 0，或换算后的工程量小于写值下限（说明这份值不是主站写的）。
    """
    raw = read_u16(regs, address)
    if raw == 0 or raw * scale < minimum:
        put_u16(regs, address, default, scale)


def _write_wave(
    regs: list[int],
    base: int,
    count: int,
    samples_per_cycle: int,
    phase: float,
    amplitude: float,
    dc: float = 0.0,
    ripple: float = 0.0,
) -> None:
    """往保持寄存器区写一段正弦波形（原始值，有符号 int16）。

    ``amplitude`` / ``dc`` / ``ripple`` 都按**原始值**给，调用方自己除以 scale。
    """
    for index in range(count):
        angle = 2.0 * math.pi * index / samples_per_cycle + phase
        value = amplitude * math.sin(angle) + dc
        if ripple:
            value += ripple * math.sin(2.0 * math.pi * index / 7.0)   # 7 次纹波，纯装饰
        raw = int(max(-32768, min(32767, round(value))))
        regs[base + index] = raw & 0xFFFF


def _average(values: list[float]) -> float:
    return sum(values) / len(values)


# --------------------------------------------------------------- 从站 41：变电站测控装置


def _substation_device() -> DeviceDef:
    """10kV 进线变电站测控装置（从站 41）。"""
    device = DeviceDef(
        name=SUBSTATION,
        channel=CHANNEL,
        unit_id=41,
        poll_interval_ms=POLL_INTERVAL_MS,
        description="10kV 进线测控装置：三相电压电流、功率、开关位置、保护动作字、遥控与定值",
    )
    device.points = [
        # ---------------- 母线（10kV 母线电压）
        scalar(
            "10kV母线A相电压", "10kV 母线 A 相电压", 0, unit="kV", scale=0.01, decimals=2,
            group="母线", display="gauge", gauge_min=5.0, gauge_max=6.5, archive=True,
            description="相电压，0.01 kV/LSB；一次侧线电压约为 10kV",
            alarm_def=alarm("critical", hi_hi=6.6, hi=6.3, lo=5.1, lo_lo=4.8),
        ),
        scalar("10kV母线B相电压", "10kV 母线 B 相电压", 1, unit="kV", scale=0.01, decimals=2,
               group="母线", display="card"),
        scalar("10kV母线C相电压", "10kV 母线 C 相电压", 2, unit="kV", scale=0.01, decimals=2,
               group="母线", display="card"),
        scalar("母线AB线电压", "母线 AB 线电压", 3, unit="kV", scale=0.01, decimals=2,
               group="母线", display="card"),
        scalar("母线BC线电压", "母线 BC 线电压", 4, unit="kV", scale=0.01, decimals=2,
               group="母线", display="card"),
        scalar("母线CA线电压", "母线 CA 线电压", 5, unit="kV", scale=0.01, decimals=2,
               group="母线", display="card"),
        scalar(
            "母线电压不平衡度", "母线电压不平衡度", 15, unit="%", scale=0.01, decimals=2,
            group="母线", display="gauge", gauge_min=0.0, gauge_max=5.0,
            description="(最大相 - 最小相) / 平均相 × 100%",
            alarm_def=alarm("warning", hi_hi=4.0, hi=2.0, deadband=0.1,
                            message="母线电压不平衡度超限，检查三相负荷与 TV 回路"),
        ),
        scalar("零序电压3U0", "零序电压 3U0", 14, unit="V", scale=0.1, decimals=1,
               group="母线", display="card",
               alarm_def=alarm("warning", hi=5.0, hi_hi=15.0)),
        # ---------------- 进线（功率与电流）
        scalar("进线A相电流", "进线 A 相电流", 6, unit="A", scale=0.01, decimals=2,
               group="进线", display="gauge", gauge_min=0.0, gauge_max=300.0,
               alarm_def=alarm("critical", hi_hi=260, hi=230)),
        scalar("进线B相电流", "进线 B 相电流", 7, unit="A", scale=0.01, decimals=2,
               group="进线", display="card"),
        scalar("进线C相电流", "进线 C 相电流", 8, unit="A", scale=0.01, decimals=2,
               group="进线", display="card"),
        scalar("进线有功功率", "进线有功功率", 9, unit="kW", scale=0.1, decimals=1,
               data_type="uint16", group="进线", display="card",
               alarm_def=alarm("warning", hi=3000)),
        scalar("进线无功功率", "进线无功功率", 10, unit="kvar", scale=0.1, decimals=1,
               data_type="uint16", group="进线", display="card"),
        scalar("进线视在功率", "进线视在功率", 11, unit="kVA", scale=0.1, decimals=1,
               group="进线", display="card"),
        scalar("进线功率因数", "进线功率因数", 12, unit="", scale=0.001, decimals=3,
               group="进线", display="card",
               alarm_def=alarm("warning", lo=0.90)),
        scalar("系统频率", "系统频率", 13, unit="Hz", scale=0.01, decimals=2,
               group="进线", display="card",
               alarm_def=alarm("critical", hi_hi=50.5, hi=50.2, lo=49.8, lo_lo=49.5)),
        scalar("有功需量", "15 分钟有功需量", 16, unit="kW", scale=0.1, decimals=1,
               group="进线", display="card",
               description="滑差式需量，用于最大需量考核",
               alarm_def=alarm("warning", hi=3200)),
        scalar("直流母线电压", "直流屏母线电压", 21, unit="V", scale=0.1, decimals=1,
               group="进线", display="card",
               alarm_def=alarm("warning", hi_hi=264, hi=242, lo=198, lo_lo=187)),
        # ---------------- 状态与保护
        scalar("断路器位置", "断路器位置", 17, data_type="uint16", decimals=0, bits=_QF_BITS,
               group="状态与保护", display="card", archive=False),
        scalar("隔离开关位置", "隔离开关位置", 18, data_type="uint16", decimals=0, bits=_QS_BITS,
               group="状态与保护", display="card", archive=False),
        scalar("保护动作字", "保护动作字", 19, data_type="uint16", decimals=0, bits=_PROTECT_BITS,
               group="状态与保护", display="card", archive=False),
        scalar("装置状态字", "装置状态字", 20, data_type="uint16", decimals=0,
               bits=_DEVICE_STATE_BITS, group="状态与保护", display="card", archive=False),
        scalar("开入量状态字", "开入量状态字", 0, data_type="uint16", decimals=0,
               register="input", bits=_DIN_BITS_41, group="状态与保护", display="card",
               archive=False, description="只读开入量（input 区地址 0）"),
        scalar("断路器出口", "断路器出口", 0, data_type="bool", register="coil", writable=True,
               group="状态与保护", display="lamp", archive=False,
               description="出口继电器；仿真里跟随断路器位置（coil 区地址 0）"),
        scalar("断路器辅助接点", "断路器辅助接点", 0, data_type="bool", register="discrete",
               group="状态与保护", display="lamp", archive=False,
               description="只读辅助接点（discrete 区地址 0）"),
        scalar("遥控命令字", "遥控命令字", 22, data_type="uint16", decimals=0, writable=True,
               group="状态与保护", display="card", archive=False, bits=_QF_BITS,
               commands=_CTRL_COMMANDS,
               description="写命令码：0x01 合闸 / 0x02 分闸 / 0x04 复归（点按钮即写值）"),
        scalar("保护定值区号", "保护定值区号", 23, data_type="uint16", decimals=0, writable=True,
               group="状态与保护", display="card", archive=False,
               write_min=1, write_max=8, description="可写，限幅 1~8"),
        scalar("速断电流定值", "速断电流定值", 24, unit="A", scale=0.01, decimals=2,
               writable=True, group="状态与保护", display="card", archive=False,
               write_min=1.0, write_max=200.0,
               description="可写定值，限幅 1~200 A（演示用通用值）"),
        scalar("过流Ⅱ段时限", "过流Ⅱ段时限", 25, unit="s", scale=0.01, decimals=2,
               writable=True, group="状态与保护", display="card", archive=False,
               write_min=0.1, write_max=10.0, description="可写定值，限幅 0.1~10 s"),
        # ---------------- 波形
        wave_point("母线A相电压波形", "10kV 母线 A 相电压波形", _WAVE_ADDR_41_U, 200,
                   "kV", _WAVE_V_SCALE, 100, "#3d8bfd", group="波形"),
        wave_point("进线A相电流波形", "进线 A 相电流波形", _WAVE_ADDR_41_I, 200,
                   "A", _WAVE_I_SCALE, 100, "#2f9e6b", group="波形"),
    ]
    return device


def _simulate_unit41(store: Any, t: float) -> None:
    """变电站测控装置（从站 41）：母线电压、进线功率、开关位置与保护动作字。"""
    regs = store_registers(store)
    with store.lock:
        # 电压不平衡度 1.2%~2.5% 缓慢摆动，会周期性越过 2% 告警线（演示用）
        unbalance = 1.2 + 1.3 * abs(math.sin(t / 97.0))
        base = 5.77 + 0.06 * math.sin(t / 47.0)          # 相电压 ≈ 5.77kV
        spread = base * unbalance / 100.0
        ua = base + spread * 0.5
        ub = base - spread * 0.3
        uc = base - spread * 0.2
        uab = ua * math.sqrt(3.0)
        ubc = ub * math.sqrt(3.0)
        uca = uc * math.sqrt(3.0)

        # 遥控命令字：0x01 合闸 / 0x02 分闸；没写过就保持上一轮的位置（首轮默认合位）
        command = read_u16(regs, 22) & 0x00FF
        previous = read_u16(regs, 17)
        closed = bool(previous & 0x0001) if previous else True
        if command == 0x01:
            closed = True
        elif command == 0x02:
            closed = False

        factor = 1.0 if closed else 0.0
        ia = (180.0 + 26.0 * math.sin(t / 13.0)) * factor
        ib = (176.0 + 24.0 * math.sin(t / 13.0 - 2.0943951)) * factor
        ic = (184.0 + 25.0 * math.sin(t / 13.0 + 2.0943951)) * factor
        power_factor = 0.955 + 0.02 * math.sin(t / 31.0)
        line_voltage = _average([uab, ubc, uca]) * factor
        current = _average([ia, ib, ic])
        p = math.sqrt(3.0) * line_voltage * current * power_factor
        q = p * math.tan(math.acos(min(0.999, max(0.0, power_factor))))
        s = math.sqrt(p * p + q * q)
        freq = 50.0 + 0.05 * math.sin(t / 23.0)
        zero_seq = 0.4 + 1.2 * max(0.0, math.sin(t / 97.0))         # 3U0，与不平衡同源
        demand = 2600.0 + 220.0 * math.sin(t / 211.0)
        dc_bus = 220.0 + 3.0 * math.sin(t / 37.0)

        # 保护动作字与状态字由测量值推导（真实装置里是保护逻辑判出来的）
        protect = 0
        if p > 2700.0:
            protect |= 1 << 4                                        # 过负荷
        if unbalance > 2.0:
            protect |= 1 << 10                                       # 装置告警
        device_state = 0x0001                                        # 运行
        if unbalance > 2.0:
            device_state |= 0x0002                                   # 告警
        if dc_bus < 198.0:
            device_state |= 0x0004                                   # 闭锁

        breaker = 0x0010 | 0x0004                                    # 远方 + 储能已好
        breaker |= 0x0001 if closed else 0x0002
        isolator = (0x0001 | 0x0004) if closed else (0x0002 | 0x0008)

        digital_in = 0x0004 | 0x0040                                 # 远方位置 + 对时正常
        digital_in |= 0x0001 if closed else 0x0002
        if device_state & 0x0002:
            digital_in |= 0x0020                                     # 装置告警

        put_u16(regs, 0, ua, 0.01)
        put_u16(regs, 1, ub, 0.01)
        put_u16(regs, 2, uc, 0.01)
        put_u16(regs, 3, uab, 0.01)
        put_u16(regs, 4, ubc, 0.01)
        put_u16(regs, 5, uca, 0.01)
        put_u16(regs, 6, ia, 0.01)
        put_u16(regs, 7, ib, 0.01)
        put_u16(regs, 8, ic, 0.01)
        put_u16(regs, 9, p, 0.1)
        put_u16(regs, 10, q, 0.1)
        put_u16(regs, 11, s, 0.1)
        put_u16(regs, 12, power_factor, 0.001)
        put_u16(regs, 13, freq, 0.01)
        put_u16(regs, 14, zero_seq, 0.1)
        put_u16(regs, 15, unbalance, 0.01)
        put_u16(regs, 16, demand, 0.1)
        put_u16(regs, 17, breaker)
        put_u16(regs, 18, isolator)
        put_u16(regs, 19, protect)
        put_u16(regs, 20, device_state)
        put_u16(regs, 21, dc_bus, 0.1)
        regs_input = getattr(store, "input", None)
        if regs_input is not None:
            regs_input[0] = digital_in
        coils = getattr(store, "coils", None)
        if coils is not None:
            coils[0] = closed
        discretes = getattr(store, "discretes", None)
        if discretes is not None:
            discretes[0] = closed

        # 可写定值的默认值（主站写过之后就不再覆盖）
        _ensure_u16(regs, 23, 1.0, 1.0, 1.0)
        _ensure_u16(regs, 24, 0.01, 25.0, 1.0)
        _ensure_u16(regs, 25, 0.01, 0.50, 0.1)

        if closed:
            # 200 点 / 100us = 20ms = 一个工频周期；峰值 = 有效值 × √2
            phase_drift = 2.0 * math.pi * ((t * 0.02) % 1.0)
            _write_wave(regs, _WAVE_ADDR_41_U, 200, 200, phase_drift,
                        math.sqrt(2.0) * ua / _WAVE_V_SCALE, ripple=10.0)
            _write_wave(regs, _WAVE_ADDR_41_I, 200, 200, phase_drift,
                        math.sqrt(2.0) * ia / _WAVE_I_SCALE, ripple=200.0)


# --------------------------------------------------------------- 从站 42：馈线保护测控


def _feeder_device() -> DeviceDef:
    """馈线 1 保护测控装置（从站 42）。"""
    device = DeviceDef(
        name=FEEDER,
        channel=CHANNEL,
        unit_id=42,
        poll_interval_ms=POLL_INTERVAL_MS,
        description="10kV 馈线 1 保护测控：三相电流、零序电流、功率、开关位置与保护动作字",
    )
    device.points = [
        scalar("馈线A相电流", "馈线 A 相电流", 0, unit="A", scale=0.01, decimals=2,
               group="馈线", display="gauge", gauge_min=0.0, gauge_max=250.0,
               alarm_def=alarm("critical", hi_hi=220, hi=200)),
        scalar("馈线B相电流", "馈线 B 相电流", 1, unit="A", scale=0.01, decimals=2,
               group="馈线", display="card"),
        scalar("馈线C相电流", "馈线 C 相电流", 2, unit="A", scale=0.01, decimals=2,
               group="馈线", display="card"),
        scalar("零序电流3I0", "零序电流 3I0", 3, unit="A", scale=0.001, decimals=3,
               group="馈线", display="card",
               description="小电流接地系统零序电流，0.001 A/LSB",
               alarm_def=alarm("warning", hi=0.05, hi_hi=0.5,
                               message="零序电流超限，可能发生单相接地")),
        scalar("馈线有功功率", "馈线有功功率", 4, unit="kW", scale=0.1, decimals=1,
               data_type="uint16", group="馈线", display="card",
               alarm_def=alarm("warning", hi=3200)),
        scalar("馈线无功功率", "馈线无功功率", 5, unit="kvar", scale=0.1, decimals=1,
               data_type="uint16", group="馈线", display="card"),
        scalar("馈线功率因数", "馈线功率因数", 6, unit="", scale=0.001, decimals=3,
               group="馈线", display="card", alarm_def=alarm("warning", lo=0.90)),
        scalar("馈线频率", "馈线频率", 7, unit="Hz", scale=0.01, decimals=2,
               group="馈线", display="card"),
        scalar("故障电流", "最近一次故障电流", 8, unit="A", scale=0.1, decimals=1,
               group="馈线", display="card", description="保护装置记录的最近一次故障电流"),
        scalar("重合闸次数", "重合闸次数", 9, data_type="uint16", decimals=0,
               group="馈线", display="card", description="累计重合闸动作次数"),
        scalar("电流不平衡度", "电流不平衡度", 18, unit="%", scale=0.01, decimals=2,
               group="馈线", display="gauge", gauge_min=0.0, gauge_max=10.0,
               alarm_def=alarm("warning", hi_hi=8.0, hi=4.0, deadband=0.1),
               description="(最大相 - 最小相) / 平均相 × 100%"),
        scalar("负荷率", "馈线负荷率", 19, unit="%", scale=0.1, decimals=1,
               group="馈线", display="gauge", gauge_min=0.0, gauge_max=120.0,
               alarm_def=alarm("warning", hi_hi=110, hi=90)),
        scalar("重合闸充电时间", "重合闸充电时间", 16, unit="s", scale=0.1, decimals=1,
               group="馈线", display="card", description="重合闸充电完成时间"),
        scalar("馈线零序电压3U0", "零序电压 3U0", 17, unit="V", scale=0.1, decimals=1,
               group="馈线", display="card"),
        scalar("开关位置", "开关位置", 13, data_type="uint16", decimals=0, bits=_SW_BITS,
               group="状态与保护", display="card", archive=False),
        scalar("刀闸位置", "刀闸位置", 14, data_type="uint16", decimals=0, bits=_SW_QS_BITS,
               group="状态与保护", display="card", archive=False),
        scalar("保护动作字", "保护动作字", 15, data_type="uint16", decimals=0,
               bits=_FEEDER_PROTECT_BITS, group="状态与保护", display="card", archive=False),
        scalar("馈线开入量状态字", "开入量状态字", 0, data_type="uint16", decimals=0,
               register="input", bits=_DIN_BITS_42, group="状态与保护", display="card",
               archive=False, description="只读开入量（input 区地址 0）"),
        scalar("保护跳闸出口", "保护跳闸出口", 0, data_type="bool", register="coil",
               writable=True, group="状态与保护", display="lamp", archive=False,
               description="置 1 模拟保护跳闸（coil 区地址 0）"),
        scalar("断路器合闸出口", "断路器合闸出口", 1, data_type="bool", register="coil",
               writable=True, group="状态与保护", display="lamp", archive=False,
               description="置 1 合闸（coil 区地址 1）"),
        scalar("馈线断路器辅助接点", "断路器辅助接点", 0, data_type="bool",
               register="discrete", group="状态与保护", display="lamp", archive=False,
               description="只读辅助接点（discrete 区地址 0）"),
        scalar("馈线保护定值区号", "保护定值区号", 10, data_type="uint16", decimals=0,
               writable=True, group="状态与保护", display="card", archive=False,
               write_min=1, write_max=8, description="可写，限幅 1~8"),
        scalar("馈线速断电流定值", "速断电流定值", 11, unit="A", scale=0.01, decimals=2,
               writable=True, group="状态与保护", display="card", archive=False,
               write_min=1.0, write_max=200.0, description="可写定值，限幅 1~200 A"),
        scalar("零序电流定值", "零序电流定值", 12, unit="A", scale=0.001, decimals=3,
               writable=True, group="状态与保护", display="card", archive=False,
               write_min=0.02, write_max=5.0, description="可写定值，限幅 0.02~5 A"),
        wave_point("馈线A相电流波形", "馈线 A 相电流波形", _WAVE_ADDR_42_I, 200,
                   "A", _WAVE_I_SCALE, 100, "#c9a227", group="波形"),
    ]
    return device


def _simulate_unit42(store: Any, t: float) -> None:
    """馈线保护测控（从站 42）：三相电流、零序电流、保护动作字。

    两个线圈就是"遥控出口"：``coils[0]`` 置 1 模拟保护跳闸，``coils[1]`` 置 1 合闸；
    两个都是 0 时保持上一轮的位置（首轮默认合位），这样即使上位机不写也有电可看。
    """
    regs = store_registers(store)
    with store.lock:
        previous = read_u16(regs, 13)
        closed = bool(previous & 0x0001) if previous else True
        coils = getattr(store, "coils", None)
        trip = bool(coils[0]) if coils is not None else False
        close = bool(coils[1]) if coils is not None else False
        if trip:
            closed = False
        elif close:
            closed = True

        factor = 1.0 if closed else 0.0
        unbalance = 1.5 + 3.0 * max(0.0, math.sin(t / 83.0))      # 1.5%~4.5%，周期性越 4% 告警线
        ia = (150.0 + 22.0 * math.sin(t / 11.0)) * factor
        ib = (145.0 + 20.0 * math.sin(t / 11.0 - 2.0943951)) * factor
        ic = (152.0 + 21.0 * math.sin(t / 11.0 + 2.0943951)) * factor
        current = _average([ia, ib, ic])
        zero_seq = (0.012 + 0.05 * max(0.0, math.sin(t / 113.0))) * factor
        power_factor = 0.945 + 0.025 * math.sin(t / 37.0)
        p = math.sqrt(3.0) * 10.0 * current * power_factor * factor
        q = p * math.tan(math.acos(min(0.999, max(0.0, power_factor))))
        freq = 50.0 + 0.06 * math.sin(t / 19.0)
        load_rate = (current / 200.0 * 100.0) if closed else 0.0
        fault_current = 3200.0 * factor                           # 装置记录的最近一次故障电流
        reclosures = int(t / 300.0)                               # 累计重合闸次数

        protect = 0
        if max(ia, ib, ic) > 168.0:
            protect |= 1 << 1                                     # 过流Ⅱ段
        if zero_seq > 0.05:
            protect |= 1 << 3                                     # 零序过流
        if not closed and (coils[0] if coils is not None else False):
            protect |= 1 << 0                                     # 速断（跳闸出口动作）

        switch = 0x0010 | 0x0004                                  # 远方 + 已储能
        switch |= 0x0001 if closed else 0x0002
        if not closed and protect:
            switch |= 0x0020                                      # 故障跳闸
        disconnector = (0x0001 | 0x0004) if closed else (0x0002 | 0x0008)

        digital_in = 0x0004 | 0x0008                              # 远方位置 + 重合闸充电完成
        digital_in |= 0x0001 if closed else 0x0002

        put_u16(regs, 0, ia, 0.01)
        put_u16(regs, 1, ib, 0.01)
        put_u16(regs, 2, ic, 0.01)
        put_u16(regs, 3, zero_seq, 0.001)
        put_u16(regs, 4, p, 0.1)
        put_u16(regs, 5, q, 0.1)
        put_u16(regs, 6, power_factor, 0.001)
        put_u16(regs, 7, freq, 0.01)
        put_u16(regs, 8, fault_current, 0.1)
        put_u16(regs, 9, reclosures)
        put_u16(regs, 13, switch)
        put_u16(regs, 14, disconnector)
        put_u16(regs, 15, protect)
        put_u16(regs, 16, 20.0, 0.1)                              # 重合闸充电时间
        put_u16(regs, 17, 0.6 + 1.5 * max(0.0, math.sin(t / 113.0)), 0.1)
        put_u16(regs, 18, unbalance, 0.01)
        put_u16(regs, 19, load_rate, 0.1)
        regs_input = getattr(store, "input", None)
        if regs_input is not None:
            regs_input[0] = digital_in
        discretes = getattr(store, "discretes", None)
        if discretes is not None:
            discretes[0] = closed

        _ensure_u16(regs, 10, 1.0, 1.0, 1.0)
        _ensure_u16(regs, 11, 0.01, 60.0, 1.0)
        _ensure_u16(regs, 12, 0.001, 0.10, 0.02)

        if closed:
            phase_drift = 2.0 * math.pi * ((t * 0.02) % 1.0)
            _write_wave(regs, _WAVE_ADDR_42_I, 200, 200, phase_drift,
                        math.sqrt(2.0) * ia / _WAVE_I_SCALE, ripple=180.0)


# --------------------------------------------------------------- 从站 43：配变监测终端 TTU


def _ttu_device() -> DeviceDef:
    """配电变压器监测终端（从站 43）。"""
    device = DeviceDef(
        name=TTU,
        channel=CHANNEL,
        unit_id=43,
        poll_interval_ms=POLL_INTERVAL_MS,
        description="配变监测终端：低压侧电压电流、功率因数、油温/绕组温度、负载率与电量",
    )
    device.points = [
        scalar("低压侧A相电压", "低压侧 A 相电压", 0, unit="V", scale=0.1, decimals=1,
               group="配变", display="gauge", gauge_min=180.0, gauge_max=260.0,
               alarm_def=alarm("critical", hi_hi=253, hi=242, lo=198, lo_lo=187)),
        scalar("低压侧B相电压", "低压侧 B 相电压", 1, unit="V", scale=0.1, decimals=1,
               group="配变", display="card"),
        scalar("低压侧C相电压", "低压侧 C 相电压", 2, unit="V", scale=0.1, decimals=1,
               group="配变", display="card"),
        scalar("低压侧A相电流", "低压侧 A 相电流", 3, unit="A", scale=0.1, decimals=1,
               group="配变", display="gauge", gauge_min=0.0, gauge_max=700.0,
               alarm_def=alarm("critical", hi_hi=620, hi=560)),
        scalar("低压侧B相电流", "低压侧 B 相电流", 4, unit="A", scale=0.1, decimals=1,
               group="配变", display="card"),
        scalar("低压侧C相电流", "低压侧 C 相电流", 5, unit="A", scale=0.1, decimals=1,
               group="配变", display="card"),
        scalar("配变有功功率", "配变有功功率", 6, unit="kW", scale=0.01, decimals=2,
               group="配变", display="card",
               alarm_def=alarm("warning", hi=340)),
        scalar("配变无功功率", "配变无功功率", 7, unit="kvar", scale=0.01, decimals=2,
               group="配变", display="card"),
        scalar("配变功率因数", "配变功率因数", 8, unit="", scale=0.001, decimals=3,
               group="配变", display="card", alarm_def=alarm("warning", lo=0.85)),
        scalar("配变频率", "配变频率", 9, unit="Hz", scale=0.01, decimals=2,
               group="配变", display="card",
               alarm_def=alarm("warning", hi=50.2, lo=49.8)),
        scalar("油温", "顶层油温", 10, unit="℃", scale=0.1, decimals=1, data_type="int16",
               group="配变", display="gauge", gauge_min=0.0, gauge_max=120.0,
               description="周期性缓慢漂移，演示时能看到油温高报警",
               alarm_def=alarm("critical", hi_hi=85, hi=75, deadband=0.5,
                               message="配变油温过高，检查负载与散热")),
        scalar("绕组温度", "绕组温度", 11, unit="℃", scale=0.1, decimals=1, data_type="int16",
               group="配变", display="gauge", gauge_min=0.0, gauge_max=130.0,
               alarm_def=alarm("critical", hi_hi=105, hi=95)),
        scalar("负载率", "负载率", 12, unit="%", scale=0.1, decimals=1,
               group="配变", display="gauge", gauge_min=0.0, gauge_max=120.0,
               alarm_def=alarm("warning", hi_hi=105, hi=85)),
        scalar("电流不平衡度", "三相电流不平衡度", 13, unit="%", scale=0.01, decimals=2,
               group="配变", display="card",
               alarm_def=alarm("warning", hi_hi=40, hi=20, deadband=0.5)),
        scalar("电压不平衡度", "三相电压不平衡度", 14, unit="%", scale=0.01, decimals=2,
               group="配变", display="card",
               alarm_def=alarm("warning", hi_hi=4.0, hi=2.0, deadband=0.1)),
        scalar("电流谐波总畸变率", "电流谐波总畸变率 THDi", 23, unit="%", scale=0.01,
               decimals=2, group="配变", display="card",
               alarm_def=alarm("warning", hi_hi=10.0, hi=5.0)),
        scalar("配变容量", "配变容量", 22, unit="kVA", decimals=0, group="配变",
               display="card", description="10/0.4kV 配变额定容量，仅作展示"),
        scalar("电容器投切档位", "电容器投切档位", 21, data_type="uint16", decimals=0,
               writable=True, group="配变", display="card", archive=False,
               codes="0:全部切除,1:投入1组,2:投入2组,3:投入3组,4:投入4组,5:投入5组",
               write_min=0, write_max=5, description="可写，限幅 0~5（无功补偿档位）"),
        scalar("油温告警定值", "油温告警定值", 24, unit="℃", scale=0.1, decimals=1,
               writable=True, group="配变", display="card", archive=False,
               write_min=60.0, write_max=110.0, description="可写定值，限幅 60~110 ℃"),
        scalar("日电量", "日电量", 15, unit="kWh", scale=0.1, decimals=1,
               data_type="uint32", group="电量", display="card", archive=False,
               description="32 位累计值，占 2 个寄存器"),
        scalar("月电量", "月电量", 17, unit="kWh", scale=0.1, decimals=1,
               data_type="uint32", group="电量", display="card", archive=False,
               description="32 位累计值，占 2 个寄存器"),
        scalar("运行状态字", "运行状态字", 19, data_type="uint16", decimals=0,
               bits=_TTU_STATE_BITS, group="状态与保护", display="card", archive=False),
        scalar("告警字", "告警字", 20, data_type="uint16", decimals=0, bits=_TTU_ALARM_BITS,
               group="状态与保护", display="card", archive=False),
        scalar("TTU开入量状态字", "开入量状态字", 0, data_type="uint16", decimals=0,
               register="input", bits=_DIN_BITS_43, group="状态与保护", display="card",
               archive=False, description="只读开入量（input 区地址 0）"),
        scalar("电容器组投入", "电容器组投入", 0, data_type="bool", register="coil",
               writable=True, group="状态与保护", display="lamp", archive=False,
               description="置 1 投入无功补偿电容（coil 区地址 0）"),
        scalar("柜门状态", "柜门状态", 0, data_type="bool", register="discrete",
               group="状态与保护", display="lamp", archive=False,
               description="只读，柜门打开时为 ON（discrete 区地址 0）"),
    ]
    return device


def _simulate_unit43(store: Any, t: float) -> None:
    """配变监测终端（从站 43）：低压侧电参量、温度、负载率与电量。

    油温按 68 ± 20℃ 缓慢漂移（48~88℃），会周期性越过 75℃/85℃ 报警线；
    每 5 分钟里有 15 秒模拟一次短时停电（电压为 0、状态字报停电）。
    """
    regs = store_registers(store)
    with store.lock:
        # 无功补偿：档位寄存器 + 电容组投入线圈共同决定补偿量
        step = max(0, min(5, read_u16(regs, 21)))
        coils = getattr(store, "coils", None)
        if coils is not None and not coils[0]:
            step = 0

        outage = (int(t) % 300) >= 285
        door_open = (int(t) % 97) < 6

        unbalance = 0.5 + 2.5 * max(0.0, math.sin(t / 71.0))      # 0.5%~3.0%，周期性越 2% 告警线
        base = 230.0 + 2.5 * math.sin(t / 43.0)
        spread = base * unbalance / 100.0
        ua = base + spread * 0.5
        ub = base - spread * 0.3
        uc = base - spread * 0.2
        if outage:
            ua = ub = uc = 0.0

        load = 0.0 if outage else 1.0
        current_unbalance = 2.0 + 12.0 * max(0.0, math.sin(t / 61.0))   # 2%~14%
        ia = (330.0 + 18.0 * (1.0 + current_unbalance / 100.0)) * load
        ib = (328.0 - 6.0 * (1.0 - current_unbalance / 100.0)) * load
        ic = (332.0 - 12.0 * (1.0 - current_unbalance / 100.0)) * load
        power_factor = 0.93 + 0.03 * math.sin(t / 53.0) + 0.02 * step
        power_factor = min(0.999, power_factor)
        voltage = _average([ua, ub, uc])
        current = _average([ia, ib, ic])
        p = math.sqrt(3.0) * voltage * current * power_factor / 1000.0    # kW
        q = math.sqrt(3.0) * voltage * current * math.sqrt(max(0.0, 1.0 - power_factor ** 2)) / 1000.0
        q = max(0.0, q - 12.0 * step)                                     # 电容组补偿无功
        freq = 50.0 - 0.04 * math.sin(t / 29.0) if not outage else 0.0

        oil_temp = 68.0 + 20.0 * math.sin(t / 85.0)                       # 48~88℃
        winding_temp = oil_temp + 12.0
        load_rate = (current / 577.0 * 100.0) if not outage else 0.0      # 400kVA/0.4kV ≈ 577A
        ohmic = 0.4 + 0.3 * max(0.0, math.sin(t / 61.0))
        thdi = (2.5 + 3.0 * max(0.0, math.sin(t / 47.0))) * load

        energy_day = 1850.0 + 6.0 * t                                     # kWh，慢速累加
        energy_month = 41200.0 + 60.0 * t

        state = 0x0080                                                   # 通信正常
        if outage:
            state |= 1 << 5                                              # 停电
        else:
            state |= 1 << 0                                              # 运行
            if load_rate < 30.0:
                state |= 1 << 1                                          # 轻载
            elif load_rate > 85.0:
                state |= 1 << 2                                          # 过载
        if oil_temp > 75.0:
            state |= 1 << 3                                              # 油温高
        if door_open:
            state |= 1 << 6                                              # 柜门开

        alarm_word = 0
        if load_rate > 85.0:
            alarm_word |= 1 << 0                                         # 过载
        if oil_temp > 75.0:
            alarm_word |= 1 << 1                                         # 油温高
        if oil_temp > 85.0:
            alarm_word |= 1 << 2                                         # 油温过高
        if current_unbalance > 10.0 and not outage:
            alarm_word |= 1 << 3                                         # 电流不平衡
        if unbalance > 2.0 and not outage:
            alarm_word |= 1 << 4                                         # 电压不平衡
        if outage:
            alarm_word |= 1 << 7                                         # 停电

        digital_in = (1 << 1) if door_open else 0
        if outage:
            digital_in |= 1 << 4
        else:
            digital_in |= 1 << 0
            if load_rate < 30.0:
                digital_in |= 1 << 2
            elif load_rate > 85.0:
                digital_in |= 1 << 3

        put_u16(regs, 0, ua, 0.1)
        put_u16(regs, 1, ub, 0.1)
        put_u16(regs, 2, uc, 0.1)
        put_u16(regs, 3, ia, 0.1)
        put_u16(regs, 4, ib, 0.1)
        put_u16(regs, 5, ic, 0.1)
        put_u16(regs, 6, p, 0.01)
        put_u16(regs, 7, q, 0.01)
        put_u16(regs, 8, power_factor if not outage else 0.0, 0.001)
        put_u16(regs, 9, freq, 0.01)
        put_i16(regs, 10, oil_temp, 0.1)
        put_i16(regs, 11, winding_temp, 0.1)
        put_u16(regs, 12, load_rate, 0.1)
        put_u16(regs, 13, current_unbalance if not outage else 0.0, 0.01)
        put_u16(regs, 14, unbalance if not outage else 0.0, 0.01)
        put_u32(regs, 15, int(round(energy_day / 0.1)))
        put_u32(regs, 17, int(round(energy_month / 0.1)))
        put_u16(regs, 19, state)
        put_u16(regs, 20, alarm_word)
        put_u16(regs, 22, 400.0)                                         # 配变容量 kVA
        put_u16(regs, 23, thdi, 0.01)
        regs_input = getattr(store, "input", None)
        if regs_input is not None:
            regs_input[0] = digital_in
        discretes = getattr(store, "discretes", None)
        if discretes is not None:
            discretes[0] = door_open

        _ensure_u16(regs, 24, 0.1, 75.0, 60.0)


# --------------------------------------------------------------- 示意图


def grid_diagram():
    """配电网主回路示意图：

    110kV 电网 → 110/10kV 主变 → 10kV 进线断路器 → 10kV 母线段 → 馈线断路器
              → 配电变压器 → 0.4kV 低压母线 → 低压计量 → 负荷

    三台装置以 signal 连线挂在对应的一次设备旁；带 action 的节点可点击进设备详情页。
    """
    nodes = [
        node("grid", "grid", "110kV 电网", 0.01, 0.26, w=0.10, h=0.20, sub_label="三相交流"),
        node("t1", "transformer", "110/10kV 主变", 0.13, 0.26, w=0.14, h=0.20,
             sub_label="有载调压", description="主变本体不在本示例的采集范围内"),
        node("qf1", "breaker", "QF1", 0.29, 0.31, w=0.08, h=0.13, sub_label="10kV 进线",
             action="device", target=SUBSTATION,
             description="点击进入变电站测控装置（从站 41）"),
        node("bus10", "bus", "10kV 母线", 0.40, 0.27, w=0.15, h=0.18, sub_label="单母线分段",
             value_point=f"{SUBSTATION}.10kV母线A相电压", unit="kV", decimals=2),
        node("qf11", "breaker", "QF11", 0.58, 0.31, w=0.08, h=0.13, sub_label="馈线 1",
             action="device", target=FEEDER,
             description="点击进入馈线保护测控（从站 42）"),
        node("t2", "transformer", "配变 S13", 0.69, 0.26, w=0.14, h=0.20,
             sub_label="10/0.4kV", value_point=f"{TTU}.油温", unit="℃", decimals=1),
        node("bus04", "bus", "0.4kV 低压母线", 0.86, 0.27, w=0.13, h=0.18,
             value_point=f"{TTU}.低压侧A相电压", unit="V", decimals=1),
        node("mtr", "meter", "低压计量", 0.86, 0.53, w=0.13, h=0.14, sub_label="考核表",
             action="device", target=TTU,
             description="点击进入配变监测终端 TTU（从站 43）"),
        node("load", "load", "低压负荷 / 台区", 0.86, 0.75, w=0.13, h=0.14),
        node("dsp41", "dsp", "变电站测控装置", 0.10, 0.66, w=0.22, h=0.16,
             sub_label=f"Modbus 从站 {UNITS[0]}",
             action="device", target=SUBSTATION,
             description="遥测/遥信/遥控 + 定值，采集周期 500ms"),
        node("dsp42", "dsp", "馈线保护测控", 0.40, 0.66, w=0.22, h=0.16,
             sub_label=f"Modbus 从站 {UNITS[1]}",
             action="device", target=FEEDER,
             description="保护动作字、零序电流与遥控出口"),
        node("dsp43", "dsp", "配变监测终端 TTU", 0.70, 0.66, w=0.22, h=0.16,
             sub_label=f"Modbus 从站 {UNITS[2]}",
             action="device", target=TTU,
             description="配变温度、负载率与电量"),
    ]
    links = [
        link("grid", "t1", "ac", "110kV"),
        link("t1", "qf1", "ac", "10kV"),
        link("qf1", "bus10", "ac"),
        link("bus10", "qf11", "ac", "馈线 1"),
        link("qf11", "t2", "ac", "10kV"),
        link("t2", "bus04", "ac", "0.4kV"),
        link("bus04", "mtr", "ac"),
        link("mtr", "load", "ac"),
        link("dsp41", "bus10", "signal"),
        link("dsp42", "qf11", "signal"),
        link("dsp43", "t2", "signal"),
    ]
    return diagram("配电网监控主回路", nodes, links)


# --------------------------------------------------------------- 工程与仿真


def build() -> Project:
    """内置示例工程：一条 Modbus 通道挂三台配网装置（从站 41/42/43）。

    现场三台装置挂在同一条 RS485 上（Modbus RTU），本示例为无硬件演示走 TCP，
    指向内置仿真器 ``tools/modbus_sim.py`` 的 unit 41/42/43。
    """
    project = Project(
        name=NAME,
        folder_name=FOLDER_NAME,
        history_interval_s=5,
        history_retention_days=30,
        storage=ProjectStorage(),
    )

    project.channels.append(
        ChannelDef(
            name=CHANNEL,
            driver="modbus",
            link="tcp",
            description=(
                "一条 485 总线挂三台装置（从站 41/42/43）；示例走 TCP 指向内置仿真器，"
                "现场改为 rtu + 实际串口（如 COM3, 9600, 8E1）"
            ),
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

    project.devices.append(_substation_device())
    project.devices.append(_feeder_device())
    project.devices.append(_ttu_device())
    project.diagram = grid_diagram()
    return project


def simulate(store: Any, t: float) -> None:
    """按仿真时间 ``t``（秒）刷新该从站的寄存器区。

    ``store`` 是 ``tools/modbus_sim.py`` 的 DataStore，按 ``store.unit_id`` 分派到
    41/42/43 三套数据；波形缓冲区写在保持寄存器里，由上位机整块读回画示波器。

    **本模块自带仿真，不修改 project.py / modbus_sim.py**：仿真循环里按
    ``store.unit_id`` 调到这里即可（例如 ``_simulate_unit41`` 里直接 ``grid.simulate``）。
    """
    unit = int(getattr(store, "unit_id", 0) or 0)
    if unit == UNITS[0]:
        _simulate_unit41(store, t)
    elif unit == UNITS[1]:
        _simulate_unit42(store, t)
    elif unit == UNITS[2]:
        _simulate_unit43(store, t)
