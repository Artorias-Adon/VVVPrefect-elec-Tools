"""点位参数对话框：「位状态」与「按位设置」同步。

以前两半各说各的：位状态显示设备当前值，按位设置却永远从全不勾开始，
勾了以后位状态也不跟着变，用户看不出"我现在到底要写什么"。

现在：

* 打开时勾选 = 设备当前值；
* 用户勾/取消后位状态里标「↑ 待置位 / ↓ 待清零」，预览写出当前值与目标值；
* 用户没动过时，取到新值就跟着同步；动过就不覆盖用户的改动；
* 设备回读成目标值后，自动认为"没有未写入的改动"；
* 「按当前值重填」可以一键丢弃改动。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core.tags import TagValue  # noqa: E402
from icomm.ui.dialogs.point_dialog import PointDialog  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


FUNC = "储能变流器PCS.功能配置字"
STATUS = "储能变流器PCS.运行状态字"


class BitSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = project_io.bess_project()
        self.values = {"value": 0b101}
        self.dialogs: list[PointDialog] = []

    def tearDown(self) -> None:
        for dialog in self.dialogs:
            dialog.close()
            dialog.deleteLater()
        self.dialogs.clear()
        _app.processEvents()

    def dialog(self, pid: str = FUNC) -> PointDialog:
        found = self.project.find_point(pid)
        self.assertIsNotNone(found)
        dialog = PointDialog(
            pid,
            found[1],
            value_of=lambda p: TagValue(pid=p, value=self.values["value"], quality="good"),
            periodic_of=lambda _p: None,
        )
        self.dialogs.append(dialog)
        return dialog

    def checked(self, dialog: PointDialog) -> set[int]:
        return {bit for bit, box in dialog._bit_boxes.items() if box.isChecked()}

    def test_boxes_start_from_the_live_value(self) -> None:
        dialog = self.dialog()
        self.assertEqual(self.checked(dialog), {0, 2}, "当前值 bit0/bit2 置位，勾选要跟上")

    def test_toggling_marks_pending_and_updates_status(self) -> None:
        dialog = self.dialog()
        dialog._bit_boxes[1].setChecked(True)
        self.assertTrue(dialog._bits_dirty)
        self.assertEqual(self.checked(dialog), {0, 1, 2})
        self.assertEqual(dialog._bits_value(), 0b111)
        self.assertIn("待置位", dialog.bit_label.text())
        self.assertIn("当前 5", dialog.bit_preview.text())
        self.assertIn("将要写入 7", dialog.bit_preview.text())

        dialog._bit_boxes[0].setChecked(False)
        self.assertIn("待清零", dialog.bit_label.text())
        self.assertEqual(dialog._bits_value(), 0b110)

    def test_live_value_follows_while_user_has_not_touched(self) -> None:
        dialog = self.dialog()
        self.values["value"] = 0b0010
        dialog.refresh()
        self.assertEqual(self.checked(dialog), {1}, "没动过勾选时跟着设备值走")

    def test_pending_edits_are_not_overwritten(self) -> None:
        dialog = self.dialog()
        dialog._bit_boxes[1].setChecked(True)  # 目标 0b111
        self.values["value"] = 0b0000  # 设备回了另一个值
        dialog.refresh()
        self.assertEqual(self.checked(dialog), {0, 1, 2}, "用户的改动不能被刷新覆盖")
        self.assertTrue(dialog._bits_dirty)

    def test_readback_of_target_clears_pending(self) -> None:
        dialog = self.dialog()
        dialog._bit_boxes[1].setChecked(True)  # 目标 0b111
        self.values["value"] = 0b111  # 设备已回读
        dialog.refresh()
        self.assertFalse(dialog._bits_dirty, "回读到目标值就不算未写入的改动了")
        self.assertNotIn("待置位", dialog.bit_label.text())

    def test_reset_bits_restores_live_value(self) -> None:
        dialog = self.dialog()
        dialog._bit_boxes[1].setChecked(True)
        dialog.reset_bits()
        self.assertFalse(dialog._bits_dirty)
        self.assertEqual(self.checked(dialog), {0, 2})
        self.assertIn("与当前值一致", dialog.bit_preview.text())

    def test_apply_sends_the_composed_value(self) -> None:
        dialog = self.dialog()
        dialog._bit_boxes[1].setChecked(True)
        sent: list[tuple[str, object]] = []
        dialog.writeRequested.connect(lambda pid, value: sent.append((pid, value)))
        dialog._apply_bits()
        self.assertEqual(sent, [(FUNC, 0b111)])

    def test_readonly_status_word_has_no_boxes(self) -> None:
        self.values["value"] = 0x30
        dialog = self.dialog(STATUS)
        self.assertEqual(dialog._bit_boxes, {})
        self.assertIn("bit4", dialog.bit_label.text())
        self.assertIn("bit5", dialog.bit_label.text())


if __name__ == "__main__":
    unittest.main()
