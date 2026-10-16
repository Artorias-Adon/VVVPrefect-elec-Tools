"""Modbus 报文的拼装、CRC 计算与解析。

与界面无关的纯字节运算，单独成模块是为了能用**公认的已知向量**把它钉死：
现场最让人抓瞎的两类问题就是「帧拼错了」和「CRC 算错了」，
把这段逻辑独立出来后可以脱离界面直接跑测试（见 ``tests/test_modbus_tools.py``）。

两种报文模式：

* **RTU**：``从站号(1) + 功能码(1) + 数据(n) + CRC(2)``。
  CRC 是 **低字节在前** 传输（高字节先发是错的，但肉眼很难发现），
  所以这里同时提供 :func:`crc16`（数值）与 :func:`crc16_bytes`（传输字节序）。
* **TCP**：``MBAP 头(7) + 功能码(1) + 数据(n)``，没有 CRC。
  MBAP = 事务号(2) + 协议号(2，恒 0) + 长度(2) + 从站号(1)，
  其中 **长度域 = 从站号(1) + PDU 长度**，不含 MBAP 头自身的 6 字节。
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from typing import Any, Sequence

# --------------------------------------------------------------- 常量表

#: CRC-16/MODBUS：反射算法，多项式 0xA001，初值 0xFFFF
CRC_POLYNOMIAL = 0xA001
CRC_INITIAL = 0xFFFF
CRC_NAME = "CRC-16/MODBUS"

#: MBAP 头的固定部分（事务号 2 + 协议号 2 + 长度 2），后面还有 1 字节从站号
MBAP_FIXED_LEN = 6
MBAP_HEADER_LEN = 7

#: Modbus 协议规定的单帧上限
MAX_PDU_LEN = 253
MAX_READ_BITS = 2000
MAX_READ_REGISTERS = 125
MAX_WRITE_BITS = 1968
MAX_WRITE_REGISTERS = 123

#: 功能码中文名。构造器只做 8 个常用码，解析侧认得更多，方便看厂家报文。
FUNCTION_NAMES: dict[int, str] = {
    0x01: "读线圈",
    0x02: "读离散输入",
    0x03: "读保持寄存器",
    0x04: "读输入寄存器",
    0x05: "写单个线圈",
    0x06: "写单个寄存器",
    0x07: "读异常状态",
    0x08: "诊断",
    0x0B: "取通信事件计数",
    0x0C: "取通信事件日志",
    0x0F: "写多个线圈",
    0x10: "写多个寄存器",
    0x11: "报告从站 ID",
    0x16: "屏蔽写寄存器",
    0x17: "读写多个寄存器",
}

#: 异常码含义。异常响应的功能码最高位置 1，紧跟一字节异常码。
EXCEPTION_CODES: dict[int, str] = {
    0x01: "非法功能码（从站不支持该功能）",
    0x02: "非法数据地址（地址超出从站范围）",
    0x03: "非法数据值（数量或数值超出允许范围）",
    0x04: "从站设备故障（执行时出现不可恢复错误）",
    0x05: "确认（从站已受理，处理需要较长时间）",
    0x06: "从站设备忙（正在处理长任务，稍后重试）",
    0x07: "否定确认（从站无法执行编程功能）",
    0x08: "存储奇偶校验错",
    0x0A: "网关路径不可用（网关配置错误或过载）",
    0x0B: "网关目标设备无响应",
}

READ_FUNCTIONS = (0x01, 0x02, 0x03, 0x04)
WRITE_SINGLE_FUNCTIONS = (0x05, 0x06)
WRITE_MULTIPLE_FUNCTIONS = (0x0F, 0x10)
COIL_FUNCTIONS = (0x01, 0x02, 0x05, 0x0F)

#: 十六进制文本里允许当分隔符的字符（现场粘贴的报文常带这些）
_SEPARATORS = " \t\r\n\u3000,，;；:：-|_"
_HEX_DIGITS = "0123456789abcdefABCDEF"


def function_name(function: int) -> str:
    """功能码的中文名；异常响应的功能码先摘掉最高位再查表。"""
    base = function & 0x7F if function & 0x80 else function
    return FUNCTION_NAMES.get(base, f"未知功能码 0x{base:02X}")


def exception_text(code: int) -> str:
    return EXCEPTION_CODES.get(code, f"未在标准异常码表里（0x{code:02X}）")


def is_exception_function(function: int) -> bool:
    """异常响应的功能码最高位置 1。"""
    return bool(function & 0x80)


# --------------------------------------------------------------- CRC


def crc16(data: bytes) -> int:
    """标准 Modbus CRC16：多项式 0xA001，初值 0xFFFF，逐位反射计算。"""
    crc = CRC_INITIAL
    for byte in bytes(data):
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ CRC_POLYNOMIAL
            else:
                crc >>= 1
    return crc & 0xFFFF


def crc16_bytes(data: bytes) -> bytes:
    """CRC 的传输字节序：Modbus 报文里 **低字节在前**。"""
    return crc16(data).to_bytes(2, "little")


def append_crc(data: bytes) -> bytes:
    """在数据段后面补 2 字节 CRC，得到完整 RTU 帧。"""
    raw = bytes(data)
    return raw + crc16_bytes(raw)


def check_crc(frame: bytes) -> tuple[bool, str]:
    """校验带 CRC 的完整帧，返回 ``(是否正确, 说明)``。"""
    raw = bytes(frame)
    if len(raw) < 2:
        return False, f"帧只有 {len(raw)} 字节，连 2 字节 CRC 都不够"
    if len(raw) < 4:
        # 从站号 + 功能码 + CRC 是最小 RTU 帧
        return False, f"帧只有 {len(raw)} 字节，不足最小 RTU 帧长 4 字节（从站号+功能码+CRC）"
    body, tail = raw[:-2], raw[-2:]
    expect = crc16_bytes(body)
    if tail == expect:
        return True, (
            f"CRC 正确：帧内 {to_hex_text(tail)}，按前 {len(body)} 字节算得 "
            f"{to_hex_text(expect)}（低字节在前）"
        )
    return False, (
        f"CRC 错误：帧内 {to_hex_text(tail)}，按前 {len(body)} 字节算得 "
        f"{to_hex_text(expect)}（低字节在前）"
    )


def crc_detail(data: bytes) -> str:
    """CRC 计算明细（现场跟厂家核对时逐项念给对方听）。

    界面里直接显示这段文本：写清楚用了哪套参数，避免"到底是 CRC-16/ARC
    还是 CRC-16/MODBUS"这种扯不清的问题。
    """
    raw = bytes(data)
    crc = crc16(raw)
    return "\n".join(
        [
            f"{CRC_NAME}：多项式 0x{CRC_POLYNOMIAL:04X}，初值 0x{CRC_INITIAL:04X}，逐位反射",
            f"参与计算 {len(raw)} 字节 → 结果 0x{crc:04X} → 传输时低字节在前："
            f"{to_hex_text(crc.to_bytes(2, 'little'))}",
        ]
    )


# --------------------------------------------------------------- 拼帧


def build_rtu(slave: int, function: int, payload: bytes = b"") -> bytes:
    """拼 RTU 帧：``从站号 + 功能码 + 数据 + CRC``（CRC 自动算）。"""
    body = bytes([_byte(slave, "从站号"), _byte(function, "功能码")]) + bytes(payload)
    _check_pdu_len(function, bytes(payload))
    return append_crc(body)


def build_tcp(transaction: int, unit: int, function: int, payload: bytes = b"") -> bytes:
    """拼 TCP 帧：``MBAP(7) + 功能码 + 数据``。

    长度域算的是 **从站号(1) + PDU 长度**，不含 MBAP 头自身的 6 字节：
    写成 6 或写成 PDU 长度，从站会按"长度域"截断报文，表现是"发出去没反应"
    但抓包看帧又"像是好的"。所以这里用 ``>HHHB`` 一次性拼出 6+1 字节，
    并且长度单独算，避免手工偏移写错。
    """
    _check_pdu_len(function, bytes(payload))
    pdu = bytes([_byte(function, "功能码")]) + bytes(payload)
    length = 1 + len(pdu)
    _u16(length, "长度域")
    header = struct.pack(
        ">HHHB",
        _u16(transaction, "事务号"),
        0,  # 协议号固定 0，Modbus 专用
        length,
        _byte(unit, "从站号"),
    )
    return header + pdu


def read_request(function: int, start: int, count: int) -> bytes:
    """读操作（0x01/0x02/0x03/0x04）的数据段：``起始地址(2) + 数量(2)``。

    返回的是 **数据段**（不含从站号、功能码、CRC），交给 :func:`build_rtu`
    或 :func:`build_tcp` 拼成完整报文。
    """
    if function not in READ_FUNCTIONS:
        raise ValueError(
            f"功能码 0x{function:02X} 不是读操作（只支持 0x01 读线圈、0x02 读离散输入、"
            "0x03 读保持寄存器、0x04 读输入寄存器）"
        )
    _u16(start, "起始地址")
    if function in (0x01, 0x02):
        limit, unit_text = MAX_READ_BITS, "位"
    else:
        limit, unit_text = MAX_READ_REGISTERS, "个寄存器"
    if not 1 <= int(count) <= limit:
        raise ValueError(f"数量 {count} 超出范围：0x{function:02X} 一次读 1~{limit} {unit_text}")
    return struct.pack(">HH", start, int(count))


def write_single(function: int, address: int, value: Any) -> bytes:
    """写单个（0x05 线圈 / 0x06 寄存器）的数据段：``地址(2) + 值(2)``。

    线圈值按标准转成 0xFF00（ON）/ 0x0000（OFF）—— 直接写 1 是最常见的错，
    从站通常直接回异常码 3。
    """
    if function not in WRITE_SINGLE_FUNCTIONS:
        raise ValueError(
            f"功能码 0x{function:02X} 不是写单个（只支持 0x05 写单个线圈、0x06 写单个寄存器）"
        )
    _u16(address, "地址")
    if function == 0x05:
        raw = _coil_value(value)
    else:
        raw = _u16(value, "寄存器值")
    return struct.pack(">HH", address, raw)


def write_multiple(function: int, start: int, values: Any) -> bytes:
    """写多个（0x0F 线圈 / 0x10 寄存器）的数据段。

    ``values`` 可以是：

    * 整数序列：0x0F 用 0/1，0x10 用 0~65535；
    * ``bytes``：0x0F 按位展开（每字节 8 个线圈，低位在前），
      0x10 按 16 位大端拆成寄存器（**字节数必须是偶数**，奇数没法拆）；
    * 字符串：``"1011"`` 这种 0/1 串、或 ``"0x0001, 2, 3"`` 这种列表文本。
    """
    if function == 0x0F:
        bits = _coil_bits(values)
        quantity = len(bits)
        if not 1 <= quantity <= MAX_WRITE_BITS:
            raise ValueError(
                f"写多个线圈数量 {quantity} 超出 Modbus 上限（一次 1~{MAX_WRITE_BITS} 个）"
            )
        packed = _pack_bits(bits)
        return struct.pack(">HHB", _u16(start, "起始地址"), quantity, len(packed)) + packed

    if function == 0x10:
        regs = _register_values(values)
        quantity = len(regs)
        if not 1 <= quantity <= MAX_WRITE_REGISTERS:
            raise ValueError(
                f"写多个寄存器数量 {quantity} 超出 Modbus 上限（一次 1~{MAX_WRITE_REGISTERS} 个）"
            )
        body = b"".join(struct.pack(">H", reg) for reg in regs)
        return struct.pack(">HHB", _u16(start, "起始地址"), quantity, len(body)) + body

    raise ValueError(
        f"功能码 0x{function:02X} 不是写多个（只支持 0x0F 写多个线圈、0x10 写多个寄存器）"
    )


# --------------------------------------------------------------- 文本辅助


def parse_hex_text(text: str) -> tuple[bytes, str]:
    """解析十六进制文本，返回 ``(字节串, 出错原因)``，成功时原因是空串。

    容忍现场粘贴的各种写法：空格、逗号、分号、冒号、短横线、换行、``0x`` 前缀、
    大小写混写、全角空格。
    """
    if not text or not text.strip():
        return b"", "内容为空，没有可解析的字节"
    cleaned = text.replace("0x", "").replace("0X", "")
    for char in _SEPARATORS:
        cleaned = cleaned.replace(char, "")
    if not cleaned:
        return b"", "去掉分隔符后没有剩下十六进制字符"
    bad = sorted({char for char in cleaned if char not in _HEX_DIGITS})
    if bad:
        return b"", f"含非十六进制字符：{'、'.join(bad)}"
    if len(cleaned) % 2:
        return b"", f"十六进制字符数为奇数（{len(cleaned)} 个），每个字节需要 2 个字符"
    return bytes.fromhex(cleaned), ""


def to_hex_text(data: bytes, group: int = 1) -> str:
    """字节串转十六进制文本，``group`` 个字节一组（默认逐字节，便于肉眼数位）。"""
    raw = bytes(data)
    if group <= 1:
        return " ".join(f"{byte:02X}" for byte in raw)
    return " ".join(
        "".join(f"{byte:02X}" for byte in raw[index : index + group])
        for index in range(0, len(raw), group)
    )


def parse_int_text(text: str, label: str = "数值") -> int:
    """把输入框里的一个整数读出来（``0x`` 开头按十六进制，其余按十进制）。"""
    body = (text or "").strip().replace("_", "")
    if not body:
        raise ValueError(f"{label}不能为空")
    try:
        if body.lower().startswith(("0x", "+0x", "-0x")):
            return int(body, 16)
        if body.lower().startswith(("0b", "+0b", "-0b")):
            return int(body, 2)
        return int(body, 10)
    except ValueError:
        raise ValueError(
            f"{label}「{text}」不是有效的数字（十进制，或 0x 开头的十六进制）"
        ) from None


def parse_int_list(text: str, label: str = "数值") -> list[int]:
    """把 ``"0x0001, 2 3"`` 这类列表文本拆成整数序列。

    分隔符支持逗号（中英文）、分号、空格、换行。
    """
    items = [item for item in re.split(f"[{re.escape(_SEPARATORS)}]+", (text or "").strip()) if item]
    if not items:
        raise ValueError(f"{label}不能为空")
    return [parse_int_text(item, f"{label}第 {index} 项") for index, item in enumerate(items, 1)]


# --------------------------------------------------------------- 解析

#: 常用功能码的地址/数量/值输入方式，构造界面按它切换控件
FUNCTION_FIELDS: dict[int, tuple[str, str, str]] = {
    0x01: ("起始地址", "数量（位）", "count"),
    0x02: ("起始地址", "数量（位）", "count"),
    0x03: ("起始地址", "数量（寄存器）", "count"),
    0x04: ("起始地址", "数量（寄存器）", "count"),
    0x05: ("地址", "线圈值", "coil"),
    0x06: ("地址", "寄存器值", "value"),
    0x0F: ("起始地址", "线圈序列（0/1）", "bits"),
    0x10: ("起始地址", "寄存器序列", "registers"),
}


@dataclass
class FrameInfo:
    """一帧报文的解析结果 —— 全部是"人能读"的东西，UI 直接显示即可。"""

    mode: str = "rtu"
    slave: int | None = None
    unit: int | None = None
    function: int | None = None
    function_name: str = ""
    address: int | None = None
    quantity: int | None = None
    values: list[int] = field(default_factory=list)
    crc_ok: bool | None = None
    crc_text: str = ""
    summary: str = ""
    error: str = ""
    # 以下为排版/核对用的补充信息
    transaction: int | None = None
    protocol: int | None = None
    byte_count: int | None = None
    exception_code: int | None = None
    is_exception: bool = False
    length: int = 0
    raw: bytes = b""

    @property
    def is_ok(self) -> bool:
        """结构解析通过且 CRC 没错（TCP 模式不看 CRC）。"""
        return not self.error and self.crc_ok is not False

    def detail_lines(self) -> list[str]:
        """多行明细，解析面板逐行显示。"""
        lines = [f"模式：{'RTU（含 CRC-16）' if self.mode == 'rtu' else 'TCP（含 7 字节 MBAP 头）'}"]
        if self.transaction is not None:
            lines.append(f"事务号：{self.transaction}（0x{self.transaction:04X}）")
        if self.protocol is not None:
            lines.append(f"协议号：{self.protocol}（Modbus TCP 固定为 0）")
        if self.unit is not None:
            lines.append(f"从站号：{self.unit}（0x{self.unit:02X}）")
        if self.function is not None:
            if self.is_exception:
                lines.append(
                    f"功能码：0x{self.function:02X}（异常响应，原功能码 "
                    f"0x{self.function & 0x7F:02X}）　{self.function_name}"
                )
            else:
                lines.append(f"功能码：0x{self.function:02X}　{self.function_name}")
        if self.is_exception:
            if self.exception_code is None:
                lines.append("异常码：缺失")
            else:
                lines.append(
                    f"异常码：0x{self.exception_code:02X}　{exception_text(self.exception_code)}"
                )
        if self.address is not None:
            lines.append(f"地址：{self.address}（0x{self.address:04X}）")
        if self.quantity is not None:
            lines.append(f"数量：{self.quantity}")
        if self.byte_count is not None:
            lines.append(f"字节数：{self.byte_count}")
        if self.values and not self.is_exception:
            # 异常响应里的 values 就是异常码，上面已经单独列过了
            lines.append(f"数据：{_fmt_values(self)}")
        lines.append(f"帧长：{self.length} 字节　原始：{to_hex_text(self.raw)}")
        lines.append(f"CRC：{self.crc_text}")
        if self.error:
            lines.append(f"错误：{self.error}")
        return lines


def parse_frame(data: bytes, mode: str = "rtu") -> FrameInfo:
    """把一帧报文解析成 :class:`FrameInfo`。

    结构错、CRC 错都只写进 ``error`` / ``crc_ok``，不抛异常 ——
    工具要能"尽量解析出错帧"，调试时看到半截信息也比一片空白有用。
    """
    kind = (mode or "").strip().lower()
    if kind not in ("rtu", "tcp"):
        raise ValueError(f"未知报文模式「{mode}」，只支持 'rtu' / 'tcp'")
    raw = bytes(data)
    info = FrameInfo(mode=kind, length=len(raw), raw=raw)
    if not raw:
        info.error = "没有数据：帧是空的"
        info.summary = "空帧，无法解析"
        info.crc_text = "无数据可校验"
        return info
    if kind == "rtu":
        _parse_rtu(raw, info)
    else:
        _parse_tcp(raw, info)
    if info.crc_ok is False:
        info.summary = f"CRC 错误：{info.summary}"
    if info.error and info.error not in info.summary:
        info.summary = f"{info.summary}（{info.error}）"
    return info


def _parse_rtu(raw: bytes, info: FrameInfo) -> None:
    if len(raw) < 4:
        info.error = f"帧太短：RTU 帧至少 4 字节（从站号+功能码+CRC），实际 {len(raw)} 字节"
        info.summary = "帧太短，无法解析"
        info.crc_text = f"帧只有 {len(raw)} 字节，不足最小 RTU 帧长 4 字节"
        return
    info.slave = raw[0]
    info.unit = raw[0]
    ok, text = check_crc(raw)
    info.crc_ok = ok
    info.crc_text = text
    _parse_pdu(raw[1:-2], info)


def _parse_tcp(raw: bytes, info: FrameInfo) -> None:
    if len(raw) < MBAP_HEADER_LEN + 1:
        info.error = (
            f"帧太短：TCP 帧至少 {MBAP_HEADER_LEN + 1} 字节（MBAP 7 字节 + 功能码），"
            f"实际 {len(raw)} 字节"
        )
        info.summary = "帧太短，无法解析"
        info.crc_text = "TCP 模式不带 CRC（校验由 TCP 链路负责）"
        return
    transaction, protocol, length, unit = struct.unpack(">HHHB", raw[:MBAP_HEADER_LEN])
    info.transaction = transaction
    info.protocol = protocol
    info.unit = unit
    info.slave = unit
    # TCP 报文本身没有 CRC，校验交给链路层，所以这里不判对错只做说明
    info.crc_ok = None
    info.crc_text = "TCP 模式不带 CRC（校验由 TCP 链路负责）"

    expect = len(raw) - MBAP_FIXED_LEN
    if protocol != 0:
        info.error = (
            f"MBAP 协议号应为 0，实际 0x{protocol:04X}；"
            "如果粘的是 RTU 帧，请把报文模式切到 RTU"
        )
    if length != expect:
        extra = (
            f"MBAP 长度域 = {length}，但按实际帧长应为 {expect}"
            f"（从站号 1 字节 + PDU {len(raw) - MBAP_HEADER_LEN} 字节）"
        )
        info.error = f"{info.error}；{extra}" if info.error else extra
    _parse_pdu(raw[MBAP_HEADER_LEN:], info)


def _parse_pdu(pdu: bytes, info: FrameInfo) -> None:
    if not pdu:
        info.error = "缺少功能码：报文只有头部，没有 PDU"
        info.summary = "无法解析（无功能码）"
        return
    function = pdu[0]
    info.function = function
    data = pdu[1:]
    if is_exception_function(function):
        _parse_exception(pdu, info)
        return
    info.function_name = function_name(function)
    if function in READ_FUNCTIONS:
        _parse_read(function, data, info)
    elif function in WRITE_SINGLE_FUNCTIONS:
        _parse_write_single(function, data, info)
    elif function in WRITE_MULTIPLE_FUNCTIONS:
        _parse_write_multiple(function, data, info)
    elif function == 0x16:
        _parse_mask_write(data, info)
    elif function == 0x17:
        _parse_read_write(data, info)
    else:
        # 没做结构化解析的功能码也把数据摆出来，别让人误以为"解析失败"
        info.values = list(data)
        info.summary = (
            f"{info.function_name}：从站 {info.unit}，数据 {to_hex_text(data) or '（无）'}"
            "（该功能码未做结构化解析）"
        )


def _parse_exception(pdu: bytes, info: FrameInfo) -> None:
    base = pdu[0] & 0x7F
    info.is_exception = True
    info.function_name = function_name(base)
    if len(pdu) < 2:
        info.error = "异常响应只有功能码，缺少异常码字节（应为 2 字节）"
        info.summary = f"异常响应：从站 {info.unit}，功能码 0x{pdu[0]:02X}，但缺少异常码"
        return
    code = pdu[1]
    info.exception_code = code
    info.values = [code]
    info.summary = (
        f"异常响应：从站 {info.unit}，功能码 0x{base:02X}（{info.function_name}），"
        f"异常码 0x{code:02X}　{exception_text(code)}"
    )


def _parse_read(function: int, data: bytes, info: FrameInfo) -> None:
    name = info.function_name
    if not data:
        info.error = "读操作缺少地址与数量（请求 PDU 应为 5 字节）"
        info.summary = f"{name}：从站 {info.unit}，报文被截断，读不到起始地址与数量"
        return
    # 请求的数据段恰好 4 字节（地址 2 + 数量 2），响应是"字节数 + 数据"。
    # 两者不会撞车：响应里出现 4 字节数据段意味着只有 3 字节数据，寄存器读不可能有。
    if len(data) == 4:
        info.address = _read_u16(data[0:2])
        info.quantity = _read_u16(data[2:4])
        word = "位" if function in (0x01, 0x02) else "个寄存器"
        info.summary = (
            f"{name} 请求：从站 {info.unit}，起始地址 {info.address}"
            f"（0x{info.address:04X}），数量 {info.quantity} {word}"
        )
        return

    # 响应：字节数 + 数据
    byte_count = data[0]
    payload = data[1:]
    info.byte_count = byte_count
    if byte_count != len(payload):
        info.error = f"字节数域写的是 {byte_count}，但实际跟了 {len(payload)} 字节数据"
    if function in (0x01, 0x02):
        bits = _bit_list(payload)
        info.values = bits
        info.summary = (
            f"{name} 响应：从站 {info.unit}，{byte_count} 字节 = {len(bits)} 位"
            f"（位序低位在前），位状态 {_fmt_bits(bits)}"
        )
    else:
        if len(payload) % 2:
            info.error = info.error or f"寄存器数据字节数为奇数（{len(payload)}），无法按 16 位拆分"
        regs = _register_list(payload)
        info.values = regs
        info.summary = (
            f"{name} 响应：从站 {info.unit}，{byte_count} 字节 = {len(regs)} 个寄存器，"
            f"值 {_fmt_registers(regs)}"
        )


def _parse_write_single(function: int, data: bytes, info: FrameInfo) -> None:
    name = info.function_name
    if len(data) != 4:
        info.error = f"写单个的 PDU 应为 5 字节（功能码+地址 2+值 2），实际数据 {len(data)} 字节"
        info.summary = f"{name}：从站 {info.unit}，报文长度不对，读不到地址与值"
        return
    info.address = _read_u16(data[0:2])
    value = _read_u16(data[2:4])
    info.values = [value]
    where = f"地址 {info.address}（0x{info.address:04X}）"
    if function == 0x05:
        if value == 0xFF00:
            state = "ON（置位）"
        elif value == 0x0000:
            state = "OFF（复位）"
        else:
            state = "非法值（标准只允许 0xFF00 / 0x0000）"
        info.summary = f"{name}：从站 {info.unit}，{where}，值 {state}（0x{value:04X}）"
    else:
        info.summary = f"{name}：从站 {info.unit}，{where}，值 {value}（0x{value:04X}）"


def _parse_write_multiple(function: int, data: bytes, info: FrameInfo) -> None:
    name = info.function_name
    word = "个线圈" if function == 0x0F else "个寄存器"
    if len(data) < 4:
        info.error = f"{name}的 PDU 至少 5 字节（功能码+地址 2+数量 2），实际数据 {len(data)} 字节"
        info.summary = f"{name}：从站 {info.unit}，报文被截断，读不到地址与数量"
        return
    info.address = _read_u16(data[0:2])
    info.quantity = _read_u16(data[2:4])
    where = f"起始地址 {info.address}（0x{info.address:04X}）"
    if len(data) == 4:
        # 响应：只回地址 + 数量
        info.summary = f"{name} 响应：从站 {info.unit}，{where}，已写入 {info.quantity} {word}"
        return

    byte_count = data[4]
    payload = data[5:]
    info.byte_count = byte_count
    if byte_count != len(payload):
        info.error = f"字节数域写的是 {byte_count}，但实际跟了 {len(payload)} 字节数据"
    if function == 0x0F:
        expect = (info.quantity + 7) // 8
        if byte_count != expect:
            info.error = (
                f"字节数与线圈数量不匹配：{info.quantity} 个线圈应占 {expect} 字节，"
                f"报文写了 {byte_count}"
            )
        bits = _bit_list(payload)[: info.quantity]
        info.values = bits
        info.summary = (
            f"{name} 请求：从站 {info.unit}，{where}，{info.quantity} {word}"
            f"（{byte_count} 字节），值 {_fmt_bits(bits)}"
        )
    else:
        if byte_count != info.quantity * 2:
            info.error = (
                f"字节数与寄存器数量不匹配：{info.quantity} 个寄存器应占 "
                f"{info.quantity * 2} 字节，报文写了 {byte_count}"
            )
        if len(payload) % 2:
            info.error = info.error or f"寄存器数据字节数为奇数（{len(payload)}），无法按 16 位拆分"
        regs = _register_list(payload)
        info.values = regs
        info.summary = (
            f"{name} 请求：从站 {info.unit}，{where}，{info.quantity} {word}"
            f"（{byte_count} 字节），值 {_fmt_registers(regs)}"
        )


def _parse_mask_write(data: bytes, info: FrameInfo) -> None:
    if len(data) != 6:
        info.error = f"屏蔽写寄存器的 PDU 应为 7 字节（功能码+地址 2+AND 2+OR 2），实际数据 {len(data)} 字节"
        info.summary = f"{info.function_name}：从站 {info.unit}，报文长度不对"
        return
    info.address = _read_u16(data[0:2])
    and_mask = _read_u16(data[2:4])
    or_mask = _read_u16(data[4:6])
    info.values = [and_mask, or_mask]
    info.summary = (
        f"{info.function_name}：从站 {info.unit}，地址 {info.address}（0x{info.address:04X}），"
        f"AND 掩码 0x{and_mask:04X}，OR 掩码 0x{or_mask:04X}"
    )


def _parse_read_write(data: bytes, info: FrameInfo) -> None:
    """0x17 读写多个寄存器：读参数在前，写参数在后。"""
    if len(data) < 9:
        info.error = f"读写多个寄存器的 PDU 至少 10 字节，实际数据 {len(data)} 字节"
        info.summary = f"{info.function_name}：从站 {info.unit}，报文被截断"
        return
    read_start = _read_u16(data[0:2])
    read_qty = _read_u16(data[2:4])
    write_start = _read_u16(data[4:6])
    write_qty = _read_u16(data[6:8])
    byte_count = data[8]
    payload = data[9:]
    info.address = read_start
    info.quantity = read_qty
    info.byte_count = byte_count
    if byte_count != len(payload):
        info.error = f"字节数域写的是 {byte_count}，但实际跟了 {len(payload)} 字节数据"
    regs = _register_list(payload)
    info.values = regs
    info.summary = (
        f"{info.function_name} 请求：从站 {info.unit}，读起始 {read_start}（0x{read_start:04X}）"
        f"{read_qty} 个，写起始 {write_start}（0x{write_start:04X}）{write_qty} 个，"
        f"写入值 {_fmt_registers(regs)}"
    )


# --------------------------------------------------------------- 内部工具


def _byte(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}「{value}」不是整数") from None
    if not 0 <= number <= 0xFF:
        raise ValueError(f"{label} {number} 超出 8 位范围（0~255）")
    return number


def _u16(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label}「{value}」不是整数") from None
    if not 0 <= number <= 0xFFFF:
        raise ValueError(f"{label} {number} 超出 16 位范围（0~65535）")
    return number


def _read_u16(raw: bytes) -> int:
    """从报文中按大端取一个 16 位字。

    注意与 :func:`_u16` 分工不同：``_u16`` 校验"用户敲进来的数值"，
    这个负责把报文里的 ``b'\\x00\\x05'`` 读成 5。
    """
    return int.from_bytes(raw[:2], "big")


def _check_pdu_len(function: int, payload: bytes) -> None:
    _byte(function, "功能码")
    length = 1 + len(payload)
    if length > MAX_PDU_LEN:
        raise ValueError(
            f"PDU 长度 {length} 字节超过 Modbus 上限 {MAX_PDU_LEN} 字节，"
            "请减少数量后分帧发送"
        )


def _coil_value(value: Any) -> int:
    """线圈值的各种写法统一成 0xFF00 / 0x0000。"""
    if isinstance(value, bool):
        return 0xFF00 if value else 0x0000
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("on", "通", "合", "1", "true", "yes"):
            return 0xFF00
        if text in ("off", "断", "分", "0", "false", "no"):
            return 0x0000
        number = parse_int_text(value, "线圈值")
    else:
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"线圈值「{value}」不是整数") from None
    if number in (0, 0x0000):
        return 0x0000
    if number in (1, 0xFF00):
        return 0xFF00
    raise ValueError(f"线圈值只能写 ON（1 或 0xFF00）或 OFF（0 或 0x0000），收到 {number}")


def _coil_bits(values: Any) -> list[int]:
    if isinstance(values, (bytes, bytearray)):
        bits = _bit_list(bytes(values))
    elif isinstance(values, str):
        cleaned = "".join(char for char in values if char not in _SEPARATORS)
        if not cleaned:
            raise ValueError("线圈序列不能为空")
        if set(cleaned) <= {"0", "1"}:
            bits = [int(char) for char in cleaned]
        else:
            # 也允许直接贴打包好的字节（如 "FF 0F"）
            data, reason = parse_hex_text(values)
            if reason:
                wrong = "".join(sorted({char for char in cleaned if char not in "01"}))
                raise ValueError(
                    f"线圈序列里的「{wrong}」不是 0 或 1：要么逐个写 0/1（如 1, 0, 1），"
                    f"要么贴打包好的十六进制字节（如 FF 0F）"
                )
            bits = _bit_list(data)
    else:
        bits = []
        for index, item in enumerate(values, 1):
            try:
                number = int(item)
            except (TypeError, ValueError):
                raise ValueError(f"第 {index} 个线圈值「{item}」不是整数") from None
            if number not in (0, 1):
                raise ValueError(f"第 {index} 个线圈值是 {number}，只能是 0 或 1")
            bits.append(number)
    if not bits:
        raise ValueError("至少要写一个线圈")
    return bits


def _register_values(values: Any) -> list[int]:
    if isinstance(values, (bytes, bytearray)):
        raw = bytes(values)
        if len(raw) % 2:
            raise ValueError(
                f"寄存器数据字节数为奇数（{len(raw)}），没法拆成 16 位寄存器（需要偶数）"
            )
        regs = _register_list(raw)
    elif isinstance(values, str):
        regs = parse_int_list(values, "寄存器序列")
    else:
        regs = []
        for item in values:
            try:
                regs.append(int(item))
            except (TypeError, ValueError):
                raise ValueError(f"寄存器值「{item}」不是整数") from None
    if not regs:
        raise ValueError("至少要写一个寄存器")
    for index, reg in enumerate(regs, 1):
        if not 0 <= reg <= 0xFFFF:
            raise ValueError(f"第 {index} 个寄存器值 {reg} 超出 16 位范围（0~65535）")
    return regs


def _pack_bits(bits: Sequence[int]) -> bytes:
    """线圈状态按每字节 8 个打包，同字节内 **低位在前**（Modbus 就是这么排的）。"""
    out = bytearray((len(bits) + 7) // 8)
    for index, bit in enumerate(bits):
        if bit:
            out[index // 8] |= 1 << (index % 8)
    return bytes(out)


def _bit_list(raw: bytes) -> list[int]:
    return [(byte >> index) & 1 for byte in raw for index in range(8)]


def _register_list(raw: bytes) -> list[int]:
    return [_read_u16(raw[index : index + 2]) for index in range(0, len(raw) - 1, 2)]


def _fmt_registers(values: Sequence[int], limit: int = 12) -> str:
    head = ", ".join(f"0x{value:04X}" for value in values[:limit])
    if len(values) > limit:
        return f"{head} …（共 {len(values)} 个）"
    return head or "（无）"


def _fmt_bits(values: Sequence[int], limit: int = 40) -> str:
    head = "".join(str(bit) for bit in values[:limit])
    if len(values) > limit:
        return f"{head}…（共 {len(values)} 位）"
    return head or "（无）"


def _fmt_values(info: FrameInfo) -> str:
    # 只有"按位"读/写多线圈的 values 才是位序列；写单个线圈的 values 是那个
    # 0xFF00 / 0x0000，按 16 位显示才看得出写的是 ON 还是 OFF。
    if info.function in (0x01, 0x02, 0x0F):
        return f"{_fmt_bits(info.values)}（位，低位在前）"
    return _fmt_registers(info.values)
