"""西门子 S7 仿真 PLC：用 python-snap7 自带的 S7 服务端起一台"会动的 PLC"。

和 Modbus / CAN / 61850 的仿真器一个思路：手边没有真机也能把 S7 驱动、点表、报警、
趋势整条链路跑通。python-snap7 3.x 是纯 Python 实现，服务端也在里面，所以这个
仿真器**不需要装任何原生库**。

仿真出来的装置（地址布局见下面的 ``LAYOUT``，点表模板就是照它生成的）::

    DB1   测量数据（母线电压 / 输出电流 / 输出功率 / 散热器温度 / 状态字 / 频率 / 运行时间）
    DB2   定值与命令（有功给定、控制字、设备编号、并网允许位）—— 可写
    M     位存储区（运行 / 告警 / 故障 / 远程方式、控制字、数字量状态）
    I     输入区（急停、数字量输入字节、模拟量输入）
    Q     输出区（主接触器、数字量输出字节）

写进去的值服务端会真的收下（``DB2`` 的有功给定、控制字、``M`` 区的位都能改），
所以"启动/停机、限功率、并网允许"这些遥控点位是可以真写、真回读的。

默认端口 **10102**：不用 102，因为 102 在 Windows 上要管理员权限，
而且 1032~1131 这一段被 Hyper-V 保留，随手挑 1102 之类会绑定失败。
"""

from __future__ import annotations

import math
import struct
import threading
import time
from dataclasses import dataclass

#: 默认监听地址与端口（点表模板里通道的端口就填这个）
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 10102

#: 各区大小（字节）
DB1_SIZE = 256
DB2_SIZE = 64
MK_SIZE = 128
PE_SIZE = 64
PA_SIZE = 64

#: struct 格式（S7 一律大端）
_FMT = {
    "uint8": ">B",
    "int8": ">b",
    "uint16": ">H",
    "int16": ">h",
    "uint32": ">I",
    "int32": ">i",
    "float32": ">f",
    "float64": ">d",
}


@dataclass(frozen=True)
class Field:
    """仿真装置里一个点位：地址、类型、工程量换算，点表模板共用这份定义。"""

    key: str
    #: db / i / q / m
    area: str
    #: DB 号（area == "db" 时有效）
    db: int
    #: 字节偏移
    offset: int
    data_type: str
    #: 点表里「协议路径」列该填什么
    path: str
    #: 中文名（点表的显示名称）
    title: str
    unit: str = ""
    scale: float = 1.0
    writable: bool = False
    #: 位点位（bool）的位号
    bit: int = 0

    @property
    def width(self) -> int:
        if self.data_type == "bool":
            return 1
        return struct.calcsize(_FMT[self.data_type])


def _point(key, area, db, offset, data_type, path, title, unit="", scale=1.0, writable=False, bit=0) -> Field:
    return Field(key, area, db, offset, data_type, path, title, unit, scale, writable, bit)


#: 仿真装置的地址布局 —— 点表模板与仿真器都按它来，改一处两边都跟着变
LAYOUT: list[Field] = [
    # ---- DB1 测量数据 ----
    _point("voltage", "db", 1, 0, "float32", "DB1.DBD0", "母线电压", "V", 1.0),
    _point("current", "db", 1, 4, "float32", "DB1.DBD4", "输出电流", "A", 1.0),
    _point("power", "db", 1, 8, "float32", "DB1.DBD8", "输出功率", "kW", 1.0),
    _point("temperature", "db", 1, 12, "float32", "DB1.DBD12", "散热器温度", "℃", 1.0),
    _point("status_word", "db", 1, 16, "uint16", "DB1.DBW16", "状态字", "", 1.0),
    _point("frequency", "db", 1, 18, "float32", "DB1.DBD18", "交流频率", "Hz", 1.0),
    _point("runtime", "db", 1, 22, "uint32", "DB1.DBD22", "累计运行时间", "s", 1.0),
    _point("grid_allow", "db", 1, 30, "bool", "DB1.DBX30.0", "并网允许", "", 1.0, True, 0),
    # ---- DB2 定值与命令（可写） ----
    _point("power_setpoint", "db", 2, 0, "float32", "DB2.DBD0", "有功功率给定", "kW", 1.0, True),
    _point("control_word", "db", 2, 4, "uint16", "DB2.DBW4", "控制字", "", 1.0, True),
    _point("device_code", "db", 2, 6, "uint8", "DB2.DBB6", "设备编号", "", 1.0),
    # ---- M 位存储区 ----
    _point("m_run", "m", 0, 0, "bool", "M0.0", "运行标志", "", 1.0, bit=0),
    _point("m_alarm", "m", 0, 0, "bool", "M0.1", "告警标志", "", 1.0, bit=1),
    _point("m_fault", "m", 0, 0, "bool", "M0.2", "故障标志", "", 1.0, bit=2),
    _point("m_remote", "m", 0, 0, "bool", "M0.3", "远程方式", "", 1.0, bit=3),
    _point("m_power_pct", "m", 0, 12, "uint16", "MW12", "功率百分比给定", "%", 1.0, True),
    _point("m_status_byte", "m", 0, 16, "uint8", "MB16", "数字量状态字节", "", 1.0),
    # ---- I 输入区 ----
    _point("i_estop", "i", 0, 0, "bool", "I0.0", "急停输入", "", 1.0, bit=0),
    _point("i_di_byte", "i", 0, 1, "uint8", "IB1", "数字量输入字节", "", 1.0),
    _point("i_ai_word", "i", 0, 2, "uint16", "IW2", "模拟量输入", "V", 0.1),
    # ---- Q 输出区 ----
    _point("q_main_contactor", "q", 0, 0, "bool", "Q0.0", "主接触器输出", "", 1.0, bit=0),
    _point("q_do_byte", "q", 0, 1, "uint8", "QB1", "数字量输出字节", "", 1.0),
]

