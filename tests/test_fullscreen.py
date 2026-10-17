"""全屏模式：菜单/F11 切换、设置里记住、Esc 退出。

工业现场常把软件挂在一台工控机上当看板用，标题栏和边框是白占地方。
所以全屏做成一等公民：设置里有勾选项、菜单里有开关、F11/Esc 随手切、状态能记住。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core import settings as app_settings  # noqa: E402
from icomm.ui.dialogs.settings_dialog import SettingsDialog  # noqa: E402
from icomm.ui.main_window import MainWindow  # noqa: E402

_app: QApplication | None = None

#: 这个用例类里的窗口统一用英文项目名，免得设置对话框弹「文件夹名要用英文」
NO_SAMPLES: list[str] = []


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class SettingsFieldTest(unittest.TestCase):
    def test_default_off(self) -> None:
        self.assertFalse(app_settings.AppSettings().fullscreen)

    def test_round_trip(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        original = app_settings.AppSettings(fullscreen=True)
        app_settings.save_settings(original, tmp.name)
        loaded = app_settings.load_settings(tmp.name)
        self.assertTrue(loaded.fullscreen)

    def test_clamp_keeps_bool(self) -> None:
        settings = app_settings.AppSettings(fullscreen="yes")  # type: ignore[arg-type]
        settings.clamp()
        self.assertIs(settings.fullscreen, True)


class WindowCase(unittest.TestCase):
    """带一个真正的主窗口。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()
        self.paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "proj"), self.project
        )
        self.settings = app_settings.AppSettings(remember_window=True)
        self.addCleanup(self._cleanup)

    def make_window(self, **overrides) -> MainWindow:
        for name, value in overrides.items():
            setattr(self.settings, name, value)
        app_settings.save_settings(self.settings, self.tmp.name)
        window = MainWindow(
            self.project, self.paths, workdir=self.tmp.name, base_dir=self.tmp.name
        )
        window.settings.confirm_on_exit = False
        window.show()
        _app.processEvents()
        self.addCleanup(self._close_window, window)
        return window

    def _close_window(self, window: MainWindow) -> None:
        if window.is_fullscreen():
            window.set_fullscreen(False, remember=False)
        window.settings.confirm_on_exit = False
        window.close()
        _app.processEvents()

    def _cleanup(self) -> None:
        pass


class MainWindowFullscreenTest(WindowCase):
    def test_toggle_enters_and_leaves(self) -> None:
        window = self.make_window()
        self.assertFalse(window.is_fullscreen())
        window.set_fullscreen(True)
        self.assertTrue(window.isFullScreen())
        self.assertTrue(window.is_fullscreen())
        self.assertTrue(window.window_action.isChecked(), "菜单里的勾要跟上")
        window.set_fullscreen(False)
        self.assertFalse(window.isFullScreen())
        self.assertFalse(window.window_action.isChecked())

    def test_state_is_remembered(self) -> None:
        window = self.make_window()
        window.set_fullscreen(True)
        self.assertTrue(window.settings.fullscreen)
        self.assertTrue(app_settings.load_settings(self.tmp.name).fullscreen)

        window.set_fullscreen(False)
        self.assertFalse(app_settings.load_settings(self.tmp.name).fullscreen)

    def test_startup_uses_saved_state(self) -> None:
        window = self.make_window(fullscreen=True)
        self.assertTrue(window.isFullScreen(), "上次全屏，这次启动就该是全屏")
        self.assertTrue(window.window_action.isChecked())

    def test_esc_leaves_fullscreen(self) -> None:
        window = self.make_window()
        window.set_fullscreen(True)
        self.assertTrue(window.isFullScreen())
        QTest.keyClick(window, Qt.Key_Escape)
        _app.processEvents()
        self.assertFalse(window.is_fullscreen(), "Esc 要能退出全屏")
        self.assertFalse(app_settings.load_settings(self.tmp.name).fullscreen)

    def test_esc_outside_fullscreen_is_harmless(self) -> None:
        window = self.make_window()
        QTest.keyClick(window, Qt.Key_Escape)
        _app.processEvents()
        self.assertFalse(window.is_fullscreen())

    def test_f11_shortcut_is_registered(self) -> None:
        window = self.make_window()
        self.assertEqual(window.window_action.shortcut(), QKeySequence("F11"))
        self.assertTrue(window.window_action.isCheckable())

    def test_action_lives_in_settings_menu(self) -> None:
        window = self.make_window()
        menus = [a.menu() for a in window.menuBar().actions() if a.menu() is not None]
        settings_menu = [menu for menu in menus if menu.title().startswith("设置")][0]
        self.assertIn(window.window_action, settings_menu.actions())

    def test_window_geometry_not_saved_while_fullscreen(self) -> None:
        window = self.make_window()
        window.settings.window_geometry = "BEFORE"
        window.set_fullscreen(True)
        window._remember_window_geometry()
        self.assertEqual(window.settings.window_geometry, "BEFORE", "全屏时不该把几何存成全屏的")

    def test_reset_window_size_leaves_fullscreen(self) -> None:
        window = self.make_window(fullscreen=True)
        self.assertTrue(window.is_fullscreen())
        window.reset_window_size()
        self.assertFalse(window.is_fullscreen())
        self.assertFalse(window.settings.fullscreen)

    def test_restore_returns_to_maximized(self) -> None:
        window = self.make_window()
        # offscreen 平台没有真的最大化，这里把「进全屏前是最大化」直接摆好
        with mock.patch.object(window, "isMaximized", return_value=True):
            window.set_fullscreen(True)
        self.assertTrue(window._was_maximized)
        with mock.patch.object(window, "showMaximized") as show_maximized:
            window.set_fullscreen(False)
        self.assertTrue(show_maximized.called, "进全屏前是最大化，退出后要还回去")


class SettingsDialogFullscreenTest(WindowCase):
    def test_checkbox_reflects_and_saves(self) -> None:
        window = self.make_window()
        dialog = window.settings_dialog()
        self.addCleanup(dialog.reject)
        self.assertFalse(dialog.fullscreen_box.isChecked())

        dialog.fullscreen_box.setChecked(True)
        dialog.accept()
        self.assertTrue(dialog.settings.fullscreen)
        self.assertTrue(
            app_settings.AppSettings.from_dict(dialog.settings.to_dict()).fullscreen
        )

    def test_accept_applies_fullscreen(self) -> None:
        window = self.make_window()
        dialog = window.settings_dialog()
        dialog.fullscreen_box.setChecked(True)
        dialog.accept()  # 确定时把表单收进 dialog.settings
        window.apply_settings(dialog)
        _app.processEvents()
        self.assertTrue(window.isFullScreen(), "确定后要真的进全屏")
        self.assertTrue(window.window_action.isChecked())

    def test_unchecking_leaves_fullscreen(self) -> None:
        window = self.make_window(fullscreen=True)
        self.assertTrue(window.is_fullscreen())
        dialog = window.settings_dialog()
        dialog.fullscreen_box.setChecked(False)
        dialog.accept()
        window.apply_settings(dialog)
        _app.processEvents()
        self.assertFalse(window.is_fullscreen())


if __name__ == "__main__":
    unittest.main()
