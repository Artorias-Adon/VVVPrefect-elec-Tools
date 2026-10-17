"""把工具窗口钉进左侧栏（导航栏）当页面用。

用户要求：记事本、分析工具这类开成小窗口的复杂工具，内容能拖到
「主页 / 运行监视」所在的那一栏里。这里盯住四件事：

* 拖动条存在且能发起拖动（MIME 里带工具名）；
* 导航栏认得出这种拖动，钉完就有对应的页面与导航项；
* 钉过的工具再点菜单是"切过去"，不是又开一个浮窗；
* 钉在哪个工具，关掉软件再开还在（写进 settings.json，换工程后按新工程重建）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store, settings as app_settings  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.main_window import NAV_ITEMS, SETTINGS_TABS, MainWindow  # noqa: E402
from icomm.ui.tool_dock import (  # noqa: E402
    TOOL_MIME,
    NavList,
    ToolPageGrip,
    install_page_grip,
)

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _close_all() -> None:
    from icomm.ui.dialogs.control_panel import close_all

    close_all()


class GripTest(unittest.TestCase):
    def test_install_inserts_one_grip_at_top(self) -> None:
        window = QWidget()
        window.setLayout(QVBoxLayout())
        window.layout().addWidget(QWidget())
        self.addCleanup(window.deleteLater)

        grip = install_page_grip(window, "记事本")
        self.assertIsInstance(grip, ToolPageGrip)
        self.assertIs(window.layout().itemAt(0).widget(), grip)

        again = install_page_grip(window, "记事本")
        self.assertIs(again, grip, "重复安装应当返回已有的那条，不再插一条")
        grips = window.findChildren(ToolPageGrip)
        self.assertEqual(len(grips), 1)

    def test_no_grip_for_non_vertical_layout(self) -> None:
        window = QWidget()
        window.setLayout(QHBoxLayout())
        self.addCleanup(window.deleteLater)
        self.assertIsNone(install_page_grip(window, "记事本"))

    def test_pin_button_reports_tool_name(self) -> None:
        window = QWidget()
        window.setLayout(QVBoxLayout())
        self.addCleanup(window.deleteLater)
        seen: list[str] = []
        grip = install_page_grip(window, "分析工具", on_pin=seen.append)
        grip.pinRequested.emit("分析工具")
        self.assertEqual(seen, ["分析工具"])

    def test_mime_carries_tool_name(self) -> None:
        mime = QMimeData()
        mime.setData(TOOL_MIME, "记事本".encode("utf-8"))
        self.assertEqual(NavList.tool_name_from(mime), "记事本")

        other = QMimeData()
        other.setText("随便什么")
        self.assertEqual(NavList.tool_name_from(other), "")
        self.assertEqual(NavList.tool_name_from(None), "")


class MainWindowToolPageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()
        self.paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "工程"), self.project
        )
        self.window = self._make_window()
        self.addCleanup(self._cleanup)
        self.addCleanup(_close_all)

    def _make_window(self) -> MainWindow:
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

    def _nav_texts(self) -> list[str]:
        return [self.window.nav.item(i).text() for i in range(self.window.nav.count())]

    def test_embed_adds_nav_item_and_page(self) -> None:
        before = len(self._nav_texts())
        self.assertTrue(self.window.embed_tool("记事本"))
        texts = self._nav_texts()
        self.assertEqual(len(texts), before + 1)
        self.assertIn("记事本", texts[-1])
        self.assertIn("记事本", self.window._embedded)
        self.assertIs(self.window.stack.currentWidget(), self.window._embedded["记事本"])
        # 导航里点一下能切回来
        self.window.nav.setCurrentRow(0)
        self.assertIs(self.window.stack.currentWidget(), self.window.overview_page)
        self.window.nav.setCurrentRow(self.window._nav_row_of_tool("记事本"))
        self.assertIs(self.window.stack.currentWidget(), self.window._embedded["记事本"])

    def test_embedding_twice_does_not_duplicate(self) -> None:
        self.window.embed_tool("记事本")
        count = len(self._nav_texts())
        self.window.embed_tool("记事本")
        self.assertEqual(len(self._nav_texts()), count)
        self.assertEqual(len(self.window._embedded), 1)

    def test_dialog_tool_cannot_be_embedded(self) -> None:
        """模态对话框类工具（如 Modbus 报文）钉不了，应当明确拒绝。"""
        from unittest import mock

        from PySide6.QtWidgets import QMessageBox

        dialog_tool = next(t for t in ui_tools.tools() if not t.non_modal)
        with mock.patch.object(QMessageBox, "information", return_value=QMessageBox.Ok):
            self.assertFalse(self.window.embed_tool(dialog_tool.name))
        self.assertNotIn(dialog_tool.name, self.window._embedded)

    def test_drop_signal_embeds_the_tool(self) -> None:
        """导航栏收到拖动（dropEvent 的信号）就钉进去。"""
        self.window.nav.toolDropped.emit("记事本")
        self.assertIn("记事本", self.window._embedded)

    def test_run_tool_focuses_embedded_page_instead_of_floating(self) -> None:
        self.window.embed_tool("记事本")
        self.window.nav.setCurrentRow(0)
        tool = ui_tools.find_tool("记事本")
        self.window.run_tool(tool)
        self.assertIs(self.window.stack.currentWidget(), self.window._embedded["记事本"])
        self.assertIsNone(type(tool)._window, "已经钉住的工具不该再开浮动窗口")

    def test_remove_tool_page(self) -> None:
        self.window.embed_tool("记事本")
        row = self.window._nav_row_of_tool("记事本")
        self.assertTrue(self.window.remove_tool_page("记事本"))
        self.assertNotIn("记事本", self.window._embedded)
        self.assertFalse(self.window._nav_row_of_tool("记事本") >= 0)
        self.assertLess(self.window.nav.count(), row + 1)
        self.assertNotIn("记事本", self.window.settings.embedded_tools)

    def test_embedded_tools_are_remembered_and_restored(self) -> None:
        self.window.embed_tool("记事本")
        self.window.embed_tool("分析工具")
        saved = list(self.window.settings.embedded_tools)
        self.assertEqual(saved, ["记事本", "分析工具"])
        on_disk = app_settings.load_settings(self.tmp.name)
        self.assertEqual(on_disk.embedded_tools, saved)

        again = self._make_window()
        texts = [again.nav.item(i).text() for i in range(again.nav.count())]
        for name in saved:
            self.assertIn(name, again._embedded, f"重启后没恢复「{name}」")
            self.assertTrue(any(name in text for text in texts), texts)
        again.close()

    def test_unknown_embedded_tool_is_ignored(self) -> None:
        settings = AppSettings(embedded_tools=["根本没有这个工具"])
        window = MainWindow(
            self.project,
            self.paths,
            workdir=self.tmp.name,
            base_dir=self.tmp.name,
        )
        self.addCleanup(window.close)
        window.settings.embedded_tools = settings.embedded_tools
        window.restore_embedded_tools()
        self.assertEqual(window._embedded, {})
        self.assertEqual(window.nav.count(), len(NAV_ITEMS))

    def test_step_page_includes_tool_pages(self) -> None:
        self.window.embed_tool("记事本")
        total = self.window.nav.count()
        self.window.nav.setCurrentRow(total - 1)
        self.window._step_page(1)
        self.assertEqual(self.window.nav.currentRow(), 0, "最后一页往后翻应当回到第一页")

    def test_tool_pages_rebuilt_after_project_switch(self) -> None:
        self.window.embed_tool("分析工具")
        old_page = self.window._embedded["分析工具"]
        other = project_io.grid_project()
        target = project_store.create_project_dir(
            os.path.join(self.tmp.name, "另一个工程"), other
        )
        self.window._open_project_path(target.project_file)
        self.assertIn("分析工具", self.window._embedded)
        new_page = self.window._embedded["分析工具"]
        self.assertIsNot(new_page, old_page, "换工程后工具页要按新工程重建")
        self.assertEqual(new_page.context.project.name, other.name)
        self.assertEqual(
            sum(1 for i in range(self.window.nav.count())
                if "分析工具" in self.window.nav.item(i).text()),
            1,
            "重建后导航里不能留下两条",
        )


class SettingsFieldTest(unittest.TestCase):
    def test_embedded_tools_are_sanitised(self) -> None:
        settings = AppSettings.from_dict(
            {"embedded_tools": ["  记事本  ", "记事本", "", None, 42, "分析工具"]}
        )
        self.assertEqual(settings.embedded_tools, ["记事本", "分析工具"])
        self.assertEqual(AppSettings.from_dict({"embedded_tools": "不是列表"}).embedded_tools, [])

    def test_embedded_tools_capped(self) -> None:
        names = [f"工具{index}" for index in range(30)]
        settings = AppSettings.from_dict({"embedded_tools": names})
        self.assertEqual(len(settings.embedded_tools), 12)


if __name__ == "__main__":
    unittest.main()
