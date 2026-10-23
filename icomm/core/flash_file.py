"""DSP 烧写文件：解析、合并、校验、在工程目录里找文件（纯逻辑，不依赖 Qt）。

支持现场最常见的几种"烧写文件"：

* **TI-TXT**（`.txt` / `.hex`）：``@地址`` + 十六进制字节 + ``q`` 结束。C2000 的
  ``hex2000`` 工具默认就能导出这种；
* **Intel HEX**（`.hex` / `.ihex`）：``:LLAAAATT…CC``；
* **Motorola S-Record**（`.s19` / `.s28` / `.s37` / `.mot`）：``S1/S2/S3``；
* **原始二进制**（`.bin`）：需要给一个起始地址。

格式按内容自动识别，不靠扩展名（现场经常把 TI-TXT 存成 `.hex`）。
``.out``（COFF/ELF）是**带调试信息的可执行文件**，不是裸数据，不能直接烧；
这里给出明确提示，让用户用厂商工具转成 hex/bin。

地址一律按**字节地址**统一表示（16 位机上一个字 = 2 字节），
发送时再由 SCI 协议层按 16 位字重排。
"""

from __future__ import annotations

import os
import re
import time
import zlib
from dataclasses import dataclass, field
from typing import Iterable, Sequence

#: 认得出的烧写文件扩展名（用来在工程目录里找文件）
FLASH_EXTENSIONS: tuple[str, ...] = (
    ".hex",
    ".txt",
    ".bin",
    ".s19",
    ".s28",
    ".s37",
    ".mot",
    ".ihex",
    ".a00",
    ".e00",
)

#: 明显不是裸数据的扩展名：给提示，别让用户白等
UNSUPPORTED_EXTENSIONS: dict[str, str] = {
    ".out": "这是 COFF/ELF 可执行文件（带调试信息），不是裸数据。"
            "请用厂商的 hex 转换工具（如 C2000 的 hex2000）导出 .hex 或 .bin。",
    ".elf": "这是 ELF 可执行文件，不是裸数据。请用厂商工具导出 .hex 或 .bin。",
    ".map": "这是链接映射表，不是烧写数据。",
}

#: 找文件时跳过的目录（构建产物/版本库，进去也只会找到一堆噪声）
SKIP_DIRS = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    "node_modules",
    "build",
    ".venv",
    "venv",
    "_archive",
}

_TI_TXT_LINE = re.compile(r"^[0-9A-Fa-f]{2}(\s+[0-9A-Fa-f]{2})*\s*$")


class FlashError(ValueError):
    """烧写文件读不了 / 内容不合法（消息直接给用户看）。"""


# --------------------------------------------------------------- 数据模型


@dataclass(frozen=True)
class FlashBlock:
    """一段连续的烧写数据（字节地址 + 字节）。"""

    address: int
    data: bytes

    @property
    def end(self) -> int:
        """结束地址（**不含**）。"""
        return self.address + len(self.data)


