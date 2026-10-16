"""示例工程的仿真数据写入小工具。

每个示例模块都要实现 ``simulate(store, t)``：按仿真时间 ``t``（秒）刷新该从站的
寄存器区。``store`` 是 ``tools/modbus_sim.py`` 里的 DataStore 对象，
它提供 ``holding / coils / input / discretes`` 四个列表（各 1024 个）与 ``lock``。

这里放寄存器写入的小助手，免得每个示例各写一遍 ``struct.pack``，
也保证"工程量 -> 原始值"的换算写法一致（**scale 必须和点表里填的一致**）。

**依赖方向**：只依赖标准库，不依赖本包以外的东西，也不依赖 Qt。
"""

from __future__ import annotations

import struct
from typing import Any

__all__ = [
    "put_f32",
    "put_i16",
    "put_u16",
    "put_u32",
    "read_u16",
    "store_registers",
    "write_f32",
    "write_u32",
]


def _clamp_u16(value: int) -> int:
    return value & 0xFFFF


def put_u16(regs: list[int], address: int, value: float, scale: float = 1.0) -> None:
    """把工程量按 ``scale`` 换算成无符号 16 位写进寄存器。"""
    raw = int(round(float(value) / scale)) if scale else int(round(float(value)))
    regs[address] = _clamp_u16(raw)


def put_i16(regs: list[int], address: int, value: float, scale: float = 1.0) -> None:
    """把工程量按 ``scale`` 换算成有符号 16 位写进寄存器（负值也能放）。"""
    raw = int(round(float(value) / scale)) if scale else int(round(float(value)))
    regs[address] = _clamp_u16(max(-32768, min(32767, raw)))


def put_u32(regs: list[int], address: int, value: int) -> None:
    """32 位整数占两个连续寄存器，高字在前。"""
    raw = int(value) & 0xFFFFFFFF
    regs[address] = (raw >> 16) & 0xFFFF
    regs[address + 1] = raw & 0xFFFF


def put_f32(regs: list[int], address: int, value: float) -> None:
    """IEEE-754 单精度占两个连续寄存器，高字在前。"""
    high, low = struct.unpack(">HH", struct.pack(">f", float(value)))
    regs[address] = high
    regs[address + 1] = low


def read_u16(regs: list[int], address: int) -> int:
    """读一个无符号 16 位寄存器。"""
    return int(regs[address]) & 0xFFFF


def write_u32(regs: list[int], address: int, value: int) -> None:
    """``put_u32`` 的别名（写值语义更直白时用）。"""
    put_u32(regs, address, value)


def write_f32(regs: list[int], address: int, value: float) -> None:
    """``put_f32`` 的别名。"""
    put_f32(regs, address, value)


def store_registers(store: Any) -> list[int]:
    """取 DataStore 的保持寄存器区（有的实现叫 holding，有的叫 registers）。"""
    for name in ("holding", "registers", "holding_registers"):
        table = getattr(store, name, None)
        if table is not None:
            return table
    raise AttributeError("DataStore 上找不到保持寄存器区")
