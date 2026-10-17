"""运行监视：拖动分组边框调整位置（含并排：状态量在左、控制量在右）。

用户要求："让画面分组可以拖动边框来调整位置，比如想拖动状态量在左控制量在右"。

这里盯住：
* 按住标题/边框才算拖整组（按在卡片中间不算）；
* 落点在目标分组的左/右是**并排**（一行最多两个），上/下是单独占一行；
* 一行满了把被挤出来的那个挪到下一行，不丢分组；
* 摆法记进设置、写盘、重启后还原；工程里没有的分组名会被清掉。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QGroupBox  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store, settings as app_settings  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.ui.main_window import MainWindow  # noqa: E402
from icomm.ui.pages.dashboard import (  # noqa: E402
    COLUMNS,
    GROUP_MIME,
    DashboardPage,
    _GroupBoard,
    _GroupDragFilter,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class GroupLayoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = project_io.bess_project()
        self.rows: list[list[str]] = []
        self.page = DashboardPage(self.project, group_rows=self.rows)
        self.page.resize(1400, 900)
        self.page.show()
        self.addCleanup(self.page.deleteLater)

    def _names(self) -> list[str]:
        return list(self.page._groups)

    def test_default_is_one_group_per_row(self) -> None:
        rows = self.page.group_rows()
        self.assertEqual(rows, [[name] for name in self._names()])

    def test_drop_on_right_makes_them_side_by_side(self) -> None:
        first, second = self._names()[:2]
        self.assertTrue(self.page.move_group(second, first, "right"))
        self.assertEqual(self.page.group_rows()[:1], [[first, second]])
        # 并排时组内列数减半，卡片才不会被挤扁
        self.assertEqual(self.page._group_columns[first], COLUMNS // 2)
        self.assertEqual(self.page._group_columns[second], COLUMNS // 2)

    def test_drop_on_left_puts_the_dragged_one_first(self) -> None:
        """「状态量在左、控制量在右」就是把状态量拖到控制量左半边。"""
        first, second = self._names()[:2]
        self.assertTrue(self.page.move_group(second, first, "left"))
        self.assertEqual(self.page.group_rows()[:1], [[second, first]])

    def test_drop_above_and_below(self) -> None:
        first, second, third = self._names()[:3]
        self.page.move_group(second, first, "right")  # 先并排
        self.assertTrue(self.page.move_group(third, first, "above"))
        rows = self.page.group_rows()
        self.assertEqual(rows[0], [third])
        self.assertEqual(rows[1], [first, second])

        self.assertTrue(self.page.move_group(third, second, "below"))
        rows = self.page.group_rows()
        index = next(i for i, row in enumerate(rows) if second in row)
        self.assertEqual(rows[index + 1], [third], "拖到某组「下方」是紧跟它，不是挪到最末")

    def test_third_group_pushes_overflow_to_next_row(self) -> None:
        first, second, third = self._names()[:3]
        self.page.move_group(second, first, "right")
        self.assertTrue(self.page.move_group(third, first, "left"))
        rows = self.page.group_rows()
        self.assertEqual(len(rows[0]), 2, "一行最多两个")
        flat = [name for row in rows for name in row]
        self.assertEqual(sorted(flat), sorted(self._names()), "分组一个都不能丢")

    def test_solo_row_restores_full_width_columns(self) -> None:
        first, second = self._names()[:2]
        self.page.move_group(second, first, "right")
        self.page.move_group(second, first, "below")
        self.assertEqual(self.page._group_columns[first], COLUMNS)

    def test_self_and_unknown_are_ignored(self) -> None:
        first = self._names()[0]
        before = self.page.group_rows()
        self.assertFalse(self.page.move_group(first, first, "left"))
        self.assertFalse(self.page.move_group("不存在的分组", first, "left"))
        self.assertFalse(self.page.move_group(first, "不存在的分组", "right"))
        self.assertEqual(self.page.group_rows(), before)

    def test_change_is_signalled(self) -> None:
        seen: list[list[list[str]]] = []
        self.page.groupLayoutChanged.connect(lambda rows: seen.append(rows))
        first, second = self._names()[:2]
        self.page.move_group(second, first, "right")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], [first, second])

    def test_stale_and_missing_groups_are_normalised(self) -> None:
        rows = [["已经删掉的分组"], ["直流侧"], ["直流侧", "不存在"]]
        page = DashboardPage(self.project, group_rows=rows)
        self.addCleanup(page.deleteLater)
        got = page.group_rows()
        self.assertNotIn("已经删掉的分组", [n for row in got for n in row])
        flat = [n for row in got for n in row]
        self.assertEqual(sorted(flat), sorted(page._groups), "现有分组都要在，且不重复")


class GroupDragTest(unittest.TestCase):
    def setUp(self) -> None:
        self.page = DashboardPage(project_io.bess_project())
        self.page.resize(1400, 900)
        self.page.show()
        self.addCleanup(self.page.deleteLater)
        self.board = self.page.container
        self.first = self.page._groups[list(self.page._groups)[0]]

    def test_board_accepts_our_mime_only(self) -> None:
        mime = QMimeData()
        mime.setData(GROUP_MIME, "直流侧".encode("utf-8"))
        self.assertEqual(_GroupBoard._group_name(mime), "直流侧")
        other = QMimeData()
        other.setText("x")
        self.assertEqual(_GroupBoard._group_name(other), "")
        self.assertEqual(_GroupBoard._group_name(None), "")

    def test_zone_uses_left_right_edges(self) -> None:
        box = self.first
        width, height = box.width(), box.height()
        self.assertTrue(self.board.isVisible())
        self.assertEqual(
            _GroupBoard.zone_for(box, box.pos() + QPoint(2, height // 2)), "left"
        )
        self.assertEqual(
            _GroupBoard.zone_for(box, box.pos() + QPoint(width - 2, height // 2)), "right"
        )
        self.assertEqual(
            _GroupBoard.zone_for(box, box.pos() + QPoint(width // 2, 1)), "above"
        )
        self.assertEqual(
            _GroupBoard.zone_for(box, box.pos() + QPoint(width // 2, height - 2)), "below"
        )

    def test_handle_is_frame_or_title(self) -> None:
        box = self.first
        self.assertTrue(_GroupDragFilter.is_handle(box, QPoint(2, box.height() // 2)))
        self.assertTrue(_GroupDragFilter.is_handle(box, QPoint(box.width() // 2, 4)))
        self.assertTrue(
            _GroupDragFilter.is_handle(box, QPoint(box.width() - 2, box.height() // 2))
        )
        self.assertFalse(
            _GroupDragFilter.is_handle(box, QPoint(box.width() // 2, box.height() // 2)),
            "按在卡片中间不该当成拖分组（那是点卡片）",
        )

    def test_every_group_has_a_drag_filter(self) -> None:
        for box in self.page._groups.values():
            self.assertEqual(
                len(box.findChildren(_GroupDragFilter)), 1, f"{box.title()} 没装拖动过滤器"
            )

    def test_board_finds_group_under_pointer(self) -> None:
        box = self.first
        centre = box.geometry().center()
        found = self.board.group_box_at(centre)
        self.assertIsInstance(found, QGroupBox)


class GroupRowsPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()
        self.paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "工程"), self.project
        )
        self.window = self._window()
        self.addCleanup(self._cleanup)

    def _window(self) -> MainWindow:
        window = MainWindow(
            self.project, self.paths, workdir=self.tmp.name, base_dir=self.tmp.name
        )
        window.settings.confirm_on_exit = False
        self.addCleanup(window.close)
        return window

    def _cleanup(self) -> None:
        if self.window.engine is not None:
            self.window.engine.stop()
        if self.window.storage is not None:
            self.window.storage.stop()
        self.window.settings.confirm_on_exit = False
        self.window.close()

    def test_layout_is_written_to_settings_file(self) -> None:
        page = self.window.dashboard_page
        first, second = list(page._groups)[:2]
        page.move_group(second, first, "right")
        saved = app_settings.load_settings(self.tmp.name).display_group_rows
        self.assertEqual(saved, [[first, second]] + [[n] for n in list(page._groups)[2:]])

    def test_layout_is_restored_next_time(self) -> None:
        page = self.window.dashboard_page
        first, second = list(page._groups)[:2]
        page.move_group(second, first, "right")
        saved = page.group_rows()

        again = self._window()
        self.assertEqual(again.dashboard_page.group_rows(), saved)
        again.close()

    def test_settings_sanitises_group_rows(self) -> None:
        settings = AppSettings.from_dict(
            {
                "display_group_rows": [
                    ["a", "b"],
                    ["a", "c"],          # 重复的 a 要去掉
                    ["x", "y", "z"],     # 一行最多两个
                    "不是列表",
                    [None, 5, "ok"],
                ]
            }
        )
        rows = settings.display_group_rows
        self.assertEqual(rows[0], ["a", "b"])
        self.assertEqual(rows[1], ["c"])
        self.assertEqual(rows[2], ["x", "y"])
        self.assertEqual(rows[3], ["ok"])
        self.assertEqual(AppSettings.from_dict({"display_group_rows": "x"}).display_group_rows, [])


if __name__ == "__main__":
    unittest.main()
