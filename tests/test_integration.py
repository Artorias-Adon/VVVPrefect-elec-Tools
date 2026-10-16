"""端到端集成测试。

链路：内置 Modbus 从站仿真器 <-> Modbus 驱动 <-> 采集引擎 <-> 实时点位表 / SQLite。
无需任何真实硬件。
"""

from __future__ import annotations

import math
import os
import socket
import tempfile
import threading
import time
import unittest

from icomm.core import project as project_io
from icomm.core.engine import AcquisitionEngine
from icomm.core.models import PointDef
from icomm.core.tags import QUALITY_BAD, QUALITY_GOOD, TagTable
from icomm.storage import Storage
from tools.modbus_sim import ModbusTcpSimulator

PORT = 15020


def wait_for(predicate, timeout: float = 12.0, interval: float = 0.05) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _wait_port(host: str, port: int, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), 0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class IntegrationTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sim = ModbusTcpSimulator("127.0.0.1", PORT)
        threading.Thread(
            target=cls.sim.serve_forever, kwargs={"freeze": False}, daemon=True
        ).start()
        if not _wait_port("127.0.0.1", PORT):
            raise RuntimeError("仿真器端口未就绪")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sim.stop()

    def setUp(self) -> None:
        # ignore_cleanup_errors：Windows 上临时目录里只要还有句柄没释放就删不掉（CI 偶发）
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.project = project_io.sample_project()
        channel = self.project.channels[0]
        channel.options["port"] = PORT
        channel.options["timeout"] = 1.0
        channel.options["retry"] = 1
        channel.options["max_gap"] = 8
        for device in self.project.devices:
            device.poll_interval_ms = 200

        self.tags = TagTable()
        self.storage = Storage(os.path.join(self.tmp.name, "history.db"))
        self.storage.start()
        self.alarms: list = []
        self.logs: list = []
        self.engine: AcquisitionEngine | None = None

    def tearDown(self) -> None:
        if self.engine is not None:
            self.engine.stop()
        self.storage.stop()
        self.tmp.cleanup()

    def start_engine(self) -> AcquisitionEngine:
        engine = AcquisitionEngine(self.project, self.tags, self.storage)
        engine.on_alarm = self.alarms.append
        engine.on_log = lambda channel, level, message: self.logs.append((level, message))
        engine.start()
        self.engine = engine
        return engine

    def value_of(self, pid: str):
        return self.tags.get(pid).value

    def quality_of(self, pid: str) -> str:
        return self.tags.get(pid).quality


