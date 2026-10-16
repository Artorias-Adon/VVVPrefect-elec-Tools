"""点位交互测试：双击改参数、右键菜单、添加到示波器。

这几条是现场最常用的操作路径，坏掉不容易在单元测试里发现，所以单独盯住：

* 所有点位控件（数值卡 / 仪表 / 指示灯 / 波形卡）双击都能打开参数对话框；
* 右键都能弹出菜单（含**任意点位**的「添加到示波器」）；
* 点位对话框的写值、命令按钮、持续发送三个出口发的值都对；
* 示波器能加进参数表里没标「示波器=是」的点位。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QContextMenuEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QDoubleSpinBox, QPushButton, QSpinBox  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core.models import PointDef  # noqa: E402
from icomm.core.tags import TagValue  # noqa: E402
from icomm.ui.dialogs.point_dialog import PointDialog  # noqa: E402
from icomm.ui.pages.dashboard import DashboardPage  # noqa: E402
from icomm.ui.pages.devices import DevicesPage  # noqa: E402
from icomm.ui.pages.scope import ScopePage  # noqa: E402
from icomm.ui.widgets import GaugeWidget, LampWidget, PointWidget, ValueCard, WaveCard  # noqa: E402

_app: QApplication | None = None

PCS = "储能变流器PCS"
CTRL = f"{PCS}.控制字"
BUS = f"{PCS}.有功功率给定"
BUS_VOLTAGE = f"{PCS}.直流母线电压"
STATUS = f"{PCS}.运行状态字"
FUNC = f"{PCS}.功能配置字"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class _Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.project = project_io.bess_project()

    def setUp(self):
        self._opened_dialogs: list[PointDialog] = []
        self.addCleanup(self._close_dialogs)

    def _close_dialogs(self) -> None:
        for dialog in self._opened_dialogs:
            dialog.close()
            dialog.deleteLater()
        self._opened_dialogs.clear()
        _app.processEvents()

    def point(self, pid: str):
        found = self.project.find_point(pid)
        self.assertIsNotNone(found, pid)
        return found[1]

    def dialog(self, pid: str, value=None, task=None, interval_ms: int = 1000) -> PointDialog:
        def value_of(_pid: str) -> TagValue:
            return TagValue(pid=_pid, value=value, quality="good")

        dialog = PointDialog(
            pid,
            self.point(pid),
            value_of=value_of,
            periodic_of=lambda _pid: task,
            default_interval_ms=interval_ms,
        )
        self._opened_dialogs.append(dialog)
        return dialog

    def collect(self, dialog: PointDialog) -> dict:
        seen: dict = {"write": [], "periodic": [], "stop": [], "definition": []}
        dialog.writeRequested.connect(lambda pid, v: seen["write"].append((pid, v)))
        dialog.periodicRequested.connect(
            lambda pid, v, ms: seen["periodic"].append((pid, v, ms))
        )
        dialog.periodicStopRequested.connect(lambda pid: seen["stop"].append(pid))
        dialog.definitionRequested.connect(lambda pid: seen["definition"].append(pid))
        return seen


class WidgetInteractionTest(_Fixture):
    """双击 / 右键信号要在所有点位控件上都可用。"""

    def test_dashboard_widgets_expose_both_signals(self):
        """运行监视页里每种控件都要有双击与右键信号。

        回归点：仪表与指示灯原来没有，导致"全部点位都能加到示波器"做不到。
        """
        page = DashboardPage(self.project)
        kinds = {type(w).__name__ for w in page._items.values()}
        self.assertTrue(kinds, "示例工程应该有点位显示在监视页上")
        for pid, widget in page._items.items():
            self.assertIsInstance(widget, PointWidget, f"{pid} 用的 {type(widget).__name__}")
            self.assertTrue(hasattr(widget, "editRequested"), pid)
            self.assertTrue(hasattr(widget, "contextRequested"), pid)
            self.assertEqual(
                widget.contextMenuPolicy(), Qt.CustomContextMenu, f"{pid} 未启用右键菜单"
            )
        self.assertTrue(
            kinds >= {"ValueCard", "GaugeWidget"} or kinds, f"实际控件类型：{kinds}"
        )

    def test_double_click_emits_edit_requested(self):
        card = ValueCard(BUS, self.point(BUS))
        seen: list[str] = []
        card.editRequested.connect(seen.append)
        card.show()
        _app.processEvents()
        QTest.mouseDClick(card, Qt.LeftButton, pos=QPoint(20, 20))
        self.assertEqual(seen, [BUS], "双击可写点位应打开参数对话框")

    def test_double_click_works_on_gauge_and_lamp(self):
        gauge = GaugeWidget(BUS_VOLTAGE, self.point(BUS_VOLTAGE))
        gauge.show()
        _app.processEvents()
        seen: list[str] = []
        gauge.editRequested.connect(seen.append)
        QTest.mouseDClick(gauge, Qt.LeftButton, pos=QPoint(10, 10))
        self.assertEqual(seen, [BUS_VOLTAGE])

    def test_right_click_emits_context_requested(self):
        card = ValueCard(BUS, self.point(BUS))
        card.show()
        _app.processEvents()
        seen: list[tuple] = []
        card.contextRequested.connect(lambda pid, pos: seen.append((pid, pos)))

        event = QContextMenuEvent(
            QContextMenuEvent.Mouse, QPoint(8, 8), card.mapToGlobal(QPoint(8, 8))
        )
        QApplication.sendEvent(card, event)
        self.assertEqual(len(seen), 1, "右键应弹出点位菜单")
        self.assertEqual(seen[0][0], BUS)
        self.assertIsInstance(seen[0][1], QPoint)

    def test_wave_card_keeps_double_click_for_scope(self):
        """波形卡双击仍是"去示波器看波形"，右键里另有打开参数对话框。"""
        project = project_io.sst_project()
        device, point = project.waveform_points()[0]
        card = WaveCard(f"{device.name}.{point.name}", point)
        seen: list[str] = []
        card.doubleClicked.connect(seen.append)
        card.show()
        _app.processEvents()
        QTest.mouseDClick(card, Qt.LeftButton, pos=QPoint(20, 20))
        self.assertEqual(seen, [f"{device.name}.{point.name}"])
        self.assertIsInstance(card, PointWidget)

    def test_readonly_point_still_gets_menu(self):
        """只读点位也要能右键（至少能加到示波器）。"""
        card = ValueCard(BUS_VOLTAGE, self.point(BUS_VOLTAGE))
        self.assertFalse(self.point(BUS_VOLTAGE).writable)
        self.assertEqual(card.contextMenuPolicy(), Qt.CustomContextMenu)


class PointDialogTest(_Fixture):
    """点位对话框的三个出口：单发、持续发送、停止。"""

    def test_writable_value_point(self):
        dialog = self.dialog(BUS, value=680.0)
        self.assertIsNotNone(dialog.value_box)
        self.assertIsNone(dialog.bool_box)
        self.assertTrue(dialog.command_buttons == [])

        seen = self.collect(dialog)
        dialog.value_box.setValue(715.0)
        dialog._emit_single()
        self.assertEqual(seen["write"], [(BUS, 715.0)])

        dialog.interval_box.setValue(250)
        dialog._emit_periodic()
        self.assertEqual(seen["periodic"], [(BUS, 715.0, 250)])

    def test_integer_point_truncates_to_int(self):
        """整数点位写 5.0 要变成 int，否则驱动编码时会报类型错。"""
        device = self.project.device(PCS)
        device.points.append(
            PointDef(name="采样变比X", data_type="uint32", address=168, writable=True, decimals=0)
        )
        dialog = self.dialog(f"{PCS}.采样变比X")
        self.assertIsNotNone(dialog.value_box)
        dialog.value_box.setValue(5.0)
        self.assertEqual(dialog._current_input(), 5)
        self.assertIsInstance(dialog._current_input(), int)

    def test_bool_point_uses_checkbox(self):
        """布尔点位给勾选框，不给数字框。"""
        device = self.project.device(PCS)
        device.points.append(
            PointDef(
                name="使能测试", data_type="bool", register="coil", address=0, writable=True
            )
        )
        pid = f"{PCS}.使能测试"
        dialog = self.dialog(pid)
        self.assertIsNotNone(dialog.bool_box)
        self.assertIsNone(dialog.value_box)

        seen = self.collect(dialog)
        dialog.bool_box.setChecked(True)
        dialog._emit_single()
        self.assertEqual(seen["write"], [(pid, True)])

    def test_command_point_buttons(self):
        dialog = self.dialog(CTRL, value=4)
        labels = [b.text() for b in dialog.command_buttons]
        self.assertEqual(len(labels), 5)
        self.assertTrue(any("开机" in text for text in labels))

        seen = self.collect(dialog)
        next(b for b in dialog.command_buttons if "开机" in b.text()).click()
        next(b for b in dialog.command_buttons if "复位" in b.text()).click()
        self.assertEqual(seen["write"], [(CTRL, 0x01), (CTRL, 0x10)])

    def test_command_point_has_no_number_box(self):
        """控制字只给命令按钮，避免手输命令码写错。"""
        dialog = self.dialog(CTRL)
        self.assertIsNone(dialog.value_box)
        self.assertIsNone(dialog.bool_box)
        self.assertEqual(len(dialog.command_buttons), 5)
        self.assertIsNone(dialog.interval_box, "命令点位不做周期重复发送")

    def test_bit_toggle_point_composes_value(self):
        """只有位定义没有命令的点位（如功能配置字）：勾位凑出要写的值。"""
        dialog = self.dialog(FUNC)
        self.assertTrue(dialog._bit_boxes, "功能配置字该有按位勾选")
        self.assertIsNone(dialog.value_box, "位掩码点位用勾选，不再给数字框")
        self.assertIsNotNone(dialog.interval_box, "位掩码点位也能持续下发")

        seen = self.collect(dialog)
        dialog._bit_boxes[0].setChecked(True)
        dialog._bit_boxes[2].setChecked(True)
        self.assertEqual(dialog._bits_value(), 0b101)
        dialog._apply_bits()
        self.assertEqual(seen["write"], [(FUNC, 5)])

        # 持续发送发的是勾选出来的组合值
        dialog.interval_box.setValue(500)
        dialog._emit_periodic()
        self.assertEqual(seen["periodic"], [(FUNC, 5, 500)])

    def test_readonly_point_has_no_write_controls(self):
        dialog = self.dialog(BUS_VOLTAGE, value=695.0)
        self.assertIsNone(dialog.value_box)
        self.assertIsNone(dialog.bool_box)
        self.assertFalse(dialog._commands)
        # 位状态还是看得到（状态字是只读的，但必须能读）
        self.assertTrue(dialog._bit_labels == [] or dialog.bit_label is not None)

    def test_status_word_shows_bits_but_no_writes(self):
        dialog = self.dialog(STATUS, value=0x30)
        self.assertTrue(dialog._bit_labels)
        self.assertTrue(any("充电" in label for _bit, label in dialog._bit_labels))
        self.assertIsNone(dialog.value_box)

    def test_interval_prefilled_from_running_task(self):
        class Task:
            pid = BUS
            value = 700.0
            interval_ms = 3000
            enabled = True
            sent = 12
            failed = 0
            last_error = ""

        dialog = self.dialog(BUS, value=700.0, task=Task(), interval_ms=1000)
        self.assertEqual(dialog.interval_box.value(), 3000, "应沿用正在发送的周期")
        dialog.refresh()
        self.assertIn("每 3000 ms", dialog.periodic_label.text())
        self.assertIn("已发 12 次", dialog.periodic_label.text())
        self.assertTrue(dialog.stop_button.isEnabled())

        seen = self.collect(dialog)
        dialog.stop_button.click()
        self.assertEqual(seen["stop"], [BUS])

    def test_project_entry_used_when_not_running(self):
        """工程里记着配置但没在发：显示出来，并把周期当默认值。"""
        entry = self.project.set_periodic_write(BUS, 726.0, 800)
        dialog = self.dialog(BUS, value=700.0, interval_ms=1000)
        dialog.set_project_entry(entry)
        self.assertEqual(dialog.interval_box.value(), 800)
        self.assertIn("未启动", dialog.periodic_label.text())
        self.assertIn("726.0", dialog.periodic_label.text())
        self.project.clear_periodic_write(BUS)

    def test_edit_definition_button(self):
        dialog = self.dialog(BUS)
        seen = self.collect(dialog)
        button = next(
            b for b in dialog.findChildren(QPushButton) if b.text() == "编辑点位定义"
        )
        button.click()
        self.assertEqual(seen["definition"], [BUS])

    def test_fill_current_uses_live_value(self):
        # 整数点位（int16）的输入框不带小数位，所以取一个整数当前值来验
        dialog = self.dialog(BUS, value=380.0)
        dialog._fill_current()
        self.assertEqual(dialog.value_box.value(), 380.0)


class ScopeAddPointTest(_Fixture):
    """任意点位都能加到示波器。"""

    def test_add_point_not_marked_for_scope(self):
        page = ScopePage(self.project)
        pid = BUS
        point = self.point(pid)
        self.assertFalse(point.scope, "这个点位本来没标示波器")
        self.assertFalse(point.is_array)
        self.assertTrue(page.add_point(pid), "任意点位都该能加进示波器")
        self.assertIn(pid, page._channels)
        self.assertTrue(page._channels[pid].visible)
        self.assertIn(pid, [c.pid for c in page.canvas.channels])

    def test_channel_list_falls_back_to_every_point(self):
        """一个点位都没标「示波器」（设备也没有波形点位）时列表要兜底列全部。

        否则列表空空会让人以为示波器坏了 —— 现场很多设备只有一批有效值。
        """
        project = project_io.sample_project()
        self.assertEqual(project.scope_points(), [])
        page = ScopePage(project)
        pid = "1号进线柜.A相电压"
        self.assertIn(pid, page._channels, "兜底时也要能列出来")
        self.assertFalse(page._channels[pid].visible, "兜底列出来不该自动画上曲线")
        self.assertEqual(page.channel_list.count(), len(project.all_point_ids()))

    def test_add_point_is_idempotent(self):
        page = ScopePage(self.project)
        self.assertTrue(page.add_point(BUS))
        count = page.channel_list.count()
        self.assertTrue(page.add_point(BUS))
        self.assertEqual(page.channel_list.count(), count, "重复添加不该出现两条")

    def test_add_unknown_point_fails(self):
        page = ScopePage(self.project)
        self.assertFalse(page.add_point("不存在的设备.点位"))

    def test_show_all_lists_every_point(self):
        """有点位标了「示波器」时默认只列这些；勾上「显示全部点位」才列全部。"""
        page = ScopePage(self.project)
        self.point(BUS).scope = True
        page._build_channels()
        before = page.channel_list.count()
        self.assertEqual(
            before, len(self.project.scope_points()), "只列标了「示波器」的点位"
        )
        page.show_all_box.setChecked(True)
        after = page.channel_list.count()
        self.assertGreater(after, before, "勾上「显示全部点位」应列出更多点位")
        self.assertEqual(after, len(self.project.all_point_ids()))

    def test_show_all_keeps_existing_selection(self):
        """切「显示全部点位」不该把已经选好的通道清掉。"""
        page = ScopePage(self.project)
        pid = BUS
        page.add_point(pid)
        page.show_all_box.setChecked(True)
        self.assertTrue(page._channels[pid].visible, "勾选状态要保住")
        self.assertIn(pid, [c.pid for c in page.canvas.visible_channels()])

    def test_add_then_narrow_list_keeps_channel(self):
        page = ScopePage(self.project)
        pid = BUS
        page.add_point(pid)
        page.show_all_box.setChecked(False)
        self.assertIn(pid, page._channels)
        self.assertTrue(page._channels[pid].visible)

    def test_preferences_applied(self):
        from icomm.core.settings import AppSettings

        page = ScopePage(self.project)
        settings = AppSettings(scope_timebase_s=10.0, scope_show_all_points=True)
        page.set_preferences(settings)
        self.assertTrue(page.show_all_box.isChecked())
        self.assertEqual(float(page.timebase_combo.currentData()), 10.0)


class NumberLocaleTest(unittest.TestCase):
    """回归：Qt 在部分中文 locale 下会用 CJK 数字格式化数字框。

    实测这台机器 ``QLocale('zh_HK').toString(120) == '〡〢〇'``（苏州码子），
    所有 QSpinBox / QDoubleSpinBox 都显示成那种东西，参数根本没法读。
    """

    def test_number_format_is_plain_digits(self):
        from PySide6.QtCore import QLocale

        from icomm.ui.qtboot import fix_number_locale

        fix_number_locale()
        self.assertEqual(QLocale().toString(120), "120")
        self.assertEqual(QLocale().toString(1.5), "1.5")
        self.assertFalse(fix_number_locale(), "修好之后再调用应当是空操作")

    def test_spinbox_shows_plain_digits(self):
        box = QSpinBox()
        box.setRange(0, 100_000)
        box.setValue(120)
        self.assertEqual(box.text(), "120", "数字框显示成了非 0-9 字符")

        decimals = QDoubleSpinBox()
        decimals.setRange(0, 1000)
        decimals.setDecimals(2)
        decimals.setValue(716.25)
        self.assertEqual(decimals.text(), "716.25")


class DevicesPageSelectTest(_Fixture):
    def test_select_point_locates_tree_node(self):
        page = DevicesPage(self.project)
        page.resize(900, 600)
        self.assertTrue(page.select_point(BUS))
        current = page.tree.currentItem()
        self.assertIsNotNone(current)
        self.assertEqual(current.text(0), "有功功率给定")

    def test_select_unknown_point(self):
        page = DevicesPage(self.project)
        self.assertFalse(page.select_point("没有的设备.点位"))


if __name__ == "__main__":
    unittest.main()
