"""波形响应时间：核心算法 + 工具窗口。

算法拿**解析解**当基准（一阶 RC 的上升时间 = 2.197τ、建立时间 2% 带 = 3.912τ，
二阶 ζ=0.5 的超调 16.3%），比"看着差不多"可靠得多。
"""

from __future__ import annotations

import math
import os
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from icomm.core import analysis, project as project_io  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.core.tags import TagValue  # noqa: E402

_app: QApplication | None = None

DT = 1e-4
TAU = 1e-3


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def first_order(points: int = 500, pre: int = 100) -> list[float]:
    """先稳态再一阶上升（τ = 1ms）—— 现场录波最常见的样子。"""
    return [0.0] * pre + [1 - math.exp(-(i * DT) / TAU) for i in range(points - pre)]


def second_order(zeta: float = 0.5, wn: float = 2 * math.pi * 500,
                 points: int = 3000) -> list[float]:
    """标准二阶欠阻尼阶跃响应。"""
    wd = wn * math.sqrt(1 - zeta**2)
    return [
        1 - math.exp(-zeta * wn * i * DT) / math.sqrt(1 - zeta**2)
        * math.sin(wd * i * DT + math.acos(zeta))
        for i in range(points)
    ]


class StepResponseTest(unittest.TestCase):
    def test_rise_time_matches_first_order_theory(self) -> None:
        """一阶系统 10%→90% 上升时间 = ln9 · τ。"""
        result = analysis.step_response(first_order(), DT)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.rise_time, math.log(9) * TAU, delta=TAU * 0.05)

    def test_settle_time_within_two_percent(self) -> None:
        """±2% 误差带的建立时间 = ln50 · τ（误差只允许一个采样点的量化）。"""
        result = analysis.step_response(first_order(), DT)
        self.assertAlmostEqual(result.settle_time, math.log(50) * TAU, delta=1.5 * DT)

    def test_delay_time_matches_first_order_theory(self) -> None:
        """延迟时间（到 10%）= ln(1/0.9) · τ，允许一个采样点的误差。"""
        result = analysis.step_response(first_order(), DT)
        self.assertAlmostEqual(result.delay_time, math.log(10 / 9) * TAU, delta=2 * DT)

    def test_baseline_and_final(self) -> None:
        result = analysis.step_response(first_order(), DT)
        self.assertAlmostEqual(result.baseline, 0.0, places=3)
        self.assertAlmostEqual(result.final, 1.0, delta=0.01)

    def test_second_order_overshoot(self) -> None:
        """ζ=0.5 的超调是 exp(-πζ/√(1-ζ²)) = 16.3%。"""
        result = analysis.step_response(second_order(), DT)
        self.assertAlmostEqual(result.overshoot_pct, 16.3, delta=0.5)
        expected_peak = math.pi / (2 * math.pi * 500 * math.sqrt(1 - 0.25))
        self.assertAlmostEqual(result.peak_time, expected_peak, delta=10 * DT)

    def test_falling_edge_is_supported(self) -> None:
        """下跳也能算：幅值为负，"上升时间"照样是 10%→90% 那段。"""
        values = [1.0] * 100 + [math.exp(-(i * DT) / TAU) for i in range(400)]
        result = analysis.step_response(values, DT)
        self.assertIsNotNone(result)
        self.assertLess(result.span, 0)
        self.assertAlmostEqual(result.rise_time, math.log(9) * TAU, delta=TAU * 0.05)

    def test_custom_thresholds(self) -> None:
        """阈值可以改（20%→80% 也常用）。"""
        result = analysis.step_response(first_order(), DT, low=0.2, high=0.8)
        expected = (math.log(5) - math.log(1.25)) * TAU
        self.assertAlmostEqual(result.rise_time, expected, delta=TAU * 0.05)

    def test_flat_wave_is_rejected(self) -> None:
        self.assertIsNone(analysis.step_response([5.0] * 500, DT))

    def test_ripple_is_rejected(self) -> None:
        """只有纹波、没有阶跃时不能硬编一个数字出来。"""
        values = [1.0 + 0.001 * math.sin(i / 3) for i in range(500)]
        self.assertIsNone(analysis.step_response(values, DT))

    def test_too_few_points(self) -> None:
        self.assertIsNone(analysis.step_response([0.0, 1.0], DT))
        self.assertIsNone(analysis.step_response([], DT))

    def test_bad_dt(self) -> None:
        self.assertIsNone(analysis.step_response(first_order(), 0.0))
        self.assertIsNone(analysis.step_response(first_order(), None))

    def test_noisy_step_still_measurable(self) -> None:
        """带 1% 噪声的阶跃也要能算出个八九不离十。"""
        import random

        random.seed(7)
        values = [value + random.gauss(0, 0.01) for value in first_order()]
        result = analysis.step_response(values, DT)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result.rise_time, math.log(9) * TAU, delta=TAU * 0.25)

    def test_rows_are_presentable(self) -> None:
        result = analysis.step_response(first_order(), DT)
        rows = dict(result.rows())
        self.assertIn("上升时间", rows)
        self.assertIn("建立时间", rows)
        self.assertIn("超调量", rows)
        self.assertTrue(all(isinstance(text, str) and text for text in rows.values()))

    def test_format_time_units(self) -> None:
        self.assertEqual(analysis.format_time(5e-6), "5.0 µs")
        self.assertEqual(analysis.format_time(2.5e-3), "2.500 ms")
        self.assertEqual(analysis.format_time(1.5), "1.5000 s")
        self.assertEqual(analysis.format_time(None), "--")


class ToolWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()
        self.context = self._context()
        self.window = self._window()

    def _context(self, values: list[float] | None = None):
        from icomm.ui.tools.base import ToolContext

        payload = list(values or first_order())
        return ToolContext(
            project=self.project,
            window=None,
            settings=AppSettings(),
            tag_of=lambda pid: TagValue(pid=pid, value=list(payload), quality="good"),
            report_dir=self.tmp.name,
        )

    def _window(self, values: list[float] | None = None):
        from icomm.ui.tools.response_time import ResponseTimeWindow

        context = self.context if values is None else self._context(values)
        window = ResponseTimeWindow(context)
        window.resize(800, 600)
        window.auto_box.setChecked(False)
        self.addCleanup(window.deleteLater)
        return window

    def test_lists_waveform_points(self) -> None:
        """工程里的数组点位都要出现在来源列表里。"""
        arrays = [p for _d, p in self.project.iter_points() if p.is_array]
        self.assertTrue(arrays)
        self.assertGreaterEqual(self.window.source_combo.count(), len(arrays))

    def test_computes_and_shows_metrics(self) -> None:
        self.window.recompute()
        rows = {
            self.window.table.item(row, 0).text(): self.window.table.item(row, 1).text()
            for row in range(self.window.table.rowCount())
        }
        self.assertIn("上升时间", rows)
        self.assertTrue(rows["上升时间"].endswith("ms"), rows["上升时间"])

    def test_cursors_mark_the_rise_time(self) -> None:
        """两个光标之间那段 Δt 就是上升时间，方便和表格互相印证。"""
        self.window.recompute()
        canvas = self.window.canvas
        self.assertGreater(canvas.cursor_b, canvas.cursor_a)
        result = self.window._result
        self.assertIsNotNone(result)
        span = (canvas.cursor_b - canvas.cursor_a) * result.duration
        self.assertAlmostEqual(span, result.rise_time, delta=1e-6)

    def test_flat_wave_shows_hint_not_numbers(self) -> None:
        window = self._window([1.0] * 500)
        window.recompute()
        self.assertIsNone(window._result)
        self.assertIn("算不出响应时间", window.hint.text())
        self.assertEqual(window.table.item(0, 0).text(), "（算不出来）")

    def test_threshold_change_recomputes(self) -> None:
        self.window.recompute()
        before = self.window._result.rise_time
        self.window.low_box.setValue(0.2)
        self.window.high_box.setValue(0.8)
        after = self.window._result.rise_time
        self.assertLess(after, before, "20%→80% 的上升时间应该比 10%→90% 短")

    def test_refresh_keeps_selection(self) -> None:
        self.window.source_combo.setCurrentIndex(
            min(2, self.window.source_combo.count() - 1)
        )
        chosen = self.window.source_combo.currentData()
        self.window.reload_sources()
        self.assertEqual(self.window.source_combo.currentData(), chosen)

    def test_export_writes_csv(self) -> None:
        path = os.path.join(self.tmp.name, "r.csv")
        original = QFileDialog.getSaveFileName
        QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (path, ""))
        try:
            self.window.recompute()
            self.window.export_csv()
        finally:
            QFileDialog.getSaveFileName = original
        with open(path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
        self.assertIn("上升时间", text)
        self.assertIn("建立时间", text)
        self.assertIn("阈值", text)

    def test_export_without_result_warns(self) -> None:
        """没有结果时只提示一句，不能抛异常。"""
        from PySide6.QtWidgets import QMessageBox

        seen: list[str] = []
        original = QMessageBox.information
        QMessageBox.information = staticmethod(
            lambda _parent, _title, text, *a, **k: seen.append(text)
        )
        try:
            window = self._window([1.0] * 500)
            window.recompute()
            window.export_csv()
        finally:
            QMessageBox.information = original
        self.assertEqual(len(seen), 1)

    def test_live_source_follows_tag(self) -> None:
        """跟随实时：实时表里的波形换了，窗口里的指标也要跟着换。"""
        canvas_channel = self.window.canvas.channel("响应")
        self.assertIsNotNone(canvas_channel)
        self.window.recompute()
        first = self.window._result.rise_time
        payload = [0.0] * 100 + [1 - math.exp(-(i * DT) / (5 * TAU)) for i in range(400)]
        self.window.context.tag_of = lambda pid: TagValue(
            pid=pid, value=list(payload), quality="good"
        )
        self.window.recompute()
        self.assertGreater(self.window._result.rise_time, first * 3)


class ToolRegisteredTest(unittest.TestCase):
    def test_tool_is_in_registry(self) -> None:
        """工具要在「工具」菜单里能找到。"""
        from icomm.ui import tools as ui_tools

        tool = ui_tools.find_tool("波形响应时间")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.group, "计算")


