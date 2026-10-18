"""工程门面：一个工程 + 采集引擎 + 历史库，**不依赖 Qt**。

界面里用的是它，外部脚本里用的也是它 —— 这样"脚本能做的事"和"界面能做的事"
走的是同一条代码路径，不会出现"界面里好好的、脚本调用就坏"这种双份实现。

两种用法：

* **脚本/无人值守**：``open_plant("现场工程.iprj")`` 自己建引擎与历史库，
  ``start()`` 开始采集，读写查完了 ``close()``；
* **界面内**：主窗口把自己已经建好的引擎与存储 ``attach`` 进来，
  于是同一套 API 直接映射到用户正在看的这个工程（``MainWindow.api``）。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Iterable

from ..core import project_store
from ..core.alarms import AlarmEvent
from ..core.engine import AcquisitionEngine
from ..core.models import DeviceDef, PointDef, parse_point_id
from ..core.settings import AppSettings
from ..core.tags import DeviceStatus, TagValue
from ..storage.db import Storage
from .envelope import (
    BAD_REQUEST,
    DENIED,
    INTERNAL,
    NOT_FOUND,
    NOT_READY,
    TIMEOUT,
    ApiError,
)

#: 引擎回调 -> 事件名。加新事件时在这里补一行，Plant 会自动接上
_CALLBACK_EVENTS = (
    ("on_tags", "tags"),
    ("on_device_state", "device_state"),
    ("on_alarm", "alarm"),
    ("on_frame", "frame"),
    ("on_log", "log"),
)


def _tag_payload(tag: TagValue) -> dict[str, Any]:
    """``TagValue`` -> 可直接 JSON 化的 dict（脚本那边不用认识内部类）。"""
    value = tag.value
    if isinstance(value, (list, tuple)):
        value = [float(item) if isinstance(item, (int, float)) else item for item in value]
    return {
        "pid": tag.pid,
        "value": value,
        "quality": tag.quality,
        "timestamp": tag.timestamp,
        "age_s": None if tag.timestamp == 0 else round(tag.age_s(), 3),
        "error": tag.error,
        "good": tag.good,
    }


def _status_payload(status: DeviceStatus) -> dict[str, Any]:
    return {
        "name": status.name,
        "state": status.state,
        "connected": status.connected,
        "error": status.error,
        "last_ok_ts": status.last_ok_ts,
        "last_error_ts": status.last_error_ts,
        "ok_count": status.ok_count,
        "error_count": status.error_count,
        "request_count": status.request_count,
        "timeout_count": status.timeout_count,
        "loss_rate": status.loss_rate,
        "recent_loss_rate": status.recent_loss_rate,
        "last_duration_ms": round(status.last_duration_ms, 3),
    }


def _alarm_payload(event: AlarmEvent) -> dict[str, Any]:
    return {
        "timestamp": event.timestamp,
        "time_text": event.time_text,
        "pid": event.pid,
        "device": event.device,
        "tag": event.tag,
        "level": event.level,
        "message": event.message,
        "value": event.value,
        "active": event.active,
        "acked": event.acked,
    }


def _point_meta(device: DeviceDef, point: PointDef) -> dict[str, Any]:
    """点位元数据：脚本要自己算东西时得有量程、单位、地址、限幅这些。"""
    return {
        "pid": f"{device.name}.{point.name}",
        "device": device.name,
        "name": point.name,
        "title": point.title,
        "unit": point.unit,
        "data_type": point.data_type,
        "kind": point.kind,
        "count": point.count,
        "address": point.address,
        "register": point.register,
        "path": point.path,
        "scale": point.scale,
        "offset": point.offset,
        "decimals": point.decimals,
        "writable": point.writable,
        "archive": point.archive,
        "scope": point.scope,
        "group": point.group,
        "description": point.description,
        "write_min": point.write_min,
        "write_max": point.write_max,
        "alarm": {
            "lo_lo": point.alarm.lo_lo,
            "lo": point.alarm.lo,
            "hi": point.alarm.hi,
            "hi_hi": point.alarm.hi_hi,
            "deadband": point.alarm.deadband,
            "enabled": point.alarm.enabled,
        },
    }


class Plant:
    """一个工程的运行时门面。线程安全（读写引擎/存储引用时有锁）。"""

    def __init__(
        self,
        project: Any,
        paths: project_store.ProjectPaths | None = None,
        *,
        engine: AcquisitionEngine | None = None,
        storage: Storage | None = None,
        read_only: bool = False,
        base_dir: str = "",
        workdir: str = "",
    ) -> None:
        self._lock = threading.RLock()
        self.project = project
        self.paths = paths
        self.base_dir = base_dir or (paths.root if paths is not None else "")
        self.workdir = workdir
        self.read_only = bool(read_only)
        self.engine: AcquisitionEngine | None = None
        self.storage: Storage | None = None
        #: 自己建的存储才由自己关（界面接进来的由界面关）
        self._own_storage = False
        self._listeners: list[Callable[[str, Any], None]] = []
        self._chained_engine_id: int | None = None
        self._settings = AppSettings()
        if engine is not None:
            self.attach_engine(engine)
        if storage is not None:
            self.attach_storage(storage)

    # -------------------------------------------------- 装配

    def set_project(self, project: Any, paths: project_store.ProjectPaths | None = None) -> None:
        """换工程（界面里切工程时调用）。引擎与存储由调用方重新 attach。"""
        with self._lock:
            self.project = project
            self.paths = paths if paths is not None else self.paths
            if self.paths is not None:
                self.base_dir = self.paths.root or self.base_dir

    def attach_engine(self, engine: AcquisitionEngine | None) -> None:
        """接上一个采集引擎，并把它的回调转成 API 事件。

        **不抢占**原有回调：原来的回调（界面桥接）照常先收到，再往事件总线发一份。
        同一个引擎重复 attach 不会出现重复事件。
        """
        with self._lock:
            self.engine = engine
            if engine is None:
                self._chained_engine_id = None
                return
            if self._chained_engine_id == id(engine):
                return
            self._chained_engine_id = id(engine)
        for attribute, event in _CALLBACK_EVENTS:
            previous = getattr(engine, attribute, None)
            setattr(engine, attribute, self._make_forwarder(previous, event))

    def _make_forwarder(self, previous: Callable[..., Any] | None,
                        event: str) -> Callable[..., Any]:
        def forward(*args: Any) -> None:
            if previous is not None:
                try:
                    previous(*args)
                except Exception:  # noqa: BLE001 - 界面回调出错不该掐断事件
                    pass
            payload: Any = args[0] if len(args) == 1 else args
            self._publish(event, payload)

        return forward

    def attach_storage(self, storage: Storage | None) -> None:
        with self._lock:
            self.storage = storage
            if storage is not None:
                self._own_storage = False

    def open_history(self) -> Storage:
        """按需打开历史库（脚本用法：不用先关心库在哪）。"""
        with self._lock:
            if self.storage is not None:
                return self.storage
            db_file = getattr(self.paths, "db_file", "") if self.paths is not None else ""
            if not db_file:
                raise ApiError(NOT_READY, "这个工程没有历史库路径（工程目录不完整？）")
            storage = Storage(db_file)
            storage.start()
            self.storage = storage
            self._own_storage = True
            return storage

    # -------------------------------------------------- 事件

    def listen(self, listener: Callable[[str, Any], None]) -> Callable[[], None]:
        """登记一个事件监听器 ``listener(事件名, 负载)``，返回取消函数。

        :func:`icomm.api.build_service` 用它把 Plant 的事件接到
        :class:`~icomm.api.ApiService` 的订阅者上 —— 这样 Plant 本身不需要知道
        "有几个人在听"。
        """
        with self._lock:
            self._listeners.append(listener)

        def remove() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)

        return remove

    def _publish(self, event: str, payload: Any) -> None:
        with self._lock:
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(event, payload)
            except Exception:  # noqa: BLE001 - 监听器自己的问题
                pass

    # -------------------------------------------------- 采集启停

    @property
    def running(self) -> bool:
        return bool(self.engine is not None and self.engine.running)

    def start(self) -> dict[str, Any]:
        """开始采集。没有引擎就自己建一个（脚本用法）。"""
        if not self.read_only and self.engine is None:
            settings = self._settings
            engine = AcquisitionEngine(
                self.project,
                storage=self.storage,
                reconnect_interval_s=settings.reconnect_interval_s,
                write_timeout_s=settings.write_timeout_s,
            )
            self.attach_engine(engine)
        if self.engine is None:
            raise ApiError(NOT_READY, "这个 Plant 没有采集引擎（只读模式下不会自建）")
        self.engine.restart(self.project)
        return self.engine_status()

    def stop(self) -> dict[str, Any]:
        if self.engine is not None:
            self.engine.stop()
        return self.engine_status()

    def close(self) -> None:
        """停采集、关自己开的库。界面接进来的存储不会被关掉。"""
        if self.engine is not None:
            try:
                self.engine.stop()
            except Exception:  # noqa: BLE001 - 关的时候不要再抛
                pass
        with self._lock:
            if self._own_storage and self.storage is not None:
                try:
                    self.storage.stop()
                except Exception:  # noqa: BLE001
                    pass
            if self._own_storage:
                self.storage = None
                self._own_storage = False

    def __enter__(self) -> "Plant":
        # 历史库按需打开（真查历史时才连），进 with 不做阻塞动作
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    # -------------------------------------------------- 点位

    def find_point(self, pid: str) -> tuple[DeviceDef, PointDef]:
        found = self.project.find_point(str(pid or ""))
        if found is None:
            raise ApiError(
                NOT_FOUND,
                f"没有点位 {pid}（点号写法是 设备名.点名）",
                {"device": parse_point_id(str(pid or ""))[0]},
            )
        return found

    def points(self, device: str = "", writable: bool | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for dev, point in self.project.iter_points():
            if device and dev.name != device:
                continue
            if writable is not None and bool(point.writable) != bool(writable):
                continue
            rows.append(_point_meta(dev, point))
        return rows

    def read(self, pid: str) -> dict[str, Any]:
        """读一个点位的**实时缓存值**（采集线程写进来的那份，不发请求）。"""
        self.find_point(pid)
        if self.engine is None:
            raise ApiError(NOT_READY, "还没有采集引擎（先 start() 或接一个引擎）")
        return _tag_payload(self.engine.tags.get(str(pid)))

    def snapshot(self, device: str = "", only_good: bool = False) -> dict[str, dict[str, Any]]:
        """一片点位的实时值；``device`` 非空时只取这台设备。"""
        if self.engine is None:
            raise ApiError(NOT_READY, "还没有采集引擎（先 start() 或接一个引擎）")
        prefix = f"{device}." if device else ""
        rows: dict[str, dict[str, Any]] = {}
        for pid, tag in self.engine.tags.snapshot().items():
            if prefix and not pid.startswith(prefix):
                continue
            if only_good and not tag.good:
                continue
            rows[pid] = _tag_payload(tag)
        return rows

    def waveform(self, pid: str) -> list[float]:
        """数组/块点位的最近一段波形（不是数组点位就 ``not_found``）。"""
        if self.engine is None:
            raise ApiError(NOT_READY, "还没有采集引擎（先 start() 或接一个引擎）")
        device, point = self.find_point(pid)
        if not point.is_array:
            raise ApiError(NOT_FOUND, f"{device.name}.{point.name} 不是数组点位，没有波形")
        data = self.engine.tags.waveform(f"{device.name}.{point.name}")
        return list(data or ())

    def write(self, pid: str, value: Any, timeout: float | None = None) -> dict[str, Any]:
        """写一个点位。**会真的动设备** —— 只读模式下直接拒绝。"""
        if self.read_only:
            raise ApiError(DENIED, "只读模式下不允许写值")
        if self.engine is None:
            raise ApiError(NOT_READY, "还没有采集引擎（先 start()）")
        if not self.engine.running:
            raise ApiError(NOT_READY, "采集还没启动，写值会没人执行（先调 engine.start）")
        device, point = self.find_point(pid)
        ok, message = self.engine.write(str(pid), value, timeout)
        if not ok:
            raise ApiError(self._write_code(message), message, {"pid": pid})
        tag = self.engine.tags.get(str(pid))
        return {"pid": f"{device.name}.{point.name}", "value": tag.value,
                "quality": tag.quality, "timestamp": tag.timestamp}

    @staticmethod
    def _write_code(message: str) -> str:
        """把引擎的失败文案对上错误码（引擎只给中文说明，这里翻译一层）。"""
        text = (message or "")
        if "未找到点位" in text:
            return NOT_FOUND
        if "不可写" in text:
            return DENIED
        if "通道未运行" in text or "先启动采集" in text:
            return NOT_READY
        if "超时" in text or "timeout" in text.lower():
            return TIMEOUT
        return INTERNAL

    # -------------------------------------------------- 状态

    def engine_status(self) -> dict[str, Any]:
        engine = self.engine
        online, total = (0, len(self.project.devices))
        if engine is not None:
            online, total = engine.online_count()
        return {
            "running": bool(engine is not None and engine.running),
            "online": online,
            "devices": total,
            "project": self.project.name,
            "history": bool(self.storage is not None),
            "read_only": self.read_only,
        }

    def device_statuses(self) -> list[dict[str, Any]]:
        engine = self.engine
        if engine is None:
            return [
                _status_payload(DeviceStatus(name=device.name))
                for device in self.project.devices
            ]
        known = {status.name: status for status in engine.device_summary()}
        rows = []
        for device in self.project.devices:
            status = known.get(device.name) or engine.statuses.get(device.name)
            rows.append(_status_payload(status or DeviceStatus(name=device.name)))
        return rows

    # -------------------------------------------------- 历史 / 报警 / 日志

    def _storage_or_open(self) -> Storage:
        if self.storage is not None:
            return self.storage
        return self.open_history()

    def history(self, pids: Iterable[str], start: float | None = None,
                end: float | None = None, limit: int = 20000) -> dict[str, list[list[float]]]:
        """查历史曲线：``{点号: [[时间戳, 值], ...]}``（时间是 Unix 秒）。"""
        pid_list = [str(pid) for pid in pids if str(pid)]
        if not pid_list:
            raise ApiError(BAD_REQUEST, "至少要给一个点号")
        for pid in pid_list:
            self.find_point(pid)
        end_ts = float(end) if end is not None else time.time()
        start_ts = float(start) if start is not None else end_ts - 3600.0
        if start_ts > end_ts:
            start_ts, end_ts = end_ts, start_ts
        raw = self._storage_or_open().query_history(pid_list, start_ts, end_ts, int(limit))
        return {pid: [[ts, value] for ts, value in rows] for pid, rows in raw.items()}

    def alarms(self, limit: int = 500, only_active: bool = False) -> list[dict[str, Any]]:
        return self._storage_or_open().query_alarms(int(limit), bool(only_active))

    def logs(self, limit: int = 500, device: str = "") -> list[dict[str, Any]]:
        return self._storage_or_open().query_logs(int(limit), device or None)

    def storage_stats(self) -> dict[str, int]:
        return self._storage_or_open().stats()


def open_plant(
    path: str,
    *,
    workdir: str | None = None,
    read_only: bool = True,
    with_history: bool = True,
) -> Plant:
    """打开一个工程（目录 / ``project.json`` / ``.iprj`` 都行），返回可用的门面。

    ``read_only`` **默认 True**：给外部脚本用的入口默认不许写值 ——
    写值会真的动设备，要写的人得自己明确传 ``read_only=False``。
    """
    project, paths = project_store.open_project(path, workdir=workdir)
    base_dir = (paths.root or os.path.dirname(os.path.abspath(path))) if paths else ""
    plant = Plant(
        project,
        paths,
        read_only=read_only,
        base_dir=base_dir,
        workdir=workdir or "",
    )
    if with_history:
        try:
            plant.open_history()
        except ApiError:
            pass  # 没有库也能跑（只读实时值），别在打开阶段就炸
    return plant


__all__ = ["Plant", "open_plant"]