class ReadTest(IntegrationTestBase):
    def test_reads_all_four_register_types(self):
        self.start_engine()
        self.assertTrue(
            wait_for(lambda: self.quality_of("1号进线柜.A相电压") == QUALITY_GOOD),
            f"未能读到 A相电压，日志：{self.logs[:5]}",
        )

        temp = self.value_of("1号进线柜.变压器温度")
        self.assertIsInstance(temp, float)
        self.assertGreater(temp, 15.0)
        self.assertLess(temp, 75.0)

        voltage = self.value_of("1号进线柜.A相电压")
        self.assertGreater(voltage, 200.0)
        self.assertLess(voltage, 250.0)

        current = self.value_of("1号进线柜.A相电流")
        self.assertGreater(current, 7.0)
        self.assertLess(current, 17.0)

        freq = self.value_of("1号进线柜.电网频率")
        self.assertAlmostEqual(freq, 50.0, delta=0.3)

        energy = self.value_of("1号进线柜.有功电能")
        self.assertIsInstance(energy, int)
        self.assertGreater(energy, 0)

        power_factor = self.value_of("1号进线柜.功率因数")
        self.assertGreater(power_factor, 0.85)
        self.assertLess(power_factor, 1.0)

        # 线圈 / 离散输入
        self.assertFalse(self.value_of("1号进线柜.主开关"))
        self.assertFalse(self.value_of("1号进线柜.运行指示"))
        self.assertIsInstance(self.value_of("1号进线柜.柜门状态"), bool)

        # 输入寄存器
        status_word = self.value_of("1号进线柜.设备状态字")
        self.assertIsInstance(status_word, int)
        self.assertGreaterEqual(status_word, 1)

    def test_second_unit_on_same_channel(self):
        self.start_engine()
        self.assertTrue(
            wait_for(lambda: self.quality_of("2号水泵.泵转速") == QUALITY_GOOD),
            "同一通道下的第二台从站未能读到",
        )
        self.assertEqual(self.value_of("2号水泵.泵转速"), 0)  # 泵未启动

    def test_device_status_online(self):
        engine = self.start_engine()
        self.assertTrue(wait_for(lambda: engine.statuses["1号进线柜"].connected))
        status = engine.statuses["1号进线柜"]
        self.assertEqual(status.state, "正常")
        self.assertGreater(status.ok_count, 0)
        self.assertGreaterEqual(status.last_duration_ms, 0.0)
        online, total = engine.online_count()
        self.assertEqual(total, 2)
        self.assertEqual(online, 2)

    def test_bad_point_does_not_break_others(self):
        """越界点位应单独失败，不影响同设备其它点位。"""
        meter = self.project.device("1号进线柜")
        meter.points.append(PointDef(name="越界点位", address=2000, data_type="uint16"))
        self.start_engine()

        pid = "1号进线柜.越界点位"
        self.assertTrue(
            wait_for(lambda: self.quality_of(pid) == QUALITY_BAD and bool(self.tags.get(pid).error)),
            f"越界点位应报错，实际 quality={self.quality_of(pid)} error={self.tags.get(pid).error!r}",
        )
        self.assertTrue(self.tags.get(pid).error)
        self.assertEqual(self.quality_of("1号进线柜.A相电压"), QUALITY_GOOD)

    def test_history_archived(self):
        self.start_engine()
        pid = "1号进线柜.A相电压"
        self.assertTrue(wait_for(lambda: self.quality_of(pid) == QUALITY_GOOD))

        def has_history() -> bool:
            data = self.storage.query_history([pid], time.time() - 120, time.time() + 1)
            return len(data.get(pid, [])) >= 2

        self.assertTrue(wait_for(has_history, timeout=15.0), "历史数据未入库")
        samples = self.storage.query_history([pid], time.time() - 120, time.time() + 1)[pid]
        self.assertEqual(samples, sorted(samples))
        for _ts, value in samples:
            self.assertGreater(value, 200.0)
            self.assertLess(value, 250.0)

    def test_comm_log_written(self):
        self.start_engine()
        self.assertTrue(
            wait_for(lambda: len(self.storage.query_logs(limit=50)) > 0, timeout=10.0),
            "连接日志未入库",
        )


class WriteTest(IntegrationTestBase):
    def test_write_coil_then_readback(self):
        engine = self.start_engine()
        self.assertTrue(wait_for(lambda: engine.statuses["1号进线柜"].connected))

        ok, error = engine.write("1号进线柜.主开关", True)
        self.assertTrue(ok, f"写线圈失败：{error}")

        # 仿真器把线圈 0 映射到线圈 1（运行指示），可验证写值真正下达
        self.assertTrue(
            wait_for(lambda: self.value_of("1号进线柜.运行指示") is True, timeout=8.0),
            "写值未生效",
        )

        ok, error = engine.write("1号进线柜.主开关", False)
        self.assertTrue(ok, f"再次写线圈失败：{error}")

    def test_write_water_pump_speed(self):
        engine = self.start_engine()
        self.assertTrue(wait_for(lambda: engine.statuses["2号水泵"].connected))

        ok, _error = engine.write("2号水泵.泵运行", True)
        self.assertTrue(ok)
        self.assertTrue(
            wait_for(lambda: (self.value_of("2号水泵.泵转速") or 0) > 0, timeout=8.0),
            "泵启动后转速未上升",
        )

    def test_write_holding_register_roundtrip(self):
        """写保持寄存器后再读回：验证工程量 <-> 寄存器换算双向一致。"""
        engine = self.start_engine()
        self.assertTrue(wait_for(lambda: engine.statuses["1号进线柜"].connected))

        ok, error = engine.write("1号进线柜.温度设定值", 65.5)
        self.assertTrue(ok, f"写寄存器失败：{error}")

        def readback() -> bool:
            value = self.value_of("1号进线柜.温度设定值")
            return value is not None and abs(float(value) - 65.5) < 0.06

        self.assertTrue(wait_for(readback, timeout=8.0), "设定值读回不一致")

    def test_write_to_readonly_point_rejected(self):
        engine = self.start_engine()
        self.assertTrue(wait_for(lambda: engine.statuses["1号进线柜"].connected))
        ok, error = engine.write("1号进线柜.A相电压", 1.0)
        self.assertFalse(ok)
        self.assertIn("不可写", error)

    def test_write_unknown_point_rejected(self):
        engine = self.start_engine()
        ok, error = engine.write("不存在.点位", 1.0)
        self.assertFalse(ok)
        self.assertIn("未找到", error)