class RecordedWaveformTest(unittest.TestCase):
    """另一条来源：从录波历史里取一段来算。"""

    def setUp(self) -> None:
        from icomm.storage.db import Storage
        from icomm.ui.tools.base import ToolContext
        from icomm.ui.tools.response_time import ResponseTimeWindow

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(os.path.join(self.tmp.name, "h.db"))
        self.storage.start()
        self.addCleanup(self.storage.stop)
        self.storage.add_waveform("d.波形", first_order(), int(DT * 1e6))
        deadline = time.time() + 5
        while time.time() < deadline and not self.storage.query_waveform_index():
            time.sleep(0.05)

        context = ToolContext(
            project=project_io.bess_project(),
            window=None,
            settings=AppSettings(),
            storage=self.storage,
            report_dir=self.tmp.name,
        )
        self.window = ResponseTimeWindow(context)
        self.window.auto_box.setChecked(False)
        self.addCleanup(self.window.deleteLater)

    def test_recorded_waveform_is_listed_and_computed(self) -> None:
        keys = [
            self.window.source_combo.itemData(index)
            for index in range(self.window.source_combo.count())
        ]
        recorded = [key for key in keys if str(key).startswith("wave:")]
        self.assertTrue(recorded, keys)
        index = keys.index(recorded[0])
        self.window.source_combo.setCurrentIndex(index)
        self.window.recompute()
        self.assertIsNotNone(self.window._result)
        self.assertAlmostEqual(
            self.window._result.rise_time, math.log(9) * TAU, delta=TAU * 0.05
        )


if __name__ == "__main__":
    unittest.main()
