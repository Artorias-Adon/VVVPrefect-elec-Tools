"""配色主题"跟随系统"：系统深色/浅色变了，界面跟着换。

盯住四件事：
* 设置能存能读（``follow_system`` 落在 settings.json 里，老配置读出来是关的）；
* 跟随时真正生效的是按系统偏好挑的那套预设，而用户手选的配色仍然记着；
* 系统偏好一变（Qt 的 ``colorSchemeChanged``）就重套配色，关掉跟随时一动不动；
* 设置界面里那个勾选框与它引起的联动（锁住手选配色、说明用哪套）。
"""

from __future__ import annotations

import gc
import os
import tempfile
import unittest
import weakref
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core import settings as app_settings  # noqa: E402
from icomm.ui import theme  # noqa: E402
from icomm.ui.dialogs.appearance_editor import AppearanceEditor  # noqa: E402
from icomm.ui.dialogs.settings_dialog import SettingsDialog  # noqa: E402
from icomm.ui.main_window import NAV_ITEMS, MainWindow  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class ThemeCase(unittest.TestCase):
    """每个用例都把"活动外观"和监听器还原，免得串味。"""

    def setUp(self) -> None:
        self._before = theme.appearance().copy()
        theme.unwatch_system_scheme()
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        theme.unwatch_system_scheme()
        theme.apply(self._before)


class AppearanceSerializationTest(ThemeCase):
    def test_default_is_not_following(self) -> None:
        self.assertFalse(theme.Appearance().follow_system)
        self.assertFalse(theme.Appearance().to_dict()["follow_system"])

    def test_round_trip(self) -> None:
        source = theme.Appearance(follow_system=True)
        data = source.to_dict()
        self.assertTrue(data["follow_system"])
        self.assertTrue(theme.Appearance.from_dict(data).follow_system)

    def test_tolerant_values(self) -> None:
        for raw, expected in (("true", True), ("1", True), (1, True), (True, True),
                              ("no", False), (0, False), (None, False), ("", False)):
            with self.subTest(raw=raw):
                got = theme.Appearance.from_dict({"follow_system": raw}).follow_system
                self.assertEqual(got, expected)

    def test_missing_key_means_off(self) -> None:
        data = theme.Appearance().to_dict()
        data.pop("follow_system")
        self.assertFalse(theme.Appearance.from_dict(data).follow_system)

    def test_copy_is_deep(self) -> None:
        source = theme.Appearance(follow_system=True)
        clone = source.copy()
        clone.palette.accent = "#123456"
        self.assertNotEqual(source.palette.accent, "#123456")


class FollowSystemApplyTest(ThemeCase):
    def test_effective_appearance_maps_scheme(self) -> None:
        for scheme in (theme.SCHEME_DARK, theme.SCHEME_LIGHT):
            with self.subTest(scheme=scheme):
                with mock.patch.object(theme, "system_color_scheme", lambda s=scheme: s):
                    theme.apply(theme.Appearance(follow_system=True))
                expected = theme.FOLLOW_SYSTEM_PRESETS[scheme]
                self.assertEqual(theme.appearance().palette.name, expected)
                self.assertEqual(theme.BG, theme.PRESETS[expected].bg)
                self.assertIn(theme.PRESETS[expected].bg, theme.STYLESHEET)

    def test_manual_palette_is_kept(self) -> None:
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            theme.apply_preset("深色工业")
            theme.apply(
                theme.Appearance(palette=theme.PRESETS["深色工业"].copy(), follow_system=True)
            )
        self.assertEqual(theme.appearance().palette.name, "天蓝", "生效的是系统那套")
        self.assertEqual(theme.preference().palette.name, "深色工业", "手选的还记着")

        theme.apply_follow_system(False)
        self.assertEqual(theme.appearance().palette.name, "深色工业", "关掉跟随要还回来")

    def test_follow_ignores_manual_colour_edits(self) -> None:
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            appearance = theme.Appearance(follow_system=True)
            appearance.palette.accent = "#ff00ff"
            theme.apply(appearance)
        self.assertEqual(theme.ACCENT, theme.PRESETS["天蓝"].accent, "跟随时手改的颜色不算数")

    def test_apply_preset_stops_following(self) -> None:
        theme.apply(theme.Appearance(follow_system=True))
        self.assertTrue(theme.preference().follow_system)
        theme.apply_preset("纯白蓝调")
        self.assertFalse(theme.preference().follow_system)
        self.assertEqual(theme.appearance().palette.name, "纯白蓝调")

    def test_follow_system_toggle_keeps_font_and_density(self) -> None:
        theme.apply(theme.Appearance(font_size=17, density="紧凑"))
        theme.apply_follow_system(True)
        self.assertEqual(theme.appearance().font_size, 17)
        self.assertEqual(theme.appearance().density, "紧凑")
        self.assertEqual(theme.preference().font_size, 17)

    def test_unknown_follow_preset_falls_back(self) -> None:
        with mock.patch.dict(theme.FOLLOW_SYSTEM_PRESETS, {"dark": "不存在的主题"}):
            with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
                theme.apply(theme.Appearance(follow_system=True))
        self.assertEqual(theme.appearance().palette.name, theme.DEFAULT_PRESET)


