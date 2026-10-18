"""报告生成（骨架）：小节取数、渲染、存盘，以及「设备与点位」下面的那个页面。

盯住这几件事：

* **每一节都能真的跑通**（用示例工程把所有小节全开 —— 这一条抓到过真实 bug：
  某小节里把 ``@property`` 当函数调，报告里只剩一句"生成失败"却没人注意）；
* **缺数据不崩**：没采集、没历史库、取实时值抛异常，都只是少内容，不是报错；
* **不静默丢数据**：表格超上限要截断并注明；
* **渲染安全**：工程名/备注里的 ``<>`` 要转义，Markdown 里 ``|`` 要逃；
* **界面接线**：导航里排在「设备与点位」下面、勾选框与 core 小节同源、存盘走信号。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from icomm.core import project as project_io
from icomm.core import report as report_core
from icomm.core.models import Project
from icomm.core.tags import DeviceStatus
from icomm.storage import Storage

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def sample_project() -> Project:
    return project_io.bess_project()


def wait_flush(storage: Storage, want: int, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    stats = storage.stats()
    while time.time() < deadline and stats["alarms"] < want:
        time.sleep(0.05)
        stats = storage.stats()
    return stats


# --------------------------------------------------------------- 小节目录


class SectionsTest(unittest.TestCase):
    def test_registry_is_consistent(self) -> None:
        keys = report_core.section_keys()
        self.assertEqual(len(keys), len(set(keys)), "小节 key 不能重复")
        choices = report_core.section_choices()
        self.assertEqual([key for key, _title, _doc in choices], keys)
        for key, title, doc in choices:
            self.assertTrue(title.strip(), key)
            self.assertTrue(doc.strip(), f"{key} 缺说明（界面上的 tooltip 靠它）")
        self.assertTrue(set(report_core.default_sections()) <= set(keys))

    def test_every_section_runs_on_sample_project(self) -> None:
        """全开所有小节跑一遍：任何一节"生成失败"都算 bug。"""
        project = sample_project()
        options = report_core.ReportOptions(
            sections=tuple(report_core.section_keys()), notes="这一版报告的内容还没定稿"
        )
        report = report_core.build_report(report_core.ReportContext(project=project, options=options))
        self.assertEqual([section.key for section in report.sections], report_core.section_keys())
        for section in report.sections:
            joined = "\n".join(section.paragraphs)
            self.assertNotIn("生成失败", joined, f"{section.key}: {joined}")
            self.assertFalse(section.empty, f"{section.key} 是空的（示例工程里不该空）")

    def test_empty_notes_section_is_dropped(self) -> None:
        """没写备注就不出「备注」这一节（空标题很丑）。"""
        project = sample_project()
        options = report_core.ReportOptions(sections=tuple(report_core.section_keys()))
        report = report_core.build_report(report_core.ReportContext(project=project, options=options))
        self.assertNotIn("notes", [section.key for section in report.sections])


class OverviewTest(unittest.TestCase):
    def test_counts(self) -> None:
        project = sample_project()
        ctx = report_core.ReportContext(project=project)
        section = report_core.SECTIONS[0][3](ctx)
        kv = dict(section.kv)
        self.assertEqual(kv["工程名称"], project.name)
        self.assertEqual(kv["点位数"], str(len(project.all_point_ids())))
        self.assertEqual(kv["通道数"], str(len(project.channels)))
        self.assertEqual(kv["设备数"], str(len(project.devices)))

    def test_time_range_is_reflected(self) -> None:
        project = sample_project()
        options = report_core.ReportOptions(time_from=1000.0, time_to=2000.0)
        section = report_core.SECTIONS[0][3](report_core.ReportContext(project=project, options=options))
        self.assertIn("统计时间范围", dict(section.kv))


class PointsTest(unittest.TestCase):
    def _section(self, options: report_core.ReportOptions | None = None):
        project = sample_project()
        ctx = report_core.ReportContext(project=project, options=options or report_core.ReportOptions())
        return project, report_core.SECTIONS[3][3](ctx)

    def test_rows_match_points(self) -> None:
        project, section = self._section()
        table = section.tables[0]
        self.assertEqual(len(table.rows), len(project.all_point_ids()))
        pids = {row[0] for row in table.rows}
        self.assertIn("储能变流器PCS.有功功率给定", pids)

    def test_write_limit_column_uses_property(self) -> None:
        """写值范围那一列要真的填上（``has_write_limit`` 是 property，别当函数调）。"""
        _project, section = self._section()
        table = section.tables[0]
        limits_index = table.columns.index("写值范围")
        rows = {row[0]: row for row in table.rows}
        self.assertNotEqual(rows["储能变流器PCS.有功功率给定"][limits_index], "--")
        self.assertEqual(rows["储能变流器PCS.直流母线电压"][limits_index], "--")

    def test_truncation_is_noted(self) -> None:
        _project, section = self._section(report_core.ReportOptions(max_rows=3))
        table = section.tables[0]
        self.assertEqual(len(table.rows), 3)
        self.assertIn("只列前 3 行", table.note)


class LiveAndCommsTest(unittest.TestCase):
    def test_live_without_values_explains(self) -> None:
        project = sample_project()
        section = report_core.build_report(
            report_core.ReportContext(project=project, options=report_core.ReportOptions(
                sections=("live",)))
        ).sections[0]
        self.assertIn("没有实时数据", "".join(section.paragraphs))

    def test_live_counts_quality(self) -> None:
        project = sample_project()
        values = {
            "储能变流器PCS.有功功率给定": {"value": 120.0, "quality": "good", "timestamp": 1700000000.0},
            "储能变流器PCS.直流母线电压": {"value": None, "quality": "bad", "timestamp": 0.0,
                                          "error": "无响应"},
        }
        section = report_core.build_report(
            report_core.ReportContext(project=project, values=values,
                                      options=report_core.ReportOptions(sections=("live",)))
        ).sections[0]
        kv = dict(section.kv)
        self.assertEqual(kv["质量良好"], "1")
        self.assertEqual(kv["质量异常"], "1")
        rows = {row[0]: row for row in section.tables[0].rows}
        self.assertIn("120", rows["储能变流器PCS.有功功率给定"][1])

    def test_comms_from_device_status(self) -> None:
        project = sample_project()
        status = DeviceStatus(name="储能变流器PCS")
        status.mark_ok(12.0)
        status.mark_error("无响应")
        section = report_core.build_report(
            report_core.ReportContext(project=project, statuses=[status],
                                      options=report_core.ReportOptions(sections=("comms",)))
        ).sections[0]
        row = section.tables[0].rows[0]
        self.assertEqual(row[0], "储能变流器PCS")
        self.assertEqual(row[2], "2", "两轮请求")
        self.assertEqual(row[3], "50.0%", "一轮没应答 -> 累计丢包 50%")

    def test_comms_without_status(self) -> None:
        project = sample_project()
        section = report_core.build_report(
            report_core.ReportContext(project=project, options=report_core.ReportOptions(
                sections=("comms",)))
        ).sections[0]
        self.assertIn("没有通信状态", "".join(section.paragraphs))


class AlarmsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(os.path.join(self.tmp.name, "history.db"))
        self.storage.start()
        self.addCleanup(self.storage.stop)

    def test_without_storage_explains(self) -> None:
        project = sample_project()
        section = report_core.build_report(
            report_core.ReportContext(project=project, options=report_core.ReportOptions(
                sections=("alarms",)))
        ).sections[0]
        self.assertIn("没有历史库", "".join(section.paragraphs))

    def test_counts_and_rows(self) -> None:
        now = time.time()
        self.storage.add_alarm("PCS.P", "PCS", "P", "critical", "过温", 95.0, True, False, now - 30)
        self.storage.add_alarm("PCS.P", "PCS", "P", "warning", "偏高", 80.0, False, True, now - 20)
        wait_flush(self.storage, 2)
        project = sample_project()
        section = report_core.build_report(
            report_core.ReportContext(project=project, storage=self.storage,
                                      options=report_core.ReportOptions(sections=("alarms",)))
        ).sections[0]
        kv = dict(section.kv)
        self.assertEqual(kv["记录条数"], "2")
        self.assertEqual(kv["未复位"], "1")
        self.assertEqual(kv["级别 critical"], "1")
        self.assertEqual(len(section.tables[0].rows), 2)

    def test_time_filter(self) -> None:
        now = time.time()
        self.storage.add_alarm("PCS.P", "PCS", "P", "warning", "旧的", 1.0, True, False, now - 7200)
        self.storage.add_alarm("PCS.P", "PCS", "P", "warning", "新的", 2.0, True, False, now - 10)
        wait_flush(self.storage, 2)
        project = sample_project()
        section = report_core.build_report(
            report_core.ReportContext(
                project=project, storage=self.storage,
                options=report_core.ReportOptions(sections=("alarms",), time_from=now - 600,
                                                  time_to=now),
            )
        ).sections[0]
        self.assertEqual(dict(section.kv)["记录条数"], "1", "只留最近 10 分钟内那条")

    def test_storage_failure_is_reported_not_raised(self) -> None:
        broken = mock.Mock()
        broken.query_alarms.side_effect = RuntimeError("库被占着")
        project = sample_project()
        section = report_core.build_report(
            report_core.ReportContext(project=project, storage=broken,
                                      options=report_core.ReportOptions(sections=("alarms",)))
        ).sections[0]
        self.assertIn("库被占着", "".join(section.paragraphs))


class NotesAndSelectionTest(unittest.TestCase):
    def test_notes_skipped_when_empty(self) -> None:
        project = sample_project()
        report = report_core.build_report(
            report_core.ReportContext(project=project, options=report_core.ReportOptions())
        )
        self.assertNotIn("notes", [section.key for section in report.sections])

    def test_notes_included_when_written(self) -> None:
        project = sample_project()
        report = report_core.build_report(
            report_core.ReportContext(project=project, options=report_core.ReportOptions(
                sections=("overview", "notes"), notes="换了 A 相 CT\n下次复测"))
        )
        notes = report.sections[-1]
        self.assertEqual(notes.key, "notes")
        self.assertEqual(len(notes.paragraphs), 2)

    def test_only_requested_sections(self) -> None:
        project = sample_project()
        report = report_core.build_report(
            report_core.ReportContext(project=project, options=report_core.ReportOptions(
                sections=("overview",)))
        )
        self.assertEqual([section.key for section in report.sections], ["overview"])

    def test_unknown_section_raises(self) -> None:
        project = sample_project()
        with self.assertRaises(ValueError):
            report_core.build_report(
                report_core.ReportContext(project=project, options=report_core.ReportOptions(
                    sections=("没有这一节",)))
            )

    def test_broken_section_does_not_kill_report(self) -> None:
        def boom(_ctx):
            raise RuntimeError("这一节坏了")

        def fine(ctx):
            return report_core.SectionResult(key="ok", title="正常", paragraphs=["还行"])

        fake = (("broken", "坏的一节", "故意坏", boom), ("ok", "正常", "正常小节", fine))
        project = sample_project()
        with mock.patch.object(report_core, "SECTIONS", fake):
            report = report_core.build_report(
                report_core.ReportContext(project=project,
                                          options=report_core.ReportOptions(sections=("broken", "ok")))
            )
        self.assertEqual([section.key for section in report.sections], ["broken", "ok"])
        self.assertIn("这一节生成失败", report.sections[0].paragraphs[0])
        self.assertEqual(report.sections[1].paragraphs, ["还行"])


# --------------------------------------------------------------- 渲染 / 存盘


class RenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = sample_project()
        self.report = report_core.build_report(
            report_core.ReportContext(
                project=self.project,
                options=report_core.ReportOptions(title="交接报告", notes="备注里有 <b>尖括号</b>"),
            )
        )

    def test_html_is_self_contained(self) -> None:
        text = self.report.to_html()
        self.assertTrue(text.startswith("<!DOCTYPE html>"))
        self.assertIn("交接报告", text)
        self.assertIn("<table", text)
        self.assertNotIn("<script", text.lower())
        self.assertNotIn("http://", text, "不能引用外部资源（离线现场打不开）")

    def test_html_escapes_dynamic_text(self) -> None:
        text = self.report.to_html()
        self.assertNotIn("<b>尖括号</b>", text)
        self.assertIn("&lt;b&gt;", text)

    def test_markdown_structure(self) -> None:
        text = self.report.to_markdown()
        self.assertTrue(text.startswith("# 交接报告"))
        self.assertIn("## 工程概况", text)
        self.assertIn("| 点号 |", text)
        self.assertIn(f"工程：{self.project.name}", text)

    def test_write_html_and_markdown(self) -> None:
        for ext in ("html", "md"):
            with self.subTest(ext=ext):
                path = os.path.join(self.tmp.name, "sub", f"r.{ext}")
                written = self.report.write(path)
                self.assertEqual(written, path)
                self.assertTrue(os.path.exists(path))
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
                self.assertIn("交接报告", text)
                if ext == "html":
                    self.assertIn("<table", text)
                else:
                    self.assertIn("| 点号 |", text)

    def test_unknown_extension_falls_back_to_html(self) -> None:
        path = os.path.join(self.tmp.name, "r.docx")
        self.report.write(path)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("<!DOCTYPE html>", text,
                      "现在只支持 html/md，其它扩展名按 html 存（别写出坏文件）")

    def test_default_name(self) -> None:
        name = self.report.default_name("html")
        self.assertTrue(name.endswith(".html"))
        self.assertIn(self.project.name, name)
        import re

        self.assertRegex(name, r"_\d{8}_\d{6}\.html$")

    def test_empty_report(self) -> None:
        empty = report_core.Report(options=report_core.ReportOptions(title="空"),
                                   project_name="P", generated_at=time.time(), app_version="x")
        self.assertTrue(empty.is_empty)
        self.assertIn("# 空", empty.to_markdown())


# --------------------------------------------------------------- 界面


class ReportPageTest(unittest.TestCase):
    def setUp(self) -> None:
        from icomm.ui.pages.report import ReportPage

        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = sample_project()
        self.page = ReportPage(self.project)
        self.addCleanup(self.page.deleteLater)
        self.saved: list[str] = []
        self.failed: list[str] = []
        self.page.saved.connect(self.saved.append)
        self.page.failed.connect(self.failed.append)

    def test_checkboxes_come_from_core(self) -> None:
        self.assertEqual(list(self.page.section_boxes), report_core.section_keys())
        checked = {key for key, box in self.page.section_boxes.items() if box.isChecked()}
        self.assertEqual(checked, set(report_core.default_sections()))

    def test_generate_preview(self) -> None:
        report = self.page.generate()
        self.assertIsNotNone(report)
        self.assertFalse(self.failed)
        self.assertIn("工程概况", self.page.preview.toPlainText())
        self.assertIn("已生成", self.page.status.text())

    def test_generate_with_nothing_checked(self) -> None:
        for box in self.page.section_boxes.values():
            box.setChecked(False)
        self.assertIsNone(self.page.generate())
        self.assertTrue(self.failed)
        self.assertIn("至少勾一项", self.page.status.text())

    def test_live_provider_failure_is_tolerated(self) -> None:
        def boom():
            raise RuntimeError("没引擎")

        self.page.set_live_provider(boom)
        report = self.page.generate()
        self.assertIsNotNone(report)
        self.assertTrue(any(section.key == "live" for section in report.sections))

    def test_live_provider_is_used(self) -> None:
        self.page.set_live_provider(lambda: (
            {"储能变流器PCS.有功功率给定": {"value": 33.0, "quality": "good", "timestamp": time.time()}},
            [],
        ))
        report = self.page.generate()
        live = [section for section in report.sections if section.key == "live"][0]
        self.assertEqual(dict(live.kv)["质量良好"], "1")

    def test_save_writes_file_and_emits(self) -> None:
        self.page.set_output_dir_provider(lambda: self.tmp.name)
        path = os.path.join(self.tmp.name, "out.html")
        with mock.patch("icomm.ui.pages.report.QFileDialog.getSaveFileName",
                        return_value=(path, "网页 (*.html)")):
            written = self.page.save()
        self.assertEqual(written, path)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.saved, [path])
        self.assertIn("已保存", self.page.status.text())

    def test_save_adds_extension_from_filter(self) -> None:
        target = os.path.join(self.tmp.name, "noext")
        with mock.patch("icomm.ui.pages.report.QFileDialog.getSaveFileName",
                        return_value=(target, "Markdown (*.md)")):
            written = self.page.save()
        self.assertEqual(written, target + ".md")
        self.assertTrue(os.path.exists(written))

    def test_save_cancelled_does_nothing(self) -> None:
        with mock.patch("icomm.ui.pages.report.QFileDialog.getSaveFileName",
                        return_value=("", "")):
            self.assertIsNone(self.page.save())
        self.assertEqual(self.saved, [])

    def test_default_output_dir_without_provider(self) -> None:
        self.assertEqual(self.page.default_output_dir(), os.getcwd())

    def test_options_carry_widget_values(self) -> None:
        self.page.title_edit.setText("  试验报告 ")
        self.page.rows_spin.setValue(500)
        self.page.notes_edit.setPlainText("  记一笔  ")
        options = self.page.make_options()
        self.assertEqual(options.title, "试验报告")
        self.assertEqual(options.max_rows, 500)
        self.assertEqual(options.notes, "记一笔")

    def test_time_range_combo(self) -> None:
        from icomm.ui.pages.report import TIME_RANGES

        self.page.range_combo.setCurrentIndex(0)
        self.assertEqual(self.page.time_range(), (None, None))
        self.page.range_combo.setCurrentIndex(1)
        start, end = self.page.time_range()
        self.assertIsNotNone(start)
        self.assertAlmostEqual(end - start, TIME_RANGES[1][1], delta=2)


class MainWindowReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def test_nav_and_page_are_wired(self) -> None:
        from icomm.core import project_store
        from icomm.ui.main_window import NAV_ITEMS, MainWindow

        project = sample_project()
        workdir = os.path.join(self.tmp.name, "work")
        paths = project_store.create_project_dir(
            os.path.join(workdir, project_store.safe_name(project.name)), project
        )
        window = MainWindow(project, paths, workdir=workdir, base_dir=self.tmp.name)
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)

        self.assertEqual(NAV_ITEMS[-2:], ["设备与点位", "报告生成"],
                         "报告生成要排在设备与点位下面")
        index = NAV_ITEMS.index("报告生成")
        self.assertIs(window.stack.widget(index), window.report_page,
                      "导航下标与页面下标必须一一对应")
        self.assertIs(window.report_page.storage, window.storage)

        # 点导航能真的切过去
        window.nav.setCurrentRow(index)
        self.assertIs(window.stack.currentWidget(), window.report_page)

        # 菜单里也有入口（编辑 → 生成报告…）
        texts = [action.text() for action in window.findChildren(type(window.action_start))]
        self.assertIn("生成报告…", texts)

        # 报告目录落在工程的报表下，且真的能生成
        out_dir = window.report_page.default_output_dir()
        self.assertIn("documents", out_dir.replace("\\", "/"))
        report = window.report_page.generate()
        self.assertIn("工程概况", [section.title for section in report.sections])

    def test_report_page_survives_theme_recreate(self) -> None:
        """换主题会整页重造；重造后报告页还得连着历史库、还能生成。"""
        from icomm.core import project_store
        from icomm.ui.main_window import MainWindow

        project = sample_project()
        workdir = os.path.join(self.tmp.name, "work2")
        paths = project_store.create_project_dir(
            os.path.join(workdir, project_store.safe_name(project.name)), project
        )
        window = MainWindow(project, paths, workdir=workdir, base_dir=self.tmp.name)
        self.addCleanup(window.close)
        self.addCleanup(window.deleteLater)

        old_page = window.report_page
        window._recreate_pages()
        self.assertIsNot(window.report_page, old_page)
        self.assertIs(window.report_page.storage, window.storage)
        self.assertIsNotNone(window.report_page.live_provider)
        report = window.report_page.generate()
        self.assertTrue(report.sections)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
