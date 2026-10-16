"""寄存器原始数据 <-> 工程值 的编解码。

这部分逻辑与协议无关（Modbus / IEC104 / DL/T645 都需要），
因此独立成模块并配套单元测试。
"""

from __future__ import annotations

import struct

from .models import is_bit_type, normalize_base, word_count

_BIG_STRUCT = {
    "uint8": ">B",
    "int8": ">b",
    "uint16": ">H",
    "int16": ">h",
    "uint32": ">I",
    "int32": ">i",
    "float32": ">f",
    "float64": ">d",
}

#: 8 位类型：Modbus 一个寄存器 16 位，这两种类型只占其中的低字节
_BYTE_TYPES = ("uint8", "int8")

#: 字序说明：
#: Modbus 报文本身是「寄存器高字节在前」，word_order 控制 32/64 位数据
#: 在多个寄存器之间的排列顺序。
#:   big    -> 高字在前（大端，业界默认，如 0x1234 / 0x5678 表示 0x12345678）
#:   little -> 低字在前（如三菱/部分仪表，0x5678 / 0x1234 表示 0x12345678）


def _pack(data_type: str, value: float) -> bytes:
    fmt = _BIG_STRUCT.get(data_type)
    if fmt is None:
        raise ValueError(f"不支持的数据类型: {data_type}")
    # struct 对整数格式要求整数入参，对浮点格式要求浮点入参，这里统一收口，
    # 避免 int(round())/float() 来回转换时出现 "required argument is not an integer"。
    if data_type.startswith("float"):
        return struct.pack(fmt, float(value))
    return struct.pack(fmt, int(value))


def combine_registers(regs: list[int]) -> bytes:
    """把 16 位寄存器序列拼成字节流（每个寄存器高字节在前）。"""
    out = bytearray()
    for r in regs:
        out += struct.pack(">H", int(r) & 0xFFFF)
    return bytes(out)


def split_registers(data: bytes) -> list[int]:
    """字节流拆成 16 位寄存器序列。"""
    if len(data) % 2:
        raise ValueError("字节数必须为偶数")
    return [int.from_bytes(data[i : i + 2], "big") for i in range(0, len(data), 2)]


def apply_word_order(regs: list[int], data_type: str, word_order: str) -> list[int]:
    """按字序要求重排寄存器，使 combine_registers 能直接拼接出正确的字节流。"""
    if word_order == "little" and word_count(data_type) > 1:
        return list(reversed(regs))
    return list(regs)


def decode(regs: list[int], data_type: str, word_order: str = "big") -> float | bool:
    """寄存器原始值 -> 工程量前的原始数值。"""
    if is_bit_type(data_type):
        # 位类型：调用方传 0/1，或传寄存器 0/非 0
        return bool(regs[0]) if regs else False

    if data_type in _BYTE_TYPES:
        if not regs:
            raise ValueError(f"{data_type} 需要 1 个寄存器，实际 0 个")
        byte = int(regs[0]) & 0xFF
        if data_type == "int8" and byte >= 0x80:
            return byte - 0x100
        return byte

    need = word_count(data_type)
    if len(regs) < need:
        raise ValueError(f"{data_type} 需要 {need} 个寄存器，实际 {len(regs)} 个")
    if data_type not in _BIG_STRUCT:
        raise ValueError(f"不支持的数据类型: {data_type}")

    ordered = apply_word_order(regs[:need], data_type, word_order)
    raw = combine_registers(ordered)
    fmt = _BIG_STRUCT[data_type]
    (value,) = struct.unpack(fmt, raw)
    return value


def encode(value: float | bool, data_type: str, word_order: str = "big") -> list[int]:
    """原始数值 -> 寄存器序列（用于写点位）。"""
    if is_bit_type(data_type):
        return [1 if value else 0]

    if data_type in _BYTE_TYPES:
        return [int(round(float(value))) & 0xFF]

    need = word_count(data_type)
    if not data_type.startswith("float"):
        value = int(round(float(value)))  # type: ignore[arg-type]
    raw = _pack(data_type, value)
    regs = split_registers(raw)
    return apply_word_order(regs, data_type, word_order)[:need]


def to_engineering(raw: float | bool, scale: float = 1.0, offset: float = 0.0) -> float | bool:
    """原始数值 -> 工程量（缩放 + 平移）。

    未配置缩放/偏移时保持原始类型，避免把计数器、状态字、电能等
    整数量无谓地变成浮点数。
    """
    if isinstance(raw, bool):
        return raw
    if scale == 1 and offset == 0:
        return raw
    return raw * scale + offset


def to_raw(engineering: float | bool, scale: float = 1.0, offset: float = 0.0) -> float | bool:
    """工程量 -> 原始数值（写入时使用）。"""
    if isinstance(engineering, bool):
        return engineering
    if scale == 1 and offset == 0:
        return engineering
    if scale == 0:
        raise ValueError("scale 不能为 0，否则无法由工程量反算原始值")
    return (engineering - offset) / scale


def registers_to_value(
    regs: list[int],
    data_type: str,
    word_order: str = "big",
    scale: float = 1.0,
    offset: float = 0.0,
):
    """一站式：寄存器 -> 工程量。"""
    raw = decode(regs, data_type, word_order)
    return to_engineering(raw, scale, offset)


def value_to_registers(
    value: float | bool,
    data_type: str,
    word_order: str = "big",
    scale: float = 1.0,
    offset: float = 0.0,
) -> list[int]:
    """一站式：工程量 -> 寄存器。"""
    raw = to_raw(value, scale, offset)
    regs = encode(raw, data_type, word_order)
    return regs


def format_base(value: int, base: int) -> str:
    """把整数按指定进制写成带前缀的字符串（0x / 0b / 0o）。"""
    if value < 0:
        return "-" + format_base(-value, base)
    if base == 16:
        return f"0x{value:X}"
    if base == 2:
        return f"0b{value:b}"
    if base == 8:
        return f"0o{value:o}"
    return str(value)


def format_value(value, decimals: int = 2, base: int = 10) -> str:
    """按小数位 / 进制格式化显示值。

    进制不是十进制时按整数取整显示（看状态字、寄存器最常用）；
    模拟量选十六进制会丢小数，这是用户自己选的。
    """
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    base = normalize_base(base)
    if base != 10:
        try:
            return format_base(int(round(float(value))), base)
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, int):
        return str(value)
    try:
        return f"{float(value):.{max(0, min(6, decimals))}f}"
    except (TypeError, ValueError):
        return str(value)
