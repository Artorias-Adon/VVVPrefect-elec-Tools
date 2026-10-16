"""CAN 总线驱动（CAN 2.0A/B 原始帧 + CANopen SDO 上传/下载）。

链路类型只有一种：``can``。一个通道 = 一块适配器的一路总线，挂在总线上的
多台设备用各自的从站号区分 —— 原始帧按帧号识别，CANopen 按节点号（0..127）。

实现要点：

* **原始帧**：帧号写在点位的协议路径里（如 ``0x181``），帧内字节偏移写在地址里。
  ``can_rx`` 是"监听到该帧就取里面的值"，``can_tx`` 是"写值就发这一帧" ——
  发送前先取最近一帧做**读-改-写**：一条 8 字节报文里塞好几个点位是 CAN 的
  常规做法，整帧覆盖会把同一帧里别的点位打回 0。
* **CANopen SDO**：只做 expedited（快速传输，1/2/4 字节）。分段传输在现场
  基本只用于参数下载，周期采集用不上，不值得为此引入一整套状态机。
* **收帧线程**：``bus.recv(timeout=…)`` 循环，收到的帧按帧号进缓存供
  ``read_points`` 取用，同时把 ``0x580+node`` 的应答交给正在等它的 SDO 请求；
  ``close()`` 先请线程退出、再 shutdown 总线，免得线程还在已关闭的句柄上收帧。
* 各后端的连接参数并不通用（slcan 没有 index、socketcan 只认通道名……），
  所以按后端白名单构造参数，而不是把一堆关键字硬塞给 ``can.Bus``。
"""

from __future__ import annotations

import re
import struct
import threading
import time
from typing import Any, Iterable

from ..codec import to_engineering, to_raw
from ..models import DeviceDef, PointDef
from .base import (
    ChannelDriver,
    DriverError,
    OptionField,
    PointResult,
    register_driver,
)

try:
    import can

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - 取决于运行环境
    can = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)


# --------------------------------------------------------------- 常量

#: 点位「协议区」的可选项与中文名
CAN_RX = "can_rx"
CAN_TX = "can_tx"
CAN_SDO = "can_sdo"

POINT_AREAS = [
    (CAN_RX, "监听帧（收到该帧即为值）"),
    (CAN_TX, "发送帧（写值就发这一帧）"),
    (CAN_SDO, "CANopen SDO 读写"),
]

POINT_AREA_HINT = (
    "can_rx：监听帧 —— 协议路径填帧号（0x181 十六进制 / 385 十进制，>0x7FF 算扩展帧），"
    "地址填帧内字节偏移（0~7），收到该帧即为本点位的值；"
    "can_tx：发送帧 —— 路径、地址同上，写值时读-改-写只动本点位占的那几字节；"
    "can_sdo：CANopen 对象 —— 协议路径填「索引:子索引」（如 0x6041:0），节点号取设备的从站号。"
)

#: 适配器后端（python-can 的后端名）与中文说明，顺序即界面下拉框的顺序
BACKENDS = (
    "slcan",
    "pcan",
    "kvaser",
    "vector",
    "ixxat",
    "etas",
    "usb2can",
    "socketcan",
    "virtual",
)

BACKEND_LABELS = (
    "slcan —— USB-CAN 棒 / 串口透传（最常见）",
    "pcan —— PEAK PCAN-USB",
    "kvaser —— Kvaser",
    "vector —— Vector（CANalyzer 那家）",
    "ixxat —— IXXAT",
    "etas —— ETAS",
    "usb2can —— 8 位 USB-CAN",
    "socketcan —— Linux SocketCAN",
    "virtual —— 内置虚拟总线（不接硬件也能跑）",
)

BITRATES = (10000, 20000, 50000, 100000, 125000, 250000, 500000, 800000, 1000000)

BITRATE_LABELS = tuple(f"{rate // 1000} kbit/s" for rate in BITRATES)

#: 经典 CAN 帧最多 8 字节
FRAME_BYTES = 8