class SystemSchemeChangeTest(ThemeCase):
    def test_refresh_does_nothing_when_not_following(self) -> None:
        theme.apply_preset("深色工业")
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_LIGHT):
            self.assertFalse(theme.refresh_system_scheme())
        self.assertEqual(theme.appearance().palette.name, "深色工业")

    def test_refresh_switches_colours(self) -> None:
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            theme.apply(theme.Appearance(follow_system=True))
        self.assertEqual(theme.appearance().palette.name, "天蓝")
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_LIGHT):
            self.assertTrue(theme.refresh_system_scheme())
        self.assertEqual(theme.appearance().palette.name, "浅色办公")
        self.assertEqual(theme.BG, theme.PRESETS["浅色办公"].bg)

    def test_callbacks_fire_only_when_following(self) -> None:
        calls: list[str] = []
        theme.watch_system_scheme(lambda: calls.append("x"))
        theme.apply_preset("天蓝")
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_LIGHT):
            theme.on_system_scheme_changed()
        self.assertEqual(calls, [], "没开跟随就不该惊动界面")

        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            theme.apply_follow_system(True)
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_LIGHT):
            self.assertTrue(theme.on_system_scheme_changed())
        self.assertEqual(calls, ["x"])

    def test_dead_callback_is_dropped(self) -> None:
        class Listener:
            def __init__(self) -> None:
                self.calls = 0

            def poke(self) -> None:
                self.calls += 1

        listener = Listener()
        theme.watch_system_scheme(listener.poke)
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            theme.apply(theme.Appearance(follow_system=True))
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_LIGHT):
            theme.on_system_scheme_changed()
        self.assertEqual(listener.calls, 1)

        ref = weakref.ref(listener)
        del listener
        gc.collect()
        self.assertIsNone(ref(), "对象该被回收了（监听不该拖着它）")

        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            theme.on_system_scheme_changed()  # 不该因为窗口没了就炸
        self.assertEqual(len(theme._system_watchers), 0, "失效的回调要被清掉")

    def test_qt_signal_reaches_the_theme(self) -> None:
        """监听要真连上 Qt 的 ``colorSchemeChanged``，并且连上后能走到重套配色那条路。"""
        self.assertTrue(theme.watch_system_scheme(), "Qt 6.5+ 应该能连上这个信号")
        self.assertTrue(theme._scheme_watch_connected, "只该连一次")
        self.assertTrue(theme.watch_system_scheme(), "重复调用不该再连一遍")

        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            theme.apply(theme.Appearance(follow_system=True))
        self.assertEqual(theme.appearance().palette.name, theme.FOLLOW_SYSTEM_PRESETS["dark"])
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_LIGHT):
            self.assertTrue(theme.on_system_scheme_changed())
        self.assertEqual(theme.appearance().palette.name, theme.FOLLOW_SYSTEM_PRESETS["light"])


