"""连接测试的核心逻辑（不依赖 Qt，可单测、可命令行复用）。

比 ping 更有用的地方：ping 只说明 IP 层通，而现场的问题往往是
**端口没开 / 被防火墙挡 / 网关把 Modbus 端口映射错了 / 串口服务器没上电**。
这里直接做 TCP 连接，并可手工发一串字节看回什么。

设计上把"格式化输出"和"真实连接"分开：:class:`ConnectionTester` 负责连，
:class:`ProbeResult` 负责把人能读的结论攒起来，界面只管显示。
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass, field

#: 单次连接/收发的最大字节数，防止对方是别的服务时收到一堆垃圾
MAX_READ_BYTES = 4096


@dataclass
class Attempt:
    """一次连接的尝试结果。"""

    index: int
    ok: bool
    duration_ms: float = 0.0
    error: str = ""
    sent: bytes = b""
    received: bytes = b""
    read_error: str = ""


@dataclass
class ProbeResult:
    """一次测试的汇总。"""

    host: str = ""
    port: int = 0
    attempts: list[Attempt] = field(default_factory=list)
    payload: bytes = b""

    @property
    def ok_count(self) -> int:
        return sum(1 for item in self.attempts if item.ok)

    @property
    def ok(self) -> bool:
        return bool(self.attempts) and self.ok_count == len(self.attempts)

    @property
    def durations(self) -> list[float]:
        return [item.duration_ms for item in self.attempts if item.ok]

    @property
    def headline(self) -> str:
        """一句话结论。"""
        if not self.attempts:
            return "没有执行测试"
        total = len(self.attempts)
        if self.ok_count == total:
            base = f"{self.host}:{self.port} 连接正常"
            if self.durations:
                base += f"（{min(self.durations):.1f} ms）"
            if total > 1:
                base += f"　{total}/{total} 次全部成功"
            return base
        if self.ok_count == 0:
            first = self.attempts[0].error or "未知原因"
            return f"{self.host}:{self.port} 连接失败：{first}"
        return f"{self.host}:{self.port} 不稳定：{self.ok_count}/{total} 次成功"

    def detail(self) -> str:
        """多行明细，含排查建议。"""
        lines: list[str] = []
        for item in self.attempts:
            if item.ok:
                line = f"[{item.index}] 连接成功，耗时 {item.duration_ms:.1f} ms"
                if item.sent:
                    line += f"\n     发送 {len(item.sent)} 字节：{item.sent.hex(' ').upper()}"
                    if item.read_error:
                        line += f"\n     未收到回应：{item.read_error}"
                    elif item.received:
                        line += (
                            f"\n     收到 {len(item.received)} 字节："
                            f"{item.received.hex(' ').upper()}"
                        )
                    else:
                        line += "\n     未收到回应（对方没发数据）"
                lines.append(line)
            else:
                lines.append(f"[{item.index}] 连接失败：{item.error}")

        if not self.ok and self.attempts:
            lines.append("")
            lines.append("排查建议：")
            lines.extend(_suggestions(self.attempts[0].error))
        elif self.ok_count and self.ok_count < len(self.attempts):
            lines.append("")
            lines.append("时通时不通常见原因：网线/水晶头接触不良、交换机端口有问题、")
            lines.append("多台设备 IP 冲突、网关过载。建议多测几次并检查物理链路。")
        return "\n".join(lines)


def _suggestions(error: str) -> list[str]:
    """按错误类型给排查建议。

    现场最费时间的就是"连不上"，把常见原因列出来能省不少电话费。
    """
    text = error.lower()
    if "拒绝" in error or "refused" in text:
        return [
            "· 端口被明确拒绝：IP 通但该端口没有服务在监听。",
            "· 确认设备的 Modbus/管理端口号（很多网关不是 502，而是 5020 / 8000 / 4196）。",
            "· 若设备只开了 RTU 串口，需要串口服务器把 TCP 转成 RTU。",
        ]
    if "超时" in error or "timeout" in text:
        return [
            "· 超时：多为防火墙/路由器拦了，或 IP 打错、设备没上电。",
            "· 先用 ping 确认 IP 能通，再检查是否有网段/路由问题。",
            "· 现场交换机若做了端口隔离，也会表现为超时。",
        ]
    if "名称" in error or "getaddrinfo" in text or "name" in text:
        return [
            "· 主机名解析失败：换成 IP 试试。",
            "· 若必须用主机名，检查这台电脑的 DNS / hosts 配置。",
        ]
    if "unreachable" in text or "不可达" in error:
        return [
            "· 网络不可达：确认本机 IP/网关配置，以及设备是否在同一网段。",
        ]
    return ["· 检查网线、交换机、设备供电与 IP 配置。"]


class ConnectionTester:
    """按参数执行一次（或多次）连接测试。"""

    def __init__(
        self,
        host: str,
        port: int,
        timeout_s: float = 3.0,
        attempts: int = 1,
        payload: bytes = b"",
        wait_response: bool = True,
        read_timeout_s: float = 3.0,
    ) -> None:
        self.host = (host or "").strip()
        self.port = int(port)
        self.timeout_s = max(0.1, float(timeout_s))
        self.attempts = max(1, min(20, int(attempts)))
        self.payload = bytes(payload or b"")
        self.wait_response = bool(wait_response)
        self.read_timeout_s = max(0.1, float(read_timeout_s))

    def run(self) -> ProbeResult:
        result = ProbeResult(host=self.host, port=self.port, payload=self.payload)
        if not self.host:
            result.attempts.append(Attempt(index=1, ok=False, error="没有填写地址"))
            return result

        for index in range(1, self.attempts + 1):
            result.attempts.append(self._one(index))
        return result

    def _one(self, index: int) -> Attempt:
        attempt = Attempt(index=index, ok=False)
        started = time.perf_counter()
        try:
            with socket.create_connection((self.host, self.port), timeout=self.timeout_s) as sock:
                attempt.ok = True
                attempt.duration_ms = (time.perf_counter() - started) * 1000
                if self.payload:
                    attempt = self._exchange(sock, attempt)
        except socket.timeout:
            attempt.error = f"超时（{self.timeout_s:g} 秒内没连上）"
        except ConnectionRefusedError:
            attempt.error = "连接被拒绝"
        except socket.gaierror as exc:
            attempt.error = f"主机名解析失败：{exc}"
        except OSError as exc:
            attempt.error = f"网络错误：{exc}"
        return attempt

    def _exchange(self, sock: socket.socket, attempt: Attempt) -> Attempt:
        """发一串字节并可选择等回应。收发失败不算连接失败（连接已经建立了）。"""
        try:
            sock.settimeout(self.read_timeout_s)
            sock.sendall(self.payload)
            attempt.sent = self.payload
        except OSError as exc:
            attempt.read_error = f"发送失败：{exc}"
            return attempt

        if not self.wait_response:
            return attempt
        try:
            data = sock.recv(MAX_READ_BYTES)
        except socket.timeout:
            attempt.read_error = f"等待 {self.read_timeout_s:g} 秒没有收到数据"
            return attempt
        except OSError as exc:
            attempt.read_error = f"接收失败：{exc}"
            return attempt
        attempt.received = data
        return attempt


# --------------------------------------------------------------- 文本解析


def parse_hex_payload(text: str) -> tuple[bytes, str]:
    """把用户敲的十六进制文本转成字节。

    容忍空格、逗号、换行、`0x` 前缀、大小写；奇数个字符或非法字符要明确报错
    （现场最常见的是抄漏一位）。
    """
    if not text or not text.strip():
        return b"", ""

    cleaned = (
        text.replace("0x", " ")
        .replace("0X", " ")
        .replace(",", " ")
        .replace(";", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\t", " ")
    )
    chunks = [chunk for chunk in cleaned.split(" ") if chunk]
    # 也接受连写成 "0103000000 0AC5CD" 甚至 "01030000000AC5CD" 的写法
    if len(chunks) == 1 and len(chunks[0]) > 2:
        joined = chunks[0]
        if len(joined) % 2:
            return b"", f"十六进制字符个数是奇数（{len(joined)} 个），可能抄漏了一位"
        chunks = [joined[i : i + 2] for i in range(0, len(joined), 2)]

    out = bytearray()
    for chunk in chunks:
        if len(chunk) % 2:
            return b"", f"「{chunk}」不是完整的字节（应为两位十六进制）"
        try:
            out += bytes.fromhex(chunk)
        except ValueError:
            return b"", f"「{chunk}」不是合法的十六进制"
    return bytes(out), ""


def format_bytes(data: bytes) -> str:
    """按字节分组显示，便于人读。"""
    return data.hex(" ").upper()
