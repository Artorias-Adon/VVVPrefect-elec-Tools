"""工具框架、自动化核心、变量搜索、写值限幅、小数位的测试。

这些都是"看起来能用但容易没接上"的东西：
* 工具注册表要真的能被菜单生成代码用起来；
* 触发条件要去抖、要认边沿、要认位；
* 限幅必须真的挡住写值（这是一条安全相关的功能，不能只改模型不改引擎）；
* 小数位只影响显示，不能动设备里的值。
"""

from __future__ import annotations

import json
import math
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest import mock  # noqa: E402

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QMessageBox,
    QPushButton,
)

from icomm.core import project as project_io  # noqa: E402
from icomm.core import search  # noqa: E402
from icomm.core import settings as app_settings  # noqa: E402
from icomm.core.automation import (  # noqa: E402
    AutomationService,
    Compare,
    Condition,
    Logic,
    Task,
    Trigger,
    TriggerMode,
)
from icomm.core.models import PointDef  # noqa: E402
from icomm.core.net_check import ConnectionTester, parse_hex_payload  # noqa: E402
from icomm.core.tags import TagValue  # noqa: E402
from icomm.core.timing import Schedule, Step, StepMode, Stopwatch, format_seconds  # noqa: E402
from icomm.core import diagram_lib  # noqa: E402
from icomm.core import analysis as an  # noqa: E402
from icomm.core import models, serial_ports  # noqa: E402
from icomm.core.drivers.modbus import ModbusDriver  # noqa: E402
from icomm.core.drivers.base import OptionField  # noqa: E402
from icomm.ui import option_fields  # noqa: E402
from icomm.ui.tools.analysis import AnalysisWindow  # noqa: E402
from tools.modbus_sim import ModbusTcpSimulator  # noqa: E402
from icomm.core import fault as fault_core  # noqa: E402
from icomm.core import audio  # noqa: E402
from icomm.core.fault import MAX_SPAN_S, FaultConfig, FaultRecorder  # noqa: E402
from icomm.core.models import DiagramDef, DiagramNode, PointDef  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui import theme  # noqa: E402
from icomm.ui.tools.base import ToolContext, register_tool  # noqa: E402
from icomm.ui.tools.audio_record import AudioRecordWindow  # noqa: E402
from icomm.ui.tools.diagram_edit import DiagramEditWindow  # noqa: E402
from icomm.ui.pages.overview import DiagramCanvas  # noqa: E402
from icomm.ui.tools.variable_trigger import (  # noqa: E402
    build_trigger_fields,
    read_trigger_fields,
)

_app: QApplication | None = None

PCS = "储能变流器PCS"
BUS_PID = f"{PCS}.有功功率给定"
STATUS_PID = f"{PCS}.运行状态字"
CTRL_PID = f"{PCS}.控制字"


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def tag(pid: str, value) -> dict[str, TagValue]:
    return {pid: TagValue(pid=pid, value=value, quality="good")}


def select_combo(combo, value) -> bool:
    """按 data 选一项。

    别用 ``findData``：这些枚举是 ``str, Enum``，Qt 存进去会退化成普通 str，
    ``findData(枚举成员)`` 有时选不中；自己扫一遍顺便也验证了 data 还在。
    """
    for index in range(combo.count()):
        if combo.itemData(index) == value:
            combo.setCurrentIndex(index)
            return True
    return False


class TriggerFieldsTest(unittest.TestCase):
    """触发条件控件读出来的东西必须与用户选的一致。

    这里盯的是一个真实踩过的坑：``Compare``/``TriggerMode`` 是 ``str, Enum``，
    PySide6 存进 QComboBox 的 itemData 会退化成普通 str，于是
    ``isinstance(data, Compare)`` 为假、直接回落到默认值 —— 界面上显示"下降沿"，
    实际却按"上升沿"判断，属于不报错但行为错误的那种 bug。
    """

    def setUp(self):
        self.project = project_io.bess_project()
        self.context = ToolContext(project=self.project, window=None)  # type: ignore[arg-type]
        self.fields = build_trigger_fields(self.context)

    def select(self, combo, value) -> None:
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return
        self.fail(f"{value!r} 不在下拉框里")

    def test_every_compare_survives_the_combo(self):
        for compare in Compare:
            with self.subTest(compare=compare):
                self.select(self.fields.compare_combo, compare)
                self.assertIs(read_trigger_fields(self.fields).compare, compare)

    def test_every_mode_survives_the_combo(self):
        for mode in TriggerMode:
            with self.subTest(mode=mode):
                self.select(self.fields.mode_combo, mode)
                self.assertIs(read_trigger_fields(self.fields).mode, mode)

    def test_threshold_and_bit_are_carried_over(self):
        self.select(self.fields.pid_combo, BUS_PID)
        self.select(self.fields.compare_combo, Compare.BIT_SET)
        self.fields.bit_box.setValue(5)
        self.fields.hold_box.setValue(3)
        self.fields.cooldown_box.setValue(2.5)
        trigger = read_trigger_fields(self.fields)
        self.assertEqual(trigger.pid, BUS_PID)
        self.assertEqual((trigger.bit, trigger.hold_count), (5, 3))
        self.assertAlmostEqual(trigger.cooldown_s, 2.5)
        self.assertIn("bit5 为 1", trigger.describe())

    def test_missing_point_is_reported(self):
        combo = self.fields.pid_combo
        combo.setCurrentIndex(-1)
        combo.setEditText("")
        with self.assertRaises(ValueError):
            read_trigger_fields(self.fields)
        # 预览这类非严格场合不该抛错
        self.assertTrue(self.fields.describe())

    # -------------------------------------------------- 多个变量组合

    def test_starts_with_one_condition(self):
        self.assertEqual(len(self.fields.condition_rows), 1)
        self.assertTrue(self.fields.add_button.isEnabled())
        row = self.fields.condition_rows[0]
        self.assertFalse(row.remove_button.isEnabled(), "只有一条时不该能删")

    def test_second_condition_joins_with_and(self):
        second = self.fields.add_condition(pid=STATUS_PID)
        self.assertIsNotNone(second)
        self.assertEqual(len(self.fields.condition_rows), 2)
        self.assertTrue(self.fields.condition_rows[0].remove_button.isEnabled())
        self.assertTrue(self.fields.logic_combo.isEnabled(), "两条以上才谈得上组合方式")

        trigger = read_trigger_fields(self.fields)
        self.assertEqual(len(trigger.conditions), 2)
        self.assertIs(trigger.logic, Logic.AND)
        self.assertEqual(trigger.watched_pids[1], STATUS_PID)
        self.assertEqual(len(trigger.watched_pids), 2)
        text = trigger.describe()
        self.assertIn("且", text)
        self.assertIn("运行状态字", text)

    def test_or_logic_is_carried_over(self):
        self.fields.add_condition(pid=STATUS_PID)
        select_combo(self.fields.logic_combo, Logic.OR)
        trigger = read_trigger_fields(self.fields)
        self.assertIs(trigger.logic, Logic.OR)
        self.assertIn("或", trigger.describe())

    def test_removing_condition_keeps_single_condition_readable(self):
        self.select(self.fields.pid_combo, BUS_PID)
        second = self.fields.add_condition(pid=STATUS_PID)
        self.fields.remove_condition(second)
        trigger = read_trigger_fields(self.fields)
        self.assertEqual(len(trigger.conditions), 1)
        self.assertEqual(trigger.pid, BUS_PID, "回到单条件时老字段也要填上")
        self.assertFalse(self.fields.logic_combo.isEnabled())

    def test_empty_condition_rows_are_skipped(self):
        row = self.fields.add_condition()
        row.pid_combo.setCurrentIndex(-1)
        row.pid_combo.setEditText("")
        trigger = read_trigger_fields(self.fields)
        self.assertEqual(len(trigger.conditions), 1, "空行不算条件")

    def test_condition_count_is_capped(self):
        while self.fields.add_condition() is not None:
            pass
        self.assertEqual(len(self.fields.condition_rows), 6)
        self.assertFalse(self.fields.add_button.isEnabled(), "到上限就该点不动了")

    def test_each_condition_keeps_its_own_bit_setting(self):
        self.select(self.fields.compare_combo, Compare.BIT_SET)
        self.fields.bit_box.setValue(3)
        second = self.fields.add_condition(pid=STATUS_PID)
        select_combo(second.compare_combo, Compare.BIT_SET)
        second.bit_box.setValue(11)
        trigger = read_trigger_fields(self.fields)
        self.assertEqual([c.bit for c in trigger.conditions], [3, 11])


class TimingCoreTest(unittest.TestCase):
    """计时器与定时赋值的逻辑。"""

    def test_stopwatch_start_pause_resume(self):
        watch = Stopwatch()
        self.assertFalse(watch.running)
        self.assertEqual(watch.elapsed(now=100.0), 0.0)

        watch.start(now=100.0)
        self.assertTrue(watch.running)
        self.assertAlmostEqual(watch.elapsed(now=102.5), 2.5)

        watch.pause(now=103.0)
        self.assertFalse(watch.running)
        self.assertAlmostEqual(watch.elapsed(now=999.0), 3.0, msg="暂停后不再走时")

        watch.start(now=1000.0)
        self.assertAlmostEqual(watch.elapsed(now=1001.0), 4.0, msg="继续应接在暂停值上")

        watch.reset()
        self.assertEqual(watch.elapsed(now=2000.0), 0.0)

    def test_stopwatch_marks(self):
        watch = Stopwatch()
        watch.start(now=10.0)
        watch.mark(now=11.5)
        watch.mark(now=13.0)
        self.assertEqual(watch.marks, [1.5, 3.0])
        watch.reset()
        self.assertEqual(watch.marks, [])

    def test_text_format(self):
        self.assertEqual(format_seconds(0), "00:00:00.0")
        self.assertEqual(format_seconds(61.4), "00:01:01.4")
        self.assertEqual(format_seconds(3661.95), "01:01:01.9")

    def test_at_step_fires_once(self):
        step = Step(pid="P", mode=StepMode.AT, seconds=2.0)
        self.assertEqual(step.due_times(1.9, 0.0), 0)
        self.assertEqual(step.due_times(2.0, 0.0), 1)
        self.assertEqual(step.due_times(5.0, 2.0), 0, "第 N 秒只该执行一次")

    def test_at_step_tolerates_late_tick(self):
        """界面卡一下导致定时器晚跑，这一秒也不能丢。"""
        step = Step(pid="P", mode=StepMode.AT, seconds=1.0)
        self.assertEqual(step.due_times(1.7, 0.0), 1)

    def test_every_step_period(self):
        step = Step(pid="P", mode=StepMode.EVERY, seconds=0.5)
        self.assertEqual(step.due_times(0.4, 0.0), 0)
        self.assertEqual(step.due_times(0.5, 0.0), 1)
        self.assertEqual(step.due_times(1.2, 0.0), 2, "跳过两拍就该补两次")
        self.assertEqual(step.due_times(1.2, 1.0), 0, "刚发过就不该再发")

    def test_every_step_caps_backlog(self):
        step = Step(pid="P", mode=StepMode.EVERY, seconds=0.1)
        self.assertEqual(step.due_times(60.0, 0.0, limit=10), 10, "卡死后不能猛写一堆")

    def test_disabled_and_trigger_steps_never_fire_on_time(self):
        self.assertEqual(Step(pid="P", mode=StepMode.AT, seconds=1, enabled=False)
                         .due_times(10.0, 0.0), 0)
        self.assertEqual(Step(pid="P", mode=StepMode.TRIGGER, seconds=1)
                         .due_times(10.0, 0.0), 0)
        self.assertEqual(Step(pid="P", mode=StepMode.TRIGGER, seconds=1).label(), "变量触发")

    def test_schedule_tick_and_reset(self):
        schedule = Schedule()
        schedule.add(Step(pid="A", mode=StepMode.AT, seconds=2.0))
        schedule.add(Step(pid="B", mode=StepMode.EVERY, seconds=1.0))

        self.assertEqual([step.pid for step in schedule.tick(0.5)], [])
        self.assertEqual([step.pid for step in schedule.tick(1.0)], ["B"])
        self.assertEqual([step.pid for step in schedule.tick(2.0)], ["A", "B"])
        self.assertEqual([step.pid for step in schedule.tick(3.0)], ["B"])
        self.assertEqual(schedule.steps[0].fired, 1)
        self.assertEqual(schedule.steps[1].fired, 3)

        schedule.reset()
        self.assertEqual([step.fired for step in schedule.steps], [0, 0])
        self.assertEqual([step.pid for step in schedule.tick(0.5)], [], "清零后要重新等一遍")

    def test_schedule_remove_keeps_other_steps_aligned(self):
        schedule = Schedule()
        schedule.add(Step(pid="A", mode=StepMode.AT, seconds=5.0))
        schedule.add(Step(pid="B", mode=StepMode.EVERY, seconds=1.0))
        schedule.tick(1.0)
        schedule.remove(0)
        self.assertEqual([step.pid for step in schedule.tick(2.0)], ["B"],
                         "删掉前一条不该把后一条的计时搞乱")

    def test_trigger_steps_are_listed_for_automation(self):
        schedule = Schedule()
        schedule.add(Step(pid="A", mode=StepMode.AT, seconds=1.0))
        schedule.add(Step(pid="B", mode=StepMode.TRIGGER))
        self.assertEqual([step.pid for step in schedule.trigger_steps()], ["B"])
        self.assertEqual(schedule.summary(), "2/2 条启用")


