"""从机（Modbus server / slave）侧：把主站请求变成标准应答。

现场调试常遇到"这台设备要我们当从机"的情况（上位机、网关、PLC 来轮询我们），
这时要排查的其实是**我们的应答对不对**：功能码支不支持、地址范围、数量上限、
写值回显、异常码选哪个、从站号不匹配时该不该回。

这里的 :func:`handle_pdu` 就是"一台守规矩的从机"的判定逻辑（纯函数，可单测），
:class:`TcpSlaveServer` 再把它套上一个真的 TCP 服务，主站连上来就能联调。

约定（照 Modbus 规范，别改）：

* 读需求超范围 → 异常码 02；数量/字节数不合法 → 03；不支持的功能码 → 01；
* 异常应答的功能码**最高位置 1**，后面跟一个字节异常码；
* 从站号不匹配 → **一声不吭**（主站等超时），广播（站号 0）只写不答；
* 写成功按标准回显（写单个回请求原样；写多个回起始地址+数量）。
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from . import modbus_tools as mb

#: 支持的读/写功能码（从机端）
SUPPORTED_READ_BITS = (0x01, 0x02)
SUPPORTED_READ_REGISTERS = (0x03, 0x04)
SUPPORTED_WRITE_SINGLE = (0x05, 0x06)
SUPPORTED_WRITE_MULTIPLE = (0x0F, 0x10)
SUPPORTED_MASK_WRITE = 0x16
SUPPORTED_READ_WRITE = 0x17

#: 从站号 0 是广播：写入生效但不应答
BROADCAST_UNIT = 0

#: 异常码
ILLEGAL_FUNCTION = 0x01
ILLEGAL_DATA_ADDRESS = 0x02
ILLEGAL_DATA_VALUE = 0x03

#: 寄存器区名字（界面与说明共用）
BANK_NAMES = {
    "coils": "线圈（0x）",
    "discrete": "离散输入（1x）",
    "holding": "保持寄存器（4x）",
    "input": "输入寄存器（3x）",
}


@dataclass
class SlaveModel:
    """一台从机的数据区。

    四个区都按"地址 0 开始的一段连续区"处理：地址超出长度就当非法数据地址（异常码 02）。
    现场设备常见的做法也是"没实现的地址直接回 02"，而不是回 0。
    """

    unit_id: int = 1
    coils: list[int] = field(default_factory=lambda: [0] * 64)
    discrete: list[int] = field(default_factory=lambda: [0] * 64)
    holding: list[int] = field(default_factory=lambda: [0] * 100)
    input: list[int] = field(default_factory=lambda: [0] * 100)
    #: 允许主站写（写线圈/保持寄存器）。关掉就一律回异常码 02（当作只读设备）
    writable: bool = True

    @classmethod
    def uniform(cls, unit_id: int = 1, size: int = 100, fill: str = "zero") -> "SlaveModel":
        """造一台"整齐"的从机：``fill`` = ``zero`` / ``increment`` / 具体整数。"""
        size = max(1, int(size))

        def bank() -> list[int]:
            if fill == "increment":
                return [index & 0xFFFF for index in range(size)]
            if fill == "zero":
                return [0] * size
            try:
                value = int(fill, 0) if isinstance(fill, str) else int(fill)
            except (TypeError, ValueError):
                value = 0
            return [value & 0xFFFF] * size

        return cls(
            unit_id=int(unit_id),
            coils=[0] * size,
            discrete=[0] * size,
            holding=bank(),
            input=bank(),
        )

    def resize(self, size: int, fill: str = "zero") -> None:
        """重设四个区的长度（改长度会丢掉原来的值，界面会先确认）。"""
        fresh = SlaveModel.uniform(self.unit_id, size, fill)
        self.coils = fresh.coils
        self.discrete = fresh.discrete
        self.holding = fresh.holding
        self.input = fresh.input


@dataclass
class SlaveReply:
    """从机对一帧请求的结论。"""

    #: 应答功能码（异常应答已置最高位）；静默不应答时保留请求功能码
    function: int = 0
    #: 应答 PDU（功能码 + 数据）；静默不应答时为空
    pdu: bytes = b""
    #: 异常码（正常应答为 None）
    exception: int | None = None
    #: 一句话说明（界面与日志用）
    summary: str = ""
    #: 多行明细
    detail: list[str] = field(default_factory=list)
    #: 从站号不匹配 / 广播：故意不应答
    silent: bool = False
    #: 这次请求改动了数据区
    written: bool = False
    address: int | None = None
    quantity: int | None = None
    values: list[int] = field(default_factory=list)

    @property
    def is_exception(self) -> bool:
        return self.exception is not None


def _exception(function: int, code: int, summary: str, **kwargs: Any) -> SlaveReply:
    detail = kwargs.pop("detail", [])
    detail = list(detail) + [f"异常码 0x{code:02X}：{mb.exception_text(code)}"]
    return SlaveReply(
        function=(function | 0x80) & 0xFF,
        pdu=bytes([(function | 0x80) & 0xFF, code]),
        exception=code,
        summary=summary,
        detail=detail,
        **kwargs,
    )


def _u16(raw: bytes, offset: int) -> int:
    return struct.unpack(">H", raw[offset:offset + 2])[0]


def _bits_payload(bits: Sequence[int]) -> bytes:
    packed = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            packed[index // 8] |= 1 << (index % 8)
    return bytes([len(packed)]) + bytes(packed)


def _registers_payload(registers: Sequence[int]) -> bytes:
    body = b"".join(struct.pack(">H", int(value) & 0xFFFF) for value in registers)
    return bytes([len(body)]) + body


def _bank_of(model: SlaveModel, function: int) -> tuple[list[int] | None, str]:
    """功能码 -> 它作用的数据区（读与写都要用同一张表）。"""
    if function in (0x01, 0x05, 0x0F):
        return model.coils, "coils"
    if function == 0x02:
        return model.discrete, "discrete"
    if function in (0x03, 0x06, 0x10, SUPPORTED_MASK_WRITE, SUPPORTED_READ_WRITE):
        return model.holding, "holding"
    if function == 0x04:
        return model.input, "input"
    return None, ""


def handle_pdu(pdu: bytes, model: SlaveModel) -> SlaveReply:
    """把一帧**请求 PDU**（功能码 + 数据，不含从站号/CRC/MBAP）交给从机处理。

    纯函数：只读 ``model`` 并在需要时写它；所有异常都变成 :class:`SlaveReply`，
    不会抛 —— 服务端不该因为一帧坏报文就断线。
    """
    raw = bytes(pdu)
    if not raw:
        return SlaveReply(summary="空帧：没有功能码", detail=["没有可解析的 PDU"])
    function = raw[0]
    data = raw[1:]

    if function in SUPPORTED_READ_BITS + SUPPORTED_READ_REGISTERS:
        return _read(function, data, model)
    if function in SUPPORTED_WRITE_SINGLE:
        return _write_single(function, data, model)
    if function in SUPPORTED_WRITE_MULTIPLE:
        return _write_multiple(function, data, model)
    if function == SUPPORTED_MASK_WRITE:
        return _mask_write(data, model)
    if function == SUPPORTED_READ_WRITE:
        return _read_write(data, model)
    return _exception(
        function, ILLEGAL_FUNCTION,
        f"不支持的功能码 0x{function:02X}（{mb.function_name(function)}）",
        detail=[f"本从机支持：01/02/03/04/05/06/0F/10/16/17"],
    )


# --------------------------------------------------------------- 读


def _read(function: int, data: bytes, model: SlaveModel) -> SlaveReply:
    if len(data) < 4:
        return _exception(function, ILLEGAL_DATA_VALUE, "读请求长度不足（要 4 字节：地址+数量）")
    address = _u16(data, 0)
    quantity = _u16(data, 2)
    bank, bank_key = _bank_of(model, function)
    assert bank is not None
    limit = mb.MAX_READ_BITS if function in SUPPORTED_READ_BITS else mb.MAX_READ_REGISTERS
    if not 1 <= quantity <= limit:
        return _exception(
            function, ILLEGAL_DATA_VALUE,
            f"数量 {quantity} 超出范围（1~{limit}）",
            address=address, quantity=quantity,
        )
    if address + quantity > len(bank):
        return _exception(
            function, ILLEGAL_DATA_ADDRESS,
            f"地址 {address}+{quantity} 超出 {BANK_NAMES[bank_key]} 范围（共 {len(bank)} 个）",
            address=address, quantity=quantity,
            detail=[f"设备只有 0~{len(bank) - 1}"],
        )
    values = [int(value) for value in bank[address:address + quantity]]
    payload = _bits_payload(values) if function in SUPPORTED_READ_BITS else _registers_payload(values)
    return SlaveReply(
        function=function,
        pdu=bytes([function]) + payload,
        summary=f"{mb.function_name(function)} 应答：{quantity} 个（地址 {address}）",
        detail=[
            f"地址 {address}~{address + quantity - 1}，共 {quantity} 个",
            f"字节数 {payload[0]}",
            f"数据：{_fmt(values)}",
        ],
        address=address,
        quantity=quantity,
        values=values,
    )


# --------------------------------------------------------------- 写单个


def _write_single(function: int, data: bytes, model: SlaveModel) -> SlaveReply:
    if len(data) < 4:
        return _exception(function, ILLEGAL_DATA_VALUE, "写单个请求长度不足（要 4 字节：地址+值）")
    address = _u16(data, 0)
    value = _u16(data, 2)
    bank, bank_key = _bank_of(model, function)
    assert bank is not None
    if not model.writable:
        return _exception(function, ILLEGAL_DATA_ADDRESS,
                          f"本从机设为只读，拒绝写 {BANK_NAMES[bank_key]}",
                          address=address)
    if address >= len(bank):
        return _exception(function, ILLEGAL_DATA_ADDRESS,
                          f"地址 {address} 超出 {BANK_NAMES[bank_key]} 范围（共 {len(bank)} 个）",
                          address=address)
    if function == 0x05:
        if value not in (0xFF00, 0x0000):
            return _exception(function, ILLEGAL_DATA_VALUE,
                              f"线圈值 0x{value:04X} 不合法（只能是 0xFF00 或 0x0000）",
                              address=address)
        bank[address] = 1 if value == 0xFF00 else 0
    else:
        bank[address] = value & 0xFFFF
    return SlaveReply(
        function=function,
        pdu=bytes([function]) + data[:4],  # 标准要求原样回显
        summary=f"{mb.function_name(function)} 应答：回显地址 {address} 值 0x{value:04X}",
        detail=[f"已写入 {BANK_NAMES[bank_key]} 地址 {address} = {bank[address]}"],
        written=True,
        address=address,
        values=[int(bank[address])],
    )


# --------------------------------------------------------------- 写多个


def _write_multiple(function: int, data: bytes, model: SlaveModel) -> SlaveReply:
    if len(data) < 5:
        return _exception(function, ILLEGAL_DATA_VALUE,
                          "写多个请求长度不足（要 起始地址+数量+字节数）")
    address = _u16(data, 0)
    quantity = _u16(data, 2)
    byte_count = data[4]
    body = data[5:]
    bank, bank_key = _bank_of(model, function)
    assert bank is not None
    if not model.writable:
        return _exception(function, ILLEGAL_DATA_ADDRESS,
                          f"本从机设为只读，拒绝写 {BANK_NAMES[bank_key]}", address=address)

    if function == 0x0F:
        limit = mb.MAX_WRITE_BITS
        if not 1 <= quantity <= limit:
            return _exception(function, ILLEGAL_DATA_VALUE,
                              f"线圈数量 {quantity} 超出范围（1~{limit}）",
                              address=address, quantity=quantity)
        if byte_count != (quantity + 7) // 8:
            return _exception(function, ILLEGAL_DATA_VALUE,
                              f"字节数 {byte_count} 与数量 {quantity} 不匹配"
                              f"（应为 {(quantity + 7) // 8}）",
                              address=address, quantity=quantity)
    else:
        limit = mb.MAX_WRITE_REGISTERS
        if not 1 <= quantity <= limit:
            return _exception(function, ILLEGAL_DATA_VALUE,
                              f"寄存器数量 {quantity} 超出范围（1~{limit}）",
                              address=address, quantity=quantity)
        if byte_count != quantity * 2:
            return _exception(function, ILLEGAL_DATA_VALUE,
                              f"字节数 {byte_count} 与数量 {quantity} 不匹配"
                              f"（应为 {quantity * 2}）",
                              address=address, quantity=quantity)

    if len(body) < byte_count:
        return _exception(function, ILLEGAL_DATA_VALUE,
                          f"数据只有 {len(body)} 字节，声明的字节数是 {byte_count}",
                          address=address, quantity=quantity)
    if address + quantity > len(bank):
        return _exception(function, ILLEGAL_DATA_ADDRESS,
                          f"地址 {address}+{quantity} 超出 {BANK_NAMES[bank_key]} 范围"
                          f"（共 {len(bank)} 个）",
                          address=address, quantity=quantity,
                          detail=[f"设备只有 0~{len(bank) - 1}"])

    values: list[int] = []
    if function == 0x0F:
        for index in range(quantity):
            bit = (body[index // 8] >> (index % 8)) & 1
            values.append(bit)
            bank[address + index] = bit
    else:
        for index in range(quantity):
            value = _u16(body, index * 2)
            values.append(value)
            bank[address + index] = value

    return SlaveReply(
        function=function,
        pdu=bytes([function]) + struct.pack(">HH", address, quantity),
        summary=f"{mb.function_name(function)} 应答：写入 {quantity} 个（地址 {address}）",
        detail=[
            f"地址 {address}~{address + quantity - 1}",
            f"写入值：{_fmt(values)}",
        ],
        written=True,
        address=address,
        quantity=quantity,
        values=values,
    )


# --------------------------------------------------------------- 屏蔽写 / 读写


def _mask_write(data: bytes, model: SlaveModel) -> SlaveReply:
    function = SUPPORTED_MASK_WRITE
    if len(data) < 6:
        return _exception(function, ILLEGAL_DATA_VALUE,
                          "屏蔽写请求长度不足（要 地址+AND 掩码+OR 掩码）")
    address = _u16(data, 0)
    and_mask = _u16(data, 2)
    or_mask = _u16(data, 4)
    if not model.writable:
        return _exception(function, ILLEGAL_DATA_ADDRESS, "本从机设为只读，拒绝写保持寄存器",
                          address=address)
    if address >= len(model.holding):
        return _exception(function, ILLEGAL_DATA_ADDRESS,
                          f"地址 {address} 超出保持寄存器范围（共 {len(model.holding)} 个）",
                          address=address)
    current = model.holding[address]
    result = (current & and_mask) | (or_mask & ~and_mask & 0xFFFF)
    model.holding[address] = result & 0xFFFF
    return SlaveReply(
        function=function,
        pdu=bytes([function]) + data[:6],
        summary=f"屏蔽写 应答：地址 {address} 0x{current:04X} → 0x{result & 0xFFFF:04X}",
        detail=[
            f"AND 掩码 0x{and_mask:04X}，OR 掩码 0x{or_mask:04X}",
            f"原值 0x{current:04X} → 新值 0x{result & 0xFFFF:04X}",
        ],
        written=True,
        address=address,
        values=[int(result & 0xFFFF)],
    )


def _read_write(data: bytes, model: SlaveModel) -> SlaveReply:
    function = SUPPORTED_READ_WRITE
    if len(data) < 9:
        return _exception(function, ILLEGAL_DATA_VALUE,
                          "读写多寄存器请求长度不足（读地址/读数量/写地址/写数量/字节数）")
    read_address = _u16(data, 0)
    read_quantity = _u16(data, 2)
    write_address = _u16(data, 4)
    write_quantity = _u16(data, 6)
    byte_count = data[8]
    body = data[9:]

    if not 1 <= read_quantity <= mb.MAX_READ_REGISTERS:
        return _exception(function, ILLEGAL_DATA_VALUE,
                          f"读数量 {read_quantity} 超出范围（1~{mb.MAX_READ_REGISTERS}）")
    if read_address + read_quantity > len(model.holding):
        return _exception(function, ILLEGAL_DATA_ADDRESS,
                          f"读地址 {read_address}+{read_quantity} 超出保持寄存器范围"
                          f"（共 {len(model.holding)} 个）")
    if not 1 <= write_quantity <= mb.MAX_WRITE_REGISTERS:
        return _exception(function, ILLEGAL_DATA_VALUE,
                          f"写数量 {write_quantity} 超出范围（1~{mb.MAX_WRITE_REGISTERS}）")
    if byte_count != write_quantity * 2 or len(body) < byte_count:
        return _exception(function, ILLEGAL_DATA_VALUE,
                          f"字节数 {byte_count} 与写数量 {write_quantity} 不匹配")
    if write_address + write_quantity > len(model.holding):
        return _exception(function, ILLEGAL_DATA_ADDRESS,
                          f"写地址 {write_address}+{write_quantity} 超出保持寄存器范围"
                          f"（共 {len(model.holding)} 个）")
    if not model.writable:
        return _exception(function, ILLEGAL_DATA_ADDRESS, "本从机设为只读，拒绝写保持寄存器")

    for index in range(write_quantity):
        model.holding[write_address + index] = _u16(body, index * 2)
    values = [int(value) for value in model.holding[read_address:read_address + read_quantity]]
    payload = _registers_payload(values)
    return SlaveReply(
        function=function,
        pdu=bytes([function]) + payload,
        summary=f"读写多寄存器：先写 {write_quantity} 个再读 {read_quantity} 个",
        detail=[
            f"写入 地址 {write_address}~{write_address + write_quantity - 1}",
            f"读回 地址 {read_address}~{read_address + read_quantity - 1}：{_fmt(values)}",
        ],
        written=True,
        address=read_address,
        quantity=read_quantity,
        values=values,
    )


def _fmt(values: Iterable[int], limit: int = 12) -> str:
    items = list(values)
    head = ", ".join(str(value) for value in items[:limit])
    return head + (f" …（共 {len(items)} 个）" if len(items) > limit else "")


# --------------------------------------------------------------- 整帧（含封包）


@dataclass
class SlaveExchange:
    """一次"收到请求 → 发出应答"的完整记录（服务端日志与界面表格用）。"""

    time: float = field(default_factory=time.time)
    peer: str = ""
    unit: int | None = None
    request: bytes = b""
    reply: bytes = b""
    function: int | None = None
    exception: int | None = None
    summary: str = ""
    silent: bool = False
    #: 这次请求动到的地址段（界面只刷这几行，别整表重画）
    address: int | None = None
    quantity: int | None = None

    @property
    def time_text(self) -> str:
        return time.strftime("%H:%M:%S", time.localtime(self.time))


@dataclass
class FrameReply:
    """``respond_frame`` 的结论：整帧应答 + 明细。"""

    mode: str = "tcp"
    request: bytes = b""
    reply: bytes = b""
    unit: int | None = None
    function: int | None = None
    exception: int | None = None
    summary: str = ""
    detail: list[str] = field(default_factory=list)
    error: str = ""
    silent: bool = False
    #: 这次请求动到的地址段（界面只刷这几行）
    address: int | None = None
    quantity: int | None = None

    @property
    def ok(self) -> bool:
        return not self.error and not self.silent

    @property
    def reply_hex(self) -> str:
        return mb.to_hex_text(self.reply)


def _tcp_header(transaction: int, unit: int, pdu: bytes) -> bytes:
    length = 1 + len(pdu)
    return struct.pack(">HHHB", transaction & 0xFFFF, 0, length, unit & 0xFF) + pdu


def respond_frame(frame: bytes, mode: str, model: SlaveModel,
                  *, transaction: int = 0) -> FrameReply:
    """按从机语义回一帧：RTU 自动算 CRC、TCP 沿用事务号并重算长度域。

    ``model.unit_id`` 与请求里的从站号不一致时不产生应答（``silent=True``）——
    这正是"两台从站挂同一条线上"时的真实行为，调试时看到"没反应"要能对上这一条。
    """
    kind = (mode or "").strip().lower()
    raw = bytes(frame)
    result = FrameReply(mode=kind if kind in ("rtu", "tcp") else "rtu", request=raw)
    if kind not in ("rtu", "tcp"):
        result.error = f"未知报文模式「{mode}」，只支持 rtu / tcp"
        return result
    if not raw:
        result.error = "没有数据：帧是空的"
        return result

    if kind == "rtu":
        if len(raw) < 4:
            result.error = "RTU 帧太短：至少 从站号+功能码+CRC 共 4 字节"
            return result
        ok, note = mb.check_crc(raw)
        if not ok:
            result.error = f"CRC 校验失败（{note}），真实从机不会应答这一帧"
            return result
        unit = raw[0]
        pdu = raw[1:-2]
    else:
        if len(raw) < mb.MBAP_HEADER_LEN + 1:
            result.error = "TCP 帧太短：至少 7 字节 MBAP 头 + 1 字节功能码"
            return result
        transaction, protocol, length, unit = struct.unpack(">HHHB", raw[:7])
        result.detail.append(f"事务号 {transaction}，协议号 {protocol}，长度域 {length}")
        if protocol != 0:
            result.error = f"协议号 {protocol} 不是 0（Modbus 专用），真实从站会丢弃这一帧"
            return result
        pdu = raw[7:]
        if length != 1 + len(pdu):
            result.detail.append(
                f"注意：长度域 {length} 与实际 {1 + len(pdu)} 不一致（按实际字节处理）"
            )

    result.unit = unit
    result.function = pdu[0] if pdu else None
    if unit != (model.unit_id & 0xFF) and unit != BROADCAST_UNIT:
        result.silent = True
        result.summary = f"从站号 {unit} ≠ 本机 {model.unit_id}：不应答（主站会等到超时）"
        return result

    reply = handle_pdu(pdu, model)
    result.exception = reply.exception
    result.summary = reply.summary
    result.address = reply.address
    result.quantity = reply.quantity
    result.detail.extend(reply.detail)
    if unit == BROADCAST_UNIT and reply.function in (
        SUPPORTED_WRITE_SINGLE + SUPPORTED_WRITE_MULTIPLE + (SUPPORTED_MASK_WRITE,)
    ):
        result.silent = True
        result.summary = "广播写：已生效但按规范不应答"
        return result
    if unit == BROADCAST_UNIT:
        result.silent = True
        result.summary = "播地址 0 的读请求：从机不应答"
        return result
    if not reply.pdu:
        result.error = reply.summary or "从机没有产生应答"
        return result

    if kind == "rtu":
        result.reply = mb.build_rtu(unit, reply.pdu[0], reply.pdu[1:])
    else:
        result.reply = _tcp_header(transaction, unit, reply.pdu)
    return result


# --------------------------------------------------------------- TCP 从机服务


class TcpSlaveServer:
    """一个只能用 TCP 的小从机服务：主站连上来轮询，它按 :class:`SlaveModel` 应答。

    只做 TCP 是有意的：串口（RTU）在调试时通常被主站/串口助手占着，
    想在中间插一台"假从机"还得改线；TCP 挂在同一网段就能顶替真设备。
    """

    def __init__(
        self,
        model: SlaveModel | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 1502,
        on_exchange: Callable[[SlaveExchange], None] | None = None,
    ) -> None:
        self.model = model or SlaveModel()
        self.host = host
        self.port = int(port)
        self.on_exchange = on_exchange
        self.requests = 0
        self.exceptions = 0
        self.last_error = ""
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._clients: list[socket.socket] = []
        self._lock = threading.RLock()

    # -------------------------------------------------- 生命周期
    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def actual_port(self) -> int:
        """实际绑定的端口（``port=0`` 时由系统挑，测试里用得到）。"""
        sock = self._sock
        if sock is None:
            return self.port
        try:
            return int(sock.getsockname()[1])
        except OSError:  # pragma: no cover - 套接字已关
            return self.port

    def start(self) -> int:
        """开始监听，返回真实端口。已在跑就直接返回当前端口。"""
        if self.running:
            return self.actual_port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((self.host, self.port))
            sock.listen(8)
            sock.settimeout(0.3)
        except OSError as error:
            sock.close()
            self.last_error = f"监听 {self.host}:{self.port} 失败：{error}"
            raise
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="modbus-slave", daemon=True)
        self._thread.start()
        return self.actual_port

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except OSError:
                pass
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    # -------------------------------------------------- 收发
    def _serve(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                return
            try:
                client, peer = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._lock:
                self._clients.append(client)
            threading.Thread(
                target=self._handle_client, args=(client, peer), name="modbus-slave-conn",
                daemon=True,
            ).start()

    def _handle_client(self, client: socket.socket, peer: tuple[str, int]) -> None:
        client.settimeout(0.5)
        buffer = b""
        address = f"{peer[0]}:{peer[1]}"
        try:
            while not self._stop.is_set():
                try:
                    chunk = client.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while len(buffer) >= mb.MBAP_HEADER_LEN + 1:
                    length = struct.unpack(">H", buffer[4:6])[0]
                    total = mb.MBAP_FIXED_LEN + length
                    if length <= 0 or total > 260:
                        buffer = b""  # 长度域坏了，丢掉等下一帧
                        break
                    if len(buffer) < total:
                        break
                    frame, buffer = buffer[:total], buffer[total:]
                    self._answer(client, address, frame)
        finally:
            try:
                client.close()
            except OSError:
                pass
            with self._lock:
                if client in self._clients:
                    self._clients.remove(client)

    def _answer(self, client: socket.socket, address: str, frame: bytes) -> None:
        reply = respond_frame(frame, "tcp", self.model)
        exchange = SlaveExchange(
            peer=address,
            unit=reply.unit,
            request=frame,
            reply=reply.reply,
            function=reply.function,
            exception=reply.exception,
            summary=reply.summary or reply.error,
            silent=reply.silent,
            address=reply.address,
            quantity=reply.quantity,
        )
        self.requests += 1
        if reply.exception is not None:
            self.exceptions += 1
        if reply.reply:
            try:
                client.sendall(reply.reply)
            except OSError as error:
                self.last_error = f"发送失败：{error}"
        if self.on_exchange is not None:
            try:
                self.on_exchange(exchange)
            except Exception:  # noqa: BLE001 - 日志回调出错不该断连接
                pass


__all__ = [
    "BANK_NAMES",
    "BROADCAST_UNIT",
    "FrameReply",
    "ILLEGAL_DATA_ADDRESS",
    "ILLEGAL_DATA_VALUE",
    "ILLEGAL_FUNCTION",
    "SlaveExchange",
    "SlaveModel",
    "SlaveReply",
    "TcpSlaveServer",
    "handle_pdu",
    "respond_frame",
]