#: 标准帧 11 位 / 扩展帧 29 位
MAX_STANDARD_ID = 0x7FF
MAX_EXTENDED_ID = 0x1FFFFFFF

#: 缓存里最多留多少帧 —— 长期运行的总线上帧号可能很多，不封顶会一直涨
MAX_CACHED_FRAMES = 256

#: 各数据类型的字节宽度与 struct 格式（按字节序拼前缀）
BYTE_WIDTH = {
    "bool": 1,
    "uint16": 2,
    "int16": 2,
    "uint32": 4,
    "int32": 4,
    "float32": 4,
    "float64": 8,
}

STRUCT_FORMAT = {
    "uint16": "H",
    "int16": "h",
    "uint32": "I",
    "int32": "i",
    "float32": "f",
    "float64": "d",
}

#: CANopen SDO 的 COB-ID 基址
SDO_REQUEST_COB = 0x600
SDO_RESPONSE_COB = 0x580

SDO_CMD_UPLOAD_REQUEST = 0x40
SDO_CMD_ABORT = 0x80
SDO_CMD_DOWNLOAD_RESPONSE = 0x60

#: 下载请求命令字：字节数 -> 命令字（n = 4 - 字节数）
SDO_DOWNLOAD_COMMANDS = {1: 0x2F, 2: 0x2B, 4: 0x23}

#: 常见中止码的中文解释（报错时带上，省得再去翻协议）
SDO_ABORT_NAMES = {
    0x05030000: "切换位未复位",
    0x06010000: "不支持对该对象的访问",
    0x06020000: "对象字典里没有这个对象",
    0x06070010: "数据类型长度不匹配",
    0x06090011: "子索引不存在",
    0x08000000: "一般性错误",
}

#: 每个后端认识的连接参数。python-can 各后端关键字不同，多传一个就报 TypeError，
#: 所以按后端列举；不在表里的后端退回最保守的「通道 + 波特率」。
_BACKEND_KWARGS: dict[str, tuple[str, ...]] = {
    "slcan": ("channel", "bitrate"),
    "pcan": ("channel", "bitrate", "receive_own_messages"),
    "kvaser": ("channel", "bitrate"),
    "vector": ("channel", "bitrate", "receive_own_messages"),
    "ixxat": ("channel", "bitrate", "receive_own_messages"),
    "etas": ("channel", "bitrate"),
    "usb2can": ("channel", "bitrate"),
    "socketcan": ("channel",),
    "virtual": ("channel", "receive_own_messages"),
}

_DEFAULT_KWARGS = ("channel", "bitrate")

#: 卡上"第几路"由 index 决定的后端，channel 要传整数
_INDEX_CHANNEL_BACKENDS = ("kvaser", "vector", "ixxat", "etas")

_PLAIN_DIGITS = re.compile(r"[0-9]+")
_HEX_DIGITS = re.compile(r"[0-9a-fA-F]+")


# --------------------------------------------------------------- 工具函数


def _parse_int_text(text: str, plain_base: int) -> int:
    """把一段文本读成整数：带 ``0x`` 前缀或含字母按十六进制，纯数字按 ``plain_base``。"""
    body = text.strip()
    if body[:2].lower() == "0x":
        return int(body[2:], 16)
    if _PLAIN_DIGITS.fullmatch(body):
        return int(body, plain_base)
    if _HEX_DIGITS.fullmatch(body):
        return int(body, 16)
    raise ValueError(text)


