"""电能的"后台记账员"：每秒钟把功率/示数采一次，攒成电量和电费。

为什么不能只放在工具窗口里算：**电费不能因为窗口关了就不算**。
所以这个服务由主窗口持有（``MainWindow.energy``），跟着采集一起跑，
工具窗口只是它的一个显示器（通过 ``ToolContext.service("energy")`` 拿到它）。

* 只在**采集运行中**才记账 —— 采集停了，实时值会停在最后一个数上，
  继续按它积分就等于凭空造电量；
* 每 :data:`AUTOSAVE_S` 秒落一次盘（原子写），退出时再强制存一次；
* 数据文件跟着工程走：``<工程>/数据/energy.json``（没有数据目录时退回程序目录）。

这里是 Qt 的薄壳，真正的算法在 :mod:`icomm.core.energy`（可单测）。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from ..core import energy
from ..core.energy import EnergyBook, Meter, MeterConfig, Tariff

#: 采一次的时间间隔（毫秒）。1 秒足够：功率积分对采样率不敏感，
#: 示数型表更是分钟级变化。
TICK_MS = 1000

#: 多久落一次盘（秒）。断电最多丢这么久的数据。
AUTOSAVE_S = 30.0


class EnergyService(QObject):
    """按工程记账的电能/电费服务。"""

    #: 数据变了（界面据此刷新，不要在里面做重活）
    changed = Signal()
    #: 已落盘
    saved = Signal(str)

    def __init__(
        self,
        project: Any = None,
        data_dir: str = "",
        base_dir: str = "",
        tag_of: Callable[[str], Any] | None = None,
        engine_running: Callable[[], bool] | None = None,
        notify: Callable[..., Any] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.project = project
        self.data_dir = data_dir or ""
        self.base_dir = base_dir or ""
        self._tag_of = tag_of
        self._engine_running = engine_running
        self._notify = notify
        self.book = EnergyBook()
        self.path = ""
        self._last_save = 0.0
        self._dirty = False
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self.tick)
        self.load()

    # -------------------------------------------------- 位置与读写

    def default_path(self) -> str:
        root = self.data_dir or self.base_dir or "."
        return os.path.join(root, "energy.json")

    def set_context(self, project: Any, data_dir: str = "", base_dir: str = "") -> None:
        """换工程/换位置：先把老账存好，再按新位置重新读。"""
        self.save(force=True)
        self.project = project
        self.data_dir = data_dir or ""
        self.base_dir = base_dir or self.data_dir or self.base_dir
        self.book = EnergyBook()
        self.load()
        self.changed.emit()

    def load(self) -> bool:
        """读账本；文件不存在就是空账本（第一次用正常情况）。"""
        self.path = self.default_path()
        self.book = EnergyBook.load(self.path)
        for meter in self.book.meters:
            meter.tariff = self.book.tariff
        self._dirty = False
        return bool(self.book.meters)

    def save(self, force: bool = False) -> str:
        """落盘。数据没变且不是 force 就跳过（1 秒一次写盘没必要）。"""
        if not force and not self._dirty:
            return self.path or ""
        try:
            self.path = self.book.save(self.path or self.default_path())
        except OSError as exc:
            if self._notify is not None:
                self._notify("warning", "电能账本存不下来", f"{self.path}：{exc}")
            return ""
        self._dirty = False
        self._last_save = time.time()
        self.saved.emit(self.path)
        return self.path

    # -------------------------------------------------- 采样

    def start(self) -> None:
        if not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self.save(force=True)

    def value_of(self, pid: str) -> float | None:
        """实时值（拿不到就给 None，界面显示 --，账本不记）。"""
        if self._tag_of is None or not pid:
            return None
        try:
            tag = self._tag_of(pid)
        except Exception:
            return None
        value = getattr(tag, "value", tag)
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    def engine_running(self) -> bool:
        if self._engine_running is None:
            return True
        try:
            return bool(self._engine_running())
        except Exception:
            return False

    def tick(self, now: datetime | None = None) -> bool:
        """采一轮并（按需）落盘。返回这一轮是否有表动过。"""
        if not self.book.meters:
            return False
        if not self.engine_running():
            # 采集停着：把时间戳放空，恢复采集后不会把停着的那段时间算成电量
            for meter in self.book.meters:
                meter.last_ts = None
            return False
        moved = self.book.tick(now or datetime.now(), self.value_of)
        if moved:
            self._dirty = True
            if time.time() - self._last_save >= AUTOSAVE_S:
                self.save()
            self.changed.emit()
        return moved

    # -------------------------------------------------- 电表编辑

    def add_meter(self, config: MeterConfig) -> Meter:
        meter = self.book.add(config)
        self._dirty = True
        self.save(force=True)
        self.changed.emit()
        return meter

    def update_meter(self, index: int, config: MeterConfig) -> bool:
        """改配置。累计值保留（改点位是换测量对象，用户自己要清就点「清零」）。"""
        if not (0 <= index < len(self.book.meters)):
            return False
        meter = self.book.meters[index]
        meter.config = config
        meter.tariff = self.book.tariff
        self._dirty = True
        self.save(force=True)
        self.changed.emit()
        return True

    def remove_meter(self, index: int) -> bool:
        if not self.book.remove(index):
            return False
        self._dirty = True
        self.save(force=True)
        self.changed.emit()
        return True

    def set_tariff(self, tariff: Tariff) -> None:
        self.book.set_tariff(tariff)
        self._dirty = True
        self.save(force=True)
        self.changed.emit()

    def reset(self, scope: str = "total", names: list[str] | None = None) -> int:
        count = self.book.reset(scope, names)
        if count:
            self._dirty = True
            self.save(force=True)
            self.changed.emit()
        return count

    def export_csv(self, path: str, now: datetime | None = None) -> str:
        return self.book.export_csv(path, now)

    # -------------------------------------------------- 展示

    def rows(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = now or datetime.now()
        return [meter.summary(now) for meter in self.book.meters]

    def totals(self, now: datetime | None = None) -> dict[str, float]:
        return self.book.totals(now)


def suggest_for(project) -> MeterConfig | None:
    """从工程里挑一个合适的电表配置（给"添加电表"做默认值）。"""
    return energy.suggest_meter(project)
