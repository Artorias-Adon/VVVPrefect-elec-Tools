"""Modbus TCP 从站仿真器。

用原生 socket 实现，不依赖 pymodbus，因此任何环境下都能跑，
用于在没有真实 PLC/仪表时联调上位机。

用法::

    python tools/modbus_sim.py                     # 监听 0.0.0.0:5020，含全部内置示例
    python tools/modbus_sim.py --port 5020 --verbose
    python tools/modbus_sim.py --freeze            # 数值固定，便于核对读数

支持功能码：01/02/03/04 读，05/06/0F/10 写。

数据区与内置示例工程一一对应：

* unit 1 / 2    配电柜监控（在 ``icomm/core/project.py`` 里）
* unit 11/12/13 SST 固态变压器（同上）
* unit 31       储能电站（``icomm/core/samples/bess.py``）
* unit 32       光伏电站（``icomm/core/samples/pv.py``）
* unit 41/42/43 电力系统配电网（``icomm/core/samples/grid.py``）

各示例模块的 ``simulate(store, t)`` 负责刷新自己那些从站的数据，
``init_defaults(store)`` 负责给可写点位写一次性出厂默认值
（**可写点不能被每轮仿真覆盖**，否则主站写进去的定值会被刷掉）。
"""

from __future__ import annotations

import argparse
import math
import os
import socket
import struct
import sys
import threading
import time

# 允许 `python tools/modbus_sim.py` 直接跑：把仓库根目录加进 sys.path，
# 这样才 import 得到 icomm —— 内置示例（储能 / 光伏 / 电力系统）的点表与仿真行为
# 都定义在 icomm/core/samples/ 里，仿真器只负责把它们接到自己的数据区上。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icomm.core import samples as sample_pack  # noqa: E402

FC_READ_COILS = 0x01
FC_READ_DISCRETE = 0x02
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_COIL = 0x05
FC_WRITE_REGISTER = 0x06
FC_WRITE_COILS = 0x0F
FC_WRITE_REGISTERS = 0x10

EXC_ILLEGAL_FUNCTION = 0x01
EXC_ILLEGAL_ADDRESS = 0x02
EXC_ILLEGAL_VALUE = 0x03

SIZE = 1024


class DataStore:
    """一台从站的数据区。"""
    def __init__(self, unit_id: int) -> None:
        self.unit_id = unit_id
        self.coils = [False] * SIZE
        self.discretes = [False] * SIZE
        self.holding = [0] * SIZE
        self.input = [0] * SIZE
        self.lock = threading.RLock()
        self.write_count = 0
        self.last_write: tuple[str, int, int] | None = None

    def read_bits(self, kind: str, address: int, count: int) -> list[bool]:
        data = self.coils if kind == "coil" else self.discretes
        with self.lock:
            return [data[i] for i in range(address, address + count)]

    def read_registers(self, kind: str, address: int, count: int) -> list[int]:
        data = self.holding if kind == "holding" else self.input
        with self.lock:
            return [data[i] for i in range(address, address + count)]

    def write_bits(self, address: int, values: list[bool]) -> None:
        with self.lock:
            for offset, value in enumerate(values):
                self.coils[address + offset] = value
            self.write_count += 1
            self.last_write = ("coil", address, int(values[0]) if values else 0)

    def write_registers(self, address: int, values: list[int]) -> None:
        with self.lock:
            for offset, value in enumerate(values):
                self.holding[address + offset] = value & 0xFFFF
            self.write_count += 1
            self.last_write = ("holding", address, values[0] if values else 0)


