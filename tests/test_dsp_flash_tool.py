"""「工具 → 工程 → DSP 程序烧写」：注册、文件查找/导入、模拟烧写、外部烧写器配置。"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import settings as app_settings  # noqa: E402
from icomm.core import flash_external, sci_flash  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.tools.dsp_flash import (  # noqa: E402
    DspFlashTool,
    DspFlashWindow,
    ExternalFlasherDialog,
    ProfileDialog,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


HEX = "@1000\n01 02 03 04 05 06 07 08\nq\n"


def pump_until(predicate, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


class DspFlashToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()
        self.reports = os.path.join(self.tmp.name, "reports")
        os.makedirs(self.reports, exist_ok=True)
        self.hex_path = os.path.join(self.tmp.name, "app.hex")
        with open(self.hex_path, "w", encoding="utf-8") as handle:
            handle.write(HEX)
        self.saved: list[int] = []
        self.logged: list[str] = []
        self.announced: list[tuple[str, str, str]] = []
        self.settings = app_settings.AppSettings()
        self.context = ui_tools.ToolContext(
            project=self.project,
            window=None,
            settings=self.settings,
            base_dir=self.tmp.name,
            report_dir=self.reports,
            log=lambda level, message: self.logged.append(message),
            save_settings=lambda: self.saved.append(1),
            notify=lambda level, title, text="": self.announced.append((level, title, text)),
        )
        self.window = DspFlashWindow(self.context)
        self.addCleanup(self.window.deleteLater)

    # -------------------------------------------------- 注册与界面

    def test_tool_is_registered_under_engineering(self) -> None:
        tool = ui_tools.find_tool("DSP 程序烧写")
        self.assertIsNotNone(tool)
        self.assertEqual(type(tool).__name__, "DspFlashTool")
        self.assertEqual(tool.group, "工程")
        self.assertTrue(tool.non_modal)
        names = [entry.name for entry in ui_tools.tools_by_group()["工程"]]
        self.assertIn("DSP 程序烧写", names)

    def test_engine_group_still_sorted_by_order(self) -> None:
        group = [(tool.order, tool.name) for tool in ui_tools.tools_by_group()["工程"]]
        self.assertEqual(group, sorted(group), "工具顺序要按 order 排")

    def test_finds_files_in_project_directory(self) -> None:
        self.window.refresh_files()
        names = [
            self.window.file_table.item(row, 0).text()
            for row in range(self.window.file_table.rowCount())
        ]
        self.assertIn("app.hex", names)
        self.assertIsNotNone(self.window.current_image(), "默认选中第一个文件并解析")

    def test_preview_shows_address_and_checksum(self) -> None:
        self.window.refresh_files()
        self.assertIn("0x00001000", self.window.preview_label.text())
        self.assertIn("ti-txt", self.window.preview_label.text())

    def test_start_is_disabled_without_file(self) -> None:
        """目录里一个烧写文件都没有：不能点开始（避免"以为在烧、其实没选文件"）。"""
        empty_root = os.path.join(self.tmp.name, "empty_project")
        empty_reports = os.path.join(empty_root, "reports")
        os.makedirs(empty_reports, exist_ok=True)
        context = ui_tools.ToolContext(
            project=self.project,
            window=None,
            settings=app_settings.AppSettings(),
            base_dir=empty_root,
            report_dir=empty_reports,
        )
        window = DspFlashWindow(context)
        self.addCleanup(window.deleteLater)
        window.refresh_files()
        self.assertIsNone(window.current_image())
        self.assertFalse(window.start_button.isEnabled())
        self.assertIn("没找到", window.preview_label.text())
        window.simulate_box.setChecked(True)
        self.assertFalse(window.start_button.isEnabled(), "勾模拟也不能在没文件时开始")

    def test_bad_file_disables_start_and_explains(self) -> None:
        bad = os.path.join(self.tmp.name, "broken.hex")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("hello world\n")
        self.window.select_file(bad)
        _app.processEvents()
        self.assertIn("不能烧", self.window.preview_label.text())
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(any("解析失败" in line for line in self.window.log_edit.toPlainText().splitlines()))

    def test_import_file_remembers_recent(self) -> None:
        other = os.path.join(self.tmp.name, "外部", "other.hex")
        os.makedirs(os.path.dirname(other), exist_ok=True)
        with open(other, "w", encoding="utf-8") as handle:
            handle.write(HEX)
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=(other, "")):
            self.assertEqual(self.window.import_file(), other)
        self.assertEqual(self.settings.flash_last_file, other)
        self.assertIn(other, self.settings.flash_recent_files)
        self.assertTrue(os.path.normcase(other) in
                        [os.path.normcase(self.window.current_file())])

    def test_ports_and_baud_are_editable(self) -> None:
        self.window.port_combo.setEditText("COM9")
        self.assertEqual(self.window.current_port(), "COM9")
        self.window.baud_combo.setCurrentText("57600")
        self.assertEqual(self.window.current_baudrate(), 57600)
        self.window.baud_combo.setCurrentText("abc")
        self.assertEqual(self.window.current_baudrate(), 115200, "坏输入退回默认")

    # -------------------------------------------------- 模拟烧写

    def test_simulated_flash_runs_end_to_end(self) -> None:
        self.window.refresh_files()
        self.window.simulate_box.setChecked(True)
        self.assertTrue(self.window.start_button.isEnabled())
        with mock.patch.object(QMessageBox, "question") as question:
            self.window.start_flash()
        self.assertFalse(question.called, "模拟烧写不该弹确认框")
        self.assertTrue(pump_until(lambda: self.window._thread is None), "烧写应很快结束")
        self.assertEqual(self.window.progress.value(), 100)
        self.assertIn("成功", self.window.status_label.text())
        self.assertIn("结果：成功", self.window.log_edit.toPlainText())
        self.assertTrue(self.settings.flash_simulate)
        self.assertTrue(self.announced, "烧写完要发通知")

    def test_flash_uses_selected_profile(self) -> None:
        self.window.refresh_files()
        self.window.simulate_box.setChecked(True)
        self.window.profile.block_words = 1
        self.window.start_flash()
        self.assertTrue(pump_until(lambda: self.window._thread is None))
        self.assertIn("每块 1 字", self.window.log_edit.toPlainText())

    def test_cancel_without_confirm_does_not_flash(self) -> None:
        self.window.refresh_files()
        self.window.port_combo.setEditText("COM9")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.No):
            self.window.start_flash()
        self.assertIsNone(self.window._thread)
        self.assertIn("已取消", self.window.log_edit.toPlainText())

    def test_confirm_yes_starts_worker(self) -> None:
        self.window.refresh_files()
        self.window.port_combo.setEditText("COM404")
        with mock.patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
            self.window.start_flash()
        # 端口不存在：worker 会很快以失败结束
        self.assertTrue(pump_until(lambda: self.window._thread is None))
        self.assertIn("失败", self.window.status_label.text())

    # -------------------------------------------------- 协议参数

    def test_profile_dialog_parses_bytes_and_saves(self) -> None:
        dialog = ProfileDialog(sci_flash.profile_by_key("ti-c2000"))
        self.addCleanup(dialog.deleteLater)
        dialog.autobaud_edit.setText("41")
        dialog.echo_edit.setText("")
        dialog.hs_send_edit.setText("AA 55")
        dialog.ack_edit.setText("")
        profile = dialog.result_profile()
        self.assertEqual(profile.autobaud, b"\x41")
        self.assertEqual(profile.autobaud_echo, b"")
        self.assertEqual(profile.handshake_send, b"\xaa\x55")
        self.assertEqual(profile.ack, b"")

    def test_profile_dialog_rejects_bad_hex(self) -> None:
        dialog = ProfileDialog(sci_flash.profile_by_key("ti-c2000"))
        self.addCleanup(dialog.deleteLater)
        dialog.autobaud_edit.setText("ZZ")
        with self.assertRaisesRegex(ValueError, "十六进制"):
            dialog.result_profile()

    def test_edit_profile_persists_to_settings(self) -> None:
        new_profile = sci_flash.profile_by_key("raw")
        with mock.patch(
            "icomm.ui.tools.dsp_flash.ProfileDialog.exec", return_value=QDialog.Accepted
        ), mock.patch(
            "icomm.ui.tools.dsp_flash.ProfileDialog.result_profile", return_value=new_profile
        ):
            self.window.edit_profile()
        self.assertEqual(self.settings.flash_profile.get("key"), "raw")
        self.assertEqual(self.window.profile.key, "raw")
        self.assertTrue(self.saved)

    # -------------------------------------------------- 外部烧写器

    def test_external_dialog_builds_command_and_saves(self) -> None:
        dialog = ExternalFlasherDialog(self.settings)
        self.addCleanup(dialog.deleteLater)
        dialog.exe_edit.setText(r"C:\tools\flash.exe")
        dialog.args_edit.setText("{exe} --port {port} --file {file}")
        argv = dialog.command(r"C:\proj\a.hex", "COM3", 115200)
        self.assertEqual(argv[0], r"C:\tools\flash.exe")
        self.assertIn("COM3", argv)
        self.assertTrue(any(arg.endswith("a.hex") for arg in argv))
        dialog.save_to(self.settings)
        self.assertEqual(self.settings.flash_external_exe, r"C:\tools\flash.exe")
        self.assertIn("{file}", self.settings.flash_external_args)

    def test_external_guide_states_no_bundling(self) -> None:
        dialog = ExternalFlasherDialog(self.settings)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("不附带", dialog.guide.toPlainText())
        self.assertIn("UniFlash", dialog.guide.toPlainText())

    def test_external_run_without_path_is_reported(self) -> None:
        dialog = ExternalFlasherDialog(self.settings)
        self.addCleanup(dialog.deleteLater)
        dialog.exe_edit.setText("")
        dialog._run_external()
        self.assertIn("先填", dialog.external_log.toPlainText())

    def test_external_run_without_file_is_reported(self) -> None:
        dialog = ExternalFlasherDialog(self.settings, file="")
        self.addCleanup(dialog.deleteLater)
        dialog.exe_edit.setText(r"C:\tools\flash.exe")
        dialog._run_external()
        self.assertIn("先在主窗口选一个烧写文件", dialog.external_log.toPlainText())


class SettingsTest(unittest.TestCase):
    def test_flash_settings_survive_save_and_load(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        settings = app_settings.AppSettings()
        settings.flash_port = "COM7"
        settings.flash_baudrate = 57600
        settings.flash_simulate = True
        settings.flash_recent_files = [os.path.join(tmp.name, "a.hex")]
        settings.flash_profile = {"key": "raw"}
        settings.flash_external_exe = "flash.exe"
        app_settings.save_settings(settings, tmp.name)
        loaded = app_settings.load_settings(tmp.name)
        self.assertEqual(loaded.flash_port, "COM7")
        self.assertEqual(loaded.flash_baudrate, 57600)
        self.assertTrue(loaded.flash_simulate)
        self.assertEqual(loaded.flash_profile["key"], "raw")
        self.assertEqual(loaded.flash_external_exe, "flash.exe")

    def test_bad_flash_settings_are_clamped(self) -> None:
        settings = app_settings.AppSettings(
            flash_baudrate="abc",  # type: ignore[arg-type]
            flash_profile=["x"],  # type: ignore[arg-type]
            flash_recent_files=[1, "   "],  # type: ignore[list-item]
            flash_external_key="",
        )
        settings.clamp()
        self.assertEqual(settings.flash_baudrate, 115200)
        self.assertEqual(settings.flash_profile, {})
        self.assertEqual(settings.flash_recent_files, [])
        self.assertEqual(settings.flash_external_key, "custom")


if __name__ == "__main__":
    unittest.main()
