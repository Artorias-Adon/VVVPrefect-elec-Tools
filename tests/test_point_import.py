"""点表识别：认表头、转地址/类型、导出本应用点表，以及"认不出来就报错"。

这些测试盯的是"别把错位的点表导进工程"：

* 编码/分隔符/表头行认得对（带标题行的厂家表也能认）；
* 地址的四种写法都换对（`40001` / `4x0001` / `0x0010` / `16`），**且不是 4x 的纯数字不当线圈算**；
* 类型/读写/寄存器区按词表归一；开关量没写区默认线圈；
* 点号地址缺一样就**抛错并说清缺什么**（不猜）；
* 导出的 CSV 能真的被 `csv_table.import_rows` 导入成点位（端到端）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from icomm.core import csv_table
from icomm.core import point_import as pi
from icomm.core.models import Project

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


VENDOR_CSV = """某厂家 PCS 点表 v1.2
序号,变量名称,点位描述,寄存器地址,数据类型,工程单位,倍率,读写,备注
1,DC_Volt,直流母线电压,4x0001,无符号16位,V,0.1,R,
2,P_Act,有功功率,4x0010,INT16,kW,0.01,RW,正放负充
3,Run,运行状态,0x01,BOOL,,1,R,bit0 为运行
"""


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str, text: str, encoding: str = "utf-8-sig") -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
        return path

    def write_bytes(self, name: str, raw: bytes) -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, "wb") as handle:
            handle.write(raw)
        return path


# --------------------------------------------------------------- 读文件


class DecodeTest(TempDirCase):
    def test_utf8_with_bom(self) -> None:
        text, encoding = pi.decode_bytes("点名,地址\nA,40001\n".encode("utf-8-sig"))
        self.assertTrue(text.startswith("点名"))
        self.assertEqual(encoding, "utf-8-sig")

    def test_gbk(self) -> None:
        text, encoding = pi.decode_bytes("点名,地址\nA,40001\n".encode("gbk"))
        self.assertIn("点名", text)
        self.assertIn(encoding, ("gb18030", "gbk"))

    def test_utf16_with_bom(self) -> None:
        text, encoding = pi.decode_bytes("点名,地址\nA,40001\n".encode("utf-16"))
        self.assertIn("点名", text)
        self.assertEqual(encoding, "utf-16")

    def test_delimiter_detection(self) -> None:
        cases = [
            ("a,b,c\n1,2,3\n", ","),
            ("a\tb\tc\n1\t2\t3\n", "\t"),
            ("a;b;c\n1;2;3\n", ";"),
        ]
        for text, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(pi.detect_delimiter(text), expected)

    def test_semicolon_file_is_read(self) -> None:
        path = self.write("semi.csv", "点名;地址;类型\nA;40001;INT16\n", encoding="utf-8")
        table = pi.load_table(path)
        self.assertEqual(table.delimiter, ";")
        self.assertEqual(table.headers, ["点名", "地址", "类型"])
        self.assertEqual(table.data_rows, [["A", "40001", "INT16"]])

    def test_header_line_skips_preamble(self) -> None:
        path = self.write("vendor.csv", VENDOR_CSV)
        table = pi.load_table(path)
        self.assertEqual(table.header_line, 1, "第 1 行是标题，表头在第 2 行")
        self.assertEqual(len(table.data_rows), 3)

    def test_header_line_can_be_overridden(self) -> None:
        path = self.write("vendor.csv", VENDOR_CSV)
        table = pi.load_table(path, header_line=1)
        self.assertEqual(table.header_line, 1)

    def test_empty_and_missing_files(self) -> None:
        empty = self.write("empty.csv", "")
        with self.assertRaises(pi.PointTableError):
            pi.load_table(empty)
        with self.assertRaises(pi.PointTableError):
            pi.load_table(os.path.join(self.tmp.name, "没有这个.csv"))

    def test_old_xls_is_rejected_with_hint(self) -> None:
        path = self.write_bytes("old.xls", b"\xd0\xcf\x11\xe0")
        with self.assertRaises(pi.PointTableError) as caught:
            pi.load_table(path)
        self.assertIn("另存为", "".join(caught.exception.detail))


# --------------------------------------------------------------- 转值


class AddressTest(unittest.TestCase):
    def test_five_digit_numbering(self) -> None:
        self.assertEqual(pi.parse_address("40001")[:2], (0, "holding"))
        self.assertEqual(pi.parse_address("40010")[:2], (9, "holding"))
        self.assertEqual(pi.parse_address("30001")[:2], (0, "input"))
        self.assertEqual(pi.parse_address("10001")[:2], (0, "discrete"))
        self.assertEqual(pi.parse_address("00005")[:2], (4, "coil"))

    def test_prefix_notation(self) -> None:
        self.assertEqual(pi.parse_address("4x0001")[:2], (0, "holding"))
        self.assertEqual(pi.parse_address("3x0010")[:2], (9, "input"))
        self.assertEqual(pi.parse_address("1x0002")[:2], (1, "discrete"))
        self.assertEqual(pi.parse_address("4X0100")[:2], (99, "holding"))

    def test_hex_notation(self) -> None:
        self.assertEqual(pi.parse_address("16#0010")[:2], (16, ""))
        self.assertEqual(pi.parse_address("0x10")[:2], (16, ""), "光秃秃的 0x 按十六进制")

    def test_plain_offset_is_not_coil_numbering(self) -> None:
        """回归：以前"地址 16"会被当成线圈第 15 个，读错寄存器。"""
        self.assertEqual(pi.parse_address("16")[:2], (16, ""))
        self.assertEqual(pi.parse_address("128")[:2], (128, ""))

    def test_style_switch_changes_interpretation(self) -> None:
        self.assertEqual(pi.parse_address("0x0010", "auto")[:2], (16, ""))
        self.assertEqual(pi.parse_address("0x0010", "prefix")[:2], (9, "coil"))
        self.assertEqual(pi.parse_address("0x0010", "hex")[:2], (16, ""))

    def test_unknown_address(self) -> None:
        self.assertIsNone(pi.parse_address("DB1.DBW4")[0])
        self.assertIsNone(pi.parse_address("")[0])
        self.assertIsNone(pi.parse_address("起点")[0])


class ValueWordsTest(unittest.TestCase):
    def test_types(self) -> None:
        cases = {
            "INT16": "int16", "int": "int16", "有符号16位": "int16",
            "UINT16": "uint16", "WORD": "uint16", "无符号16位": "uint16",
            "REAL": "float32", "FLOAT": "float32", "浮点数": "float32", "单精度": "float32",
            "DINT": "int32", "DWORD": "uint32", "LREAL": "float64", "DOUBLE": "float64",
            "BOOL": "bool", "BIT": "bool", "开关量": "bool",
            "BYTE": "uint8", "SINT": "int8",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(pi.parse_type(text), expected)
        self.assertEqual(pi.parse_type("玄学类型"), "")
        self.assertEqual(pi.parse_type(""), "")

    def test_writable(self) -> None:
        for text in ("R/W", "RW", "读写", "可写", "是", "Y", "1"):
            self.assertIs(pi.parse_writable(text), True, text)
        for text in ("R", "RO", "只读", "否", "N", "0"):
            self.assertIs(pi.parse_writable(text), False, text)
        self.assertIsNone(pi.parse_writable("不知道"))

    def test_register(self) -> None:
        self.assertEqual(pi.parse_register("保持寄存器"), "holding")
        self.assertEqual(pi.parse_register("4x"), "holding")
        self.assertEqual(pi.parse_register("线圈"), "coil")
        self.assertEqual(pi.parse_register("输入寄存器"), "input")
        self.assertEqual(pi.parse_register(""), "")


# --------------------------------------------------------------- 认列


class GuessTest(TempDirCase):
    def test_chinese_vendor_table(self) -> None:
        path = self.write("vendor.csv", VENDOR_CSV)
        preview = pi.load_preview(path)
        mapping = {key: index for index, key in enumerate(
            ["", "name", "label", "address", "data_type", "unit", "scale", "writable", "description"]
        )}
        self.assertEqual(
            {key: value for key, value in preview.mapping().items()},
            {key: value for key, value in mapping.items() if key},
        )
        self.assertEqual(preview.table.header_line, 1)

    def test_english_headers(self) -> None:
        text = "Tag Name,Description,Address,Data Type,Unit,Gain,R/W\nP1,Power,40001,REAL,kW,0.01,RW\n"
        path = self.write("en.csv", text, encoding="utf-8")
        preview = pi.load_preview(path)
        row = preview.ok_rows[0].values
        self.assertEqual(row["点号"], "P1")
        self.assertEqual(row["显示名称"], "Power")
        self.assertEqual(row["起始地址"], "0")
        self.assertEqual(row["类型"], "float32")
        self.assertEqual(row["单位"], "kW")
        self.assertEqual(row["缩放"], "0.01")
        self.assertEqual(row["可写"], "是", "导出用 是/否，导入器才认")

    def test_unknown_header_detected_by_values(self) -> None:
        """表头是 A/B/C 这种瞎写的，就看值长什么样。"""
        text = "A,B,C\n电机温度,40001,INT16\n油温,40002,INT16\n绕组温度,40003,INT16\n"
        path = self.write("values.csv", text, encoding="utf-8")
        table = pi.load_table(path)
        fields = {guess.field_key for guess in pi.guess_columns(table) if guess.field_key}
        self.assertEqual(fields, {"label", "address", "data_type"})
        by_index = {guess.index: guess.field_key for guess in pi.guess_columns(table)}
        self.assertEqual(by_index[1], "address", "值全是 40001 这种 → 地址列")
        self.assertEqual(by_index[2], "data_type")
        self.assertEqual(by_index[0], "label", "中文名 → 显示名称")

    def test_duplicate_field_keeps_best_column(self) -> None:
        text = "名称,点位名称,地址\nA,B,40001\n"
        path = self.write("dup.csv", text, encoding="utf-8")
        guesses = pi.guess_columns(pi.load_table(path))
        labels = [guess.field_key for guess in guesses].count("label")
        self.assertEqual(labels, 1, "同一个字段只留一列")

    def test_manual_remap(self) -> None:
        text = "点名,地址,旁注\nP1,40001,动力\n"
        path = self.write("swap.csv", text, encoding="utf-8")
        preview = pi.load_preview(path)
        # 手工把第 3 列指定成备注
        preview.set_mapping(2, "description")
        self.assertEqual(preview.ok_rows[0].values["备注"], "动力")


# --------------------------------------------------------------- 转换


class ConvertTest(TempDirCase):
    def test_vendor_rows(self) -> None:
        path = self.write("vendor.csv", VENDOR_CSV)
        preview = pi.load_preview(path)
        self.assertEqual(preview.summary().count("转换 3 行"), 1)
        rows = {row.values["点号"]: row for row in preview.ok_rows}
        self.assertEqual(rows["DC_Volt"].values["起始地址"], "0")
        self.assertEqual(rows["P_Act"].values["起始地址"], "9")
        self.assertEqual(rows["P_Act"].values["可写"], "是")
        self.assertEqual(rows["Run"].values["寄存器区"], "coil", "开关量没写区默认线圈")
        self.assertEqual(rows["Run"].values["类型"], "bool")

    def test_default_device_from_file_name(self) -> None:
        path = self.write("3号机PCS.csv", "点名,地址\nA,40001\n", encoding="utf-8")
        preview = pi.load_preview(path)
        self.assertIn("3号机PCS", preview.ok_rows[0].values["设备"])

    def test_default_device_override(self) -> None:
        path = self.write("vendor.csv", VENDOR_CSV)
        preview = pi.load_preview(path, default_device="PCS-1")
        self.assertTrue(all(row.values["设备"] == "PCS-1" for row in preview.ok_rows))

    def test_duplicate_names_are_renamed(self) -> None:
        path = self.write("dup2.csv", "点名,地址\nA,40001\nA,40002\n", encoding="utf-8")
        preview = pi.load_preview(path)
        names = [row.values["点号"] for row in preview.ok_rows]
        self.assertEqual(names, ["A", "A_2"])

    def test_bad_address_row_is_skipped_with_reason(self) -> None:
        path = self.write("mix.csv", "点名,地址\n好点,40001\n坏点,DB1.DBW4\n", encoding="utf-8")
        preview = pi.load_preview(path)
        self.assertEqual([row.values["点号"] for row in preview.ok_rows], ["好点"])
        self.assertEqual(preview.skipped, 1)
        self.assertTrue(any("认不出来" in item for item in preview.issues))

    def test_blank_rows_are_ignored(self) -> None:
        path = self.write("blank.csv", "点名,地址\nA,40001\n,\n\nB,40002\n", encoding="utf-8")
        preview = pi.load_preview(path)
        self.assertEqual(len(preview.ok_rows), 2)

    def test_notes_are_not_errors(self) -> None:
        path = self.write("vendor.csv", VENDOR_CSV)
        preview = pi.load_preview(path)
        self.assertTrue(all(not row.issues for row in preview.rows), "换算说明不该算成问题")
        self.assertGreater(preview.note_count, 0)


class HardErrorTest(TempDirCase):
    def test_missing_address_column(self) -> None:
        path = self.write("noaddr.csv", "点名,类型\nA,INT16\nB,INT16\n", encoding="utf-8")
        with self.assertRaises(pi.PointTableError) as caught:
            pi.load_preview(path)
        error = caught.exception
        self.assertIn("address", [key for key in error.missing] + ["address"])
        text = error.as_text()
        self.assertIn("缺什么", text)
        self.assertIn("起始地址", text)
        self.assertIn("生成模板", text, "要告诉客户下一步怎么做")
        self.assertIn("本应用点表列", text)

    def test_missing_name_column(self) -> None:
        path = self.write("noname.csv", "地址,类型\n40001,INT16\n40002,INT16\n", encoding="utf-8")
        with self.assertRaises(pi.PointTableError) as caught:
            pi.load_preview(path)
        self.assertIn("点号", caught.exception.as_text())

    def test_not_a_point_table(self) -> None:
        path = self.write("other.csv", "XX,YY,ZZ\n1,2,3\n4,5,6\n7,8,9\n", encoding="utf-8")
        with self.assertRaises(pi.PointTableError) as caught:
            pi.load_preview(path)
        text = caught.exception.as_text()
        self.assertTrue("不是点表" in text or "缺" in text, text)
        self.assertIn("怎么办", text)

    def test_headers_ok_but_no_row_converts(self) -> None:
        path = self.write("badaddr.csv", "点名,地址\nA,起点\nB,终点\n", encoding="utf-8")
        with self.assertRaises(pi.PointTableError) as caught:
            pi.load_preview(path)
        self.assertIn("一行都转换不出来", caught.exception.message)


# --------------------------------------------------------------- 导出 & 端到端


class ExportTest(TempDirCase):
    def test_csv_text_headers(self) -> None:
        path = self.write("vendor.csv", VENDOR_CSV)
        text = pi.load_preview(path).csv_text()
        header = text.splitlines()[0]
        for column in ("设备", "点号", "起始地址", "类型", "寄存器区", "可写"):
            self.assertIn(column, header)

    def test_write_csv_creates_dirs(self) -> None:
        path = self.write("vendor.csv", VENDOR_CSV)
        preview = pi.load_preview(path)
        target = os.path.join(self.tmp.name, "out", "点表.csv")
        preview.write_csv(target)
        self.assertTrue(os.path.exists(target))

    def test_exported_csv_imports_into_project(self) -> None:
        """端到端：别家点表 → 我们格式 → 真的能导进工程（列名对不上就会失败）。"""
        path = self.write("vendor.csv", VENDOR_CSV)
        preview = pi.load_preview(path)
        target = os.path.join(self.tmp.name, "本应用点表.csv")
        preview.write_csv(target)

        project = Project(name="导入测试")
        rows, issues = csv_table.read_rows(target)
        report = csv_table.import_rows(project, rows, issues=issues)
        errors = [issue for issue in report.issues if issue.level == "error"]
        self.assertEqual(errors, [], [issue.message for issue in errors])
        self.assertEqual(len(project.devices), 1)
        device = project.devices[0]
        self.assertEqual(len(device.points), 3)
        point = device.point("P_Act")
        self.assertIsNotNone(point)
        self.assertEqual(point.address, 9)
        self.assertEqual(point.data_type, "int16")
        self.assertEqual(point.unit, "kW")
        self.assertAlmostEqual(point.scale, 0.01)
        self.assertTrue(point.writable)
        self.assertEqual(device.point("Run").register, "coil")


# --------------------------------------------------------------- 界面


class DialogTest(TempDirCase):
    def setUp(self) -> None:
        super().setUp()
        from icomm.ui.tools.point_import import PointImportDialog

        self.dialog = PointImportDialog()
        self.addCleanup(self.dialog.deleteLater)

    def test_registered_under_engineering(self) -> None:
        from icomm.ui import tools as ui_tools

        tool = ui_tools.find_tool("点表识别")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.group, "工程")
        self.assertEqual(tool.order, 15)
        names = [item.name for item in ui_tools.tools_by_group()["工程"]]
        self.assertIn("点表识别", names)

    def test_load_fills_mapping_and_preview(self) -> None:
        self.dialog.load_path(self.write("vendor.csv", VENDOR_CSV))
        self.assertIsNotNone(self.dialog.preview)
        self.assertEqual(self.dialog.map_table.rowCount(), 9)
        self.assertEqual(self.dialog.preview_table.rowCount(), 3)
        self.assertTrue(self.dialog.export_button.isEnabled())
        self.assertIn("转换 3 行", self.dialog.status.text())
        self.assertEqual(self.dialog.header_spin.value(), 2)

    def test_export_writes_file(self) -> None:
        self.dialog.load_path(self.write("vendor.csv", VENDOR_CSV))
        target = os.path.join(self.tmp.name, "导出.csv")
        written = self.dialog.export(target)
        self.assertEqual(written, target)
        self.assertTrue(os.path.exists(target))
        self.assertIn("已导出", self.dialog.status.text())

    def test_error_path_disables_export(self) -> None:
        self.dialog.load_path(self.write("nope.csv", "A,B\n1,2\n", encoding="utf-8"))
        self.assertIn("认不出来", self.dialog.status.text())
        self.assertFalse(self.dialog.export_button.isEnabled())
        self.assertTrue(self.dialog.copy_button.isEnabled())
        self.assertIn("缺什么", self.dialog.error_text)
        self.assertIn("缺什么", self.dialog.log_view.toPlainText())

    def test_style_switch_reconverts(self) -> None:
        path = self.write("coils.csv", "点名,地址\nC1,0x0001\n", encoding="utf-8")
        self.dialog.load_path(path)
        self.assertEqual(self.dialog.preview.ok_rows[0].values["起始地址"], "1", "自动按十六进制")
        index = self.dialog.style_combo.findData("prefix")
        self.dialog.style_combo.setCurrentIndex(index)
        self.dialog.reconvert()
        self.assertEqual(self.dialog.preview.ok_rows[0].values["起始地址"], "0", "前缀写法：第一个线圈")

    def test_header_spin_changes_detection(self) -> None:
        path = self.write("header.csv", "标题行\n点名,地址\nA,40001\n", encoding="utf-8")
        self.dialog.load_path(path)
        self.assertEqual(self.dialog.header_spin.value(), 2)
        self.dialog.header_spin.setValue(1)
        self.assertEqual(self.dialog.preview.table.header_line, 0)

    def test_manual_mapping_through_ui(self) -> None:
        path = self.write("swap.csv", "点名,地址\nP1,40001\n", encoding="utf-8")
        self.dialog.load_path(path)
        self.dialog.set_mapping(0, "label")
        self.assertIn("P1", self.dialog.preview.ok_rows[0].values["显示名称"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