class AlarmIntegrationTest(IntegrationTestBase):
    def test_alarm_raised_and_persisted(self):
        # 把高高限压到 -1，保证第一次采集就触发 critical
        meter = self.project.device("1号进线柜")
        meter.point("变压器温度").alarm.hi_hi = -1.0
        builder = self.start_engine()
        self.assertIsNotNone(builder)

        self.assertTrue(
            wait_for(lambda: len(self.alarms) > 0, timeout=10.0), "未产生报警事件"
        )
        event = self.alarms[0]
        self.assertEqual(event.level, "critical")
        self.assertTrue(event.active)
        self.assertEqual(event.pid, "1号进线柜.变压器温度")

        self.assertTrue(
            wait_for(
                lambda: len(self.storage.query_alarms(limit=50, only_active=True)) > 0,
                timeout=10.0,
            ),
            "报警未入库",
        )
        rows = self.storage.query_alarms(limit=50)
        self.assertEqual(rows[0]["level"], "critical")
        self.assertEqual(rows[0]["device"], "1号进线柜")


class RecoveryTest(IntegrationTestBase):
    def test_disconnect_marks_quality_bad(self):
        """连不上的通道应把该通道设备的所有点位打成 bad，而不是抛异常崩线程。"""
        self.project.channels[0].options["port"] = 15099  # 无人监听
        self.project.channels[0].options["timeout"] = 0.3
        engine = self.start_engine()

        self.assertTrue(
            wait_for(
                lambda: self.quality_of("1号进线柜.A相电压") == QUALITY_BAD, timeout=10.0
            ),
            "断线后点位质量应为 bad",
        )
        self.assertTrue(wait_for(lambda: engine.statuses["1号进线柜"].error, timeout=5.0))
        self.assertFalse(engine.statuses["1号进线柜"].connected)
        self.assertTrue(self.logs, "应记录连接失败日志")


