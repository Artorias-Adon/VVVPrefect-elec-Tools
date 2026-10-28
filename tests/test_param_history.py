# -*- coding: utf-8 -*-
"""参数**下发历史**：记每一次下发（带时间），导成表格查得到，导入按最后一次自动下发。

盯住四件事：

* 记：每次下发都留一条（最新的在前），同一个值挨着重复发只算一条，回读（记录当前值）不算下发；
* 存：导出成表格（Excel / CSV / JSON，TXT 只给人看），时间列能直接排序筛选；
* 读：只取**每个点位的最后一次**，老「参数记录」文件也能当历史导入；
* 路：工具窗口与「文件 → 保存为…」两处入口都能走通。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog  # noqa: E402

from icomm.core import params, sheetio  # noqa: E402
from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core.models import DeviceDef, PointDef, Project  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.core.tags import TagValue  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.main_window import MainWindow  # noqa: E402
from icomm.ui.tools import param_record  # noqa: E402
from icomm.ui.tools.param_record import ParamRecordWindow  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def pair_project() -> Project:
    """两台同型号设备（点号一样、站号不同）。"""
    project = Project(name="下发历史")
    channel = project_io.bess_project().channels[0].name

    def unit(name: str, unit_id: int) -> DeviceDef:
        return DeviceDef(
            name=name,
            channel=channel,
            unit_id=unit_id,
            points=[
                PointDef(name="kp", label="比例增益", address=0, writable=True,
                         data_type="uint16"),
                PointDef(name="ki", label="积分时间", address=1, writable=True,
                         data_type="float32", decimals=3, unit="s"),
                PointDef(name="enable", label="使能", address=2, writable=True,
                         data_type="bool"),
                PointDef(name="u", label="母线电压", address=3),
                PointDef(name="cmd", label="命令", address=4, writable=True,
                         commands="1:启动,2:停机"),
            ],
        )

    project.devices.append(unit("1号整流器", 11))
    project.devices.append(unit("2号整流器", 12))
    return project


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def path(self, name: str) -> str:
        return os.path.join(self.tmp.name, name)


class HistoryRecorderTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = pair_project()
        self.recorder = params.ParamRecorder()

    def point(self, device: str, name: str):
        return self.project.device(device).point(name)

    def record(self, name: str, value, when: str, device: str = "1号整流器"):
        return self.recorder.record(
            device, self.point(device, name), value, when=when
        )

    # -------------------------------------------------- 记

    def test_every_write_is_kept_newest_first(self) -> None:
        self.record("kp", 40, "2026-10-28 20:00:00")
        self.record("kp", 55, "2026-10-28 21:00:00")
        self.record("ki", 0.25, "2026-10-28 21:05:00")
        events = self.recorder.events()
        self.assertEqual(
            [(e.point, e.value) for e in events],
            [("ki", 0.25), ("kp", 55), ("kp", 40)],
            "最新的在前，且每一次下发都留着",
        )
        self.assertEqual(events[0].time, "2026-10-28 21:05:00")
        self.assertEqual(events[0].source, params.SOURCE_MANUAL)

    def test_same_value_in_a_burst_counts_once(self) -> None:
        self.record("kp", 55, "2026-10-28 21:00:00")
        self.record("kp", 55, "2026-10-28 21:00:02")
        events = self.recorder.events()
        self.assertEqual(len(events), 1, "两秒内重复发同一个值只算一次")
        self.assertEqual(events[0].time, "2026-10-28 21:00:02", "时间要更新到最近一次")

    def test_same_value_after_the_window_is_a_new_event(self) -> None:
        self.record("kp", 55, "2026-10-28 21:00:00")
        self.record("kp", 55, "2026-10-28 21:10:00")
        self.assertEqual(len(self.recorder.events()), 2, "隔了十分钟就是两次下发")

    def test_different_values_are_separate_events(self) -> None:
        self.record("kp", 40, "2026-10-28 21:00:00")
        self.record("kp", 55, "2026-10-28 21:00:01")
        self.assertEqual(len(self.recorder.events()), 2)

    def test_burst_across_other_points_still_collapses(self) -> None:
        self.record("kp", 55, "2026-10-28 21:00:00")
        self.record("ki", 0.5, "2026-10-28 21:00:01")
        self.record("kp", 55, "2026-10-28 21:00:02")
        events = self.recorder.events()
        self.assertEqual(len(events), 2, "同一个点位连点两下只算一次，哪怕中间写了别的点位")
        self.assertEqual(events[0].point, "ki", "别的点位各占一条")
        self.assertEqual([e.time for e in events if e.point == "kp"], ["2026-10-28 21:00:02"])

    def test_snapshot_is_not_a_write(self) -> None:
        self.recorder.snapshot(
            self.project,
            "1号整流器",
            lambda pid: TagValue(pid=pid, value=10, quality="good"),
        )
        self.assertEqual(len(self.recorder.entries("1号整流器")), 3, "最新值照记")
        self.assertEqual(self.recorder.events(), [], "回读不是下发，不进历史")

    def test_import_source_is_kept(self) -> None:
        self.recorder.record(
            "1号整流器", self.point("1号整流器", "kp"), 40,
            source=params.SOURCE_IMPORT,
        )
        self.assertEqual(self.recorder.events()[0].source, params.SOURCE_IMPORT)

    def test_history_limit_drops_oldest(self) -> None:
        recorder = params.ParamRecorder(limit=3)
        for index in range(5):
            recorder.record(
                "1号整流器", self.point("1号整流器", "kp"), index,
                when=f"2026-10-28 21:00:0{index}",
            )
        values = [event.value for event in recorder.events()]
        self.assertEqual(values, [4, 3, 2], "只留最新的三条")

    def test_history_limit_keeps_merging_after_trim(self) -> None:
        recorder = params.ParamRecorder(limit=2)
        recorder.record("1号整流器", self.point("1号整流器", "kp"), 1, when="2026-10-28 21:00:00")
        recorder.record("1号整流器", self.point("1号整流器", "ki"), 2, when="2026-10-28 21:00:01")
        recorder.record("1号整流器", self.point("1号整流器", "enable"), True, when="2026-10-28 21:00:02")
        self.assertEqual(len(recorder.events()), 2)
        recorder.record("1号整流器", self.point("1号整流器", "enable"), True, when="2026-10-28 21:00:03")
        self.assertEqual(
            [e.point for e in recorder.events()], ["enable", "ki"],
            "裁掉旧记录后，同一个值仍然只占一条",
        )

    # -------------------------------------------------- 查

    def test_filters(self) -> None:
        self.record("kp", 40, "2026-10-28 20:00:00")
        self.record("ki", 0.3, "2026-10-28 21:00:00")
        self.record("kp", 60, "2026-10-28 22:00:00", device="2号整流器")
        self.assertEqual(
            [e.value for e in self.recorder.events(device_name="2号整流器")], [60]
        )
        self.assertEqual(
            [e.value for e in self.recorder.events(device_name="1号整流器", point="kp")],
            [40],
        )
        self.assertEqual(
            [e.value for e in self.recorder.events(
                time_from="2026-10-28 20:30:00", time_to="2026-10-28 21:30:00")],
            [0.3],
        )
        self.assertEqual(self.recorder.history_count(), 3)
        self.assertEqual(self.recorder.history_count("1号整流器"), 2)

    def test_clear_history_keeps_latest_values(self) -> None:
        self.record("kp", 40, "2026-10-28 20:00:00")
        self.recorder.clear_history("1号整流器")
        self.assertEqual(self.recorder.events(), [])
        self.assertEqual(len(self.recorder.entries("1号整流器")), 1, "最新值不该被清掉")

    def test_clear_device_clears_history_too(self) -> None:
        self.record("kp", 40, "2026-10-28 20:00:00")
        self.record("kp", 50, "2026-10-28 20:10:00", device="2号整流器")
        self.recorder.clear("1号整流器")
        self.assertEqual([e.device for e in self.recorder.events()], ["2号整流器"])


class HistoryFormatTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = pair_project()
        self.recorder = params.ParamRecorder()
        device = self.project.device("1号整流器")
        self.recorder.record("1号整流器", device.point("kp"), 40, when="2026-10-28 20:00:00")
        self.recorder.record("1号整流器", device.point("kp"), 55, when="2026-10-28 21:00:00")
        self.recorder.record("1号整流器", device.point("enable"), False, when="2026-10-28 21:01:00")
        self.events = self.recorder.events()

    def test_csv_has_time_and_device_columns(self) -> None:
        path = self.path("history.csv")
        params.save_history(path, self.events)
        with open(path, encoding="utf-8-sig") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("# 参数下发历史"))
        header = text.splitlines()[1].split(",")
        self.assertEqual(
            header,
            ["设备", "点号", "显示名", "值", "单位", "数据类型", "地址", "下发时间", "来源"],
            "导出的表头要能直接在 Excel 里按时间排序筛选",
        )
        self.assertIn("2026-10-28 21:00:00", text)
        self.assertIn("1号整流器", text)

    def test_csv_round_trip_keeps_values_and_types(self) -> None:
        path = self.path("history.csv")
        params.save_history(path, self.events)
        loaded = params.load_history(path)
        self.assertEqual(len(loaded), 3)
        self.assertEqual([e.value for e in loaded if e.point == "kp"], [55, 40])
        enable = [e for e in loaded if e.point == "enable"][0]
        self.assertIs(enable.value, False, "布尔值读回来还得是 False，不能变成「False」字符串")
        self.assertEqual(enable.data_type, "bool")
        self.assertEqual(enable.time, "2026-10-28 21:01:00")

    def test_json_round_trip(self) -> None:
        path = self.path("history.json")
        params.save_history(path, self.events)
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["format"], params.HISTORY_FORMAT)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(len(params.load_history(path)), 3)

    def test_xlsx_round_trip(self) -> None:
        if not sheetio.excel_ready():
            self.skipTest("没装 openpyxl，跳过 Excel 往返")
        path = self.path("history.xlsx")
        params.save_history(path, self.events)
        loaded = params.load_history(path)
        self.assertEqual(len(loaded), 3)
        self.assertEqual(loaded[0].point, "enable")

    def test_txt_is_view_only(self) -> None:
        path = self.path("history.txt")
        params.save_history(path, self.events)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("参数下发历史", text)
        self.assertIn("21:00:00", text)
        with self.assertRaises(ValueError):
            params.load_history(path)

    def test_xls_write_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            params.save_history(self.path("history.xls"), self.events)

    def test_empty_history_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            params.save_history(self.path("empty.csv"), [])

    def test_old_record_file_can_be_loaded_as_history(self) -> None:
        record = self.recorder.record_for("1号整流器", self.project)
        path = self.path("old.json")
        params.save(path, record)
        loaded = params.load_history(path)
        self.assertEqual({e.point for e in loaded}, {"kp", "enable"})
        self.assertEqual(max(e.value for e in loaded if e.point == "kp"), 55)
        self.assertTrue(all(e.device == "1号整流器" for e in loaded))

    def test_foreign_file_is_reported(self) -> None:
        path = self.path("bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"hello": 1}')
        with self.assertRaises(ValueError):
            params.load_history(path)


class HistoryPlanTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = pair_project()

    def event(self, device: str, point: str, value, when: str) -> params.ParamEvent:
        return params.ParamEvent(
            device=device, point=point, value=value, time=when, label=point
        )

    def test_takes_last_value_per_point(self) -> None:
        events = [
            self.event("1号整流器", "kp", 40, "2026-10-28 20:00:00"),
            self.event("1号整流器", "kp", 55, "2026-10-28 21:00:00"),
            self.event("1号整流器", "ki", 0.3, "2026-10-28 21:05:00"),
        ]
        plan = params.plan_history_import(events, self.project, "1号整流器")
        self.assertEqual(plan.values(), {"kp": 55, "ki": 0.3})
        self.assertEqual(plan.older, 1, "更早的那条要说明被跳过了")

    def test_prefers_events_of_the_target_device(self) -> None:
        events = [
            self.event("2号整流器", "kp", 90, "2026-10-28 22:00:00"),
            self.event("1号整流器", "kp", 40, "2026-10-28 21:00:00"),
        ]
        plan = params.plan_history_import(events, self.project, "1号整流器")
        self.assertEqual(plan.values(), {"kp": 40}, "自己设备的记录优先，哪怕别的设备更晚")

    def test_falls_back_to_point_names_for_another_device(self) -> None:
        events = [self.event("别的设备", "kp", 40, "2026-10-28 21:00:00")]
        plan = params.plan_history_import(events, self.project, "2号整流器")
        self.assertEqual(plan.values(), {"kp": 40}, "同型号设备靠点号对上")

    def test_reports_missing_and_readonly(self) -> None:
        events = [
            self.event("1号整流器", "nope", 1, "2026-10-28 21:00:00"),
            self.event("1号整流器", "u", 500, "2026-10-28 21:00:00"),
            self.event("1号整流器", "cmd", 1, "2026-10-28 21:00:00"),
        ]
        plan = params.plan_history_import(events, self.project, "1号整流器")
        self.assertEqual(plan.entries, [])
        self.assertEqual(plan.missing, ["nope"])
        self.assertEqual(sorted(plan.readonly), ["cmd", "u"])
        self.assertIn("只读", plan.summary())

    def test_apply_writes_only_the_last_value(self) -> None:
        events = [
            self.event("1号整流器", "kp", 40, "2026-10-28 20:00:00"),
            self.event("1号整流器", "kp", 55, "2026-10-28 21:00:00"),
        ]
        plan = params.plan_history_import(events, self.project, "1号整流器")
        written: list[tuple[str, object]] = []
        told: list[str] = []
        errors = params.apply_history_plan(
            plan,
            lambda pid, value: written.append((pid, value)),
            on_written=lambda pid, _event: told.append(pid),
        )
        self.assertEqual(errors, [])
        self.assertEqual(written, [("1号整流器.kp", 55)], "只下发最后一次，不要重放全部历史")
        self.assertEqual(told, ["1号整流器.kp"])

    def test_apply_collects_errors(self) -> None:
        plan = params.plan_history_import(
            [self.event("1号整流器", "kp", 55, "2026-10-28 21:00:00")],
            self.project,
            "1号整流器",
        )

        def write(_pid: str, _value) -> bool:
            raise RuntimeError("链路断了")

        errors = params.apply_history_plan(plan, write)
        self.assertEqual(len(errors), 1)
        self.assertIn("链路断了", errors[0])


class HistoryWindowTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = pair_project()
        self.written: list[tuple[str, object]] = []
        self.running = True
        self.recorder = params.ParamRecorder()
        self.context = ui_tools.ToolContext(
            project=self.project,
            window=None,
            settings=AppSettings(),
            report_dir=self.tmp.name,
            tag_of=lambda pid: TagValue(pid=pid, value=1, quality="good"),
            write=lambda pid, value: self.written.append((pid, value)),
            engine_running=lambda: self.running,
            notify=lambda level, title, text="": None,
            param_recorder=self.recorder,
        )
        self.window = ParamRecordWindow(self.context)
        self.addCleanup(self.window.deleteLater)
        device = self.project.device("1号整流器")
        self.recorder.record("1号整流器", device.point("kp"), 40, when="2026-10-28 20:00:00")
        self.recorder.record("1号整流器", device.point("kp"), 55, when="2026-10-28 21:00:00")
        self.recorder.record("1号整流器", device.point("ki"), 0.25, when="2026-10-28 21:05:00")
        self.window.refresh()
        self.window.refresh_history()

    def test_history_tab_shows_every_write(self) -> None:
        self.assertEqual(self.window.tabs.count(), 2)
        self.assertEqual(self.window.tabs.tabText(1), "下发历史")
        self.assertEqual(self.window.history_table.rowCount(), 3)
        self.assertEqual(self.window.history_table.item(0, 0).text(), "2026-10-28 21:05:00",
                         "时间放第一列，最新在上")
        self.assertEqual(self.window.history_table.item(0, 2).text(), "ki")
        self.assertIn("显示 3 条", self.window.history_status.text())

    def test_history_filter_matches_point_and_label(self) -> None:
        self.window.history_filter.setText("kp")
        self.assertEqual(self.window.history_table.rowCount(), 2)
        self.window.history_filter.setText("积分")
        self.assertEqual(self.window.history_table.rowCount(), 1, "显示名也能筛")

    def test_history_toggle_to_one_device(self) -> None:
        device = self.project.device("2号整流器")
        self.recorder.record("2号整流器", device.point("kp"), 60, when="2026-10-28 22:00:00")
        self.window.refresh_history()
        self.assertEqual(self.window.history_table.rowCount(), 4, "默认所有设备")
        self.window.history_all_check.setChecked(False)
        self.assertEqual(self.window.history_table.rowCount(), 3, "只看当前设备")

    def test_export_writes_a_queryable_table(self) -> None:
        path = self.path("out.csv")
        self.window.export_history_to(path)
        with open(path, encoding="utf-8-sig") as handle:
            text = handle.read()
        self.assertIn("2026-10-28 21:00:00", text)
        self.assertEqual(len(params.load_history(path)), 3)
        self.assertIn("已导出 3 条", self.window.history_status.text())

    def test_import_writes_only_the_last_value(self) -> None:
        source = params.ParamRecorder()
        device = self.project.device("2号整流器")
        source.record("2号整流器", device.point("kp"), 11, when="2026-10-28 09:00:00")
        source.record("2号整流器", device.point("kp"), 77, when="2026-10-28 10:00:00")
        source.record("2号整流器", device.point("ki"), 0.5, when="2026-10-28 10:05:00")
        path = self.path("in.xlsx" if sheetio.excel_ready() else "in.csv")
        params.save_history(path, source.events())

        self.window.device_combo.setCurrentIndex(1)  # 2号整流器
        result = self.window.import_history_from(path)
        self.assertEqual(result.count, 2)
        self.assertEqual(result.errors, [])
        self.assertEqual(
            sorted(self.written), [("2号整流器.ki", 0.5), ("2号整流器.kp", 77)],
            "每个点位只下发最后一次",
        )
        sources = {e.point: e.source for e in self.recorder.events(device_name="2号整流器")}
        self.assertEqual(sources, {"kp": params.SOURCE_IMPORT, "ki": params.SOURCE_IMPORT})

    def test_import_needs_running_acquisition(self) -> None:
        self.running = False
        path = self.path("in.csv")
        params.save_history(path, self.recorder.events())
        result = self.window.import_history_from(path)
        self.assertEqual(result.count, 0)
        self.assertEqual(self.written, [])
        self.assertIn("先启动采集", result.message)

    def test_import_reports_foreign_history(self) -> None:
        path = self.path("empty.csv")
        with open(path, "w", encoding="utf-8-sig") as handle:
            handle.write("甲,乙\n1,2\n")
        result = self.window.import_history_from(path)
        self.assertEqual(result.count, 0)
        self.assertIn("读不了", result.message)

    def test_clear_history_button_keeps_latest_values(self) -> None:
        self.window.clear_history_button.click()
        self.assertEqual(self.recorder.events(), [])
        self.assertEqual(self.window.history_table.rowCount(), 0)
        self.assertEqual(len(self.recorder.entries("1号整流器")), 2, "最新值要留着")
        self.assertIn("已清空", self.window.history_status.text())


class MainWindowHistoryMenuTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = pair_project()
        self.paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "proj"), self.project
        )
        self.window = MainWindow(
            self.project, self.paths, workdir=self.tmp.name, base_dir=self.tmp.name
        )
        self.window.settings.confirm_on_exit = False
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if self.window.engine is not None:
            self.window.engine.stop()
        if self.window.storage is not None:
            self.window.storage.stop()
        self.window.settings.confirm_on_exit = False
        self.window.close()

    def save_as_actions(self) -> list[str]:
        for menu_action in self.window.menuBar().actions():
            if menu_action.text().startswith("文件"):
                for child in menu_action.menu().actions():
                    if child.menu() is not None and child.text().startswith("保存为"):
                        return [item.text() for item in child.menu().actions() if item.text()]
        return []

    def test_file_menu_carries_history_actions(self) -> None:
        texts = self.save_as_actions()
        self.assertIn("保存参数下发历史…", texts)
        self.assertIn("导入参数下发历史…", texts)

    def test_save_param_history_exports_all_devices(self) -> None:
        device = self.project.device("1号整流器")
        self.window.param_recorder.record(
            "1号整流器", device.point("kp"), 55, when="2026-10-28 21:00:00"
        )
        other = self.project.device("2号整流器")
        self.window.param_recorder.record(
            "2号整流器", other.point("kp"), 66, when="2026-10-28 21:10:00"
        )
        target = self.path("all.xlsx" if sheetio.excel_ready() else "all.csv")
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(target, "CSV（Excel 可编辑） (*.csv)")):
            saved = self.window.save_param_history()
        self.assertEqual(saved, target)
        loaded = params.load_history(target)
        self.assertEqual({e.device for e in loaded}, {"1号整流器", "2号整流器"})

    def test_save_param_history_says_when_there_is_nothing(self) -> None:
        with mock.patch.object(param_record.QMessageBox, "information") as info:
            saved = self.window.save_param_history()
        self.assertEqual(saved, "")
        self.assertTrue(info.called, "没记录时要告诉用户先写几个参数")

    def test_choose_device_with_single_candidate(self) -> None:
        project = Project(name="单台")
        project.channels.append(project_io.bess_project().channels[0])
        project.devices.append(pair_project().device("1号整流器"))
        paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "single"), project
        )
        window = MainWindow(project, paths, workdir=self.tmp.name, base_dir=self.tmp.name)
        window.settings.confirm_on_exit = False
        self.addCleanup(window.close)
        self.assertEqual(window.param_history_devices(), ["1号整流器"])
        self.assertEqual(window.choose_param_device(), "1号整流器")

    def test_choose_device_asks_when_several(self) -> None:
        with mock.patch.object(QInputDialog, "getItem",
                               return_value=("2号整流器", True)) as getter:
            name = self.window.choose_param_device()
        self.assertEqual(name, "2号整流器")
        self.assertTrue(getter.called)

    def test_import_param_history_uses_the_chosen_device(self) -> None:
        with mock.patch.object(MainWindow, "choose_param_device", return_value="2号整流器"), \
                mock.patch.object(param_record, "import_history_dialog",
                                  return_value=param_record.HistoryImportResult(
                                      count=2, message="已下发 2 项")) as dialog:
            result = self.window.import_param_history()
        self.assertEqual(result.count, 2)
        self.assertEqual(dialog.call_args[0][3], "2号整流器", "要按选中的设备导入")

    def test_import_param_history_without_device_warns(self) -> None:
        with mock.patch.object(MainWindow, "choose_param_device", return_value=""), \
                mock.patch.object(self.window, "notify") as notify:
            result = self.window.import_param_history()
        self.assertEqual(result.count, 0)
        self.assertTrue(notify.called)

    def test_write_source_reaches_the_history(self) -> None:
        pid = "1号整流器.kp"
        self.window._last_write_values[pid] = 40
        self.window._last_write_source[pid] = params.SOURCE_IMPORT
        with mock.patch.object(param_record.QMessageBox, "warning"):
            self.window._on_write_finished(pid, True, "")
        events = self.window.param_recorder.events()
        self.assertEqual([(e.point, e.value, e.source) for e in events],
                         [("kp", 40, params.SOURCE_IMPORT)])
        self.assertNotIn(pid, self.window._last_write_source, "记账后要清掉，免得下次串来源")


class HistoryImportDialogTest(TempCase):
    """导出 / 导入对话框那两条路（不弹真对话框）。"""

    def setUp(self) -> None:
        super().setUp()
        self.project = pair_project()
        self.recorder = params.ParamRecorder()
        self.context = ui_tools.ToolContext(
            project=self.project,
            window=None,
            settings=AppSettings(),
            report_dir=self.tmp.name,
            write=lambda pid, value: None,
            engine_running=lambda: True,
            notify=lambda level, title, text="": None,
            param_recorder=self.recorder,
        )

    def test_export_dialog_saves_what_it_was_given(self) -> None:
        device = self.project.device("1号整流器")
        self.recorder.record("1号整流器", device.point("kp"), 55, when="2026-10-28 21:00:00")
        target = self.path("picked.csv")
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(target, "CSV（Excel 可编辑） (*.csv)")):
            saved = param_record.export_history_dialog(None, self.context, self.recorder)
        self.assertEqual(saved, target)
        self.assertEqual(len(params.load_history(target)), 1)

    def test_export_dialog_adds_extension_from_filter(self) -> None:
        device = self.project.device("1号整流器")
        self.recorder.record("1号整流器", device.point("kp"), 55, when="2026-10-28 21:00:00")
        with mock.patch.object(QFileDialog, "getSaveFileName",
                               return_value=(self.path("无扩展名"), "CSV（Excel 可编辑） (*.csv)")):
            saved = param_record.export_history_dialog(None, self.context, self.recorder)
        self.assertTrue(saved.endswith(".csv"))

    def test_import_dialog_cancel_is_quiet(self) -> None:
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=("", "")):
            result = param_record.import_history_dialog(
                None, self.context, self.recorder, "1号整流器"
            )
        self.assertEqual((result.count, result.errors, result.message), (0, [], ""))

    def test_import_file_requires_a_device(self) -> None:
        result = param_record.import_history_file(
            self.context, self.recorder, "", self.path("whatever.csv")
        )
        self.assertIn("先选一台设备", result.message)

    def test_import_reports_unmatched_history(self) -> None:
        path = self.path("other.csv")
        params.save_history(path, [params.ParamEvent(device="x", point="nope", value=1,
                                                     time="2026-10-28 21:00:00")])
        result = param_record.import_history_file(
            self.context, self.recorder, "1号整流器", path
        )
        self.assertEqual(result.count, 0)
        self.assertIn("没有能对到", result.message)


class ToolRegistrationTest(unittest.TestCase):
    def test_tool_description_mentions_history(self) -> None:
        tool = ui_tools.find_tool("参数记录")
        self.assertIsNotNone(tool)
        self.assertIn("历史", tool.description)


if __name__ == "__main__":
    unittest.main()
