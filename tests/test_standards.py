"""关于对话框里的「标准与规范」：条目索引 + 本地标准库。

两条红线要守住：

* **软件不附带标准正文**（版权在发布机构）—— 只给编号/名称和官方入口；
* 用户自己导入的文件要**落在工程目录里**（``standards/``）、同名不覆盖、能打开。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl  # noqa: E402
from PySide6.QtGui import QDesktopServices  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core import standards  # noqa: E402
from icomm.ui.dialogs.about import AboutDialog, standard_rows  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def write(path: str, text: str = "标准正文") -> str:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


# --------------------------------------------------------------- 索引


class CatalogTest(unittest.TestCase):
    def test_catalog_is_filled_and_unique(self) -> None:
        self.assertGreaterEqual(len(standards.STANDARDS), 15)
        codes = [item.code for item in standards.STANDARDS]
        self.assertEqual(len(codes), len(set(codes)), "标准编号不能重复")
        for item in standards.STANDARDS:
            self.assertIn(item.group, standards.GROUPS, item.code)
            self.assertTrue(item.title.strip(), item.code)
            self.assertTrue(item.note.strip(), f"{item.code} 要写清跟软件哪块对应")

    def test_groups_cover_every_group_used(self) -> None:
        grouped = standards.by_group()
        self.assertEqual(list(grouped), [g for g in standards.GROUPS if g in grouped])
        total = sum(len(items) for items in grouped.values())
        self.assertEqual(total, len(standards.STANDARDS))

    def test_key_standards_are_listed(self) -> None:
        codes = {item.code for item in standards.STANDARDS}
        for expected in (
            "GB/T 14549-1993",
            "GB/T 12325-2008",
            "GB/T 19582.1-2008",
            "GB/T 19582.3-2008",
            "GB/T 36547",
            "DL/T 860",
        ):
            self.assertIn(expected, codes)

    def test_search_url_points_at_official_system(self) -> None:
        url = standards.search_url("GB/T 14549-1993")
        self.assertIn("openstd.samr.gov.cn", url)
        self.assertIn("14549", url)
        self.assertIn("%2F", url, "斜杠要转义，否则检索串会被截断")
        self.assertEqual(standards.search_url("DL/T 645"), standards.STD_SERVICE_HOME)

    def test_relevant_groups_by_project_name(self) -> None:
        relevant = standards.relevant_groups("储能电站（BESS）")
        self.assertIn("储能与光伏", relevant)
        self.assertIn("电能计量", relevant)
        self.assertEqual(standards.relevant_groups(""), [])
        self.assertEqual(standards.relevant_groups("随便一个工程"), [])
        ordered = standards.ordered_groups("光伏电站")
        flags = dict(ordered)
        self.assertTrue(flags.get("储能与光伏"))
        # 相关的排前面
        first_unrelated = next(i for i, (_name, related) in enumerate(ordered) if not related)
        for index, (_name, related) in enumerate(ordered):
            if related:
                self.assertLess(index, first_unrelated)

    def test_disclaimer_says_no_full_text(self) -> None:
        text = standards.DISCLAIMER
        self.assertIn("不附带任何标准正文", text)
        self.assertIn("版权", text)
        self.assertIn("国家标准全文公开系统", text)


# --------------------------------------------------------------- 本地库


class LibraryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_library_dir_is_inside_the_project(self) -> None:
        folder = standards.library_dir(self.root, "", create=True)
        self.assertEqual(folder, os.path.join(self.root, standards.STANDARDS_DIR))
        self.assertTrue(os.path.isdir(folder))
        # 没要求 create 时不该凭空建目录
        other = os.path.join(self.root, "另一个工程")
        os.makedirs(other)
        standards.library_dir(other, self.root)
        self.assertFalse(os.path.isdir(os.path.join(other, standards.STANDARDS_DIR)))

    def test_library_dir_falls_back_to_base_dir(self) -> None:
        folder = standards.library_dir("", self.root)
        self.assertEqual(folder, os.path.join(self.root, standards.STANDARDS_DIR))

    def test_scan_missing_folder_is_empty(self) -> None:
        self.assertEqual(standards.scan_files(os.path.join(self.root, "没有")), [])
        self.assertEqual(standards.scan_files(""), [])

    def test_scan_lists_files_sorted_and_skips_noise(self) -> None:
        folder = standards.library_dir(self.root, create=True)
        write(os.path.join(folder, "b.pdf"))
        write(os.path.join(folder, "A.txt"))
        os.makedirs(os.path.join(folder, "子目录"), exist_ok=True)
        write(os.path.join(folder, ".隐藏"))
        entries = standards.scan_files(folder)
        self.assertEqual([entry["name"] for entry in entries], ["A.txt", "b.pdf"])
        self.assertEqual(entries[0]["kind"], "TXT")
        self.assertGreater(entries[0]["size"], 0)
        self.assertTrue(standards.format_time(entries[0]["mtime"]))
        self.assertIn("B", standards.format_size(3))

    def test_import_copies_without_touching_source(self) -> None:
        src = write(os.path.join(self.tmp.name, "外部", "GB-T 14549.pdf"))
        folder = standards.library_dir(self.root, create=True)
        saved = standards.import_file(src, folder)
        self.assertTrue(os.path.isfile(saved))
        self.assertEqual(os.path.dirname(saved), folder)
        self.assertTrue(os.path.isfile(src), "只复制，源文件不动")
        with open(saved, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "标准正文")

    def test_import_same_name_does_not_overwrite(self) -> None:
        folder = standards.library_dir(self.root, create=True)
        src = write(os.path.join(self.tmp.name, "a.pdf"), "第一份")
        first = standards.import_file(src, folder)
        second = standards.import_file(src, folder)
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith("(2).pdf"), second)
        with open(first, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "第一份")

    def test_import_missing_file_raises(self) -> None:
        folder = standards.library_dir(self.root, create=True)
        with self.assertRaises(OSError):
            standards.import_file(os.path.join(self.tmp.name, "没有.pdf"), folder)
        with self.assertRaises(OSError):
            standards.import_file("", folder)


# --------------------------------------------------------------- 对话框


class AboutDialogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dialog = AboutDialog("储能电站（BESS）", self.tmp.name, self.tmp.name)
        self.addCleanup(self.dialog.deleteLater)

    def select_row(self, row: int) -> None:
        self.dialog.standard_table.selectRow(row)

    def test_two_tabs(self) -> None:
        labels = [self.dialog.tabs.tabText(i) for i in range(self.dialog.tabs.count())]
        self.assertEqual(labels, ["关于本软件", "标准与规范"])

    def test_about_text_has_version_and_license(self) -> None:
        from icomm import APP_VERSION

        text = self.dialog.about_label.text()
        self.assertIn(APP_VERSION, text)
        self.assertIn("GPL-3.0-or-later", text)

    def test_all_standards_are_listed_and_relevant_marked(self) -> None:
        rows = standard_rows(self.dialog)
        self.assertEqual(len(rows), len(standards.STANDARDS))
        self.assertIn("★", self.dialog.standard_table.item(0, 0).text(), "相关的组排最前并标星")
        self.assertIn("储能", self.dialog.relevant_label.text())

    def test_selecting_a_row_gives_its_code(self) -> None:
        self.select_row(0)
        code = self.dialog.selected_standard_code()
        self.assertTrue(code)
        self.assertIn(code, [item.code for item in standards.STANDARDS])

    def test_open_standard_page_uses_official_url(self) -> None:
        self.select_row(0)
        code = self.dialog.selected_standard_code()
        with mock.patch.object(QDesktopServices, "openUrl", mock.Mock(return_value=True)) as opened:
            self.dialog.open_standard_page()
        self.assertTrue(opened.called)
        url = opened.call_args[0][0]
        self.assertIsInstance(url, QUrl)
        self.assertEqual(url.toString(QUrl.FullyEncoded), standards.search_url(code))

    def test_copy_code_reaches_clipboard(self) -> None:
        self.select_row(0)
        code = self.dialog.selected_standard_code()
        self.dialog.copy_standard_code()
        self.assertEqual(QApplication.clipboard().text(), code)

    def test_library_starts_empty_with_hint(self) -> None:
        self.assertEqual(self.dialog.library_list.count(), 0)
        self.assertTrue(self.dialog.library_empty.isVisibleTo(self.dialog) or
                        not self.dialog.library_empty.isHidden())

    def test_import_files_lands_in_project_and_refreshes(self) -> None:
        source = write(os.path.join(self.tmp.name, "外部", "GB-T 14549.pdf"))
        saved = self.dialog.import_paths([source])
        self.assertEqual(len(saved), 1)
        folder = os.path.join(self.tmp.name, standards.STANDARDS_DIR)
        self.assertEqual(os.path.dirname(saved[0]), folder)
        self.assertEqual(self.dialog.library_list.count(), 1)
        self.assertIn("GB-T 14549.pdf", self.dialog.library_list.item(0).text())
        self.assertTrue(self.dialog.library_empty.isHidden())

    def test_import_twice_keeps_both(self) -> None:
        source = write(os.path.join(self.tmp.name, "a.pdf"))
        self.dialog.import_paths([source])
        self.dialog.import_paths([source])
        self.assertEqual(self.dialog.library_list.count(), 2)

    def test_import_nothing_does_nothing(self) -> None:
        self.assertEqual(self.dialog.import_paths([]), [])
        self.assertEqual(self.dialog.library_list.count(), 0)

    def test_import_failure_is_reported_not_swallowed(self) -> None:
        with mock.patch.object(standards, "import_file", side_effect=OSError("磁盘满了")), \
                mock.patch.object(QApplication, "processEvents", lambda *a, **k: None), \
                mock.patch("icomm.ui.dialogs.about.QMessageBox.warning") as warned:
            self.dialog.import_paths([write(os.path.join(self.tmp.name, "a.pdf"))])
        self.assertTrue(warned.called)

    def test_open_file_and_folder_go_through_shell(self) -> None:
        source = write(os.path.join(self.tmp.name, "a.pdf"))
        self.dialog.import_paths([source])
        self.dialog.library_list.setCurrentRow(0)
        with mock.patch("icomm.core.shell.open_path") as opened:
            self.dialog.open_library_file()
            self.dialog.open_library_folder()
        self.assertEqual(opened.call_count, 2)
        first = opened.call_args_list[0][0][0]
        self.assertTrue(first.endswith("a.pdf"))
        self.assertTrue(opened.call_args_list[1][0][0].endswith(standards.STANDARDS_DIR))

    def test_open_file_without_selection_is_noop(self) -> None:
        with mock.patch("icomm.core.shell.open_path") as opened:
            self.dialog.open_library_file()
        self.assertFalse(opened.called)


class AboutFromMainWindowTest(unittest.TestCase):
    def test_menu_action_opens_dialog_with_project_and_paths(self) -> None:
        from icomm.ui.main_window import MainWindow

        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        project = project_io.bess_project()
        paths = project_store.create_project_dir(
            os.path.join(tmp.name, project_store.safe_name(project.name)), project
        )
        window = MainWindow(project, paths, workdir=tmp.name, base_dir=tmp.name)
        self.addCleanup(window.close)
        window.settings.confirm_on_exit = False

        captured: dict[str, object] = {}

        def fake_exec(dialog):
            captured["name"] = dialog.project_name
            captured["root"] = dialog.library_root
            captured["tabs"] = dialog.tabs.count()
            return 0

        with mock.patch("icomm.ui.dialogs.about.AboutDialog.exec", fake_exec):
            window._show_about()
        self.assertEqual(captured["name"], project.name)
        self.assertEqual(captured["root"], paths.root)
        self.assertEqual(captured["tabs"], 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
