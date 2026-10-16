"""生成 CAN / IEC 61850 的点表模板（走真正的导出函数，格式与软件一致）。

这两份 CSV 既是"点表模板"（现场照它改），也是回归测试的输入：
换协议以后最容易错的就是「寄存器区 / 协议路径 / 起始地址」这三列的含义，
所以让它们真过一遍导出-导入。
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icomm.core import csv_table  # noqa: E402
from icomm.core.models import (  # noqa: E402
    AlarmDef,
    ChannelDef,
    DeviceDef,
    PointDef,
    Project,
)

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")


def can_project() -> Project:
    """CAN 示例：一台储能变流器挂在 CAN 总线上（帧布局对齐 tools/can_sim.py）。"""
    channel = ChannelDef(
        name="CAN总线",
        driver="can",
        link="can",
        description="整车/储能 CAN，500kbit/s",
        options={
            "backend": "virtual",
            "channel": "vvp",
            "bitrate": 500000,
            "index": 0,
            "receive_own": False,
            "timeout_ms": 200,
        },
    )
    device = DeviceDef(
        name="储能变流器",
        channel=channel.name,
        unit_id=5,
        protocol="can",
        poll_interval_ms=200,
        description="CANopen 节点号 5（SDO 用从站号当节点号）",
    )
    device.points = [
        PointDef(
            name="母线电压", register="can_rx", path="0x181", address=0,
            data_type="uint16", scale=0.1, unit="V", decimals=1,
            label="母线电压", group="直流侧", display="card", archive=True, scope=True,
        ),
        PointDef(
            name="输出电流", register="can_rx", path="0x181", address=2,
            data_type="uint16", scale=0.1, unit="A", decimals=1,
            label="输出电流", group="交流侧", display="card", scope=True,
            alarm=AlarmDef(enabled=True, level="warning", hi=40.0, hi_hi=48.0, message="输出电流偏高"),
        ),
        PointDef(
            name="输出功率", register="can_rx", path="0x181", address=4,
            data_type="uint16", scale=0.1, unit="kW", decimals=1,
            group="统计", display="card",
        ),
        PointDef(
            name="散热器温度", register="can_rx", path="0x181", address=6,
            data_type="int16", scale=0.1, unit="℃", decimals=1,
            group="温度", display="card", scope=True,
            alarm=AlarmDef(enabled=True, level="critical", hi=75.0, hi_hi=85.0, message="散热器过温"),
        ),
        PointDef(
            name="状态字", register="can_rx", path="0x182", address=0,
            data_type="uint16", unit="", decimals=0,
            label="状态字", group="状态与控制", display="card",
            # 大端：低字节是状态（bit0 运行…），高字节是报警
            bits="8:运行,9:故障,10:告警,16:过温,17:过流",
        ),
        PointDef(
            name="交流频率", register="can_rx", path="0x182", address=2,
            data_type="uint16", scale=0.01, unit="Hz", decimals=2,
            group="交流侧", display="card",
        ),
        PointDef(
            name="累计运行时间", register="can_rx", path="0x182", address=4,
            data_type="uint32", unit="s", decimals=0, group="统计", display="card",
        ),
        PointDef(
            name="电压给定", register="can_tx", path="0x201", address=0,
            data_type="uint16", scale=0.1, unit="V", decimals=1, writable=True,
            group="状态与控制",
        ),
        PointDef(
            name="控制字", register="can_tx", path="0x201", address=2,
            data_type="uint16", unit="", decimals=0,
            group="状态与控制",
            commands="1:启动,2:停止,4:复位",
        ),
        PointDef(
            name="SDO状态字", register="can_sdo", path="0x6041:0",
            data_type="uint16", unit="", decimals=0, group="状态与控制",
            bits="0:就绪,1:运行,3:故障,7:告警",
        ),
        PointDef(
            name="SDO转速", register="can_sdo", path="0x6042:0",
            data_type="int16", unit="rpm", decimals=0, group="交流侧", scope=True,
        ),
        PointDef(
            name="SDO功率给定", register="can_sdo", path="0x2000:0",
            data_type="uint16", scale=0.1, unit="kW", decimals=1, writable=True,
            group="状态与控制",
        ),
        PointDef(
            name="SDO设备类型", register="can_sdo", path="0x1000:0",
            data_type="uint32", unit="", decimals=0, group="统计",
        ),
    ]
    project = Project(name="CAN 储能变流器（点表模板）", channels=[channel], devices=[device])
    project.name = "CAN 储能变流器（点表模板）"
    return project


def iec61850_project() -> Project:
    """61850 示例：对齐 assets/sim_iec61850.icd，端口也对着内置仿真器。"""
    channel = ChannelDef(
        name="61850站控",
        driver="iec61850",
        link="tcp",
        description="站控层 MMS",
        options={
            "host": "127.0.0.1",
            "ied_name": "VVPSIM",
            "port": 8102,
            "timeout_ms": 5000,
            "request_timeout_ms": 4000,
            "batch_size": 40,
            "reports": True,
            "rcb_refs": "MEAS/LLN0.RP1",
            "report_poll_ms": 200,
        },
    )
    meas = DeviceDef(
        name="测量",
        channel=channel.name,
        unit_id=1,
        protocol="iec61850",
        poll_interval_ms=500,
        description="逻辑设备 MEAS（只看数据）",
    )
    meas.points = [
        PointDef(
            name="频率", register="MX", path="MEAS/MMXU1.Hz.mag.f",
            data_type="float32", unit="Hz", decimals=3, display="card", scope=True,
            label="系统频率", group="测量",
        ),
        PointDef(
            name="电流", register="MX", path="MEAS/MMXU1.A.mag.f",
            data_type="float32", unit="A", decimals=2, display="card", scope=True,
            label="A 相电流", group="测量",
            alarm=AlarmDef(enabled=True, level="warning", hi=280.0, message="电流偏高"),
        ),
        PointDef(
            name="定值", register="SP", path="MEAS/ASG1.SetPnt.setMag.f",
            data_type="float32", unit="", decimals=2, writable=True, group="定值",
        ),
        PointDef(
            name="死区", register="CF", path="MEAS/MMXU1.Hz.db",
            data_type="uint32", unit="", decimals=0, writable=True, group="定值",
        ),
    ]
    ctrl = DeviceDef(
        name="控制",
        channel=channel.name,
        unit_id=2,
        protocol="iec61850",
        poll_interval_ms=500,
        description="逻辑设备 CTRL（开关位置）",
    )
    ctrl.points = [
        PointDef(
            name="开关位置", register="ST", path="CTRL/XCBR1.Pos.stVal",
            data_type="uint16", unit="", decimals=0, display="card",
            label="断路器位置", group="控制",
            codes="0:中间,1:合位,2:分位,3:无效",
        ),
    ]
    project = Project(name="IEC 61850 变电站（点表模板）", channels=[channel], devices=[meas, ctrl])
    project.name = "IEC 61850 变电站（点表模板）"
    return project


def main() -> int:
    os.makedirs(EXAMPLES, exist_ok=True)
    for name, project in (
        ("can_parameters.csv", can_project()),
        ("iec61850_parameters.csv", iec61850_project()),
    ):
        path = os.path.join(EXAMPLES, name)
        rows = csv_table.export_csv(project, path)
        print(f"{name}: 导出 {rows} 行 -> {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