@dataclass
class FlashImage:
    """一份完整的烧写镜像：若干段 + 起始执行地址（可空）。"""

    blocks: list[FlashBlock] = field(default_factory=list)
    path: str = ""
    fmt: str = ""
    entry: int | None = None
    #: 解析时收集的非致命提示（例如"跳过了空行""段之间有空洞"）
    notes: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.blocks = _normalize(self.blocks, source=self.path)

    # -------------------------------------------------- 统计

    @property
    def start_address(self) -> int:
        return min((block.address for block in self.blocks), default=0)

    @property
    def end_address(self) -> int:
        return max((block.end for block in self.blocks), default=0)

    @property
    def byte_count(self) -> int:
        return sum(len(block.data) for block in self.blocks)

    @property
    def word_count(self) -> int:
        """按 16 位字算的字节数（奇数长度向上取整，末尾补 0xFF）。"""
        return sum((len(block.data) + 1) // 2 for block in self.blocks)

    def bytes_in_order(self) -> bytes:
        return b"".join(block.data for block in self.blocks)

    def checksum16(self) -> int:
        """16 位字节和（按大端两两成字，奇数补 0xFF），用于展示与粗校验。"""
        total = 0
        for block in self.blocks:
            data = block.data
            if len(data) % 2:
                data += b"\xff"
            for index in range(0, len(data), 2):
                total += (data[index] << 8) | data[index + 1]
        return total & 0xFFFF

    def crc32(self) -> int:
        return zlib.crc32(self.bytes_in_order()) & 0xFFFFFFFF

    def is_empty(self) -> bool:
        return not any(block.data for block in self.blocks)

    def describe(self) -> str:
        """一行给人看的摘要（界面列表、日志都用它）。"""
        if not self.blocks:
            return "空镜像"
        parts = [f"{len(self.blocks)} 段", f"{self.byte_count} 字节"]
        if len(self.blocks) == 1:
            parts.insert(0, f"0x{self.start_address:08X}–0x{self.end_address:08X}")
        else:
            parts.insert(0, f"0x{self.start_address:08X} … 0x{self.end_address:08X}")
        parts.append(f"校验和 0x{self.checksum16():04X}")
        return " · ".join(parts)

    def summary_lines(self) -> list[str]:
        lines = [f"格式：{self.fmt or '未知'}", f"文件：{self.path or '(内存)'}", self.describe()]
        for index, block in enumerate(self.blocks, 1):
            lines.append(
                f"  段 {index}：0x{block.address:08X}–0x{block.end:08X}"
                f"（{len(block.data)} 字节）"
            )
        if self.entry is not None:
            lines.append(f"入口地址：0x{self.entry:08X}")
        lines.extend(f"提示：{note}" for note in self.notes)
        return lines

    # -------------------------------------------------- 输出

    def to_ti_txt(self) -> str:
        """导回 TI-TXT（便于用别的工具复核或再烧一次）。"""
        lines: list[str] = []
        for block in self.blocks:
            lines.append(f"@{block.address:04X}")
            data = block.data
            for index in range(0, len(data), 16):
                lines.append(" ".join(f"{byte:02X}" for byte in data[index : index + 16]))
        lines.append("q")
        return "\n".join(lines) + "\n"

    def words(self, order: str = "little") -> list[tuple[int, int]]:
        """``[(字地址, 16 位值)]``：按 16 位字拆（SCI 烧写真正下发的东西）。"""
        result: list[tuple[int, int]] = []
        little = (order or "little").lower() != "big"
        for block in self.blocks:
            data = block.data
            base = block.address // 2
            if block.address % 2:
                # 奇数地址：前面补一个 0xFF 占位，保证字对齐（并在提示里说过）
                data = b"\xff" + data
                base = block.address // 2
            if len(data) % 2:
                data += b"\xff"
            for index in range(0, len(data), 2):
                low, high = data[index], data[index + 1]
                value = (low | (high << 8)) if little else ((low << 8) | high)
                result.append((base + index // 2, value))
        return result


def _normalize(blocks: Sequence[FlashBlock], source: str = "") -> list[FlashBlock]:
    """排序 + 合并相邻段；**重叠且内容不同**时报错（宁可停下也不猜该信谁）。"""
    ordered = sorted((block for block in blocks if block.data), key=lambda b: b.address)
    merged: list[FlashBlock] = []
    for block in ordered:
        if merged and block.address < merged[-1].end:
            previous = merged[-1]
            overlap = previous.end - block.address
            if block.data[:overlap] != previous.data[-overlap:]:
                raise FlashError(
                    f"数据段重叠且内容不一致：0x{block.address:08X}"
                    f"（{source or '输入'}）。这说明文件本身有问题，别往下烧。"
                )
            tail = block.data[overlap:]
            if tail:
                merged[-1] = FlashBlock(previous.address, previous.data + tail)
            continue
        if merged and block.address == merged[-1].end:
            merged[-1] = FlashBlock(merged[-1].address, merged[-1].data + block.data)
            continue
        merged.append(block)
    return merged


# --------------------------------------------------------------- 解析：按内容认格式


def detect_format(text: str) -> str:
    """按内容判断是哪种文本格式（认不出返回空串）。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()][:200]
    for line in lines:
        if line.startswith("@"):
            return "ti-txt"
        if line.startswith(":"):
            return "ihex"
        if len(line) > 2 and line[0] in "Ss" and line[1] in "0123456789":
            return "srec"
    # 没有地址行、全是十六进制字节：仍按 TI-TXT 处理，
    # 这样报错能说清"数据在地址行之前"，而不是笼统的"认不出格式"
    if lines and all(_TI_TXT_LINE.match(line) for line in lines):
        return "ti-txt"
    return ""


def parse_text(text: str, fmt: str = "", path: str = "") -> FlashImage:
    """解析文本形式的烧写文件（TI-TXT / Intel HEX / S-Record）。"""
    body = (text or "").lstrip("\ufeff")
    if not body.strip():
        raise FlashError("文件是空的。")
    chosen = (fmt or detect_format(body) or "").lower()
    if chosen in ("ti-txt", "ti_txt", "titxt"):
        return _parse_ti_txt(body, path)
    if chosen in ("ihex", "intel-hex", "hex"):
        return _parse_ihex(body, path)
    if chosen in ("srec", "s-record", "s19"):
        return _parse_srec(body, path)
    raise FlashError(
        "认不出这是什么格式（既不像 TI-TXT，也不像 Intel HEX / S-Record）。"
        "请确认导出的是烧写数据，而不是 .out / .elf 这类可执行文件。"
    )


def parse_file(path: str, base_address: int = 0, fmt: str = "") -> FlashImage:
    """按文件读入并解析；``.bin`` 用 ``base_address`` 当起始地址。"""
    if not path or not os.path.isfile(path):
        raise FlashError(f"文件不存在：{path}")
    extension = os.path.splitext(path)[1].lower()
    if extension in UNSUPPORTED_EXTENSIONS:
        raise FlashError(UNSUPPORTED_EXTENSIONS[extension])
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError as exc:
        raise FlashError(f"读不了这个文件：{exc}") from exc
    if not raw:
        raise FlashError("文件是空的。")
    if extension in (".bin",) or (fmt or "").lower() == "bin":
        if base_address < 0:
            raise FlashError("起始地址不能是负数。")
        image = FlashImage(
            blocks=[FlashBlock(int(base_address), raw)],
            path=path,
            fmt="bin",
        )
        image.notes.append(f"二进制文件按起始地址 0x{base_address:08X} 装入")
        return image
    text = _decode_text(raw)
    image = parse_text(text, fmt=fmt, path=path)
    if extension in (".hex", ".ihex") and image.fmt not in ("ihex", "ti-txt", "srec"):
        image.notes.append(f"扩展名是 {extension}，但内容按 {image.fmt} 解析")
    return image


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


# --------------------------------------------------------------- TI-TXT


def _parse_ti_txt(text: str, path: str) -> FlashImage:
    blocks: list[FlashBlock] = []
    notes: list[str] = []
    current: int | None = None
    buffer = bytearray()
    terminated = False

    def flush() -> None:
        nonlocal buffer, current
        if current is not None and buffer:
            blocks.append(FlashBlock(current, bytes(buffer)))
        buffer = bytearray()

    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if line.lower() in ("q", "q\n"):
            terminated = True
            break
        if line.startswith("@"):
            flush()
            token = line[1:].strip()
            try:
                current = int(token, 16)
            except ValueError as exc:
                raise FlashError(f"第 {number} 行的地址看不懂：{line}") from exc
            continue
        if line.startswith("//") or line.startswith(";"):
            continue
        if current is None:
            raise FlashError(f"第 {number} 行出现在地址行之前（TI-TXT 要先写 @地址）。")
        if not _TI_TXT_LINE.match(line):
            raise FlashError(
                f"第 {number} 行不是十六进制字节：{line[:40]}。"
                "TI-TXT 每行是两位十六进制、用空格分开。"
            )
        buffer.extend(int(token, 16) for token in line.split())
    flush()
    if not blocks:
        raise FlashError("TI-TXT 里没读到任何数据（只有地址行？）。")
    if not terminated:
        notes.append("文件没有以 q 结尾，按已读到的内容处理")
    return FlashImage(blocks=blocks, path=path, fmt="ti-txt", notes=notes)


# --------------------------------------------------------------- Intel HEX


def _parse_ihex(text: str, path: str) -> FlashImage:
    blocks: list[FlashBlock] = []
    notes: list[str] = []
    base = 0
    entry: int | None = None
    seen_eof = False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise FlashError(f"第 {number} 行不是 Intel HEX 记录（要以 : 开头）。")
        payload = line[1:]
        if len(payload) < 10 or len(payload) % 2:
            raise FlashError(f"第 {number} 行的长度不对：{line[:40]}")
        try:
            record = bytes.fromhex(payload)
        except ValueError as exc:
            raise FlashError(f"第 {number} 行含非十六进制字符：{line[:40]}") from exc
        length = record[0]
        if len(record) != length + 5:
            raise FlashError(f"第 {number} 行的字节数对不上（声明 {length}）。")
        address = (record[1] << 8) | record[2]
        kind = record[3]
        data = record[4 : 4 + length]
        checksum = record[-1]
        if (sum(record[:-1]) + checksum) & 0xFF:
            raise FlashError(f"第 {number} 行校验和不对（文件可能被改坏了）。")
        if kind == 0x00:
            absolute = base + address
            if data:
                blocks.append(FlashBlock(absolute, data))
        elif kind == 0x01:
            seen_eof = True
            break
        elif kind == 0x02:
            if length != 2:
                raise FlashError(f"第 {number} 行的扩展段地址长度不对。")
            base = ((data[0] << 8) | data[1]) << 4
        elif kind == 0x04:
            if length != 2:
                raise FlashError(f"第 {number} 行的扩展线性地址长度不对。")
            base = ((data[0] << 8) | data[1]) << 16
        elif kind == 0x03:
            if len(data) >= 4:
                entry = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
        elif kind == 0x05:
            if len(data) >= 4:
                entry = (data[0] << 24) | (data[1] << 16) | (data[2] << 8) | data[3]
        else:
            notes.append(f"第 {number} 行是未知记录类型 0x{kind:02X}，已跳过")
    if not blocks:
        raise FlashError("Intel HEX 里没读到数据记录。")
    if not seen_eof:
        notes.append("文件没有 :00000001FF 结束记录，按已读到的内容处理")
    return FlashImage(blocks=blocks, path=path, fmt="ihex", entry=entry, notes=notes)


# --------------------------------------------------------------- S-Record


def _parse_srec(text: str, path: str) -> FlashImage:
    blocks: list[FlashBlock] = []
    notes: list[str] = []
    entry: int | None = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        if len(line) < 4 or line[0] not in "Ss":
            raise FlashError(f"第 {number} 行不是 S-Record。")
        kind = line[1]
        try:
            record = bytes.fromhex(line[2:])
        except ValueError as exc:
            raise FlashError(f"第 {number} 行含非十六进制字符。") from exc
        count = record[0]
        if len(record) - 1 != count:
            raise FlashError(f"第 {number} 行的字节数对不上（声明 {count}）。")
        checksum = record[-1]
        if (sum(record[:-1]) + checksum) & 0xFF != 0xFF:
            raise FlashError(f"第 {number} 行校验和不对（文件可能被改坏了）。")
        address_size = {"0": 2, "1": 2, "2": 3, "3": 4, "5": 2, "6": 3, "7": 4, "8": 3, "9": 2}.get(kind)
        if address_size is None:
            notes.append(f"第 {number} 行的记录类型 S{kind} 不认识，已跳过")
            continue
        body = record[1:-1]
        if len(body) < address_size:
            raise FlashError(f"第 {number} 行的地址不完整。")
        address = int.from_bytes(body[:address_size], "big")
        data = body[address_size:]
        if kind in ("1", "2", "3"):
            if data:
                blocks.append(FlashBlock(address, data))
        elif kind in ("7", "8", "9"):
            entry = address
    if not blocks:
        raise FlashError("S-Record 里没读到数据记录。")
    return FlashImage(blocks=blocks, path=path, fmt="srec", entry=entry, notes=notes)


# --------------------------------------------------------------- 在工程目录里找


@dataclass(frozen=True)
class FlashFile:
    """工程目录里找到的一个候选烧写文件。"""

    path: str
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def extension(self) -> str:
        return os.path.splitext(self.path)[1].lower()

    def size_text(self) -> str:
        size = float(self.size)
        for unit in ("B", "KB", "MB"):
            if size < 1024 or unit == "MB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} MB"

    def time_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.mtime))


def discover_files(
    roots: Iterable[str],
    *,
    max_depth: int = 5,
    limit: int = 300,
) -> list[FlashFile]:
    """在若干目录里找烧写文件，按修改时间从新到旧返回。

    跳过构建产物/版本库目录，最多下探 ``max_depth`` 层、最多返回 ``limit`` 个
    —— 工程目录里可能塞着几万个文件，不能无脑遍历。
    """
    found: list[FlashFile] = []
    seen: set[str] = set()
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        base_depth = root.rstrip("\\/").count(os.sep)
        for current, dirnames, filenames in os.walk(root):
            if current.count(os.sep) - base_depth >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = [
                    name for name in dirnames if name.lower() not in SKIP_DIRS
                ]
            for name in filenames:
                extension = os.path.splitext(name)[1].lower()
                if extension not in FLASH_EXTENSIONS:
                    continue
                path = os.path.join(current, name)
                key = os.path.normcase(os.path.abspath(path))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if stat.st_size <= 0:
                    continue
                found.append(FlashFile(path=path, size=stat.st_size, mtime=stat.st_mtime))
                if len(found) >= limit:
                    break
            if len(found) >= limit:
                break
        if len(found) >= limit:
            break
    found.sort(key=lambda entry: entry.mtime, reverse=True)
    return found


def format_address(address: int | None) -> str:
    return "—" if address is None else f"0x{int(address):08X}"
