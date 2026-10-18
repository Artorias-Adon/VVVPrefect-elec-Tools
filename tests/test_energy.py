"""电能与电费：功率积分、电量示数、分时电价、累计与落盘。

要盯住的是"现场会算错钱"的那几处：

* 采样间隔拉长（采集停过）**不能凭空造电量**，但**示数差是真的**要照记；
* 跨天/跨月只清"当期"，累计不能动；
* 反向电量（倒送）单独记、**不计电费**；
* 分时电价要落在对的时段（含跨零点的低谷）；
* 账本重启读回来之后接着算，不能从零开始。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from icomm.core import energy  # noqa: E402
from icomm.core import project as project_io  # noqa: E402
from icomm.core.energy import EnergyBook, Meter, MeterConfig, Tariff, TariffSlot  # noqa: E402
from icomm.core.models import DeviceDef, PointDef, Project  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.core.tags import TagValue  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.energy_service import EnergyService  # noqa: E402
from icomm.ui.tools.energy_meter import (  # noqa: E402
    EnergyWindow,
    MeterDialog,
    TariffDialog,
    tou_tariff_slots,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def run_power(meter: Meter, power: float, start: datetime, seconds: int) -> None:
    """从 start 开始，每秒采一次功率，跑 seconds 秒。"""
    meter.accrue(power, start)
    for second in range(1, seconds + 1):
        meter.accrue(power, start + timedelta(seconds=second))


# --------------------------------------------------------------- 电价


class TariffTest(unittest.TestCase):
    def test_single_tariff_has_one_price(self) -> None:
        tariff = energy.single_tariff(0.72)
        self.assertFalse(tariff.segmented)
        self.assertEqual(tariff.price_at(datetime(2026, 10, 18, 3, 0)), ("单一电价", 0.72))
        self.assertIn("单一电价", tariff.describe())

    def test_tou_slots_pick_the_right_period(self) -> None:
        tariff = energy.tou_tariff()
        cases = {
            (10, 30): "尖峰",
            (19, 30): "尖峰",
            (9, 0): "高峰",
            (12, 0): "高峰",
            (7, 30): "平段",
            (22, 30): "平段",
            (2, 0): "低谷",
            (6, 59): "低谷",
            (7, 0): "平段",  # 时段左闭右开
        }
        for (hour, minute), expected in cases.items():
            name, _price = tariff.price_at(datetime(2026, 10, 18, hour, minute))
            self.assertEqual(name, expected, f"{hour:02d}:{minute:02d} 应该是 {expected}")

    def test_slot_can_wrap_midnight(self) -> None:
        slot = TariffSlot("低谷", "23:00", "07:00", 0.35)
        self.assertTrue(slot.covers(23 * 60 + 30))
        self.assertTrue(slot.covers(6 * 60))
        self.assertFalse(slot.covers(12 * 60))
        self.assertTrue(TariffSlot("全天", "00:00", "00:00", 1.0).covers(15 * 60))

    def test_bad_time_text_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TariffSlot("怪", "8点", "18:00", 1.0).covers(0)

    def test_tariff_dict_roundtrip_tolerates_bad_slots(self) -> None:
        tariff = energy.tou_tariff()
        clone = Tariff.from_dict(tariff.to_dict())
        self.assertEqual(len(clone.slots), len(tariff.slots))
        broken = Tariff.from_dict({"price": 0.5, "slots": [{"start": "xx"}, {"name": "好",
                                                                           "start": "00:00",
                                                                           "end": "01:00",
                                                                           "price": 1}]})
        self.assertEqual(len(broken.slots), 1)
        self.assertEqual(broken.slots[0].name, "好")

    def test_negative_prices_are_clamped(self) -> None:
        self.assertEqual(energy.single_tariff(-5).price, 0.0)


# --------------------------------------------------------------- 功率积分


class PowerModeTest(unittest.TestCase):
    def make(self, tariff: Tariff | None = None) -> Meter:
        book = EnergyBook(tariff=tariff or energy.single_tariff(0.5))
        return book.add(MeterConfig(name="总表", mode=energy.MODE_POWER, power_point="d.p"))

    def test_integrates_power_into_energy(self) -> None:
        meter = self.make()
        run_power(meter, 100.0, datetime(2026, 10, 18, 8, 0, 0), 3600)
        self.assertAlmostEqual(meter.forward_total, 100.0, places=3)
        self.assertAlmostEqual(meter.cost_total, 50.0, places=3)
        self.assertAlmostEqual(meter.peak_kw, 100.0, places=3)
        self.assertEqual(meter.samples, 3600)

    def test_first_sample_only_starts_the_clock(self) -> None:
        meter = self.make()
        self.assertEqual(meter.accrue(50.0, datetime(2026, 10, 18, 8, 0, 0)), 0.0)
        self.assertEqual(meter.forward_total, 0.0)
        self.assertIsNotNone(meter.last_ts)

    def test_same_timestamp_adds_nothing(self) -> None:
        meter = self.make()
        when = datetime(2026, 10, 18, 8, 0, 0)
        meter.accrue(50.0, when)
        meter.accrue(50.0, when)
        self.assertEqual(meter.forward_total, 0.0)

    def test_long_gap_is_not_invented(self) -> None:
        """采集停过一小时：那一段没数据，不能凭空算成 100 kWh。"""
        meter = self.make()
        meter.accrue(100.0, datetime(2026, 10, 18, 12, 0, 0))
        meter.accrue(100.0, datetime(2026, 10, 18, 12, 0, 1))
        before = meter.forward_total
        meter.accrue(100.0, datetime(2026, 10, 18, 13, 0, 0))
        self.assertAlmostEqual(meter.forward_total, before, places=6)
        self.assertEqual(meter.skipped_gaps, 1)
        self.assertAlmostEqual(before, 100.0 / 3600.0, places=6)

    def test_reverse_power_counts_but_costs_nothing(self) -> None:
        meter = self.make()
        run_power(meter, -200.0, datetime(2026, 10, 18, 12, 0, 0), 3600)
        self.assertAlmostEqual(meter.reverse_total, 200.0, places=3)
        self.assertEqual(meter.forward_total, 0.0)
        self.assertEqual(meter.cost_total, 0.0)

    def test_ratio_multiplies_energy(self) -> None:
        book = EnergyBook(tariff=energy.single_tariff(0.5))
        meter = book.add(MeterConfig(name="带倍率", power_point="d.p", ratio=2.0))
        run_power(meter, 10.0, datetime(2026, 10, 18, 8, 0, 0), 3600)
        self.assertAlmostEqual(meter.forward_total, 20.0, places=3)

    def test_day_and_month_roll_over_but_total_stays(self) -> None:
        meter = self.make()
        meter.accrue(3600.0, datetime(2026, 10, 31, 23, 59, 0))
        for second in range(1, 60):  # 23:59:00 → 23:59:59，3600kW 每秒 1 度
            meter.accrue(3600.0, datetime(2026, 10, 31, 23, 59, second))
        total_before = meter.forward_total
        self.assertAlmostEqual(total_before, 59.0, places=3)
        self.assertAlmostEqual(meter.forward_month, 59.0, places=3)
        meter.accrue(3600.0, datetime(2026, 11, 1, 0, 0, 0))  # 跨天又跨月，再 +1 度
        self.assertAlmostEqual(meter.forward_total, total_before + 1.0, places=3)
        self.assertAlmostEqual(meter.forward_day, 1.0, places=3, msg="跨天后今日清零")
        self.assertAlmostEqual(meter.forward_month, 1.0, places=3, msg="跨月后本月清零")

    def test_tou_energy_is_split_by_period(self) -> None:
        meter = self.make(energy.tou_tariff())
        start = datetime(2026, 10, 18, 9, 0, 0)  # 高峰
        meter.accrue(100.0, start)
        for minute in range(1, 61):  # 9:00-10:00 高峰
            meter.accrue(100.0, start + timedelta(minutes=minute))
        for minute in range(1, 61):  # 10:00-11:00 尖峰
            meter.accrue(100.0, start + timedelta(hours=1, minutes=minute))
        self.assertAlmostEqual(meter.period_kwh["高峰"], 100.0, places=3)
        self.assertAlmostEqual(meter.period_kwh["尖峰"], 100.0, places=3)
        self.assertAlmostEqual(meter.cost_total, 100 * 0.95 + 100 * 1.20, places=3)


# --------------------------------------------------------------- 电量示数


class ReadingModeTest(unittest.TestCase):
    def make(self) -> Meter:
        book = EnergyBook(tariff=energy.single_tariff(1.0))
        return book.add(MeterConfig(name="电表", mode=energy.MODE_READING, reading_point="d.e"))

    def test_first_reading_is_a_baseline(self) -> None:
        meter = self.make()
        self.assertEqual(meter.apply_reading(1000.0, datetime(2026, 10, 18, 8, 0)), 0.0)
        self.assertEqual(meter.reading_base, 1000.0)

    def test_delta_is_energy_and_implies_power(self) -> None:
        meter = self.make()
        meter.apply_reading(1000.0, datetime(2026, 10, 18, 8, 0))
        meter.apply_reading(1025.5, datetime(2026, 10, 18, 8, 30))
        self.assertAlmostEqual(meter.forward_total, 25.5, places=6)
        self.assertAlmostEqual(meter.power_kw or 0.0, 51.0, places=3)
        self.assertAlmostEqual(meter.cost_total, 25.5, places=6)

    def test_long_interval_still_counts(self) -> None:
        """示数差是表量出来的：隔了两小时也得记（功率模式才会因间隔太长而跳过）。"""
        meter = self.make()
        meter.apply_reading(1000.0, datetime(2026, 10, 18, 8, 0))
        meter.apply_reading(1006.0, datetime(2026, 10, 18, 10, 0))
        self.assertAlmostEqual(meter.forward_total, 6.0, places=6)
        self.assertEqual(meter.skipped_gaps, 0)
        self.assertAlmostEqual(meter.power_kw or 0.0, 3.0, places=3)

    def test_short_interval_gives_no_fake_power(self) -> None:
        """1 秒的示数差推出来的是假功率：装置的累计量是攒着跳的。"""
        meter = self.make()
        meter.apply_reading(1000.0, datetime(2026, 10, 18, 8, 0, 0))
        meter.apply_reading(1048.0, datetime(2026, 10, 18, 8, 0, 1))
        self.assertAlmostEqual(meter.forward_total, 48.0, places=6, msg="电量照记")
        self.assertIsNone(meter.power_kw, "间隔太短不给功率")
        self.assertEqual(meter.peak_kw, 0.0)

    def test_meter_reset_is_not_reverse_energy(self) -> None:
        meter = self.make()
        meter.apply_reading(1000.0, datetime(2026, 10, 18, 8, 0))
        meter.apply_reading(1005.0, datetime(2026, 10, 18, 8, 10))
        meter.apply_reading(2.0, datetime(2026, 10, 18, 8, 20))  # 换表/清零
        self.assertEqual(meter.reading_resets, 1)
        self.assertEqual(meter.reverse_total, 0.0)
        self.assertAlmostEqual(meter.forward_total, 5.0, places=6)
        meter.apply_reading(4.0, datetime(2026, 10, 18, 8, 30))
        self.assertAlmostEqual(meter.forward_total, 7.0, places=6)

    def test_small_backwards_jitter_does_not_double_count(self) -> None:
        meter = self.make()
        meter.apply_reading(100.0, datetime(2026, 10, 18, 8, 0))
        meter.apply_reading(99.9, datetime(2026, 10, 18, 8, 1))
        self.assertEqual(meter.forward_total, 0.0)
        self.assertEqual(meter.reading_resets, 0)


# --------------------------------------------------------------- 账本


class BookTest(unittest.TestCase):
    def test_tick_reads_each_meter_by_its_mode(self) -> None:
        book = EnergyBook(tariff=energy.single_tariff(0.5))
        book.add(MeterConfig(name="功率表", power_point="d.p"))
        book.add(MeterConfig(name="示数表", mode=energy.MODE_READING, reading_point="d.e"))
        book.add(MeterConfig(name="没点位的"))
        values = {"d.p": 60.0, "d.e": 500.0}
        self.assertTrue(book.tick(datetime(2026, 10, 18, 8, 0), lambda pid: values.get(pid)))
        values["d.p"] = 60.0
        values["d.e"] = 501.0
        self.assertTrue(book.tick(datetime(2026, 10, 18, 8, 1), lambda pid: values.get(pid)))
        self.assertAlmostEqual(book.meters[0].forward_total, 1.0, places=3)
        self.assertAlmostEqual(book.meters[1].forward_total, 1.0, places=3)
        self.assertEqual(book.meters[2].forward_total, 0.0)

    def test_missing_values_are_skipped(self) -> None:
        book = EnergyBook()
        meter = book.add(MeterConfig(name="表", power_point="d.p"))
        self.assertFalse(book.tick(datetime(2026, 10, 18, 8, 0), lambda pid: None))
        self.assertEqual(meter.forward_total, 0.0)

    def test_reset_scopes(self) -> None:
        book = EnergyBook(tariff=energy.single_tariff(1.0))
        meter = book.add(MeterConfig(name="表", power_point="d.p"))
        run_power(meter, 3600.0, datetime(2026, 10, 18, 8, 0, 0), 3600)
        self.assertAlmostEqual(meter.forward_total, 3600.0, places=3)
        book.reset("day", ["表"])
        self.assertEqual(meter.forward_day, 0.0)
        self.assertAlmostEqual(meter.forward_total, 3600.0, places=3)
        book.reset("month", ["表"])
        self.assertEqual(meter.forward_month, 0.0)
        book.reset("total", ["表"])
        self.assertEqual(meter.forward_total, 0.0)
        self.assertEqual(meter.cost_total, 0.0)
        self.assertEqual(meter.period_kwh, {})

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "energy.json")
            book = EnergyBook(tariff=energy.tou_tariff())
            meter = book.add(MeterConfig(name="总表", power_point="d.p", ratio=1.5, note="进线"))
            run_power(meter, 100.0, datetime(2026, 10, 18, 9, 0, 0), 60)
            book.save(path)
            again = EnergyBook.load(path)
            self.assertTrue(again.tariff.segmented)
            self.assertEqual(len(again.meters), 1)
            clone = again.meters[0]
            self.assertEqual(clone.config.name, "总表")
            self.assertAlmostEqual(clone.config.ratio, 1.5)
            self.assertAlmostEqual(clone.forward_total, meter.forward_total, places=6)
            self.assertEqual(clone.last_ts, meter.last_ts)
            self.assertEqual(clone.period_kwh.keys(), meter.period_kwh.keys())

    def test_load_missing_or_broken_file_gives_empty_book(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(EnergyBook.load(os.path.join(tmp, "none.json")).meters, [])
            bad = os.path.join(tmp, "bad.json")
            with open(bad, "w", encoding="utf-8") as handle:
                handle.write("{ 这不是 JSON")
            self.assertEqual(EnergyBook.load(bad).meters, [])

    def test_export_csv_has_header_and_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            book = EnergyBook(tariff=energy.single_tariff(0.6))
            meter = book.add(MeterConfig(name="1号表", power_point="d.p"))
            run_power(meter, 100.0, datetime(2026, 10, 18, 8, 0, 0), 60)
            path = os.path.join(tmp, "out", "energy.csv")
            book.export_csv(path, datetime(2026, 10, 18, 9, 0))
            with open(path, encoding="utf-8-sig") as handle:
                text = handle.read()
            self.assertIn("电价：单一电价", text)
            self.assertIn("导出时间：2026-10-18 09:00:00", text)
            self.assertIn("正向总电量", text)
            self.assertIn("1号表", text)

    def test_totals_add_up_all_meters(self) -> None:
        book = EnergyBook(tariff=energy.single_tariff(1.0))
        for name in ("A", "B"):
            meter = book.add(MeterConfig(name=name, power_point="d.p"))
            run_power(meter, 3600.0, datetime(2026, 10, 18, 8, 0, 0), 3600)
        totals = book.totals(datetime(2026, 10, 18, 9, 0))
        self.assertAlmostEqual(totals["forward_total"], 7200.0, places=2)
        self.assertAlmostEqual(totals["cost_total"], 7200.0, places=2)

    def test_set_tariff_reaches_every_meter(self) -> None:
        book = EnergyBook()
        meter = book.add(MeterConfig(name="表", power_point="d.p"))
        book.set_tariff(energy.single_tariff(0.9))
        self.assertIs(meter.tariff, book.tariff)
        meter.accrue(3600.0, datetime(2026, 10, 18, 8, 0))
        meter.accrue(3600.0, datetime(2026, 10, 18, 8, 0, 1))
        self.assertAlmostEqual(meter.cost_total, 0.9, places=6)


# --------------------------------------------------------------- 工程里猜电表


class SuggestTest(unittest.TestCase):
    def test_bess_suggests_total_charge_reading(self) -> None:
        config = energy.suggest_meter(project_io.bess_project())
        self.assertIsNotNone(config)
        self.assertEqual(config.mode, energy.MODE_READING)
        self.assertIn("累计", config.name)

    def test_grid_suggests_power_point(self) -> None:
        config = energy.suggest_meter(project_io.grid_project())
        self.assertIsNotNone(config)
        self.assertEqual(config.mode, energy.MODE_POWER)
        self.assertIn("有功", config.name)

    def test_pv_suggests_energy_reading(self) -> None:
        config = energy.suggest_meter(project_io.pv_project())
        self.assertIsNotNone(config)
        self.assertEqual(config.mode, energy.MODE_READING)

    def test_project_without_energy_points_gives_none(self) -> None:
        project = Project(name="空")
        project.devices.append(
            DeviceDef(
                name="装置",
                channel="ch",
                points=[PointDef(name="温度", unit="℃", address=0)],
            )
        )
        self.assertIsNone(energy.suggest_meter(project))


# --------------------------------------------------------------- 服务（记账员）


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.values: dict[str, float] = {}
        self.running = True
        self.service = EnergyService(
            project=Project(name="测试"),
            data_dir=self.tmp.name,
            tag_of=lambda pid: TagValue(pid=pid, value=self.values.get(pid), quality="good"),
            engine_running=lambda: self.running,
        )
        self.service.add_meter(MeterConfig(name="总表", power_point="d.p"))

    def test_does_not_count_while_acquisition_stopped(self) -> None:
        meter = self.service.book.meters[0]
        self.values["d.p"] = 100.0
        self.service.tick(datetime(2026, 10, 18, 8, 0, 0))
        self.running = False
        self.assertFalse(self.service.tick(datetime(2026, 10, 18, 8, 0, 1)))
        self.assertEqual(meter.forward_total, 0.0)
        self.assertIsNone(meter.last_ts, "停机后时间戳要放空，恢复时不会补算停机那段")
        self.running = True
        self.values["d.p"] = 3600.0
        self.service.tick(datetime(2026, 10, 18, 8, 0, 2))
        self.assertTrue(self.service.tick(datetime(2026, 10, 18, 8, 0, 3)))
        self.assertAlmostEqual(meter.forward_total, 1.0, places=3)

    def test_autosave_writes_json_next_to_project_data(self) -> None:
        self.values["d.p"] = 3600.0
        self.service.tick(datetime(2026, 10, 18, 8, 0, 0))
        self.service.tick(datetime(2026, 10, 18, 8, 0, 1))
        self.service.save(force=True)
        path = os.path.join(self.tmp.name, "energy.json")
        self.assertTrue(os.path.isfile(path))
        again = EnergyService(
            project=Project(name="测试"),
            data_dir=self.tmp.name,
            tag_of=lambda pid: None,
            engine_running=lambda: False,
        )
        self.assertEqual(len(again.book.meters), 1)
        self.assertAlmostEqual(again.book.meters[0].forward_total, 1.0, places=3)

    def test_set_context_saves_previous_book(self) -> None:
        self.values["d.p"] = 3600.0
        self.service.tick(datetime(2026, 10, 18, 8, 0, 0))
        self.service.tick(datetime(2026, 10, 18, 8, 0, 1))
        with tempfile.TemporaryDirectory() as other:
            self.service.set_context(Project(name="另一个"), other)
            self.assertTrue(os.path.isfile(os.path.join(self.tmp.name, "energy.json")))
            self.assertEqual(self.service.book.meters, [], "换了工程就是新账本")

    def test_export_and_reset(self) -> None:
        self.values["d.p"] = 3600.0
        self.service.tick(datetime(2026, 10, 18, 8, 0, 0))
        self.service.tick(datetime(2026, 10, 18, 8, 0, 1))
        path = os.path.join(self.tmp.name, "export.csv")
        self.service.export_csv(path, datetime(2026, 10, 18, 9, 0))
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(self.service.reset("day", ["总表"]), 1)
        self.assertEqual(self.service.book.meters[0].forward_day, 0.0)


# --------------------------------------------------------------- 界面


def meter_project() -> Project:
    project = Project(name="计量")
    project.devices.append(
        DeviceDef(
            name="1号变压器",
            channel="ch",
            points=[
                PointDef(name="p", label="有功功率", unit="kW", address=0),
                PointDef(name="e", label="累计电量", unit="kWh", address=2, data_type="uint32"),
            ],
        )
    )
    return project


class EnergyWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.values: dict[str, float] = {"1号变压器.p": 200.0, "1号变压器.e": 1000.0}
        self.reported: list[tuple[str, str]] = []
        self.service = EnergyService(
            project=meter_project(),
            data_dir=self.tmp.name,
            tag_of=lambda pid: TagValue(pid=pid, value=self.values.get(pid), quality="good"),
            engine_running=lambda: True,
        )
        self.context = ui_tools.ToolContext(
            project=meter_project(),
            window=None,
            settings=AppSettings(),
            report_dir=self.tmp.name,
            data_dir=self.tmp.name,
            tag_of=lambda pid: TagValue(pid=pid, value=self.values.get(pid), quality="good"),
            engine_running=lambda: True,
            log=lambda level, message: self.reported.append((level, message)),
            services={"energy": self.service},
        )
        self.window = EnergyWindow(self.context)
        self.addCleanup(self.window.deleteLater)

    def test_tool_is_registered_under_calculation(self) -> None:
        tool = ui_tools.find_tool("电能与电费")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.group, "计算")
        self.assertEqual(tool.order, 12)
        self.assertTrue(tool.non_modal)

    def test_rows_show_power_and_money(self) -> None:
        meter = self.service.add_meter(
            MeterConfig(name="总表", power_point="1号变压器.p")
        )
        self.values["1号变压器.p"] = 3600.0
        self.service.tick(datetime(2026, 10, 18, 8, 0, 0))
        self.service.tick(datetime(2026, 10, 18, 8, 0, 1))
        self.window.refresh()
        self.assertEqual(self.window.table.rowCount(), 1)
        texts = [
            self.window.table.item(0, column).text()
            for column in range(self.window.table.columnCount())
        ]
        self.assertIn("总表", texts)
        self.assertIn("3,600.0", texts, "当前功率要显示出来")
        self.assertIn(meter.summary()["period"], texts)
        self.assertIn("合计", self.window.status.text())
        self.assertIn("¥", self.window.status.text())
        self.assertIn(energy.format_money(meter.cost_total), self.window.status.text())

    def test_empty_state_tells_user_what_to_do(self) -> None:
        self.window.refresh()
        self.assertIn("添加电表", self.window.status.text())

    def test_tariff_label_reflects_current_setting(self) -> None:
        self.service.set_tariff(energy.tou_tariff())
        self.window.refresh()
        self.assertIn("分时电价", self.window.tariff_label.text())

    def test_reset_uses_service(self) -> None:
        self.service.add_meter(MeterConfig(name="总表", power_point="1号变压器.p"))
        with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes):
            self.window.reset("day")
        self.assertTrue(self.reported)


class MeterDialogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ui_tools.ToolContext(
            project=meter_project(),
            window=None,
            settings=AppSettings(),
        )

    def test_config_maps_mode_and_point(self) -> None:
        dialog = MeterDialog(self.context, None)
        self.addCleanup(dialog.deleteLater)
        dialog.name_edit.setText("总表")
        index = dialog.point_combo.findData("1号变压器.p")
        dialog.point_combo.setCurrentIndex(index)
        config = dialog.config()
        self.assertEqual(config.name, "总表")
        self.assertEqual(config.mode, energy.MODE_POWER)
        self.assertEqual(config.power_point, "1号变压器.p")
        self.assertEqual(config.reading_point, "")

        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(energy.MODE_READING))
        dialog.point_combo.setCurrentIndex(dialog.point_combo.findData("1号变压器.e"))
        config = dialog.config()
        self.assertEqual(config.mode, energy.MODE_READING)
        self.assertEqual(config.reading_point, "1号变压器.e")
        self.assertEqual(config.power_point, "")

    def test_point_choice_survives_mode_switching(self) -> None:
        dialog = MeterDialog(self.context, None)
        self.addCleanup(dialog.deleteLater)
        dialog.point_combo.setCurrentIndex(dialog.point_combo.findData("1号变压器.p"))
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(energy.MODE_READING))
        dialog.point_combo.setCurrentIndex(dialog.point_combo.findData("1号变压器.e"))
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(energy.MODE_POWER))
        self.assertEqual(dialog.point_combo.currentData(), "1号变压器.p")
        dialog.mode_combo.setCurrentIndex(dialog.mode_combo.findData(energy.MODE_READING))
        self.assertEqual(dialog.point_combo.currentData(), "1号变压器.e")

    def test_missing_point_is_rejected(self) -> None:
        dialog = MeterDialog(self.context, None)
        self.addCleanup(dialog.deleteLater)
        dialog.point_combo.setCurrentIndex(-1)
        dialog.point_combo.setCurrentText("")
        with mock.patch.object(
            QMessageBox, "warning", mock.Mock(return_value=QMessageBox.Ok)
        ) as warned:
            dialog._accept()
        self.assertTrue(warned.called)
        self.assertEqual(dialog.result(), 0)


class TariffDialogTest(unittest.TestCase):
    def test_single_tariff_roundtrip(self) -> None:
        dialog = TariffDialog(energy.single_tariff(0.8))
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.single_radio.isChecked())
        dialog.price_box.setValue(0.75)
        tariff = dialog.tariff()
        self.assertFalse(tariff.segmented)
        self.assertAlmostEqual(tariff.price, 0.75)

    def test_tou_tariff_roundtrip(self) -> None:
        dialog = TariffDialog(energy.tou_tariff())
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.tou_radio.isChecked())
        self.assertEqual(dialog.table.rowCount(), len(tou_tariff_slots()))
        tariff = dialog.tariff()
        self.assertTrue(tariff.segmented)
        self.assertEqual(len(tariff.slots), len(energy.TOU_SLOTS))
        name, price = tariff.price_at(datetime(2026, 10, 18, 10, 30))
        self.assertEqual(name, "尖峰")
        self.assertAlmostEqual(price, 1.20)

    def test_preset_button_fills_slots(self) -> None:
        dialog = TariffDialog(energy.single_tariff())
        self.addCleanup(dialog.deleteLater)
        dialog.table.setRowCount(0)
        dialog._load_preset()
        self.assertEqual(dialog.table.rowCount(), len(energy.TOU_SLOTS))
        self.assertTrue(dialog.tou_radio.isChecked())

    def test_bad_time_is_reported_and_not_accepted(self) -> None:
        dialog = TariffDialog(energy.tou_tariff())
        self.addCleanup(dialog.deleteLater)
        dialog.table.item(0, 1).setText("8点")
        with mock.patch.object(
            QMessageBox, "warning", mock.Mock(return_value=QMessageBox.Ok)
        ) as warned:
            dialog._accept()
        self.assertTrue(warned.called)
        self.assertEqual(dialog.result(), QDialog.Rejected)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
