"""DSP SCI 串口烧写协议（纯逻辑，不依赖 Qt，可在脚本里单独跑）。

分三层，每层都能单独测：

* :class:`SciProfile` —— 协议参数（自举字符、握手、包头、校验、块大小）。
  不同厂家/不同 bootloader 的细节不一样，所以这些参数**不写死**，界面上能改、
  能存进软件设置；
* :class:`SciBootloader` —— 状态机：自举 → 握手 → 按块下发（包头 + 数据 + 校验）→ 等应答；
* 传输层 —— :class:`SerialTransport`（真串口，pyserial）与
  :class:`SimulatedTransport` + :class:`SimulatedDsp`（自检/测试用，不需要硬件）。

内置两套档案：

``ti-c2000``
    TI C2000（F28x）SCI Boot 的常见流程：先发自举字符让从机锁波特率，
    再 0xAA/0x55 握手；每块数据包头 = 32 位字地址 + 16 位字数 + 16 位数据校验
    （16 位补码），数据按 16 位字小端下发，块尾等 1 字节应答。

``raw``
    设备自带 bootloader 的情况：握手后只发数据、不做包头/校验，
    可选等应答。具体字节在界面上能改。

> 这两套是按公开的引导流程整理的**通用实现**，不包含任何厂商工具代码。
> 如果实机的 bootloader 与参数不符，改参数或换 ``raw`` 档案即可；
> 实在对不上还可以用「外部烧写器」接厂商工具（软件只负责调用，不打包）。
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

from .flash_file import FlashError, FlashImage


class SciError(RuntimeError):
    """SCI 烧写失败（消息直接给用户看）。"""


class SciCancelled(SciError):
    """用户点了中止。"""


# --------------------------------------------------------------- 协议参数


@dataclass
class SciProfile:
    """一套 SCI 烧写参数。"""

    key: str = "ti-c2000"
    label: str = "TI C2000 SCI Boot"
    description: str = ""
    #: 自举字符（从机用它锁波特率）
    autobaud: bytes = b"\x41"
    #: 自举后从机应回的字节（空 = 不等）
    autobaud_echo: bytes = b"\x41"
    #: 握手时主机发 / 从机回
    handshake_send: bytes = b"\xaa"
    handshake_expect: bytes = b"\x55"
    #: ``c2000`` = 8 字节包头 + 校验；``raw`` = 只发数据
    framing: str = "c2000"
    #: ``sum16``（16 位补码）/ ``crc16``（XMODEM）/ ``none``
    checksum: str = "sum16"
    #: 每块最多多少个 16 位字
    block_words: int = 128
    #: 每块之后等从机回的字节（空 = 不等）
    ack: bytes = b"\x55"
    #: 数据字的字节序（C2000 是小端）
    word_order: str = "little"
    #: 块之间额外等待（秒）；有些 bootloader 写 Flash 需要喘口气
    inter_block_s: float = 0.0
    #: 等应答/数据的超时（秒）
    timeout_s: float = 3.0
    #: ``none`` 不校验；``echo`` 要求从机把收到的数据回显一遍
    verify: str = "none"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for name in ("autobaud", "autobaud_echo", "handshake_send", "handshake_expect", "ack"):
            data[name] = getattr(self, name).hex()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None, base: "SciProfile | None" = None) -> "SciProfile":
        """从设置里恢复；坏字段忽略，用 ``base``（默认内置档案）顶上。"""
        profile = cls(**(asdict(base) if base is not None else {}))
        if not data:
            return profile
        for name in ("autobaud", "autobaud_echo", "handshake_send", "handshake_expect", "ack"):
            if name in data and isinstance(data[name], str):
                try:
                    setattr(profile, name, bytes.fromhex(data[name]))
                except ValueError:
                    pass
        for name in ("key", "label", "description", "framing", "checksum", "word_order", "verify"):
            if isinstance(data.get(name), str) and data[name]:
                setattr(profile, name, data[name])
        if isinstance(data.get("block_words"), int):
            profile.block_words = max(1, min(int(data["block_words"]), 4096))
        if isinstance(data.get("inter_block_s"), (int, float)):
            profile.inter_block_s = max(0.0, min(float(data["inter_block_s"]), 10.0))
        if isinstance(data.get("timeout_s"), (int, float)):
            profile.timeout_s = max(0.2, min(float(data["timeout_s"]), 120.0))
        return profile

    def header_bytes(self, word_address: int, word_count: int, checksum16: int) -> bytes:
        """8 字节包头：32 位字地址 + 16 位字数 + 16 位数据校验（大端）。"""
        return (
            int(word_address).to_bytes(4, "big")
            + int(word_count).to_bytes(2, "big")
            + int(checksum16 & 0xFFFF).to_bytes(2, "big")
        )

    def describe(self) -> str:
        return (
            f"{self.label}：自举 {self.autobaud.hex(' ').upper() or '—'}，"
            f"握手 {self.handshake_send.hex(' ').upper() or '—'}"
            f"→{self.handshake_expect.hex(' ').upper() or '—'}，"
            f"包头 {self.framing}，校验 {self.checksum}，"
            f"每块 {self.block_words} 字，超时 {self.timeout_s:g}s"
        )


def builtin_profiles() -> list[SciProfile]:
    """内置档案（顺序即界面下拉顺序）。"""
    return [
        SciProfile(
            key="ti-c2000",
            label="TI C2000 SCI Boot",
            description="自举 0x41 → 0xAA/0x55 握手 → 8 字节包头（字地址/字数/校验）+ 数据 → 等 0x55",
        ),
        SciProfile(
            key="raw",
            label="通用数据流（无包头）",
            description="自举与握手后直接发数据；适合设备自带 bootloader、协议更简单的情况",
            framing="raw",
            checksum="none",
            ack=b"",
            block_words=256,
        ),
    ]


def profile_by_key(key: str) -> SciProfile:
    for profile in builtin_profiles():
        if profile.key == key:
            return profile
    return builtin_profiles()[0]


# --------------------------------------------------------------- 传输层


class Transport(Protocol):
    """烧写用的最小传输接口（真串口与模拟设备都实现它）。"""

    def write(self, data: bytes) -> None: ...

    def read(self, size: int, timeout: float) -> bytes: ...

    def close(self) -> None: ...


class SerialTransport:
    """真串口（pyserial）。打开失败抛 :class:`SciError`，消息给用户看。"""

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: float = 1,
        timeout: float = 0.2,
    ) -> None:
        try:
            import serial  # noqa: PLC0415 - 可选依赖，不用串口时不加载
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise SciError("本机没有 pyserial，打不开串口：pip install pyserial") from exc
        self.description = f"{port} @ {baudrate} {bytesize}{parity}{stopbits:g}"
        try:
            self._serial = serial.Serial(
                port=port,
                baudrate=baudrate,
                bytesize=bytesize,
                parity=parity,
                stopbits=stopbits,
                timeout=timeout,
                write_timeout=max(1.0, timeout * 5),
            )
        except Exception as exc:  # noqa: BLE001 - pyserial 的异常类型不统一
            raise SciError(f"打不开串口 {port}：{exc}") from exc
        self._serial.reset_input_buffer()

    def write(self, data: bytes) -> None:
        if not data:
            return
        try:
            self._serial.write(data)
            self._serial.flush()
        except Exception as exc:  # noqa: BLE001
            raise SciError(f"串口写失败：{exc}") from exc

    def read(self, size: int, timeout: float) -> bytes:
        deadline = time.monotonic() + max(0.0, timeout)
        chunks = bytearray()
        while len(chunks) < size:
            try:
                chunk = self._serial.read(size - len(chunks))
            except Exception as exc:  # noqa: BLE001
                raise SciError(f"串口读失败：{exc}") from exc
            if chunk:
                chunks.extend(chunk)
                continue
            if time.monotonic() >= deadline:
                break
        return bytes(chunks)

    def close(self) -> None:
        try:
            self._serial.close()
        except Exception:  # noqa: BLE001 - 关不上也不该影响结果
            pass


class SimulatedDsp:
    """模拟一台 C2000 风格的 SCI bootloader（自检与测试用）。

    它实现与 :class:`SciBootloader` 对称的状态机：自举回显、握手、按包头收块、
    算校验、写"Flash"，并通过 :attr:`memory` 暴露结果。
    """

    def __init__(self, profile: SciProfile | None = None) -> None:
        self.profile = profile or profile_by_key("ti-c2000")
        self.memory: dict[int, int] = {}
        self.blocks_written = 0
        self.words_written = 0
        self.state = "idle"
        self.errors: list[str] = []
        #: 故障注入：第 N 块之后开始返回 NACK / 不回话 / 数据写错
        self.fail_after_blocks = -1
        self.fail_mode = ""  # nack | silent | corrupt
        self._buffer = bytearray()
        self._header: bytes | None = None

    # -------------------------------------------------- 供传输层调用

    def feed(self, data: bytes) -> bytes:
        """把主机发来的字节喂给设备，返回设备要回给主机的字节。"""
        response = bytearray()
        for byte in data:
            response.extend(self._consume(byte))
        return bytes(response)

    def _consume(self, byte: int) -> bytes:
        profile = self.profile
        if self.state == "idle":
            wanted = profile.autobaud[:1]
            if wanted and byte == wanted[0]:
                self.state = "handshake"
                return profile.autobaud_echo
            return b""
        if self.state == "handshake":
            wanted = profile.handshake_send[:1]
            if wanted and byte == wanted[0]:
                self.state = "data"
                return profile.handshake_expect
            return b""
        if self.state != "data":
            return b""
        if self.fail_after_blocks >= 0 and self.blocks_written >= self.fail_after_blocks:
            if self.fail_mode == "silent":
                return b""
            if self.fail_mode == "nack":
                return b"\xe1"
        if profile.framing == "raw":
            return self._consume_raw(byte)
        return self._consume_c2000(byte)

    def _consume_raw(self, byte: int) -> bytes:
        """无包头：按块长收字节，收满一块就写内存并应答。"""
        self._buffer.append(byte)
        chunk_bytes = max(1, self.profile.block_words) * 2
        if len(self._buffer) >= chunk_bytes:
            data = bytes(self._buffer[:chunk_bytes])
            del self._buffer[:chunk_bytes]
            self._store_raw(data)
            self.blocks_written += 1
            return self.profile.ack
        return b""

    def _store_raw(self, data: bytes) -> None:
        little = self.profile.word_order == "little"
        base = self.words_written
        for index in range(0, len(data), 2):
            low, high = data[index], data[index + 1] if index + 1 < len(data) else 0xFF
            value = (low | (high << 8)) if little else ((low << 8) | high)
            self.memory[base + index // 2] = value
        self.words_written += len(data) // 2

    def finalize(self) -> None:
        """无包头模式没有"块尾"标记：把最后不满一块的数据也写进内存（自检用）。"""
        if self.profile.framing != "raw" or not self._buffer:
            return
        data = bytes(self._buffer)
        del self._buffer[:]
        if len(data) % 2:
            data += b"\xff"
        self._store_raw(data)
        self.blocks_written += 1

    def _consume_c2000(self, byte: int) -> bytes:
        profile = self.profile
        if self._header is None:
            self._buffer.append(byte)
            if len(self._buffer) < 8:
                return b""
            self._header = bytes(self._buffer)
            del self._buffer[:]
            address = int.from_bytes(self._header[0:4], "big")
            count = int.from_bytes(self._header[4:6], "big")
            checksum = int.from_bytes(self._header[6:8], "big")
            self._current = [address, max(1, count), checksum]
            return b""
        self._buffer.append(byte)
        count = self._current[1]
        if len(self._buffer) < count * 2:
            return b""
        data = bytes(self._buffer)
        del self._buffer[:]
        address, _count, checksum = self._current
        self._header = None
        little = profile.word_order == "little"
        words: list[int] = []
        for index in range(0, len(data), 2):
            low, high = data[index], data[index + 1]
            words.append((low | (high << 8)) if little else ((low << 8) | high))
        actual = 0
        for value in words:
            actual += value
        actual &= 0xFFFF
        if profile.checksum == "sum16":
            expected = (0x10000 - actual) & 0xFFFF
            if expected != checksum:
                self.errors.append(
                    f"块校验错：地址 0x{address:04X}，收到 0x{checksum:04X}，算出 0x{expected:04X}"
                )
                return b"\xe1" if profile.ack else b""
        elif profile.checksum == "crc16":
            expected = crc16(data)
            if expected != checksum:
                self.errors.append(f"CRC 错：地址 0x{address:04X}")
                return b"\xe1" if profile.ack else b""
        if self.fail_after_blocks >= 0 and self.blocks_written >= self.fail_after_blocks \
                and self.fail_mode == "corrupt":
            words = [value ^ 0xFFFF for value in words]
        for offset, value in enumerate(words):
            self.memory[address + offset] = value
        self.blocks_written += 1
        self.words_written += len(words)
        return profile.ack

    # -------------------------------------------------- 校验

    def expected_words(self, image: FlashImage) -> dict[int, int]:
        """这份镜像按同样规则应该写进内存的字 ``{字地址: 值}``（测试与回读比对用）。"""
        little = self.profile.word_order == "little"
        expected: dict[int, int] = {}
        for block in image.blocks:
            data = block.data
            address = block.address
            if address % 2:
                data = b"\xff" + data
                address -= 1
            if len(data) % 2:
                data += b"\xff"
            base = address // 2
            for index in range(0, len(data), 2):
                low, high = data[index], data[index + 1]
                value = (low | (high << 8)) if little else ((low << 8) | high)
                expected[base + index // 2] = value
        return expected


class SimulatedTransport:
    """把 :class:`SimulatedDsp` 包装成 :class:`Transport`（无需真串口）。"""

    def __init__(self, device: SimulatedDsp | None = None, profile: SciProfile | None = None) -> None:
        self.device = device or SimulatedDsp(profile)
        self.description = "模拟设备（自检）"
        self.sent = bytearray()
        self._received = bytearray()

    def write(self, data: bytes) -> None:
        self.sent.extend(data)
        self._received.extend(self.device.feed(data))

    def read(self, size: int, timeout: float) -> bytes:
        chunk = bytes(self._received[:size])
        del self._received[:size]
        return chunk

    def close(self) -> None:
        pass


def open_transport(
    port: str,
    *,
    baudrate: int,
    bytesize: int = 8,
    parity: str = "N",
    stopbits: float = 1,
    simulate: bool = False,
    profile: SciProfile | None = None,
) -> Transport:
    """按参数开传输层；``simulate=True`` 时开一台模拟设备。"""
    if simulate:
        return SimulatedTransport(profile=profile)
    return SerialTransport(
        port,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=parity,
        stopbits=stopbits,
    )


# --------------------------------------------------------------- 校验算法


def crc16(data: bytes, polynomial: int = 0x1021, initial: int = 0x0000) -> int:
    """CRC-16/XMODEM（多项式 0x1021，初值 0）。"""
    crc = initial & 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ polynomial) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc & 0xFFFF


# --------------------------------------------------------------- 结果


@dataclass
class FlashResult:
    ok: bool
    message: str
    blocks: int = 0
    words: int = 0
    bytes_sent: int = 0
    elapsed_s: float = 0.0
    log: list[str] = field(default_factory=list)
    cancelled: bool = False

    def as_text(self) -> str:
        lines = [
            f"结果：{'成功' if self.ok else '失败'}",
            f"说明：{self.message}",
            f"块数：{self.blocks}　字数：{self.words}　发送：{self.bytes_sent} 字节",
            f"耗时：{self.elapsed_s:.2f} 秒",
            "",
            "过程：",
        ]
        lines.extend(f"  {entry}" for entry in self.log)
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------- 状态机


class SciBootloader:
    """SCI 烧写状态机：握手 → 分块下发 → 等应答（可选回显校验）。"""

    def __init__(self, transport: Transport, profile: SciProfile | None = None) -> None:
        self.transport = transport
        self.profile = profile or profile_by_key("ti-c2000")
        self.log: list[str] = []
        self.sent_bytes = 0

    # -------------------------------------------------- 内部

    def _log(self, text: str) -> None:
        self.log.append(text)

    def _write(self, data: bytes, note: str = "") -> None:
        self.transport.write(data)
        self.sent_bytes += len(data)
        if note:
            self._log(f"→ {note}：{preview_bytes(data)}")

    def _expect(self, expected: bytes, what: str) -> bytes | None:
        if not expected:
            return b""
        got = self.transport.read(len(expected), self.profile.timeout_s)
        if got != expected:
            self._log(f"✗ {what}：期望 {preview_bytes(expected)}，收到 {preview_bytes(got)}")
            raise SciError(
                f"{what}失败：期望 {preview_bytes(expected)}，收到 {preview_bytes(got) or '（无应答）'}。"
                "检查波特率、接线（TX/RX 是否交叉）、设备是否已进入 SCI 引导模式。"
            )
        self._log(f"← {what}：{preview_bytes(got)}")
        return got

    # -------------------------------------------------- 主流程

    def handshake(self) -> None:
        profile = self.profile
        if profile.autobaud:
            self._write(profile.autobaud, "自举字符")
            if profile.autobaud_echo:
                self._expect(profile.autobaud_echo, "自举回显")
        if profile.handshake_send:
            self._write(profile.handshake_send, "握手")
            if profile.handshake_expect:
                self._expect(profile.handshake_expect, "握手应答")
        self._log("握手完成")

    def chunks(self, image: FlashImage) -> list[tuple[int, bytes]]:
        """把镜像切成 ``[(字地址, 数据字节)]``（已经是下发用的字节序）。"""
        profile = self.profile
        limit = max(1, profile.block_words) * 2
        chunks: list[tuple[int, bytes]] = []
        little = profile.word_order == "little"
        for block in image.blocks:
            data = block.data
            address = block.address
            if address % 2:
                data = b"\xff" + data
                address -= 1
                self._log("提示：有段的起始地址是奇数，前面补了一个 0xFF 占位")
            if len(data) % 2:
                data += b"\xff"
            word_address = address // 2
            for offset in range(0, len(data), limit):
                piece = data[offset : offset + limit]
                if not little:
                    swapped = bytearray()
                    for index in range(0, len(piece), 2):
                        low, high = piece[index], piece[index + 1]
                        swapped.extend((high, low))
                    piece = bytes(swapped)
                chunks.append((word_address + offset // 2, piece))
        return chunks

    def flash(
        self,
        image: FlashImage,
        *,
        progress: Callable[[int, int, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> FlashResult:
        started = time.monotonic()
        profile = self.profile
        chunks = self.chunks(image)
        total_words = image.word_count
        done_words = 0
        done_blocks = 0
        total = len(chunks)
        try:
            self.handshake()
            for index, (address, data) in enumerate(chunks, 1):
                if should_stop is not None and should_stop():
                    raise SciCancelled("已中止")
                words = len(data) // 2
                if profile.framing == "c2000":
                    if profile.checksum == "sum16":
                        total_sum = sum(
                            (data[pos] | (data[pos + 1] << 8)) if profile.word_order == "little"
                            else ((data[pos] << 8) | data[pos + 1])
                            for pos in range(0, len(data), 2)
                        )
                        checksum = (0x10000 - (total_sum & 0xFFFF)) & 0xFFFF
                    elif profile.checksum == "crc16":
                        checksum = crc16(data)
                    else:
                        checksum = 0
                    header = profile.header_bytes(address, words, checksum)
                    self._write(header, f"块 {index}/{total} 包头")
                    self._write(data, f"块 {index}/{total} 数据（{words} 字 @0x{address:04X}）")
                    if profile.verify == "echo":
                        echoed = self.transport.read(len(data), profile.timeout_s)
                        if echoed != data:
                            raise SciError(f"块 {index} 回显校验失败（数据不一致）")
                    if profile.ack:
                        self._expect(profile.ack, f"块 {index} 应答")
                else:
                    self._write(data, f"块 {index}/{total} 数据（{words} 字）")
                    if profile.verify == "echo":
                        echoed = self.transport.read(len(data), profile.timeout_s)
                        if echoed != data:
                            raise SciError(f"块 {index} 回显校验失败（数据不一致）")
                    if profile.ack:
                        self._expect(profile.ack, f"块 {index} 应答")
                done_words += words
                done_blocks = index
                if profile.inter_block_s:
                    time.sleep(profile.inter_block_s)
                if progress is not None:
                    progress(index, total, f"{done_words}/{total_words} 字")
            elapsed = time.monotonic() - started
            message = f"已下发 {len(chunks)} 块、{total_words} 字"
            self._log(f"✓ {message}")
            return FlashResult(
                ok=True,
                message=message,
                blocks=done_blocks,
                words=total_words,
                bytes_sent=self.sent_bytes,
                elapsed_s=elapsed,
                log=list(self.log),
            )
        except SciCancelled as exc:
            elapsed = time.monotonic() - started
            return FlashResult(
                ok=False,
                message=str(exc),
                blocks=done_blocks,
                words=done_words,
                bytes_sent=self.sent_bytes,
                elapsed_s=elapsed,
                log=list(self.log) + ["× 用户中止"],
                cancelled=True,
            )
        except (SciError, FlashError) as exc:
            elapsed = time.monotonic() - started
            self._log(f"✗ {exc}")
            return FlashResult(
                ok=False,
                message=str(exc),
                blocks=done_blocks,
                words=done_words,
                bytes_sent=self.sent_bytes,
                elapsed_s=elapsed,
                log=list(self.log),
            )
        except Exception as exc:  # noqa: BLE001 - 串口异常类型不统一，别让线程静默死掉
            elapsed = time.monotonic() - started
            self._log(f"✗ {exc.__class__.__name__}: {exc}")
            return FlashResult(
                ok=False,
                message=f"{exc.__class__.__name__}: {exc}",
                blocks=done_blocks,
                words=done_words,
                bytes_sent=self.sent_bytes,
                elapsed_s=elapsed,
                log=list(self.log),
            )


def preview_bytes(data: bytes, limit: int = 12) -> str:
    """日志里的字节预览：前几个 + 总长。"""
    if not data:
        return "（无）"
    head = " ".join(f"{byte:02X}" for byte in data[:limit])
    if len(data) > limit:
        return f"{head} …（{len(data)} 字节）"
    return head


def flash_simulated(
    image: FlashImage,
    profile: SciProfile | None = None,
    *,
    progress: Callable[[int, int, str], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[FlashResult, SimulatedDsp]:
    """不开真串口，直接在模拟设备上跑一遍完整流程（自检 / 测试 / 演示）。"""
    device = SimulatedDsp(profile)
    transport = SimulatedTransport(device)
    result = SciBootloader(transport, device.profile).flash(
        image, progress=progress, should_stop=should_stop
    )
    device.finalize()
    return result, device


def compare_words(expected: dict[int, int], actual: dict[int, int]) -> list[str]:
    """比对"应该写的字"和"设备里的字"，返回不一致的说明（空 = 一致）。"""
    problems: list[str] = []
    for address, value in sorted(expected.items()):
        got = actual.get(address)
        if got is None:
            problems.append(f"0x{address:04X} 缺失（应为 0x{value:04X}）")
        elif got != value:
            problems.append(f"0x{address:04X} 应为 0x{value:04X}，实际 0x{got:04X}")
        if len(problems) >= 20:
            problems.append("…（只列前 20 处）")
            break
    return problems


__all__ = [
    "SciBootloader",
    "SciCancelled",
    "SciError",
    "SciProfile",
    "SerialTransport",
    "SimulatedDsp",
    "SimulatedTransport",
    "builtin_profiles",
    "compare_words",
    "crc16",
    "flash_simulated",
    "open_transport",
    "preview_bytes",
    "profile_by_key",
]