def parse_frame_id(text: str) -> tuple[int, bool]:
    """解析帧号，返回 ``(can_id, 是否扩展帧)``。

    写法：``0x181`` / ``0X181``（十六进制）、含字母的 ``18F``（也是十六进制）、
    纯数字的 ``385``（十进制）。``can_id > 0x7FF`` 即扩展帧（29 位）。

    注意十进制 385 和十六进制 0x181 是同一帧：**想写 0x181 就必须带 ``0x``**，
    否则按十进制理解 —— 两种写法都留着，是因为现场有的设备手册就写十进制。
    """
    body = (text or "").strip()
    if not body:
        raise DriverError("CAN 点位的协议路径里要写帧号，如 0x181")
    if body.startswith("-"):
        raise DriverError(f"帧号不能为负：{body!r}")
    try:
        can_id = _parse_int_text(body, 10)
    except ValueError:
        raise DriverError(
            f"帧号写法不对：{body!r}（写 0x181 这样的十六进制，或 385 这样的十进制）"
        ) from None
    if can_id > MAX_EXTENDED_ID:
        raise DriverError(f"帧号超出 29 位范围（最大 0x{MAX_EXTENDED_ID:X}）：{body!r}")
    return can_id, can_id > MAX_STANDARD_ID


def parse_sdo_object(text: str) -> tuple[int, int]:
    """解析 CANopen 对象，返回 ``(索引, 子索引)``。

    写法：``0x6041:0`` / ``6041:0`` / ``0x6041.0`` / ``6041/0``；只写索引
    （``0x6041``）算子索引 0。

    **索引按十六进制**（CANopen 惯例，``0x6041`` 与 ``6041`` 是一回事），
    子索引按十进制，写了 ``0x`` 前缀或含字母时按十六进制。
    """
    body = (text or "").strip()
    if not body:
        raise DriverError("CANopen 点位的协议路径里要写对象，如 0x6041:0")
    if body.startswith("-"):
        raise DriverError(f"对象索引不能为负：{body!r}")

    head, tail = body, ""
    for separator in (":", ".", "/"):
        if separator in body:
            head, _, tail = body.partition(separator)
            break
    try:
        index = _parse_int_text(head, 16)
        subindex = _parse_int_text(tail, 10) if tail.strip() else 0
    except ValueError:
        raise DriverError(
            f"对象写法不对：{body!r}（写 0x6041:0 这样的「索引:子索引」）"
        ) from None
    if not 0 <= index <= 0xFFFF:
        raise DriverError(f"对象索引超出 16 位范围（0..0xFFFF）：{body!r}")
    if not 0 <= subindex <= 0xFF:
        raise DriverError(f"子索引超出 0..255：{body!r}")
    return index, subindex


def payload_width(data_type: str) -> int:
    """该数据类型在帧里占几个字节。"""
    try:
        return BYTE_WIDTH[data_type]
    except KeyError:
        raise DriverError(
            f"CAN 不支持的数据类型：{data_type}（可用 {'/'.join(BYTE_WIDTH)}）"
        ) from None


def _slice(data: bytes, offset: int, width: int, data_type: str) -> bytes:
    offset = int(offset)
    if offset < 0 or offset + width > len(data):
        raise DriverError(
            f"字节偏移越界：{data_type} 从第 {offset} 字节起要占 {width} 字节，"
            f"这一帧只有 {len(data)} 字节"
        )
    return bytes(data[offset : offset + width])


def decode_bytes(
    data: bytes, offset: int, data_type: str, word_order: str = "big"
) -> float | bool:
    """帧字节 -> 原始数值（不套缩放）。``word_order`` 就是字节序，默认大端。"""
    width = payload_width(data_type)
    chunk = _slice(data, offset, width, data_type)
    if data_type == "bool":
        # CAN 里布尔量通常占一整个字节：非 0 为真
        return chunk[0] != 0
    fmt = ("<" if word_order == "little" else ">") + STRUCT_FORMAT[data_type]
    return struct.unpack(fmt, chunk)[0]


def encode_bytes(value: Any, data_type: str, word_order: str = "big") -> bytes:
    """原始数值 -> 帧字节。"""
    if data_type == "bool":
        return bytes([1 if value else 0])
    fmt = ("<" if word_order == "little" else ">") + STRUCT_FORMAT[data_type]
    try:
        if data_type.startswith("float"):
            return struct.pack(fmt, float(value))
        return struct.pack(fmt, int(round(float(value))))
    except (struct.error, ValueError, TypeError) as exc:
        raise DriverError(f"值 {value!r} 不能按 {data_type} 编码：{exc}") from None


