"""实时点位表与报警判定测试。"""

from __future__ import annotations

import unittest

from icomm.core.alarms import AlarmEngine
from icomm.core.models import AlarmDef, DeviceDef, PointDef
from icomm.core.tags import QUALITY_BAD, QUALITY_GOOD, DeviceStatus, TagTable


class TagTableTest(unittest.TestCase):
    def test_ensure_and_update(self):
        table = TagTable()
        self.assertEqual(table.get("a.b").quality, QUALITY_BAD)
        table.ensure("a.b")
        tag = table.update("a.b", 12.5, QUALITY_GOOD)
        self.assertTrue(tag.good)
        self.assertEqual(tag.value, 12.5)
        self.assertGreater(tag.timestamp, 0)
        self.assertEqual(len(table.snapshot()), 1)

    def test_mark_device_degrades_quality(self):
        table = TagTable()
        table.update("dev1.p1", 1.0, QUALITY_GOOD)
        table.update("dev1.p2", 2.0, QUALITY_GOOD)
        table.update("dev2.p1", 3.0, QUALITY_GOOD)
        table.mark_device("dev1", QUALITY_BAD, "断线")
        self.assertEqual(table.get("dev1.p1").quality, QUALITY_BAD)
        self.assertEqual(table.get("dev1.p1").error, "断线")
        self.assertEqual(table.get("dev2.p1").quality, QUALITY_GOOD)

    def test_drop_device(self):
        table = TagTable()
        table.update("dev1.p1", 1.0, QUALITY_GOOD)
        table.update("dev2.p1", 1.0, QUALITY_GOOD)
        table.drop_device("dev1")
        self.assertEqual(len(table.snapshot()), 1)

    def test_clear(self):
        table = TagTable()
        table.update("dev1.p1", 1.0, QUALITY_GOOD)
        table.clear()
        self.assertEqual(table.snapshot(), {})


class DeviceStatusTest(unittest.TestCase):
    def test_mark_ok(self):
        status = DeviceStatus(name="d")
        status.mark_ok(12.5)
        self.assertTrue(status.connected)
        self.assertEqual(status.state, "正常")
        self.assertEqual(status.ok_count, 1)
        self.assertAlmostEqual(status.last_duration_ms, 12.5)

    def test_mark_error(self):
        status = DeviceStatus(name="d")
        status.mark_error("超时")
        self.assertFalse(status.connected)
        self.assertEqual(status.state, "通信故障")
        self.assertEqual(status.error, "超时")
        self.assertEqual(status.error_count, 1)


class AlarmEngineTest(unittest.TestCase):
    def setUp(self):
        self.engine = AlarmEngine()
        self.device = DeviceDef(name="dev")
        self.point = PointDef(
            name="温度",
            unit="℃",
            alarm=AlarmDef(enabled=True, level="warning", hi_hi=80.0, hi=60.0, lo=5.0),
        )

    def test_disabled_alarm_ignored(self):
        self.point.alarm.enabled = False
        self.assertIsNone(self.engine.evaluate(self.device, self.point, 999.0))

    def test_critical_on_hi_hi(self):
        event = self.engine.evaluate(self.device, self.point, 85.0)
        self.assertIsNotNone(event)
        self.assertEqual(event.level, "critical")
        self.assertTrue(event.active)
        self.assertEqual(event.pid, "dev.温度")

    def test_warning_on_hi(self):
        event = self.engine.evaluate(self.device, self.point, 65.0)
        self.assertEqual(event.level, "warning")

    def test_low_alarm(self):
        event = self.engine.evaluate(self.device, self.point, 3.0)
        self.assertIsNotNone(event)
        self.assertTrue(event.active)

    def test_no_event_while_normal(self):
        self.assertIsNone(self.engine.evaluate(self.device, self.point, 30.0))

    def test_no_duplicate_event_for_same_level(self):
        first = self.engine.evaluate(self.device, self.point, 65.0)
        second = self.engine.evaluate(self.device, self.point, 66.0)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_level_escalation_emits_new_event(self):
        self.engine.evaluate(self.device, self.point, 65.0)
        event = self.engine.evaluate(self.device, self.point, 85.0)
        self.assertIsNotNone(event)
        self.assertEqual(event.level, "critical")

    def test_recovery_event(self):
        self.engine.evaluate(self.device, self.point, 85.0)
        event = self.engine.evaluate(self.device, self.point, 30.0)
        self.assertIsNotNone(event)
        self.assertFalse(event.active)
        self.assertIn("恢复", event.message)

    def test_active_alarm_tracking(self):
        self.engine.evaluate(self.device, self.point, 85.0)
        self.assertIn("dev.温度", self.engine.active_alarms())
        self.engine.evaluate(self.device, self.point, 30.0)
        self.assertNotIn("dev.温度", self.engine.active_alarms())

    def test_bool_value_ignored(self):
        self.assertIsNone(self.engine.evaluate(self.device, self.point, True))

    def test_none_value_ignored(self):
        self.assertIsNone(self.engine.evaluate(self.device, self.point, None))

    def test_boundary_is_inclusive(self):
        event = self.engine.evaluate(self.device, self.point, 60.0)
        self.assertIsNotNone(event)


if __name__ == "__main__":
    unittest.main()
