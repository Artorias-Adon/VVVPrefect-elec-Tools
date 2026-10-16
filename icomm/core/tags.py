"""实时点位值表。

采集线程写入、界面线程读取，用一把可重入锁保证安全。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: 质量戳
QUALITY_GOOD = "good"
QUALITY_BAD = "bad"
QUALITY_UNCERTAIN = "uncertain"
QUALITY_STALE = "stale"


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

    def mark_ok(self, duration_ms: float) -> None:
        self.connected = True
        self.error = ""
        self.state = "正常"
        self.last_ok_ts = time.time()
        self.ok_count += 1
        self.last_duration_ms = duration_ms

    def mark_error(self, error: str, duration_ms: float = 0.0) -> None:
        self.connected = False
        self.error = error
        self.state = "通信故障"
        self.last_error_ts = time.time()
        self.error_count += 1
        self.last_duration_ms = duration_ms