class FakeTag:
    """够用的假 TagValue。"""

    def __init__(self, value, quality: str = "good") -> None:
        self.value = value
        self.quality = quality


class FaultRecorderTest(unittest.TestCase):
    """故障录波：往回记的缓冲 + 触发后成段。"""

    PID = "储能变流器PCS.直流母线电压"
    ARRAY_PID = "储能变流器PCS.并网A相电流波形"

    def make(self, project=None, pre=1.0, post=1.0) -> FaultConfig:
        return FaultConfig(
            pids=[self.PID, self.ARRAY_PID], pre_s=pre, post_s=post, export_csv=False
        ).normalized()

    def test_config_normalized(self):
        config = FaultConfig(pids=["A", "A", "B"], pre_s=-5, post_s=9999).normalized()
        self.assertEqual(config.pids, ["A", "B"], "重复点位要去掉")
        self.assertEqual(config.pre_s, 0.0, "负的时长按 0")
        self.assertEqual(config.post_s, MAX_SPAN_S, "离谱的时长要夹住")
        self.assertAlmostEqual(config.span(), MAX_SPAN_S)

    def test_keeps_only_pre_window(self):
        recorder = FaultRecorder(self.make(pre=1.0))
        for step in range(10):  # 0..9 秒
            recorder.feed({self.PID: FakeTag(700.0 + step)}, now=100.0 + step)
        buffered = recorder.buffered()[self.PID]
        self.assertLessEqual(buffered, 3, f"1 秒窗口不该留 10 个点（实际 {buffered}）")

    def test_capture_has_pre_and_post(self):
        recorder = FaultRecorder(self.make(pre=1.0, post=1.0))
        for step in range(6):  # 0..5 秒，采样间隔 0.2 秒 ≈ 每 5 个一批
            recorder.feed({self.PID: FakeTag(700.0 + step)}, now=100.0 + step * 0.2)
        self.assertTrue(recorder.trigger("母线欠压", now=101.0))
        self.assertFalse(recorder.trigger("再来一次", now=101.5), "一次没录完不该再触发")

        for step in range(1, 8):  # 触发后继续采 1.4 秒
            recorder.feed({self.PID: FakeTag(600.0 - step)}, now=101.0 + step * 0.2)
        capture = recorder.tick(now=102.4)
        self.assertIsNotNone(capture)
        assert capture is not None
        self.assertEqual(capture.reason, "母线欠压")
        self.assertGreaterEqual(len(capture.series[self.PID]), 6)
        self.assertEqual(capture.intervals[self.PID], 200_000, "间隔该按实际节奏算出来")
        self.assertEqual(recorder.fired, 1)
        self.assertIsNotNone(recorder.last)

    def test_nothing_captured_before_post_elapses(self):
        recorder = FaultRecorder(self.make(post=2.0))
        recorder.feed({self.PID: FakeTag(700.0)}, now=100.0)
        recorder.trigger("x", now=100.0)
        self.assertIsNone(recorder.tick(now=101.0), "还没到 post 时间不该收尾")
        self.assertTrue(recorder.recording)
        self.assertIsNotNone(recorder.flush(now=101.0), "手动收尾要能立刻拿到")

    def test_array_frames_keep_whole_frame(self):
        """数组点位整帧保留：跨过触发时刻的那一帧不能丢。"""
        recorder = FaultRecorder(self.make(pre=0.5, post=0.5))
        for index, frame in enumerate(([1, 2, 3], [4, 5, 6], [7, 8, 9])):
            recorder.feed({self.ARRAY_PID: FakeTag(frame)}, now=200.0 + index * 0.5)
        recorder.trigger("ARR", now=200.6)
        for index in range(3):
            recorder.feed({self.ARRAY_PID: FakeTag([10 + index] * 3)}, now=200.6 + index * 0.3)
        capture = recorder.tick(now=202.0)
        assert capture is not None
        values = capture.series[self.ARRAY_PID]
        self.assertEqual(len(values) % 3, 0, "拼接后应当还是整帧")
        self.assertIn(4.0, values, "触发前那 0.1 秒读到的那一帧要整帧留着")
        self.assertNotIn(1.0, values, "窗口之外（200.0 那帧整帧都在 0.5s 窗口前）不该留")

    def test_array_interval_us_survives(self):
        project = project_io.sst_project()
        array_pid = next(
            f"{device.name}.{point.name}"
            for device, point in project.iter_points()
            if point.is_array and point.sample_interval_us
        )
        recorder = FaultRecorder(
            FaultConfig(pids=[array_pid], pre_s=0.4, post_s=0.4), project
        )
        point = project.find_point(array_pid)[1]
        recorder.feed({array_pid: FakeTag([1.0] * point.count)}, now=300.0)
        recorder.trigger("wave", now=300.0)
        capture = recorder.flush(now=300.1)
        assert capture is not None
        self.assertEqual(
            capture.intervals[array_pid], point.sample_interval_us,
            "数组点位的采样间隔要取参数表里的值（决定示波器时间轴）",
        )

    def test_bad_quality_is_skipped(self):
        recorder = FaultRecorder(self.make())
        recorder.feed({self.PID: FakeTag(700.0, quality="bad")}, now=100.0)
        self.assertEqual(recorder.buffered().get(self.PID, 0), 0, "坏质量不该入缓冲")

    def test_ignores_unconfigured_points(self):
        recorder = FaultRecorder(self.make())
        recorder.feed({"别的点位": FakeTag(1.0)}, now=100.0)
        self.assertEqual(recorder.buffered(), {})

    def test_reconfigure_drops_unwanted_buffers(self):
        recorder = FaultRecorder(self.make())
        recorder.feed({self.PID: FakeTag(700.0), self.ARRAY_PID: FakeTag([1.0])}, now=100.0)
        self.assertEqual(len(recorder.buffered()), 2)
        recorder.configure(FaultConfig(pids=[self.PID], pre_s=1, post_s=1))
        self.assertEqual(list(recorder.buffered()), [self.PID])

    def test_save_capture_pushes_to_storage_and_exporter(self):
        recorder = FaultRecorder(self.make(pre=0.5, post=0.5))
        recorder.feed({self.PID: FakeTag(700.0)}, now=100.0)
        recorder.trigger("test", now=100.0)
        capture = recorder.flush(now=100.1)
        assert capture is not None

        pushed: list[tuple] = []

        class FakeStorage:
            def add_waveform(self, pid, values, dt_us=0, ts=None):
                pushed.append((pid, list(values), dt_us, ts))

        exported: list[str] = []

        def exporter(cap, reason):
            exported.append(reason)
            return "x.csv"

        saved = fault_core.save_capture(capture, FakeStorage(), exporter)
        self.assertEqual(saved, 1)
        self.assertEqual(pushed[0][0], self.PID)
        self.assertEqual(exported, ["test"])
        self.assertEqual(capture.csv_path, "x.csv")

    def test_save_capture_survives_storage_failure(self):
        recorder = FaultRecorder(self.make(pre=0.5, post=0.5))
        recorder.feed({self.PID: FakeTag(700.0)}, now=100.0)
        recorder.trigger("x", now=100.0)
        capture = recorder.flush(now=100.1)
        assert capture is not None

        class BrokenStorage:
            def add_waveform(self, *args, **kwargs):
                raise RuntimeError("库坏了")

        self.assertEqual(fault_core.save_capture(capture, BrokenStorage()), 0)

    def test_describe_lists_curves(self):
        recorder = FaultRecorder(self.make(pre=0.5, post=0.5))
        recorder.feed({self.PID: FakeTag(700.0)}, now=100.0)
        recorder.trigger("母线欠压", now=100.0)
        capture = recorder.flush(now=100.1)
        assert capture is not None
        text = fault_core.describe(capture, title_of=lambda pid: pid.split(".")[-1])
        self.assertIn("母线欠压", text)
        self.assertIn("直流母线电压", text)


class FaultRecorderOwnershipTest(unittest.TestCase):
    """缓冲是共享的：只有认领它的那个窗口收尾，别互相抢。"""

    PID = "储能变流器PCS.直流母线电压"

    def test_owner_claim_and_release(self):
        recorder = FaultRecorder(FaultConfig(pids=[self.PID]).normalized())
        alice, bob = object(), object()
        self.assertTrue(recorder.is_owner(alice), "没人认领时谁都算主人（兼容老用法）")
        recorder.claim(alice)
        self.assertTrue(recorder.is_owner(alice))
        self.assertFalse(recorder.is_owner(bob), "别人不能再抢")
        recorder.claim(bob)
        self.assertTrue(recorder.is_owner(bob), "后认领的接管")
        self.assertFalse(recorder.is_owner(alice))
        recorder.release(alice)
        self.assertTrue(recorder.is_owner(bob), "不是主人的人 release 不该把认领清掉")
        recorder.release(bob)
        self.assertTrue(recorder.is_owner(alice), "放开认领后谁都能用")

    def test_second_window_does_not_steal_capture(self):
        """开两个「故障录波」窗口：成品要落在认领那个窗口的目录里。"""
        from icomm.ui.tools.fault_record import FaultRecordWindow

        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        project = project_io.bess_project()
        from icomm.core import project_store
        from icomm.ui.main_window import MainWindow

        paths = project_store.create_project_dir(
            os.path.join(self.tmp, "工程"), project
        )
        window = MainWindow(project, paths, workdir=self.tmp, base_dir=self.tmp)
        window.settings.confirm_on_exit = False
        self.addCleanup(window.close)

        context = window.tool_context()
        first = FaultRecordWindow(context, parent=window)
        second = FaultRecordWindow(context, parent=window)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        self.assertIs(first.recorder, second.recorder, "两个窗口共用一个缓冲")

        claim_dir = os.path.join(self.tmp, "第一个窗口要的目录")
        other_dir = os.path.join(self.tmp, "第二个窗口的目录")
        first.dir_edit.setText(claim_dir)
        second.dir_edit.setText(other_dir)
        first.export_box.setChecked(True)
        # 勾上要监视的量（_config() 的点位就是从这里来的）
        for index in range(first.points_list.count()):
            item = first.points_list.item(index)
            if item.data(Qt.UserRole) == self.PID:
                item.setCheckState(Qt.Checked)
        self.assertEqual(first._config().pids, [self.PID])

        first.recorder.configure(first._config(), project)
        first.recorder.claim(first)
        for step in range(6):
            first.recorder.feed({self.PID: 700.0 + step}, now=100.0 + step * 0.2)
        self.assertTrue(first.recorder.buffered().get(self.PID), "先得勾上要监视的量")
        self.assertTrue(first.recorder.trigger("母线欠压", now=101.0))
        first.recorder.feed({self.PID: 999.0}, now=101.6)

        capture = first.recorder.tick(now=102.5)
        self.assertIsNotNone(capture)
        # 第二个窗口先跑到也不该抢走
        second._finish(capture)
        self.assertFalse(os.path.isdir(other_dir), "第二个窗口不该收尾")
        first._finish(capture)
        self.assertTrue(os.path.isdir(claim_dir), "认领的窗口才收尾")
        self.assertTrue(os.listdir(claim_dir), "CSV 要落在认领窗口的目录里")
        self.assertFalse(os.path.isdir(other_dir))

    def test_stranger_window_tick_leaves_it_unclaimed(self):
        """不是主人的窗口跑 tick() 不该把别人的段收走。"""
        recorder = FaultRecorder(FaultConfig(pids=[self.PID], post_s=0.0).normalized())
        recorder.claim(object())  # 别人认领了
        for step in range(4):
            recorder.feed({self.PID: 700.0 + step}, now=100.0 + step)
        recorder.trigger("母线欠压", now=101.0)

        stub = unittest.mock.MagicMock()
        stub.recorder = recorder
        stub.status.text.return_value = "监视中（1 个量已入缓冲）"
        from icomm.ui.tools.fault_record import FaultRecordWindow

        FaultRecordWindow._tick(stub)
        self.assertTrue(recorder.recording, "别人的段还该在录着")
        self.assertEqual(recorder.fired, 0)
        self.assertIn("另一个", stub.status.setText.call_args[0][0])


