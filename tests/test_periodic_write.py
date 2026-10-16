"""持续发送（周期写值）与写值队列测试。

持续发送是"上位机会一直往设备写值"的功能，做错了在现场很危险，
所以这里既要验证它真的按周期发，也要验证各种边界：
只读点位不许发、没启动采集不许发、停止后不残留、重开工程不自动开跑。
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest

from icomm.core import project as project_io
from icomm.core.engine import AcquisitionEngine, WriteRequest, _ChannelWorker
from icomm.core.models import Project
from icomm.storage import Storage
from tools.modbus_sim import ModbusTcpSimulator

PORT = 15050

BUS_PID = "储能变流器PCS.有功功率给定"
READONLY_PID = "储能变流器PCS.直流母线电压"
#: 第二台设备（同通道、同从站，用来验证多设备互不阻塞）
TWIN_DEVICE = "电池管理系统BMS"
TWIN_POINT = "SOC上限设定"


def wait_for(predicate, timeout: float = 15.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class _FakeDriver:
    """只记账不通信的驱动，用来单测写值调度。"""

    def __init__(self) -> None:
        self.writes: list[tuple[str, str, object]] = []

    @property
    def is_open(self) -> bool:
        return True

    def open(self) -> None:  # pragma: no cover - 测试用不到
        pass

    def close(self) -> None:  # pragma: no cover
        pass

    def write_point(self, device, point, value) -> None:
        self.writes.append((device.name, point.name, value))


class WriteQueueTest(unittest.TestCase):
    """回归：同一通道上多台设备时，一台的写请求不能挡住另一台。

    原实现遇到"不是本设备"的请求会取出又放回、并直接结束本轮处理，
    结果这条请求每轮都被取出一次、后面的也永远轮不到，写入会被彻底卡死。
    """

    @classmethod
    def setUpClass(cls):
        cls.project = project_io.sample_project()
        cls.channel = cls.project.channels[0]
        cls.devices = [
            d for d in cls.project.devices if d.channel == cls.channel.name and d.enabled
        ]

    def setUp(self):
        self.assertGreaterEqual(len(self.devices), 2, "示例工程需要同通道两台设备")
        self.engine = AcquisitionEngine(self.project)
        self.worker = _ChannelWorker(self.engine, self.channel, self.devices)
        self.driver = _FakeDriver()
        self.worker.driver = self.driver

    def test_other_device_request_does_not_block(self):
        first, second = self.devices[0], self.devices[1]
        request_first = WriteRequest("A", first, first.points[0], 11)
        request_second = WriteRequest("B", second, second.points[0], 22)
        self.worker.writes.put(request_first)
        self.worker.writes.put(request_second)

        self.worker._process_writes(second)

        self.assertTrue(request_second.done.is_set(), "第二台设备的写请求被第一台挡住了")
        self.assertFalse(request_first.done.is_set())
        self.assertEqual(request_first.error, "")
        self.assertEqual(self.driver.writes, [(second.name, second.points[0].name, 22)])

        # 被让路的那条要原样留在队列里，等它自己的设备轮询时处理
        self.assertEqual(self.worker.writes.qsize(), 1)
        pending = self.worker.writes.get_nowait()
        self.assertIs(pending, request_first)

    def test_requests_for_same_device_processed_in_order(self):
        device = self.devices[0]
        requests = [WriteRequest(f"p{i}", device, device.points[0], i) for i in range(3)]
        for request in requests:
            self.worker.writes.put(request)
        self.worker._process_writes(device)
        self.assertEqual([v for _d, _p, v in self.driver.writes], [0, 1, 2])
        self.assertTrue(all(r.done.is_set() for r in requests))

    def test_write_failure_only_fails_that_request(self):
        device = self.devices[0]

        def boom(_device, _point, _value):
            raise RuntimeError("掉线了")

        self.worker.driver.write_point = boom
        bad = WriteRequest("bad", device, device.points[0], 1)
        self.worker.writes.put(bad)
        self.worker._process_writes(device)
        self.assertTrue(bad.done.is_set())
        self.assertIn("掉线了", bad.error)


class PeriodicWriteTest(unittest.TestCase):
    """真机链路上的周期写值。"""

    @classmethod
    def setUpClass(cls):
        cls.sim = ModbusTcpSimulator("127.0.0.1", PORT)
        threading.Thread(
            target=cls.sim.serve_forever, kwargs={"freeze": False}, daemon=True
        ).start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", PORT), 0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("仿真器未就绪")

    @classmethod
    def tearDownClass(cls):
        cls.sim.stop()

    def setUp(self):
        # ignore_cleanup_errors：Windows 上临时目录里只要还有句柄没释放就删不掉（CI 偶发）
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.project = project_io.bess_project()
        self.project.channels[0].options["port"] = PORT
        self.device = self.project.devices[0]
        self.device.poll_interval_ms = 100
        self.engine = AcquisitionEngine(self.project)
        self.storage = Storage(os.path.join(self.tmp.name, "h.db"))
        self.storage.start()
        self.engine.storage = self.storage
        self.engine.start()
        self.addCleanup(self._cleanup)
        self.assertTrue(
            wait_for(
                lambda: self.engine.tags.get("储能变流器PCS.A相电压").quality == "good",
                timeout=15.0,
            ),
            "采集未就绪",
        )

    def _cleanup(self):
        self.engine.stop()
        self.storage.stop()
        self.tmp.cleanup()

    def task(self, pid: str):
        for item in self.engine.periodic_tasks():
            if item.pid == pid:
                return item
        return None

    def test_set_periodic_keeps_writing(self):
        ok, error = self.engine.set_periodic(BUS_PID, 305.0, 200)
        self.assertTrue(ok, error)

        self.assertTrue(
            wait_for(lambda: (self.task(BUS_PID) or None) and self.task(BUS_PID).sent >= 3, 8.0),
            "持续发送没有真的发出去",
        )
        task = self.task(BUS_PID)
        self.assertEqual(task.interval_ms, 200)
        self.assertEqual(task.failed, 0)
        self.assertEqual(task.last_error, "")
        self.assertAlmostEqual(float(self.engine.tags.get(BUS_PID).value), 305.0, delta=1e-3)

    def test_interval_is_respected(self):
        self.engine.set_periodic(BUS_PID, 300.0, 600)
        time.sleep(1.6)
        task = self.task(BUS_PID)
        self.assertIsNotNone(task)
        self.assertGreaterEqual(task.sent, 2, "1.6 秒内 600ms 周期至少该发 2 次")
        self.assertLessEqual(task.sent, 4, "发得太快，周期没被尊重")

    def test_write_actually_reaches_device(self):
        """写进去的值要被设备记住：改回读的给定值即可验证。"""
        self.engine.set_periodic(BUS_PID, 312.5, 150)
        self.assertTrue(
            wait_for(
                lambda: abs(float(self.engine.tags.get(BUS_PID).value or 0) - 312.5) < 1e-3,
                8.0,
            )
        )
        time.sleep(0.4)
        self.assertAlmostEqual(
            float(self.engine.tags.get(BUS_PID).value), 312.5, delta=1e-3
        )

    def test_readonly_point_rejected(self):
        ok, error = self.engine.set_periodic(READONLY_PID, 1.0, 200)
        self.assertFalse(ok)
        self.assertIn("不可写", error)
        self.assertEqual(self.project.periodic_writes, [])

    def test_unknown_point_rejected(self):
        ok, error = self.engine.set_periodic("没有这个.点位", 1.0)
        self.assertFalse(ok)
        self.assertIn("未找到", error)

    def test_not_running_rejected(self):
        self.engine.stop()
        ok, error = self.engine.set_periodic(BUS_PID, 300.0)
        self.assertFalse(ok)
        self.assertIn("启动采集", error)

    def test_pause_and_resume(self):
        self.engine.set_periodic(BUS_PID, 300.0, 150)
        self.assertTrue(wait_for(lambda: self.task(BUS_PID).sent >= 2, 6.0))

        self.assertTrue(self.engine.set_periodic_enabled(BUS_PID, False))
        time.sleep(0.2)
        frozen = self.task(BUS_PID).sent
        time.sleep(0.6)
        self.assertEqual(self.task(BUS_PID).sent, frozen, "暂停后不该继续发包")
        self.assertFalse(self.task(BUS_PID).enabled)

        self.assertTrue(self.engine.set_periodic_enabled(BUS_PID, True))
        self.assertTrue(wait_for(lambda: self.task(BUS_PID).sent > frozen, 6.0))

    def test_config_is_remembered_in_project(self):
        self.engine.set_periodic(BUS_PID, 333.0, 250)
        entry = self.project.periodic_write(BUS_PID)
        self.assertIsNotNone(entry, "持续发送配置要记进工程")
        self.assertEqual(entry.interval_ms, 250)
        self.assertEqual(entry.value, 333.0)

        restored = Project.from_dict(self.project.to_dict())
        entry = restored.periodic_write(BUS_PID)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.interval_ms, 250)

    def test_clear_stops_and_forgets(self):
        self.engine.set_periodic(BUS_PID, 300.0, 150)
        self.assertTrue(wait_for(lambda: self.task(BUS_PID).sent >= 2, 6.0))

        self.assertTrue(self.engine.clear_periodic(BUS_PID))
        self.assertIsNone(self.task(BUS_PID))
        self.assertIsNone(self.project.periodic_write(BUS_PID))
        self.assertFalse(self.engine.clear_periodic(BUS_PID), "重复清除应返回 False")

    def test_stop_engine_keeps_config_but_stops_sending(self):
        """停止采集后：运行态清空，配置保留（按用户选择：不自动续发）。"""
        self.engine.set_periodic(BUS_PID, 300.0, 150)
        self.assertTrue(wait_for(lambda: self.task(BUS_PID).sent >= 1, 6.0))

        self.engine.stop()
        self.assertEqual(self.engine.periodic_tasks(), [])
        entry = self.project.periodic_write(BUS_PID)
        self.assertIsNotNone(entry, "配置应该留着，下次手动启动")

        # 重新采集也不会自动开跑
        self.engine.start()
        self.assertTrue(
            wait_for(lambda: self.engine.tags.get("储能变流器PCS.A相电压").quality == "good", 15.0)
        )
        time.sleep(0.5)
        self.assertEqual(
            self.engine.periodic_tasks(), [], "重开采集不该自动开始持续发送"
        )

    def test_update_value_and_interval_in_place(self):
        self.engine.set_periodic(BUS_PID, 300.0, 400)
        self.assertTrue(wait_for(lambda: self.task(BUS_PID).sent >= 1, 6.0))
        ok, _error = self.engine.set_periodic(BUS_PID, 320.0, 120)
        self.assertTrue(ok)
        self.assertEqual(len(self.engine.periodic_tasks()), 1, "同一点位只保留一条任务")
        task = self.task(BUS_PID)
        self.assertEqual(task.value, 320.0)
        self.assertEqual(task.interval_ms, 120)

    def test_two_devices_do_not_block_each_other(self):
        """同一条通道上两台设备各自持续发送，互不影响。

        这是写队列那个 bug 的端到端版本：一台设备的写请求若把另一台的挡住，
        两台都发不顺，这里两条任务的计数都要正常增长。

        BESS 的两台设备（PCS / BMS）挂在同一条通道、同一个从站上。
        """
        twin = self.project.device(TWIN_DEVICE)
        self.assertIsNotNone(twin, "示例工程里该有第二台设备")
        twin_pid = f"{TWIN_DEVICE}.{TWIN_POINT}"
        self.assertTrue(
            wait_for(lambda: self.engine.tags.get(twin_pid).quality == "good", 20.0),
            "第二台设备未就绪",
        )

        ok, error = self.engine.set_periodic(BUS_PID, 301.0, 150)
        self.assertTrue(ok, error)
        ok, error = self.engine.set_periodic(twin_pid, 70.0, 150)
        self.assertTrue(ok, error)

        def both_sending() -> bool:
            first = self.task(BUS_PID)
            second = self.task(twin_pid)
            return bool(
                first and second and first.sent >= 2 and second.sent >= 2
            )

        self.assertTrue(wait_for(both_sending, 10.0), "一台设备的发送拖住了另一台")
        for pid in (BUS_PID, twin_pid):
            task = self.task(pid)
            self.assertEqual(task.failed, 0, f"{pid} 持续发送失败：{task.last_error}")

    def test_second_device_writes_do_not_starve_first(self):
        """写队列里混着别的设备的请求时，本设备的单发仍然能通。"""
        twin_pid = f"{TWIN_DEVICE}.{TWIN_POINT}"
        self.assertTrue(
            wait_for(
                lambda: self.engine.tags.get(twin_pid).quality == "good"
                and self.engine.tags.get("储能变流器PCS.A相电压").quality == "good",
                20.0,
            )
        )

        # 两台设备交替单发，任何一次都不该因为队列里混着对方的请求而超时
        for index in range(3):
            ok, error = self.engine.write(twin_pid, 60.0 + index, timeout=3.0)
            self.assertTrue(ok, f"第 {index + 1} 次写第二台设备失败：{error}")
            ok, error = self.engine.write(BUS_PID, 300.0 + index, timeout=3.0)
            self.assertTrue(ok, f"第 {index + 1} 次写第一台设备失败：{error}")


if __name__ == "__main__":
    unittest.main()
