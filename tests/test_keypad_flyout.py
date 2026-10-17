"""「控制台 → 按键控制台」：鼠标停上去就在菜单里展开按键面板。

用户要求："按键控制台也变成像工具栏那种鼠标悬浮产生下级内容"。
这里盯住四件事：面板确实是菜单里的一块（不是另一个浮窗）、展开后按键就是参数表里的命令、
点按键真的走写值出口、鼠标移开/关菜单会收起并停掉刷新定时器。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QPushButton, QWidgetAction  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core.models import DeviceDef, PointDef, Project  # noqa: E402
from icomm.ui.dialogs import control_panel  # noqa: E402
from icomm.ui.main_window import MainWindow  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def two_device_project() -> Project:
    """两台设备各有命令点位：用来验证"换设备"能换掉按键。"""
    project = Project(name="双设备")
    project.channels.append(project_io.bess_project().channels[0])
    channel = project.channels[0].name
    project.devices.append(
        DeviceDef(
            name="甲柜",
            channel=channel,
            unit_id=1,
            points=[
                PointDef(
                    name="cmd",
                    label="甲柜命令字",
                    register="holding",
                    address=0,
                    data_type="uint16",
                    writable=True,
                    commands="1:甲开,2:甲停",
                )
            ],
        )
    )
    project.devices.append(
        DeviceDef(
            name="乙柜",
            channel=channel,
            unit_id=2,
            points=[
                PointDef(
                    name="cmd",
                    label="乙柜命令字",
                    register="holding",
                    address=0,
                    data_type="uint16",
                    writable=True,
                    commands="4:乙启动,8:乙停止",
                )
            ],
        )
    )
    return project


class KeypadFlyoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()
        self.paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "proj"), self.project
        )
        self.window = MainWindow(
            self.project, self.paths, workdir=self.tmp.name, base_dir=self.tmp.name
        )
        self.window.settings.confirm_on_exit = False
        self.addCleanup(self._cleanup)
        # 不用真的挪鼠标：判断"在不在菜单上"走这个
        self.window._cursor_pos = lambda: self.window.console_menu.mapToGlobal(QPoint(-50, -50))

    def _cleanup(self) -> None:
        if self.window.engine is not None:
            self.window.engine.stop()
        if self.window.storage is not None:
            self.window.storage.stop()
        self.window.settings.confirm_on_exit = False
        self.window.close()

    # -------------------------------------------------- 菜单结构

    def test_panel_lives_inside_console_menu(self) -> None:
        """面板是「控制台」菜单里的一块（QWidgetAction），不是单独的浮窗。"""
        menu = self.window.console_menu
        texts = [action.text() for action in menu.actions() if not action.isSeparator()]
        self.assertIn("按键控制台", texts, texts)
        holders = [a for a in menu.actions() if isinstance(a, QWidgetAction)]
        self.assertEqual(len(holders), 1, "菜单里应当只有按键面板这一个控件项")
        self.assertIs(holders[0].defaultWidget(), self.window.keypad_flyout)
        self.assertFalse(holders[0].isVisible(), "没悬浮时不占菜单的位置")

    def test_hover_expands_and_rehover_keeps_device(self) -> None:
        menu = self.window.console_menu
        menu.popup(QPoint(100, 100))
        self.addCleanup(menu.close)
        flyout = self.window.keypad_flyout

        self.window._on_console_item_hovered(self.window.action_control)
        self.assertTrue(flyout.isVisible(), "悬浮「按键控制台」后面板要展开")
        self.assertEqual(flyout.current_device(), "储能变流器PCS")

        flyout.set_device("储能变流器PCS")
        self.window._on_console_item_hovered(self.window.keypad_holder)
        self.assertTrue(flyout.isVisible(), "鼠标移到面板本身上不该收起来")
        self.assertTrue(self.window.keypad_holder.isVisible())

    def test_keys_come_from_the_parameter_table(self) -> None:
        menu = self.window.console_menu
        menu.popup(QPoint(100, 100))
        self.addCleanup(menu.close)
        self.window.show_keypad_flyout()
        labels = [button.text() for button in self.window.keypad_flyout.keys()]
        self.assertEqual(labels, ["开机", "关机", "充电", "放电", "复位"])

    def test_hover_other_item_hides_panel(self) -> None:
        menu = self.window.console_menu
        menu.popup(QPoint(100, 100))
        self.addCleanup(menu.close)
        self.window.show_keypad_flyout()
        self.assertTrue(self.window.keypad_flyout_visible())

        self.window._on_console_item_hovered(self.window.action_periodic)
        self.assertFalse(self.window.keypad_flyout_visible(), "停到别的菜单项上要收起")
        self.assertFalse(self.window.keypad_flyout.is_refreshing(), "收起后刷新定时器要停")

    def test_leaving_the_menu_hides_panel(self) -> None:
        menu = self.window.console_menu
        menu.popup(QPoint(100, 100))
        self.addCleanup(menu.close)
        self.window.show_keypad_flyout()
        # 光标在菜单外（setUp 里给的坐标是负的）
        self.window._watch_keypad_hover()
        self.assertFalse(self.window.keypad_flyout_visible())

        # 光标在菜单内则保持展开
        self.window._cursor_pos = lambda: menu.mapToGlobal(menu.rect().center())
        self.window.show_keypad_flyout()
        self.window._watch_keypad_hover()
        self.assertTrue(self.window.keypad_flyout_visible())

    def test_closing_menu_stops_everything(self) -> None:
        menu = self.window.console_menu
        menu.popup(QPoint(100, 100))
        self.window.show_keypad_flyout()
        self.assertTrue(self.window._keypad_hover_timer.isActive())
        menu.close()
        self.assertFalse(self.window.keypad_flyout_visible())
        self.assertFalse(self.window._keypad_hover_timer.isActive())

    # -------------------------------------------------- 按键真的写值

    def test_click_sends_write_request(self) -> None:
        menu = self.window.console_menu
        menu.popup(QPoint(100, 100))
        self.addCleanup(menu.close)
        self.window.show_keypad_flyout()
        keys = self.window.keypad_flyout.keys()
        start = [button for button in keys if button.text() == "开机"][0]

        with mock.patch.object(QMessageBox, "information") as info:
            start.click()
        self.assertTrue(info.called, "没启动采集时应当提示先启动")
        args = info.call_args[0]
        self.assertEqual(args[1], "提示")
        self.assertIn("启动采集", args[2])

    def test_write_request_reaches_engine_when_running(self) -> None:
        menu = self.window.console_menu
        menu.popup(QPoint(100, 100))
        self.addCleanup(menu.close)
        self.window.show_keypad_flyout()
        start = [b for b in self.window.keypad_flyout.keys() if b.text() == "开机"][0]

        engine = mock.Mock()
        engine.running = True
        engine.write.return_value = (True, "")
        self.window.engine = engine
        with mock.patch("threading.Thread") as thread_cls:
            start.click()
        thread_cls.assert_called_once()
        self.assertEqual(thread_cls.call_args.kwargs["args"][0], "储能变流器PCS.控制字")
        self.assertEqual(thread_cls.call_args.kwargs["args"][1], 1)

    def test_float_button_opens_independent_panel(self) -> None:
        menu = self.window.console_menu
        menu.popup(QPoint(100, 100))
        self.addCleanup(menu.close)
        self.addCleanup(control_panel.close_all)
        self.window.show_keypad_flyout()
        self.window.keypad_flyout._float_current()
        panel = control_panel._open_panels.get("device::储能变流器PCS")
        self.assertIsNotNone(panel, "「独立浮窗」要真的开出一个浮动面板")


class KeypadFlyoutSwitchDeviceTest(unittest.TestCase):
    """换设备：面板里的按键要跟着换（不能还留着上一台设备的键）。"""

    def setUp(self) -> None:
        self.project = two_device_project()
        self.flyout = control_panel.KeypadFlyout(
            self.project,
            value_of=lambda pid: mock.Mock(value=0, quality="good", error=""),
            periodic_of=lambda pid: None,
        )
        self.addCleanup(self.flyout.deleteLater)

    def test_device_list_only_has_devices_with_keys(self) -> None:
        self.assertEqual(self.flyout.device_names(), ["甲柜", "乙柜"])
        self.assertEqual(self.flyout.current_device(), "甲柜")

    def test_switching_device_rebuilds_keys(self) -> None:
        self.flyout.show()
        self.addCleanup(self.flyout.hide)
        self.assertEqual([b.text() for b in self.flyout.keys()], ["甲开", "甲停"])
        self.flyout.set_device("乙柜")
        self.assertEqual([b.text() for b in self.flyout.keys()], ["乙启动", "乙停止"])
        self.assertEqual(self.flyout.device_combo.currentText(), "乙柜")

    def test_no_controllable_points_shows_hint(self) -> None:
        empty = Project(name="空工程")
        empty.channels.append(project_io.bess_project().channels[0])
        empty.devices.append(
            DeviceDef(name="只读设备", channel=empty.channels[0].name, unit_id=1, points=[])
        )
        flyout = control_panel.KeypadFlyout(
            empty, value_of=lambda pid: None, periodic_of=lambda pid: None
        )
        self.addCleanup(flyout.deleteLater)
        self.assertEqual(flyout.device_names(), [])
        self.assertTrue(flyout.note.isVisibleTo(flyout), "没有可控点位时要给提示，别给空白")
        self.assertFalse(flyout.device_combo.isEnabled())

    def test_project_switch_drops_old_points(self) -> None:
        self.flyout.show()
        self.addCleanup(self.flyout.hide)
        self.assertEqual(self.flyout.current_device(), "甲柜")
        other = project_io.bess_project()
        self.flyout.set_project(other)
        self.assertEqual(self.flyout.current_device(), "储能变流器PCS")
        self.assertEqual([b.text() for b in self.flyout.keys()], ["开机", "关机", "充电", "放电", "复位"])

    def test_panel_timer_stops_when_hidden(self) -> None:
        self.flyout.show()
        self.assertTrue(self.flyout.is_refreshing())
        self.flyout.hide()
        self.assertFalse(self.flyout.is_refreshing(), "面板收起来就不该再刷值")


class KeypadFlyoutWiringTest(unittest.TestCase):
    """Project 换掉后，菜单里的面板也要跟着换工程。"""

    def test_swap_project_updates_flyout(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        project = two_device_project()
        paths = project_store.create_project_dir(os.path.join(tmp.name, "proj"), project)
        window = MainWindow(project, paths, workdir=tmp.name, base_dir=tmp.name)
        window.settings.confirm_on_exit = False
        self.addCleanup(window.close)
        self.assertEqual(window.keypad_flyout.device_names(), ["甲柜", "乙柜"])

        other = project_io.bess_project()
        other_paths = project_store.create_project_dir(os.path.join(tmp.name, "proj2"), other)
        window._swap_project(other, other_paths)
        self.assertEqual(window.keypad_flyout.device_names(), ["储能变流器PCS"])


if __name__ == "__main__":
    unittest.main()
