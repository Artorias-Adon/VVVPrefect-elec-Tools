"""内置 CAN 从站仿真器（储能变流器示例）。

不接硬件也能把上位机跑通：用 python-can 的 ``virtual`` 后端起一条虚拟总线，
周期发几帧"像真设备"的数据，并应答 CANopen SDO 请求。
思路与 ``tools/modbus_sim.py`` 一样，只是把 Modbus 数据区换成了 CAN 报文。

用法::

    python tools/can_sim.py                                    # 虚拟总线 vvp，节点 5
    python tools/can_sim.py --channel vvp --node 5
    python tools/can_sim.py --interface slcan --channel COM3    # 接真 CAN 棒

报文布局（点表模板按这个填；帧内数值都是**大端**，CANopen SDO 是小端）：

``0x181`` 变流器测量帧（10 Hz，8 字节）
    [0..1] uint16 母线电压    scale 0.1  -> V
    [2..3] uint16 输出电流    scale 0.1  -> A
    [4..5] uint16 输出功率    scale 0.1  -> kW
    [6..7] int16  散热器温度  scale 0.1  -> ℃

``0x182`` 状态帧（10 Hz，8 字节）
    [0]    uint8  状态字      bit0 运行 / bit1 故障 / bit2 告警
    [1]    uint8  报警字      bit0 过温 / bit1 过流
    [2..3] uint16 交流频率    scale 0.01 -> Hz
    [4..7] uint32 累计运行时间 -> s

``0x201`` 命令帧（上位机 -> 变流器，仿真器收到后按它改上面的量）
    [0..1] uint16 电压给定    scale 0.1  -> V
    [2..3] uint16 控制字      bit0 启动 / bit1 停止 / bit2 复位
    [4..7] 保留（上位机读-改-写时会原样留着）

对象字典（节点号 = ``--node``，SDO 请求 ``0x600+node`` / 应答 ``0x580+node``）：
    0x6041:0 uint16 状态字（只读）
    0x6042:0 int16  电流给定（可写，scale 0.1 -> A）
    0x2000:0 uint16 母线电压给定（可写，scale 0.1 -> V）
    0x1000:0 uint32 设备类型（只读）
    不存在的对象回中止码 0x06020000，写只读对象回 0x06010000。

仿真量按一阶惯性逼近给定值：给定怎么变，实际值就慢慢地跟过去 ——
现场看曲线就是这种手感，值也一直在小范围变化，便于确认"数据在刷新"。
"""

from __future__ import annotations

import argparse
import math
import os
import struct
import sys
import threading
import time
from dataclasses import dataclass

# 允许 `python tools/can_sim.py` 直接跑：把仓库根目录加进 sys.path，
# 这样才 import 得到 icomm（开总线的那套参数白名单在驱动里，两边共用一份）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icomm.core.drivers.can import create_bus  # noqa: E402

try:
    import can
except Exception:  # pragma: no cover - 取决于运行环境
    can = None  # type: ignore[assignment]

# --------------------------------------------------------------- 报文定义

MEASUREMENT_ID = 0x181
STATUS_ID = 0x182
COMMAND_ID = 0x201

STATUS_RUNNING = 0x01
STATUS_FAULT = 0x02
STATUS_ALARM = 0x04

COMMAND_START = 0x01
COMMAND_STOP = 0x02
COMMAND_RESET = 0x04

ALARM_OVERTEMP = 0x01
ALARM_OVERCURRENT = 0x02

#: 发送周期：10 Hz，人眼看曲线够用，总线也不至于被刷满
FRAME_HZ = 10.0

MAX_CURRENT_A = 80.0
OVERTEMP_C = 70.0

# --------------------------------------------------------------- CANopen

SDO_REQUEST_COB = 0x600
SDO_RESPONSE_COB = 0x580

SDO_CMD_UPLOAD_REQUEST = 0x40
SDO_CMD_ABORT = 0x80
SDO_CMD_DOWNLOAD_RESPONSE = 0x60

#: expedited 传输命令字：字节数 -> 命令字
SDO_UPLOAD_COMMANDS = {1: 0x4F, 2: 0x4B, 4: 0x43}
SDO_DOWNLOAD_COMMANDS = {1: 0x2F, 2: 0x2B, 4: 0x23}

ABORT_NO_OBJECT = 0x06020000
ABORT_READ_ONLY = 0x06010000
ABORT_BAD_LENGTH = 0x06070010

OBJECT_STATUS_WORD = (0x6041, 0)
OBJECT_CURRENT_SETPOINT = (0x6042, 0)
OBJECT_SETPOINT = (0x2000, 0)
OBJECT_DEVICE_TYPE = (0x1000, 0)

#: 设备类型：低 16 位是设备类型码，高 16 位是附加信息
DEVICE_TYPE = 0x00020192


@dataclass
class SdoObject:
    """对象字典里的一项。"""

    size: int
    value: int = 0
    writable: bool = False


