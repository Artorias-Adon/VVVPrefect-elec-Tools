"""数据模型与工程文件测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from icomm.core import project as project_io
from icomm.core.models import (
    AlarmDef,
    ChannelDef,
    DeviceDef,
    PointDef,
    Project,
    default_channel_options,
    parse_point_id,
    point_id,
    word_count,
)


class PointIdTest(unittest.TestCase):
    def test_roundtrip(self):
        pid = point_id("1号柜", "A相电压")
        self.assertEqual(pid, "1号柜.A相电压")
        self.assertEqual(parse_point_id(pid), ("1号柜", "A相电压"))

    def test_tag_with_dot(self):
        device, tag = parse_point_id("dev.tag.extra")
        self.assertEqual(device, "dev")
        self.assertEqual(tag, "tag.extra")


class WordCountTest(unittest.TestCase):
    def test_widths(self):
        self.assertEqual(word_count("uint16"), 1)
        self.assertEqual(word_count("int16"), 1)
        self.assertEqual(word_count("uint32"), 2)
        self.assertEqual(word_count("float32"), 2)
        self.assertEqual(word_count("float64"), 4)


class DefaultOptionsTest(unittest.TestCase):
    def test_tcp_defaults(self):
        options = default_channel_options("tcp")
        self.assertEqual(options["port"], 502)
        self.assertIn("host", options)

    def test_rtu_defaults(self):
        options = default_channel_options("rtu")
        self.assertEqual(options["baudrate"], 9600)
        self.assertEqual(options["parity"], "E", "现场默认 9600 8E1（偶校验）")
        self.assertEqual(options["bytesize"], 8)
        self.assertEqual(options["stopbits"], 1)
        self.assertNotIn("host", options)


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.project = Project(name="测试工程")
        self.channel = ChannelDef(
            name="ch1", driver="modbus", link="tcp", options=default_channel_options("tcp")
        )
        self.project.channels.append(self.channel)
        self.device = DeviceDef(name="dev1", channel="ch1", unit_id=3)
        self.device.points = [
            PointDef(name="p1", address=0, display="card"),
            PointDef(name="p2", address=1, display="none"),
            PointDef(name="p3", address=2, display="lamp"),
        ]
        self.project.devices.append(self.device)

    def test_find_point(self):
        found = self.project.find_point("dev1.p1")
        self.assertIsNotNone(found)
        device, point = found
        self.assertEqual(device.name, "dev1")
        self.assertEqual(point.name, "p1")
        self.assertIsNone(self.project.find_point("dev1.nope"))
        self.assertIsNone(self.project.find_point("nope.p1"))

    def test_channel_and_device_lookup(self):
        self.assertIs(self.project.channel("ch1"), self.channel)
        self.assertIs(self.project.device("dev1"), self.device)
        self.assertIsNone(self.project.channel("ch2"))

    def test_display_points_filters_none(self):
        names = [p.name for _d, p in self.project.display_points()]
        self.assertEqual(names, ["p1", "p3"])

    def test_display_points_skips_disabled_device(self):
        self.device.enabled = False
        self.assertEqual(self.project.display_points(), [])

    def test_json_roundtrip(self):
        payload = self.project.to_dict()
        text = json.dumps(payload, ensure_ascii=False)
        restored = Project.from_dict(json.loads(text))
        self.assertEqual(restored.name, "测试工程")
        self.assertEqual(len(restored.channels), 1)
        self.assertEqual(restored.channels[0].options["port"], 502)
        self.assertEqual(restored.devices[0].unit_id, 3)
        self.assertEqual([p.name for p in restored.devices[0].points], ["p1", "p2", "p3"])

    def test_alarm_roundtrip(self):
        self.device.points[0].alarm = AlarmDef(
            enabled=True, level="critical", hi_hi=80.0, hi=70.0, lo=None, lo_lo=None
        )
        restored = Project.from_dict(json.loads(json.dumps(self.project.to_dict())))
        alarm = restored.devices[0].points[0].alarm
        self.assertTrue(alarm.enabled)
        self.assertEqual(alarm.level, "critical")
        self.assertAlmostEqual(alarm.hi_hi, 80.0)
        self.assertIsNone(alarm.lo)

    def test_unknown_keys_ignored(self):
        """旧版本工程里多出的字段不应导致载入失败。"""
        payload = self.project.to_dict()
        payload["future_field"] = 1
        payload["channels"][0]["another_new_field"] = "x"
        restored = Project.from_dict(payload)
        self.assertEqual(restored.channels[0].name, "ch1")

    def test_missing_options_get_defaults(self):
        data = self.project.to_dict()
        data["channels"][0]["options"] = {}
        restored = Project.from_dict(data)
        self.assertEqual(restored.channels[0].options["port"], 502)


class ProjectFileTest(unittest.TestCase):
    def test_save_load_and_create_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sub", "project.json")
            self.assertFalse(os.path.exists(path))

            project, created = project_io.load_or_create_sample(path)
            self.assertTrue(created)
            self.assertTrue(os.path.exists(path))
            self.assertTrue(project.channels)

            again, created2 = project_io.load_or_create_sample(path)
            self.assertFalse(created2)
            self.assertEqual(again.name, project.name)
            self.assertEqual(
                [p.name for _d, p in again.iter_points()],
                [p.name for _d, p in project.iter_points()],
            )

    def test_sample_project_matches_simulator(self):
        """示例工程的点位必须与仿真器数据区一致，否则一键演示会读不到数。"""
        project = project_io.sample_project()
        meter = project.device("1号进线柜")
        self.assertIsNotNone(meter)
        self.assertEqual(meter.unit_id, 1)
        addresses = {(p.register, p.address, p.data_type) for p in meter.points}
        for expected in (
            ("holding", 0, "float32"),
            ("holding", 2, "uint16"),
            ("holding", 3, "uint16"),
            ("holding", 4, "uint16"),
            ("holding", 5, "uint32"),
            ("holding", 7, "uint16"),
            ("holding", 8, "uint16"),
            ("coil", 0, "bool"),
            ("coil", 1, "bool"),
            ("discrete", 0, "bool"),
            ("input", 0, "uint16"),
        ):
            self.assertIn(expected, addresses, f"示例工程缺少 {expected}")

        pump = project.device("2号水泵")
        self.assertIsNotNone(pump)
        self.assertEqual(pump.unit_id, 2)


if __name__ == "__main__":
    unittest.main()
