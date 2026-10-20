"""表格格式：CSV / TXT / Excel（.xlsx / .xls）。

现场同事的表九成是 Excel，所以"所有能导出 CSV 的地方"也要能导出 .xlsx、
"所有能导入 CSV 的地方"也要能导入 .xlsx / .xls。这里守住：

* 读写往返（含 bool → 是/否、整数不带 .0、中文）；
* CSV 编码兼容（UTF-8 BOM / GBK）；
* .xls 能读不能写（写要报明确的错，而不是悄悄写成别的东西）；
* 参数表、参数记录、点表识别、历史/录波这些真实入口都真的走通；
* 对话框过滤器里确实列了 Excel。
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from datetime import datetime

from icomm.core import analysis, csv_table, energy, params, point_import, project as project_io
from icomm.core import sheetio
from icomm.core import curvegen

try:
    import openpyxl  # noqa: F401

    HAVE_OPENPYXL = True
except ImportError:  # pragma: no cover - 环境里没装
    HAVE_OPENPYXL = False

try:
    import xlrd  # noqa: F401

    HAVE_XLRD = True
except ImportError:  # pragma: no cover
    HAVE_XLRD = False


class SheetIOTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def path(self, name: str) -> str:
        return os.path.join(self.tmp.name, name)

    def test_csv_round_trip(self) -> None:
        path = self.path("a.csv")
        sheetio.write_rows(path, [["名称", "值"], ["甲", 1.5], ["乙", True]])
        rows = sheetio.read_rows(path)
        self.assertEqual(rows[0], ["名称", "值"])
        self.assertEqual(rows[1], ["甲", "1.5"])
        self.assertEqual(rows[2], ["乙", "是"], "布尔统一写成 是/否")

    def test_gbk_csv_is_readable(self) -> None:
        path = self.path("gbk.csv")
        with io.open(path, "w", encoding="gb18030", newline="") as handle:
            handle.write("设备,点号,起始地址\n储能,电压,0\n")
        rows = sheetio.read_rows(path)
        self.assertEqual(rows[1][0], "储能")

    @unittest.skipUnless(HAVE_OPENPYXL, "需要 openpyxl")
    def test_xlsx_round_trip(self) -> None:
        path = self.path("a.xlsx")
        sheetio.write_rows(
            path,
            [["设备", "点号", "起始地址", "值"], ["储能", "电压", 0, 1.25]],
            sheet_title="参数表",
        )
        rows = sheetio.read_rows(path)
        self.assertEqual(rows[0], ["设备", "点号", "起始地址", "值"])
        self.assertEqual(rows[1], ["储能", "电压", "0", "1.25"])

    def test_write_xls_is_refused_clearly(self) -> None:
        with self.assertRaises(sheetio.SheetError) as caught:
            sheetio.write_rows(self.path("a.xls"), [["a"]])
        self.assertIn(".xlsx", str(caught.exception))
        self.assertIn(".xls", str(caught.exception))

    def test_missing_file_is_reported(self) -> None:
        with self.assertRaises(sheetio.SheetError):
            sheetio.read_rows(self.path("nope.xlsx"))

    def test_filters_mention_excel(self) -> None:
        self.assertIn("*.xlsx", sheetio.read_filter())
        self.assertIn("*.xls", sheetio.read_filter())
        self.assertIn("*.xlsx", sheetio.write_filter())
        self.assertNotIn("*.xls)", sheetio.write_filter(), "写不支持 .xls，别把它列进去")

    def test_ensure_suffix(self) -> None:
        self.assertEqual(
            sheetio.ensure_suffix("报告", "Excel 工作簿 (*.xlsx)"), "报告.xlsx"
        )
        self.assertEqual(
            sheetio.ensure_suffix("报告", "CSV 表格 (*.csv)"), "报告.csv"
        )
        self.assertEqual(sheetio.ensure_suffix("a.csv", ""), "a.csv", "已有扩展名不动")

    @unittest.skipUnless(HAVE_XLRD, "需要 xlrd")
    def test_xls_without_xlrd_message_mentions_alternatives(self) -> None:
        """没有 xls 样例文件也能验证：坏文件报错里带"另存为"提示。"""
        path = self.path("bad.xls")
        with open(path, "wb") as handle:
            handle.write(b"not-an-excel")
        with self.assertRaises(sheetio.SheetError) as caught:
            sheetio.read_rows(path)
        self.assertIn("打不开", str(caught.exception))


class ParameterTableExcelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()

    @unittest.skipUnless(HAVE_OPENPYXL, "需要 openpyxl")
    def test_export_then_import_xlsx(self) -> None:
        path = os.path.join(self.tmp.name, "参数表.xlsx")
        count = csv_table.export_table(self.project, path)
        self.assertGreater(count, 0)
        target = project_io.Project(name="目标")
        report = csv_table.import_csv(target, path, mode="replace")
        self.assertEqual(len(target.all_point_ids()), count)
        self.assertFalse([issue for issue in report.issues if issue.level == "error"], report.issues)

    @unittest.skipUnless(HAVE_OPENPYXL, "需要 openpyxl")
    def test_template_export_is_xlsx_when_asked(self) -> None:
        path = os.path.join(self.tmp.name, "模板.xlsx")
        csv_table.export_table(project_io.sst_project(), path)
        rows = sheetio.read_rows(path)
        self.assertIn("点号", rows[0])
        self.assertIn("起始地址", rows[0])
        self.assertGreater(len(rows), 2)


class ParamsRecordExcelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def _record(self) -> params.ParamRecord:
        record = params.ParamRecord(device="储能变流器PCS", unit_id=31)
        record.entries.append(
            params.ParamEntry(point="控制字", label="控制字", value=4, unit="", address="H#0")
        )
        return record

    @unittest.skipUnless(HAVE_OPENPYXL, "需要 openpyxl")
    def test_save_and_load_xlsx(self) -> None:
        path = os.path.join(self.tmp.name, "记录.xlsx")
        params.save(path, self._record(), params.FORMAT_XLSX)
        loaded = params.load(path)
        self.assertEqual(loaded.device, "储能变流器PCS")
        self.assertEqual(loaded.unit_id, 31)
        self.assertEqual(loaded.entries[0].point, "控制字")
        self.assertEqual(loaded.entries[0].value, 4)

    def test_filter_lists_excel(self) -> None:
        text = params.file_filter()
        self.assertIn("*.xlsx", text)
        self.assertIn("*.xls", text)

    def test_saving_xls_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            params.save(os.path.join(self.tmp.name, "记录.xls"), self._record())


class PointImportExcelTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    @unittest.skipUnless(HAVE_OPENPYXL, "需要 openpyxl")
    def test_load_table_from_xlsx(self) -> None:
        path = os.path.join(self.tmp.name, "别家点表.xlsx")
        sheetio.write_rows(
            path,
            [
                ["点表导出", "", ""],
                ["信号名称", "寄存器地址", "数据类型"],
                ["电压A", "40001", "UINT16"],
                ["电流A", "40002", "UINT16"],
            ],
        )
        table = point_import.load_table(path)
        self.assertEqual(table.encoding, "xlsx（openpyxl）")
        self.assertEqual(table.header_line, 1)
        preview = point_import.ImportPreview(
            table, point_import.guess_columns(table), default_device="储能变流器PCS"
        )
        preview.rebuild_rows()
        self.assertTrue(preview.ok_rows, "至少要认出两行")

    def test_load_table_from_xls_reports_error_without_crashing(self) -> None:
        path = os.path.join(self.tmp.name, "坏.xls")
        with open(path, "wb") as handle:
            handle.write(b"xls")
        with self.assertRaises(point_import.PointTableError) as caught:
            point_import.load_table(path)
        self.assertTrue(caught.exception.detail, "要给出「另存为」这类可行建议")


class ToolDataExcelTest(unittest.TestCase):
    """曲线回放、谐波分析、电能电费这些"读/写 CSV"的入口也认 Excel。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    @unittest.skipUnless(HAVE_OPENPYXL, "需要 openpyxl")
    def test_curvegen_replay_round_trip_xlsx(self) -> None:
        path = os.path.join(self.tmp.name, "curve.xlsx")
        values = [0.1 * index for index in range(12)]
        curvegen.write_replay_csv(path, values, 0.5)
        loaded, interval = curvegen.read_replay_csv(path)
        self.assertEqual(len(loaded), len(values))
        self.assertAlmostEqual(loaded[3], values[3], places=5)
        self.assertAlmostEqual(interval, 0.5, places=3)

    @unittest.skipUnless(HAVE_OPENPYXL, "需要 openpyxl")
    def test_analysis_reads_xlsx_waveform(self) -> None:
        path = os.path.join(self.tmp.name, "wave.xlsx")
        sheetio.write_rows(path, [["value"]] + [[1.0 * index] for index in range(16)])
        values = analysis.read_waveform_csv(path)
        self.assertEqual(len(values), 16)

    @unittest.skipUnless(HAVE_OPENPYXL, "需要 openpyxl")
    def test_energy_export_xlsx(self) -> None:
        path = os.path.join(self.tmp.name, "电能.xlsx")
        book = energy.EnergyBook()
        saved = book.export_csv(path, now=datetime(2026, 10, 20, 9, 0, 0))
        self.assertTrue(saved.endswith(".xlsx"))
        rows = sheetio.read_rows(path)
        self.assertTrue(any("电表" in cell for cell in rows[3]), rows[:4])


if __name__ == "__main__":
    unittest.main()