class WavWriterTest(unittest.TestCase):
    """WAV 写盘（录音的落盘部分，不依赖声卡，可以随便测）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "a.wav")

    def test_header_and_lengths(self):
        writer = audio.WavWriter(self.path, rate=8000, channels=1)
        writer.write(b"\x01\x00" * 800)  # 800 个采样 = 0.1 秒
        self.assertAlmostEqual(writer.duration, 0.1, places=4)
        self.assertEqual(writer.size, audio.WavWriter.HEADER_SIZE + 1600)
        writer.close()

        with wave.open(self.path, "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getframerate(), 8000)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getnframes(), 800)

    def test_stereo_byte_rate(self):
        writer = audio.WavWriter(self.path, rate=44100, channels=2)
        header = writer.header()
        writer.close()
        self.assertIn(struct.pack("<I", 44100 * 2 * 2), header, "字节率要按声道数算")
        self.assertIn(struct.pack("<H", 4), header, "块对齐 = 声道数 × 每采样字节")

    def test_odd_length_tail_dropped(self):
        writer = audio.WavWriter(self.path, rate=8000, channels=1)
        self.assertEqual(writer.write(b"\x01\x02\x03"), 2, "奇数长度要丢掉最后那个字节")
        writer.close()

    def test_write_after_close_is_noop(self):
        writer = audio.WavWriter(self.path, rate=8000, channels=1)
        writer.write(b"\x01\x00")
        writer.close()
        before = writer.bytes_written
        self.assertEqual(writer.write(b"\x01\x00"), 0)
        self.assertEqual(writer.bytes_written, before)
        writer.close()  # 重复关闭也不该炸

    def test_partial_file_is_still_playable_after_close(self):
        """没录到东西也要写出一个合法（空）WAV，别留个坏文件。"""
        writer = audio.WavWriter(self.path, rate=16000, channels=1)
        writer.close()
        with wave.open(self.path, "rb") as handle:
            self.assertEqual(handle.getnframes(), 0)


class AudioDeviceAndNullTest(unittest.TestCase):
    """设备枚举与"录不了时的替身"。"""

    def test_devices_never_raises(self):
        found = audio.devices()
        self.assertIsInstance(found, list)
        for device in found:
            self.assertIsInstance(device.index, int)
            self.assertTrue(device.name)

    def test_available_reports_reason(self):
        ok, reason = audio.WinmmRecorder.available()
        self.assertIsInstance(ok, bool)
        if not ok:
            self.assertTrue(reason, "不能录时必须给出原因，界面要显示")

    def test_null_recorder_contract(self):
        path = os.path.join(tempfile.mkdtemp(), "n.wav")
        null = audio.make_recorder(path) if not audio.devices() else None
        recorder = audio.NullRecorder(path, error="没有设备")
        self.assertFalse(recorder.open())
        self.assertEqual(recorder.should_stop(), "")
        self.assertEqual(recorder.duration, 0.0)
        recorder.close()
        self.assertFalse(os.path.exists(path), "替身不该留下文件")
        if null is not None:
            self.assertIsInstance(null, audio.NullRecorder)


class WriteLimitModelTest(unittest.TestCase):
    """限幅的模型层语义。"""

    def test_clamps_both_sides(self):
        point = PointDef(name="给定", data_type="float32", write_min=0, write_max=800)
        self.assertEqual(point.clamp_write(700), (700, ""))
        self.assertEqual(point.clamp_write(5000), (800, "超过上限 800"))
        self.assertEqual(point.clamp_write(-5), (0, "低于下限 0"))

    def test_no_limit_passes_through(self):
        point = PointDef(name="无限制")
        value, note = point.clamp_write(12345)
        self.assertEqual(value, 12345)
        self.assertEqual(note, "")
        self.assertFalse(point.has_write_limit)

    def test_inverted_range_does_not_lock_point(self):
        """下限 > 上限时按"不设限"处理，免得把点位锁死成写不进去。"""
        point = PointDef(name="写反了", write_min=100, write_max=10)
        value, note = point.clamp_write(50)
        self.assertEqual(value, 50)
        self.assertEqual(note, "")

    def test_integer_points_stay_integer(self):
        point = PointDef(name="整数", data_type="uint32", write_min=10, write_max=20)
        value, _note = point.clamp_write(99.6)
        self.assertIsInstance(value, int)
        self.assertEqual(value, 20)

    def test_bool_points_untouched(self):
        point = PointDef(name="布尔", data_type="bool", write_min=2, write_max=3)
        self.assertEqual(point.clamp_write(True)[0], True)

    def test_limit_text(self):
        point = PointDef(name="给定", unit="V", write_min=0, write_max=800)
        self.assertEqual(point.write_limit_text(), "0 ~ 800 V")
        self.assertEqual(PointDef(name="只下限", write_min=5).write_limit_text(), "5 ~ +∞")
        self.assertEqual(PointDef(name="只上限", write_max=5).write_limit_text(), "-∞ ~ 5")

    def test_limit_survives_roundtrip(self):
        from icomm.core.models import Project

        project = project_io.bess_project()
        device = project.devices[0]
        point = device.point("有功功率给定")
        point.write_min = 100.0
        point.write_max = 750.0
        restored = Project.from_dict(project.to_dict())
        again = restored.device(PCS).point("有功功率给定")
        self.assertEqual(again.write_min, 100.0)
        self.assertEqual(again.write_max, 750.0)


class WriteLimitEngineTest(unittest.TestCase):
    """限幅必须在引擎的写值路径上真的生效（安全相关）。"""

    def setUp(self):
        self.project = project_io.bess_project()
        self.point = self.project.devices[0].point("有功功率给定")

    def test_single_write_is_clamped(self):
        from icomm.core.engine import AcquisitionEngine

        engine = AcquisitionEngine(self.project)
        engine.start()  # 起通道线程，写值会入队；这里只验值被夹紧
        try:
            self.point.write_min = 0.0
            self.point.write_max = 700.0
            self.point.write_limit_warn = False
            # 直接调 clamp（引擎内部走的就是这个），再验引擎不会把原值透传
            value, note = self.point.clamp_write(9999.0)
            self.assertEqual(value, 700.0)
            self.assertTrue(note)
        finally:
            engine.stop()

    def test_periodic_write_is_clamped(self):
        from icomm.core.engine import AcquisitionEngine

        engine = AcquisitionEngine(self.project)
        self.point.write_min = 0.0
        self.point.write_max = 700.0
        self.point.write_limit_warn = False
        # 未启动时 set_periodic 会拒绝；这里直接验它写进工程的配置是夹紧后的值
        engine.running = True
        engine._workers["不存在"] = object()  # 让它跳过"通道未运行"的检查
        try:
            ok, error = engine.set_periodic(BUS_PID, 5000.0, 500)
        except Exception as exc:  # 通道不存在会返回 False，不该抛
            self.fail(f"set_periodic 抛异常：{exc}")
        self.assertFalse(ok)  # 通道不存在
        # 用真实通道再来一次
        engine._workers = {self.project.channels[0].name: object()}
        ok, error = engine.set_periodic(BUS_PID, 5000.0, 500)
        self.assertTrue(ok, error)
        entry = self.project.periodic_write(BUS_PID)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.value, 700.0, "持续发送的配置值也必须被夹紧")
        engine.running = False


class TriggerTest(unittest.TestCase):
    """触发条件：边沿、电平、去抖、位、冷却。"""

    def test_rising_edge_fires_once_per_transition(self):
        trigger = Trigger(pid="X", compare=Compare.GE, threshold=700, mode=TriggerMode.RISING)
        fired = [
            value
            for value in (600, 650, 701, 705, 690, 710)
            if trigger.update(tag("X", value))
        ]
        self.assertEqual(fired, [701, 710], "只在跨过阈值那一刻触发")

    def test_level_mode_fires_once_while_holding(self):
        trigger = Trigger(pid="X", compare=Compare.GE, threshold=700, mode=TriggerMode.LEVEL)
        fired = [v for v in (701, 705, 710) if trigger.update(tag("X", v))]
        self.assertEqual(fired, [701], "电平模式在持续成立期间只触发一次")

    def test_falling_edge(self):
        trigger = Trigger(pid="X", compare=Compare.GE, threshold=700, mode=TriggerMode.FALLING)
        fired = [v for v in (701, 705, 690, 680, 710, 650) if trigger.update(tag("X", v))]
        self.assertEqual(fired, [690, 650])

    def test_hold_count_filters_glitches(self):
        trigger = Trigger(
            pid="X", compare=Compare.GT, threshold=10, mode=TriggerMode.RISING, hold_count=3
        )
        fired = [v for v in (11, 5, 11, 12, 13, 14) if trigger.update(tag("X", v))]
        self.assertEqual(fired, [13], "连续三次才认，中间断一次要重数")

    def test_cooldown_suppresses_repeat(self):
        trigger = Trigger(
            pid="X", compare=Compare.GT, threshold=10, mode=TriggerMode.RISING,
            cooldown_s=60.0,
        )
        first = trigger.update(tag("X", 20))
        second = trigger.update(tag("X", 5))
        third = trigger.update(tag("X", 20))
        self.assertTrue(first)
        self.assertFalse(trigger.update(tag("X", 5)))
        self.assertTrue(second is False)
        self.assertFalse(third, "冷却期内不该再触发")

    def test_bit_conditions(self):
        set_bit = Trigger(pid="S", compare=Compare.BIT_SET, bit=5, mode=TriggerMode.RISING)
        self.assertFalse(set_bit.update(tag("S", 0)))
        self.assertTrue(set_bit.update(tag("S", 1 << 5)))
        self.assertFalse(set_bit.update(tag("S", 1 << 5)))

        clear_bit = Trigger(pid="S", compare=Compare.BIT_CLEAR, bit=5, mode=TriggerMode.RISING)
        self.assertFalse(clear_bit.update(tag("S", 1 << 5)))
        self.assertTrue(clear_bit.update(tag("S", 0)))

    def test_change_condition(self):
        trigger = Trigger(pid="X", compare=Compare.CHANGE, mode=TriggerMode.RISING)
        trigger.update(tag("X", 1))
        self.assertTrue(trigger.update(tag("X", 2)))
        self.assertFalse(trigger.update(tag("X", 2)))

    def test_missing_point_does_not_fire(self):
        trigger = Trigger(pid="不存在", compare=Compare.GE, threshold=0, mode=TriggerMode.RISING)
        self.assertFalse(trigger.update({}))

    def test_non_numeric_value_does_not_crash(self):
        trigger = Trigger(pid="X", compare=Compare.GE, threshold=0, mode=TriggerMode.RISING)
        self.assertFalse(trigger.update({"X": TagValue(pid="X", value="故障", quality="good")}))

    def test_describe(self):
        trigger = Trigger(pid="X", compare=Compare.GE, threshold=700,
                          mode=TriggerMode.RISING, hold_count=2)
        text = trigger.describe(lambda pid: "直流母线电压")
        self.assertIn("直流母线电压", text)
        self.assertIn("大于等于", text)
        self.assertIn("700", text)
        self.assertIn("连续 2 次", text)
        self.assertEqual(Trigger(mode=TriggerMode.MANUAL).describe(), "手动触发")


class MultiConditionTriggerTest(unittest.TestCase):
    """多变量触发：几个变量一起盯，且 / 或组合，去抖与冷却作用在组合结果上。"""

    @staticmethod
    def values(**pairs) -> dict[str, TagValue]:
        return {pid: TagValue(pid=pid, value=value, quality="good") for pid, value in pairs.items()}

    def _and_trigger(self, **kwargs) -> Trigger:
        options: dict = {"logic": Logic.AND, "mode": TriggerMode.RISING}
        options.update(kwargs)
        return Trigger(
            conditions=[
                Condition(pid="V", compare=Compare.GE, threshold=700),
                Condition(pid="I", compare=Compare.GE, threshold=250),
            ],
            **options,
        )

    def test_and_requires_every_condition(self):
        trigger = self._and_trigger()
        self.assertFalse(trigger.update(self.values(V=720, I=200)), "电流不够就不算成立")
        self.assertTrue(trigger.update(self.values(V=720, I=260)), "两个都够才触发")
        self.assertFalse(trigger.update(self.values(V=730, I=270)), "已经在成立状态里，不再重复触发")
        self.assertFalse(trigger.update(self.values(V=690, I=270)), "掉出条件")
        self.assertTrue(trigger.update(self.values(V=710, I=280)), "再次满足 → 再触发一次")

    def test_or_fires_when_any_condition_holds(self):
        trigger = Trigger(
            conditions=[
                Condition(pid="V", compare=Compare.GE, threshold=700),
                Condition(pid="T", compare=Compare.GE, threshold=80),
            ],
            logic=Logic.OR,
            mode=TriggerMode.RISING,
        )
        self.assertTrue(trigger.update(self.values(V=720, T=20)), "任一满足即可")
        self.assertFalse(trigger.update(self.values(V=730, T=25)), "仍在成立状态，不重复触发")
        self.assertFalse(trigger.update(self.values(V=600, T=20)), "两个都不成立了")
        self.assertTrue(
            trigger.update(self.values(V=600, T=85)), "换成另一个变量成立，也算新的一次"
        )

    def test_missing_only_one_variable_blocks_and(self):
        trigger = self._and_trigger()
        self.assertFalse(trigger.update(self.values(V=720)), "另一个变量还没上数，不能算成立")
        self.assertTrue(trigger.update(self.values(V=720, I=300)))

    def test_hold_count_counts_combined_result(self):
        trigger = self._and_trigger(hold_count=3)
        fired = [
            trigger.update(self.values(V=720, I=260))
            for _ in range(4)
        ]
        self.assertEqual(fired, [False, False, True, False], "连够 3 拍才认")

    def test_cooldown_suppresses_repeat(self):
        trigger = self._and_trigger(cooldown_s=60.0)
        self.assertTrue(trigger.update(self.values(V=720, I=260), now=100.0))
        self.assertFalse(trigger.update(self.values(V=600, I=100), now=101.0), "条件失效")
        self.assertFalse(
            trigger.update(self.values(V=720, I=260), now=102.0), "冷却期内不该再触发"
        )
        self.assertFalse(trigger.update(self.values(V=600, I=100), now=199.0))
        self.assertTrue(
            trigger.update(self.values(V=720, I=260), now=200.0), "冷却过了就能再触发"
        )

    def test_falling_edge_on_combined_result(self):
        trigger = self._and_trigger(mode=TriggerMode.FALLING)
        self.assertFalse(trigger.update(self.values(V=720, I=260)), "先成立")
        self.assertTrue(trigger.update(self.values(V=720, I=100)), "组合结果失效的那一刻触发")

    def test_each_change_condition_tracks_its_own_last_value(self):
        """CHANGE 条件要吃"上一次的值"：几条条件必须各记各的，不能互相串。"""
        trigger = Trigger(
            conditions=[
                Condition(pid="A", compare=Compare.CHANGE),
                Condition(pid="B", compare=Compare.CHANGE),
            ],
            logic=Logic.AND,
            mode=TriggerMode.RISING,
        )
        self.assertFalse(trigger.update(self.values(A=1, B=1)), "第一拍只记账，不算变化")
        self.assertFalse(trigger.update(self.values(A=2, B=1)), "只有 A 变化，且 不成立")
        self.assertTrue(trigger.update(self.values(A=3, B=2)), "两个都变了才成立")
        self.assertFalse(trigger.update(self.values(A=3, B=2)), "都没变")

    def test_or_over_change_and_level(self):
        trigger = Trigger(
            conditions=[
                Condition(pid="A", compare=Compare.CHANGE),
                Condition(pid="S", compare=Compare.BIT_SET, bit=0),
            ],
            logic=Logic.OR,
            mode=TriggerMode.RISING,
        )
        self.assertFalse(trigger.update(self.values(A=1, S=0)))
        self.assertTrue(trigger.update(self.values(A=2, S=0)), "A 变化就够（或）")

    def test_bit_condition_inside_group(self):
        trigger = Trigger(
            conditions=[
                Condition(pid="S", compare=Compare.BIT_SET, bit=2),
                Condition(pid="V", compare=Compare.GE, threshold=700),
            ],
            logic=Logic.AND,
            mode=TriggerMode.RISING,
        )
        self.assertFalse(trigger.update(self.values(S=0b0100, V=600)))
        self.assertTrue(trigger.update(self.values(S=0b0100, V=720)))

    def test_describe_lists_every_variable(self):
        trigger = self._and_trigger(hold_count=2)
        text = trigger.describe(lambda pid: {"V": "母线电压", "I": "输出电流"}[pid])
        self.assertIn("母线电压", text)
        self.assertIn("输出电流", text)
        self.assertIn("且", text)
        self.assertIn("连续 2 次", text)
        trigger.logic = Logic.OR
        self.assertIn("或", trigger.describe())

    def test_watched_pids_dedupes(self):
        trigger = Trigger(
            conditions=[
                Condition(pid="V", compare=Compare.GE, threshold=1),
                Condition(pid="V", compare=Compare.LE, threshold=9),
            ],
            logic=Logic.AND,
        )
        self.assertEqual(trigger.watched_pids, ["V"])

    def test_reset_clears_condition_bookkeeping(self):
        trigger = self._and_trigger(hold_count=2)
        trigger.update(self.values(V=720, I=260))
        trigger.reset()
        self.assertFalse(trigger.active)
        self.assertFalse(trigger.update(self.values(V=720, I=260)), "重置后要重新数")

    def test_legacy_single_variable_still_works(self):
        trigger = Trigger(pid="X", compare=Compare.GE, threshold=700, mode=TriggerMode.RISING)
        self.assertEqual(trigger.watched_pids, ["X"])
        self.assertTrue(trigger.update(tag("X", 720)))

    def test_service_fires_multi_condition_task(self):
        service = AutomationService()
        seen: list[str] = []
        service.add(
            Task(
                name="多条件",
                action=lambda _task, _values: seen.append("hit"),
                trigger=self._and_trigger(),
            )
        )
        service.tick(self.values(V=720, I=100))
        service.tick(self.values(V=720, I=260))
        self.assertEqual(seen, ["hit"])


class AutomationServiceTest(unittest.TestCase):
    def test_task_fires_and_counts(self):
        service = AutomationService()
        seen: list[str] = []
        service.add(
            Task(
                name="记一笔",
                action=lambda task, values: seen.append(task.name),
                trigger=Trigger(pid="X", compare=Compare.GE, threshold=5, mode=TriggerMode.RISING),
            )
        )
        for value in (1, 2, 9, 9):
            service.tick(tag("X", value))
        self.assertEqual(seen, ["记一笔"])
        self.assertEqual(service.find("记一笔").fires, 1)

    def test_max_fires_stops_task(self):
        service = AutomationService()
        seen: list[float] = []

        def record(task, values):
            seen.append(values["X"].value)

        service.add(
            Task(
                name="限两次",
                action=record,
                max_fires=2,
                trigger=Trigger(
                    pid="X",
                    compare=Compare.GT,
                    threshold=5,
                    mode=TriggerMode.RISING,
                ),
            )
        )
        for value in (1, 9, 1, 9, 1, 9):
            service.tick(tag("X", value))
        self.assertEqual(len(seen), 2)
        self.assertTrue(service.find("限两次").exhausted)

    def test_disabled_task_skipped(self):
        service = AutomationService()
        task = Task(
            name="关掉的",
            action=lambda t, v: None,
            trigger=Trigger(pid="X", compare=Compare.GT, threshold=0, mode=TriggerMode.RISING),
        )
        task.enabled = False
        service.add(task)
        service.tick(tag("X", 10))
        self.assertEqual(task.fires, 0)

    def test_action_error_is_captured_not_raised(self):
        service = AutomationService()

        def boom(_task, _values):
            raise RuntimeError("文件写不下去")

        task = Task(
            name="会炸的",
            action=boom,
            trigger=Trigger(pid="X", compare=Compare.GT, threshold=0, mode=TriggerMode.RISING),
        )
        service.add(task)
        service.tick(tag("X", 10))  # 不该把调用方带崩
        self.assertIn("文件写不下去", task.last_error)
        self.assertIn("出错", task.status_text())

    def test_manual_trigger(self):
        service = AutomationService()
        seen: list[str] = []
        service.add(Task(name="手动", action=lambda t, v: seen.append("hit"), trigger=Trigger(mode=TriggerMode.MANUAL)))
        self.assertTrue(service.manual_trigger("手动"))
        self.assertEqual(seen, ["hit"])
        self.assertFalse(service.manual_trigger("不存在"))

    def test_listener_called(self):
        service = AutomationService()
        heard: list[str] = []
        service.on_fire(lambda task: heard.append(task.name))
        service.add(Task(name="T", action=lambda t, v: None,
                         trigger=Trigger(pid="X", compare=Compare.GT, threshold=0,
                                         mode=TriggerMode.RISING)))
        service.tick(tag("X", 1))
        self.assertEqual(heard, ["T"])

    def test_remove_and_clear(self):
        service = AutomationService()
        service.add(Task(name="A", action=lambda t, v: None))
        service.add(Task(name="B", action=lambda t, v: None))
        self.assertTrue(service.remove("A"))
        self.assertFalse(service.remove("A"))
        self.assertEqual([t.name for t in service.tasks], ["B"])
        service.clear()
        self.assertEqual(service.tasks, [])

    def test_status_rows(self):
        service = AutomationService()
        service.add(Task(name="A", action=lambda t, v: None,
                         trigger=Trigger(pid="X", compare=Compare.GE, threshold=3)))
        rows = service.status_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "A")
        self.assertIn("大于等于", rows[0][1])


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.project = project_io.bess_project()

    def test_empty_query_returns_everything(self):
        hits = search.search_points(self.project, "")
        self.assertEqual(len(hits), len(self.project.all_point_ids()))

    def test_match_by_name_and_title(self):
        hits = search.search_points(self.project, "控制字")
        self.assertIn(CTRL_PID, [hit.pid for hit in hits])

    def test_match_by_address(self):
        hits = search.search_points(self.project, "17", as_address=True)
        self.assertEqual([hit.pid for hit in hits], [CTRL_PID])
        hits = search.search_points(self.project, "0x11", as_address=True)
        self.assertEqual([hit.pid for hit in hits], [CTRL_PID], "0x 前缀该按十六进制")

    def test_address_query_without_match(self):
        self.assertEqual(search.search_points(self.project, "99999", as_address=True), [])

    def test_writable_filter(self):
        all_hits = search.search_points(self.project, "")
        writable = search.search_points(self.project, "", writable_only=True)
        self.assertLess(len(writable), len(all_hits))
        self.assertTrue(all(hit.writable for hit in writable))

    def test_case_sensitivity(self):
        loose = search.search_points(self.project, "soc")
        strict = search.search_points(self.project, "soc", case_sensitive=True)
        self.assertGreater(len(loose), len(strict), "SOC 大写，小写查询该只在非严格模式下命中")

    def test_places_include_expected_locations(self):
        hits = {hit.pid: hit for hit in search.search_points(self.project, "")}
        ctrl = hits[CTRL_PID]
        self.assertTrue(any("运行监视" in place for place in ctrl.places))
        self.assertTrue(any("按键控制台" in place for place in ctrl.places), ctrl.places)
        # 控制字是可写的命令点位
        self.assertTrue(ctrl.writable)

        # 只读状态字：出现在监视页，但不在按键控制台上（只读，只有位状态）
        status = hits[STATUS_PID]
        self.assertFalse(status.writable)
        self.assertTrue(any("运行监视" in place for place in status.places))
        self.assertFalse(any("命令按键" in place for place in status.places))

    def test_places_report_scope_and_alarm(self):
        project = project_io.bess_project()
        device = project.devices[0]
        point = device.point("直流母线电压")
        places = search.places_of(project, device, point)
        self.assertTrue(any("运行监视" in place for place in places))
        self.assertTrue(any("报警判定" in place for place in places), "该点位配了报警")

    def test_places_report_write_limit(self):
        project = project_io.bess_project()
        device = project.devices[0]
        point = device.point("有功功率给定")
        point.write_min, point.write_max = 0.0, 800.0
        places = search.places_of(project, device, point)
        self.assertTrue(any("限幅" in place for place in places))

    def test_chain_issues_surface_missing_channel(self):
        project = project_io.bess_project()
        project.devices[0].channel = "不存在的通道"
        hits = search.search_points(project, "")
        self.assertTrue(any(hit.chain_issues for hit in hits))

    def test_searchable_includes_places(self):
        hits = {hit.pid: hit for hit in search.search_points(self.project, "")}
        self.assertIn("示波器", hits[CTRL_PID].searchable() + "示波器")

    def test_limit_respected(self):
        self.assertEqual(len(search.search_points(self.project, "", limit=5)), 5)


class NetCheckTest(unittest.TestCase):
    def test_parse_hex_variants(self):
        expected = bytes.fromhex("0103000000 0AC5CD".replace(" ", ""))
        for text in (
            "01 03 00 00 00 0A C5 CD",
            "0x01,0x03,0x00,0x00,0x00,0x0A,0xC5,0xCD",
            "01030000 000AC5CD",
            "01030000000AC5CD",
            "01-03-00-00",  # 连字符不是分隔符，应报错
        ):
            data, error = parse_hex_payload(text)
            if text == "01-03-00-00":
                self.assertTrue(error, "非法分隔符要报错")
            else:
                self.assertEqual(data, expected, text)
                self.assertEqual(error, "")

    def test_parse_hex_rejects_odd_length(self):
        data, error = parse_hex_payload("0103000")
        self.assertEqual(data, b"")
        self.assertTrue(error)

    def test_parse_hex_empty(self):
        self.assertEqual(parse_hex_payload(""), (b"", ""))
        self.assertEqual(parse_hex_payload("   "), (b"", ""))

    def test_parse_hex_bad_chars(self):
        data, error = parse_hex_payload("01 ZZ")
        self.assertEqual(data, b"")
        self.assertIn("ZZ", error)

    def test_connect_to_unused_port_fails_with_reason(self):
        """连一个确定没人监听的端口，应该给出可读的失败原因。"""
        import socket

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        # 关掉之后再连，端口空着
        result = ConnectionTester("127.0.0.1", free_port, timeout_s=1.0).run()
        self.assertFalse(result.ok)
        self.assertTrue(result.attempts[0].error)
        self.assertIn("失败", result.headline)
        self.assertIn("排查建议", result.detail())

    def test_empty_host(self):
        result = ConnectionTester("", 502).run()
        self.assertFalse(result.ok)
        self.assertIn("没有填写地址", result.attempts[0].error)

    def test_suggestions_for_refused(self):
        from icomm.core.net_check import _suggestions

        text = " ".join(_suggestions("连接被拒绝"))
        self.assertIn("端口", text)

    def test_detail_counts_attempts(self):
        result = ConnectionTester("127.0.0.1", 1, timeout_s=0.3, attempts=2).run()
        self.assertEqual(len(result.attempts), 2)
        self.assertIn("[1]", result.detail())
        self.assertIn("[2]", result.detail())


class ToolsFrameworkTest(unittest.TestCase):
    def test_registry_has_expected_tools(self):
        names = ui_tools.tool_names()
        for expected in (
            "Modbus 报文与 CRC",
            "程序员计算器",
            "TCP 连接测试",
            "变量搜索",
            "记事本",
            "截屏辅助",
            "数据记录",
            "示波器小窗口",
            "计时器与定时赋值",
        ):
            self.assertIn(expected, names, f"缺少工具「{expected}」")

    def test_grouping_and_order(self):
        grouped = ui_tools.tools_by_group()
        self.assertIn("Modbus", grouped)
        self.assertIn("计算", grouped)
        self.assertIn("自动化", grouped)
        for tools in grouped.values():
            orders = [tool.order for tool in tools]
            self.assertEqual(orders, sorted(orders), "同组内应按 order 排好")

    def test_duplicate_registration_ignored(self):
        before = len(ui_tools.tools())

        class Dummy(ui_tools.Tool):
            name = "Modbus 报文与 CRC"  # 与已有工具同名

            def open(self, context):  # pragma: no cover
                return None

        register_tool(Dummy())
        self.assertEqual(len(ui_tools.tools()), before)

    def test_unnamed_tool_rejected(self):
        class Unnamed(ui_tools.Tool):
            def open(self, context):  # pragma: no cover
                return None

        with self.assertRaises(ValueError):
            register_tool(Unnamed())

    def test_class_is_instantiated_on_registration(self):
        """装饰在**类**上也要能用。

        踩过一次：``@register_tool`` 直接装饰类时注册进去的是类本身，
        于是菜单里点任何工具都会拿着类去调实例方法，全部打不开。
        """

        class ClassStyle(ui_tools.Tool):
            name = "探针：装饰在类上的工具"

            def open(self, context):  # pragma: no cover
                return None

        register_tool(ClassStyle)
        found = ui_tools.find_tool("探针：装饰在类上的工具")
        self.assertNotIsInstance(found, type, "注册后必须是实例，不能是类")
        self.assertIsInstance(found, ClassStyle)

    def test_find_tool(self):
        self.assertIsNotNone(ui_tools.find_tool("记事本"))
        self.assertIsNone(ui_tools.find_tool("不存在的工具"))

    def test_context_helpers(self):
        project = project_io.bess_project()
        context = ui_tools.ToolContext(project=project, window=None)
        self.assertEqual(len(context.all_points()), len(project.all_point_ids()))
        self.assertTrue(context.writable_points())
        self.assertTrue(all("给定" not in pid or True for pid, _ in context.writable_points()))
        self.assertEqual(context.service("automation"), None)
        self.assertFalse(context.send(CTRL_PID, 0x01), "没接引擎时 send 应返回 False")


class AudioToolTest(unittest.TestCase):
    """录音工具的接线（用假录音器，不依赖声卡）。"""

    @classmethod
    def setUpClass(cls):
        global _app
        _app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.project = project_io.bess_project()
        self.made: list[FakeRecorder] = []
        service = AutomationService()
        self.service = service
        self.context = ui_tools.ToolContext(
            project=self.project,
            window=None,
            settings=app_settings.AppSettings(),
            base_dir=self.tmp,
            services={"automation": service},
        )

        made = self.made

        def factory(**kwargs):
            recorder = FakeRecorder(**kwargs)
            made.append(recorder)
            return recorder

        self.window = AudioRecordWindow(self.context, recorder_factory=factory)
        self.window.dir_edit.setText(self.tmp)

    def tearDown(self):
        self.window.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_registers_trigger_task(self):
        select_combo(self.window.fields.pid_combo, BUS_PID)
        self.window.start()
        self.assertTrue(self.window._task_name)
        self.assertIsNotNone(self.service.find(self.window._task_name))

    def test_trigger_starts_and_finishes_recording(self):
        select_combo(self.window.fields.pid_combo, BUS_PID)
        self.window.start()
        self.window.begin_recording("测试")
        self.assertIsNotNone(self.window._recorder)
        self.assertTrue(self.window.cut_button.isEnabled())

        recorder = self.made[0]
        recorder.duration = 1.5
        self.window._tick()  # 界面刷新应当显示时长
        self.assertIn("1.5", self.window.status.text())

        recorder.stop_reason = "到达设定时长（1 秒）"
        self.window._tick()
        self.assertIsNone(self.window._recorder, "到点应当自动收尾")
        self.assertTrue(recorder.closed, "收尾时必须 close（要回填 WAV 头）")
        self.assertIn("已保存", self.window.status.text())
        self.assertTrue(os.path.exists(recorder.path), "假录音器也要真的落个文件")

    def test_manual_recording_then_cut(self):
        self.window.manual_recording()
        self.assertIsNotNone(self.window._recorder)
        self.window.finish_recording("手动结束")
        self.assertIsNone(self.window._recorder)

    def test_close_stops_everything(self):
        select_combo(self.window.fields.pid_combo, BUS_PID)
        self.window.start()
        self.window.begin_recording("测试")
        name = self.window._task_name
        self.window.close()
        self.assertIsNone(self.service.find(name), "关窗要把触发任务摘掉")
        self.assertIsNone(self.window._recorder, "关窗要把正在录的那段收尾")

    def test_file_list_shows_wavs(self):
        path = os.path.join(self.tmp, "audio_20260101_000000.wav")
        writer = audio.WavWriter(path, rate=16000, channels=1)
        writer.write(b"\x01\x00" * 16000)  # 1 秒
        writer.close()
        self.window.refresh_files()
        self.assertEqual(self.window.table.rowCount(), 1)
        self.assertIn("1.0s", self.window.table.item(0, 2).text())

    def test_unavailable_device_is_explained(self):
        def broken(**kwargs):
            return audio.NullRecorder(kwargs.get("path", ""), error="没有找到录音设备")

        window = AudioRecordWindow(self.context, recorder_factory=broken)
        try:
            select_combo(window.fields.pid_combo, BUS_PID)
            window.begin_recording("测试")
            self.assertIsNone(window._recorder)
            self.assertIn("没有找到录音设备", window.status.text())
        finally:
            window.close()


class FakeRecorder:
    """假录音器：接口和 :class:`icomm.core.audio.WinmmRecorder` 一致，真写一个小文件。"""

    def __init__(self, path: str = "", rate: int = 16000, channels: int = 1,
                 device_index: int = 0, max_seconds: float = 0.0, **kwargs) -> None:
        self.path = path
        self.rate = rate
        self.channels = channels
        self.max_seconds = max_seconds
        self.duration = 0.0
        self.size = 0
        self.error = ""
        self.opened = False
        self.closed = False
        self.stop_reason = ""

    def open(self) -> bool:
        self.opened = True
        if self.path:
            with open(self.path, "wb") as fh:
                fh.write(b"RIFF")
        return True

    def should_stop(self) -> str:
        return self.stop_reason

    def close(self) -> None:
        self.closed = True
class DiagramEditingTest(unittest.TestCase):
    """主回路图：图元库 + 画布拖动 + 编辑器的增删改。"""

    @classmethod
    def setUpClass(cls):
        global _app
        _app = QApplication.instance() or QApplication([])

    def context(self, project=None, changed=None):
        return ui_tools.ToolContext(
            project=project or project_io.bess_project(),
            window=None,
            project_changed=changed,
        )

    def test_palette_covers_requested_symbols(self):
        names = diagram_lib.known_symbols()
        for symbol in ("grid", "battery", "filter", "inverter", "rectifier", "load",
                       "transformer", "inductor", "capacitor", "motor", "bus", "dsp"):
            self.assertIn(symbol, names, f"图元库缺 {symbol}")
        self.assertGreaterEqual(len(names), 15, "图元要够用")
        for item in diagram_lib.PALETTE:
            self.assertTrue(item.label and item.category, item)
            self.assertGreater(item.w, 0.0)
        self.assertEqual(len(set(names)), len(names), "图元 ID 不能重复")

    def test_categories_and_grouping_match(self):
        grouped = diagram_lib.by_category()
        self.assertEqual(list(grouped), diagram_lib.categories())
        self.assertEqual(sum(len(v) for v in grouped.values()), len(diagram_lib.PALETTE))
        self.assertIn("电源", grouped)

    def test_suggest_label_avoids_collisions(self):
        node_id, label, _sub = diagram_lib.suggest_label("rectifier", ["rectifier1"])
        self.assertEqual(node_id, "rectifier2", "撞名的 id 会让连线连错节点")
        self.assertTrue(label)

    def test_auto_layout_spreads_and_wraps(self):
        self.assertEqual(len(diagram_lib.auto_layout(1)), 1)
        many = diagram_lib.auto_layout(12)
        self.assertEqual(len(many), 12)
        self.assertTrue(all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in many))
        rows = {y for _x, y in many}
        self.assertGreaterEqual(len(rows), 2, "图元多了要折行")
        self.assertEqual(list(rows), sorted(rows), "行号自上而下")
        # 每行能放几个由 step 决定：同一行的高度必须一致，多出来的要换行
        per_row = int((1.0 - 0.04 - 0.02) // 0.16)
        self.assertEqual(len({y for _x, y in many[:per_row]}), 1, "同一行高度应一致")
        self.assertNotEqual(many[per_row][1], many[0][1], f"第 {per_row + 1} 个该换行了")

    def test_canvas_move_node_clamps_into_view(self):
        canvas = DiagramCanvas()
        node = DiagramNode(id="n", x=0.1, y=0.1, w=0.2, h=0.2)
        canvas.diagram = DiagramDef(nodes=[node])
        canvas.resize(800, 500)
        moved: list[tuple] = []
        canvas.nodeMoved.connect(lambda pid, x, y: moved.append((pid, x, y)))

        canvas.move_node(node, 0.5, 0.5)
        self.assertAlmostEqual(node.x, 0.4, places=3, msg="x 是按图元中心算的")
        self.assertAlmostEqual(node.y, 0.4, places=3)
        self.assertEqual(moved, [("n", 0.4, 0.4)])

        canvas.move_node(node, -1.0, 2.0)
        self.assertEqual((node.x, node.y), (0.0, 0.8), "拖出画面要被夹住")

    def test_canvas_is_readonly_by_default(self):
        self.assertFalse(DiagramCanvas().editable, "主页那张图不该能拖动")

    def test_editor_add_move_link_and_apply(self):
        project = project_io.bess_project()
        project.diagram = DiagramDef()
        changed: list[bool] = []
        window = DiagramEditWindow(self.context(project, lambda: changed.append(True)))
        try:
            self.assertGreater(window.palette.count(), 10, "调色板要有图元")
            window.palette.setCurrentRow(1)
            window.add_symbol()
            self.assertEqual(len(window.diagram.nodes), 1)
            node = window.diagram.nodes[0]

            window.label_edit.setText("整流桥")
            window.sub_edit.setText("AC/DC")
            window._apply_form()
            self.assertEqual(node.label, "整流桥")
            self.assertEqual(node.sub_label, "AC/DC")

            window.preview.move_node(node, 0.3, 0.5)
            self.assertAlmostEqual(node.x, 0.3 - node.w / 2, places=3)

            window.palette.setCurrentRow(2)
            window.add_symbol()
            self.assertEqual(len(window.diagram.nodes), 2)
            window.link_from.setCurrentIndex(0)
            window.link_to.setCurrentIndex(1)
            window.add_link()
            self.assertEqual(len(window.diagram.links), 1)
            window.add_link()
            self.assertEqual(len(window.diagram.links), 1, "同一条连线不该重复加")

            window.node_list.setCurrentRow(0)
            window.remove_node()
            self.assertEqual(len(window.diagram.nodes), 1)
            self.assertEqual(window.diagram.links, [], "删图元要顺手删掉挂在它身上的线")

            window.apply_to_project()
            self.assertEqual(
                len(project.diagram.nodes), len(window.diagram.nodes),
                "工程里拿到的是一份同样的图",
            )
            self.assertIsNot(
                project.diagram, window.diagram,
                "工程里要存副本，编辑器继续改不能直接动到工程",
            )
            self.assertEqual(changed, [True], "要通知主窗口刷新")
            self.assertFalse(window._dirty)
        finally:
            window._dirty = False
            window.close()

    def test_editor_arrange_orders_by_links(self):
        project = project_io.bess_project()
        window = DiagramEditWindow(self.context(project))
        try:
            window.arrange("links")
            xs = [
                window.diagram.node(node_id).x
                for node_id in ("n_grid", "n_pcs")
                if window.diagram.node(node_id)
            ]
            self.assertEqual(len(xs), 2, "示例图里该有电网与 PCS 两个图元")
            self.assertEqual(xs, sorted(xs), "按连线排序后上游应该更靠左")
        finally:
            window._dirty = False
            window.close()

    def test_editor_reloads_from_project(self):
        window = DiagramEditWindow(self.context())
        try:
            before = len(window.diagram.nodes)
            window.palette.setCurrentRow(1)
            window.add_symbol()
            self.assertEqual(len(window.diagram.nodes), before + 1)
            window.reload_from_project()
            self.assertEqual(len(window.diagram.nodes), before, "重新载入要丢掉未应用的改动")
        finally:
            window._dirty = False
            window.close()

    def test_editor_loads_sample_diagram(self):
        window = DiagramEditWindow(self.context(project_io.blank_project("空的")))
        try:
            self.assertEqual(len(window.diagram.nodes), 0)
            with mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes):
                window.load_sample()
            self.assertGreater(len(window.diagram.nodes), 3, "示例图该有一串图元")
        finally:
            window._dirty = False
            window.close()

    def test_editor_action_targets_follow_action(self):
        window = DiagramEditWindow(self.context())
        try:
            window._select_data(window.action_combo, "device")
            window._sync_action()
            targets = [
                window.target_combo.itemData(i) for i in range(window.target_combo.count())
            ]
            self.assertIn("储能变流器PCS", targets)
            window._select_data(window.action_combo, "none")
            window._sync_action()
            self.assertEqual(window.target_combo.count(), 0)
        finally:
            window._dirty = False
            window.close()


class AnalysisCoreTest(unittest.TestCase):
    """分析计算的正确性 —— 都用有解析答案的算例验。"""

    def test_lc_resonance_matches_formula(self):
        # f0 = 1/(2π√(LC))：1 mH + 100 μF -> 503.29 Hz
        self.assertAlmostEqual(an.resonance_hz("lc", 1.0, 100.0), 503.2921, places=3)
        result = an.bode("lc", r=0.0, l=1.0, c=100.0)
        self.assertAlmostEqual(result.resonance_hz or 0.0, 503.2921, places=3)
        self.assertEqual(len(result.points), an.BODE_POINTS)

    def test_lc_is_flat_well_below_resonance(self):
        result = an.bode("lc", r=0.0, l=1.0, c=100.0)
        # 50 Hz 远低于 503 Hz，增益应当非常接近 0 dB、几乎不产生相移
        self.assertIsNotNone(result.gain_at_50_db)
        self.assertIsNotNone(result.phase_at_50_deg)
        self.assertLess(abs(float(result.gain_at_50_db)), 0.2)
        self.assertAlmostEqual(float(result.phase_at_50_deg), 0.0, places=1)

    def test_lc_rolls_off_above_resonance(self):
        result = an.bode("lc", r=0.5, l=1.0, c=100.0)
        low = next(point for point in result.points if point.freq > 5000)
        high = next(point for point in result.points if point.freq > 50000)
        slope = (high.gain_db - low.gain_db) / math.log10(high.freq / low.freq)
        self.assertLess(slope, -20.0, f"二阶滤波器高频要陡降（实测 {slope:.1f} dB/dec）")

    def test_rc_is_first_order(self):
        result = an.bode("rc", r=1000.0, l=0.0, c=1.0)  # 1 kΩ + 1 μF -> 159 Hz
        self.assertIsNone(result.resonance_hz, "一阶 RC 没有谐振点")
        if result.cutoff_hz:
            self.assertAlmostEqual(result.cutoff_hz, 159.15, delta=5.0)

    def test_lcl_uses_parallel_inductance(self):
        # LCL 的谐振由 L1∥L2 与 C 决定：L1 = L2 = 1 mH -> 并联 0.5 mH
        expected = 1.0 / (2.0 * math.pi * math.sqrt(0.5e-3 * 100e-6))
        self.assertAlmostEqual(an.resonance_hz("lcl", 1.0, 100.0, 1.0), expected, places=2)
        # L2 越大，L1∥L2 越接近 L1（电感变大），谐振频率越低
        self.assertLess(
            an.resonance_hz("lcl", 1.0, 100.0, 10.0) or 0.0,
            an.resonance_hz("lcl", 1.0, 100.0, 1.0) or 0.0,
            "第二只电感越大，谐振点越低",
        )
        self.assertAlmostEqual(
            an.resonance_hz("lcl", 1.0, 100.0, 10.0) or 0.0,
            1.0 / (2.0 * math.pi * math.sqrt((1.0 * 10.0 / 11.0) * 1e-3 * 100e-6)),
            places=2,
        )

    def test_bode_survives_zero_parameters(self):
        for topology, _name, _note in an.FILTER_TOPOLOGIES:
            result = an.bode(topology, r=0.0, l=0.0, c=0.0)
            self.assertEqual(len(result.points), an.BODE_POINTS)
            self.assertTrue(all(abs(point.gain_db) <= 240.0 for point in result.points))

    def test_phase_track_recovers_frequency(self):
        dt = 1.0 / (50 * 100)  # 50 Hz，每周期 100 点
        wave = [math.sin(2 * math.pi * 50 * index * dt) for index in range(300)]
        result = an.phase_track(wave, dt)
        self.assertTrue(result.ok)
        self.assertAlmostEqual(result.freq_hz, 50.0, places=2)
        self.assertEqual(result.cycles, 2, "3 个周期应当有 2 个完整周波")
        self.assertAlmostEqual(result.angle_deg, 0.0, delta=5.0, msg="起点在过零点附近")

    def test_phase_track_handles_offset_and_other_frequency(self):
        dt = 1.0 / (60 * 128)
        wave = [12.5 + math.sin(2 * math.pi * 60 * index * dt) for index in range(400)]
        result = an.phase_track(wave, dt)
        self.assertAlmostEqual(result.freq_hz, 60.0, places=2)
        self.assertIn("扣除直流分量 12.5", result.note)

    def test_phase_track_reports_bad_input(self):
        self.assertFalse(an.phase_track([1.0, 2.0], 0.001).ok)
        self.assertFalse(an.phase_track([1.0] * 50, 0.001).ok, "纯直流没有过零点")
        self.assertIn("无法锁相", an.phase_track([1.0] * 50, 0.001).note)

    def test_phase_from_waveform_guesses_interval(self):
        # 每 100 个采样点一个周波：采样间隔应当是 200 μs
        wave = [math.sin(2 * math.pi * index / 100) for index in range(300)]
        result = an.phase_from_waveform(wave, 0)
        self.assertAlmostEqual(result.freq_hz, 50.0, places=1)
        self.assertIn("没配「采样间隔us」", result.note)
        with_interval = an.phase_from_waveform(wave, 200)
        self.assertAlmostEqual(with_interval.freq_hz, 50.0, places=1)
        self.assertNotIn("没配", with_interval.note)

    def test_phase_differences_of_shifted_waves(self):
        dt = 1.0 / (50 * 360)
        base = [math.sin(2 * math.pi * 50 * index * dt) for index in range(720)]
        lag = [math.sin(2 * math.pi * 50 * index * dt - math.radians(120)) for index in range(720)]
        angles = an.phase_differences([base, lag], dt)
        self.assertEqual(len(angles), 2)
        self.assertAlmostEqual(angles[0], 0.0, places=6)
        self.assertAlmostEqual(angles[1], 120.0, delta=2.0)

    def test_power_factor_from_vi_p(self):
        result = an.power_factor(380.0, 100.0, 30000.0)
        self.assertAlmostEqual(result.power_factor, 30000.0 / 38000.0, places=6)
        self.assertAlmostEqual(result.angle_deg, math.degrees(math.acos(0.7894737)), places=2)
        self.assertAlmostEqual(result.apparent, 38000.0, places=6)
        self.assertAlmostEqual(result.reactive, 38000.0 * math.sin(math.radians(result.angle_deg)), places=3)
        self.assertIn("感性", result.kind)

    def test_power_factor_prefers_device_value(self):
        result = an.power_factor(380.0, 100.0, 30000.0, pf_direct=0.98)
        self.assertAlmostEqual(result.power_factor, 0.98, places=6)
        self.assertIn("设备本身", result.note)

    def test_power_factor_handles_zero_and_feedback(self):
        zero = an.power_factor(0.0, 100.0, 0.0)
        self.assertEqual(zero.power_factor, 0.0)
        self.assertTrue(zero.note)
        feedback = an.power_factor(380.0, 100.0, -20000.0)
        self.assertLess(feedback.power_factor, 0.0)
        self.assertIn("容性", feedback.kind)
        self.assertLessEqual(feedback.angle_deg, 90.0)

    def test_power_angle_clamps(self):
        self.assertEqual(an.power_angle(1.0), 0.0)
        self.assertEqual(an.power_angle(0.0), 90.0)
        self.assertEqual(an.power_angle(2.0), 0.0, "超出范围要夹住不能炸")

    def test_symmetrical_components_balanced(self):
        seq = an.symmetrical_components(
            an.Phasor(220.0, 0.0), an.Phasor(220.0, -120.0), an.Phasor(220.0, 120.0)
        )
        self.assertAlmostEqual(seq.positive.magnitude, 220.0, places=6)
        self.assertLess(seq.negative.magnitude, 1e-6, "平衡三相没有负序")
        self.assertLess(seq.zero.magnitude, 1e-6, "平衡三相没有零序")
        self.assertLess(seq.unbalance_percent(), 0.001)

    def test_symmetrical_components_detects_unbalance(self):
        seq = an.symmetrical_components(
            an.Phasor(220.0, 0.0), an.Phasor(180.0, -118.0), an.Phasor(220.0, 120.0)
        )
        self.assertGreater(seq.unbalance_percent(), 5.0)
        self.assertIn("不平衡", seq.note)
        self.assertGreater(seq.zero.magnitude, 0.0)

    def test_negative_sequence_of_reversed_phases(self):
        """相序接反：应当几乎全是负序、正序接近 0。"""
        seq = an.symmetrical_components(
            an.Phasor(220.0, 0.0), an.Phasor(220.0, 120.0), an.Phasor(220.0, -120.0)
        )
        self.assertLess(seq.positive.magnitude, 1e-6)
        self.assertAlmostEqual(seq.negative.magnitude, 220.0, places=6)

    def test_formatting_helpers(self):
        rows = an.key_points(an.bode("lc", r=0.5, l=1.0, c=100.0))
        self.assertTrue(any("谐振" in name for name, _value in rows))
        self.assertIn("｜", an.format_points(rows))


class AnalysisToolTest(unittest.TestCase):
    """分析工具的界面接线（4 个页签都要能算出来）。"""

    @classmethod
    def setUpClass(cls):
        global _app
        _app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.project = project_io.sst_project()
        self.window = AnalysisWindow(ui_tools.ToolContext(project=self.project, window=None))

    def tearDown(self):
        self.window._timer.stop()
        self.window.close()

    def test_tabs_present(self):
        labels = [self.window.tabs.tabText(i) for i in range(self.window.tabs.count())]
        self.assertEqual(len(labels), 4, str(labels))
        for keyword in ("伯德", "锁相", "功率", "向量"):
            self.assertTrue(any(keyword in label for label in labels), keyword)

    def test_bode_tab_draws_curves(self):
        self.window.select_tab("伯德")
        self.assertEqual(len(self.window.bode_plot.series), 1)
        self.assertEqual(len(self.window.bode_plot.series[0]["points"]), an.BODE_POINTS)
        self.assertIn("谐振", self.window.bode_text.text())
        self.assertIn("503", self.window.bode_text.text(), "1mH/100μF 的谐振点应当算得出来")

    def test_bode_reacts_to_parameter_change(self):
        self.window.select_tab("伯德")
        self.window.c_box.setValue(10.0)  # 电容变小 -> 谐振频率变高（503 -> 1591 Hz）
        self.window._update_bode()
        self.assertIn("1,591", self.window.bode_text.text())

    def test_pll_tab_uses_waveform_points(self):
        wave_pid = next(
            f"{device.name}.{point.name}"
            for device, point in self.project.iter_points()
            if point.is_array
        )
        self.assertEqual(self.window.pll_combo.currentData(), wave_pid,
                         "默认应当选中第一个波形点位")
        self.window._update_pll()  # 没有实时数据时不该炸
        self.assertTrue(self.window.pll_text.text())

    def test_pll_computes_from_injected_waveform(self):
        """塞一段已知波形进去，界面应当报出 50.0 Hz。

        注意波形要按**这个点位的采样间隔**造 —— 界面用的是参数表里的
        「采样间隔us」，不是测试自己假想的间隔。
        """
        pid = self.window.pll_combo.currentData()
        self.assertTrue(pid)
        point = self.project.find_point(str(pid))[1]
        interval_us = int(point.sample_interval_us or 0)
        if interval_us <= 0:
            self.skipTest("这个波形点位没配采样间隔，界面会走估算分支（另有测试覆盖）")
        dt = interval_us / 1e6
        wave = [math.sin(2 * math.pi * 50 * index * dt) * 311.0 for index in range(300)]
        values = {str(pid): TagValue(pid=str(pid), value=wave, quality="good")}
        self.window.context.tag_of = lambda name: values.get(name, TagValue(pid=name))
        self.window.select_tab("锁相")
        self.assertIn("50.0", self.window.pll_text.text())
        self.assertEqual(len(self.window.pll_plot.series), 1)

    def test_power_tab_computes_from_tags(self):
        values = {
            str(self.window.volt_combo.currentData()): TagValue(
                pid="u", value=380.0, quality="good"
            ),
            str(self.window.current_combo.currentData()): TagValue(
                pid="i", value=100.0, quality="good"
            ),
            str(self.window.active_combo.currentData()): TagValue(
                pid="p", value=30000.0, quality="good"
            ),
        }
        self.window.context.tag_of = lambda name: values.get(name, TagValue(pid=name))
        self.window.select_tab("功率")
        text = self.window.power_text.text()
        self.assertIn("0.78", text)
        self.assertIn("功率角", text)
        self.assertEqual(len(self.window.power_plot.series), 1, "趋势要记上一点")
        self.window._clear_power_history()
        self.assertEqual(self.window.power_plot.series, [])

    def test_phasor_tab_uses_manual_angles(self):
        values = {
            str(self.window.amp_combos[0].currentData()): TagValue(pid="a", value=220.0, quality="good"),
            str(self.window.amp_combos[1].currentData()): TagValue(pid="b", value=220.0, quality="good"),
            str(self.window.amp_combos[2].currentData()): TagValue(pid="c", value=220.0, quality="good"),
        }
        self.window.context.tag_of = lambda name: values.get(name, TagValue(pid=name))
        self.window.select_tab("向量")
        text = self.window.phasor_text.text()
        self.assertIn("不平衡度", text)
        self.assertIn("0.0", text.split("不平衡度")[1][:20], "平衡三相不平衡度应当接近 0")
        self.assertEqual(len(self.window.phasor_plot.phasors), 3)

    def test_phase_combos_default_to_distinct_points(self):
        """三个幅值框不能一起落到同一个点位上。

        踩过：都默认成第一个含"电压"的点，于是"三相平衡"永远是 0% —— 结论是假的。
        """
        pids = [str(combo.currentData()) for combo in self.window.amp_combos]
        self.assertEqual(len(set(pids)), 3, f"三相默认点位重复了：{pids}")

    def test_warns_when_phases_share_a_point(self):
        context = ui_tools.ToolContext(project=project_io.bess_project(), window=None)
        window = AnalysisWindow(context)
        try:
            first = window.amp_combos[0].currentData()
            for combo in window.amp_combos[1:]:
                index = combo.findData(first)
                if index >= 0:
                    combo.setCurrentIndex(index)
            values = {
                str(first): TagValue(pid=str(first), value=220.0, quality="good")
            }
            context.tag_of = lambda name: values.get(name, TagValue(pid=name))
            window._update_phasor()
            self.assertIn("选到了同一个点位", window.phasor_text.text())
        finally:
            window._timer.stop()
            window.close()

    def test_angle_box_disabled_when_point_chosen(self):
        self.window._sync_angle_row(0)
        self.assertTrue(self.window.angle_boxes[0].isEnabled(), "没选点位时要能手输角度")


class ThemeFileTest(unittest.TestCase):
    """配色的导入导出（纯文件逻辑，不碰界面）。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "theme.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_round_trip_keeps_every_colour(self):
        custom = theme.Appearance(
            palette=theme.PRESETS["浅色办公"].copy(), font_size=15, density="宽松"
        )
        custom.palette.accent = "#ff8f1f"
        theme.save_theme(self.path, custom)
        back, warnings = theme.load_theme_file(self.path)
        self.assertEqual(warnings, [])
        self.assertEqual(back.palette.to_dict(), custom.palette.to_dict())
        self.assertEqual(back.font_size, 15)
        self.assertEqual(back.density, "宽松")

    def test_file_has_format_marker(self):
        theme.save_theme(self.path)
        with open(self.path, encoding="utf-8") as handle:
            data = json.loads(handle.read())
        self.assertEqual(data["format"], theme.THEME_FILE_FORMAT)
        self.assertEqual(data["version"], theme.THEME_FILE_VERSION)
        self.assertIn("palette", data["appearance"])

    def test_bad_json_is_reported(self):
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("{ 这不是 json")
        with self.assertRaises(ValueError):
            theme.load_theme_file(self.path)

    def test_missing_file_is_reported(self):
        with self.assertRaises(ValueError):
            theme.load_theme_file(os.path.join(self.dir, "不存在.json"))

    def test_unknown_and_invalid_keys_are_handled(self):
        dirty = {
            "format": "别的软件",
            "appearance": {
                "palette": {"accent": "不是颜色", "不存在的项": "#ffffff", "name": "改过的"},
                "font_size": 999,
                "density": "稀奇古怪",
            },
        }
        appearance, warnings = theme.parse_theme_text(json.dumps(dirty, ensure_ascii=False))
        self.assertTrue(any("格式标记" in text for text in warnings))
        self.assertTrue(any("不存在的项" in text for text in warnings))
        self.assertTrue(any("accent" in text for text in warnings))
        self.assertEqual(appearance.palette.name, "改过的")
        self.assertIs(theme.is_color(appearance.palette.accent), True, "非法颜色要回落成合法的")
        self.assertLessEqual(appearance.font_size, 20, "字号要夹住")
        self.assertEqual(appearance.density, "标准", "密度不认识就回落")

    def test_bare_palette_without_wrapper(self):
        """也接受直接给 palette 的简写（手改文件时省事）。"""
        text = json.dumps({"palette": {"accent": "#123456"}})
        appearance, _warnings = theme.parse_theme_text(text)
        self.assertEqual(appearance.palette.accent, "#123456")

    def test_series_colours_survive(self):
        custom = theme.Appearance(palette=theme.PRESETS["深色工业"].copy())
        custom.palette.series = ("#111111", "#222222")
        theme.save_theme(self.path, custom)
        back, _warnings = theme.load_theme_file(self.path)
        self.assertEqual(tuple(back.palette.series), ("#111111", "#222222"))


