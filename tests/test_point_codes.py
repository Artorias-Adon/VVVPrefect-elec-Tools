"""状态码（``codes``）支持：整个值 -> 名称，而不是位掩码。

现场不少装置的运行状态就是一个编号（0/1/2…），不是"每一位什么意思"的位掩码。
这两条链路都要锁住：
* 模型层：``parse_code_labels`` / ``PointDef.code_labels()`` / ``code_label()``；
* 界面层：数值卡片把值翻成名称显示，表里没有的值照原样显示数字（不猜）；
* 参数表层：``状态码`` 列能导出也能导回。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import csv_table  # noqa: E402
from icomm.core import project as project_io  # noqa: E402
from icomm.core.models import PointDef, parse_code_labels  # noqa: E402
from icomm.ui.widgets import ValueCard  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class ParseCodeLabelsTest(unittest.TestCase):
    def test_parses_decimal_and_hex(self):
        self.assertEqual(
            parse_code_labels("0:停机,1:充电,0x10:放电"),
            [(0, "停机"), (1, "充电"), (16, "放电")],
        )

    def test_tolerates_separators_spaces_and_junk(self):
        labels = parse_code_labels(" 0:停机 ； 1: 待机 ,, 坏行,abc:无效,2:放电 ")
        self.assertEqual(labels, [(0, "停机"), (1, "待机"), (2, "放电")])

    def test_empty_text_gives_nothing(self):
        self.assertEqual(parse_code_labels(""), [])
        self.assertEqual(parse_code_labels(None), [])

    def test_out_of_range_values_are_dropped(self):
        self.assertEqual(parse_code_labels("0:甲,70000:乙,-1:丙"), [(0, "甲")])


class PointCodesTest(unittest.TestCase):
    def point(self, **kwargs) -> PointDef:
        base = {"name": "档位", "label": "档位", "address": 0}
        base.update(kwargs)
        return PointDef(**base)

    def test_code_label_lookup(self):
        point = self.point(codes="0:全部切除,1:投入1组,2:投入2组")
        self.assertEqual(point.code_labels()[1], (1, "投入1组"))
        self.assertEqual(point.code_label(2), "投入2组")
        self.assertEqual(point.code_label(9), "", "表里没有的值不猜")
        self.assertEqual(point.code_label(None), "")

    def test_code_label_accepts_text_numbers(self):
        point = self.point(codes="0:关,1:开")
        self.assertEqual(point.code_label("1"), "开")

    def test_bits_win_when_both_are_set(self):
        """两个都填时按位掩码显示（位定义信息量更大，不猜用户想用哪个）。"""
        point = self.point(bits="0:甲,1:乙", codes="0:关,1:开")
        self.assertTrue(point.bit_labels())
        self.assertEqual(point.code_labels(), [(0, "关"), (1, "开")])


class ValueCardCodesTest(unittest.TestCase):
    def card(self, **kwargs) -> ValueCard:
        point = PointDef(name="档位", label="电容器投切档位", address=21, **kwargs)
        card = ValueCard("1号配变.电容器投切档位", point)
        card.setFixedWidth(330)
        card.setFixedHeight(150)
        card.show()
        _app.processEvents()
        self.addCleanup(card.deleteLater)
        return card

    def test_shows_code_name_instead_of_number(self):
        card = self.card(codes="0:全部切除,1:投入1组,2:投入2组")
        card.set_value(1)
        self.assertEqual(card.value_label.text(), "投入1组")

    def test_unknown_value_shows_number_and_says_undefined(self):
        card = self.card(codes="0:全部切除,1:投入1组")
        card.set_value(7)
        self.assertIn("7", card.value_label.text())
        self.assertIn("未定义", card.value_label.text())

    def test_tooltip_lists_the_whole_table(self):
        card = self.card(codes="0:全部切除,1:投入1组")
        card.set_value(1)
        tip = card.toolTip()
        self.assertIn("0=全部切除", tip)
        self.assertIn("1=投入1组", tip)

    def test_bit_word_still_wins(self):
        card = self.card(bits="0:停机,1:运行", codes="0:甲,1:乙")
        card.set_value(2)  # bit1 置位 -> 运行（位定义优先于状态码）
        self.assertEqual(card.value_label.text(), "运行")

    def test_plain_point_unaffected(self):
        card = self.card()
        card.set_value(7.5)
        self.assertEqual(card.value_label.text(), "7.50")

    def test_bad_quality_does_not_translate(self):
        card = self.card(codes="0:全部切除,1:投入1组")
        card.set_value(None, quality="bad", error="通讯超时")
        self.assertNotEqual(card.value_label.text(), "全部切除")


class CodesCsvTest(unittest.TestCase):
    def test_codes_column_round_trips(self):
        project = project_io.grid_project()
        point = project.devices[2].point("电容器投切档位")
        self.assertTrue(point.codes, "示例里这台配变的投切档位该带状态码")

        rows = csv_table.export_rows(project)
        self.assertIn("状态码", rows[0], "参数表该有「状态码」这一列")
        row = next(row for row in rows if row["点号"] == "电容器投切档位")
        self.assertEqual(row["状态码"], point.codes)

        # 改一位再导回，应该被认下来（而不是丢字段）
        report = csv_table.import_rows(project, rows)
        self.assertEqual(list(report.issues), [])
        self.assertEqual(project.devices[2].point("电容器投切档位").codes, point.codes)

    def test_import_reads_codes_from_csv(self):
        project = project_io.bess_project()
        device = project.devices[0]
        row = {
            "启用": "是",
            "通道": device.channel,
            "设备": device.name,
            "从站号": str(device.unit_id),
            "点号": "并网开关",
            "显示名称": "并网开关",
            "寄存器区": "coil",
            "起始地址": "0",
            "类型": "bool",
            "状态码": "0:分闸,1:合闸",
        }
        report = csv_table.import_rows(project, [row])
        self.assertEqual(list(report.issues), [])
        self.assertEqual(device.point("并网开关").codes, "0:分闸,1:合闸")


if __name__ == "__main__":
    unittest.main()
