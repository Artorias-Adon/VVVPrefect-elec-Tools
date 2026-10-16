"""运行监视：同分组内用鼠标中键拖动显示块调整顺序。

用户要求"运行监视里的画面显示块可以在同分组里用鼠标中键拖动"。
这里盯住：只在同组内生效、顺序记进软件设置、关掉程序再开还在、
以及不能把右键菜单挤坏。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store, settings as app_settings  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.ui.pages.dashboard import DashboardPage  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _mouse(kind, widget, button, buttons, pos=None) -> QMouseEvent:
    local = widget.rect().center() if pos is None else pos
    glob = widget.mapToGlobal(local)
    return QMouseEvent(kind, QPointF(local), QPointF(glob), button, buttons, Qt.NoModifier)


class DashboardOrderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = project_io.bess_project()
        self.order: dict[str, list[str]] = {}
        self.page = DashboardPage(self.project, order=self.order)
        self.page.resize(1200, 800)
        self.page.show()
        self.addCleanup(self.page.deleteLater)

    def _group_with_two(self) -> str:
        for name, pids in self.page._group_entries.items():
            if len(pids) >= 2:
                return name
        self.fail("示例工程里应该有含两个以上点位的分组")

    # ------------------------------------------------------------ 逻辑

    def test_move_within_group(self) -> None:
        group = self._group_with_two()
        pids = self.page.group_order(group)
        self.assertTrue(self.page.move_pid(pids[1], pids[0]))
        self.assertEqual(self.page.group_order(group)[:2], [pids[1], pids[0]])

    def test_move_to_last_position(self) -> None:
        group = self._group_with_two()
        pids = self.page.group_order(group)
        self.assertTrue(self.page.move_pid(pids[0], pids[-1]))
        self.assertEqual(self.page.group_order(group)[-1], pids[0])
        self.assertEqual(len(self.page.group_order(group)), len(pids))

    def test_cross_group_is_rejected(self) -> None:
        """只能在自己那一组里挪 —— 拖到别的分组不算数。"""
        names = list(self.page._group_entries)
        first, second = names[0], names[1]
        before = self.page.group_order(first)
        self.assertFalse(
            self.page.move_pid(
                self.page.group_order(first)[0], self.page.group_order(second)[0]
            )
        )
        self.assertEqual(self.page.group_order(first), before)

    def test_same_pid_is_noop(self) -> None:
        group = self._group_with_two()
        pid = self.page.group_order(group)[0]
        self.assertFalse(self.page.move_pid(pid, pid))

    def test_unknown_pids_are_ignored(self) -> None:
        self.assertFalse(self.page.move_pid("不存在.点位", "也不存在.点位"))

    def test_order_recorded_in_store(self) -> None:
        group = self._group_with_two()
        pids = self.page.group_order(group)
        self.page.move_pid(pids[1], pids[0])
        self.assertEqual(self.order[group], self.page.group_order(group))

    def test_signal_fires(self) -> None:
        seen: list[tuple] = []
        self.page.orderChanged.connect(lambda name, pids: seen.append((name, pids)))
        group = self._group_with_two()
        pids = self.page.group_order(group)
        self.page.move_pid(pids[1], pids[0])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], group)
        self.assertEqual(seen[0][1], self.page.group_order(group))

    # ------------------------------------------------------------ 持久化

    def test_saved_order_is_used_next_time(self) -> None:
        """拖过的顺序要留下来（存在软件设置里）。"""
        group = self._group_with_two()
        pids = self.page.group_order(group)
        self.page.move_pid(pids[1], pids[0])
        expected = self.page.group_order(group)

        fresh = DashboardPage(self.project, order=dict(self.order))
        self.addCleanup(fresh.deleteLater)
        self.assertEqual(fresh.group_order(group), expected)

    def test_order_with_removed_points_is_safe(self) -> None:
        """工程里删过点位以后，老的顺序也得能用（多余的自动忽略）。"""
        group = self._group_with_two()
        pids = self.page.group_order(group)
        stale = {group: ["早就删了的.点位", pids[1]]}
        fresh = DashboardPage(self.project, order=stale)
        self.addCleanup(fresh.deleteLater)
        self.assertEqual(fresh.group_order(group)[0], pids[1])
        self.assertEqual(len(fresh.group_order(group)), len(pids))

    def test_reset_order_restores_project_order(self) -> None:
        group = self._group_with_two()
        pids = self.page.group_order(group)
        self.page.move_pid(pids[1], pids[0])
        self.page.reset_order()
        self.assertEqual(self.page.group_order(group), pids)

    # ------------------------------------------------------------ 鼠标

    def test_middle_drag_reorders(self) -> None:
        """真的发中键事件走一遍：按下 -> 移到目标 -> 松开。"""
        group = self._group_with_two()
        pids = self.page.group_order(group)
        source = self.page._items[pids[0]]
        target = self.page._items[pids[1]]

        self.assertTrue(source.rect().contains(source.rect().center()))
        _app.sendEvent(source, _mouse(QEvent.MouseButtonPress, source,
                                     Qt.MiddleButton, Qt.MiddleButton))
        moved = _mouse(QEvent.MouseMove, target, Qt.NoButton, Qt.MiddleButton)
        moved.setTimestamp(1)
        _app.sendEvent(target, moved)
        _app.sendEvent(target, _mouse(QEvent.MouseButtonRelease, target,
                                      Qt.MiddleButton, Qt.NoButton))

        self.assertEqual(self.page.group_order(group)[1], pids[0])

    def test_middle_press_marks_drag(self) -> None:
        group = self._group_with_two()
        pid = self.page.group_order(group)[0]
        source = self.page._items[pid]
        _app.sendEvent(source, _mouse(QEvent.MouseButtonPress, source,
                                     Qt.MiddleButton, Qt.MiddleButton))
        self.assertEqual(self.page._drag_pid, pid)
        _app.sendEvent(source, _mouse(QEvent.MouseButtonRelease, source,
                                      Qt.MiddleButton, Qt.NoButton))
        self.assertIsNone(self.page._drag_pid)

    def test_left_press_is_left_alone(self) -> None:
        """左键（双击看参数）不能被拖动拦掉。"""
        group = self._group_with_two()
        pid = self.page.group_order(group)[0]
        source = self.page._items[pid]
        _app.sendEvent(source, _mouse(QEvent.MouseButtonPress, source,
                                      Qt.LeftButton, Qt.LeftButton))
        self.assertIsNone(self.page._drag_pid)

    def test_right_click_still_reaches_menu(self) -> None:
        """右键菜单不能被拖动拦掉。"""
        from PySide6.QtGui import QContextMenuEvent

        group = self._group_with_two()
        pid = self.page.group_order(group)[0]
        source = self.page._items[pid]
        seen: list[str] = []
        self.page.contextRequested.connect(lambda pid, _pos: seen.append(pid))
        _app.sendEvent(source, _mouse(QEvent.MouseButtonPress, source,
                                      Qt.RightButton, Qt.RightButton))
        self.assertIsNone(self.page._drag_pid)
        centre = source.rect().center()
        _app.sendEvent(
            source,
            QContextMenuEvent(
                QContextMenuEvent.Mouse,
                centre,
                source.mapToGlobal(centre),
            ),
        )
        self.assertEqual(seen, [pid])


class SettingsStorageTest(unittest.TestCase):
    def test_order_survives_settings_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            saved = AppSettings(display_order={"直流侧": ["a.b", "c.d"]})
            app_settings.save_settings(saved, base)
            self.assertEqual(
                app_settings.load_settings(base).display_order, {"直流侧": ["a.b", "c.d"]}
            )

    def test_broken_order_is_sanitised(self) -> None:
        settings = AppSettings.from_dict({"display_order": {"g": "不是列表"}})
        self.assertEqual(settings.display_order, {})
        settings = AppSettings.from_dict({"display_order": {"g": ["ok", 3]}})
        self.assertEqual(settings.display_order, {"g": ["ok"]})


class WindowWiringTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self) -> None:
        from icomm.ui.main_window import MainWindow

        self.base = tempfile.mkdtemp(prefix="dash-", dir=self.tmp.name)
        self.project = project_io.bess_project()
        paths = project_store.create_project_dir(
            os.path.join(self.base, "工程"), self.project
        )
        self.window = MainWindow(
            self.project, paths, workdir=self.base, base_dir=self.base
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

    def test_page_shares_settings_dict(self) -> None:
        """页面里拖的是设置里那本字典，不用额外抄一遍。"""
        self.assertIs(self.window.dashboard_page._order, self.window.settings.display_order)

    def test_order_is_written_to_settings_file(self) -> None:
        page = self.window.dashboard_page
        group = next(name for name, pids in page._group_entries.items() if len(pids) >= 2)
        pids = page.group_order(group)
        page.move_pid(pids[1], pids[0])
        self.assertEqual(
            app_settings.load_settings(self.base).display_order[group],
            page.group_order(group),
        )


if __name__ == "__main__":
    unittest.main()