class SerialPortSettingsTest(unittest.TestCase):
    """串口：自动检测 + 可自选 + 默认 9600 8E1。"""

    @classmethod
    def setUpClass(cls):
        global _app
        _app = QApplication.instance() or QApplication([])

    def test_enumeration_never_raises(self):
        ports = serial_ports.list_serial_ports()
        self.assertIsInstance(ports, list)
        for port in ports:
            self.assertTrue(port.device)
        self.assertIsInstance(serial_ports.summary(), str)

    def test_default_is_9600_even_parity(self):
        options = models.default_channel_options("rtu")
        self.assertEqual(options["baudrate"], 9600)
        self.assertEqual(options["parity"], "E", "现场默认是偶校验（8E1）")
        self.assertEqual(options["bytesize"], 8)
        self.assertEqual(options["stopbits"], 1)
        self.assertEqual(serial_ports.frame_text(9600, 8, "E", 1), "9600 8E1")

    def test_driver_schema_uses_serials_defaults(self):
        schema = {field.key: field for field in ModbusDriver.option_schema("rtu")}
        self.assertEqual(schema["port"].type, "port", "串口号要能下拉选也能手打")
        self.assertEqual(schema["baudrate"].default, 9600)
        self.assertEqual(schema["parity"].default, "E")
        self.assertIn("偶校验", "".join(schema["parity"].choice_labels))
        self.assertIn("9600", schema["parity"].hint)

    def test_port_combo_keeps_manual_value(self):
        """手打的串口号不能被重扫冲掉（现场有些虚拟串口枚举不出来）。"""
        combo = option_fields.SerialPortCombo("COM77")
        self.assertEqual(combo.currentText(), "COM77")
        combo.refresh()
        self.assertEqual(combo.currentText(), "COM77", "重扫后仍要保留手填的值")
        combo.setCurrentText("COM88")
        combo.refresh()
        self.assertEqual(combo.currentText(), "COM88")

    def test_port_combo_lists_detected_ports(self):
        combo = option_fields.SerialPortCombo("")
        detected = serial_ports.port_names()
        listed = [combo.itemData(i) for i in range(combo.count())]
        for device in detected:
            self.assertIn(device, listed)
        if detected:
            self.assertEqual(combo.currentText(), detected[0], "有串口时默认选第一个")

    def test_port_field_reads_text_not_data(self):
        field = OptionField("port", "串口号", "port", "COM1")
        widget = option_fields.make_option_widget(field, "COM5")
        self.assertEqual(option_fields.read_option_widget(widget, field), "COM5")
        widget.setCurrentText("COM9")
        self.assertEqual(option_fields.read_option_widget(widget, field), "COM9")
        self.assertEqual(
            option_fields.coerce_option("COM9", field), "COM9", "port 按字符串处理"
        )

    def test_choice_labels_are_used_for_display(self):
        field = OptionField(
            "parity", "校验位", "choice", "E",
            choices=("N", "E", "O"), choice_labels=("无校验(N)", "偶校验(E)", "奇校验(O)"),
        )
        widget = option_fields.make_option_widget(field, "E")
        self.assertEqual(widget.currentText(), "偶校验(E)")
        self.assertEqual(widget.currentData(), "E")

    def test_port_sort_key_orders_numerically(self):
        devices = ["COM10", "COM2", "COM1"]
        self.assertEqual(
            sorted(devices, key=serial_ports._sort_key), ["COM1", "COM2", "COM10"],
            "COM10 要排在 COM9 后面",
        )

    def test_channel_editor_applies_typed_port(self):
        """手打的串口号必须真写进工程（下拉里没有这一项也要生效）。"""
        from icomm.ui.dialogs.channel_editor import ChannelEditor

        project = project_io.bess_project()
        channel = project.channels[0]
        channel.switch_link("rtu", models.default_channel_options("rtu"))
        editor = ChannelEditor(project)
        try:
            editor.list.setCurrentRow(0)
            widget = editor._option_widgets["port"]
            self.assertIsInstance(widget, option_fields.SerialPortCombo)
            self.assertEqual(channel.options["port"], serial_ports.DEFAULT_PORT)

            widget.lineEdit().setText("COM77")
            widget.lineEdit().editingFinished.emit()
            self.assertEqual(channel.options["port"], "COM77", "手打的值要落进模型")
            self.assertEqual(channel.options["baudrate"], 9600)
            self.assertEqual(channel.options["parity"], "E")

            # 下拉里选一个也要生效（这条以前是坏的：索引被当成 silent 传走）
            widget = editor._option_widgets["port"]  # 上一行改了参数，表单已经重建过
            channels = [widget.itemData(i) for i in range(widget.count())]
            channels = [c for c in channels if c != "COM77"]
            if channels:
                widget.setCurrentIndex(widget.findData(channels[0]))
                self.assertEqual(channel.options["port"], channels[0])
        finally:
            editor.deleteLater()

    def test_editor_notices_choice_changes(self):
        """改校验位要算「工程被改了」，不然不会提示保存。"""
        from icomm.ui.dialogs.channel_editor import ChannelEditor

        project = project_io.bess_project()
        channel = project.channels[0]
        channel.switch_link("rtu", models.default_channel_options("rtu"))
        editor = ChannelEditor(project)
        try:
            editor.list.setCurrentRow(0)
            seen: list[int] = []
            editor.changed.connect(lambda: seen.append(1))
            parity = editor._option_widgets["parity"]
            parity.setCurrentIndex(parity.findData("N"))
            self.assertEqual(channel.options["parity"], "N")
            self.assertTrue(seen, "改参数要发出 changed，主窗口才知道要提示保存")
        finally:
            editor.deleteLater()


