"""位域 / 命令点位卡片的渲染测试（离屏运行，不弹窗）。

锁住这几条行为：
* 状态字多个位同时置位时，标题要一起显示（「就绪 / 运行」），不能只显示第一个；
* 位名一行放不下时只列置位的，并用 ``+N`` 收口，不能出现被裁掉一半的文字；
* 置位太多时标题退化为「N / M 置位」；
* 命令点位渲染出按钮，点击后发出正确的写入值；
* 完整位名表始终在悬停提示里，保证信息不丢。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFontMetrics  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.ui.widgets import ValueCard  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class BitCardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.device = project_io.bess_project().devices[0]

    def card(self, point_name: str, width: int = 330) -> ValueCard:
        point = self.device.point(point_name)
        card = ValueCard("储能变流器PCS." + point_name, point)
        card.setFixedWidth(width)
        card.setFixedHeight(150)
        card.show()
        _app.processEvents()
        return card

    def test_single_active_bit(self):
        card = self.card("运行状态字")
        card.set_value(1 << 2)  # bit2 充电
        self.assertEqual(card.value_label.text(), "充电")

    def test_multiple_active_bits_all_shown(self):
        """回归：48 = 故障(bit4) + 维护(bit5)，标题必须两个都显示。"""
        card = self.card("运行状态字")
        card.set_value(0x30)
        self.assertEqual(card.value_label.text(), "故障 / 维护")

    def test_no_active_bit(self):
        card = self.card("运行状态字")
        card.set_value(0)
        self.assertEqual(card.value_label.text(), "无")

    def test_full_state_map_when_it_fits(self):
        """卡片够宽时显示完整状态图，置位的用强调色标出。"""
        card = self.card("运行状态字", width=560)
        card.set_value(0x30)
        row = card.sub_label.text()
        for label in ("停机", "待机", "充电", "放电", "故障", "维护"):
            self.assertIn(label, row, f"状态图缺少 {label}")
        self.assertNotIn("+", row)

    def test_long_bit_list_shows_only_active_with_counter(self):
        """16 位告警字一次置位 15 个：只列置位的，其余用 +N 收口。"""
        card = self.card("告警字", width=240)
        card.set_value(0x7FFF)
        row = card.sub_label.text()
        self.assertIn("交流过压", row)
        self.assertIn("+", row, row)
        self.assertNotIn("BMS告警", row)  # 未置位的位不占位置

    def test_many_active_bits_fall_back_to_counter_headline(self):
        card = self.card("告警字")
        card.set_value(0x7FFF)
        self.assertEqual(card.value_label.text(), "15 / 16 置位")

    def test_three_long_names_fall_back_to_counter_when_too_narrow(self):
        """窄到任何字体都排不下时退化为计数，而不是显示半截名字。"""
        card = self.card("功能配置字", width=130)
        card.set_value(0x0D)
        self.assertEqual(card.value_label.text(), "3 / 8 置位")

    def test_three_long_names_shrink_to_fit_when_wide(self):
        """卡片够宽时 3 个长名字降字号并排显示。"""
        card = self.card("功能配置字", width=560)
        card.set_value(0x0D)
        self.assertEqual(
            card.value_label.text(), "使能低电压穿越 / 使能防孤岛 / 使能限功率"
        )

    def test_headline_never_overflows_card(self):
        """不变量：不管多宽的卡片，标题都不许超出卡片（字体度量随平台而变）。"""
        for width in (150, 220, 330, 460, 700):
            card = self.card("功能配置字", width=width)
            card.set_value(0x0D)
            text = card.value_label.text()
            used = QFontMetrics(card.value_label.font()).horizontalAdvance(text)
            self.assertLessEqual(used, width, f"宽度 {width} 时标题溢出：{text}")

    def test_short_row_without_truncation_when_narrow(self):
        """卡片很窄时状态图放不下，退化为只列置位并收口，不出现半截文字。"""
        card = self.card("运行状态字", width=200)
        card.set_value(0x30)
        row = card.sub_label.text()
        self.assertIn("故障", row)
        self.assertIn("维护", row)

    def test_tooltip_keeps_full_bit_table(self):
        card = self.card("告警字")
        card.set_value(0x7FFF)
        tip = card.toolTip()
        self.assertIn("交流过压", tip)
        self.assertIn("BMS告警", tip)
        self.assertIn("●", tip)
        self.assertIn("○", tip)

    def test_bad_quality_shows_dash(self):
        card = self.card("运行状态字")
        card.set_value(None, quality="bad", error="通信异常")
        self.assertEqual(card.value_label.text(), "--")
        self.assertEqual(card.sub_label.text(), "通信异常")

    def test_command_buttons_emit_value(self):
        """控制字必须渲染出 5 个命令按钮，点「开机」发出 0x01。"""
        card = self.card("控制字")
        labels = [button.text() for button in card.command_buttons]
        self.assertEqual(labels, ["开机", "关机", "充电", "放电", "复位"])

        emitted: list[tuple[str, object]] = []
        card.writeRequested.connect(lambda pid, value: emitted.append((pid, value)))
        next(b for b in card.command_buttons if b.text() == "开机").click()
        self.assertEqual(emitted, [("储能变流器PCS.控制字", 0x01)])

        emitted.clear()
        next(b for b in card.command_buttons if b.text() == "复位").click()
        self.assertEqual(emitted, [("储能变流器PCS.控制字", 0x10)])

    def test_command_card_has_no_number_box(self):
        card = self.card("控制字")
        self.assertIsNone(card.write_box, "命令点位应只给按钮，不该再给数字输入框")

    def test_writable_plain_point_still_has_number_box(self):
        card = self.card("有功功率给定")
        self.assertIsNotNone(card.write_box)
        self.assertEqual(card.command_buttons, [])


if __name__ == "__main__":
    unittest.main()
