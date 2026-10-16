"""采集引擎。

线程模型：
* 每个**通道**一个工作线程，负责该链路上所有设备的轮询、写值、重连；
* 线程通过回调把结果抛给界面层，界面层再转成 Qt 信号（跨线程安全）；
* 写值走每条通道的队列，界面不会因为等一次写响应而卡住。

支持运行时热重载：``stop()`` 后再 ``start()`` 即按新工程配置重建连接。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Iterable

from .. import APP_SHORT
from .alarms import AlarmEngine, AlarmEvent
from .drivers import DriverError, create_driver, driver_names
from .models import ChannelDef, DeviceDef, PointDef, Project, point_id
from .tags import (
    DeviceStatus,
    QUALITY_BAD,
    QUALITY_GOOD,
    TagTable,
)

#: 回调签名
TagCallback = Callable[[dict], None]
StateCallback = Callable[[Any], None]
AlarmCallback = Callable[[Any], None]
FrameCallback = Callable[[str, str, str], None]
LogCallback = Callable[[str, str, str], None]

#: 连接失败后的最小重连间隔（秒）
RECONNECT_INTERVAL_S = 3.0


class WriteRequest:
    __slots__ = ("pid", "device", "point", "value", "done", "error")

    def __init__(self, pid: str, device: DeviceDef, point: PointDef, value: Any) -> None:
        self.pid = pid
        self.device = device
        self.point = point
        self.value = value
        self.done = threading.Event()
        self.error = ""


class PeriodicWrite:
    """一条正在运行的持续发送任务。

    与 :class:`WriteRequest` 不同，它不等响应：周期写值关心的是"一直在发"，
    单次成功与否通过 ``last_error`` / ``sent`` 体现，不阻塞通道线程。
    """

    __slots__ = ("pid", "device_name", "point_name", "value", "interval_ms",
                 "enabled", "sent", "failed", "last_error", "last_sent")

    def __init__(
        self,
        pid: str,
        device_name: str,
        point_name: str,
        value: Any,
        interval_ms: int,
    ) -> None:
        self.pid = pid
        self.device_name = device_name
        self.point_name = point_name
        self.value = value
        self.interval_ms = max(50, int(interval_ms))
        self.enabled = True
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        self.last_sent = 0.0


class _ChannelWorker:
    """一条通道的采集线程。"""

    def __init__(
        self,
        engine: "AcquisitionEngine",
        channel: ChannelDef,
        devices: list[DeviceDef],
    ) -> None:
        self.engine = engine
        self.channel = channel
        self.devices = devices
        self.driver = None
        self.stop_event = threading.Event()
        self.writes: "queue.Queue[WriteRequest]" = queue.Queue()
        self.thread: threading.Thread | None = None
        self._connected = False
        self._last_connect_try = 0.0
        self._periodic_due: dict[str, float] = {}

    # -------------------------------------------------- 线程主体

    def start(self) -> None:
        self.thread = threading.Thread(
            target=self._run, name=f"icomm-{self.channel.name}", daemon=True
        )
        self.thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=timeout)
            self.thread = None
        self._drain_writes("已停止")

    def _drain_writes(self, reason: str) -> None:
        while True:
            try:
                req = self.writes.get_nowait()
            except queue.Empty:
                return
            req.error = reason
            req.done.set()

    def _run(self) -> None:
        try:
            self.driver = create_driver(self.channel)
        except Exception as exc:
            self._mark_all_error(str(exc))
            return

        self.driver.set_callbacks(
            on_frame=lambda direction, text: self._emit_frame(direction, text),
            on_log=lambda level, message: self._emit_log(level, message),
        )

        next_due = {d.name: 0.0 for d in self.devices}

        while not self.stop_event.is_set():
            # 这层 try 是「采集线程绝不能死」的保险：轮询、写值、归档、报警、
            # 界面回调里任何一处抛出没预料到的异常，都只让这一拍失败并记一条日志，
            # 线程继续跑下一拍。否则线程一退出，界面就永远停在断线状态，
            # 必须重启软件才恢复 —— 现场最怕这种问题。
            try:
                next_due = self._tick(next_due)
            except Exception as exc:
                # 这里不标记设备错误、也不拆连接：异常很可能来自界面回调、归档、
                # 报警判定这些跟链路无关的地方，把它们报成"断线"会让整屏数据变灰。
                # 真连不上时下一拍 _ensure_connected 会失败，那时才报断线。
                self._emit_log("error", f"{self.channel.name} 采集异常，已跳过本拍：{exc}")
                # 退半步再重来：异常如果一直抛，不能把自己转成不休止的死循环。
                if self.stop_event.wait(0.5):
                    break
                next_due = {d.name: 0.0 for d in self.devices}

        if self.driver is not None:
            self.driver.close()
        self.driver = None
        self._connected = False

    def _tick(self, next_due: dict[str, float]) -> dict[str, float]:
        """跑一拍：查连接 → 发持续发送 → 轮询到期的设备，返回更新后的到期时刻表。

        抽成单独方法就是为了让 :meth:`_run` 能用一层兜底 ``try`` 把它整个包住。
        """
        self._ensure_connected()

        if not self._connected:
            self._mark_all_error(self._last_error)
            if self.stop_event.wait(self.engine.reconnect_interval_s):
                return next_due
            return {d.name: 0.0 for d in self.devices}

        now = time.monotonic()
        self._service_periodic(now)
        due = [d for d in self.devices if next_due.get(d.name, 0.0) <= now]

        if not due:
            wakes = list(next_due.values())
            periodic_next = self._next_periodic_time()
            if periodic_next is not None:
                wakes.append(periodic_next)
            sleep_for = max(0.01, min(wakes) - now)
            self.stop_event.wait(min(sleep_for, 0.2))
            return next_due

        for device in due:
            if self.stop_event.is_set():
                break
            self._poll_device(device)
            next_due[device.name] = (
                time.monotonic() + max(0.05, device.poll_interval_ms / 1000.0)
            )
        return next_due

    # -------------------------------------------------- 连接管理

    _last_error = ""

    def _ensure_connected(self) -> None:
        if self.driver is None:
            self._connected = False
            return
        if self.driver.is_open:
            self._connected = True
            return
        # 驱动没打开就是没连上。下面那个 return 只是把重连频率限到 2 次/秒，
        # 不能因此保留上一轮的 ``_connected = True`` —— 否则会拿着已经关掉的连接
        # 继续轮询，每台设备读一次错一次，界面一直闪"读取失败"。
        self._connected = False
        now = time.monotonic()
        if now - self._last_connect_try < 0.5:
            return
        self._last_connect_try = now
        try:
            self.driver.open()
            self._connected = True
            self._last_error = ""
        except Exception as exc:
            self._connected = False
            self._last_error = str(exc)
            self._emit_log("error", f"连接 {self.channel.name} 失败：{exc}")

    def _mark_all_error(self, error: str) -> None:
        for device in self.devices:
            self.engine.mark_device_error(device, error or "连接不可用")

    # -------------------------------------------------- 单设备轮询

    def _poll_device(self, device: DeviceDef) -> None:
        self._process_writes(device)

        results: dict = {}
        started = time.perf_counter()
        try:
            results = self.driver.read_points(device, device.points)
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            self.engine.mark_device_error(device, str(exc), duration)
            try:
                self.driver.close()
            except Exception:
                pass
            self._connected = False
            return

        duration = (time.perf_counter() - started) * 1000
        self.engine.apply_results(device, results, duration)
        self._process_writes(device)

    def _service_periodic(self, now: float) -> None:
        """把到期的持续发送任务发出去。

        周期精度受通道轮询节奏限制：周期设得比轮询周期还短时，按轮询周期发
        （Modbus 一条链路上本来就只能串行发帧）。
        """
        tasks = self.engine.periodic_for_channel(self.channel.name)
        active = {task.pid for task in tasks}
        for gone in [pid for pid in self._periodic_due if pid not in active]:
            del self._periodic_due[gone]
        if not tasks:
            return

        by_name = {d.name: d for d in self.devices}
        for task in tasks:
            if not task.enabled or now < self._periodic_due.get(task.pid, 0.0):
                continue
            device = by_name.get(task.device_name)
            point = device.point(task.point_name) if device is not None else None
            if device is None or point is None or not point.writable:
                task.enabled = False
                task.last_error = "点位不存在或不可写"
                self._emit_log("error", f"持续发送 {task.pid} 已停止：点位不存在或不可写")
                continue
            try:
                self.driver.write_point(device, point, task.value)
            except Exception as exc:
                task.failed += 1
                task.last_error = str(exc)
                self._emit_log("error", f"持续发送 {task.pid} 失败：{exc}")
            else:
                task.sent += 1
                task.last_error = ""
                task.last_sent = now
                # 写下去的就是设备的实际给定值，实时表跟着更新，
                # 否则界面显示的还是上一次轮询读回来的旧值
                tag = self.engine.tags.update(task.pid, task.value, QUALITY_GOOD)
                self.engine.emit_tags({task.pid: tag})
            self._periodic_due[task.pid] = now + task.interval_ms / 1000.0

    def _next_periodic_time(self) -> float | None:
        """最近一条持续发送任务的到期时刻，用来决定线程该睡多久。"""
        if not self._periodic_due:
            return None
        return min(self._periodic_due.values())

    def _process_writes(self, device: DeviceDef) -> None:
        """处理本设备的单发写值请求。

        队列里可能混着同通道其它设备的请求，那些要原样放回队列尾部，
        但不能因此中断本次处理 —— 否则一台设备的写请求会一直挡住另一台设备的写入。
        """
        pending: list[WriteRequest] = []
        while True:
            try:
                req = self.writes.get_nowait()
            except queue.Empty:
                break
            if req.device.name != device.name:
                pending.append(req)
                continue
            try:
                self.driver.write_point(device, req.point, req.value)
            except Exception as exc:
                req.error = str(exc)
                self._emit_log("error", f"{device.name}.{req.point.name} 写入失败：{exc}")
            req.done.set()

        for req in pending:
            self.writes.put(req)

    # -------------------------------------------------- 事件上行

    def _emit_log(self, level: str, message: str) -> None:
        if self.engine.on_log:
            self.engine.on_log(self.channel.name, level, message)
        if self.engine.storage is not None:
            self.engine.storage.log(self.channel.name, level, message)

    def _emit_frame(self, direction: str, text: str) -> None:
        if self.engine.on_frame:
            self.engine.on_frame(self.channel.name, direction, text)


class AcquisitionEngine:
    """采集引擎总控。"""

    def __init__(
        self,
        project: Project,
        tag_table: TagTable | None = None,
        storage=None,
        alarm_engine: AlarmEngine | None = None,
        reconnect_interval_s: float = RECONNECT_INTERVAL_S,
        write_timeout_s: float = 3.0,
    ) -> None:
        self.project = project
        self.tags = tag_table or TagTable()
        self.storage = storage
        self.alarms = alarm_engine or AlarmEngine()
        self.statuses: dict[str, DeviceStatus] = {}
        self.running = False
        #: 由软件设置注入，见 icomm.core.settings
        self.reconnect_interval_s = max(0.2, float(reconnect_interval_s))
        self.write_timeout_s = max(0.2, float(write_timeout_s))

        self.on_tags: TagCallback | None = None
        self.on_device_state: StateCallback | None = None
        self.on_alarm: AlarmCallback | None = None
        self.on_frame: FrameCallback | None = None
        self.on_log: LogCallback | None = None

        self._workers: dict[str, _ChannelWorker] = {}
        self._archive_state: dict[str, tuple[float, float]] = {}
        self._periodic: dict[str, PeriodicWrite] = {}
        self._periodic_lock = threading.RLock()

    # -------------------------------------------------- 启停

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.tags.clear()
        self.alarms.clear()
        self.statuses = {}

        for device in self.project.devices:
            self.statuses[device.name] = DeviceStatus(name=device.name, state="未启动")
            for point in device.points:
                self.tags.ensure(point_id(device.name, point.name))

        for channel in self.project.channels:
            if not channel.enabled:
                continue
            devices = [
                d
                for d in self.project.devices
                if d.channel == channel.name and d.enabled
            ]
            if not devices:
                continue
            if channel.driver not in driver_names():
                for device in devices:
                    self.mark_device_error(device, f"未安装驱动 {channel.driver}")
                continue
            worker = _ChannelWorker(self, channel, devices)
            self._workers[channel.name] = worker
            worker.start()

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        for worker in self._workers.values():
            worker.stop()
        self._workers.clear()
        # 持续发送是"运行态"，停止采集即全部停发；
        # 配置本身留在 project.periodic_writes 里，下次手动点开始
        with self._periodic_lock:
            self._periodic.clear()

    def restart(self, project: Project | None = None) -> None:
        self.stop()
        if project is not None:
            self.project = project
        self.start()

    # -------------------------------------------------- 写值

    def write(self, pid: str, value: Any, timeout: float | None = None) -> tuple[bool, str]:
        """同步写值：返回 (是否成功, 错误信息)。界面请放到后台线程调用。"""
        if timeout is None:
            timeout = self.write_timeout_s
        found = self.project.find_point(pid)
        if found is None:
            return False, f"未找到点位 {pid}"
        device, point = found
        if not point.writable:
            return False, f"点位 {point.name} 不可写"

        value, note = point.clamp_write(value)
        if note and point.write_limit_warn:
            self.log("warning", f"{pid} 写值被限幅：{note}，已按 {value} 写入", device.channel)

        worker = self._workers.get(device.channel)
        if worker is None:
            return False, "通道未运行"

        req = WriteRequest(pid, device, point, value)
        worker.writes.put(req)
        if not req.done.wait(timeout):
            return False, "写入超时"
        if req.error:
            return False, req.error
        self.tags.update(pid, value, QUALITY_GOOD)
        self._notify_tags({pid: self.tags.get(pid)})
        return True, ""

    # -------------------------------------------------- 持续发送（周期写值）

    def set_periodic(
        self, pid: str, value: Any, interval_ms: int = 1000
    ) -> tuple[bool, str]:
        """登记并启用一条持续发送任务，同时把配置记进工程。

        返回 ``(是否成功, 错误信息)``。调用方在界面线程即可，
        真正的发送在通道线程里按周期做。
        """
        found = self.project.find_point(pid)
        if found is None:
            return False, f"未找到点位 {pid}"
        device, point = found
        if not point.writable:
            return False, f"点位 {point.name} 不可写"
        if not self.running:
            return False, "请先启动采集再持续发送"
        if self._workers.get(device.channel) is None:
            return False, f"通道 {device.channel} 未运行"

        interval_ms = max(50, int(interval_ms))
        value, note = point.clamp_write(value)
        if note and point.write_limit_warn:
            self.log("warning", f"{pid} 写值被限幅：{note}，已按 {value} 发送", device.channel)
        with self._periodic_lock:
            task = self._periodic.get(pid)
            if task is None:
                task = PeriodicWrite(pid, device.name, point.name, value, interval_ms)
                self._periodic[pid] = task
            else:
                task.value = value
                task.interval_ms = interval_ms
                task.enabled = True
                task.last_error = ""
        self.project.set_periodic_write(pid, value, interval_ms)
        return True, ""

    def clear_periodic(self, pid: str) -> bool:
        """停止并移除一条持续发送，同时从工程配置里删掉。"""
        with self._periodic_lock:
            removed = self._periodic.pop(pid, None)
        self.project.clear_periodic_write(pid)
        return removed is not None

    def set_periodic_enabled(self, pid: str, enabled: bool) -> bool:
        """只停发/续发，保留配置（停止采集后重启采集时不自动续发）。"""
        with self._periodic_lock:
            task = self._periodic.get(pid)
            if task is None:
                return False
            task.enabled = bool(enabled)
            return True

    def periodic_tasks(self) -> list[PeriodicWrite]:
        with self._periodic_lock:
            return list(self._periodic.values())

    def periodic_for_channel(self, channel_name: str) -> list[PeriodicWrite]:
        """本通道上的持续发送任务副本，供通道线程遍历。

        遍历时不能持有锁（发送本身可能阻塞），所以返回副本。
        """
        names = {
            d.name for d in self.project.devices if d.channel == channel_name
        }
        if not names:
            return []
        with self._periodic_lock:
            return [t for t in self._periodic.values() if t.device_name in names]

    # -------------------------------------------------- 结果处理

    def apply_results(self, device: DeviceDef, results: dict, duration_ms: float) -> None:
        changed: dict = {}
        failures = 0

        for point in device.points:
            pid = point_id(device.name, point.name)
            result = results.get(point.name)
            if result is None:
                continue
            if not result.ok:
                failures += 1
                tv = self.tags.update(pid, None, QUALITY_BAD, result.error)
                changed[pid] = tv
                continue

            tv = self.tags.update(pid, result.value, QUALITY_GOOD)
            changed[pid] = tv
            if point.is_array:
                if self.storage is not None and point.record and isinstance(result.value, list):
                    self.storage.add_waveform(
                        pid, result.value, point.sample_interval_us
                    )
                continue
            self._archive(pid, point, result.value)
            self._check_alarm(device, point, result.value)

        if changed:
            self._notify_tags(changed)

        status = self.statuses.setdefault(device.name, DeviceStatus(name=device.name))
        if failures == len(device.points) and device.points:
            status.mark_error("全部点位读取失败", duration_ms)
        else:
            status.mark_ok(duration_ms)
        if self.on_device_state:
            self.on_device_state(status)

    def mark_device_error(self, device: DeviceDef, error: str, duration_ms: float = 0.0) -> None:
        self.tags.mark_device(device.name, QUALITY_BAD, error)
        status = self.statuses.setdefault(device.name, DeviceStatus(name=device.name))
        status.mark_error(error, duration_ms)
        if self.on_device_state:
            self.on_device_state(status)

    def _notify_tags(self, values: dict) -> None:
        if self.on_tags and values:
            self.on_tags(values)

    def emit_tags(self, values: dict) -> None:
        """把点位值抛给界面层（通道线程调用，供持续发送回填用）。"""
        self._notify_tags(values)

    def log(self, level: str, message: str, channel: str = "") -> None:
        """写一条日志（界面 + 数据库），供引擎自身与各工具使用。"""
        if self.on_log is not None:
            self.on_log(channel or APP_SHORT, level, message)
        if self.storage is not None:
            self.storage.log(channel or APP_SHORT, level, message)

    # -------------------------------------------------- 归档

    def _archive(self, pid: str, point: PointDef, value) -> None:
        if self.storage is None or not point.archive:
            return
        if isinstance(value, bool):
            numeric = 1.0 if value else 0.0
        else:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return

        now = time.time()
        last = self._archive_state.get(pid)
        interval = max(1, int(self.project.history_interval_s))

        if last is None:
            self._archive_state[pid] = (now, numeric)
            self.storage.archive(pid, numeric, now)
            return

        last_ts, last_value = last
        if abs(numeric - last_value) > abs(point.deadband) or (now - last_ts) >= interval:
            self._archive_state[pid] = (now, numeric)
            self.storage.archive(pid, numeric, now)

    # -------------------------------------------------- 报警

    def _check_alarm(self, device: DeviceDef, point: PointDef, value) -> None:
        event = self.alarms.evaluate(device, point, value)
        if event is None:
            return
        if self.storage is not None:
            self.storage.add_alarm(
                pid=event.pid,
                device=event.device,
                tag=event.tag,
                level=event.level,
                message=event.message,
                value=event.value,
                active=event.active,
                ts=event.timestamp,
            )
        if self.on_alarm:
            self.on_alarm(event)

    # -------------------------------------------------- 查询

    def device_summary(self) -> list[DeviceStatus]:
        return [self.statuses[d.name] for d in self.project.devices if d.name in self.statuses]

    def online_count(self) -> tuple[int, int]:
        total = len(self.statuses)
        online = sum(1 for s in self.statuses.values() if s.connected)
        return online, total