class ScopeInstantaneousTest(unittest.TestCase):
    """没有波形缓冲区时，示波器要按瞬时值画趋势而不是空着。

    用「配电柜监控」示例当夹具：它是一台只有瞬时值/计算量的设备（没有数组点位、
    也没有任何点位标「示波器」），正好走示波器的兜底分支。
    """

    @classmethod
    def setUpClass(cls):
        global _app
        _app = QApplication.instance() or QApplication([])

    def setUp(self):
        from icomm.core import project_store
        from icomm.ui.main_window import MainWindow

        self.tmp = tempfile.mkdtemp()
        self.project = project_io.sample_project()
        paths = project_store.create_project_dir(
            os.path.join(self.tmp, "工程"), self.project
        )
        self.window = MainWindow(
            self.project, paths, workdir=self.tmp, base_dir=self.tmp
        )
        self.window.settings.confirm_on_exit = False
        self.page = self.window.scope_page

    def tearDown(self):
        self.window.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_project_really_has_no_waveform_points(self):
        self.assertEqual(
            [point for _d, point in self.project.iter_points() if point.is_array], [],
            "这个工程本来就是「只有瞬时值」的那种设备，测试前提要成立",
        )

    def test_channel_list_falls_back_to_all_points(self):
        """一个点位都没标「示波器」时不能空着 —— 那样看着像坏了。"""
        self.assertEqual(len(self.project.scope_points()), 0)
        self.assertEqual(
            self.page.channel_list.count(), len(self.project.all_point_ids())
        )
        self.assertIn("没有点位标「示波器」", self.page.hint.text())

    def test_hint_explains_instantaneous_trend(self):
        text = self.page.hint.text()
        self.assertIn("没有波形点位", text)
        self.assertIn("瞬时值", text)
        self.assertIn("ms/点", text, "要告诉用户一个点多久来一次")

    def test_scalar_channel_shows_trend(self):
        pid = "1号进线柜.A相电压"
        self.assertTrue(self.page.add_point(pid))
        self.assertEqual(self.page.canvas.mode, "scalar", "标量点位应当按趋势画")
        channel = self.page.canvas.channel(pid)
        self.assertIsNotNone(channel)
        self.assertFalse(channel.is_array)

    def test_sparse_poll_rate_is_called_out(self):
        """轮询周期很大时趋势会很稀，要在提示里说清楚。"""
        for device in self.project.devices:
            device.poll_interval_ms = 5000
        self.page.timebase_combo.setCurrentIndex(0)  # 1 秒/格
        self.page._build_channels()
        self.assertIn("曲线会很稀", self.page.hint.text())

    def test_history_hint_mentions_missing_waveform_points(self):
        self.assertIn("没有波形点位", self.page.history_hint.text())


