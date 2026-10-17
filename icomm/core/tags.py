"""实时点位值表。

采集线程写入、界面线程读取，用一把可重入锁保证安全。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: 质量戳
QUALITY_GOOD = "good"
QUALITY_BAD = "bad"
QUALITY_UNCERTAIN = "uncertain"
QUALITY_STALE = "stale"

#: "最近丢包率"看最近多少轮请求
RECENT_WINDOW = 100


@dataclass
class TagValue:
    """一个点位的实时值。"""

    pid: str
    value: Any = None
    quality: str = QUALITY_BAD
    timestamp: float = 0.0
    error: str = ""

    @property
    def good(self) -> bool:
        return self.quality == QUALITY_GOOD

    @property
    def is_wave(self) -> bool:
        """是否是数组/块点位的一段波形。"""
        return isinstance(self.value, (list, tuple))

    def age_s(self) -> float:
        return time.time() - self.timestamp if self.timestamp else float("inf")


class TagTable:
    """``点号 -> TagValue`` 的线程安全缓存。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._values: dict[str, TagValue] = {}

    def ensure(self, pid: str) -> TagValue:
        with self._lock:
            tv = self._values.get(pid)
            if tv is None:
                tv = TagValue(pid=pid)
                self._values[pid] = tv
            return tv

    def waveform(self, pid: str) -> list[float] | None:
        """取数组/块点位的最近一段波形；不是数组点位返回 None。"""
        value = self.get(pid).value
        if isinstance(value, (list, tuple)):
            return [float(v) for v in value]
        return None

    def get(self, pid: str) -> TagValue:
        with self._lock:
            return self._values.get(pid) or TagValue(pid=pid)

    def update(
        self,
        pid: str,
        value: Any,
        quality: str = QUALITY_GOOD,
        error: str = "",
    ) -> TagValue:
        with self._lock:
            tv = self._values.get(pid)
            if tv is None:
                tv = TagValue(pid=pid)
                self._values[pid] = tv
            tv.value = value
            tv.quality = quality
            tv.timestamp = time.time()
            tv.error = error
            return tv

    def snapshot(self) -> dict[str, TagValue]:
        with self._lock:
            return dict(self._values)

    def mark_device(self, device_name: str, quality: str, error: str = "") -> None:
        """设备通信中断时，把它所有点位打成指定的质量戳。"""
        prefix = f"{device_name}."
        now = time.time()
        with self._lock:
            for pid, tv in self._values.items():
                if pid.startswith(prefix):
                    tv.quality = quality
                    tv.error = error
                    tv.timestamp = now

    def drop_device(self, device_name: str) -> None:
        prefix = f"{device_name}."
        with self._lock:
            for pid in [p for p in self._values if p.startswith(prefix)]:
                self._values.pop(pid, None)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


#: 判"这一轮没拿到数据"的关键词（各驱动/各协议抛的文案不一样，中英文都兜住）。
#: 只列"根本没回应"的：CRC 错、异常响应、长度不足说明**包到了**，不算丢包。
NO_RESPONSE_KEYWORDS: tuple[str, ...] = (
    "无响应",
    "没有响应",
    "未响应",
    "超时",
    "没有收到",
    "未收到",
    "连接失败",
    "连接不可用",
    "连接未建立",
    "无法连接",
    "timeout",
    "timed out",
    "no response",
    "not connected",
    "connection refused",
    "connection reset",
    "broken pipe",
    "cannot connect",
)


def looks_like_no_response(error: str) -> bool:
    """这条错误是不是"发出去没人应答"（=丢包），而不是"应答了但内容不对"。

    区分很重要：CRC 错、异常响应说明**包是到了**的，算丢包会把链路质量说差。
    连接层面的失败（连不上 / 连接断了）也算没拿到数据 —— 现场看到的就是 100% 丢包。
    """
    body = (error or "").lower()
    return any(keyword.lower() in body for keyword in NO_RESPONSE_KEYWORDS)


def format_loss_rate(rate: float | None) -> str:
    """丢包率显示：``0.0%`` / ``12.3%``（没数据时 ``--``）。"""
    if rate is None:
        return "--"
    return f"{max(0.0, float(rate)) * 100:.1f}%"


@dataclass
class DeviceStatus:
    """设备通信状态，供界面显示。"""

    name: str
    connected: bool = False
    error: str = ""
    last_ok_ts: float = 0.0
    last_error_ts: float = 0.0
    ok_count: int = 0
    error_count: int = 0
    last_duration_ms: float = 0.0
    state: str = field(default="未启动")
    #: 一共发起过多少轮请求（成功 + 失败）
    request_count: int = 0
    #: 其中"发出去没拿到数据"的轮数（丢包；连接层面的失败也算）
    timeout_count: int = 0
    #: 最近若干轮有没有回应（True=有回应），算"最近丢包率"用
    recent: deque[bool] = field(default_factory=lambda: deque(maxlen=RECENT_WINDOW))

    def mark_ok(self, duration_ms: float) -> None:
        self.connected = True
        self.error = ""
        self.state = "正常"
        self.last_ok_ts = time.time()
        self.ok_count += 1
        self.last_duration_ms = duration_ms
        self.request_count += 1
        self.recent.append(True)

    def mark_error(self, error: str, duration_ms: float = 0.0) -> None:
        self.connected = False
        self.error = error
        self.state = "通信故障"
        self.last_error_ts = time.time()
        self.error_count += 1
        self.last_duration_ms = duration_ms
        self.request_count += 1
        lost = looks_like_no_response(error)
        if lost:
            self.timeout_count += 1
        self.recent.append(not lost)

    # -------------------------------------------------- 丢包率

    @property
    def loss_rate(self) -> float | None:
        """累计丢包率（无响应轮数 / 请求轮数）；还没发过请求时 ``None``。"""
        if self.request_count <= 0:
            return None
        return self.timeout_count / self.request_count

    @property
    def recent_loss_rate(self) -> float | None:
        """最近 :data:`RECENT_WINDOW` 轮的丢包率（链路刚坏/刚恢复时更灵）。"""
        if not self.recent:
            return None
        lost = sum(1 for answered in self.recent if not answered)
        return lost / len(self.recent)

    def loss_text(self) -> str:
        return format_loss_rate(self.loss_rate)

    def recent_loss_text(self) -> str:
        return format_loss_rate(self.recent_loss_rate)

