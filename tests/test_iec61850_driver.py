"""IEC 61850 驱动测试。

分两层：

* **逻辑层**（不依赖库）：用假的连接对象测引用解析、IED 名前缀、批量分块、
  失败逐项标记、位串转换、报告翻译 —— 这些是最容易写错又最难在现场发现的地方；
* **端到端**：拿内置 ICD 起一个真的 MMS 服务端（`tools/iec61850_sim.py`），
  驱动连上去读，验证"点表按 ICD 写法 + 通道填 IED 名"这条常用路径真的通。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
import unittest

from icomm.core.drivers.iec61850 import (
    DEFAULT_FC,
    FC_NAMES,
    Iec61850Driver,
    normalise_reference,
    reference_key,
)
from icomm.core.models import ChannelDef, DeviceDef, PointDef
from icomm.core.drivers.base import DriverError

try:
    import iec61850  # noqa: F401

    HAVE_LIB = True
except Exception:  # pragma: no cover - 取决于运行环境
    HAVE_LIB = False

REQUIRES_LIB = unittest.skipUnless(HAVE_LIB, "未安装 iec61850 库")


# --------------------------------------------------------------- 假连接


class _FakeConnection:
    """假的 MMS 连接：只实现驱动用到的那几个 async 方法。"""

    def __init__(self, values=None, fail_for=(), marker_for=None):
        self.values = dict(values or {})
        self.fail_for = set(fail_for)
        self.marker_for = dict(marker_for or {})
        self.written: list[tuple] = []
        self.batches: list[list] = []
        self.is_connected = True
        self.report_handlers: dict = {}
        self.polls = 0

    async def read_multiple(self, targets, strict=False):
        self.batches.append(list(targets))
        out = []
        for reference, _fc in targets:
            if reference in self.fail_for:
                out.append({"error": "ObjectAccessDenied", "code": 3})
            elif reference in self.marker_for:
                out.append(self.marker_for[reference])
            else:
                out.append(self.values.get(reference, 0.0))
        return out

    async def write(self, reference, fc, value):
        self.written.append((reference, fc.value if hasattr(fc, "value") else fc, value))

    async def disconnect(self):
        self.is_connected = False

    async def abort(self):
        self.is_connected = False

    async def install_report_handler(self, ref, callback):
        self.report_handlers[ref] = callback

    async def poll_reports(self, timeout_ms):
        self.polls += 1
        return 0

    async def get_server_directory(self):
        return ["VVPSIMMEAS"]

    async def get_logical_device_directory(self, ld):
        return ["LLN0", "MMXU1"]

    async def get_logical_node_directory(self, ln_ref, cls):
        return ["Hz", "A"]

    async def get_data_directory(self, ref):
        return ["mag", "q", "t"]


def _fake_report(entries, rcb="VVPSIMMEAS/LLN0.RP1"):
    class _Entry:
        def __init__(self, ref, value):
            self.ref = ref
            self.value = value

    class _Report:
        def __init__(self):
            self.rcb_reference = rcb
            self.entries = [_Entry(ref, value) for ref, value in entries]

    return _Report()


class _Base(unittest.TestCase):
    def make_driver(self, **options) -> Iec61850Driver:
        opts = {"host": "127.0.0.1", "port": 102, "reports": False}
        opts.update(options)
        channel = ChannelDef(name="61850-1", driver="iec61850", link="tcp", options=opts)
        driver = Iec61850Driver(channel)
        # 不真起事件循环：把投递改成当场跑一遍协程
        driver._loop = object()
        driver._call = lambda factory, timeout=None: asyncio.run(factory())
        return driver

    def attach(self, driver, conn) -> None:
        driver._conn = conn

    def device(self) -> DeviceDef:
        return DeviceDef(name="测量", channel="61850-1", unit_id=1)


# --------------------------------------------------------------- 引用解析


class ReferenceTest(unittest.TestCase):
    def test_plain_reference(self) -> None:
        self.assertEqual(normalise_reference("MEAS/MMXU1.Hz.mag.f"), ("MEAS/MMXU1.Hz.mag.f", ""))

    def test_mms_form_carries_fc(self) -> None:
        """抓包/导出常见的分段写法里第 2 段就是 FC。"""
        self.assertEqual(
            normalise_reference("MEAS$MX$MMXU1$Hz$mag$f"), ("MEAS/MMXU1.Hz.mag.f", "MX")
        )

    def test_leading_slash_is_dropped(self) -> None:
        self.assertEqual(normalise_reference("/MEAS/MMXU1.Hz"), ("MEAS/MMXU1.Hz", ""))

    def test_empty_is_rejected_with_hint(self) -> None:
        with self.assertRaises(DriverError) as ctx:
            normalise_reference("")
        self.assertIn("MEAS/MMXU1.Hz.mag.f", str(ctx.exception))

    def test_no_ld_is_rejected(self) -> None:
        with self.assertRaises(DriverError):
            normalise_reference("MMXU1.Hz.mag.f")

    def test_reference_key_ignores_style(self) -> None:
        self.assertEqual(
            reference_key("MEAS/MMXU1.Hz.mag.f"),
            reference_key("MEAS$MX$MMXU1$Hz$mag$f"),
        )
        self.assertEqual(reference_key("/VVPSIMMEAS/MMXU1.Hz"), "vvpsimmeas/mmxu1/hz")


class ResolveTest(_Base):
    def test_fc_from_register(self) -> None:
        driver = self.make_driver()
        point = PointDef(name="p", register="ST", path="MEAS/MMXU1.Hz")
        self.assertEqual(driver._resolve(self.device(), point), ("MEAS/MMXU1.Hz", "ST"))

    def test_fc_from_path_when_register_empty(self) -> None:
        driver = self.make_driver()
        point = PointDef(name="p", register="", path="MEAS$ST$MMXU1$Hz")
        self.assertEqual(driver._resolve(self.device(), point), ("MEAS/MMXU1.Hz", "ST"))

    def test_default_fc(self) -> None:
        driver = self.make_driver()
        point = PointDef(name="p", register="", path="MEAS/MMXU1.Hz")
        self.assertEqual(driver._resolve(self.device(), point)[1], DEFAULT_FC)

    def test_bogus_register_falls_back(self) -> None:
        """寄存器区写了 Modbus 那套（holding 之类）不该让整个点位废掉。"""
        driver = self.make_driver()
        point = PointDef(name="p", register="holding", path="MEAS/MMXU1.Hz")
        self.assertEqual(driver._resolve(self.device(), point)[1], DEFAULT_FC)

    def test_ied_name_prefix(self) -> None:
        """通道填了 IED 名，点表就能照 ICD 写法填（现场点表都是照 ICD 抄的）。"""
        driver = self.make_driver(ied_name="VVPSIM")
        point = PointDef(name="p", register="MX", path="MEAS/MMXU1.Hz.mag.f")
        self.assertEqual(
            driver._resolve(self.device(), point),
            ("VVPSIMMEAS/MMXU1.Hz.mag.f", "MX"),
        )

    def test_ied_name_not_doubled(self) -> None:
        driver = self.make_driver(ied_name="VVPSIM")
        point = PointDef(name="p", register="MX", path="VVPSIMMEAS/MMXU1.Hz.mag.f")
        self.assertEqual(
            driver._resolve(self.device(), point)[0], "VVPSIMMEAS/MMXU1.Hz.mag.f"
        )

    def test_all_fc_choices_have_labels(self) -> None:
        areas = Iec61850Driver.point_areas()
        self.assertGreaterEqual(len(areas), 8)
        for value, label in areas:
            self.assertIn(value, FC_NAMES)
            self.assertTrue(label)


# --------------------------------------------------------------- 读写逻辑


class ReadWriteTest(_Base):
    def _point(self, name="hz", path="MEAS/MMXU1.Hz.mag.f", **kwargs) -> PointDef:
        return PointDef(name=name, register=kwargs.pop("register", "MX"), path=path, **kwargs)

    def test_read_maps_values_to_point_names(self) -> None:
        driver = self.make_driver(ied_name="VVPSIM")
        conn = _FakeConnection({"VVPSIMMEAS/MMXU1.Hz.mag.f": 50.05})
        self.attach(driver, conn)
        results = driver.read_points(self.device(), [self._point()])
        self.assertTrue(results["hz"].ok)
        self.assertAlmostEqual(results["hz"].value, 50.05, places=3)

    def test_batching_splits_by_batch_size(self) -> None:
        driver = self.make_driver(ied_name="VVPSIM", batch_size=2)
        conn = _FakeConnection()
        self.attach(driver, conn)
        points = [self._point(name=f"p{i}", path=f"MEAS/MMXU1.D{i}") for i in range(5)]
        driver.read_points(self.device(), points)
        self.assertEqual([len(batch) for batch in conn.batches], [2, 2, 1])

    def test_one_failure_does_not_stop_the_rest(self) -> None:
        driver = self.make_driver(ied_name="VVPSIM", batch_size=10)
        conn = _FakeConnection(
            {"VVPSIMMEAS/MMXU1.Ok.mag.f": 1.5},
            fail_for={"VVPSIMMEAS/MMXU1.Bad.mag.f"},
        )
        self.attach(driver, conn)
        points = [
            self._point(name="ok", path="MEAS/MMXU1.Ok.mag.f"),
            self._point(name="bad", path="MEAS/MMXU1.Bad.mag.f"),
        ]
        results = driver.read_points(self.device(), points)
        self.assertTrue(results["ok"].ok)
        self.assertFalse(results["bad"].ok)
        self.assertIn("ObjectAccessDenied", results["bad"].error)

    def test_undefined_object_gives_helpful_error(self) -> None:
        """FC 写错时最常见的报错是"对象不存在"，提示里要带 FC。"""
        driver = self.make_driver()
        conn = _FakeConnection(
            marker_for={"MEAS/MMXU1.Hz.mag.f": {"error": "ObjectUndefined", "code": 1}}
        )
        self.attach(driver, conn)
        results = driver.read_points(self.device(), [self._point()])
        self.assertFalse(results["hz"].ok)
        self.assertIn("ObjectUndefined", results["hz"].error)
        self.assertIn("FC", results["hz"].error)

    def test_bad_path_is_reported_per_point(self) -> None:
        driver = self.make_driver()
        self.attach(driver, _FakeConnection())
        bad = PointDef(name="bad", register="MX", path="没写逻辑设备")
        results = driver.read_points(self.device(), [bad])
        self.assertFalse(results["bad"].ok)
        self.assertIn("协议路径", results["bad"].error)

    def test_bytes_position_is_unpacked(self) -> None:
        """开关位置是位串，装置回 0x40/0x80，界面要看到 1/2 而不是 64/128。"""
        driver = self.make_driver()
        conn = _FakeConnection(
            marker_for={
                "MEAS/MMXU1.Pos": b"\x40",
                "MEAS/MMXU1.Oct": b"\x05",
            }
        )
        self.attach(driver, conn)
        results = driver.read_points(
            self.device(),
            [
                PointDef(name="pos", register="ST", path="MEAS/MMXU1.Pos"),
                PointDef(name="oct", register="ST", path="MEAS/MMXU1.Oct"),
            ],
        )
        self.assertEqual(results["pos"].value, 1)
        self.assertEqual(results["oct"].value, 5)

    def test_scale_and_offset_optional(self) -> None:
        driver = self.make_driver()
        conn = _FakeConnection({"MEAS/MMXU1.Hz": 50.0})
        self.attach(driver, conn)
        plain = driver.read_points(self.device(), [PointDef(name="a", register="MX", path="MEAS/MMXU1.Hz")])
        self.assertAlmostEqual(plain["a"].value, 50.0)
        scaled = driver.read_points(
            self.device(),
            [PointDef(name="b", register="MX", path="MEAS/MMXU1.Hz", scale=0.001, offset=1.0)],
        )
        self.assertAlmostEqual(scaled["b"].value, 1.05, places=6)

    def test_write_uses_fc_and_coerces_type(self) -> None:
        driver = self.make_driver(ied_name="VVPSIM")
        conn = _FakeConnection()
        self.attach(driver, conn)
        point = PointDef(
            name="sp", register="SP", path="MEAS/ASG1.SetPnt", data_type="float32"
        )
        driver.write_point(self.device(), point, 12)
        self.assertEqual(conn.written, [("VVPSIMMEAS/ASG1.SetPnt", "SP", 12.0)])

    def test_write_int_point_rounds(self) -> None:
        driver = self.make_driver()
        conn = _FakeConnection()
        self.attach(driver, conn)
        point = PointDef(name="n", register="CF", path="MEAS/MMXU1.Hz.db", data_type="uint16")
        driver.write_point(self.device(), point, 6.6)
        self.assertEqual(conn.written[0][2], 7)

    def test_read_without_connection_raises(self) -> None:
        driver = self.make_driver()
        self.attach(driver, None)
        with self.assertRaises(DriverError):
            driver.read_points(self.device(), [self._point()])

    def test_write_failure_raises_driver_error(self) -> None:
        driver = self.make_driver()

        class _Bad(_FakeConnection):
            async def write(self, *args, **kwargs):
                raise RuntimeError("装置拒绝")

        self.attach(driver, _Bad())
        point = PointDef(name="sp", register="SP", path="MEAS/ASG1.SetPnt")
        with self.assertRaises(DriverError) as ctx:
            driver.write_point(self.device(), point, 1.0)
        self.assertIn("装置拒绝", str(ctx.exception))


# --------------------------------------------------------------- 报告


class ReportTest(_Base):
    def setUp(self) -> None:
        self.driver = self.make_driver(ied_name="VVPSIM")
        self.conn = _FakeConnection()
        self.attach(self.driver, self.conn)
        self.pushed: list[dict] = []
        self.driver.set_callbacks(on_values=self.pushed.append)

    def test_report_values_are_mapped_to_pids(self) -> None:
        self.driver._ref_to_pid = {
            reference_key("VVPSIMMEAS/MMXU1.Hz.mag.f"): "测量.hz",
            reference_key("VVPSIMMEAS/MMXU1.A.mag.f"): "测量.a",
        }
        self.driver._on_report(
            _fake_report(
                [
                    ("VVPSIMMEAS/MMXU1.Hz.mag.f", 50.02),
                    ("VVPSIMMEAS/MMXU1.A.mag.f", 231.5),
                ]
            )
        )
        self.assertEqual(len(self.pushed), 1)
        self.assertEqual(self.pushed[0], {"测量.hz": 50.02, "测量.a": 231.5})

    def test_report_with_unknown_object_is_ignored_once(self) -> None:
        logs: list[tuple] = []
        self.driver.set_callbacks(on_values=self.pushed.append, on_log=lambda level, msg: logs.append((level, msg)))
        self.driver._on_report(_fake_report([("VVPSIMMEAS/MMXU1.Unknown.mag.f", 1.0)]))
        self.driver._on_report(_fake_report([("VVPSIMMEAS/MMXU1.Unknown.mag.f", 1.0)]))
        self.assertEqual(self.pushed, [])
        self.assertEqual(len(logs), 1)
        self.assertIn("Unknown.mag.f", logs[0][1])

    def test_report_without_entries_is_ignored(self) -> None:
        self.driver._on_report(_fake_report([]))
        self.assertEqual(self.pushed, [])

    def test_ref_map_built_from_project(self) -> None:
        """报告映射要按整份点表建，而不是"读过哪些点位"。"""
        from icomm.core.models import Project

        project = Project(name="p")
        project.channels = [self.driver.channel]
        device = DeviceDef(name="测量", channel=self.driver.channel.name, unit_id=1)
        device.points = [
            PointDef(name="hz", register="MX", path="MEAS/MMXU1.Hz.mag.f"),
        ]
        project.devices = [device]
        self.driver.set_project(project)
        key = reference_key("VVPSIMMEAS/MMXU1.Hz.mag.f")
        self.assertEqual(self.driver._ref_to_pid.get(key), "测量.hz")


# --------------------------------------------------------------- 端到端


@REQUIRES_LIB
class EndToEndTest(unittest.TestCase):
    """拿内置 ICD 起真服务端，整条读链路走一遍。"""

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from tools.iec61850_sim import Iec61850Simulator

        cls.sim = Iec61850Simulator(port=0)
        cls.sim.start()

    @classmethod
    def tearDownClass(cls):
        cls.sim.stop()

    def setUp(self) -> None:
        host, _, port = self.sim.address.rpartition(":")
        options = {
            "host": host,
            "port": int(port),
            "ied_name": "VVPSIM",
            "reports": False,
            "timeout_ms": 4000,
        }
        self.channel = ChannelDef(name="sim", driver="iec61850", link="tcp", options=options)
        self.driver = Iec61850Driver(self.channel)
        self.device = DeviceDef(name="测量", channel="sim", unit_id=1)
        self.addCleanup(self.driver.close)

    def test_reads_live_values_through_driver(self) -> None:
        self.driver.open()
        self.assertTrue(self.driver.is_open)
        time.sleep(0.6)  # 等仿真装置的行为任务刷过一轮
        points = [
            PointDef(name="hz", register="MX", path="MEAS/MMXU1.Hz.mag.f"),
            PointDef(name="amp", register="MX", path="MEAS/MMXU1.A.mag.f"),
            PointDef(name="pos", register="ST", path="CTRL/XCBR1.Pos.stVal"),
        ]
        results = self.driver.read_points(self.device, points)
        self.assertTrue(results["hz"].ok, results["hz"].error)
        self.assertTrue(results["amp"].ok, results["amp"].error)
        self.assertGreater(results["hz"].value, 49.0)
        self.assertLess(results["hz"].value, 51.0)
        self.assertGreater(results["amp"].value, 100.0)

    def test_browse_helpers(self) -> None:
        self.driver.open()
        lds = self.driver.browse_server()
        self.assertIn("VVPSIMMEAS", lds)
        lns = self.driver.browse_logical_device("VVPSIMMEAS")
        self.assertIn("MMXU1", lns)
        attrs = self.driver.browse_attributes("VVPSIMMEAS", "MMXU1", "Hz")
        self.assertIn("mag", attrs)

    def test_write_to_readonly_attribute_reports_device_error(self) -> None:
        """内置 ICD 的属性是只读的：写值要如实报装置的错误，不能假装成功。"""
        self.driver.open()
        point = PointDef(name="hz", register="MX", path="MEAS/MMXU1.Hz.mag.f")
        with self.assertRaises(DriverError) as ctx:
            self.driver.write_point(self.device, point, 50.0)
        self.assertTrue("失败" in str(ctx.exception) or "拒绝" in str(ctx.exception))

    def test_bad_reference_fails_that_point_only(self) -> None:
        self.driver.open()
        points = [
            PointDef(name="good", register="MX", path="MEAS/MMXU1.Hz.mag.f"),
            PointDef(name="bad", register="MX", path="MEAS/MMXU1.NotThere.mag.f"),
        ]
        results = self.driver.read_points(self.device, points)
        self.assertTrue(results["good"].ok)
        self.assertFalse(results["bad"].ok)


if __name__ == "__main__":
    unittest.main()