#: key -> 定义
BY_KEY: dict[str, Field] = {field.key: field for field in LAYOUT}

#: 状态字位定义（点表模板与仿真器共用）
STATUS_BITS = "0:运行,1:就绪,2:告警,3:故障,4:限功率,8:交流接触器合,9:预充完成"
#: 控制字位定义
CONTROL_BITS = "0:启动,1:停机,2:复位,3:远程"


class S7Simulator:
    """一台会动的西门子 S7 装置（真 S7 服务端，不是假的桩）。"""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        refresh_s: float = 0.1,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.refresh_s = max(0.02, float(refresh_s))
        self._server = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._error = ""
        self._lock = threading.Lock()
        #: 各区缓冲区：服务端直接读写这些 bytearray，客户端写进来的值立刻可见
        self._db: dict[int, bytearray] = {
            1: bytearray(DB1_SIZE),
            2: bytearray(DB2_SIZE),
        }
        self._areas: dict[str, bytearray] = {
            "m": bytearray(MK_SIZE),
            "i": bytearray(PE_SIZE),
            "q": bytearray(PA_SIZE),
        }
        self._started_at = time.monotonic()
        self._reset_values()

    # -------------------------------------------------- 对外状态

    @property
    def running(self) -> bool:
        return bool(self._running and self._server is not None)

    @property
    def error(self) -> str:
        return self._error

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    # -------------------------------------------------- 生命周期

    def start(self) -> "S7Simulator":
        """起服务端与"装置行为"线程；失败抛异常（调用方负责提示）。"""
        if self.running:
            return self
        from snap7 import Server, type as snap7_type

        server = Server(log=False)
        server.register_area(snap7_type.SrvArea.DB, 1, self._db[1])
        server.register_area(snap7_type.SrvArea.DB, 2, self._db[2])
        server.register_area(snap7_type.SrvArea.MK, 0, self._areas["m"])
        server.register_area(snap7_type.SrvArea.PE, 0, self._areas["i"])
        server.register_area(snap7_type.SrvArea.PA, 0, self._areas["q"])
        server.set_cpu_status(8)  # 8 = RUN
        server.start(self.port)

        self._server = server
        self._running = True
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._behave, name="icomm-s7-sim", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        """停服务端与行为线程，允许重复调用。"""
        self._running = False
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        server, self._server = self._server, None
        if server is not None:
            try:
                server.stop()
            except Exception:
                pass
            try:
                server.destroy()
            except Exception:
                pass

    # -------------------------------------------------- 初值

    def _reset_values(self) -> None:
        """给各点位一个像样的初值（服务端一开就能看到数）。"""
        self._set("voltage", 750.0)
        self._set("current", 250.0)
        self._set("power", 187.5)
        self._set("temperature", 42.0)
        self._set("frequency", 50.0)
        self._set("runtime", 0)
        self._set("power_setpoint", 200.0)
        self._set("control_word", 0)
        self._set("device_code", 0x21)
        self._set("m_power_pct", 100)
        self._set("m_status_byte", 0x0D)
        self._set("i_di_byte", 0x05)
        self._set("i_ai_word", 750.0)
        self._set("q_do_byte", 0x01)
        self._set("grid_allow", True)

    # -------------------------------------------------- 读写缓冲区

    def _buffer(self, field: Field) -> bytearray:
        if field.area == "db":
            return self._db.setdefault(field.db, bytearray(DB1_SIZE))
        return self._areas[field.area]

    def _set(self, key: str, value: float) -> None:
        """按工程量写一个点位（内部自动做 scale 反算与位处理）。"""
        field = BY_KEY.get(key)
        if field is None:
            raise KeyError(key)
        with self._lock:
            buffer = self._buffer(field)
            if field.data_type == "bool":
                mask = 1 << field.bit
                if value:
                    buffer[field.offset] |= mask
                else:
                    buffer[field.offset] &= ~mask & 0xFF
                return
            raw = float(value) / field.scale if field.scale else float(value)
            fmt = _FMT[field.data_type]
            if not field.data_type.startswith("float"):
                raw = int(round(raw))
            struct.pack_into(fmt, buffer, field.offset, raw)

    def _get(self, key: str) -> float:
        field = BY_KEY[key]
        with self._lock:
            buffer = self._buffer(field)
            if field.data_type == "bool":
                return 1.0 if (buffer[field.offset] >> field.bit) & 1 else 0.0
            (raw,) = struct.unpack_from(_FMT[field.data_type], buffer, field.offset)
            return float(raw) * field.scale

    # -------------------------------------------------- 装置行为

    def _behave(self) -> None:
        """周期刷新测量值，并把控制字/给定值反映到状态字与标志位上。"""
        while self._running:
            try:
                self._step()
            except Exception as exc:  # pragma: no cover - 仿真线程不该把程序带崩
                self._error = str(exc)
                return
            time.sleep(self.refresh_s)

    def _step(self) -> None:
        elapsed = time.monotonic() - self._started_at
        control = int(self._get("control_word"))
        running = bool(control & 0x0001) and not bool(control & 0x0002)
        if control & 0x0004:  # 复位：清掉给定值，方便再演示一遍
            self._set("power_setpoint", 200.0)
            self._set("m_power_pct", 100)
            self._set("control_word", control & ~0x0004)

        setpoint = self._get("power_setpoint")
        limited = setpoint < 50.0
        load = 0.02 if limited else 1.0

        voltage = 750.0 + 6.0 * math.sin(2 * math.pi * 0.13 * elapsed)
        current = (250.0 + 20.0 * math.sin(2 * math.pi * 0.07 * elapsed)) * load
        power = voltage * current / 1000.0
        temperature = 42.0 + 1.5 * math.sin(2 * math.pi * 0.02 * elapsed)
        frequency = 50.0 + 0.05 * math.sin(2 * math.pi * 0.11 * elapsed)

        status = 0
        if running:
            status |= 1 << 0
        status |= 1 << 1  # 就绪
        if temperature > 43.0:
            status |= 1 << 2  # 告警
        if limited:
            status |= 1 << 4  # 限功率
        if running:
            status |= 1 << 8  # 交流接触器合
        status |= 1 << 9  # 预充完成

        self._set("voltage", voltage)
        self._set("current", current)
        self._set("power", power)
        self._set("temperature", temperature)
        self._set("frequency", frequency)
        self._set("runtime", int(elapsed))
        self._set("status_word", status)
        self._set("m_run", running)
        self._set("m_alarm", bool(status & (1 << 2)))
        self._set("m_fault", bool(status & (1 << 3)))
        self._set("m_remote", bool(control & 0x0008))
        self._set("m_status_byte", (0x0D if running else 0x05) | (0x02 if temperature > 43.0 else 0))
        self._set("i_di_byte", 0x05 | (0x02 if int(elapsed) % 8 < 4 else 0))
        self._set("i_ai_word", voltage)
        self._set("q_main_contactor", running)
        self._set("q_do_byte", 0x01 if running else 0x00)


def main(argv: list[str] | None = None) -> int:
    """命令行跑一个仿真 PLC：``python -m tools.s7_sim [端口]``。"""
    import argparse

    parser = argparse.ArgumentParser(description="西门子 S7 仿真 PLC（真 S7 服务端）")
    parser.add_argument("port", nargs="?", type=int, default=DEFAULT_PORT, help="监听端口")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址")
    args = parser.parse_args(argv)

    simulator = S7Simulator(host=args.host, port=args.port)
    simulator.start()
    print(f"S7 仿真 PLC 已启动：{simulator.address}（机架 0 插槽 1，Ctrl+C 退出）")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        simulator.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
