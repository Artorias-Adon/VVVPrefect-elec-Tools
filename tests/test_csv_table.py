"""CSV 参数表导入导出测试。

覆盖点：
* 导出带 BOM（Excel 中文不乱码）、列顺序固定；
* 导出 -> 导入 往返不丢信息；
* 导入自动建通道/设备，merge 更新、replace 重建；
* 逐行校验：地址越界、类型非法、点号重复等只跳过该行并报错；
* 数组点位由「元素个数」识别；
* 三相电压这种成组点位能正确落到不同地址。
"""

from __future__ import annotations

import csv
import os
import tempfile
import unittest

from icomm.core import csv_table, project as project_io
from icomm.core.models import Project


def _write_csv(path: str, rows: list[dict], columns: list[str] | None = None) -> None:
    columns = columns or csv_table.CSV_COLUMNS
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "parameters.csv")
        self.project = project_io.sst_project()

    def test_file_has_utf8_bom(self):
        csv_table.export_csv(self.project, self.path)
        with open(self.path, "rb") as fh:
            head = fh.read(3)
        self.assertEqual(head, b"\xef\xbb\xbf", "必须带 BOM，否则 Excel 打开中文乱码")

    def test_header_order_fixed(self):
        csv_table.export_csv(self.project, self.path)
        with open(self.path, "r", encoding="utf-8-sig", newline="") as fh:
            header = next(csv.reader(fh))
        self.assertEqual(header, csv_table.CSV_COLUMNS)

    def test_row_count_matches_points(self):
        count = csv_table.export_csv(self.project, self.path)
        self.assertEqual(count, len(self.project.all_point_ids()))

    def test_array_point_exported_with_count(self):
        # export_rows 输出的就是「可直接写进 CSV 的单元格文本」
        rows = {r["点号"]: r for r in csv_table.export_rows(self.project)}
        wave = rows["A相电压波形"]
        self.assertEqual(wave["元素个数"], "200")
        self.assertEqual(wave["采样间隔us"], "100")
        self.assertEqual(wave["录波"], "是")
        self.assertEqual(wave["示波器"], "是")
        self.assertEqual(rows["电网线电压"]["元素个数"], "1")
        self.assertEqual(rows["电网线电压"]["可写"], "否")

    def test_exported_bools_use_chinese_words(self):
        """布尔列导出为 是/否，而不是 True/False（配合 Excel 观感与表约定）。"""
        rows = csv_table.export_rows(self.project)
        values = {r["可写"] for r in rows} | {r["归档"] for r in rows} | {r["录波"] for r in rows}
        self.assertTrue(values <= {"是", "否"}, f"出现非 是/否 的布尔文本：{values}")


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "p.csv")

    def test_sst_roundtrip_preserves_everything(self):
        source = project_io.sst_project()
        csv_table.export_csv(source, self.path)

        target = Project(name="空工程")
        report = csv_table.import_csv(target, self.path)
        self.assertTrue(report.ok, report.detail())
        self.assertEqual(report.channels_added, 1)
        self.assertEqual(report.devices_added, 3)
        self.assertEqual(report.points_added, len(source.all_point_ids()))

        for device, point in source.iter_points():
            imported = target.device(device.name)
            self.assertIsNotNone(imported, device.name)
            got = imported.point(point.name)
            self.assertIsNotNone(got, f"{device.name}.{point.name}")
            for attr in (
                "register", "address", "data_type", "word_order", "scale", "offset",
                "unit", "decimals", "writable", "archive", "display", "kind", "count",
                "sample_interval_us", "record", "scope", "group",
            ):
                self.assertEqual(
                    getattr(got, attr), getattr(point, attr),
                    f"{device.name}.{point.name} 的 {attr} 不一致",
                )
            self.assertAlmostEqual(got.gauge_max, point.gauge_max)
            self.assertEqual(got.alarm.enabled, point.alarm.enabled)

    def test_alarm_thresholds_roundtrip(self):
        source = project_io.sst_project()
        csv_table.export_csv(source, self.path)
        target = Project()
        csv_table.import_csv(target, self.path)
        alarm = target.device("级联整流DSP").point("电网线电压").alarm
        self.assertTrue(alarm.enabled)
        self.assertAlmostEqual(alarm.hi_hi, 11.0)
        self.assertAlmostEqual(alarm.hi, 10.5)
        self.assertAlmostEqual(alarm.lo, 9.5)
        self.assertAlmostEqual(alarm.lo_lo, 9.0)

    def test_no_alarm_when_thresholds_empty(self):
        source = project_io.sst_project()
        csv_table.export_csv(source, self.path)
        target = Project()
        csv_table.import_csv(target, self.path)
        self.assertFalse(target.device("级联整流DSP").point("整流功率").alarm.enabled)


class MergeAndReplaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "p.csv")
        self.project = project_io.sst_project()

    def _rows_from_project(self):
        return csv_table.export_rows(self.project)

    def test_merge_updates_existing_point(self):
        rows = self._rows_from_project()
        for row in rows:
            if row["点号"] == "电网线电压":
                row["缩放"] = 0.01
                row["显示名称"] = "线电压(改)"
        _write_csv(self.path, rows)

        report = csv_table.import_csv(self.project, self.path)
        self.assertTrue(report.ok, report.detail())
        self.assertEqual(report.points_updated, len(rows))
        self.assertEqual(report.points_added, 0)
        point = self.project.device("级联整流DSP").point("电网线电压")
        self.assertAlmostEqual(point.scale, 0.01)
        self.assertEqual(point.label, "线电压(改)")

    def test_replace_clears_old_points(self):
        rows = [r for r in self._rows_from_project() if r["设备"] == "级联整流DSP"]
        _write_csv(self.path, rows)

        report = csv_table.import_csv(self.project, self.path, mode="replace")
        self.assertTrue(report.ok, report.detail())
        # 表里没提到的设备，点位被清空
        self.assertEqual(len(self.project.device("计量单元").points), 0)
        self.assertEqual(len(self.project.device("DCDC DSP").points), 0)
        # 表里有的设备保留（8 个标量 + 3 个波形数组）
        self.assertEqual(len(self.project.device("级联整流DSP").points), 11)

    def test_import_creates_channel_when_project_empty(self):
        rows = self._rows_from_project()
        _write_csv(self.path, rows)
        empty = Project(name="全新")
        report = csv_table.import_csv(empty, self.path)
        self.assertTrue(report.ok, report.detail())
        self.assertEqual(len(empty.channels), 1)
        self.assertTrue(any("默认 TCP 参数创建" in i.message for i in report.warnings))

    def test_existing_channel_is_reused(self):
        rows = self._rows_from_project()
        _write_csv(self.path, rows)
        report = csv_table.import_csv(self.project, self.path)
        self.assertEqual(report.channels_added, 0)
        self.assertEqual(len(self.project.channels), 1)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "p.csv")
        self.project = Project(name="校验")

    def _row(self, **kwargs):
        base = {
            "启用": "是", "通道": "ch1", "设备": "dev1", "从站号": "1",
            "点号": "p1", "显示名称": "P1", "分组": "默认", "寄存器区": "holding",
            "起始地址": "0", "类型": "uint16", "元素个数": "1", "字序": "big",
            "缩放": "1", "偏移": "0", "单位": "", "小数位": "2",
            "可写": "否", "归档": "是", "死区": "0", "显示方式": "card",
            "仪表下限": "0", "仪表上限": "100", "录波": "否", "采样间隔us": "0",
            "示波器": "否", "报警级别": "", "高高限": "", "高限": "",
            "低限": "", "低低限": "", "备注": "",
        }
        base.update(kwargs)
        return base

    def test_bad_register_reports_and_skips(self):
        _write_csv(self.path, [self._row(寄存器区="nope")])
        report = csv_table.import_csv(self.project, self.path)
        self.assertFalse(report.ok)
        self.assertEqual(report.skipped, 1)
        self.assertIn("寄存器区", report.errors[0].message)

    def test_bad_data_type_reports(self):
        _write_csv(self.path, [self._row(类型="uint24")])
        report = csv_table.import_csv(self.project, self.path)
        self.assertFalse(report.ok)
        self.assertIn("类型", report.errors[0].message)

    def test_address_out_of_range(self):
        _write_csv(self.path, [self._row(起始地址="70000")])
        report = csv_table.import_csv(self.project, self.path)
        self.assertFalse(report.ok)
        self.assertIn("起始地址", report.errors[0].message)

    def test_duplicate_point_reported(self):
        _write_csv(self.path, [self._row(点号="dup"), self._row(点号="dup", 起始地址="5")])
        report = csv_table.import_csv(self.project, self.path)
        self.assertFalse(report.ok)
        self.assertTrue(any("点号重复" in i.message for i in report.errors))
        self.assertEqual(report.points_added, 1)

    def test_error_row_does_not_block_others(self):
        _write_csv(
            self.path,
            [
                self._row(点号="good1"),
                self._row(点号="bad", 类型="xxx"),
                self._row(点号="good2", 起始地址="2"),
            ],
        )
        report = csv_table.import_csv(self.project, self.path)
        self.assertEqual(report.points_added, 2)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(len(report.errors), 1)

    def test_missing_required_columns_raises(self):
        _write_csv(self.path, [{"foo": "bar"}], columns=["foo"])
        with self.assertRaises(ValueError):
            csv_table.import_csv(self.project, self.path)

    def test_unknown_column_warns(self):
        rows = [self._row()]
        for row in rows:
            row["莫名其妙的列"] = "x"
        _write_csv(self.path, rows, columns=csv_table.CSV_COLUMNS + ["莫名其妙的列"])
        report = csv_table.import_csv(self.project, self.path)
        self.assertTrue(any("无法识别的列" in i.message for i in report.warnings))

    def test_excel_quirks_parsed(self):
        _write_csv(
            self.path,
            [
                self._row(
                    点号="q", 起始地址="10.0", 小数位="3.0", 缩放="0.1",
                    可写="TRUE", 归档="否", 显示方式="gauge",
                )
            ],
        )
        report = csv_table.import_csv(self.project, self.path)
        self.assertTrue(report.ok, report.detail())
        point = self.project.device("dev1").point("q")
        self.assertEqual(point.address, 10)
        self.assertEqual(point.decimals, 3)
        self.assertAlmostEqual(point.scale, 0.1)
        self.assertTrue(point.writable)
        self.assertFalse(point.archive)
        self.assertEqual(point.display, "gauge")

    def test_multi_element_becomes_array(self):
        # 「示波器」留空时应跟随数组点位默认值（进示波器通道）
        _write_csv(
            self.path,
            [self._row(点号="w", 元素个数="256", 采样间隔us="50", 录波="是", 示波器="")],
        )
        report = csv_table.import_csv(self.project, self.path)
        self.assertTrue(report.ok, report.detail())
        point = self.project.device("dev1").point("w")
        self.assertEqual(point.kind, "array")
        self.assertEqual(point.count, 256)
        self.assertEqual(point.sample_interval_us, 50)
        self.assertTrue(point.record)
        self.assertTrue(point.scope, "数组点位默认进示波器通道")

    def test_scope_explicit_false_respected(self):
        _write_csv(self.path, [self._row(点号="w2", 元素个数="8", 示波器="否")])
        report = csv_table.import_csv(self.project, self.path)
        self.assertTrue(report.ok, report.detail())
        self.assertFalse(self.project.device("dev1").point("w2").scope)

    def test_bool_array_rejected(self):
        _write_csv(self.path, [self._row(点号="b", 类型="bool", 元素个数="8")])
        report = csv_table.import_csv(self.project, self.path)
        self.assertFalse(report.ok)
        self.assertIn("bool 类型不支持数组", report.errors[0].message)

    def test_blank_line_ignored(self):
        _write_csv(self.path, [self._row(), {"点号": "", "设备": ""}])
        report = csv_table.import_csv(self.project, self.path)
        self.assertEqual(report.points_added, 1)

    def test_missing_device_reported(self):
        _write_csv(self.path, [self._row(设备="")])
        report = csv_table.import_csv(self.project, self.path)
        self.assertFalse(report.ok)
        self.assertIn("设备", report.errors[0].message)