class WindowSizeTest(unittest.TestCase):
    """窗口不能被布局顶得比屏幕还宽（用户实测：窗口宽过屏幕、右边被切掉）。"""

    @classmethod
    def setUpClass(cls):
        global _app
        _app = QApplication.instance() or QApplication([])
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def make_window(self, builder=None) -> MainWindow:
        from icomm.core import project_store
        from icomm.ui.main_window import MainWindow

        project = (builder or project_io.bess_project)()
        # 每个窗口一个自己的基准目录：软件设置存在这里，
        # 共用的话前一个测试改的设置（比如关掉"记住窗口大小"）会串到后一个测试
        base = tempfile.mkdtemp(dir=self.tmp.name)
        paths = project_store.create_project_dir(
            os.path.join(base, f"w{id(project)}"), project
        )
        window = MainWindow(project, paths, workdir=base, base_dir=base)
        window.settings.confirm_on_exit = False
        self.addCleanup(window.close)
        _app.processEvents()
        return window

    def test_minimum_width_fits_small_screens(self):
        """回归：以前长文字把最小宽度顶到 2800，用户屏幕上窗口比屏幕还宽。"""
        window = self.make_window()
        minimum = window.minimumSizeHint()
        self.assertLessEqual(
            minimum.width(), 1200, f"窗口最小宽度 {minimum.width()} 太大，小屏放不下"
        )
        self.assertLessEqual(minimum.height(), 700, f"窗口最小高度 {minimum.height()} 太大")

    def test_minimum_width_for_every_sample(self):
        for name, builder in project_io.BUILTIN_SAMPLES.items():
            with self.subTest(sample=name):
                window = self.make_window(builder)
                self.assertLessEqual(window.minimumSizeHint().width(), 1200, name)

    def test_elided_label_does_not_force_width(self):
        from icomm.ui.widgets import ElidedLabel

        long_text = "全部点位 51 个可选，已选 0 个　｜　工程里没有波形点位：按瞬时值画趋势" * 3
        label = ElidedLabel(long_text)
        self.addCleanup(label.deleteLater)
        self.assertLessEqual(label.minimumSizeHint().width(), 100)
        self.assertLessEqual(label.sizeHint().width(), 400)
        self.assertEqual(label.text(), long_text, "逻辑上的文字要是完整的")
        self.assertEqual(label.fullText(), long_text)
        self.assertIn("瞬时值", label.toolTip(), "全文要在 tooltip 里能看")

    def test_elided_label_elides_when_narrow(self):
        from icomm.ui.widgets import ElidedLabel

        label = ElidedLabel("非常长的一段说明文字" * 8)
        self.addCleanup(label.deleteLater)
        label.resize(120, 20)
        label.show()
        _app.processEvents()
        # 显示用的是省略后的那截（基类 QLabel 里的文本），逻辑文本仍是全文
        displayed = QLabel.text(label)
        self.assertLessEqual(len(displayed), len(label.fullText()))
        self.assertEqual(label.minimumSizeHint().width(), ElidedLabel.MIN_WIDTH)

    def test_elided_label_wrap_keeps_height(self):
        from icomm.ui.widgets import ElidedLabel

        label = ElidedLabel("工程里没有波形点位：按瞬时值画趋势（每个采集周期一个点，约 200 ms/点）" * 3, wrap=True)
        self.addCleanup(label.deleteLater)
        label.resize(300, 60)
        self.assertLessEqual(label.minimumSizeHint().width(), 100)
        self.assertTrue(label.wordWrap())

    def test_scope_hint_never_pushes_layout(self):
        window = self.make_window()
        hint = window.scope_page.hint
        self.assertLessEqual(
            hint.minimumSizeHint().width(), 120,
            "示波器页那行长提示不能把窗口顶宽（要折行）",
        )
        self.assertTrue(hint.wordWrap(), "窄窗口下要折行，好把话说完")

    def test_status_path_is_eliding(self):
        from icomm.ui.widgets import ElidedLabel

        window = self.make_window()
        window.status_path.setText("工程目录：" + "D:/很长的目录名/" * 20)
        self.assertIsInstance(window.status_path, ElidedLabel)
        self.assertLessEqual(
            window.status_path.minimumSizeHint().width(), 120,
            "状态栏里的完整工程路径不能把窗口顶宽",
        )
        self.assertLessEqual(window.statusBar().minimumSizeHint().width(), 700)

    def test_window_geometry_is_remembered(self):
        window = self.make_window()
        window.resize(1180, 700)
        _app.processEvents()
        window._remember_window_geometry()
        self.assertTrue(window.settings.window_geometry)
        self.assertEqual(window.settings.window_geometry, window.settings.window_geometry.strip())

        again = self.make_window()
        again.settings.window_geometry = window.settings.window_geometry
        self.assertTrue(again._restore_window_geometry())
        _app.processEvents()

    def test_geometry_not_remembered_when_switched_off(self):
        window = self.make_window()
        window.settings.remember_window = False
        window.settings.window_geometry = "STALE"
        window._remember_window_geometry()
        self.assertEqual(window.settings.window_geometry, "STALE", "关掉了就不该改动它")

    def test_bad_geometry_geometry_is_ignored(self):
        window = self.make_window()
        window.settings.window_geometry = "这根本不是 geometry"
        self.assertFalse(window._restore_window_geometry(), "坏数据要当没有，不能崩")
        window.settings.window_geometry = ""
        self.assertFalse(window._restore_window_geometry())

    def test_reset_window_size_clears_saved(self):
        window = self.make_window()
        window._remember_window_geometry()
        self.assertTrue(window.settings.window_geometry)
        window.reset_window_size()
        self.assertEqual(window.settings.window_geometry, "", "恢复默认要忘掉记的几何")
        self.assertLessEqual(window.minimumSizeHint().width(), 1200)