class SstWaveformTest(IntegrationTestBase):
    """SST 示例：数组/块点位（录波）端到端。

    这是"示波器画真实波形"的关键通路：
    仿真器把一段波形放在连续寄存器里 -> 驱动成块读回（自动分块）-> 引擎解码成数组
    -> 实时表可查 -> 可选写入数据库录波表。
    """

    def setUp(self) -> None:
        super().setUp()
        # 换成 SST 工程（unit 11/12/13），端口指向本测试的仿真器
        self.project = project_io.sst_project()
        self.project.channels[0].options["port"] = PORT
        for device in self.project.devices:
            device.poll_interval_ms = 200

    def test_waveform_point_read_as_array(self):
        engine = self.start_engine()
        pid = "级联整流DSP.A相电压波形"
        self.assertTrue(
            wait_for(lambda: self.quality_of(pid) == QUALITY_GOOD, timeout=12.0),
            f"波形点位未读到，日志：{self.logs[:5]}",
        )
        values = engine.tags.waveform(pid)
        self.assertIsNotNone(values)
        self.assertEqual(len(values), 200, "应一次读回 200 个采样点")

        # 200 点 / 100us = 20ms = 一个 50Hz 工频周期，且 scale=0.5、峰值 325V
        self.assertAlmostEqual(max(values), 325.0, delta=25.0)
        self.assertAlmostEqual(min(values), -325.0, delta=25.0)
        # 缓冲区正好一个周期，是循环波形：过零还要算上「末点 -> 首点」那一次
        cyclic = values + values[:1]
        zero_crossings = sum(
            1 for a, b in zip(cyclic, cyclic[1:]) if (a < 0) != (b < 0)
        )
        self.assertEqual(zero_crossings, 2, "一个工频周期应恰好两次过零")

    def test_three_phase_shifted_by_120_degrees(self):
        engine = self.start_engine()
        pids = [
            "级联整流DSP.A相电压波形",
            "级联整流DSP.B相电压波形",
            "级联整流DSP.C相电压波形",
        ]
        self.assertTrue(
            wait_for(lambda: all(self.quality_of(p) == QUALITY_GOOD for p in pids), timeout=15.0)
        )
        waves = [engine.tags.waveform(p) for p in pids]
        for wave in waves:
            self.assertEqual(len(wave), 200)

        # 用基波相位近似校验：B 相落后 A 相 120°
        import cmath

        def fundamental_phase(wave: list[float]) -> float:
            total = sum(
                v * cmath.exp(-2j * math.pi * i / len(wave)) for i, v in enumerate(wave)
            )
            return math.degrees(cmath.phase(total))

        phases = [fundamental_phase(w) for w in waves]
        diff_ab = (phases[1] - phases[0]) % 360
        diff_ac = (phases[2] - phases[0]) % 360
        self.assertAlmostEqual(diff_ab, 240.0, delta=8.0)
        self.assertAlmostEqual(diff_ac, 120.0, delta=8.0)

    def test_waveform_recorded_to_database(self):
        engine = self.start_engine()
        pid = "级联整流DSP.A相电压波形"
        self.assertTrue(wait_for(lambda: self.quality_of(pid) == QUALITY_GOOD, timeout=12.0))

        def has_waveform() -> bool:
            index = self.storage.query_waveform_index(pid, limit=5)
            return bool(index)

        self.assertTrue(wait_for(has_waveform, timeout=12.0), "录波未入库")
        index = self.storage.query_waveform_index(pid, limit=5)
        self.assertEqual(index[0]["n"], 200)
        self.assertEqual(index[0]["dt_us"], 100)

        values = self.storage.query_waveform(index[0]["id"])
        self.assertEqual(len(values), 200)
        self.assertAlmostEqual(max(values), 325.0, delta=30.0)

    def test_array_point_isolated_from_scalar_failure(self):
        """波形点位与标量点位互相隔离：把波形的地址改到越界，标量仍应正常。"""
        rectifier = self.project.device("级联整流DSP")
        rectifier.point("A相电压波形").address = 900   # 900+200 超出仿真器 1024
        self.start_engine()

        # 先等标量点位真正读到值（初始质量戳就是 bad，直接等 bad 会立刻返回）
        scalar = "级联整流DSP.电网线电压"
        self.assertTrue(
            wait_for(lambda: self.quality_of(scalar) == QUALITY_GOOD, timeout=12.0),
            f"标量点位未读到，日志：{self.logs[:5]}",
        )
        # 越界波形点位应当报错（有具体错误原因，说明确实尝试读取过）
        wave = "级联整流DSP.A相电压波形"
        self.assertTrue(
            wait_for(
                lambda: self.quality_of(wave) == QUALITY_BAD and bool(self.tags.get(wave).error),
                timeout=12.0,
            ),
            f"越界波形点位应报错，error={self.tags.get(wave).error!r}",
        )
        self.assertEqual(self.quality_of(scalar), QUALITY_GOOD, "标量点位不应被波形失败连累")
        self.assertEqual(
            self.quality_of("级联整流DSP.B相电压波形"), QUALITY_GOOD, "另一路波形不受影响"
        )

    def test_sst_scalar_values(self):
        engine = self.start_engine()
        self.assertTrue(
            wait_for(lambda: self.quality_of("DCDC DSP.输出电压") == QUALITY_GOOD, timeout=12.0)
        )
        self.assertAlmostEqual(float(engine.tags.get("DCDC DSP.输出电压").value), 750.0, delta=10.0)
        self.assertAlmostEqual(float(engine.tags.get("级联整流DSP.电网线电压").value), 10.0, delta=0.5)
        self.assertAlmostEqual(float(engine.tags.get("计量单元.整机效率").value), 97.5, delta=2.0)
        energy = engine.tags.get("计量单元.累计输出电量").value
        self.assertGreater(int(energy), 5000)


if __name__ == "__main__":
    unittest.main()