class ModbusTcpSimulator:
    def __init__(self, host: str = "0.0.0.0", port: int = 5020, verbose: bool = False) -> None:
        self.host = host
        self.port = port
        self.verbose = verbose
        self.units: dict[int, DataStore] = {
            # 配电柜演示
            1: DataStore(1),
            2: DataStore(2),
            # SST 固态变压器演示：级联整流 DSP / DCDC DSP / 计量单元
            11: DataStore(11),
            12: DataStore(12),
            13: DataStore(13),
            # 储能 / 光伏 / 电力系统：从站号由各示例模块自己声明，
            # 加示例时这里不用改
            **{unit: DataStore(unit) for unit in sample_pack.SIM_UNIT_IDS},
        }
        self._stop = threading.Event()
        self._server: socket.socket | None = None
        #: 已经接进来的连接。停止时要主动断掉，否则"停止仿真器"之后
        #: 已经连着的主站还能继续读（accept 停了、老连接还活着），
        #: 看起来就是"停了没停干净"。
        self._clients: set[socket.socket] = set()
        self._clients_lock = threading.Lock()
        self.start_time = time.time()
        # SST 演示默认把整流与 DCDC 置为运行，这样一启动就有波形可看（主站仍可写线圈关掉）
        self.units[11].coils[0] = True
        self.units[12].coils[0] = True
        # 储能 / 光伏 / 电力系统的可写点（控制字、给定值、定值）只在启动时写一次
        # 出厂默认值；之后每轮的 simulate 只刷只读遥测，免得把主站写进来的值覆盖掉
        for unit, module in sample_pack.SIM_UNITS:
            init = getattr(module, "init_defaults", None)
            if init is not None:
                init(self.units[unit])

    # --------------------------------------------------------- 数据模拟

    def _simulate_unit1(self, store: DataStore, t: float) -> None:
        # 变压器温度 20~70℃ 缓慢波动（会周期性触发高限报警）
        temp = 45.0 + 25.0 * math.sin(t / 30.0)
        temp_regs = struct.unpack(">HH", struct.pack(">f", temp))
        # A 相电压 205~245V
        voltage = 225.0 + 20.0 * math.sin(t / 45.0)
        # A 相电流 8~16A
        current = 12.0 + 4.0 * math.sin(t / 17.0)
        # 频率 49.9~50.1Hz
        freq = 50.0 + 0.1 * math.sin(t / 23.0)
        # 功率因数 0.90~0.99
        pf = 0.945 + 0.045 * math.sin(t / 61.0)
        # 有功电能累计
        energy = 1234 + int(t / 3)

        with store.lock:
            store.holding[0], store.holding[1] = temp_regs
            store.holding[2] = int(round(voltage * 10)) & 0xFFFF
            store.holding[3] = int(round(current * 100)) & 0xFFFF
            store.holding[4] = int(round(freq * 100)) & 0xFFFF
            store.holding[5] = (energy >> 16) & 0xFFFF
            store.holding[6] = energy & 0xFFFF
            store.holding[7] = int(round(pf * 1000)) & 0xFFFF
            # holding[8] 为设定值，由主站写入，这里不改
            running = store.coils[0]
            store.coils[1] = running  # 运行指示跟随主开关
            store.discretes[0] = (int(t) % 40) < 5  # 柜门周期性打开
            store.input[0] = 0x0001 | (0x0002 if running else 0) | (0x0004 if store.discretes[0] else 0)

    def _simulate_unit2(self, store: DataStore, t: float) -> None:
        running = store.coils[0]
        speed = 1600 + int(600 * math.sin(t / 12.0)) if running else 0
        with store.lock:
            store.holding[0] = max(0, min(3000, speed))
            store.input[0] = 1 if running else 0

    # ------------------------------------------------- SST 演示（unit 11/12/13）
    #
    # 波形缓冲区就是 DSP 里的一段保持寄存器：上位机一次性成块读回来即可画示波器。
    # 采样间隔由工程点位配置（sample_interval_us）描述，仿真器这里按 50Hz 工频生成：
    #   200 点 / 100us = 20ms = 正好一个工频周期

    @staticmethod
    def _write_wave(
        store: DataStore,
        base: int,
        count: int,
        samples_per_cycle: int,
        phase: float,
        amplitude: float,
        dc: float = 0.0,
        ripple: float = 0.0,
    ) -> None:
        for index in range(count):
            angle = 2.0 * math.pi * index / samples_per_cycle + phase
            value = amplitude * math.sin(angle) + dc
            if ripple:
                value += ripple * math.sin(2.0 * math.pi * index / 7.0)
            store.holding[base + index] = int(max(-32768, min(32767, round(value)))) & 0xFFFF

    def _simulate_unit11(self, store: DataStore, t: float) -> None:
        """级联整流 DSP：电网侧电参量 + 三相电压波形缓冲区。"""
        line_voltage = 10.0 + 0.15 * math.sin(t / 37.0)          # kV
        dc_bus = 750 + 20 * math.sin(t / 21.0)                    # V（行业常见的 750V 直流母线）
        power = 45.0 + 12.0 * math.sin(t / 13.0)                  # kW
        grid_current = 8.0 + 2.5 * math.sin(t / 13.0)             # A
        power_factor = 0.985 + 0.012 * math.sin(t / 29.0)
        temperature = 52.0 + 14.0 * math.sin(t / 90.0)            # ℃（会周期越限报警）
        running = store.coils[0]

        with store.lock:
            store.holding[0] = int(round(line_voltage * 10)) & 0xFFFF
            store.holding[1] = int(round(dc_bus)) & 0xFFFF
            store.holding[2] = int(round(power * 10)) & 0xFFFF
            store.holding[3] = int(round(temperature * 10)) & 0xFFFF
            store.holding[4] = int(round(grid_current * 10)) & 0xFFFF
            store.holding[5] = int(round(power_factor * 1000)) & 0xFFFF
            store.input[0] = 0x0001 | (0x0002 if running else 0)

        if not running:
            return
        # 相电压峰值 ≈ 230V * 1.414 ≈ 325V，scale=0.5 -> 原始值 650
        drift = (t * 0.02) % 1.0
        self._write_wave(store, 10, 200, 200, 2 * math.pi * drift, 650.0, ripple=6.0)
        self._write_wave(store, 210, 200, 200, 2 * math.pi * (drift - 1 / 3), 650.0, ripple=6.0)
        self._write_wave(store, 410, 200, 200, 2 * math.pi * (drift - 2 / 3), 650.0, ripple=6.0)

    def _simulate_unit12(self, store: DataStore, t: float) -> None:
        """DC/DC DSP：输入/输出电参量 + 电感电流波形缓冲区。"""
        vin = 750 + 18 * math.sin(t / 21.0)
        vout = 750.0 + 5.0 * math.sin(t / 17.0)
        iout = 12.5 + 3.0 * math.sin(t / 11.0)
        freq = 20.0 + 0.3 * math.sin(t / 41.0)                    # kHz
        temperature = 61.0 + 22.0 * math.sin(t / 75.0)            # ℃（会周期越限报警）
        enabled = store.coils[0]

        with store.lock:
            store.holding[0] = int(round(vin * 10)) & 0xFFFF
            store.holding[1] = int(round(vout * 10)) & 0xFFFF
            store.holding[2] = int(round(iout * 100)) & 0xFFFF
            store.holding[3] = int(round(freq * 10)) & 0xFFFF
            store.holding[4] = int(round(temperature * 10)) & 0xFFFF

        if not enabled:
            return
        # 电感电流：三角波纹波，scale=0.01 -> 原始值 ≈ 1000 表示 10A
        count = 128
        drift = (t * 0.05) % 1.0
        for index in range(count):
            angle = 2.0 * math.pi * index / count + 2.0 * math.pi * drift
            value = 1000.0 * math.sin(angle) + 260.0 * math.sin(6.0 * angle)
            store.holding[10 + index] = int(max(-32768, min(32767, round(value)))) & 0xFFFF

    def _simulate_unit13(self, store: DataStore, t: float) -> None:
        """计量单元：输出功率、效率、累计电量。"""
        power = 9.6 + 1.6 * math.sin(t / 13.0)      # kW
        efficiency = 97.5 + 1.2 * math.sin(t / 53.0)  # %
        energy = 5231 + int(t / 4)                  # kWh
        with store.lock:
            store.holding[0] = int(round(power * 10)) & 0xFFFF
            store.holding[1] = int(round(efficiency * 100)) & 0xFFFF
            store.holding[2] = (energy >> 16) & 0xFFFF
            store.holding[3] = energy & 0xFFFF

    def _simulate_loop(self, freeze: bool) -> None:
        while not self._stop.is_set():
            t = 0.0 if freeze else time.time() - self.start_time
            try:
                self._simulate_step(t)
            except Exception as exc:
                # 某个从站的仿真抛异常不能把整个仿真线程带走：线程一没，所有从站的
                # 数据会一起冻住，看起来像上位机卡死了。
                print(f"[sim] 仿真异常（t={t:.1f}s），已跳过本拍：{exc}")
            time.sleep(0.2)

    def _simulate_step(self, t: float) -> None:
        """刷新所有从站的一拍数据。"""
        self._simulate_unit1(self.units[1], t)
        self._simulate_unit2(self.units[2], t)
        self._simulate_unit11(self.units[11], t)
        self._simulate_unit12(self.units[12], t)
        self._simulate_unit13(self.units[13], t)
        # 储能 / 光伏 / 电力系统：交给各示例模块自己刷新
        for unit, module in sample_pack.SIM_UNITS:
            module.simulate(self.units[unit], t)

    # --------------------------------------------------------- 服务

    def serve_forever(self, freeze: bool = False) -> None:
        threading.Thread(target=self._simulate_loop, args=(freeze,), daemon=True).start()

        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(16)
        units_text = ", ".join(str(unit) for unit in sorted(self.units))
        print(
            f"[sim] Modbus TCP 从站已启动 http://{self.host}:{self.port}"
            f"  (unit {units_text})"
        )
        print("[sim] Ctrl+C 退出")

        failures = 0
        try:
            while not self._stop.is_set():
                try:
                    conn, addr = self._server.accept()
                except OSError as exc:
                    if self._stop.is_set():
                        break
                    # 单次 accept 出错（被信号打断之类）不该让整个服务退出：
                    # 一退出新连接就全部连不上，只能重启软件才恢复。
                    failures += 1
                    if failures > 50:
                        print(f"[sim] accept 连续失败 {failures} 次，停止监听：{exc}")
                        break
                    time.sleep(0.1)
                    continue
                failures = 0
                with self._clients_lock:
                    self._clients.add(conn)
                threading.Thread(
                    target=self._handle_client, args=(conn, addr), daemon=True
                ).start()
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop.set()
        # 先断开已经连上的主站，再关监听套接字：这样"停止仿真器"等于设备真的掉线，
        # 上位机立刻能看到断线并开始重连（现场就是这么验证重连的）。
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for conn in clients:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass

    def _handle_client(self, conn: socket.socket, addr) -> None:
        conn.settimeout(30.0)
        try:
            while True:
                header = _recv_exact(conn, 7)
                if header is None:
                    return
                transaction, protocol, length, unit_id = struct.unpack(">HHHB", header)
                if protocol != 0 or length < 2:
                    return
                body = _recv_exact(conn, length - 1)
                if body is None:
                    return
                pdu = body
                response = self._dispatch(unit_id, pdu)
                if response is None:
                    return
                # MBAP 头固定 7 字节：事务号(2) + 协议号(2) + 长度域(2) + 从站号(1)
                # 长度域 = 从站号(1) + PDU 长度
                payload = (
                    struct.pack(">HHH", transaction, 0, len(response) + 1)
                    + bytes([unit_id])
                    + response
                )
                conn.sendall(payload)
                if self.verbose:
                    print(f"[sim] {addr[0]} unit={unit_id} RX={pdu.hex(' ')} TX={response.hex(' ')}")
        except (OSError, struct.error):
            pass
        finally:
            with self._clients_lock:
                self._clients.discard(conn)
            try:
                conn.close()
            except OSError:
                pass

    def _dispatch(self, unit_id: int, pdu: bytes) -> bytes | None:
        if not pdu:
            return None
        fc = pdu[0]
        store = self.units.get(unit_id)
        if store is None:
            return bytes([fc | 0x80, EXC_ILLEGAL_ADDRESS])

        try:
            if fc in (FC_READ_COILS, FC_READ_DISCRETE):
                address, count = struct.unpack(">HH", pdu[1:5])
                if not 1 <= count <= 2000:
                    return bytes([fc | 0x80, EXC_ILLEGAL_VALUE])
                if address + count > SIZE:
                    return bytes([fc | 0x80, EXC_ILLEGAL_ADDRESS])
                bits = store.read_bits(
                    "coil" if fc == FC_READ_COILS else "discrete", address, count
                )
                return bytes([fc, (count + 7) // 8]) + _pack_bits(bits)

            if fc in (FC_READ_HOLDING, FC_READ_INPUT):
                address, count = struct.unpack(">HH", pdu[1:5])
                if not 1 <= count <= 125:
                    return bytes([fc | 0x80, EXC_ILLEGAL_VALUE])
                if address + count > SIZE:
                    return bytes([fc | 0x80, EXC_ILLEGAL_ADDRESS])
                regs = store.read_registers(
                    "holding" if fc == FC_READ_HOLDING else "input", address, count
                )
                body = b"".join(struct.pack(">H", r) for r in regs)
                return bytes([fc, len(body)]) + body

            if fc == FC_WRITE_COIL:
                address, value = struct.unpack(">HH", pdu[1:5])
                if value not in (0x0000, 0xFF00):
                    return bytes([fc | 0x80, EXC_ILLEGAL_VALUE])
                store.write_bits(address, [value == 0xFF00])
                return pdu[:5]

            if fc == FC_WRITE_REGISTER:
                address, value = struct.unpack(">HH", pdu[1:5])
                store.write_registers(address, [value])
                return pdu[:5]

            if fc == FC_WRITE_COILS:
                address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
                bits = _unpack_bits(pdu[6 : 6 + byte_count], count)
                store.write_bits(address, bits)
                return pdu[:5]

            if fc == FC_WRITE_REGISTERS:
                address, count, byte_count = struct.unpack(">HHB", pdu[1:6])
                if byte_count != count * 2:
                    return bytes([fc | 0x80, EXC_ILLEGAL_VALUE])
                regs = list(struct.unpack(f">{count}H", pdu[6 : 6 + count * 2]))
                store.write_registers(address, regs)
                return pdu[:5]

            return bytes([fc | 0x80, EXC_ILLEGAL_FUNCTION])
        except struct.error:
            return bytes([fc | 0x80, EXC_ILLEGAL_VALUE])


# --------------------------------------------------------------- 工具函数


def _recv_exact(conn: socket.socket, size: int) -> bytes | None:
    buffer = b""
    while len(buffer) < size:
        chunk = conn.recv(size - len(buffer))
        if not chunk:
            return None
        buffer += chunk
    return buffer


def _pack_bits(bits: list[bool]) -> bytes:
    out = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            out[index // 8] |= 1 << (index % 8)
    return bytes(out)


def _unpack_bits(raw: bytes, count: int) -> list[bool]:
    return [bool(raw[i // 8] & (1 << (i % 8))) for i in range(count)]


def main() -> None:
    parser = argparse.ArgumentParser(description="Modbus TCP 从站仿真器")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5020)
    parser.add_argument("--verbose", action="store_true", help="打印原始报文")
    parser.add_argument("--freeze", action="store_true", help="数值固定不变化")
    args = parser.parse_args()

    sim = ModbusTcpSimulator(args.host, args.port, args.verbose)
    try:
        sim.serve_forever(freeze=args.freeze)
    except KeyboardInterrupt:
        print("\n[sim] 退出")
        sim.stop()


if __name__ == "__main__":
    main()