def _clamp_u16(value: float) -> int:
    return max(0, min(0xFFFF, int(round(value))))


def _clamp_i16(value: float) -> int:
    return max(-0x8000, min(0x7FFF, int(round(value))))


class CanSimulator:
    """一台会说话的 CAN 从站（储能变流器演示）。"""

    def __init__(
        self,
        interface: str = "virtual",
        channel: str = "vvp",
        bitrate: int = 500000,
        node_ids: tuple[int, ...] = (5,),
    ) -> None:
        self.interface = interface
        self.channel = channel
        self.bitrate = int(bitrate)
        self.node_ids = tuple(int(node) for node in node_ids)
        self._bus = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._start_time = 0.0

        # 电气量：上电即运行，电流按给定爬升
        self.bus_voltage = 700.0
        self.current = 18.0
        self.temperature = 35.0
        self.frequency = 50.0
        self.running_time_s = 0.0
        self.output_on = True
        self.fault = False
        self.alarm_overtemp = False
        self.alarm_overcurrent = False

        # 可被命令帧 / SDO 改写的给定值
        self.setpoint_v = 700.0
        self.current_setpoint_a = 20.0

        self.objects = self._default_objects()

    # -------------------------------------------------- 生命周期

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        if can is None:
            raise RuntimeError("未安装 python-can，请执行 pip install python-can")
        self._stop.clear()
        self._bus = create_bus(
            interface=self.interface, channel=self.channel, bitrate=self.bitrate
        )
        self._start_time = time.time()
        self._thread = threading.Thread(
            target=self._loop, args=(self._bus,), name="can-sim", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        thread, self._thread = self._thread, None
        bus, self._bus = self._bus, None
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass

    # -------------------------------------------------- 主循环

    def _loop(self, bus) -> None:
        """收收命令、到点发报文。"""
        next_frame = time.time()
        while not self._stop.is_set():
            try:
                message = bus.recv(timeout=0.05)
            except Exception:
                break  # 总线已经关了，收帧到头
            if message is not None:
                self._handle(message, bus)
            now = time.time()
            if now >= next_frame:
                next_frame = now + 1.0 / FRAME_HZ
                with self._lock:
                    self._advance(now - self._start_time)
                    self._transmit(bus)

    def _handle(self, message, bus) -> None:
        can_id = int(message.arbitration_id)
        data = bytes(message.data or b"")
        if can_id == COMMAND_ID:
            self._apply_command(data)
        elif SDO_REQUEST_COB <= can_id < SDO_REQUEST_COB + 0x80:
            node = can_id - SDO_REQUEST_COB
            response = self._sdo_response(node, data)
            if response is not None:
                bus.send(
                    can.Message(
                        arbitration_id=SDO_RESPONSE_COB + node,
                        is_extended_id=False,
                        data=response,
                    )
                )

    # -------------------------------------------------- 命令与物理量

    def _apply_command(self, data: bytes) -> None:
        """命令帧：上位机写进来的电压给定与控制字在这里生效。"""
        with self._lock:
            if len(data) >= 2:
                self.setpoint_v = int.from_bytes(data[0:2], "big") * 0.1
            if len(data) >= 4:
                control = int.from_bytes(data[2:4], "big")
                if control & COMMAND_START:
                    self.output_on = True
                if control & COMMAND_STOP:
                    self.output_on = False
                if control & COMMAND_RESET:
                    self.fault = False
                    self.alarm_overtemp = False
                    self.alarm_overcurrent = False

    def _advance(self, elapsed: float) -> None:
        """推进一拍的物理量（调用方已持锁）。"""
        target_v = self.setpoint_v
        target_i = self.current_setpoint_a if self.output_on else 0.0
        self.bus_voltage += (target_v - self.bus_voltage) * 0.08
        self.current += (target_i - self.current) * 0.10
        temp_target = 28.0 if self.current < 0.5 else 34.0 + self.current / 60.0 * 30.0
        self.temperature += (temp_target - self.temperature) * 0.05
        self.frequency = 50.0 + 0.05 * math.sin(elapsed / 3.0)
        self.running_time_s += 1.0 / FRAME_HZ
        self.alarm_overtemp = self.temperature > OVERTEMP_C
        self.alarm_overcurrent = self.current > MAX_CURRENT_A
        self.fault = self.fault or self.alarm_overcurrent
        self.objects[OBJECT_STATUS_WORD].value = self._status_word()

    def _status_word(self) -> int:
        word = 0
        if self.output_on:
            word |= STATUS_RUNNING
        if self.fault:
            word |= STATUS_FAULT
        if self.alarm_overtemp or self.alarm_overcurrent:
            word |= STATUS_ALARM
        return word

    def _alarm_word(self) -> int:
        word = 0
        if self.alarm_overtemp:
            word |= ALARM_OVERTEMP
        if self.alarm_overcurrent:
            word |= ALARM_OVERCURRENT
        return word

    def _transmit(self, bus) -> None:
        """把当前状态发成两帧（调用方已持锁）。"""
        measurement = struct.pack(
            ">HHHh",
            _clamp_u16(self.bus_voltage * 10),
            _clamp_u16(self.current * 10),
            _clamp_u16(self.bus_voltage * self.current / 1000.0 * 10),
            _clamp_i16(self.temperature * 10),
        )
        status = (
            struct.pack(
                ">BBH",
                self._status_word(),
                self._alarm_word(),
                _clamp_u16(self.frequency * 100),
            )
            + struct.pack(">I", int(self.running_time_s) & 0xFFFFFFFF)
        )
        for can_id, data in ((MEASUREMENT_ID, measurement), (STATUS_ID, status)):
            bus.send(can.Message(arbitration_id=can_id, is_extended_id=False, data=data))

    # -------------------------------------------------- SDO 从站

    def _default_objects(self) -> dict[tuple[int, int], SdoObject]:
        return {
            OBJECT_STATUS_WORD: SdoObject(2, self._status_word()),
            OBJECT_CURRENT_SETPOINT: SdoObject(
                2, _clamp_i16(self.current_setpoint_a * 10), writable=True
            ),
            OBJECT_SETPOINT: SdoObject(2, _clamp_u16(self.setpoint_v * 10), writable=True),
            OBJECT_DEVICE_TYPE: SdoObject(4, DEVICE_TYPE),
        }

    def _sdo_response(self, node: int, data: bytes) -> bytes | None:
        """应答一条 SDO 请求；不是本节点或命令字不认识就返回 None。"""
        if node not in self.node_ids or len(data) < 4:
            return None
        command = data[0]
        index = int.from_bytes(data[1:3], "little")
        subindex = data[3]
        with self._lock:
            obj = self.objects.get((index, subindex))

        if command == SDO_CMD_UPLOAD_REQUEST:
            if obj is None:
                return self._abort(index, subindex, ABORT_NO_OBJECT)
            return self._upload_response(index, subindex, obj)

        if command in SDO_DOWNLOAD_COMMANDS.values():
            if obj is None:
                return self._abort(index, subindex, ABORT_NO_OBJECT)
            if not obj.writable:
                return self._abort(index, subindex, ABORT_READ_ONLY)
            size = 4 - ((command >> 2) & 0x03)
            if size != obj.size:
                return self._abort(index, subindex, ABORT_BAD_LENGTH)
            value = int.from_bytes(bytes(data[4 : 4 + size]), "little", signed=True)
            with self._lock:
                obj.value = value
            self._apply_object_write(index, subindex, value)
            return (
                bytes([SDO_CMD_DOWNLOAD_RESPONSE])
                + struct.pack("<HB", index, subindex)
                + bytes(4)
            )

        return None  # 分段传输等没实现的命令字，直接不理

    def _apply_object_write(self, index: int, subindex: int, value: int) -> None:
        """对象被写进来以后，该跟着变的电气量在这里改。"""
        if (index, subindex) == OBJECT_CURRENT_SETPOINT:
            self.current_setpoint_a = value * 0.1
        elif (index, subindex) == OBJECT_SETPOINT:
            self.setpoint_v = value * 0.1

    @staticmethod
    def _upload_response(index: int, subindex: int, obj: SdoObject) -> bytes:
        raw = int(obj.value) & ((1 << (obj.size * 8)) - 1)
        return (
            bytes([SDO_UPLOAD_COMMANDS[obj.size]])
            + struct.pack("<HB", index, subindex)
            + raw.to_bytes(obj.size, "little")
            + bytes(4 - obj.size)
        )

    @staticmethod
    def _abort(index: int, subindex: int, code: int) -> bytes:
        return (
            bytes([SDO_CMD_ABORT])
            + struct.pack("<HB", index, subindex)
            + code.to_bytes(4, "little")
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="CAN 从站仿真器（储能变流器示例）")
    parser.add_argument("--interface", default="virtual", help="python-can 后端（默认 virtual）")
    parser.add_argument("--channel", default="vvp", help="总线名 / 串口（默认 vvp）")
    parser.add_argument("--bitrate", type=int, default=500000, help="波特率（默认 500000）")
    parser.add_argument("--node", type=int, default=5, help="CANopen 节点号（默认 5）")
    args = parser.parse_args()

    simulator = CanSimulator(
        interface=args.interface,
        channel=args.channel,
        bitrate=args.bitrate,
        node_ids=(args.node,),
    )
    simulator.start()
    print(
        f"CAN 仿真器已启动：{args.interface}@{args.channel} {args.bitrate // 1000} kbit/s，"
        f"节点 {args.node}；周期帧 0x{MEASUREMENT_ID:03X}/0x{STATUS_ID:03X}，"
        f"命令帧 0x{COMMAND_ID:03X}，Ctrl+C 停止"
    )
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("收到中断，正在停止……")
    finally:
        simulator.stop()


if __name__ == "__main__":
    main()
