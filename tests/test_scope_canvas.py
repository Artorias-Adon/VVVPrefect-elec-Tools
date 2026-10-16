"""示波器画布/页面测试：光标拖动、时基缩放、归位。

盯住的是用户报过的两个现象和一条新需求：

* 光标 B 拖不动（原来是"右键放 B"，可右键被上下文菜单占了，
  拖拽时 `event.button()` 恒为 NoButton，于是永远只动 A）；
* 时基调短没反应（录波模式原来总是把整段波形铺满画面，`sec_per_div` 完全没参与），
  现在要能一直放大到"每格 1 个采样点"；
* "归位"一键把观察参数恢复默认。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core.tags import TagValue  # noqa: E402
from icomm.ui.pages.scope import ARRAY_POINTS_PER_DIV, TIMEBASES, ScopePage  # noqa: E402
from icomm.ui.scope_canvas import ScopeCanvas, ScopeChannel, format_seconds  # noqa: E402

_app: QApplication | None = None

WAVE_US = 100  # 示例工程里波形点位的采样间隔
WAVE_POINTS = 200


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _press(widget, x: float, y: float, button=Qt.LeftButton) -> None:
    widget.mousePressEvent(
        QMouseEvent(
            QEvent.MouseButtonPress,
            QPointF(x, y),
            QPointF(x, y),
            button,
            button,
            Qt.NoModifier,
        )
    )


def _move(widget, x: float, y: float, buttons=Qt.LeftButton) -> None:
    widget.mouseMoveEvent(
        QMouseEvent(
            QEvent.MouseMove, QPointF(x, y), QPointF(x, y), Qt.NoButton, buttons, Qt.NoModifier
        )
    )


def _release(widget, x: float, y: float) -> None:
    widget.mouseReleaseEvent(
        QMouseEvent(
            QEvent.MouseButtonRelease,
            QPointF(x, y),
            QPointF(x, y),
            Qt.LeftButton,
            Qt.NoButton,
            Qt.NoModifier,
        )
    )


class ScopeCanvasTest(unittest.TestCase):
    """画布本身：光标拖动、缩放窗口、时间刻度。"""

    def setUp(self) -> None:
        self.canvas = ScopeCanvas()
        self.canvas.resize(900, 400)
        self.canvas.set_channels(
            [
                ScopeChannel(
                    pid="d.波形",
                    name="d.波形",
                    unit="A",
                    color="#38bdf8",
                    visible=True,
                    kind="array",
                    sample_interval_us=WAVE_US,
                )
            ]
        )
        self.canvas.set_waveform(
            "d.波形", [float(index) for index in range(WAVE_POINTS)], WAVE_US
        )
        plot = self.canvas._plot_rect(self.canvas._margins())
        self.plot = plot

    def _x(self, ratio: float) -> float:
        return self.plot.left() + self.plot.width() * ratio

    # ------------------------------------------------------------ 光标

    def test_cursor_b_can_be_dragged(self) -> None:
        """光标 B 必须能拖（原来只有右键能放 B，等于拖不动）。"""
        _press(self.canvas, self._x(0.75), self.plot.center().y())
        _move(self.canvas, self._x(0.55), self.plot.center().y())
        self.assertAlmostEqual(self.canvas.cursor_b, 0.55, places=2)
        self.assertAlmostEqual(self.canvas.cursor_a, 0.25, places=2)

    def test_cursor_a_can_be_dragged(self) -> None:
        _press(self.canvas, self._x(0.25), self.plot.center().y())
        _move(self.canvas, self._x(0.40), self.plot.center().y())
        self.assertAlmostEqual(self.canvas.cursor_a, 0.40, places=2)
        self.assertAlmostEqual(self.canvas.cursor_b, 0.75, places=2)

    def test_drag_picks_nearest_cursor(self) -> None:
        """按在中间偏 B 就要抓 B，不能乱抓。"""
        _press(self.canvas, self._x(0.62), self.plot.center().y())
        _move(self.canvas, self._x(0.70), self.plot.center().y())
        self.assertAlmostEqual(self.canvas.cursor_b, 0.70, places=2)
        self.assertAlmostEqual(self.canvas.cursor_a, 0.25, places=2)

    def test_drag_stops_after_release(self) -> None:
        """松手以后再移动鼠标不能继续拖动（否则右键菜单一弹光标就飞了）。"""
        _press(self.canvas, self._x(0.75), self.plot.center().y())
        _move(self.canvas, self._x(0.55), self.plot.center().y())
        _release(self.canvas, self._x(0.55), self.plot.center().y())
        _move(self.canvas, self._x(0.90), self.plot.center().y(), buttons=Qt.NoButton)
        self.assertAlmostEqual(self.canvas.cursor_b, 0.55, places=2)

    def test_right_click_leaves_cursors_alone(self) -> None:
        """右键留给上下文菜单，不再拿来放光标 B。"""
        before = (self.canvas.cursor_a, self.canvas.cursor_b)
        _press(self.canvas, self._x(0.5), self.plot.center().y(), button=Qt.RightButton)
        self.assertEqual((self.canvas.cursor_a, self.canvas.cursor_b), before)

    def test_hover_near_cursor_hints_resize(self) -> None:
        """悬停在光标附近换成横向缩放形状，提示"这个能拖"。"""
        _move(self.canvas, self._x(0.75), self.plot.center().y(), buttons=Qt.NoButton)
        self.assertEqual(self.canvas.cursor().shape(), Qt.SizeHorCursor)
        _move(self.canvas, self._x(0.50), self.plot.center().y(), buttons=Qt.NoButton)
        self.assertEqual(self.canvas.cursor().shape(), Qt.ArrowCursor)

    # ------------------------------------------------------------ 缩放窗口

    def test_full_view_is_default(self) -> None:
        """默认要把整段波形铺满（老行为不能变）。"""
        first, last, window = self.canvas._array_view(WAVE_POINTS, WAVE_US / 1e6)
        self.assertEqual(first, 0)
        self.assertEqual(last, WAVE_POINTS - 1)
        self.assertAlmostEqual(window, (WAVE_POINTS - 1) * WAVE_US / 1e6, places=9)

    def test_zoom_shows_one_sample_per_division(self) -> None:
        """时基调到最短：一格正好一个采样点。"""
        self.canvas.set_timebase(WAVE_US / 1e6)
        first, last, window = self.canvas._array_view(WAVE_POINTS, WAVE_US / 1e6)
        self.assertEqual(first, 0)
        self.assertAlmostEqual(window, 10 * WAVE_US / 1e6, places=9)
        self.assertEqual(last, 10)  # 10 格 → 10 个点 + 1 个端点

    def test_zoom_shows_five_samples_per_division(self) -> None:
        self.canvas.set_timebase(5 * WAVE_US / 1e6)
        _first, last, window = self.canvas._array_view(WAVE_POINTS, WAVE_US / 1e6)
        self.assertAlmostEqual(window, 50 * WAVE_US / 1e6, places=9)
        self.assertEqual(last, 50)

    def test_zoom_never_goes_past_record(self) -> None:
        """放大 + 一直往右平移，窗口不能跑到记录外面。"""
        self.canvas.set_timebase(WAVE_US / 1e6)
        self.canvas.pan_samples = 10000
        self.canvas.clamp_pan()
        first, last, _window = self.canvas._array_view(WAVE_POINTS, WAVE_US / 1e6)
        self.assertEqual(last, WAVE_POINTS - 1)
        self.assertEqual(first, WAVE_POINTS - 1 - 10)
        self.assertEqual(self.canvas.pan_samples, first)

    def test_unknown_interval_falls_back_to_index(self) -> None:
        """没标采样间隔就退回"按点序号铺满"，不能崩也不能除零。"""
        self.canvas.set_waveform("d.波形", [1.0, 2.0, 3.0], 0)
        first, last, window = self.canvas._array_view(3, 0.0)
        self.assertEqual((first, last, window), (0, 2, 0.0))

    def test_pan_pixels_moves_window(self) -> None:
        self.canvas.set_timebase(WAVE_US / 1e6)
        self.canvas.pan_by_pixels(-self.plot.width() * 0.5)  # 往左拖 = 看后面
        self.assertGreater(self.canvas.pan_samples, 0)

    def test_sample_at_follows_window(self) -> None:
        """放大以后光标读数要按窗口取点，不然量出来的跳变位置是错的。"""
        self.canvas.set_timebase(WAVE_US / 1e6)
        self.assertEqual(self.canvas._sample_at(self.canvas.channels[0], 0.0), 0.0)
        self.assertEqual(self.canvas._sample_at(self.canvas.channels[0], 1.0), 10.0)

    def test_sample_at_follows_pan(self) -> None:
        self.canvas.set_timebase(WAVE_US / 1e6)
        self.canvas.pan_samples = 40
        self.assertEqual(self.canvas._sample_at(self.canvas.channels[0], 0.0), 40.0)

    def test_clear_pan_resets_window(self) -> None:
        self.canvas.set_timebase(WAVE_US / 1e6)
        self.canvas.pan_samples = 40
        self.canvas.clear_pan()
        self.assertEqual(self.canvas.pan_samples, 0)

    def test_timebase_signal_fires(self) -> None:
        seen: list[float] = []
        self.canvas.timebaseChanged.connect(seen.append)
        self.canvas.set_timebase(0.5)
        self.assertEqual(seen, [0.5])

    def test_timebase_allows_sub_millisecond(self) -> None:
        """采样间隔 100us 时要放大到 1 点/格，1ms 的下限会卡住。"""
        self.canvas.set_timebase(WAVE_US / 1e6)
        self.assertAlmostEqual(self.canvas.sec_per_div, WAVE_US / 1e6, places=12)


class FormatSecondsTest(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(format_seconds(5e-6), "5.0µs")
        self.assertEqual(format_seconds(2.5e-4), "250.0µs")
        self.assertEqual(format_seconds(1.99e-3), "1.990ms")
        self.assertEqual(format_seconds(2.5), "2.500s")


class ScopePageTest(unittest.TestCase):
    """页面：按模式切时基档位、滚轮同步、归位。"""

    @classmethod
    def setUpClass(cls):
        cls.project = project_io.bess_project()

    def setUp(self) -> None:
        self.page = ScopePage(self.project)
        self.page.resize(1000, 600)
        self.addCleanup(self.page.deleteLater)
        self.wave_pid = self._visible_array().pid
        # 波形是采集线程推过来的 —— 没有数据时下拉只有"全长（0 点）"，
        # 所以先喂一段，才谈得上"按点数/采样间隔切档位"
        self.page.on_tags(
            {
                self.wave_pid: TagValue(
                    pid=self.wave_pid,
                    value=[float(index) for index in range(WAVE_POINTS)],
                    timestamp=1.0,
                )
            }
        )

    def _visible_array(self):
        for channel in self.page.canvas.visible_channels():
            if channel.is_array:
                return channel
        self.fail("示例工程里应该有波形点位")

    def test_array_mode_uses_points_per_division(self) -> None:
        labels = [
            self.page.timebase_combo.itemText(index)
            for index in range(self.page.timebase_combo.count())
        ]
        self.assertTrue(labels[0].startswith("全长"), labels)
        self.assertIn("1 点/格", labels)
        self.assertNotIn("秒/格", " ".join(labels))

    def test_shortest_item_is_one_sample_per_division(self) -> None:
        index = self.page.timebase_combo.findText("1 点/格")
        self.assertGreaterEqual(index, 0)
        self.assertAlmostEqual(
            float(self.page.timebase_combo.itemData(index)), WAVE_US / 1e6, places=9
        )

    def test_item_count_matches_constants(self) -> None:
        """比"全长"还粗的档位不该出现（跟全长重复）。"""
        labels = [
            self.page.timebase_combo.itemText(index)
            for index in range(self.page.timebase_combo.count())
        ]
        per_div = [label for label in labels if label.endswith("点/格")]
        self.assertTrue(per_div)
        for label in per_div:
            self.assertIn(int(label.split(" ")[0]), ARRAY_POINTS_PER_DIV)

    def test_trend_mode_uses_seconds_per_division(self) -> None:
        for channel in self.page.canvas.channels:
            if channel.is_array:
                channel.visible = False
        self.page._rebuild_timebase_items()
        labels = self.page._timebase_labels()
        self.assertEqual(labels, [label for label, _seconds in TIMEBASES])

    def test_no_wave_data_yet_falls_back_to_seconds(self) -> None:
        """波形还没到之前下拉要按"秒/格"填，否则只剩一个数据为 0 的死档位。"""
        for channel in self.page.canvas.channels:
            channel.values = []
        self.page._rebuild_timebase_items()
        self.assertEqual(self.page._timebase_labels(), [label for label, _s in TIMEBASES])

    def test_wave_data_switches_to_points_per_division(self) -> None:
        """波形到了以后自动换成"每格几个采样点"。"""
        self.assertIn("1 点/格", self.page._timebase_labels())

    def test_waveform_data_keeps_zoom(self) -> None:
        """波形持续刷新（点数没变）不能把用户的缩放重置掉。"""
        index = self.page.timebase_combo.findText("5 点/格")
        self.page.timebase_combo.setCurrentIndex(index)
        self.page._rebuild_timebase_items()
        self.assertEqual(self.page.timebase_combo.currentText(), "5 点/格")

    def test_zoom_syncs_combo(self) -> None:
        """画布上滚轮缩放之后，下拉要对齐到最接近的档位（不然读数骗人）。"""
        self.page.canvas.set_timebase(3 * WAVE_US / 1e6)
        self.assertEqual(self.page.timebase_combo.currentText(), "2 点/格")
        self.assertAlmostEqual(self.page.canvas.sec_per_div, 2 * WAVE_US / 1e6, places=9)

    def test_reset_view_restores_defaults(self) -> None:
        self.page.timebase_combo.setCurrentIndex(
            self.page.timebase_combo.findText("1 点/格")
        )
        self.page.canvas.pan_samples = 50
        self.page.auto_y.setChecked(False)
        self.page.cursors.setChecked(False)
        self.page.trigger_mode.setCurrentIndex(1)
        self.page.trigger_edge.setCurrentIndex(1)
        self.page.trigger_level.setValue(12.5)

        self.page._reset_view()

        self.assertTrue(self.page.auto_y.isChecked())
        self.assertTrue(self.page.cursors.isChecked())
        self.assertEqual(self.page.trigger_mode.currentIndex(), 0)
        self.assertEqual(self.page.trigger_edge.currentIndex(), 0)
        self.assertEqual(self.page.trigger_level.value(), 0.0)
        self.assertEqual(self.page.canvas.pan_samples, 0)
        self.assertEqual((self.page.canvas.cursor_a, self.page.canvas.cursor_b), (0.25, 0.75))
        self.assertEqual(self.page.timebase_combo.currentIndex(), 0)
        self.assertAlmostEqual(
            self.page.canvas.sec_per_div,
            self.page.canvas.array_full_timebase(),
            places=9,
        )

    def test_reset_view_after_hiding_waveforms(self) -> None:
        """没有波形点位时"归位"要落到趋势默认档，不能停在波形档上。"""
        for channel in self.page.canvas.channels:
            if channel.is_array:
                channel.visible = False
        self.page._rebuild_timebase_items()
        self.page._reset_view()
        self.assertEqual(self.page.timebase_combo.currentIndex(), 1)
        self.assertIn("秒/格", self.page.timebase_combo.currentText())


if __name__ == "__main__":
    unittest.main()
