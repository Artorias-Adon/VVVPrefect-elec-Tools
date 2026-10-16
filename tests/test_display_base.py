"""显示进制测试。

用户要求：数据显示可以右键选不同的进制。这里盯住四层：
格式化结果、点位字段与工程/CSV 往返、右键菜单里的选项、以及真的改了以后界面跟着变。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QWidget  # noqa: E402

from icomm.core import csv_table, project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core.codec import format_base, format_value, normalize_base  # noqa: E402
from icomm.core.models import DISPLAY_BASES  # noqa: E402

_app: QApplication | None = None

PID = "储能变流器PCS.运行状态字"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class FormatTest(unittest.TestCase):
    def test_hex_dec_bin_oct(self) -> None:
        self.assertEqual(format_value(255, 0, 16), "0xFF")
        self.assertEqual(format_value(255, 0, 10), "255")
        self.assertEqual(format_value(5, 0, 2), "0b101")
        self.assertEqual(format_value(8, 0, 8), "0o10")

    def test_decimal_keeps_old_behaviour(self) -> None:
        """不给进制参数时和老版本完全一样。"""
        self.assertEqual(format_value(3.14159, 2), "3.14")
        self.assertEqual(format_value(7, 3), "7")
        self.assertEqual(format_value(True, 2), "ON")
        self.assertEqual(format_value(None, 2), "--")

    def test_unknown_base_falls_back_to_decimal(self) -> None:
        self.assertEqual(format_value(255.5, 2, 7), "255.50")
        self.assertEqual(format_value(255.5, 2, "乱七八糟"), "255.50")

    def test_negative_keeps_sign(self) -> None:
        self.assertEqual(format_value(-3, 0, 16), "-0x3")

    def test_float_is_rounded_in_other_bases(self) -> None:
        """非十进制按整数取整（看状态字/寄存器最常用，模拟量会丢小数）。"""
        self.assertEqual(format_value(50.5, 1, 16), "0x32")

    def test_bad_values_do_not_raise(self) -> None:
        self.assertEqual(format_value("abc", 2, 16), "abc")
        self.assertEqual(format_base(0, 16), "0x0")

    def test_normalize_accepts_names(self) -> None:
        self.assertEqual(normalize_base("十六进制"), 16)
        self.assertEqual(normalize_base("hex"), 16)
        self.assertEqual(normalize_base("0b".replace("b", "x")), 16)
        self.assertEqual(normalize_base("二进制"), 2)
        self.assertEqual(normalize_base("十进制"), 10)
        self.assertEqual(normalize_base(None), 10)
        self.assertEqual(normalize_base(8), 8)


class ProjectRoundTripTest(unittest.TestCase):
    def test_point_field_default_is_decimal(self) -> None:
        _device, point = list(project_io.bess_project().iter_points())[0]
        self.assertEqual(point.display_base, 10)

    def test_point_json_roundtrip(self) -> None:
        from icomm.core.models import Project

        project = project_io.bess_project()
        _device, point = project.find_point(PID)
        point.display_base = 16
        restored = Project.from_dict(project.to_dict())
        _device2, point2 = restored.find_point(PID)
        self.assertEqual(point2.display_base, 16)

    def test_illegal_base_is_sanitised(self) -> None:
        from icomm.core.models import PointDef

        self.assertEqual(PointDef.from_dict({"name": "x", "display_base": 7}).display_base, 10)
        self.assertEqual(
            PointDef.from_dict({"name": "x", "display_base": "hex"}).display_base, 16
        )

    def test_csv_roundtrip(self) -> None:
        project = project_io.bess_project()
        _device, point = project.find_point(PID)
        point.display_base = 2
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.csv")
            csv_table.export_csv(project, path)
            with open(path, "r", encoding="utf-8-sig") as fh:
                head = fh.readline()
            self.assertIn("显示进制", head)
            target = project_io.bess_project()
            report = csv_table.import_csv(target, path)
        _device2, point2 = target.find_point(PID)
        self.assertEqual(point2.display_base, 2)
        self.assertEqual(report.issues, [])


class MenuTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self) -> None:
        from icomm.ui.main_window import MainWindow

        self.project = project_io.bess_project()
        paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "工程"), self.project
        )
        self.window = MainWindow(
            self.project, paths, workdir=self.tmp.name, base_dir=self.tmp.name
        )
        self.window.settings.confirm_on_exit = False
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if self.window.engine is not None:
            self.window.engine.stop()
        if self.window.storage is not None:
            self.window.storage.stop()
        self.window.settings.confirm_on_exit = False
        self.window.close()

    def _base_menu(self):
        menu = self.window.build_point_menu(PID)
        for action in menu.actions():
            if action.text().startswith("显示进制"):
                return action.menu()
        self.fail("右键菜单里应该有「显示进制」")

    def test_menu_lists_all_bases(self) -> None:
        submenu = self._base_menu()
        labels = [action.text() for action in submenu.actions()]
        self.assertEqual(labels, list(DISPLAY_BASES.values()))

    def test_menu_marks_current_choice(self) -> None:
        submenu = self._base_menu()
        checked = [a.text() for a in submenu.actions() if a.isChecked()]
        self.assertEqual(checked, ["十进制"])

    def test_set_base_updates_point_and_marks_dirty(self) -> None:
        self.window._dirty = False
        self.assertTrue(self.window.set_point_display_base(PID, 16))
        _device, point = self.window.project.find_point(PID)
        self.assertEqual(point.display_base, 16)
        self.assertTrue(self.window._dirty, "改了显示设置要标记工程有改动")

    def test_set_base_rejects_unknown_point(self) -> None:
        self.assertFalse(self.window.set_point_display_base("不存在.点位", 16))

    def test_set_base_sanitises_argument(self) -> None:
        self.window.set_point_display_base(PID, "hex")
        _device, point = self.window.project.find_point(PID)
        self.assertEqual(point.display_base, 16)

    def test_card_shows_chosen_base(self) -> None:
        """卡片上的数字要跟着进制走（原先是十进制，改成十六进制后立刻变）。"""
        from icomm.ui.widgets import ValueCard

        _device, point = self.window.project.find_point("储能变流器PCS.直流母线电压")
        card = ValueCard("储能变流器PCS.直流母线电压", point)
        card.set_value(255, "good", "")
        self.assertIn("255", " ".join(self._texts(card)))

        point.display_base = 16
        card.set_value(255, "good", "")
        self.assertTrue(
            any("0xFF" in text for text in self._texts(card)),
            f"卡片上应显示 0xFF，实际：{self._texts(card)}",
        )

    @staticmethod
    def _texts(widget) -> list[str]:
        return [child.text() for child in widget.findChildren(QLabel) if child.text()]


if __name__ == "__main__":
    unittest.main()
