"""Modbus TCP / RTU 驱动。

支持链路：``tcp``、``rtu``（串口）、``rtu_over_tcp``（串口服务器/透传）。

实现要点：
* 批量合并读 —— 按寄存器类型分组、按地址排序，把相邻地址合并成一次请求，
  避免一个点位一次请求导致通信效率极低；
* 单个批次失败不影响其余批次；
* 兼容 pymodbus 3.6 ~ 3.11 的 API 差异（``slave`` / ``device_id`` 参数改名、
  framer 导入路径变化），在运行时按签名自适应。
"""

from __future__ import annotations

import inspect
from typing import Any, Iterable

from .. import serial_ports
from ..codec import registers_to_value, value_to_registers
from ..models import BIT_REGISTERS, DeviceDef, PointDef, word_count
from .base import (
    ChannelDriver,
    DriverError,
    OptionField,
    PointResult,
    register_driver,
)

try:
    from pymodbus.client import ModbusSerialClient, ModbusTcpClient

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - 取决于运行环境
    ModbusSerialClient = None  # type: ignore[assignment]
    ModbusTcpClient = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)


# --------------------------------------------------------------- 兼容工具


def _signature_params(func) -> dict[str, inspect.Parameter]:
    try:
        return dict(inspect.signature(func).parameters)
    except (TypeError, ValueError):
        return {}


def _filter_kwargs(func, kwargs: dict[str, Any]) -> dict[str, Any]:
    """只保留目标函数认识的参数，屏蔽各版本签名差异。"""
    params = _signature_params(func)
    if not params:
        return kwargs
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