class ThreePhaseTest(unittest.TestCase):
    """三相电压这种成组点位：一行一相，各自配地址与类型。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "3ph.csv")

    def test_three_phase_rows(self):
        rows = []
        for index, phase in enumerate(("A", "B", "C")):
            rows.append(
                {
                    "设备": "整流DSP", "从站号": "11", "点号": f"{phase}相电压",
                    "显示名称": f"{phase}相电压", "分组": "三相电压",
                    "寄存器区": "holding", "起始地址": str(index * 2),
                    "类型": "float32", "字序": "big", "缩放": "1", "单位": "V",
                    "小数位": "1", "归档": "是", "显示方式": "card",
                }
            )
        _write_csv(self.path, rows)

        project = Project(name="三相")
        report = csv_table.import_csv(project, self.path)
        self.assertTrue(report.ok, report.detail())
        self.assertEqual(report.points_added, 3)

        device = project.device("整流DSP")
        self.assertEqual([p.name for p in device.points], ["A相电压", "B相电压", "C相电压"])
        self.assertEqual([p.address for p in device.points], [0, 2, 4])
        for point in device.points:
            self.assertEqual(point.data_type, "float32")
            self.assertEqual(point.span, 2)


class HistoryExportTest(unittest.TestCase):
    """归档的历史数据能导出成宽表（一列一个点位），便于交报表。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from icomm.storage import Storage

        self.storage = Storage(os.path.join(self.tmp.name, "h.db"))
        self.storage.start()
        self.addCleanup(self.storage.stop)

    def test_export_history_wide_table(self):
        import time

        base = time.time()
        for index in range(3):
            self.storage.archive("dev.p1", 10 + index, base + index)
            self.storage.archive("dev.p2", 20 + index, base + index)
        self.storage.stop()

        path = os.path.join(self.tmp.name, "history.csv")
        rows = csv_table.export_history_csv(self.storage, ["dev.p1", "dev.p2"], base - 1, base + 10, path)
        self.assertEqual(rows, 3)

        import csv as _csv

        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            data = list(_csv.reader(fh))
        self.assertEqual(data[0], ["时间", "dev.p1", "dev.p2"])
        self.assertEqual(data[1][1:], ["10.0", "20.0"])
        self.assertEqual(data[3][1:], ["12.0", "22.0"])


if __name__ == "__main__":
    unittest.main()