class SettingsDialogFollowTest(ThemeCase):
    """在设置对话框里开关「跟随系统」：确定要存下来，取消要原样还原。"""

    def _dialog(self, follow: bool) -> tuple[object, object]:
        project = project_io.bess_project()
        dialog = SettingsDialog(
            app_settings.AppSettings(appearance=theme.Appearance(follow_system=follow).to_dict()),
            project,
            list(project_io.BUILTIN_SAMPLES),
        )
        self.addCleanup(dialog.reject)
        return dialog, project

    def test_accept_persists_follow_flag(self) -> None:
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            dialog, _project = self._dialog(follow=False)
        dialog.appearance_editor.follow_box.setChecked(True)
        self.assertTrue(theme.preference().follow_system, "勾上就该实时预览")

        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            dialog.accept()
        self.assertTrue(dialog.settings.appearance["follow_system"])
        self.assertTrue(
            theme.Appearance.from_dict(dialog.settings.appearance).follow_system
        )

    def test_cancel_restores_manual_palette_and_follow(self) -> None:
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            theme.apply(
                theme.Appearance(palette=theme.PRESETS["深色工业"].copy(), follow_system=True)
            )
            dialog, _project = self._dialog(follow=True)
        self.assertEqual(theme.appearance().palette.name, "天蓝", "跟随时生效的是系统那套")

        dialog.appearance_editor.follow_box.setChecked(False)
        dialog.reject()

        self.assertTrue(theme.preference().follow_system, "取消后要恢复成「跟随系统」")
        self.assertEqual(
            theme.preference().palette.name, "深色工业", "手选的那套不能被冲掉"
        )


class AppearanceEditorTest(ThemeCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes: list[object] = []
        self.editor = AppearanceEditor(theme.Appearance())
        self.editor.changed.connect(lambda appearance: self.changes.append(appearance))
        self.addCleanup(self.editor.deleteLater)

    def test_checkbox_toggles_follow(self) -> None:
        self.assertFalse(self.editor.follow_box.isChecked())
        self.editor.follow_box.setChecked(True)
        self.assertTrue(self.editor.current_appearance().follow_system)
        self.assertTrue(self.changes, "改勾选要发 changed（界面实时预览）")

    def test_manual_palette_locked_while_following(self) -> None:
        self.assertTrue(self.editor.preset_combo.isEnabled())
        self.editor.follow_box.setChecked(True)
        self.assertFalse(self.editor.preset_combo.isEnabled(), "跟随时不该手选预设")
        self.assertFalse(self.editor.color_tabs.isEnabled(), "也不该逐个改颜色")
        self.assertIn("当前系统", self.editor.follow_note.text())
        self.editor.follow_box.setChecked(False)
        self.assertTrue(self.editor.preset_combo.isEnabled())

    def test_note_names_the_preset_in_use(self) -> None:
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_LIGHT):
            self.editor.follow_box.setChecked(True)
        self.assertIn(theme.FOLLOW_SYSTEM_PRESETS[theme.SCHEME_LIGHT],
                      self.editor.follow_note.text())

    def test_picking_preset_clears_follow(self) -> None:
        self.editor.follow_box.setChecked(True)
        index = self.editor.preset_combo.findData("深色工业")
        self.editor.preset_combo.setCurrentIndex(index)
        self.assertFalse(self.editor.current_appearance().follow_system)

    def test_loads_following_state(self) -> None:
        editor = AppearanceEditor(theme.Appearance(follow_system=True))
        self.addCleanup(editor.deleteLater)
        self.assertTrue(editor.follow_box.isChecked())
        self.assertFalse(editor.preset_combo.isEnabled())