def format_frame(can_id: int, data: bytes, is_extended: bool = False) -> str:
    """通信监视页里的那一行：``0x00000181 [8] 01 02 03 04 05 06 07 08``。"""
    body = " ".join(f"{byte:02X}" for byte in bytes(data))
    return f"0x{int(can_id):08X} [{len(data)}] {body}"


def _channel_value(interface: str, channel: Any, index: int) -> Any:
    """把界面上的「通道 / 串口」文本折算成该后端要的类型。

    kvaser/vector/ixxat/etas 的 channel 是"第几路"（整数），pcan 是
    PCAN_USBBUS1 这样的名字，slcan 是串口号，virtual/socketcan 是名字。
    """
    text = str(channel if channel is not None else "").strip()
    if interface == "pcan":
        return text or f"PCAN_USBBUS{index + 1}"
    if interface in _INDEX_CHANNEL_BACKENDS:
        if not text:
            return index
        return int(text) if text.isdigit() else text
    if text:
        return text
    return "vvp" if interface == "virtual" else ""


def create_bus(
    interface: str = "slcan",
    channel: Any = "",
    bitrate: int = 500000,
    index: int = 0,
    receive_own_messages: bool = False,
):
    """按后端白名单开一条 CAN 总线（驱动与内置仿真器共用）。

    参数只挑该后端认识的传；万一某个版本改过关键字，收到 TypeError 就逐级
    退让重试（先丢掉回显开关，再丢掉波特率），最后如实报错 —— 总比把
    不认识的关键字硬塞进去、让用户看一句莫名其妙的 TypeError 强。
    """
    if can is None:
        raise DriverError(f"未安装 python-can（{_IMPORT_ERROR}），请执行 pip install python-can")

    interface = str(interface or "slcan").strip().lower()
    index = max(0, int(index))
    values = {
        "channel": _channel_value(interface, channel, index),
        "bitrate": int(bitrate),
        "receive_own_messages": bool(receive_own_messages),
    }
    names = _BACKEND_KWARGS.get(interface, _DEFAULT_KWARGS)
    last_error: Exception | None = None
    while True:
        try:
            return can.Bus(interface=interface, **{name: values[name] for name in names})
        except TypeError as exc:
            last_error = exc
            optional = [name for name in ("receive_own_messages", "bitrate") if name in names]
            if not optional:
                break
            names = tuple(name for name in names if name not in optional)
        except Exception as exc:
            raise DriverError(
                f"打开 CAN 总线失败（{interface} / {values['channel']}）：{exc}"
            ) from exc
    raise DriverError(f"打开 CAN 总线失败（{interface} / {values['channel']}）：{last_error}")


def _sdo_node(device: DeviceDef) -> int:
    """CANopen 节点号取设备的从站号。"""
    node = int(device.unit_id or 0)
    if not 0 <= node <= 127:
        raise DriverError(
            f"CANopen 节点号必须在 0..127：设备 {device.name} 的从站号是 {node}"
            "（节点号用的是设备的从站号这一栏）"
        )
    return node


def _sdo_value(raw: bytes, data_type: str) -> float | bool:
    """把 SDO 里的 1/2/4 字节按点位数据类型解出数值（CANopen 数据是小端）。"""
    if data_type == "bool":
        return any(raw)
    if data_type.startswith("float"):
        if len(raw) != 4:
            raise DriverError(f"float32 需要 4 字节，SDO 应答里只有 {len(raw)} 字节")
        return struct.unpack("<f", raw)[0]
    return int.from_bytes(raw, "little", signed=data_type.startswith("int"))


# --------------------------------------------------------------- 驱动实现


