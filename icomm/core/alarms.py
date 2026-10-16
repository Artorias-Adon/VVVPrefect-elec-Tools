"""报警判定引擎。

阈值语义（对工程量）：lo_lo < lo < hi < hi_hi
    value >= hi_hi           -> critical
    lo < value < hi          -> 正常
超出阈值触发报警，回到阈值内（带死区）自动复位。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .models import DeviceDef, PointDef, point_id


@dataclass
class AlarmEvent:
    """一条报警事件。"""

    timestamp: float
    pid: str
    device: str
    tag: str
    level: str
    message: str
    value: object = None
    active: bool = True
    acked: bool = False

    @property
    def time_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp))


@dataclass
class _AlarmState:
    level: str = ""
    since: float = 0.0


class AlarmEngine:
    """逐点评估阈值，产生/复位报警事件。"""

    def __init__(self) -> None:
        self._active: dict[str, _AlarmState] = {}
        self._muted: set[str] = set()

    def active_alarms(self) -> dict[str, _AlarmState]:
        return dict(self._active)

    def ack(self, pid: str) -> None:
        self._muted.add(pid)

    def clear(self) -> None:
        self._active.clear()
        self._muted.clear()

    def evaluate(self, device: DeviceDef, point: PointDef, value) -> AlarmEvent | None:
        """返回本次状态变化产生的报警事件；无变化返回 None。"""
        cfg = point.alarm
        pid = point_id(device.name, point.name)

        if cfg is None or not cfg.enabled or value is None or isinstance(value, bool):
            if pid in self._active:
                self._active.pop(pid, None)
                self._muted.discard(pid)
                return self._event(device, point, value, "info", f"{point.title} 报警已复位", False)
            return None

        try:
            v = float(value)
        except (TypeError, ValueError):
            return None

        level, message = self._judge(cfg, v, point)
        prev = self._active.get(pid)

        if level and (prev is None or prev.level != level):
            self._active[pid] = _AlarmState(level=level, since=time.time())
            self._muted.discard(pid)
            return self._event(device, point, v, level, message, True)

        if not level and prev is not None:
            self._active.pop(pid, None)
            self._muted.discard(pid)
            return self._event(device, point, v, "info", f"{point.title} 恢复正常", False)

        return None

    @staticmethod
    def _judge(cfg, v: float, point: PointDef) -> tuple[str, str]:
        """返回 (level, message)，level 为空表示正常。"""
        unit = point.unit or ""
        base = cfg.message or point.title

        if cfg.hi_hi is not None and v >= cfg.hi_hi:
            return "critical", f"{base} 高高限报警 ({v:g}{unit} ≥ {cfg.hi_hi:g})"
        if cfg.hi is not None and v >= cfg.hi:
            return cfg.level if cfg.level in ("warning", "critical") else "warning", (
                f"{base} 高限报警 ({v:g}{unit} ≥ {cfg.hi:g})"
            )
        if cfg.lo_lo is not None and v <= cfg.lo_lo:
            return "critical", f"{base} 低低限报警 ({v:g}{unit} ≤ {cfg.lo_lo:g})"
        if cfg.lo is not None and v <= cfg.lo:
            return cfg.level if cfg.level in ("warning", "critical") else "warning", (
                f"{base} 低限报警 ({v:g}{unit} ≤ {cfg.lo:g})"
            )
        return "", ""

    def _event(
        self,
        device: DeviceDef,
        point: PointDef,
        value,
        level: str,
        message: str,
        active: bool,
    ) -> AlarmEvent:
        return AlarmEvent(
            timestamp=time.time(),
            pid=point_id(device.name, point.name),
            device=device.name,
            tag=point.name,
            level=level,
            message=message,
            value=value,
            active=active,
        )
