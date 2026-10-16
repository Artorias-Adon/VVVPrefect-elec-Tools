"""西门子 S7 驱动（S7comm over ISO-on-TCP，TCP 端口 102）。

走的是**以太网口直连**：S7-1200 / S7-1500 的网口、S7-300/400 的 CP343/CP443、
以及各种网关/仿真器，都是同一条路（RFC1006 上的 S7 协议）。
MPI / Profibus / PPI 那些老链路不在支持范围（要专门的适配器）。

依赖 ``python-snap7``（3.x 是**纯 Python** 实现，不带原生 DLL，打包省事）。

点表三列怎么写 —— 这是本驱动最容易搞混的地方::

    寄存器区        协议路径            起始地址          数据类型
    db/i/q/m       DB1.DBX4.0 等写法   路径没写偏移时用它   bool/uint8/uint16/…

* **协议路径**按西门子习惯写就行（大小写不敏感）：

  ================  ==============================================
  ``DB1.DBX4.0``    DB1 第 4 字节第 0 位（bool）
  ``DB1.DBB4``      DB1 第 4 字节（uint8 / int8）
  ``DB1.DBW6``      DB1 第 6 字节起的字（uint16 / int16）
  ``DB1.DBD8``      DB1 第 8 字节起的双字（uint32 / int32 / float32）
  ``M10.0`` / ``MB10`` / ``MW12`` / ``MD14``   位存储区（M）
  ``I0.0`` / ``IB0`` / ``IW0`` / ``ID0``       输入区（也认 E 开头的写法）
  ``Q0.0`` / ``QB0`` / ``QW0`` / ``QD0``       输出区（也认 A 开头的写法）
  ``DB1`` 或 ``1``                              只写 DB 号，偏移看"起始地址"列
  ================  ==============================================

* **寄存器区**决定"路径里没带区字母"时走哪个区：``db``（数据块）、``i``（输入）、
  ``q``（输出）、``m``（位存储）。路径里已经带了（``MW12``）就以路径为准 ——
  现场点表往往是混着写的，两种都能填才不至于逼人改表。
* **起始地址**是**字节偏移**，不是字偏移 —— 这点和 Modbus 不一样，别照搬。
  位点位（bool）的位号写在路径里（``M10.3``），不写就是第 0 位。
* 数据类型决定读几个字节：``uint8``/``int8`` 1 个、``uint16``/``int16`` 2 个、
  ``uint32``/``int32``/``float32`` 4 个、``float64`` 8 个。路径里的尺寸字母
  （``B``/``W``/``D``）只是给你对表看的，对不上会在通信日志里提醒一句，
  真正生效的是数据类型。

读取按「同区同 DB、地址相近」合并成一次请求，读回来的字节流再各自解码，
所以几百个点位通常也只要几次往返。
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Any, Iterable

from ..codec import to_engineering, to_raw
from ..models import DeviceDef, PointDef
from .base import ChannelDriver, DriverError, OptionField, PointResult, register_driver

try:
    from snap7 import Client as _S7Client

    _IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - 取决于运行环境
    _S7Client = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(exc)


# --------------------------------------------------------------- 位点表常量

#: 点位「寄存器区」列的可选项与中文名
POINT_AREAS = [
    ("db", "数据块 DB"),
    ("i", "输入区 I/E"),
    ("q", "输出区 Q/A"),
    ("m", "位存储区 M"),
]

#: 内部区名 -> 显示用的前缀
AREA_LABELS = {"db": "DB", "i": "I", "q": "Q", "m": "M"}

#: 路径里的区字母 -> 内部区名（E/A 是德语写法，现场两种都会出现）
_AREA_LETTERS = {"I": "i", "E": "i", "Q": "q", "A": "q", "M": "m"}

#: 数据类型 -> (struct 格式, 占用字节数)。S7 一律大端。
_TYPE_FORMAT: dict[str, tuple[str, int]] = {
    "uint8": (">B", 1),
    "int8": (">b", 1),
    "uint16": (">H", 2),
    "int16": (">h", 2),
    "uint32": (">I", 4),
    "int32": (">i", 4),
    "float32": (">f", 4),
    "float64": (">d", 8),
}

#: 路径里的尺寸字母暗示的数据类型（只用来提醒）
_SIZE_LETTER_HINT = {
    "X": "bool",
    "B": "uint8 / int8",
    "W": "uint16 / int16",
    "D": "uint32 / int32 / float32",
}

#: 尺寸字母对应的数据类型，用来判断"类型与路径是否对得上"
_SIZE_LETTER_TYPES = {
    "X": ("bool",),
    "B": ("uint8", "int8"),
    "W": ("uint16", "int16"),
    "D": ("uint32", "int32", "float32"),
}

#: ``DB1`` / ``DB1.DBX4.0`` / ``DB1.DBW6`` / ``DB1.4``
_DB_RE = re.compile(
    r"^DB(?P<db>\d+)"
    r"(?:\.(?:(?:DB)?(?P<size>[XBWD]))?(?P<byte>\d+)(?:\.(?P<bit>\d))?)?$",
    re.IGNORECASE,
)

#: ``M10.0`` / ``MW12`` / ``IB0`` / ``E0.0`` / ``QD4``
_AREA_RE = re.compile(
    r"^(?P<area>[EIQAM])(?:(?P<size>[XBWD]))?(?P<byte>\d+)(?:\.(?P<bit>\d))?$",
    re.IGNORECASE,
)

#: 只写了 DB 号（``1``）
_NUMBER_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class S7Address:
    """一个点位解析后的落点。"""

    #: db / i / q / m
    area: str
    #: DB 号（只有 area == "db" 时有效）
    db: int
    #: 字节偏移
    byte: int
    #: 位号；非位点位为 None
    bit: int | None
    #: 路径里写的尺寸字母（X/B/W/D），没写为空
    size: str
    #: 原始文本，报错时回显
    text: str = ""

    @property
    def prefix(self) -> str:
        if self.area == "db":
            return f"DB{self.db}"
        return AREA_LABELS.get(self.area, self.area.upper())

    @property
    def label(self) -> str:
        """给通信日志看的写法，如 ``DB1.4.0`` / ``MW12``。"""
        text = f"{self.prefix}.{self.byte}"
        if self.bit is not None:
            text += f".{self.bit}"
        return text

    @property
    def area_label(self) -> str:
        if self.area == "db":
            return f"DB{self.db}"
        return AREA_LABELS.get(self.area, self.area.upper())


def parse_address(
    text: str,
    *,
    area: str = "db",
    byte: int = 0,
    is_bit: bool = False,
) -> S7Address:
    """把西门子写法的地址解析成 :class:`S7Address`。

    ``text`` 为空时退回「寄存器区 + 起始地址」两列（老点表常见的写法）。
    """
    raw = (text or "").strip()
    base_area = _normalise_area(area)
    base_byte = max(0, int(byte or 0))

    if not raw:
        if base_area == "db":
            raise DriverError(
                "DB 点位必须填「协议路径」，例如 DB1.DBD8 或只写 DB1（偏移写起始地址列）"
            )
        return S7Address(
            area=base_area,
            db=0,
            byte=base_byte,
            bit=0 if is_bit else None,
            size="",
        )

    compact = raw.replace(" ", "")

    if _NUMBER_RE.match(compact):
        return S7Address(
            area="db", db=int(compact), byte=base_byte, bit=0 if is_bit else None, size="", text=raw
        )

    matched = _DB_RE.match(compact)
    if matched:
        offset = matched.group("byte")
        bit_text = matched.group("bit")
        bit: int | None
        if is_bit:
            bit = int(bit_text) if bit_text is not None else 0
        else:
            bit = None
        return S7Address(
            area="db",
            db=int(matched.group("db")),
            byte=int(offset) if offset is not None else base_byte,
            bit=bit,
            size=(matched.group("size") or "").upper(),
            text=raw,
        )

    matched = _AREA_RE.match(compact)
    if matched:
        offset = int(matched.group("byte"))
        bit_text = matched.group("bit")
        return S7Address(
            area=_AREA_LETTERS[matched.group("area").upper()],
            db=0,
            byte=offset,
            bit=(int(bit_text) if bit_text is not None else 0) if is_bit else None,
            size=(matched.group("size") or "").upper(),
            text=raw,
        )

    raise DriverError(
        f"看不懂的西门子地址「{raw}」：应形如 DB1.DBX4.0 / DB1.DBW6 / DB1.DBD8 / "
        "MW12 / M10.0 / IW64 / QB0，或只写 DB 号（DB1 / 1）再把偏移写起始地址列"
    )


def _normalise_area(area: str) -> str:
    key = (area or "db").strip().lower()
    aliases = {
        "db": "db", "db1": "db", "数据块": "db", "datablock": "db",
        "i": "i", "e": "i", "input": "i", "pe": "i", "输入": "i",
        "q": "q", "a": "q", "output": "q", "pa": "q", "输出": "q",
        "m": "m", "mk": "m", "flag": "m", "merker": "m", "位存储": "m",
    }
    resolved = aliases.get(key)
    if resolved is None:
        raise DriverError(
            f"寄存器区「{area}」无效：西门子 S7 填 db（数据块）/ i（输入）/ q（输出）/ m（位存储）"
        )
    return resolved


def item_width(data_type: str, is_bit: bool) -> int:
    """单个元素占用的字节数（位点位按 1 字节算，读回来取其中一位）。"""
    if is_bit:
        return 1
    info = _TYPE_FORMAT.get(data_type)
    if info is None:
        raise DriverError(
            f"数据类型「{data_type}」不支持：西门子 S7 可用 {'/'.join(_TYPE_FORMAT)}"
        )
    return info[1]


# --------------------------------------------------------------- 批量合并


@dataclass
class _Item:
    """一个待读点位：落点 + 宽度 + 元素个数。"""

    point: PointDef
    addr: S7Address
    width: int
    count: int

    @property
    def end(self) -> int:
        return self.addr.byte + self.width * self.count


def plan_spans(
    items: list[_Item],
    max_span: int = 200,
    max_gap: int = 8,
) -> list[tuple[str, int, int, list[tuple[_Item, int]]]]:
    """把点位合并成若干次读。

    返回 ``[(区, DB号, 起始字节, 字节数, [(点位, 相对偏移), ...]), ...]``

    合并规则和 Modbus 那边一样：同一区、同一 DB、地址升序，只要「地址空洞 <= max_gap」
    且「跨度 <= max_span」就并进同一次请求。单个点位的长度超过 max_span 时
    自己独占一段（读的时候会再分块拼接），免得拖累旁边的标量点位。
    """
    spans: list[tuple[str, int, int, list[tuple[_Item, int]]]] = []
    groups: dict[tuple[str, int], list[_Item]] = {}
    for item in items:
        groups.setdefault((item.addr.area, item.addr.db), []).append(item)

    for (area, db), group in groups.items():
        entries = sorted(group, key=lambda it: it.addr.byte)
        cursor_start: int | None = None
        cursor_end = 0
        members: list[_Item] = []

        def flush(start: int | None, end: int, chunk: list[_Item]) -> None:
            if start is None or not chunk:
                return
            spans.append((area, db, start, end - start, [(it, it.addr.byte - start) for it in chunk]))

        for item in entries:
            if cursor_start is None:
                cursor_start, cursor_end, members = item.addr.byte, item.end, [item]
                continue
            new_end = max(cursor_end, item.end)
            if (item.addr.byte - cursor_end) <= max_gap and (new_end - cursor_start) <= max_span:
                cursor_end = new_end
                members.append(item)
            else:
                flush(cursor_start, cursor_end, members)
                cursor_start, cursor_end, members = item.addr.byte, item.end, [item]
        flush(cursor_start, cursor_end, members)

    return spans


def _decode_value(data_type: str, chunk: bytes, is_bit: bool, bit: int, little: bool) -> Any:
    """字节流 -> 原始数值（尚未做缩放/平移）。"""
    if is_bit:
        return bool((chunk[0] >> bit) & 1)
    info = _TYPE_FORMAT[data_type]
    raw = chunk[: info[1]]
    if little:
        # 只有第三方网关把字节序调过时才需要；真机（西门子）都是大端
        raw = raw[::-1]
    (value,) = struct.unpack(info[0], raw)
    return value


def _encode_value(data_type: str, value: float, little: bool) -> bytes:
    """原始数值 -> 字节流。"""
    info = _TYPE_FORMAT[data_type]
    if data_type.startswith("float"):
        raw = struct.pack(info[0], float(value))
    else:
        raw = struct.pack(info[0], int(round(float(value))))
    return raw[::-1] if little else raw


# --------------------------------------------------------------- 驱动实现


@register_driver
class S7Driver(ChannelDriver):
    """西门子 S7 主站驱动（一个通道 = 一个 PLC 的 ISO-on-TCP 连接）。"""

    driver_name = "s7"

    @classmethod
    def point_areas(cls) -> list[tuple[str, str]]:
        return list(POINT_AREAS)

    @classmethod
    def point_area_hint(cls) -> str:
        return (
            "西门子：db=数据块、i=输入区(I/E)、q=输出区(Q/A)、m=位存储区；"
            "协议路径写 DB1.DBD8 / MW12 / M10.0 这类西门子地址，起始地址是字节偏移"
        )

    link_types = ("tcp",)
    description = "西门子 S7（S7comm / ISO-on-TCP，端口 102）：S7-200Smart/300/400/1200/1500 与兼容网关"
    multi_device = True

    def __init__(self, channel) -> None:
        super().__init__(channel)
        self._client = None
        #: 已经提醒过的点位（类型与地址不一致之类），只提醒一次免得刷屏
        self._warned: set[str] = set()

    # -------------------------------------------------- 参数定义

    @classmethod
    def option_schema(cls, link: str) -> list[OptionField]:
        return [
            OptionField("host", "IP 地址", "str", "127.0.0.1"),
            OptionField("port", "端口", "int", 102, minimum=1, maximum=65535, hint="S7 固定 102"),
            OptionField(
                "rack",
                "机架号",
                "int",
                0,
                minimum=0,
                maximum=7,
                hint="S7-1200/1500、S7-300 一般是 0；S7-400 见手册",
            ),
            OptionField(
                "slot",
                "插槽号",
                "int",
                1,
                minimum=0,
                maximum=31,
                hint="S7-1200/1500 用 1；S7-300 用 2；S7-400 常见 2~3",
            ),
            OptionField("timeout", "超时(秒)", "float", 3.0, minimum=0.1, maximum=30.0),
            OptionField(
                "max_span_bytes",
                "单次最大字节数",
                "int",
                200,
                minimum=1,
                maximum=960,
                hint="一次请求最多读多少字节；PDU 480 时留余量取 200",
            ),
            OptionField(
                "max_gap",
                "地址空洞阈值",
                "int",
                8,
                minimum=0,
                maximum=64,
                hint="两个点位字节偏移相差不超过该值时合并成一次读取",
            ),
            OptionField("retry", "失败重试次数", "int", 1, minimum=0, maximum=5),
        ]

    # -------------------------------------------------- 生命周期

    def open(self) -> None:
        if _IMPORT_ERROR:
            raise DriverError(
                f"python-snap7 不可用：{_IMPORT_ERROR}（请执行 pip install python-snap7）"
            )

        self.close()
        opts = self.channel.options or {}
        host = str(opts.get("host", "127.0.0.1"))
        port = int(opts.get("port", 102))
        rack = int(opts.get("rack", 0))
        slot = int(opts.get("slot", 1))
        timeout = float(opts.get("timeout", 3.0))

        client = _S7Client()
        try:
            client.connect(host, rack, slot, port)
        except Exception as exc:
            try:
                client.disconnect()
            except Exception:
                pass
            raise DriverError(
                f"连接失败：{host}:{port}（机架 {rack} 插槽 {slot}）—— {exc}"
            ) from exc

        _apply_socket_timeout(client, timeout)
        self._client = client
        self.emit_log(
            "info", f"{self.channel.name} 已连接 {host}:{port}（机架 {rack} 插槽 {slot}）"
        )

    def close(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

    @property
    def is_open(self) -> bool:
        client = self._client
        if client is None:
            return False
        try:
            # snap7 3.x 的 get_connected() 会主动探一下套接字，
            # 断网/拔线以后这里就会变 False，引擎随即重连。
            return bool(client.get_connected())
        except Exception:
            return False

    def _endpoint_text(self) -> str:
        opts = self.channel.options or {}
        return f"{opts.get('host', '127.0.0.1')}:{opts.get('port', 102)}"

    # -------------------------------------------------- 地址与校验

    def _address_of(self, point: PointDef) -> S7Address:
        """解析点位地址（顺带把"类型与路径对不上"提醒一次）。"""
        is_bit = point.data_type == "bool"
        if is_bit and point.is_array:
            raise DriverError("位点位不支持数组：bool 只能一个点一位")
        addr = parse_address(
            point.path,
            area=point.register,
            byte=point.address,
            is_bit=is_bit,
        )
        self._check_consistency(point, addr, is_bit)
        return addr

    def _check_consistency(self, point: PointDef, addr: S7Address, is_bit: bool) -> None:
        """路径里的尺寸字母和数据类型对不上时提醒一次（不强行改）。"""
        if not addr.size or point.name in self._warned:
            return
        if point.data_type in _SIZE_LETTER_TYPES.get(addr.size, ()):
            return
        self._warned.add(point.name)
        self.emit_log(
            "warning",
            f"{point.name}：协议路径「{addr.text}」里的 {addr.size} 通常对应 "
            f"{_SIZE_LETTER_HINT.get(addr.size, '')}，而数据类型填的是 {point.data_type} "
            f"—— 按数据类型读 {item_width(point.data_type, is_bit)} 字节",
        )

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
        max_span = max(1, int(opts.get("max_span_bytes", 200)))
        max_gap = max(0, int(opts.get("max_gap", 8)))
        retry = int(opts.get("retry", 1))

        items: list[_Item] = []
        for point in points:
            try:
                addr = self._address_of(point)
                is_bit = point.data_type == "bool"
                items.append(
                    _Item(
                        point=point,
                        addr=addr,
                        width=item_width(point.data_type, is_bit),
                        count=max(1, point.count) if point.is_array else 1,
                    )
                )
            except Exception as exc:
                results[point.name] = PointResult(tag=point.name, ok=False, error=str(exc))

        for area, db, start, size, members in plan_spans(items, max_span, max_gap):
            payload = self._read_span(area, db, start, size, device, retry, max_span)
            if payload is None:
                for item, _offset in members:
                    results[item.point.name] = PointResult(
                        tag=item.point.name, ok=False, error="读取失败"
                    )
                continue
            for item, offset in members:
                try:
                    results[item.point.name] = PointResult(
                        tag=item.point.name,
                        value=self._decode_item(item, payload, offset),
                        ok=True,
                    )
                except Exception as exc:
                    results[item.point.name] = PointResult(
                        tag=item.point.name, ok=False, error=str(exc)
                    )
        return results

    def _decode_item(self, item: _Item, payload: bytes, offset: int):
        """从读回来的字节流里取出这个点位的值（已换算工程量）。"""
        point = item.point
        is_bit = point.data_type == "bool"
        little = point.word_order == "little"

        def one(position: int):
            chunk = payload[position : position + item.width]
            if len(chunk) < item.width:
                raise DriverError("响应长度不足")
            raw = _decode_value(
                point.data_type,
                chunk,
                is_bit,
                item.addr.bit or 0,
                little,
            )
            return to_engineering(raw, point.scale, point.offset)

        if not point.is_array:
            return one(offset)
        return [one(offset + index * item.width) for index in range(item.count)]

    def _read_span(
        self,
        area: str,
        db: int,
        start: int,
        size: int,
        device: DeviceDef,
        retry: int,
        max_span: int,
    ) -> bytes | None:
        """读一段字节（必要时按 max_span 分块拼接），失败返回 None。"""
        collected = bytearray()
        done = 0
        while done < size:
            chunk = min(max_span, size - done)
            part = self._read_once(
                area, db, start + done, chunk, device, retry
            )
            if part is None:
                return None
            collected.extend(part)
            done += chunk
        return bytes(collected)

    def _read_once(
        self,
        area: str,
        db: int,
        start: int,
        size: int,
        device: DeviceDef,
        retry: int,
    ) -> bytes | None:
        """读一段字节（单个 S7 请求），失败返回 None。

        读失败会把连接关掉：这样 ``is_open`` 立刻变 False，
        引擎下一拍就会重新连 —— 现场"断开一下再连就连不上"的老毛病就是这么来的。
        """
        label = _area_text(area, db, start)
        last_error = ""
        for attempt in range(retry + 1):
            try:
                payload = bytes(self._read_area_bytes(area, db, start, size))
                self.emit_frame(
                    "RX",
                    f"{device.name} 读 {label} {size}B -> {_brief(payload)}",
                )
                return payload
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                if attempt < retry:
                    self.emit_log(
                        "warning", f"{device.name} 读 {label} 重试 {attempt + 1}/{retry}：{last_error}"
                    )
                    self._reconnect()
                else:
                    self.emit_frame("RX", f"{device.name} 读 {label} -> 失败：{last_error}")

        self.emit_log("error", f"{device.name} 读取失败：{last_error}")
        self.close()
        return None

    def _read_area_bytes(self, area: str, db: int, start: int, size: int) -> bytes:
        client = self._client
        if area == "db":
            return bytes(client.db_read(db, start, size))
        if area == "i":
            return bytes(client.eb_read(start, size))
        if area == "q":
            return bytes(client.ab_read(start, size))
        return bytes(client.mb_read(start, size))

    def _write_bytes(self, area: str, db: int, start: int, data: bytes) -> None:
        client = self._client
        payload = bytearray(data)
        if area == "db":
            client.db_write(db, start, payload)
            return
        if area == "i":
            client.eb_write(start, len(payload), payload)
            return
        if area == "q":
            client.ab_write(start, payload)
            return
        client.mb_write(start, len(payload), payload)

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

        addr = self._address_of(point)
        is_bit = point.data_type == "bool"

        if is_bit:
            # 位写：先把所在字节读回来改一位再写回去 —— 很多 S7 兼容设备
            # （尤其是网关）不支持独立的位写请求，走读-改-写最稳。
            bit = addr.bit or 0
            current = self._read_area_bytes(addr.area, addr.db, addr.byte, 1)
            if bool(value):
                new_byte = current[0] | (1 << bit)
            else:
                new_byte = current[0] & ~(1 << bit) & 0xFF
            self.emit_frame("TX", f"{device.name} 写 {addr.area_label}.{addr.byte}.{bit} = {bool(value)}")
            self._write_bytes(addr.area, addr.db, addr.byte, bytes([new_byte]))
            return

        raw = to_raw(value, point.scale, point.offset)
        payload = _encode_value(point.data_type, raw, point.word_order == "little")
        self.emit_frame(
            "TX",
            f"{device.name} 写 {addr.label} = {value}（{payload.hex(' ')}）",
        )
        self._write_bytes(addr.area, addr.db, addr.byte, payload)


def _apply_socket_timeout(client, seconds: float) -> None:
    """把超时落到套接字上（尽力而为，内部结构变了也不该导致连不上）。"""
    try:
        connection = getattr(client, "connection", None)
        sock = getattr(connection, "socket", None)
        if sock is not None:
            sock.settimeout(max(0.1, float(seconds)))
    except Exception:  # pragma: no cover - 只是优化项
        pass


def _area_text(area: str, db: int, start: int) -> str:
    """通信日志里的一段地址，写法跟点表的协议路径一致：DB1.4 / M10 / I0。"""
    if area == "db":
        return f"DB{db}.{start}"
    return f"{AREA_LABELS.get(area, area.upper())}{start}"


def _brief(payload: bytes, head: int = 16) -> str:
    """字节流太长时截断显示，避免通信监视页被一次块读刷屏。"""
    text = payload[:head].hex(" ")
    if len(payload) > head:
        return f"{text} …(共 {len(payload)}B)"
    return text