@register_driver
class CanDriver(ChannelDriver):
    """CAN 主站驱动（一个通道 = 一块适配器的一路总线）。"""

    driver_name = "can"
    link_types = ("can",)
    description = "CAN 总线（CAN 2.0A/B，支持原始帧与 CANopen SDO）"
    multi_device = True

    def __init__(self, channel) -> None:
        super().__init__(channel)
        self._bus = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()
        #: 收到的帧：``{帧号: (字节, 收到时刻)}``，can_rx 从这里取值
        self._rx_cache: dict[int, tuple[bytes, float]] = {}
        #: 我们发出去的帧：``{帧号: (字节, 发出时刻)}``，can_tx 的读-改-写拿它当底
        self._tx_cache: dict[int, tuple[bytes, float]] = {}
        #: 正在等应答的 SDO 请求：``{应答帧号: (事件, [应答数据])}``
        self._sdo_wait: dict[int, tuple[threading.Event, list[bytes]]] = {}

    # -------------------------------------------------- 参数定义

    @classmethod
    def point_areas(cls) -> list[tuple[str, str]]:
        return list(POINT_AREAS)

    @classmethod
    def point_area_hint(cls) -> str:
        return POINT_AREA_HINT

    @classmethod
    def option_schema(cls, link: str) -> list[OptionField]:
        return [
            OptionField(
                "backend",
                "适配器后端",
                "choice",
                "slcan",
                choices=BACKENDS,
                choice_labels=BACKEND_LABELS,
                hint="python-can 的后端名；选「内置虚拟总线」不接硬件也能先把点表跑通",
            ),
            OptionField(
                "channel",
                "通道 / 串口",
                "str",
                "COM3",
                hint="slcan 填串口（COM3）；pcan 填 PCAN_USBBUS1；kvaser / vector / ixxat 填第几路（0）；"
                "virtual 填总线名（收发双方要一致，如 vvp）",
            ),
            OptionField(
                "bitrate",
                "波特率",
                "choice",
                500000,
                choices=BITRATES,
                choice_labels=BITRATE_LABELS,
                hint="整条总线上所有节点必须一致",
            ),
            OptionField(
                "index",
                "多路卡通道号",
                "int",
                0,
                minimum=0,
                maximum=15,
                hint="多路卡的第几路（pcan / kvaser / vector 用）",
            ),
            OptionField(
                "receive_own",
                "回显自己发的帧",
                "bool",
                False,
                hint="回显自己发的帧（自测用）",
            ),
            OptionField(
                "timeout_ms",
                "超时(毫秒)",
                "int",
                200,
                minimum=10,
                maximum=5000,
                hint="SDO 等待应答的上限 / 接收线程的读超时",
            ),
        ]

    # -------------------------------------------------- 生命周期

    def _timeout_ms(self) -> int:
        raw = int((self.channel.options or {}).get("timeout_ms", 200))
        return max(10, min(5000, raw))

    def _endpoint_text(self) -> str:
        opts = self.channel.options or {}
        return f"{opts.get('backend', 'slcan')}@{opts.get('channel', '')} {int(opts.get('bitrate', 500000)) // 1000} kbit/s"

    def open(self) -> None:
        if can is None:
            raise DriverError(
                f"未安装 python-can（{_IMPORT_ERROR}），请执行 pip install python-can"
            )
        self.close()
        opts = self.channel.options or {}
        bus = create_bus(
            interface=str(opts.get("backend", "slcan")),
            channel=opts.get("channel", "COM3"),
            bitrate=int(opts.get("bitrate", 500000)),
            index=int(opts.get("index", 0)),
            receive_own_messages=bool(opts.get("receive_own", False)),
        )
        self._bus = bus
        self._stop.clear()
        thread = threading.Thread(
            target=self._receive_loop,
            args=(bus,),
            name=f"can-rx-{self.channel.name}",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        self.emit_log("info", f"{self.channel.name} 已打开 {self._endpoint_text()}")

    def close(self) -> None:
        thread, self._thread = self._thread, None
        bus, self._bus = self._bus, None
        self._stop.set()
        if thread is not None and thread is not threading.current_thread():
            # 先请线程退出再关总线：它可能正卡在 recv 上
            thread.join(timeout=2.0)
        if bus is not None:
            try:
                bus.shutdown()
            except Exception as exc:
                self.emit_log("warning", f"{self.channel.name} 关闭 CAN 总线出错：{exc}")
        with self._lock:
            self._rx_cache.clear()
            self._tx_cache.clear()

    @property
    def is_open(self) -> bool:
        return self._bus is not None

    # -------------------------------------------------- 收帧线程

    def _receive_loop(self, bus) -> None:
        timeout = self._timeout_ms() / 1000.0
        while not self._stop.is_set():
            try:
                message = bus.recv(timeout=timeout)
            except Exception as exc:
                if self._stop.is_set():
                    break
                self.emit_log("warning", f"{self.channel.name} 收帧出错：{exc}")
                time.sleep(0.05)  # 总线坏了别在这里空转刷日志
                continue
            if message is None or getattr(message, "is_error_frame", False):
                continue
            can_id = int(message.arbitration_id)
            data = bytes(message.data or b"")
            self._store_frame(can_id, data)
            self.emit_frame(
                "RX", format_frame(can_id, data, bool(getattr(message, "is_extended_id", False)))
            )
            self._dispatch_sdo(can_id, data)

    def _store_frame(self, can_id: int, data: bytes) -> None:
        with self._lock:
            self._rx_cache[can_id] = (data, time.time())
            if len(self._rx_cache) > MAX_CACHED_FRAMES:
                stale = sorted(self._rx_cache, key=lambda key: self._rx_cache[key][1])
                for key in stale[: len(self._rx_cache) - MAX_CACHED_FRAMES]:
                    self._rx_cache.pop(key, None)

    def _lookup_frame(self, can_id: int, from_tx: bool = False) -> tuple[bytes, float] | None:
        """取最近一帧：can_tx 先看自己发的，can_rx 只看收到的。"""
        caches = (self._tx_cache, self._rx_cache) if from_tx else (self._rx_cache,)
        with self._lock:
            for cache in caches:
                entry = cache.get(can_id)
                if entry is not None:
                    return entry
        return None

    def _dispatch_sdo(self, can_id: int, data: bytes) -> None:
        """把 ``0x580+node`` 的应答交给正在等它的请求。"""
        if not SDO_RESPONSE_COB <= can_id < SDO_RESPONSE_COB + 0x80:
            return
        with self._lock:
            entry = self._sdo_wait.get(can_id)
        if entry is None:
            return
        event, holder = entry
        holder.append(data)
        event.set()

    # -------------------------------------------------- 编解码

    def _decode(self, data: bytes, offset: int, point: PointDef):
        """帧字节 -> 工程量（字节序 -> 原始值 -> 缩放/偏移）。"""
        width = payload_width(point.data_type)
        if point.is_array:
            count = max(1, point.count)
            start = int(offset)
            end = start + count * width
            if start < 0 or end > len(data):
                raise DriverError(
                    f"数组点位要占第 {start}~{end - 1} 字节，这一帧只有 {len(data)} 字节"
                )
            return [
                to_engineering(
                    decode_bytes(data, start + index * width, point.data_type, point.word_order),
                    point.scale,
                    point.offset,
                )
                for index in range(count)
            ]
        raw = decode_bytes(data, offset, point.data_type, point.word_order)
        return to_engineering(raw, point.scale, point.offset)

    def _encode(self, value: Any, point: PointDef, payload: bytearray) -> bytearray:
        """把工程量写进帧字节——只动本点位占的字节，别的字节保持原样。"""
        width = payload_width(point.data_type)
        if point.is_array:
            items = list(value) if isinstance(value, (list, tuple)) else None
            if items is None:
                raise DriverError(
                    f"数组点位 {point.name} 要写一个列表（{max(1, point.count)} 个元素）"
                )
            if len(items) != max(1, point.count):
                raise DriverError(
                    f"数组点位 {point.name} 要 {max(1, point.count)} 个元素，收到 {len(items)} 个"
                )
        else:
            items = [value]

        for index, item in enumerate(items):
            offset = int(point.address) + index * width
            chunk = encode_bytes(
                to_raw(item, point.scale, point.offset), point.data_type, point.word_order
            )
            if offset < 0 or offset + len(chunk) > len(payload):
                raise DriverError(
                    f"字节偏移越界：{point.data_type} 从第 {offset} 字节起要占 {len(chunk)} 字节，"
                    f"这一帧只有 {len(payload)} 字节"
                )
            payload[offset : offset + len(chunk)] = chunk
        return payload

    # -------------------------------------------------- 读取

    def read_points(
        self, device: DeviceDef, points: Iterable[PointDef]
    ) -> dict[str, PointResult]:
        points = list(points)
        results: dict[str, PointResult] = {
            p.name: PointResult(tag=p.name, ok=False, error="未执行") for p in points
        }
        if not points:
            return results
        if not self.is_open:
            raise DriverError("连接未建立")

        for point in points:
            try:
                results[point.name] = self._read_one(device, point)
            except Exception as exc:
                # 单个点位的问题（帧没来、对象不存在、超时）只标记它自己
                results[point.name] = PointResult(
                    tag=point.name, ok=False, error=str(exc) or exc.__class__.__name__
                )
        return results

    def _read_one(self, device: DeviceDef, point: PointDef) -> PointResult:
        if point.register in (CAN_RX, CAN_TX):
            return self._read_frame_point(point)
        if point.register == CAN_SDO:
            return self._read_sdo_point(device, point)
        return PointResult(
            tag=point.name, ok=False, error="CAN 点位区只能是 can_rx / can_tx / can_sdo"
        )

    def _read_frame_point(self, point: PointDef) -> PointResult:
        can_id, _extended = parse_frame_id(point.path)
        cached = self._lookup_frame(can_id, from_tx=point.register == CAN_TX)
        if cached is None:
            return PointResult(tag=point.name, ok=False, error="还没收到该帧")
        data, _timestamp = cached
        return PointResult(tag=point.name, value=self._decode(data, point.address, point), ok=True)

    def _read_sdo_point(self, device: DeviceDef, point: PointDef) -> PointResult:
        index, subindex = parse_sdo_object(point.path)
        node = _sdo_node(device)
        request = (
            bytes([SDO_CMD_UPLOAD_REQUEST])
            + struct.pack("<HB", index, subindex)
            + bytes(4)
        )
        response = self._sdo_transfer(node, request, f"上传 {index:#06x}:{subindex}")
        if not (response[0] & 0x02 and response[0] & 0x01):
            raise DriverError(
                f"SDO 应答不是 expedited 快速传输（命令字 0x{response[0]:02X}）：分段传输暂不支持"
            )
        size = 4 - ((response[0] >> 2) & 0x03)
        value = _sdo_value(bytes(response[4 : 4 + size]), point.data_type)
        return PointResult(tag=point.name, value=to_engineering(value, point.scale, point.offset), ok=True)

    # -------------------------------------------------- 写入

    def write_point(self, device: DeviceDef, point: PointDef, value: Any) -> None:
        if not self.is_open:
            raise DriverError("连接未建立")
        if point.register == CAN_TX:
            self._write_frame_point(point, value)
        elif point.register == CAN_SDO:
            self._write_sdo_point(device, point, value)
        elif point.register == CAN_RX:
            raise DriverError("can_rx 是只读的监听点位")
        else:
            raise DriverError("CAN 点位区只能是 can_rx / can_tx / can_sdo")

    def _write_frame_point(self, point: PointDef, value: Any) -> None:
        can_id, is_extended = parse_frame_id(point.path)
        cached = self._lookup_frame(can_id, from_tx=True)
        # 读-改-写：底子取最近一帧（自己发过的优先），没有就用全 0 的 8 字节
        payload = bytearray(cached[0]) if cached is not None else bytearray(FRAME_BYTES)
        need = int(point.address) + payload_width(point.data_type) * (
            max(1, point.count) if point.is_array else 1
        )
        if need > FRAME_BYTES:
            raise DriverError(
                f"点位 {point.name} 从第 {int(point.address)} 字节起放不下："
                f"经典 CAN 帧最多 {FRAME_BYTES} 字节"
            )
        if len(payload) < need:
            payload.extend(bytes(need - len(payload)))

        self._encode(value, point, payload)
        data = bytes(payload)
        self._send_frame(can_id, data, is_extended)
        with self._lock:
            self._tx_cache[can_id] = (data, time.time())

    def _write_sdo_point(self, device: DeviceDef, point: PointDef, value: Any) -> None:
        index, subindex = parse_sdo_object(point.path)
        node = _sdo_node(device)
        body = self._encode_sdo(value, point)
        request = (
            bytes([SDO_DOWNLOAD_COMMANDS[len(body)]])
            + struct.pack("<HB", index, subindex)
            + body
        )
        response = self._sdo_transfer(
            node, request + bytes(FRAME_BYTES - len(request)), f"下载 {index:#06x}:{subindex}"
        )
        if response[0] != SDO_CMD_DOWNLOAD_RESPONSE:
            raise DriverError(
                f"SDO 下载应答异常：命令字 0x{response[0]:02X}（应为 0x{SDO_CMD_DOWNLOAD_RESPONSE:02X}）"
            )

    def _encode_sdo(self, value: Any, point: PointDef) -> bytes:
        """工程量 -> SDO 下载的数据字节（expedited 只发 1/2/4 字节）。"""
        width = payload_width(point.data_type)
        if width not in SDO_DOWNLOAD_COMMANDS:
            raise DriverError(
                f"SDO 快速传输只支持 1/2/4 字节，{point.data_type} 是 {width} 字节："
                "改用原始帧点位，或拆成两个点位"
            )
        raw = to_raw(value, point.scale, point.offset)
        if point.data_type == "bool":
            return bytes([1 if raw else 0])
        if point.data_type.startswith("float"):
            return struct.pack("<f", float(raw))
        signed = point.data_type.startswith("int")
        number = int(round(float(raw)))
        low = -(1 << (width * 8 - 1)) if signed else 0
        high = (1 << (width * 8 - (1 if signed else 0))) - 1
        if not low <= number <= high:
            raise DriverError(f"值 {value!r} 超出 {point.data_type} 的范围")
        return number.to_bytes(width, "little", signed=signed)

    # -------------------------------------------------- 收发

    def _send_frame(self, can_id: int, data: bytes, is_extended: bool) -> None:
        bus = self._bus
        if bus is None:
            raise DriverError("连接未建立")
        message = can.Message(
            arbitration_id=int(can_id), is_extended_id=bool(is_extended), data=bytes(data)
        )
        try:
            bus.send(message)
        except Exception as exc:
            raise DriverError(f"发送帧 0x{can_id:03X} 失败：{exc}") from exc
        self.emit_frame("TX", format_frame(can_id, data, is_extended))

    def _sdo_transfer(self, node: int, request: bytes, what: str) -> bytes:
        """发一条 SDO 请求并等应答，返回应答帧的 8 个字节。"""
        request_id = SDO_REQUEST_COB + node
        response_id = SDO_RESPONSE_COB + node
        event = threading.Event()
        holder: list[bytes] = []
        with self._lock:
            self._sdo_wait[response_id] = (event, holder)
        try:
            self._send_frame(request_id, request, False)
            timeout = self._timeout_ms() / 1000.0
            if not event.wait(timeout):
                raise DriverError(
                    f"SDO {what} 超时：{self._timeout_ms()} ms 内没有收到 0x{response_id:03X} 应答"
                )
        finally:
            with self._lock:
                self._sdo_wait.pop(response_id, None)

        response = holder[0] if holder else b""
        if not response:
            raise DriverError(f"SDO {what} 应答为空")
        if response[0] == SDO_CMD_ABORT:
            code = int.from_bytes(bytes(response[4:8]), "little")
            name = SDO_ABORT_NAMES.get(code, "未知中止原因")
            raise DriverError(f"SDO {what} 被中止：0x{code:08X}（{name}）")
        return response
