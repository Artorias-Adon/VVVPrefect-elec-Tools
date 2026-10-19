"""内置操作手册（「关于 → 帮助」）：「帮助」窗口里的内容与渲染。

守住四条：

* 内容与代码对得上（章节里的路径、点号、方法名都是真实存在的）；
* 「工具一览」那张表与真实的工具注册表一一对应（加工具忘了改手册会红）；
* 渲染不泄露标记（``**`` 不许跑到 HTML 里）、不引用任何外部资源（离线可用、可打印）；
* 搜索能真过滤目录，筛空了要有话说，清空搜索要能还原；
* 另存为 HTML / Markdown 真的写出文件，编码是 UTF-8。
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import manual  # noqa: E402
from icomm.ui.dialogs.about import ManualDialog  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


# --------------------------------------------------------------- 内容


class ManualContentTest(unittest.TestCase):
    def test_has_enough_sections(self) -> None:
        self.assertGreaterEqual(len(manual.sections()), 15)

    def test_keys_and_titles_are_unique(self) -> None:
        keys = manual.keys()
        self.assertEqual(len(keys), len(set(keys)))
        titles = [entry.title for entry in manual.sections()]
        self.assertEqual(len(titles), len(set(titles)))

    def test_every_section_has_summary_and_blocks(self) -> None:
        for entry in manual.sections():
            with self.subTest(key=entry.key):
                self.assertTrue(entry.title.strip())
                self.assertGreaterEqual(len(entry.summary.strip()), 6)
                self.assertGreaterEqual(len(entry.blocks), 2)

    def test_every_section_has_real_text(self) -> None:
        """一章里至少 80 个字的正文 —— 空壳章节没法当手册用。"""
        for entry in manual.sections():
            with self.subTest(key=entry.key):
                body = entry.plain().replace(entry.title, "").replace(entry.summary, "")
                self.assertGreaterEqual(len(body.strip()), 80)

    def test_block_kinds_are_known(self) -> None:
        known = {"para", "bullets", "steps", "table", "note", "code"}
        for entry in manual.sections():
            for block in entry.blocks:
                with self.subTest(key=entry.key, kind=block.kind):
                    self.assertIn(block.kind, known)

    def test_tables_are_rectangular(self) -> None:
        for entry in manual.sections():
            for block in entry.blocks:
                if block.kind != "table":
                    continue
                with self.subTest(key=entry.key):
                    self.assertTrue(block.header)
                    for row in block.rows:
                        self.assertEqual(len(row), len(block.header))

    def test_no_empty_rows_or_items(self) -> None:
        """空单元格/空条目会让表格看起来像没填完。"""
        for entry in manual.sections():
            for block in entry.blocks:
                with self.subTest(key=entry.key, kind=block.kind):
                    for item in block.items:
                        self.assertTrue(item.strip())
                    for row in block.rows:
                        for cell in row:
                            self.assertTrue(cell.strip())

    def test_lookup_by_key(self) -> None:
        self.assertIsNotNone(manual.section("quickstart"))
        self.assertIsNone(manual.section("no-such-chapter"))

    def test_stats_matches_sections(self) -> None:
        chapters, blocks = manual.stats()
        self.assertEqual(chapters, len(manual.sections()))
        self.assertEqual(blocks, sum(len(entry.blocks) for entry in manual.sections()))

    def test_mentions_real_paths_and_methods(self) -> None:
        """跟代码对齐的硬事实：改功能时这些也得跟着改。"""
        everything = manual.to_markdown()
        for needle in (
            "VVPproject",
            "config/",
            "parameters.csv",
            "history.db",
            "energy.json",
            "reports/documents",
            "point.write",
            "open_plant",
            "0x04:停机",
            "起始地址",
            "F11",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, everything)


# --------------------------------------------------------------- 工具一览


def _chinese_number(text: str) -> int:
    """读「十九」「二十」这类小写中文数字（手册里只用到 1–99）。"""
    digits = {
        "零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    if text == "十":
        return 10
    if "十" in text:
        tens, _, ones = text.partition("十")
        return (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)
    return digits[text]


class ManualToolTableTest(unittest.TestCase):
    """「工具一览」那张表必须和真实的工具注册表一一对应。

    加 / 删 / 改名工具时忘了改手册，这里就会红——以前只有文字，没有东西盯着。
    """

    def setUp(self) -> None:
        from icomm.ui.tools import tools

        self.registry = {(tool.group, tool.name) for tool in tools()}
        section = manual.section("tools")
        self.assertIsNotNone(section)
        tables = [block for block in section.blocks if block.rows]
        self.assertEqual(len(tables), 1, "「工具一览」应当只有一张表")
        self.rows = tables[0].rows
        self.summary = section.summary

    def test_table_matches_registry(self) -> None:
        listed = {(row[0], row[1]) for row in self.rows}
        self.assertEqual(listed, self.registry)
        self.assertEqual(len(self.rows), len(self.registry))

    def test_summary_tool_count_matches_table(self) -> None:
        match = re.search(r"([零一二三四五六七八九十]+)个工具", self.summary)
        self.assertIsNotNone(match, f"摘要里要写清工具数量：{self.summary}")
        self.assertEqual(_chinese_number(match.group(1)), len(self.rows))

    def test_summary_group_count_matches_table(self) -> None:
        groups = {row[0] for row in self.rows}
        match = re.search(r"([零一二三四五六七八九十]+)个分组", self.summary)
        self.assertIsNotNone(match, f"摘要里要写清分组数量：{self.summary}")
        self.assertEqual(_chinese_number(match.group(1)), len(groups))


# --------------------------------------------------------------- 搜索


class ManualSearchTest(unittest.TestCase):
    def test_empty_query_returns_all(self) -> None:
        self.assertEqual([entry.key for entry in manual.search("")], manual.keys())
        self.assertEqual(len(manual.search("   ")), len(manual.sections()))

    def test_single_word(self) -> None:
        hits = [entry.key for entry in manual.search("丢包率")]
        self.assertIn("acquire", hits)
        self.assertNotIn("power", hits)

    def test_multiple_words_must_all_match(self) -> None:
        both = manual.search("丢包率 通信日志")
        self.assertTrue(both)
        for entry in both:
            text = entry.plain()
            self.assertIn("丢包率", text)
            self.assertIn("通信日志", text)

    def test_case_insensitive(self) -> None:
        lower = [entry.key for entry in manual.search("modbus")]
        upper = [entry.key for entry in manual.search("MODBUS")]
        self.assertEqual(lower, upper)
        self.assertTrue(lower)

    def test_no_match_is_empty(self) -> None:
        self.assertEqual(manual.search("zzz-不可能出现的词-zzz"), [])


# --------------------------------------------------------------- 渲染


class ManualRenderTest(unittest.TestCase):
    def test_inline_markup(self) -> None:
        self.assertEqual(
            manual._inline_all("用 `code` 和 **重点**"),
            "用 <code>code</code> 和 <b>重点</b>",
        )

    def test_inline_escapes_html(self) -> None:
        out = manual._inline_all("a<b & c>d")
        self.assertIn("&lt;b", out)
        self.assertIn("&amp;", out)
        self.assertNotIn("<b", out)

    def test_lone_double_star_is_left_alone(self) -> None:
        """落单的 ``**`` 不许把后面整段吞成粗体（真踩过）。"""
        out = manual._inline_all("**落单 与 a<b")
        self.assertNotIn("<b>", out)
        self.assertIn("**落单", out)

    def test_body_html_has_title_and_escapes(self) -> None:
        entry = manual.section("quickstart")
        html = manual.body_html(entry)
        self.assertIn(entry.title, html)
        self.assertNotIn("**", html)

    def test_body_html_table_has_border(self) -> None:
        entry = manual.section("paramtable")
        html = manual.body_html(entry)
        self.assertIn("<table", html)
        self.assertIn("border=", html)

    def test_document_html_is_self_contained(self) -> None:
        html = manual.document_html(app_name="VVPhmi", app_version="9.9.9")
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn('charset="utf-8"', html)
        self.assertIn("VVPhmi 操作手册", html)
        self.assertIn("9.9.9", html)
        for external in ("http://", "https://", "<img", "<script"):
            with self.subTest(external=external):
                self.assertNotIn(external, html)

    def test_document_html_covers_every_section(self) -> None:
        html = manual.document_html()
        for entry in manual.sections():
            with self.subTest(key=entry.key):
                self.assertIn(f'id="{entry.key}"', html)
                self.assertIn(entry.title, html)

    def test_document_html_has_print_rules(self) -> None:
        self.assertIn("@media print", manual.document_html())

    def test_markdown_structure(self) -> None:
        text = manual.to_markdown(app_name="VVPhmi", app_version="9.9.9")
        self.assertIn("VVPhmi 操作手册", text)
        self.assertIn("9.9.9", text)
        for entry in manual.sections():
            with self.subTest(key=entry.key):
                self.assertIn(f"## {entry.title}", text)
        self.assertEqual(text.count("```") % 2, 0, "代码块围栏要成对")
        self.assertIn("|---|", text, "表格要有分隔行")

    def test_default_filename(self) -> None:
        self.assertEqual(manual.default_filename(".html", app_name="VVPhmi"), "VVPhmi-操作手册.html")
        self.assertEqual(manual.default_filename(".md"), "操作手册.md")


class ManualSaveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def test_save_html(self) -> None:
        target = os.path.join(self.tmp.name, "manual", "handbook.html")
        saved = manual.save_html(target, app_name="VVPhmi", app_version="1.0.0")
        self.assertEqual(saved, target)
        with open(saved, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("<!DOCTYPE html>", text)
        self.assertIn("VVPhmi 操作手册", text)

    def test_saved_file_has_no_bom(self) -> None:
        target = manual.save_html(os.path.join(self.tmp.name, "a.html"))
        with open(target, "rb") as handle:
            self.assertFalse(handle.read(3).startswith(b"\xef\xbb\xbf"))

    def test_save_markdown(self) -> None:
        target = os.path.join(self.tmp.name, "handbook.md")
        saved = manual.save_markdown(target, app_name="VVPhmi")
        with open(saved, encoding="utf-8") as handle:
            self.assertIn("# 操作手册", handle.read())


# --------------------------------------------------------------- 界面


class ManualDialogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.dialog = ManualDialog("储能电站（BESS）", self.tmp.name, self.tmp.name)
        self.addCleanup(self.dialog.deleteLater)

    def test_dialog_is_manual_only(self) -> None:
        """「帮助」是独立窗口：标题就是手册，不再带三个页签。"""
        self.assertEqual(self.dialog.windowTitle(), manual.MANUAL_TITLE)
        self.assertFalse(hasattr(self.dialog, "tabs"), "手册窗口不该再有页签")

    def test_toc_matches_sections(self) -> None:
        self.assertEqual(self.dialog.manual_list.count(), len(manual.sections()))
        first = self.dialog.manual_list.item(0)
        self.assertEqual(str(first.data(0x0100)), manual.sections()[0].key)

    def test_first_section_is_shown(self) -> None:
        self.assertEqual(self.dialog.current_manual_key(), manual.sections()[0].key)
        self.assertIn(manual.sections()[0].title, self.dialog.manual_view.toPlainText())

    def test_show_section(self) -> None:
        self.dialog.show_manual_section("api")
        text = self.dialog.manual_view.toPlainText()
        self.assertIn("point.write", text)
        self.assertIn("只读", text)

    def test_show_unknown_section_keeps_current(self) -> None:
        before = self.dialog.current_manual_key()
        self.dialog.show_manual_section("no-such-chapter")
        self.assertEqual(self.dialog.current_manual_key(), before)

    def test_filter_hides_other_chapters(self) -> None:
        self.dialog.filter_manual("丢包率")
        visible = [
            str(self.dialog.manual_list.item(row).data(0x0100))
            for row in range(self.dialog.manual_list.count())
            if not self.dialog.manual_list.item(row).isHidden()
        ]
        self.assertIn("acquire", visible)
        self.assertLess(len(visible), len(manual.sections()))
        self.assertIn("筛出", self.dialog.manual_count.text())

    def test_filter_selects_first_hit(self) -> None:
        self.dialog.filter_manual("分时电价")
        expected = manual.search("分时电价")[0].key
        self.assertEqual(self.dialog.current_manual_key(), expected)
        self.assertIn("power", [entry.key for entry in manual.search("分时电价")])

    def test_filter_without_hits_says_so(self) -> None:
        self.dialog.filter_manual("zzz-没有这一章-zzz")
        self.assertIn("没有章节匹配", self.dialog.manual_view.toPlainText())
        for row in range(self.dialog.manual_list.count()):
            self.assertTrue(self.dialog.manual_list.item(row).isHidden())

    def test_clearing_filter_restores_all(self) -> None:
        self.dialog.filter_manual("丢包率")
        self.dialog.filter_manual("")
        for row in range(self.dialog.manual_list.count()):
            self.assertFalse(self.dialog.manual_list.item(row).isHidden())
        self.assertIn("共", self.dialog.manual_count.text())

    def test_save_manual_html(self) -> None:
        target = os.path.join(self.tmp.name, "out", "manual.html")
        saved = self.dialog.save_manual(target)
        self.assertEqual(saved, target)
        self.assertTrue(os.path.exists(saved))
        self.assertIn("已保存", self.dialog.manual_status.text())

    def test_save_manual_markdown(self) -> None:
        target = os.path.join(self.tmp.name, "manual.md")
        saved = self.dialog.save_manual(target)
        self.assertTrue(saved.endswith(".md"))
        with open(saved, encoding="utf-8") as handle:
            self.assertIn("## 五分钟上手", handle.read())


if __name__ == "__main__":
    unittest.main()