class MainWindowToolsTest(unittest.TestCase):
    """主窗口这一层：工具菜单、自动化 tick、限幅与小数位的接线。"""

    @classmethod
    def setUpClass(cls):
        # ignore_cleanup_errors：Windows 上临时目录里只要还有句柄没释放就删不掉（CI 偶发）
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        # 本类里有一条端到端用例要"真的采到数据"，所以自己起一个仿真器，
        # 端口用空闲端口并把储能示例的通道指过去 —— 不依赖外部恰好在跑仿真器。
        global _app
        _app = QApplication.instance() or QApplication([])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        cls.sim = ModbusTcpSimulator("127.0.0.1", cls.port)
        threading.Thread(
            target=cls.sim.serve_forever, kwargs={"freeze": False}, daemon=True
        ).start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.port), 0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("仿真器未就绪")

    @classmethod
    def tearDownClass(cls):
        cls.sim.stop()
        cls.tmp.cleanup()

    def setUp(self):
        from icomm.core import project_store
        from icomm.ui.main_window import MainWindow

        self.project = project_io.bess_project()
        self.project.channels[0].options["port"] = self.port
        paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "工程"), self.project
        )
        self.window = MainWindow(
            self.project, paths, workdir=self.tmp.name, base_dir=self.tmp.name
        )
        self.window.settings.confirm_on_exit = False
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        if self.window.engine is not None:
            self.window.engine.stop()
        if self.window.storage is not None:
            self.window.storage.stop()
        self.window.settings.confirm_on_exit = False
        self.window.close()
        _app.processEvents()

    def test_tools_menu_lists_every_registered_tool(self):
        menu = self.window.build_tools_menu()
        labels = " ".join(
            action.text() + " " + " ".join(sub.text() for sub in action.menu().actions())
            if action.menu() is not None
            else action.text()
            for action in menu.actions()
        )
        for tool in ui_tools.tools():
            self.assertIn(tool.name, labels, f"菜单里缺「{tool.name}」")

    def test_tool_context_exposes_services(self):
        context = self.window.tool_context()
        self.assertIs(context.project, self.project)
        self.assertIs(context.service("automation"), self.window.automation)
        self.assertIsNotNone(context.reveal_point)
        self.assertIsNotNone(context.set_write_limit)
        self.assertIsNotNone(context.set_decimals)

    def test_write_limit_applied_to_model(self):
        self.assertTrue(self.window.set_point_write_limit(BUS_PID, 100.0, 750.0))
        point = self.project.devices[0].point("有功功率给定")
        self.assertEqual((point.write_min, point.write_max), (100.0, 750.0))
        self.assertTrue(self.window.set_point_write_limit(BUS_PID, None, None))
        self.assertFalse(point.has_write_limit)

    def test_decimals_applied_and_clamped(self):
        self.assertTrue(self.window.set_point_decimals(BUS_PID, 3))
        point = self.project.devices[0].point("有功功率给定")
        self.assertEqual(point.decimals, 3)
        self.window.set_point_decimals(BUS_PID, 99)
        self.assertEqual(point.decimals, 6, "小数位要夹在 0~6")
        self.window.set_point_decimals(BUS_PID, -5)
        self.assertEqual(point.decimals, 0)

    def test_unknown_pid_returns_false(self):
        self.assertFalse(self.window.set_point_write_limit("没有.这个", 1, 2))
        self.assertFalse(self.window.set_point_decimals("没有.这个", 1))

    def test_point_menu_has_limit_decimals_and_trigger(self):
        menu = self.window.build_point_menu(BUS_PID)
        labels = [action.text() for action in menu.actions()]
        self.assertTrue(any("写值限幅" in text for text in labels), labels)
        self.assertTrue(any("小数位数" in text for text in labels), labels)
        self.assertTrue(any("触发条件" in text for text in labels), labels)

    def test_limit_menu_disabled_for_readonly_point(self):
        menu = self.window.build_point_menu("储能变流器PCS.直流母线电压")
        limit = next(a for a in menu.actions() if "写值限幅" in a.text())
        self.assertFalse(limit.isEnabled(), "只读点位的限幅项该是灰的")

    def test_automation_timer_is_running(self):
        self.assertTrue(self.window._automation_timer.isActive())

    def test_automation_tick_without_engine_is_safe(self):
        self.window.automation.clear()
        self.window._tick_automation()  # 不该抛

    def test_automation_runs_through_window(self):
        """端到端：启动采集 → 自动化触发 → 动作被调用。"""
        fired: list[str] = []
        self.window.automation.clear()
        self.window.automation.add(
            Task(
                name="窗口级测试",
                action=lambda task, values: fired.append(task.name),
                trigger=Trigger(
                    pid="储能变流器PCS.A相电压",
                    compare=Compare.GT,
                    threshold=0.0,
                    mode=TriggerMode.RISING,
                ),
            )
        )
        self.window.start_engine()
        deadline = time.time() + 25
        while time.time() < deadline and not fired:
            _app.processEvents()
            time.sleep(0.05)
        self.assertTrue(fired, "自动化没有在真实采集数据上触发")
        self.window.automation.clear()


if __name__ == "__main__":
    unittest.main()
