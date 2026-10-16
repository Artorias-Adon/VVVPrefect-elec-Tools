"""断线重连与「采集线程不能死」的回归测试。

现场报过两个症状，这里都锁住：

1. 设备断开一段时间再接回来，上位机连不回去，**必须重启软件**才恢复；
2. 只要回调/归档里抛过一次没预料到的异常，整条通道就再也不采集了
   （界面永远停在断线，同样只能重启）。

两条都靠内置仿真器跑，不需要任何真实硬件。
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest

from icomm.core import project as project_io
from icomm.core.engine import AcquisitionEngine
from icomm.core.tags import QUALITY_GOOD, TagTable
from icomm.storage import Storage
from tools.modbus_sim import ModbusTcpSimulator

#: 独立端口，避免和别的集成测试抢同一个
PORT = 15070

#: 储能示例里的两个遥测点号（设备名.点位名）
VOLTAGE_PID = "储能变流器PCS.A相电压"
BUS_PID = "储能变流器PCS.直流母线电压"


def wait_for(predicate, timeout: float = 12.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def wait_port(host: str, port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), 0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class ReconnectTest(unittest.TestCase):
    def setUp(self) -> None:
        # ignore_cleanup_errors：Windows 上临时目录里还有句柄没释放就删不掉（CI 偶发）
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.project = project_io.bess_project()
        channel = self.project.channels[0]
        channel.options["host"] = "127.0.0.1"
        channel.options["port"] = PORT
        channel.options["timeout"] = 0.6
        channel.options["retry"] = 0
        channel.options["max_gap"] = 8
        for device in self.project.devices:
            device.poll_interval_ms = 200

        self.tags = TagTable()
        self.storage = Storage(os.path.join(self.tmp.name, "history.db"))
        self.storage.start()
        self.logs: list[tuple[str, str]] = []
        self.sim: ModbusTcpSimulator | None = None
        self.engine: AcquisitionEngine | None = None

    def tearDown(self) -> None:
        if self.engine is not None:
            self.engine.stop()
            self.engine = None
        self.stop_sim()
        self.storage.stop()
        self.tmp.cleanup()

    # ---------------------------------------------------------- 夹具

    def start_sim(self) -> None:
        """起一个仿真器（一台"设备"）。"""
        self.sim = ModbusTcpSimulator("127.0.0.1", PORT)
        threading.Thread(
            target=self.sim.serve_forever, kwargs={"freeze": False}, daemon=True
        ).start()
        self.assertTrue(wait_port("127.0.0.1", PORT), "仿真器端口未就绪")

    def stop_sim(self) -> None:
        """把设备"断电"：连监听带已建立的连接一起断掉。"""
        if self.sim is not None:
            self.sim.stop()
            self.sim = None
            time.sleep(0.2)

    def start_engine(self, reconnect_s: float = 0.3) -> AcquisitionEngine:
        engine = AcquisitionEngine(
            self.project, self.tags, self.storage, reconnect_interval_s=reconnect_s
        )
        engine.on_log = lambda channel, level, message: self.logs.append((level, message))
        engine.start()
        self.engine = engine
        return engine

    def quality(self, pid: str) -> str:
        return self.tags.get(pid).quality

    # ---------------------------------------------------------- 用例

    def test_reconnects_after_device_comes_back(self):
        """断线一会儿再接回来，必须自己连回去（回归：以前要重启软件）。"""
        self.start_sim()
        self.start_engine()
        self.assertTrue(
            wait_for(lambda: self.quality(VOLTAGE_PID) == QUALITY_GOOD, 25.0),
            f"首次连接就没成功：{self.logs[:5]}",
        )

        # 设备掉线
        self.stop_sim()
        self.assertTrue(
            wait_for(lambda: self.quality(VOLTAGE_PID) != QUALITY_GOOD, 20.0),
            "设备已经停了，上位机还认为数据是好的",
        )
        # "断开一段时间"再回来：这段时间里上位机会反复重连失败
        time.sleep(2.5)

        # 设备回来（新实例绑同一个端口）
        self.start_sim()
        self.assertTrue(
            wait_for(lambda: self.quality(VOLTAGE_PID) == QUALITY_GOOD, 40.0),
            f"设备回来了却没重连上（现场就得重启软件）—— 最近日志：{self.logs[-8:]}",
        )
        # 连回来之后数据得是真在刷新，不只是把质量标回 good
        first = self.tags.get(BUS_PID).value
        self.assertTrue(
            wait_for(
                lambda: self.tags.get(BUS_PID).value != first
                and self.tags.get(BUS_PID).quality == QUALITY_GOOD,
                10.0,
            ),
            "重连后数据没有再刷新",
        )

    def test_channel_keeps_polling_when_callback_raises(self):
        """界面回调抛异常不能把采集线程带走（回归：线程一死只能重启）。"""
        self.start_sim()
        engine = AcquisitionEngine(
            self.project, self.tags, self.storage, reconnect_interval_s=0.3
        )

        def boom(_values) -> None:
            raise RuntimeError("故意在回调里抛一个异常")

        engine.on_tags = boom
        engine.on_log = lambda channel, level, message: self.logs.append((level, message))
        engine.start()
        self.engine = engine

        self.assertTrue(
            wait_for(lambda: self.quality(BUS_PID) == QUALITY_GOOD, 25.0),
            "回调抛异常后连第一拍都没读到",
        )
        self.assertTrue(
            wait_for(
                lambda: any("采集异常" in message for _level, message in self.logs), 15.0
            ),
            f"兜底日志没出现，说明异常没被挡住：{self.logs[-5:]}",
        )

        # 线程还活着的话，标签值会继续刷新（值本身是变化的模拟量）
        seen = set()
        deadline = time.time() + 8.0
        while time.time() < deadline:
            value = self.tags.get(BUS_PID).value
            if value is not None:
                seen.add(round(float(value), 1))
            time.sleep(0.1)
        self.assertGreaterEqual(
            len(seen), 3, "回调抛异常后采集线程死了，数据不再更新"
        )


if __name__ == "__main__":
    unittest.main()