class PageRestyleTest(ThemeCase):
    """换主题要连"构造时写死的颜色"一起换掉（内联样式只看全局样式表是不会变的）。"""

    def setUp(self) -> None:
        super().setUp()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        project = project_io.bess_project()
        paths = project_store.create_project_dir(os.path.join(tmp.name, "proj"), project)
        self.window = MainWindow(project, paths, workdir=tmp.name, base_dir=tmp.name)
        self.window.settings.confirm_on_exit = False
        self.addCleanup(self.window.close)

    def test_inline_colours_follow_new_theme(self) -> None:
        theme.apply_preset("浅色办公")
        self.window._refresh_appearance()
        self.assertIn(theme.TEXT, self.window.overview_page.title.styleSheet())

        theme.apply_preset("天蓝")
        self.window._refresh_appearance()
        self.assertIn(theme.TEXT, self.window.overview_page.title.styleSheet())
        self.assertNotIn("#1d1d1f", self.window.overview_page.title.styleSheet())

    def test_pages_keep_their_stack_positions(self) -> None:
        before = [self.window.stack.widget(i) for i in range(len(NAV_ITEMS))]
        self.window._refresh_appearance()
        after = [self.window.stack.widget(i) for i in range(len(NAV_ITEMS))]
        self.assertEqual(len(after), len(before))
        for index, page in enumerate(after):
            self.assertIsNotNone(page, f"第 {index} 个页面不见了")
            self.assertIs(page, getattr(self.window, [
                "overview_page", "dashboard_page", "scope_page", "trend_page",
                "alarm_page", "monitor_page", "devices_page", "report_page",
                "device_detail_page",
            ][index]), "导航序号要与 stack 位置对得上")

    def test_embedded_tool_page_survives_restyle(self) -> None:
        self.window.embed_tool("记事本")
        page = self.window._embedded.get("记事本")
        self.assertIsNotNone(page, "记事本应该能钉进左侧栏")
        self.window._refresh_appearance()
        self.assertIs(self.window._embedded.get("记事本"), page, "工具页不该被主题切换删掉")
        self.assertGreaterEqual(self.window.stack.indexOf(page), len(NAV_ITEMS))


class MainWindowFollowsSystemTest(ThemeCase):
    def _window(self, appearance: dict | None = None) -> tuple[MainWindow, str]:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        if appearance is not None:
            app_settings.save_settings(
                app_settings.AppSettings(appearance=appearance), tmp.name
            )
        project = project_io.bess_project()
        paths = project_store.create_project_dir(os.path.join(tmp.name, "proj"), project)
        window = MainWindow(project, paths, workdir=tmp.name, base_dir=tmp.name)
        window.settings.confirm_on_exit = False
        self.addCleanup(window.close)
        return window, tmp.name

    def test_startup_uses_system_scheme(self) -> None:
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_LIGHT):
            window, _tmp = self._window(theme.Appearance(follow_system=True).to_dict())
        self.assertEqual(theme.appearance().palette.name, "浅色办公")
        self.assertIn(theme.BG, window.styleSheet())
        self.assertTrue(window.settings.appearance["follow_system"])

    def test_scheme_change_restyles_window(self) -> None:
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            window, _tmp = self._window(theme.Appearance(follow_system=True).to_dict())
        self.assertEqual(theme.appearance().palette.name, "天蓝")

        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_LIGHT):
            self.assertTrue(theme.on_system_scheme_changed(), "系统换浅色，配色该跟着换")
        self.assertEqual(theme.appearance().palette.name, "浅色办公")
        self.assertIn(theme.BG, window.styleSheet(), "窗口样式表要跟着换")
        self.assertIn("系统配色变了", window.status_last.text(), "状态栏要说明一句")

    def test_scheme_change_ignored_when_not_following(self) -> None:
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_DARK):
            window, _tmp = self._window(theme.Appearance().to_dict())
        self.assertEqual(theme.appearance().palette.name, theme.DEFAULT_PRESET)
        before = window.styleSheet()
        with mock.patch.object(theme, "system_color_scheme", lambda: theme.SCHEME_LIGHT):
            self.assertFalse(theme.on_system_scheme_changed(), "没开跟随就不该换配色")
        self.assertEqual(theme.appearance().palette.name, theme.DEFAULT_PRESET)
        self.assertEqual(window.styleSheet(), before)


if __name__ == "__main__":
    unittest.main()
