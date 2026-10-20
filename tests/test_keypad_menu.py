"""「控制台 → 按键控制台」：主窗口右侧的停靠面板。

现场反馈：以前"菜单里悬浮展开"和"点菜单项开置顶小窗"两种做法都会卡住界面
（菜单还开着时弹窗/抢焦点）。现在只有一个开关，面板是主窗口的一部分。

这里盯住：

* 菜单里是**勾选项**，勾上出面板、取消就收起；状态记进设置；
* 面板里是设备下拉 + 常用按键（按使用次数排）+ 完整按键面板；
* 点按键只走写值出口，**未启动采集时给通知、不弹模态框**（模态框正是卡死的来源）；
* 换主题 / 换工程后面板会重建且仍然可用。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core import settings as app_settings  # noqa: E402
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


class KeypadDockTest(unittest.TestCase):
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
        self.addCleanup(control_panel.close_all)

    def _cleanup(self) -> None:
        if self.window.engine is not None:
            self.window.engine.stop()
        if self.window.storage is not None:
            self.window.storage.stop()
        self.window.settings.confirm_on_exit = False
        self.window.close()

    # -------------------------------------------------- 开关

    def test_console_menu_has_a_checkable_entry(self) -> None:
        menu = self.window.console_menu
        entries = [action.text() for action in menu.actions() if action.text()]
        self.assertIn("按键控制台", entries)
        self.assertTrue(self.window.action_control.isCheckable())
        self.assertFalse(self.window.keypad_dock_visible(), "默认不占地方")

    def test_toggle_shows_and_hides_the_dock(self) -> None:
        self.window.action_control.setChecked(True)
        self.assertTrue(self.window.keypad_dock_visible())
        self.assertEqual(
            self.window.keypad_panel.device_names(), ["储能变流器PCS"]
        )
        self.window.action_control.setChecked(False)
        self.assertFalse(self.window.keypad_dock_visible())

    def test_visibility_is_remembered(self) -> None:
        self.window.action_control.setChecked(True)
        self.assertTrue(self.window.settings.keypad_dock_visible)
        self.window.action_control.setChecked(False)
        self.assertFalse(self.window.settings.keypad_dock_visible)

    def test_no_hover_panel_leftovers(self) -> None:
        """旧的"菜单里悬浮展开"机制彻底拆掉了，别再留半套。"""
        for gone in ("keypad_flyout", "keypad_menu", "keypad_holder"):
            self.assertFalse(hasattr(self.window, gone), f"旧机制还在：{gone}")
        for gone in ("show_keypad_flyout", "_on_console_item_hovered", "_watch_keypad_hover"):
            self.assertFalse(hasattr(self.window, gone), f"旧方法还在：{gone}")

    # -------------------------------------------------- 面板内容

    def test_panel_lists_command_keys_from_parameter_table(self) -> None:
        self.window.show_keypad_dock()
        labels = [button.text() for button in self.window.keypad_panel.keys()]
        self.assertEqual(labels, ["开机", "关机", "充电", "放电", "复位"])

    def test_no_frequent_row_before_any_use(self) -> None:
        self.window.show_keypad_dock()
        self.assertEqual(self.window.keypad_panel.frequent_buttons(), [])
        self.assertFalse(self.window.keypad_panel.frequent_caption.isVisible())

    def test_frequent_keys_follow_usage(self) -> None:
        keys = control_panel.all_keys(self.project)
        first, second = keys[0], keys[-1]
        for device, pid, kind, value, _label in (first, second):
            ident = control_panel.key_id(device, pid, kind, value)
            self.assertNotIn(ident, self.window._key_usage)
        device, pid, kind, value, _label = second
        self.window._note_key_used(pid, kind, value)
        self.window._note_key_used(pid, kind, value)
        device, pid, kind, value, first_label = first
        self.window._note_key_used(pid, kind, value)
        self.window.show_keypad_dock()
        captions = [button.text() for button in self.window.keypad_panel.frequent_buttons()]
        self.assertEqual(
            captions,
            [second[4].split(" · ")[-1], first_label.split(" · ")[-1]],
            "按得多的排前面",
        )

    # -------------------------------------------------- 按键真的写值

    def test_press_writes_and_counts(self) -> None:
        self.window.show_keypad_dock()
        start = [b for b in self.window.keypad_panel.keys() if b.text() == "开机"][0]
        engine = mock.Mock()
        engine.running = True
        engine.write.return_value = (True, "")
        self.window.engine = engine
        with mock.patch("threading.Thread") as thread_cls:
            start.click()
        thread_cls.assert_called_once()
        pid, value = thread_cls.call_args.kwargs["args"]
        self.assertEqual(pid, "储能变流器PCS.控制字")
        self.assertEqual(value, 1)
        ident = control_panel.key_id("储能变流器PCS", pid, "v", value)
        self.assertEqual(self.window._key_usage[ident], 1)
        self.assertEqual(self.window.settings.key_usage[ident], 1, "用量要写进设置对象")

    def test_press_without_engine_notifies_instead_of_modal(self) -> None:
        """回归：未启动采集时不能弹模态框（菜单/面板还开着时弹框会把界面卡死）。"""
        self.window.show_keypad_dock()
        with mock.patch.object(QMessageBox, "information") as info, \
                mock.patch.object(self.window, "notify") as notify:
            self.window.keypad_panel.keys()[0].click()
        self.assertFalse(info.called, "不该弹模态框")
        self.assertTrue(notify.called)
        self.assertEqual(notify.call_args[0][1], "请先启动采集")

    def test_bit_key_toggles_the_bit(self) -> None:
        project = two_device_project()
        device = project.device("甲柜")
        device.points.append(
            PointDef(
                name="mask",
                label="掩码",
                register="holding",
                address=1,
                data_type="uint16",
                writable=True,
                bits="0:甲位,1:乙位",
            )
        )
        paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "proj2"), project
        )
        window = MainWindow(project, paths, workdir=self.tmp.name, base_dir=self.tmp.name)
        window.settings.confirm_on_exit = False
        self.addCleanup(window.close)

        pid = "甲柜.掩码"
        window.keypad_panel._value_of = lambda _pid: mock.Mock(value=1)  # 当前 bit0 置位
        with mock.patch.object(window, "_on_write_requested") as write:
            window.keypad_panel.press(pid, "b", 1)
        write.assert_called_once_with(pid, 1 | (1 << 1))

    # -------------------------------------------------- 重建

    def test_theme_change_rebuilds_the_panel(self) -> None:
        self.window.show_keypad_dock()
        before = self.window.keypad_panel
        self.window._refresh_appearance()
        self.assertIsNot(self.window.keypad_panel, before)
        self.assertTrue(self.window.keypad_dock_visible())
        self.assertEqual(
            [b.text() for b in self.window.keypad_panel.keys()],
            ["开机", "关机", "充电", "放电", "复位"],
        )

    def test_project_swap_rebuilds_the_panel(self) -> None:
        self.window.show_keypad_dock()
        other = two_device_project()
        other_paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "proj3"), other
        )
        self.window._swap_project(other, other_paths)
        self.assertEqual(self.window.keypad_panel.device_names(), ["甲柜", "乙柜"])


class KeypadFlyoutWidgetTest(unittest.TestCase):
    """面板里那块小面板本身（设备下拉 + 按键）。"""

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
        self.assertEqual(
            [b.text() for b in self.flyout.keys()],
            ["开机", "关机", "充电", "放电", "复位"],
        )

    def test_panel_timer_stops_when_hidden(self) -> None:
        self.flyout.show()
        self.assertTrue(self.flyout.is_refreshing())
        self.flyout.hide()
        self.assertFalse(self.flyout.is_refreshing(), "面板收起来就不该再刷值")


class KeyUsageSettingsTest(unittest.TestCase):
    def test_counts_survive_save_and_load(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        settings = app_settings.AppSettings()
        settings.key_usage = {"甲柜|甲柜.cmd|v:1": 3}
        settings.keypad_dock_visible = True
        app_settings.save_settings(settings, tmp.name)
        loaded = app_settings.load_settings(tmp.name)
        self.assertEqual(loaded.key_usage["甲柜|甲柜.cmd|v:1"], 3)
        self.assertTrue(loaded.keypad_dock_visible)

    def test_bad_values_are_dropped(self) -> None:
        settings = app_settings.AppSettings(
            key_usage={"x": -5, "y": "abc", "": 2, "z": 2}
        )
        settings.clamp()
        self.assertEqual(settings.key_usage, {"z": 2})

    def test_list_like_usage_is_reset(self) -> None:
        settings = app_settings.AppSettings(key_usage=["a", "b"])  # type: ignore[arg-type]
        settings.clamp()
        self.assertEqual(settings.key_usage, {})


if __name__ == "__main__":
    unittest.main()
