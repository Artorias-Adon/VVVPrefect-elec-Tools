"""「控制台 → 按键控制台 ▸」：子菜单 + 常用按键 + 全部可控按键窗口。

用户要求：「改成工具里那种带小箭头再点进去的版本」，子菜单里放
**几个常用的按键（按使用次数自动调整）**，再加一个「全部可控按键…」弹窗入口。

这里盯住：

* 菜单里是普通子菜单（有箭头），不再把控件塞进菜单、也没有悬停展开；
* 子菜单按使用次数排序，按过的键会浮到最前面；
* 菜单里按一下 = 记一次使用 + 走和面板相同的写值出口；
* 「全部可控按键…」只开一个非模态窗口，重复点是置顶；
* 用量存进设置，重启后还在。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox, QWidgetAction  # noqa: E402

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


class KeypadMenuTest(unittest.TestCase):
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

    def _labels(self) -> list[str]:
        return [action.text() for action in self.window.keypad_menu.actions() if action.text()]

    def _first_key_action(self):
        actions = [action for action in self.window.keypad_menu.actions() if action.text()]
        return actions[1]  # 第 0 个是「常用按键 / 先试这几个」小标题

    # -------------------------------------------------- 菜单结构

    def test_is_a_real_submenu_with_arrow(self) -> None:
        """「按键控制台」是带箭头的子菜单，不是塞进菜单里的控件。"""
        keypad_action = [
            action
            for action in self.window.console_menu.actions()
            if action.text() == "按键控制台"
        ]
        self.assertEqual(len(keypad_action), 1, "「控制台」里应有「按键控制台」")
        self.assertIs(keypad_action[0].menu(), self.window.keypad_menu)
        self.assertFalse(
            any(
                isinstance(action, QWidgetAction)
                for action in self.window.console_menu.actions()
            ),
            "菜单里不该再有 QWidgetAction（悬浮面板已移除）",
        )
        for gone in ("keypad_flyout", "show_keypad_flyout", "_on_console_item_hovered"):
            self.assertFalse(hasattr(self.window, gone), f"旧的悬停机制还在：{gone}")

    def test_lists_a_few_keys_and_the_all_keys_entry(self) -> None:
        self.window._fill_keypad_menu()
        labels = self._labels()
        self.assertEqual(labels[0], "先试这几个", "一次都没按过时给几个能点的")
        self.assertEqual(labels[-1], "全部可控按键…")
        keys = labels[1:-1]
        self.assertEqual(len(keys), 5, keys)
        self.assertTrue(all("储能变流器PCS · 控制字" in label for label in keys), keys)

    def test_usage_moves_the_key_to_the_top(self) -> None:
        keys = control_panel.all_keys(self.project)
        first, second = keys[0], keys[-1]
        for device, pid, kind, value, _label in (first, second):
            self.assertFalse(
                self.window._key_usage.get(control_panel.key_id(device, pid, kind, value))
            )
        device, pid, kind, value, _label = first
        self.window._key_usage[control_panel.key_id(device, pid, kind, value)] = 3
        device, pid, kind, value, second_label = second
        self.window._key_usage[control_panel.key_id(device, pid, kind, value)] = 9
        self.window._fill_keypad_menu()
        labels = self._labels()
        self.assertEqual(labels[0], "常用按键")
        self.assertEqual(labels[1], second_label, "按得最多的要排第一")
        self.assertEqual(labels[2], first[4], "按得少的排第二")

    # -------------------------------------------------- 按键真的写值

    def test_press_key_writes_and_counts(self) -> None:
        self.window._fill_keypad_menu()
        start = [
            action for action in self.window.keypad_menu.actions() if action.text().endswith("· 开机")
        ][0]

        engine = mock.Mock()
        engine.running = True
        engine.write.return_value = (True, "")
        self.window.engine = engine
        with mock.patch("threading.Thread") as thread_cls:
            start.trigger()

        thread_cls.assert_called_once()
        pid, value = thread_cls.call_args.kwargs["args"]
        self.assertEqual(pid, "储能变流器PCS.控制字")
        self.assertEqual(value, 1)
        ident = control_panel.key_id("储能变流器PCS", pid, "v", value)
        self.assertEqual(self.window._key_usage[ident], 1)
        self.assertEqual(self.window.settings.key_usage[ident], 1, "用量要写进设置对象")

    def test_press_without_engine_asks_to_start_first(self) -> None:
        self.window._fill_keypad_menu()
        first = self._first_key_action()
        with mock.patch.object(QMessageBox, "information") as info:
            first.trigger()
        self.assertTrue(info.called)
        self.assertEqual(info.call_args[0][1], "提示")

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
        window._tag_of = lambda _pid: mock.Mock(value=1)  # 当前 bit0 置位
        with mock.patch.object(window, "_on_write_requested") as write:
            window.press_key(pid, "b", 1)
        write.assert_called_once_with(pid, 1 | (1 << 1))

    # -------------------------------------------------- 全部可控按键窗口

    def test_all_keys_entry_opens_one_reusable_window(self) -> None:
        self.window.show_all_keys_dialog()
        dialog = self.window._all_keys_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.isVisible())
        self.assertEqual(dialog.windowTitle(), "全部可控按键")
        self.assertEqual(dialog.flyout.device_names(), ["储能变流器PCS"])

        self.window.show_all_keys_dialog()
        self.assertIs(self.window._all_keys_dialog, dialog, "重复点只置顶，不再开一个")

        dialog.close()
        self.window.show_all_keys_dialog()
        self.assertIsNot(self.window._all_keys_dialog, dialog, "关了以后再点要重新开")

    def test_all_keys_window_panel_keys_match_parameter_table(self) -> None:
        self.window.show_all_keys_dialog()
        labels = [button.text() for button in self.window._all_keys_dialog.flyout.keys()]
        self.assertEqual(labels, ["开机", "关机", "充电", "放电", "复位"])

    def test_panel_key_presses_are_counted_too(self) -> None:
        """面板里按的键也要计数，不然只有菜单里的键会变成"常用"。"""
        self.window.show_all_keys_dialog()
        self.window._note_key_used("储能变流器PCS.控制字", "v", 1)
        ident = control_panel.key_id("储能变流器PCS", "储能变流器PCS.控制字", "v", 1)
        self.assertEqual(self.window._key_usage[ident], 1)

    def test_swap_project_closes_the_all_keys_window(self) -> None:
        self.window.show_all_keys_dialog()
        dialog = self.window._all_keys_dialog
        other = project_io.bess_project()
        other_paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "proj3"), other
        )
        self.window._swap_project(other, other_paths)
        self.assertFalse(dialog.isVisible(), "换工程时旧窗口要关掉")
        self.assertIsNone(self.window._all_keys_dialog)


class KeypadFlyoutWidgetTest(unittest.TestCase):
    """窗口里那块小面板本身（设备下拉 + 按键）。"""

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
        app_settings.save_settings(settings, tmp.name)
        loaded = app_settings.load_settings(tmp.name)
        self.assertEqual(loaded.key_usage["甲柜|甲柜.cmd|v:1"], 3)

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
