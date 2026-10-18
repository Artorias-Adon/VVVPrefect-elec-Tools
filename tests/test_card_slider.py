"""画面卡片上的拖动滑块：只有"可写 + 有上下限"的参数才给拖，拖动只预览、松手写一次。

现场最怕的两件事这里都要挡住：

* 拖一下就往外发一串报文（总线被灌满、设备反复执行）→ 拖动中只预览，**松手才写**；
* 轮询回来的旧值把正在拖的滑块拽回去 → 用户动过之后到写下去之前，不再自动回填。
"""

from __future__ import annotations

import os
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core.models import DeviceDef, PointDef, Project  # noqa: E402
from icomm.ui.pages.dashboard import DashboardPage  # noqa: E402
from icomm.ui.widgets import ValueCard  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def setpoint(**kwargs) -> PointDef:
    """一个可写、带上下限的设定点（-500 ~ 500，一位小数）。"""
    data = dict(
        name="sp",
        label="有功给定",
        address=0,
        writable=True,
        data_type="int16",
        unit="kW",
        decimals=1,
        write_min=-500.0,
        write_max=500.0,
    )
    data.update(kwargs)
    return PointDef(**data)


def card_for(point: PointDef, pid: str = "d.sp") -> ValueCard:
    card = ValueCard(pid, point)
    card.resize(260, 150)
    return card


class SliderVisibilityTest(unittest.TestCase):
    def test_slider_appears_for_writable_point_with_limits(self) -> None:
        card = card_for(setpoint())
        self.assertIsNotNone(card.write_slider)
        self.assertIsNotNone(card.slider_value_label)
        self.assertEqual(card.write_slider.minimum(), 0)
        self.assertEqual(card.write_slider.maximum(), 10000, "1000 宽 × 10 格/单位")

    def test_no_slider_for_readonly_point(self) -> None:
        card = card_for(setpoint(writable=False))
        self.assertIsNone(card.write_slider)
        self.assertIsNone(card.write_box, "只读点位连输入框都没有")

    def test_no_slider_without_both_limits(self) -> None:
        for low, high in ((None, 100.0), (0.0, None), (None, None), (10.0, 10.0)):
            card = card_for(setpoint(write_min=low, write_max=high))
            self.assertIsNone(card.write_slider, f"{low}~{high} 不该给滑块")
            self.assertIsNotNone(card.write_box, "没有滑块时输入框还在，照样能写")

    def test_no_slider_for_bool_command_or_state_points(self) -> None:
        cases = {
            "bool": setpoint(data_type="bool", decimals=0, write_min=0.0, write_max=1.0),
            "命令点位": setpoint(commands="1:开机,2:关机"),
            "位定义": setpoint(bits="0:运行,1:故障", decimals=0),
            "状态码": setpoint(codes="0:停机,1:运行", decimals=0),
            "数组点位": setpoint(kind="array", count=8),
        }
        for label, point in cases.items():
            card = card_for(point)
            self.assertIsNone(card.write_slider, f"{label} 不该给滑块")


class SliderValueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.card = card_for(setpoint())
        self.writes: list[tuple[str, object]] = []
        self.card.writeRequested.connect(lambda pid, value: self.writes.append((pid, value)))

    def test_position_maps_to_engineering_value(self) -> None:
        slider = self.card.write_slider
        slider.setValue(0)
        self.assertAlmostEqual(self.card.slider_value(), -500.0)
        slider.setValue(5000)
        self.assertAlmostEqual(self.card.slider_value(), 0.0)
        slider.setValue(10000)
        self.assertAlmostEqual(self.card.slider_value(), 500.0)
        slider.setValue(7500)
        self.assertAlmostEqual(self.card.slider_value(), 250.0)
        self.assertIn("250", self.card.slider_value_label.text())

    def test_dragging_only_previews(self) -> None:
        self.card.write_slider.setValue(7500)
        self.assertEqual(self.writes, [], "拖动过程中不能往设备发")
        self.assertTrue(self.card._slider_dirty)
        self.assertTrue(self.card._slider_timer.isActive(), "键盘/点滑轨要靠防抖补一次写入")

    def test_release_writes_once(self) -> None:
        self.card.write_slider.setValue(6000)
        self.card.commit_slider_value()
        self.assertEqual(len(self.writes), 1)
        pid, value = self.writes[0]
        self.assertEqual(pid, "d.sp")
        self.assertAlmostEqual(value, 100.0)
        self.assertFalse(self.card._slider_dirty)
        self.assertFalse(self.card._slider_timer.isActive(), "写下去后防抖定时器要停")

    def test_commit_without_slider_is_noop(self) -> None:
        card = card_for(setpoint(write_min=None))
        writes: list = []
        card.writeRequested.connect(lambda pid, value: writes.append(pid))
        card.commit_slider_value()
        self.assertEqual(writes, [])

    def test_timer_is_the_debounce(self) -> None:
        timer = self.card._slider_timer
        self.assertTrue(timer.isSingleShot())
        self.assertGreaterEqual(timer.interval(), 300)

    def test_int_point_writes_int(self) -> None:
        card = card_for(setpoint(data_type="uint16", decimals=0, write_min=1, write_max=8))
        writes: list = []
        card.writeRequested.connect(lambda pid, value: writes.append(value))
        card.write_slider.setValue(3)  # 1 + 3
        card.commit_slider_value()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0], 4)
        self.assertIsInstance(writes[0], int)

    def test_float_point_writes_rounded_value(self) -> None:
        card = card_for(setpoint(data_type="float32", decimals=2, write_min=0.0, write_max=10.0))
        writes: list = []
        card.writeRequested.connect(lambda pid, value: writes.append(value))
        card.write_slider.setValue(450)  # 0 + 4.50
        card.commit_slider_value()
        self.assertAlmostEqual(writes[0], 4.5)


class SliderSyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.card = card_for(setpoint())

    def test_realtime_value_moves_the_slider(self) -> None:
        self.card.set_value(250.0, "good")
        self.assertAlmostEqual(self.card.slider_value(), 250.0)
        self.assertIn("250", self.card.slider_value_label.text())

    def test_realtime_value_is_clamped_to_limits(self) -> None:
        self.card.set_value(9000.0, "good")
        self.assertAlmostEqual(self.card.slider_value(), 500.0)
        self.card.set_value(-9000.0, "good")
        self.assertAlmostEqual(self.card.slider_value(), -500.0)

    def test_bad_quality_shows_dash(self) -> None:
        self.card.set_value(None, "bad", "通信异常")
        self.assertEqual(self.card.slider_value_label.text(), "--")

    def test_poll_does_not_yank_the_slider_while_user_adjusts(self) -> None:
        self.card.write_slider.setValue(7500)
        self.card.set_value(0.0, "good")  # 轮询回来的旧值
        self.assertAlmostEqual(self.card.slider_value(), 250.0, msg="用户动过的位置不能被拽回去")

    def test_poll_does_not_yank_right_after_write(self) -> None:
        self.card.set_value(0.0, "good")
        self.card.write_slider.setValue(7500)
        self.card.commit_slider_value()
        self.card.set_value(0.0, "good")  # 写下去还没读回新值
        self.assertAlmostEqual(self.card.slider_value(), 250.0)
        # 宽限期过了就重新跟随实时值
        self.card._slider_hold_until = 0.0
        self.card.set_value(100.0, "good")
        self.assertAlmostEqual(self.card.slider_value(), 100.0)

    def test_realtime_value_updates_while_dragging_is_ignored(self) -> None:
        self.card.write_slider.setValue(7500)
        self.card._sync_slider(-100.0, dragging=True)
        self.assertAlmostEqual(self.card.slider_value(), 250.0)


class DashboardSliderTest(unittest.TestCase):
    """运行监视页里真的会出现带滑块的卡片，并且写请求能从页面抛出去。"""

    def test_sample_project_has_slider_cards(self) -> None:
        page = DashboardPage(project_io.bess_project())
        self.addCleanup(page.deleteLater)
        cards = [
            widget
            for widget in page._items.values()
            if getattr(widget, "write_slider", None) is not None
        ]
        self.assertTrue(cards, "储能示例里应该有带滑块的给定值卡片")
        titles = " ".join(card.pid for card in cards)
        self.assertIn("有功功率给定", titles)

    def test_commit_reaches_the_page_signal(self) -> None:
        page = DashboardPage(project_io.bess_project())
        self.addCleanup(page.deleteLater)
        written: list[tuple[str, object]] = []
        page.writeRequested.connect(lambda pid, value: written.append((pid, value)))
        card = next(
            widget
            for widget in page._items.values()
            if getattr(widget, "write_slider", None) is not None
        )
        card.set_value(0.0, "good")
        card.write_slider.setValue(card.write_slider.maximum())
        card.commit_slider_value()
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0][0], card.pid)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