def _unit_kwargs(func, unit_id: int, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    """从站号参数在不同版本叫 slave / device_id / unit。"""
    params = _signature_params(func)
    for name in ("device_id", "slave", "unit"):
        if name in params:
            return {name: unit_id}
    return dict(fallback or {})


def _check_response(response, what: str) -> Any:
    if response is None:
        raise DriverError(f"{what} 无响应")
    is_error = getattr(response, "isError", None)
    if callable(is_error) and is_error():
        raise DriverError(f"{what} 返回异常响应: {response}")
    return response


def _rtu_framer():
    """取 RTU 帧解析器，兼容新旧导入路径。"""
    try:
        from pymodbus.framer import FramerType  # pymodbus >= 3.7

        return FramerType.RTU
    except Exception:
        pass
    try:
        from pymodbus.transaction import ModbusRtuFramer  # pymodbus <= 3.6

        return ModbusRtuFramer
    except Exception:
        return None


# --------------------------------------------------------------- 批次规划

#: 读函数按寄存器类型
READ_FUNCS = {
    "holding": "read_holding_registers",
    "input": "read_input_registers",
    "coil": "read_coils",
    "discrete": "read_discrete_inputs",
}

#: 点位「寄存器区」列的可选项与中文名
POINT_AREAS = [
    ("holding", "保持寄存器"),
    ("input", "输入寄存器"),
    ("coil", "线圈"),
    ("discrete", "离散输入"),
]


def plan_batches(
    points: list[PointDef],
    max_regs: int = 120,
    max_gap: int = 8,
) -> list[tuple[str, int, int, list[tuple[PointDef, int]]]]:
    """把点位合并成若干次批量读。

    返回 ``[(寄存器类型, 起始地址, 数量, [(点位, 相对偏移寄存器数), ...]), ...]``

    合并规则：同一寄存器类型、地址升序排列后，只要「地址空洞 <= max_gap」
    且「跨度 <= max_regs」就并入同一请求。

    **数组/块点位（录波）不参与合并，各自单独成批**：
    一段波形往往是连续几百个寄存器，混进合并批里一旦失败会连累旁边的标量点位。
    """
    batches: list[tuple[str, int, int, list[tuple[PointDef, int]]]] = []

    by_register: dict[str, list[PointDef]] = {}
    arrays: list[PointDef] = []
    for p in points:
        if p.register not in READ_FUNCS:
            continue
        if p.is_array:
            arrays.append(p)
        else:
            by_register.setdefault(p.register, []).append(p)

    for point in arrays:
        batches.append(
            (point.register, max(0, int(point.address)), point.span, [(point, 0)])
        )

    for register, group in by_register.items():
        items: list[tuple[int, int, PointDef]] = []
        for p in group:
            width = 1 if p.register in BIT_REGISTERS else word_count(p.data_type)
            start = max(0, int(p.address))
            items.append((start, width, p))
        items.sort(key=lambda it: it[0])

        cursor_start: int | None = None
        cursor_end = 0
        members: list[tuple[int, int, PointDef]] = []

        def flush(start: int | None, end: int, chunk: list[tuple[int, int, PointDef]]) -> None:
            if start is None or not chunk:
                return
            offset_map = [(p, a - start) for a, _w, p in chunk]
            batches.append((register, start, end - start, offset_map))

        for start, width, point in items:
            if cursor_start is None:
                cursor_start, cursor_end, members = start, start + width, [(start, width, point)]
                continue
            new_end = max(cursor_end, start + width)
            if (start - cursor_end) <= max_gap and (new_end - cursor_start) <= max_regs:
                cursor_end = new_end
                members.append((start, width, point))
            else:
                flush(cursor_start, cursor_end, members)
                cursor_start, cursor_end, members = start, start + width, [(start, width, point)]
        flush(cursor_start, cursor_end, members)

    return batches


# --------------------------------------------------------------- 驱动实现


@register_driver
class ModbusDriver(ChannelDriver):
    """Modbus 主站驱动（一个通道 = 一条 TCP 连接或一个串口）。"""

    driver_name = "modbus"

    @classmethod
    def point_areas(cls) -> list[tuple[str, str]]:
        return list(POINT_AREAS)
    link_types = ("tcp", "rtu", "rtu_over_tcp")
    description = "Modbus 主站：TCP / RTU 串口 / RTU over TCP，支持线圈、离散输入、保持寄存器、输入寄存器"
    multi_device = True

    def __init__(self, channel) -> None:
        super().__init__(channel)
        self._client = None

    # -------------------------------------------------- 参数定义

    @classmethod
    def option_schema(cls, link: str) -> list[OptionField]:
        common = [
            OptionField("timeout", "超时(秒)", "float", 1.0, minimum=0.05, maximum=30.0),
            OptionField(
                "max_regs_per_read",
                "单次最大寄存器数",
                "int",
                120,
                minimum=1,
                maximum=125,
                hint="Modbus 标准上限 125，留余量取 120",
            ),
            OptionField(
                "max_gap",
                "地址空洞阈值",
                "int",
                8,
                minimum=0,
                maximum=64,
                hint="两个点位地址间隔不超过该值时合并为一次读取",
            ),
            OptionField("retry", "失败重试次数", "int", 1, minimum=0, maximum=5),
        ]
        if link == "rtu":
            return [
                OptionField(
                    "port",
                    "串口号",
                    "port",
                    serial_ports.DEFAULT_PORT,
                    hint="点开下拉会重新检测系统上的串口，也可以手打（如 COM3 或 /dev/ttyUSB0）",
                ),
                OptionField(
                    "baudrate",
                    "波特率",
                    "choice",
                    serial_ports.DEFAULT_BAUDRATE,
                    choices=serial_ports.BAUDRATES,
                    hint="要和设备侧一致；最常见的默认是 9600",
                ),
                OptionField(
                    "bytesize",
                    "数据位",
                    "choice",
                    8,
                    choices=(5, 6, 7, 8),
                    hint="几乎都是 8 位",
                ),
                OptionField(
                    "parity",
                    "校验位",
                    "choice",
                    serial_ports.DEFAULT_PARITY,
                    choices=("N", "E", "O"),
                    choice_labels=(
                        serial_ports.PARITY_LABELS["N"],
                        serial_ports.PARITY_LABELS["E"],
                        serial_ports.PARITY_LABELS["O"],
                    ),
                    hint="默认 9600 8E1（偶校验）：Modbus RTU 现场最常用的组合",
                ),
                OptionField("stopbits", "停止位", "choice", 1, choices=(1, 2), hint="几乎都是 1 位"),
                *common,
            ]
        return [
            OptionField("host", "IP 地址", "str", "127.0.0.1"),
            OptionField("port", "端口", "int", 502, minimum=1, maximum=65535),
            *common,
        ]

    # -------------------------------------------------- 生命周期

    def open(self) -> None:
        if _IMPORT_ERROR:
            raise DriverError(f"pymodbus 不可用：{_IMPORT_ERROR}（请执行 pip install pymodbus）")

        self.close()
        opts = self.channel.options or {}
        link = self.channel.link
        timeout = float(opts.get("timeout", 1.0))

        if link == "rtu":
            kwargs: dict[str, Any] = {
                "port": opts.get("port", serial_ports.DEFAULT_PORT),
                "baudrate": int(opts.get("baudrate", serial_ports.DEFAULT_BAUDRATE)),
                "bytesize": int(opts.get("bytesize", 8)),
                "parity": str(opts.get("parity", serial_ports.DEFAULT_PARITY)),
                "stopbits": int(opts.get("stopbits", 1)),
                "timeout": timeout,
            }
            kwargs = _filter_kwargs(ModbusSerialClient.__init__, kwargs)
            client = ModbusSerialClient(**kwargs)
        else:
            kwargs = {
                "host": opts.get("host", "127.0.0.1"),
                "port": int(opts.get("port", 502)),
                "timeout": timeout,
            }
            if link == "rtu_over_tcp":
                framer = _rtu_framer()
                if framer is not None:
                    kwargs["framer"] = framer
            kwargs = _filter_kwargs(ModbusTcpClient.__init__, kwargs)
            client = ModbusTcpClient(**kwargs)

        if not client.connect():
            # 连不上时要把刚建出来的客户端也关掉：ModbusTcpClient 在 connect 失败后
            # 不会自己释放套接字，断线久了每 3 秒重试一次就会攒下一堆没关的 socket，
            # 最后新连接连不上，只能重启软件。
            try:
                client.close()
            except Exception:
                pass
            raise DriverError(f"连接失败：{self._endpoint_text()}")

        self._client = client
        self.emit_log("info", f"{self.channel.name} 已连接 {self._endpoint_text()}")

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    @property
    def is_open(self) -> bool:
        client = self._client
        if client is None:
            return False
        return bool(getattr(client, "connected", True))

    def _endpoint_text(self) -> str:
        opts = self.channel.options or {}
        if self.channel.link == "rtu":
            return f"{opts.get('port')}@{opts.get('baudrate')},{opts.get('parity')}{opts.get('bytesize')}{opts.get('stopbits')}"
        return f"{opts.get('host')}:{opts.get('port')}"

    # -------------------------------------------------- 读取

    def read_points(self, device: DeviceDef, points: Iterable[PointDef]) -> dict[str, PointResult]:
        points = list(points)
        results: dict[str, PointResult] = {
            p.name: PointResult(tag=p.name, ok=False, error="未执行") for p in points
        }
        if not points:
            return results
        if not self.is_open:
            raise DriverError("连接未建立")

        opts = self.channel.options or {}
        max_regs = int(opts.get("max_regs_per_read", 120))
        max_gap = int(opts.get("max_gap", 8))
        retry = int(opts.get("retry", 1))

        for register, start, count, members in plan_batches(points, max_regs, max_gap):
            payload = self._read_once(register, start, count, device, retry)
            if payload is None:
                for point, _off in members:
                    results[point.name] = PointResult(
                        tag=point.name, ok=False, error="读取失败"
                    )
                continue

            for point, offset in members:
                try:
                    if point.is_array:
                        results[point.name] = PointResult(
                            tag=point.name, value=_decode_array(point, payload, offset), ok=True
                        )
                        continue
                    width = 1 if point.register in BIT_REGISTERS else word_count(point.data_type)
                    chunk = payload[offset : offset + width]
                    if len(chunk) < width:
                        raise DriverError("响应长度不足")
                    value = registers_to_value(
                        list(chunk),
                        point.data_type,
                        point.word_order,
                        point.scale,
                        point.offset,
                    )
                    results[point.name] = PointResult(tag=point.name, value=value, ok=True)
                except Exception as exc:
                    results[point.name] = PointResult(
                        tag=point.name, ok=False, error=str(exc)
                    )
        return results

    def _read_once(
        self,
        register: str,
        start: int,
        count: int,
        device: DeviceDef,
        retry: int,
    ) -> list[int] | None:
        """读一批数据（必要时自动分成多次 Modbus 请求），返回原始值数组。

        数组/块点位常常一次要读几百个寄存器，而 Modbus 单次最多 125 个寄存器
        （线圈 2000 位），所以这里必须分块拼接。
        """
        func_name = READ_FUNCS.get(register)
        if func_name is None:
            return None

        options = self.channel.options or {}
        if register in BIT_REGISTERS:
            limit = 2000
        else:
            limit = max(1, min(int(options.get("max_regs_per_read", 120)), 125))

        collected: list[int] = []
        offset = 0
        while offset < count:
            chunk = min(limit, count - offset)
            part = self._read_frame(func_name, register, start + offset, chunk, device, retry)
            if part is None:
                return None
            collected.extend(part)
            offset += chunk
        return collected

    def _read_frame(
        self,
        func_name: str,
        register: str,
        start: int,
        count: int,
        device: DeviceDef,
        retry: int,
    ) -> list[int] | None:
        """读单个 Modbus 请求，返回原始寄存器/位数组；失败返回 None。"""
        last_error = ""
        for attempt in range(retry + 1):
            try:
                func = getattr(self._client, func_name)
                kwargs = _unit_kwargs(func, device.unit_id, {"slave": device.unit_id})
                response = func(start, count=count, **kwargs)
                response = _check_response(response, f"{func_name}({start},{count})")

                if register in BIT_REGISTERS:
                    data = [1 if b else 0 for b in response.bits[:count]]
                    self.emit_frame(
                        "RX",
                        f"unit={device.unit_id} {_fn_label(register)} {start}..{start + count - 1}"
                        f" -> {_brief(data)}",
                    )
                else:
                    data = [int(r) & 0xFFFF for r in response.registers[:count]]
                    self.emit_frame(
                        "RX",
                        f"unit={device.unit_id} {_fn_label(register)} {start}..{start + count - 1}"
                        f" -> {_brief([f'{r:04X}' for r in data])}",
                    )
                return data
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                if attempt < retry:
                    self.emit_log(
                        "warning", f"{device.name} {func_name} 重试 {attempt + 1}/{retry}：{last_error}"
                    )
                    self._reconnect()
                else:
                    self.emit_frame(
                        "RX",
                        f"unit={device.unit_id} {_fn_label(register)} {start}..{start + count - 1}"
                        f" -> 失败：{last_error}",
                    )

        self.emit_log("error", f"{device.name} 读取失败：{last_error}")
        return None

    def _reconnect(self) -> None:
        try:
            self.close()
            self.open()
        except Exception as exc:
            self.emit_log("warning", f"{self.channel.name} 重连失败：{exc}")

    # -------------------------------------------------- 写入

    def write_point(self, device: DeviceDef, point: PointDef, value: Any) -> None:
        if not self.is_open:
            raise DriverError("连接未建立")
        if not point.writable:
            raise DriverError(f"点位 {point.name} 未标记为可写")

        if point.register in BIT_REGISTERS:
            func = getattr(self._client, "write_coil")
            kwargs = _unit_kwargs(func, device.unit_id, {"slave": device.unit_id})
            self.emit_frame("TX", f"unit={device.unit_id} 写线圈 {point.address} = {1 if value else 0}")
            response = func(int(point.address), bool(value), **kwargs)
        else:
            regs = value_to_registers(
                value, point.data_type, point.word_order, point.scale, point.offset
            )
            if len(regs) == 1:
                func = getattr(self._client, "write_register")
                self.emit_frame("TX", f"unit={device.unit_id} 写寄存器 {point.address} = {regs[0]:04X}")
                response = func(int(point.address), int(regs[0]), **{**_unit_kwargs(func, device.unit_id, {"slave": device.unit_id})})
            else:
                func = getattr(self._client, "write_registers")
                self.emit_frame("TX", f"unit={device.unit_id} 写寄存器 {point.address}..{point.address + len(regs) - 1} = {' '.join(f'{r:04X}' for r in regs)}")
                response = func(int(point.address), [int(r) for r in regs], **{**_unit_kwargs(func, device.unit_id, {"slave": device.unit_id})})

        _check_response(response, f"写入 {point.name}")


def _fn_label(register: str) -> str:
    return {
        "holding": "读保持寄存器",
        "input": "读输入寄存器",
        "coil": "读线圈",
        "discrete": "读离散输入",
    }.get(register, register)


def _decode_array(point: PointDef, payload: list[int], offset: int) -> list:
    """把一段原始寄存器解码成工程量数组（录波用）。"""
    width = point.element_width
    count = max(1, point.count)
    values: list = []
    for index in range(count):
        start = offset + index * width
        chunk = payload[start : start + width]
        if len(chunk) < width:
            raise DriverError(f"响应长度不足：需要 {count * width} 个寄存器，只收到 {len(payload)} 个")
        values.append(
            registers_to_value(chunk, point.data_type, point.word_order, point.scale, point.offset)
        )
    return values


def _brief(items: list, head: int = 12) -> str:
    """报文内容过长时截断显示，避免通信监视页被一次块读刷屏。"""
    if len(items) <= head:
        return " ".join(str(i) for i in items)
    return " ".join(str(i) for i in items[:head]) + f" …(共 {len(items)} 个)"
