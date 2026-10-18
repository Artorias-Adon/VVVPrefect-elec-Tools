"""曲线给定：让点位按曲线连续写值 —— 曲线算法、播放节奏、量化、CSV 回放与工具界面。

现场最怕的四件事，测试盯着它们：

* 发出去的值越界/带长尾小数 → 一律先按点位的小数位与写值限幅量化；
* 曲线该停了还在发（斜坡、不循环回放）→ 走完必须 ``finished``；
* 采集都没跑还在往总线上写 → 开始前必须确认且要求采集运行中；
* 界面把正在跑的播放器换掉（预览时重建 player）→ 发送进度全乱。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import curvegen  # noqa: E402
from icomm.core.curvegen import CurvePlayer, CurveSpec, quantize_value  # noqa: E402
from icomm.core.models import DeviceDef, PointDef, Project  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.tools import curve_output  # noqa: E402
from icomm.ui.tools.curve_output import CurveWindow  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


# --------------------------------------------------------------- 曲线形状


class SineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = CurveSpec(kind="sine", amplitude=50.0, offset=50.0, period_s=10.0)

    def test_key_points(self) -> None:
        self.assertAlmostEqual(self.spec.value_at(0.0), 50.0)
        self.assertAlmostEqual(self.spec.value_at(2.5), 100.0)
        self.assertAlmostEqual(self.spec.value_at(5.0), 50.0)
        self.assertAlmostEqual(self.spec.value_at(7.5), 0.0)
        self.assertAlmostEqual(self.spec.value_at(10.0), 50.0, "一个周期后回到起点")

    def test_negative_time_is_clamped(self) -> None:
        self.assertAlmostEqual(self.spec.value_at(-3.0), self.spec.value_at(0.0))

    def test_phase_shifts_the_wave(self) -> None:
        shifted = CurveSpec(kind="sine", amplitude=50.0, offset=50.0, period_s=10.0, phase_deg=90.0)
        self.assertAlmostEqual(shifted.value_at(0.0), 100.0)

    def test_unknown_kind_falls_back_to_sine(self) -> None:
        spec = CurveSpec(kind="whatever", amplitude=1.0)
        self.assertEqual(spec.kind, "sine")


class ShapeTest(unittest.TestCase):
    def test_triangle(self) -> None:
        spec = CurveSpec(kind="triangle", amplitude=10.0, offset=0.0, period_s=8.0)
        self.assertAlmostEqual(spec.value_at(0.0), -10.0)
        self.assertAlmostEqual(spec.value_at(4.0), 10.0)
        self.assertAlmostEqual(spec.value_at(2.0), 0.0)
        self.assertAlmostEqual(spec.value_at(8.0), -10.0)

    def test_square_duty(self) -> None:
        spec = CurveSpec(kind="square", amplitude=5.0, offset=0.0, period_s=4.0, duty=0.25)
        self.assertAlmostEqual(spec.value_at(0.5), 5.0)
        self.assertAlmostEqual(spec.value_at(1.1), -5.0)
        self.assertAlmostEqual(spec.value_at(3.9), -5.0)

    def test_duty_is_clamped_into_range(self) -> None:
        self.assertAlmostEqual(CurveSpec(duty=0.0).duty, 0.01)
        self.assertAlmostEqual(CurveSpec(duty=1.0).duty, 0.99)
        self.assertAlmostEqual(CurveSpec(duty="oops").duty, 0.5)

    def test_sawtooth(self) -> None:
        spec = CurveSpec(kind="sawtooth", amplitude=2.0, offset=0.0, period_s=4.0)
        self.assertAlmostEqual(spec.value_at(0.0), -2.0)
        self.assertAlmostEqual(spec.value_at(2.0), 0.0)
        self.assertAlmostEqual(spec.value_at(4.0), -2.0, "回绕到起点")

    def test_ramp_finishes_at_period(self) -> None:
        spec = CurveSpec(kind="ramp", amplitude=100.0, offset=0.0, period_s=6.0)
        self.assertTrue(spec.one_shot)
        self.assertAlmostEqual(spec.value_at(0.0), -100.0)
        self.assertAlmostEqual(spec.value_at(3.0), 0.0)
        self.assertIsNone(spec.value_at(6.0), "走完就该停")
        self.assertEqual(spec.duration_s(), 6.0)

    def test_cyclic_kinds_are_not_one_shot(self) -> None:
        for kind in ("sine", "triangle", "square", "sawtooth"):
            spec = CurveSpec(kind=kind, period_s=5.0)
            self.assertFalse(spec.one_shot, kind)
            self.assertIsNone(spec.duration_s(), kind)


class ReplaySpecTest(unittest.TestCase):
    def test_looped_replay(self) -> None:
        spec = CurveSpec(kind="replay", samples=[1.0, 2.0, 3.0], sample_interval_s=0.5, loop=True)
        self.assertAlmostEqual(spec.value_at(0.0), 1.0)
        self.assertAlmostEqual(spec.value_at(0.6), 2.0)
        self.assertAlmostEqual(spec.value_at(1.6), 1.0, "循环回到第一个点")
        self.assertFalse(spec.one_shot)
        self.assertIsNone(spec.duration_s())

    def test_non_looped_replay_ends(self) -> None:
        spec = CurveSpec(kind="replay", samples=[1.0, 2.0, 3.0], sample_interval_s=0.5, loop=False)
        self.assertTrue(spec.one_shot)
        self.assertAlmostEqual(spec.duration_s(), 1.5)
        self.assertIsNone(spec.value_at(1.5))

    def test_empty_replay_has_no_value(self) -> None:
        self.assertIsNone(CurveSpec(kind="replay").value_at(0.0))


class ValidateDescribeTest(unittest.TestCase):
    def test_ok(self) -> None:
        self.assertEqual(CurveSpec(kind="sine", period_s=5.0, amplitude=2.0).validate(), "")

    def test_zero_period(self) -> None:
        self.assertIn("周期", CurveSpec(kind="sine", period_s=0.0).validate())

    def test_zero_amplitude(self) -> None:
        self.assertIn("幅度", CurveSpec(kind="sine", amplitude=0.0).validate())

    def test_replay_without_samples(self) -> None:
        self.assertIn("导入", CurveSpec(kind="replay").validate())

    def test_replay_with_bad_interval(self) -> None:
        spec = CurveSpec(kind="replay", samples=[1.0], sample_interval_s=0.0)
        self.assertIn("间隔", spec.validate())

    def test_describe_mentions_shape_and_period(self) -> None:
        text = CurveSpec(kind="triangle", amplitude=10.0, offset=5.0, period_s=4.0).describe()
        self.assertIn("三角波", text)
        self.assertIn("5 ± 10", text)
        self.assertIn("周期 4s", text)

    def test_describe_replay(self) -> None:
        spec = CurveSpec(kind="replay", samples=[0.0] * 10, sample_interval_s=0.2, loop=False)
        text = spec.describe()
        self.assertIn("10 点", text)
        self.assertIn("不循环", text)


# --------------------------------------------------------------- 量化


class QuantizeTest(unittest.TestCase):
    def test_decimals(self) -> None:
        self.assertAlmostEqual(quantize_value(3.14159, decimals=1), 3.1)
        self.assertAlmostEqual(quantize_value(3.14159, decimals=3), 3.142)
        self.assertAlmostEqual(quantize_value(3.14159, decimals=0), 3.0)

    def test_integer_beats_decimals(self) -> None:
        self.assertAlmostEqual(quantize_value(3.6, decimals=3, integer=True), 4.0)

    def test_limits(self) -> None:
        self.assertAlmostEqual(quantize_value(200.0, low=0.0, high=100.0), 100.0)
        self.assertAlmostEqual(quantize_value(-5.0, low=0.0, high=100.0), 0.0)

    def test_reversed_limits_are_ignored(self) -> None:
        self.assertAlmostEqual(quantize_value(500.0, low=100.0, high=0.0), 500.0)

    def test_decimals_are_bounded(self) -> None:
        self.assertAlmostEqual(quantize_value(1.0 / 3.0, decimals=99), round(1.0 / 3.0, 6))


# --------------------------------------------------------------- 播放节奏


class PlayerTest(unittest.TestCase):
    def make(self, **kwargs) -> CurvePlayer:
        spec = kwargs.pop("spec", None) or CurveSpec(
            kind="sine", amplitude=1.0, offset=0.0, period_s=4.0
        )
        return CurvePlayer(spec, **kwargs)

    def test_waits_for_interval(self) -> None:
        player = self.make(interval_s=0.5, decimals=2)
        self.assertIsNotNone(player.tick(0.0)[0])
        self.assertEqual(player.tick(0.1), (None, False), "还没到下一个间隔")
        self.assertIsNotNone(player.tick(0.5)[0])

    def test_sent_counter(self) -> None:
        player = self.make(interval_s=1.0)
        for index in range(3):
            player.tick(float(index))
        self.assertEqual(player.sent, 3)

    def test_duration_stops_the_player(self) -> None:
        player = self.make(interval_s=0.5, duration_s=2.0)
        sent = 0
        finished = False
        for index in range(0, 25):
            value, finished = player.tick(index * 0.1)
            if value is not None:
                sent += 1
        self.assertEqual(sent, 4, "0 / 0.5 / 1.0 / 1.5 各一次")
        self.assertTrue(finished)
        self.assertTrue(player.finished)

    def test_ramp_ends_by_itself(self) -> None:
        spec = CurveSpec(kind="ramp", amplitude=10.0, offset=0.0, period_s=1.0)
        player = CurvePlayer(spec, interval_s=0.25)
        results = [player.tick(index * 0.25) for index in range(6)]
        self.assertEqual(results[-1], (None, True))
        self.assertTrue(player.finished)

    def test_finished_is_idempotent(self) -> None:
        player = self.make(interval_s=0.5, duration_s=0.5)
        player.tick(0.0)
        self.assertEqual(player.tick(1.0), (None, True))
        self.assertEqual(player.tick(2.0), (None, True), "已经停了就一直是 (None, True)")

    def test_tick_quantizes(self) -> None:
        spec = CurveSpec(kind="sine", amplitude=100.0, offset=0.0, period_s=10.0)
        player = CurvePlayer(spec, interval_s=0.1, decimals=1, low=-50.0, high=50.0)
        value, _finished = player.tick(2.5)
        self.assertAlmostEqual(value, 50.0, msg="峰值 100 被限幅到 50")
        self.assertEqual(value, round(value, 1))

    def test_integer_point_gets_whole_numbers(self) -> None:
        spec = CurveSpec(kind="sine", amplitude=10.0, offset=0.0, period_s=10.0)
        player = CurvePlayer(spec, interval_s=0.1, integer=True)
        value, _finished = player.tick(2.5)
        self.assertEqual(value, float(int(value)))

    def test_interval_is_never_below_minimum(self) -> None:
        player = self.make(interval_s=0.0)
        self.assertAlmostEqual(player.interval_s, curvegen.MIN_INTERVAL_S)

    def test_reset_clears_progress(self) -> None:
        player = self.make(interval_s=0.5, duration_s=0.5)
        player.tick(0.0)
        player.tick(1.0)
        player.reset()
        self.assertFalse(player.finished)
        self.assertEqual(player.sent, 0)
        self.assertIsNone(player.last_value)
        self.assertIsNotNone(player.tick(0.0)[0], "reset 之后重新从 0 秒开始")


class PreviewTest(unittest.TestCase):
    def test_preview_is_quantized(self) -> None:
        spec = CurveSpec(kind="sine", amplitude=1.0, offset=0.0, period_s=4.0)
        player = CurvePlayer(spec, decimals=2)
        for _t, value in player.preview(4.0, count=17):
            self.assertEqual(value, round(value, 2))

    def test_preview_span_defaults(self) -> None:
        self.assertAlmostEqual(CurvePlayer(CurveSpec(period_s=3.0)).preview_span(), 6.0)
        replay = CurveSpec(kind="replay", samples=[0.0] * 8, sample_interval_s=0.5)
        self.assertAlmostEqual(CurvePlayer(replay).preview_span(), 4.0)

    def test_preview_stops_at_end_of_one_shot(self) -> None:
        spec = CurveSpec(kind="ramp", amplitude=1.0, period_s=1.0)
        points = CurvePlayer(spec).preview(10.0, count=50)
        self.assertLess(len(points), 50)
        self.assertLessEqual(points[-1][0], 1.0 + 1e-9)

    def test_preview_count_is_at_least_two(self) -> None:
        self.assertEqual(len(CurvePlayer(CurveSpec()).preview(2.0, count=1)), 2)


# --------------------------------------------------------------- CSV 回放


class ReplayCsvTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str, text: str) -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_single_column_skips_header(self) -> None:
        path = self.write("one.csv", "value\n0\n10\n20\n")
        values, interval = curvegen.read_replay_csv(path)
        self.assertEqual(values, [0.0, 10.0, 20.0])
        self.assertAlmostEqual(interval, 0.5, msg="没时间列就用默认间隔")

    def test_two_columns_use_median_interval(self) -> None:
        path = self.write(
            "two.csv",
            "time,value\n0,1\n0.5,2\n1.0,3\n1.6,4\n2.1,5\n",
        )
        values, interval = curvegen.read_replay_csv(path)
        self.assertEqual(values, [1.0, 2.0, 3.0, 4.0, 5.0])
        self.assertAlmostEqual(interval, 0.5, msg="相邻差 0.5/0.5/0.6/0.5 的中位数")

    def test_empty_file_raises(self) -> None:
        path = self.write("empty.csv", "# 只有注释\n")
        with self.assertRaises(ValueError):
            curvegen.read_replay_csv(path)

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(ValueError):
            curvegen.read_replay_csv(os.path.join(self.tmp.name, "nope.csv"))

    def test_write_then_read_roundtrip(self) -> None:
        path = os.path.join(self.tmp.name, "out.csv")
        curvegen.write_replay_csv(path, [0.0, 1.5, 3.0], 0.25)
        values, interval = curvegen.read_replay_csv(path)
        self.assertEqual(values, [0.0, 1.5, 3.0])
        self.assertAlmostEqual(interval, 0.25)

    def test_load_replay_keeps_loop_flag(self) -> None:
        path = self.write("two.csv", "0,1\n0.5,2\n")
        self.assertTrue(curvegen.load_replay(path).loop)
        self.assertFalse(curvegen.load_replay(path, loop=False).loop)

    def test_format_seconds(self) -> None:
        self.assertEqual(curvegen.format_seconds(12.34), "12.3s")
        self.assertEqual(curvegen.format_seconds(90.0), "1:30.0")
        self.assertEqual(curvegen.format_seconds(-1.0), "0.0s")


# --------------------------------------------------------------- 界面


def test_project() -> Project:
    """三台设备：功率给定（可写数值）、合闸命令（bool）、遥控（命令字）、只读电流。"""
    project = Project(name="曲线给定")
    project.devices.append(
        DeviceDef(
            name="PCS",
            channel="默认通道",
            unit_id=1,
            points=[
                PointDef(
                    name="Pset",
                    label="有功给定",
                    address=0,
                    writable=True,
                    data_type="int16",
                    unit="kW",
                    decimals=1,
                    write_min=-500.0,
                    write_max=500.0,
                ),
                PointDef(
                    name="On",
                    label="启动",
                    address=1,
                    writable=True,
                    data_type="bool",
                ),
                PointDef(
                    name="Cmd",
                    label="遥控命令",
                    address=2,
                    writable=True,
                    data_type="uint16",
                    commands="1:合闸,2:分闸",
                ),
                PointDef(name="I", label="电流", address=3, data_type="uint16", unit="A"),
            ],
        )
    )
    return project


class CurveWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.writes: list[tuple[str, object]] = []
        self.reported: list[tuple[str, str]] = []
        self.announced: list[tuple[str, str, str]] = []
        self.running = True
        self.context = ui_tools.ToolContext(
            project=test_project(),
            window=None,
            settings=AppSettings(),
            report_dir=self.tmp.name,
            log=lambda level, message: self.reported.append((level, message)),
            notify=lambda level, title, text="": self.announced.append((level, title, text)),
            write=lambda pid, value: self.writes.append((pid, value)),
            engine_running=lambda: self.running,
        )
        self.window = CurveWindow(self.context)
        self.addCleanup(self.window.deleteLater)

    def fake_clock(self, seconds: float) -> None:
        """把 elapsed 钉死，测试就不受真实时钟影响。"""
        self.window._elapsed = lambda: seconds  # noqa: SLF001

    def select_point(self, pid: str) -> None:
        index = self.window.point_combo.findData(pid)
        self.assertGreaterEqual(index, 0, f"{pid} 应该在可写数值点位里")
        self.window.point_combo.setCurrentIndex(index)

    def select_kind(self, kind: str) -> None:
        index = self.window.kind_combo.findData(kind)
        self.assertGreaterEqual(index, 0)
        self.window.kind_combo.setCurrentIndex(index)

    # -------------------------------------------------- 注册与表单

    def test_registered_under_automation(self) -> None:
        tool = ui_tools.find_tool("曲线给定")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.group, "自动化")
        self.assertEqual(tool.order, 16)
        names = [item.name for item in ui_tools.tools_by_group()["自动化"]]
        self.assertIn("曲线给定", names)

    def test_point_combo_lists_only_writable_numbers(self) -> None:
        items = [
            self.window.point_combo.itemData(index)
            for index in range(self.window.point_combo.count())
        ]
        self.assertIn("PCS.Pset", items)
        self.assertNotIn("PCS.On", items, "bool 点位不适合按曲线写")
        self.assertNotIn("PCS.Cmd", items, "命令点位要走按键")
        self.assertNotIn("PCS.I", items, "只读点位不列")

    def test_preview_follows_curve_params(self) -> None:
        self.select_point("PCS.Pset")
        self.fake_clock(0.0)
        self.assertEqual(len(self.window.plot.series), 1, "只有给定曲线")
        self.window.duration_box.setValue(30.0)
        self.assertIn("周期", self.window.summary.text())

    def test_curve_spec_reads_widgets(self) -> None:
        self.window.amplitude_box.setValue(20.0)
        self.window.offset_box.setValue(30.0)
        self.window.period_box.setValue(7.0)
        spec = self.window.curve_spec()
        self.assertAlmostEqual(spec.amplitude, 20.0)
        self.assertAlmostEqual(spec.offset, 30.0)
        self.assertAlmostEqual(spec.period_s, 7.0)

    # -------------------------------------------------- 启动前的拦截

    def test_start_refuses_without_engine(self) -> None:
        self.running = False
        with mock.patch.object(curve_output.QMessageBox, "information") as info:
            self.window.start()
        self.assertTrue(info.called)
        self.assertFalse(self.window._timer.isActive())
        self.assertEqual(self.writes, [])

    def test_start_refuses_without_point(self) -> None:
        self.window.point_combo.clear()
        with mock.patch.object(curve_output.QMessageBox, "information") as info:
            self.window.start()
        self.assertTrue(info.called)
        self.assertFalse(self.window._timer.isActive())

    def test_start_refuses_bad_curve(self) -> None:
        self.select_point("PCS.Pset")
        self.window.amplitude_box.setValue(0.0)
        with mock.patch.object(curve_output.QMessageBox, "warning") as warn:
            self.window.start()
        self.assertTrue(warn.called)
        self.assertFalse(self.window._timer.isActive())

    def test_start_needs_confirmation(self) -> None:
        self.select_point("PCS.Pset")
        with mock.patch.object(curve_output.QMessageBox, "question", return_value=curve_output.QMessageBox.No) as ask:
            self.window.start()
        self.assertTrue(ask.called)
        self.assertFalse(self.window._timer.isActive(), "点了「否」就不能开始")
        self.assertEqual(self.writes, [])

    # -------------------------------------------------- 写值

    def start_running(self) -> None:
        self.select_point("PCS.Pset")
        self.window.interval_box.setValue(0.5)
        with mock.patch.object(curve_output.QMessageBox, "question", return_value=curve_output.QMessageBox.Yes):
            self.window.start()
        self.assertTrue(self.window._timer.isActive())
        self.window._timer.stop()  # 测试自己驱动 _tick，不依赖事件循环

    def test_tick_writes_through_context(self) -> None:
        self.start_running()
        self.fake_clock(0.0)
        self.window._tick()
        self.fake_clock(0.5)
        self.window._tick()
        self.assertEqual(len(self.writes), 2)
        self.assertEqual(self.writes[0][0], "PCS.Pset")
        self.assertEqual(self.window.sent_label.text(), "已发 2 次")

    def test_tick_skips_before_interval(self) -> None:
        self.start_running()
        self.fake_clock(0.0)
        self.window._tick()
        self.fake_clock(0.1)
        self.window._tick()
        self.assertEqual(len(self.writes), 1)

    def test_sent_values_respect_write_limits(self) -> None:
        self.select_point("PCS.Pset")
        self.window.amplitude_box.setValue(1000.0)
        self.window.offset_box.setValue(0.0)
        self.start_running()
        for step in range(6):
            self.fake_clock(step * 0.5)
            self.window._tick()
        for _pid, value in self.writes:
            self.assertLessEqual(value, 500.0)
            self.assertGreaterEqual(value, -500.0)

    def test_written_values_use_point_decimals(self) -> None:
        self.start_running()
        self.fake_clock(0.0)
        self.window._tick()
        _pid, value = self.writes[0]
        self.assertEqual(value, round(float(value), 1))

    def test_preview_does_not_reset_the_running_player(self) -> None:
        self.start_running()
        self.fake_clock(0.0)
        self.window._tick()
        player = self.window.player
        self.window.update_preview()
        self.assertIs(self.window.player, player, "预览绝不能换掉正在跑的播放器")
        self.assertEqual(player.sent, 1)

    def test_auto_stop_when_curve_finishes(self) -> None:
        self.select_kind("ramp")
        self.window.period_box.setValue(1.0)
        self.window.interval_box.setValue(0.5)
        self.start_running()
        self.fake_clock(0.0)
        self.window._tick()
        self.fake_clock(2.0)
        self.window._tick()
        self.assertFalse(self.window._timer.isActive(), "斜坡走完要自动停")
        self.assertIn("自动停止", self.window.status.text())
        self.assertTrue(self.announced, "结束后要发一条通知")

    def test_duration_stop_says_time_is_up(self) -> None:
        self.window.duration_box.setValue(1.0)
        self.start_running()
        self.fake_clock(0.0)
        self.window._tick()
        self.fake_clock(1.5)
        self.window._tick()
        self.assertFalse(self.window._timer.isActive())
        self.assertIn("持续时间", self.window.status.text())

    def test_auto_stop_when_engine_stops(self) -> None:
        self.start_running()
        self.running = False
        self.fake_clock(0.5)
        self.window._tick()
        self.assertFalse(self.window._timer.isActive())
        self.assertIn("采集已停止", self.window.status.text())

    def test_stop_reports_elapsed_time(self) -> None:
        self.start_running()
        self.fake_clock(0.0)
        self.window._tick()
        self.fake_clock(3.5)
        self.window.stop("已手动停止")
        self.assertEqual(self.window.elapsed_label.text(), "3.5s")
        self.assertIn("已手动停止", self.window.status.text())

    def test_controls_locked_while_running(self) -> None:
        self.start_running()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        self.assertFalse(self.window.interval_box.isEnabled())
        self.window.stop("已停止")
        self.assertTrue(self.window.start_button.isEnabled())
        self.assertFalse(self.window.stop_button.isEnabled())

    def test_close_stops_writing(self) -> None:
        self.start_running()
        self.window._timer.start(1000)  # 模拟"正在跑"
        self.window.close()
        self.assertFalse(self.window._timer.isActive())
        self.assertTrue(any("关窗口" in message for _level, message in self.reported))

    def test_clicking_start_twice_does_not_restart(self) -> None:
        self.start_running()
        player = self.window.player
        self.fake_clock(0.0)
        self.window._tick()
        allowed = self.window._timer.isActive()
        self.window._timer.start(1000)  # 模拟"还在跑"
        self.window.start()
        self.assertIs(self.window.player, player)
        self.assertEqual(player.sent, 1)
        self.window._timer.stop()
        if not allowed:  # pragma: no cover - 只是别让变量未使用
            pass


class CurveWindowImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.context = ui_tools.ToolContext(
            project=test_project(),
            window=None,
            settings=AppSettings(),
            report_dir=self.tmp.name,
            engine_running=lambda: False,
        )
        self.window = CurveWindow(self.context)
        self.addCleanup(self.window.deleteLater)

    def test_import_switches_to_replay(self) -> None:
        path = os.path.join(self.tmp.name, "curve.csv")
        curvegen.write_replay_csv(path, [0.0, 10.0, 20.0, 10.0], 0.5)
        with mock.patch.object(curve_output.QFileDialog, "getOpenFileName", return_value=(path, "")):
            self.window.import_curve()
        self.assertEqual(self.window.kind_combo.currentData(), "replay")
        self.assertIn("4 点", self.window.replay_label.text())
        self.assertTrue(self.window.loop_box.isEnabled())
        self.assertFalse(self.window.amplitude_box.isEnabled(), "回放时幅度不参与计算")
        spec = self.window.curve_spec()
        self.assertEqual(len(spec.samples), 4)

    def test_import_bad_file_shows_warning(self) -> None:
        path = os.path.join(self.tmp.name, "bad.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# 空的\n")
        with mock.patch.object(curve_output.QFileDialog, "getOpenFileName", return_value=(path, "")), \
                mock.patch.object(curve_output.QMessageBox, "warning") as warn:
            self.window.import_curve()
        self.assertTrue(warn.called)
        self.assertNotEqual(self.window.kind_combo.currentData(), "replay")

    def test_cancelled_import_keeps_curve(self) -> None:
        with mock.patch.object(curve_output.QFileDialog, "getOpenFileName", return_value=("", "")):
            self.window.import_curve()
        self.assertNotEqual(self.window.kind_combo.currentData(), "replay")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
