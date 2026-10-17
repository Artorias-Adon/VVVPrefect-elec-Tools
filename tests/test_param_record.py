"""下发参数记录：把一台设备调好的参数存下来，导到同型号的下一台。

盯住四件事：
* 记的对不对（只记可写的"设定项"，命令/只读不记；写失败不记）；
* 存/读三种格式（JSON 默认、CSV 能在 Excel 里改、TXT 只给人看）；
* 导入按**点号**对到另一台设备，缺的/只读的要说清楚；
* 界面那条路（下拉、表格、保存对话框、导入前检查采集是否在跑）。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import params  # noqa: E402
from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core.models import DeviceDef, PointDef, Project  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.core.tags import TagValue  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.main_window import MainWindow  # noqa: E402
from icomm.ui.tools.param_record import ParamRecordWindow  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def tuning_project() -> Project:
    """两台同型号设备（点号一样、站号不同）+ 一台不同型号的。"""
    project = Project(name="调参")
    project.channels.append(project_io.bess_project().channels[0])
    channel = project.channels[0].name

    def unit(name: str, unit_id: int) -> DeviceDef:
        return DeviceDef(
            name=name,
            channel=channel,
            unit_id=unit_id,
            points=[
                PointDef(name="kp", label="比例增益", address=0, writable=True,
                         data_type="uint16", unit=""),
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
    project.devices.append(
        DeviceDef(
            name="别的型号",
            channel=channel,
            unit_id=21,
            points=[PointDef(name="other", label="别的点位", address=0, writable=True)],
        )
    )
    return project


class TempCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)


class RecorderTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = tuning_project()
        self.recorder = params.ParamRecorder()

    def point(self, device: str, name: str):
        return self.project.device(device).point(name)

    def test_records_writable_settings(self) -> None:
        entry = self.recorder.record("1号整流器", self.point("1号整流器", "kp"), 40)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.point, "kp", "记录里只存点号，搬到别的设备才方便")
        self.assertEqual(entry.value, 40)
        self.assertEqual(entry.address, "holding#0")
        self.assertTrue(entry.time)

    def test_command_points_are_not_parameters(self) -> None:
        self.assertIsNone(
            self.recorder.record("1号整流器", self.point("1号整流器", "cmd"), 1),
            "命令是「动作」，记成参数会误导下一台设备",
        )

    def test_readonly_points_are_not_parameters(self) -> None:
        self.assertIsNone(self.recorder.record("1号整流器", self.point("1号整流器", "u"), 500))

    def test_same_point_recorded_twice_keeps_latest(self) -> None:
        self.recorder.record("1号整流器", self.point("1号整流器", "kp"), 40)
        self.recorder.record("1号整流器", self.point("1号整流器", "kp"), 55)
        entries = self.recorder.entries("1号整流器")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].value, 55)

    def test_values_are_cleaned(self) -> None:
        entry = self.recorder.record("1号整流器", self.point("1号整流器", "ki"), 0.5)
        self.assertEqual(entry.value, 0.5)
        entry = self.recorder.record("1号整流器", self.point("1号整流器", "kp"), 40.0)
        self.assertEqual(entry.value, 40, "整数不该写成 40.0")
        self.assertIsInstance(
            self.recorder.record("1号整流器", self.point("1号整流器", "enable"), 1).value, int
        )

    def test_snapshot_reads_current_values(self) -> None:
        values = {"1号整流器.kp": 40, "1号整流器.ki": 0.25, "1号整流器.enable": True,
                  "1号整流器.u": 530.0, "1号整流器.cmd": 1}

        def value_of(pid: str) -> TagValue:
            return TagValue(pid=pid, value=values.get(pid), quality="good")

        count = self.recorder.snapshot(self.project, "1号整流器", value_of)
        self.assertEqual(count, 3, "只读点位与命令点位不该进记录")
        recorded = {entry.point: entry.value for entry in self.recorder.entries("1号整流器")}
        self.assertEqual(recorded, {"kp": 40, "ki": 0.25, "enable": True})

    def test_snapshot_skips_points_without_value(self) -> None:
        count = self.recorder.snapshot(
            self.project, "1号整流器", lambda pid: TagValue(pid=pid, value=None)
        )
        self.assertEqual(count, 0)

    def test_record_for_follows_point_order(self) -> None:
        for name in ("enable", "ki", "kp"):
            self.recorder.record("1号整流器", self.point("1号整流器", name), 1)
        record = self.recorder.record_for("1号整流器", self.project)
        self.assertEqual([entry.point for entry in record.entries], ["kp", "ki", "enable"])
        self.assertEqual(record.unit_id, 11)

    def test_clear(self) -> None:
        self.recorder.record("1号整流器", self.point("1号整流器", "kp"), 40)
        self.recorder.record("2号整流器", self.point("2号整流器", "kp"), 41)
        self.recorder.clear("1号整流器")
        self.assertEqual(self.recorder.devices(), ["2号整流器"])
        self.recorder.clear()
        self.assertEqual(len(self.recorder), 0)


class FormatTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.record = params.ParamRecord(
            device="1号整流器",
            unit_id=11,
            saved_at="2026-10-18 04:30:00",
            entries=[
                params.ParamEntry(point="kp", label="比例增益", value=40,
                                  unit="", address="holding#0", time="2026-10-18 04:29:00"),
                params.ParamEntry(point="ki", label="积分,时间", value=0.25,
                                  unit="s", address="holding#1", time=""),
                params.ParamEntry(point="enable", label="使能", value=True,
                                  unit="", address="holding#2", time=""),
            ],
        )

    def test_default_format_is_json(self) -> None:
        self.assertEqual(params.DEFAULT_FORMAT, params.FORMAT_JSON)
        self.assertEqual(params.format_for_path("a.json"), params.FORMAT_JSON)
        self.assertEqual(params.format_for_path("a.csv"), params.FORMAT_CSV)
        self.assertEqual(params.format_for_path("a.txt"), params.FORMAT_TXT)
        self.assertEqual(params.format_for_path("a.unknown"), params.FORMAT_JSON)

    def test_format_from_filter(self) -> None:
        self.assertEqual(params.format_from_filter("CSV（Excel 可编辑） (*.csv)"), params.FORMAT_CSV)
        self.assertEqual(params.format_from_filter("乱写的"), params.FORMAT_JSON)

    def test_json_round_trip(self) -> None:
        text = params.to_json_text(self.record)
        self.assertEqual(json.loads(text)["format"], params.FILE_FORMAT)
        back = params.from_json_text(text)
        self.assertEqual(back.device, "1号整流器")
        self.assertEqual(back.unit_id, 11)
        self.assertEqual(back.saved_at, self.record.saved_at)
        self.assertEqual([e.point for e in back.entries], ["kp", "ki", "enable"])
        self.assertEqual(back.entries[0].value, 40)
        self.assertEqual(back.entries[1].label, "积分,时间")
        self.assertIs(back.entries[2].value, True)

    def test_json_accepts_plain_list(self) -> None:
        back = params.from_json_text(json.dumps([{"point": "kp", "value": 1}]))
        self.assertEqual(len(back), 1)

    def test_json_errors(self) -> None:
        with self.assertRaisesRegex(ValueError, "JSON"):
            params.from_json_text("{不是 json")
        with self.assertRaisesRegex(ValueError, "entries"):
            params.from_json_text('{"device": "x"}')
        with self.assertRaisesRegex(ValueError, "点号"):
            params.from_json_text('{"entries": [{"value": 1}]}')

    def test_csv_round_trip_with_commas(self) -> None:
        text = params.to_csv_text(self.record)
        self.assertTrue(text.startswith("\ufeff"), "要带 BOM，Excel 打开才不乱码")
        self.assertIn('"积分,时间"', text, "含逗号的字段要加引号")
        back = params.from_csv_text(text)
        self.assertEqual(back.device, "1号整流器")
        self.assertEqual(back.unit_id, 11)
        self.assertEqual([e.point for e in back.entries], ["kp", "ki", "enable"])
        self.assertEqual(back.entries[1].label, "积分,时间")
        self.assertEqual(back.entries[0].value, 40)

    def test_csv_rejects_foreign_files(self) -> None:
        with self.assertRaisesRegex(ValueError, "点号"):
            params.from_csv_text("设备,值\n甲,1\n")

    def test_txt_is_read_only(self) -> None:
        text = params.to_txt_text(self.record)
        self.assertIn("1号整流器", text)
        self.assertIn("比例增益", text)
        self.assertIn("TXT 只用于查看", text)
        path = os.path.join(self.tmp.name, "r.txt")
        params.save(path, self.record)
        with self.assertRaisesRegex(ValueError, "JSON 或 CSV"):
            params.load(path)

    def test_save_and_load_by_extension(self) -> None:
        for fmt in (params.FORMAT_JSON, params.FORMAT_CSV):
            with self.subTest(fmt=fmt):
                path = os.path.join(self.tmp.name, f"record.{fmt}")
                saved = params.save(path, self.record)
                self.assertEqual(saved, path)
                self.assertEqual(len(params.load(path)), 3)

    def test_save_creates_directory(self) -> None:
        target = os.path.join(self.tmp.name, "深一层", "r.json")
        params.save(target, self.record)
        self.assertTrue(os.path.isfile(target))

    def test_file_filter_lists_json_first(self) -> None:
        first = params.file_filter().split(";;")[0]
        self.assertIn("*.json", first)


class ImportTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = tuning_project()

    def record(self) -> params.ParamRecord:
        return params.ParamRecord(
            device="1号整流器",
            unit_id=11,
            entries=[
                params.ParamEntry(point="kp", value=40),
                params.ParamEntry(point="ki", value=0.25),
                params.ParamEntry(point="cmd", value=1),      # 目标是命令点位
                params.ParamEntry(point="不存在", value=9),   # 目标没有这个点
            ],
        )

    def test_plan_maps_by_point_name(self) -> None:
        plan = params.plan_import(self.record(), self.project, "2号整流器")
        self.assertEqual(plan.device, "2号整流器")
        self.assertEqual([item[0] for item in plan.writes], ["kp", "ki"])
        self.assertEqual(plan.missing, ["不存在"])
        self.assertEqual(plan.readonly, ["cmd"])
        self.assertIn("可写入 2 项", plan.summary())

    def test_plan_rejects_unknown_device(self) -> None:
        with self.assertRaises(ValueError):
            params.plan_import(self.record(), self.project, "没有这台")

    def test_apply_writes_each_point(self) -> None:
        plan = params.plan_import(self.record(), self.project, "2号整流器")
        calls: list[tuple[str, object]] = []
        errors = params.apply_plan(plan, lambda pid, value: calls.append((pid, value)) or True)
        self.assertEqual(errors, [])
        self.assertEqual(
            calls, [("2号整流器.kp", 40), ("2号整流器.ki", 0.25)], "要写到目标设备上"
        )

    def test_apply_collects_errors(self) -> None:
        plan = params.plan_import(self.record(), self.project, "2号整流器")

        def write(pid: str, value):
            if pid.endswith(".ki"):
                return False
            return True

        errors = params.apply_plan(plan, write)
        self.assertEqual(len(errors), 1)
        self.assertIn("2号整流器.ki", errors[0])

    def test_apply_keeps_going_after_exception(self) -> None:
        plan = params.plan_import(self.record(), self.project, "2号整流器")
        seen: list[str] = []

        def write(pid: str, value):
            seen.append(pid)
            if pid.endswith(".kp"):
                raise RuntimeError("超时")
            return True

        errors = params.apply_plan(plan, write)
        self.assertEqual(len(seen), 2, "一条失败不该挡住后面那条")
        self.assertIn("超时", errors[0])


class ParamRecordWindowTest(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = tuning_project()
        self.written: list[tuple[str, object]] = []
        self.running = True
        self.values = {"1号整流器.kp": 40, "1号整流器.ki": 0.25, "1号整流器.enable": True}
        self.announced: list[tuple[str, str, str]] = []
        self.recorder = params.ParamRecorder()
        self.context = ui_tools.ToolContext(
            project=self.project,
            window=None,
            settings=AppSettings(),
            report_dir=self.tmp.name,
            tag_of=lambda pid: TagValue(
                pid=pid, value=self.values.get(pid), quality="good"
            ),
            write=lambda pid, value: self.written.append((pid, value)),
            engine_running=lambda: self.running,
            notify=lambda level, title, text="": self.announced.append((level, title, text)),
            param_recorder=self.recorder,
        )
        self.window = ParamRecordWindow(self.context)
        self.addCleanup(self.window.deleteLater)

    def test_tool_is_registered_under_engineering(self) -> None:
        tool = ui_tools.find_tool("参数记录")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.group, "工程")
        self.assertTrue(tool.non_modal)
        self.assertIn("参数记录", [t.name for t in ui_tools.tools_by_group()["工程"]])

    def test_device_combo_lists_only_devices_with_settings(self) -> None:
        names = [self.window.device_combo.itemText(i) for i in range(self.window.device_combo.count())]
        self.assertEqual(names, ["1号整流器", "2号整流器", "别的型号"])
        self.assertEqual(self.window.device_name(), "1号整流器")

    def test_empty_state_has_hint(self) -> None:
        self.assertEqual(self.window.table.rowCount(), 0)
        self.assertIn("还没记下参数", self.window.status.text())

    def test_table_shows_recorded_entries(self) -> None:
        device = self.project.device("1号整流器")
        self.recorder.record(device.name, device.point("kp"), 40)
        self.recorder.record(device.name, device.point("ki"), 0.25)
        self.window.refresh()
        self.assertEqual(self.window.table.rowCount(), 2)
        self.assertEqual(self.window.table.item(0, 0).text(), "kp")
        self.assertEqual(self.window.table.item(1, 2).text(), "0.25", "值列只放数字，单位单独一列")
        self.assertEqual(self.window.table.item(1, 3).text(), "s")
        self.assertIn("已记 2 项", self.window.status.text())

    def test_snapshot_button_records_current_values(self) -> None:
        self.window.snapshot_button.click()
        self.assertEqual(len(self.recorder.entries("1号整流器")), 3)
        self.assertIn("已记录", self.window.status.text())

    def test_save_as_defaults_to_json_and_honours_csv(self) -> None:
        device = self.project.device("1号整流器")
        self.recorder.record(device.name, device.point("kp"), 40)
        json_path = os.path.join(self.tmp.name, "out.json")
        with mock.patch("PySide6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(json_path, "JSON 记录（默认，信息最全） (*.json)")):
            saved = self.window.save_as()
        self.assertEqual(saved, json_path)
        self.assertTrue(os.path.isfile(json_path))
        self.assertEqual(params.load(json_path).entries[0].point, "kp")

        csv_path = os.path.join(self.tmp.name, "out.csv")
        with mock.patch("PySide6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(csv_path, "CSV（Excel 可编辑） (*.csv)")):
            saved = self.window.save_as()
        self.assertEqual(saved, csv_path)
        with open(csv_path, encoding="utf-8-sig") as handle:
            self.assertTrue(handle.read().startswith("# 参数记录"))
        self.assertTrue(self.announced, "存完要在通知中心说一声")

    def test_save_without_extension_follows_filter(self) -> None:
        device = self.project.device("1号整流器")
        self.recorder.record(device.name, device.point("kp"), 40)
        target = os.path.join(self.tmp.name, "无扩展名")
        with mock.patch("PySide6.QtWidgets.QFileDialog.getSaveFileName",
                        return_value=(target, "CSV（Excel 可编辑） (*.csv)")):
            saved = self.window.save_as()
        self.assertEqual(saved, target + ".csv")
        self.assertTrue(os.path.isfile(saved))

    def test_import_writes_to_selected_device(self) -> None:
        record = params.ParamRecord(device="1号整流器", entries=[
            params.ParamEntry(point="kp", value=40),
            params.ParamEntry(point="ki", value=0.25),
        ])
        path = os.path.join(self.tmp.name, "from.json")
        params.save(path, record)
        self.window.device_combo.setCurrentIndex(1)  # 2号整流器
        with mock.patch("PySide6.QtWidgets.QFileDialog.getOpenFileName",
                        return_value=(path, "JSON (*.json)")):
            errors = self.window.import_into_device()
        self.assertEqual(errors, [])
        self.assertEqual(
            self.written, [("2号整流器.kp", 40), ("2号整流器.ki", 0.25)]
        )
        self.assertIn("下发到", self.window.status.text())
        self.assertEqual(self.window.table.rowCount(), 2, "导入后自己也要记上")
        self.assertTrue(self.announced)

    def test_import_refuses_when_engine_stopped(self) -> None:
        self.running = False
        record = params.ParamRecord(entries=[params.ParamEntry(point="kp", value=40)])
        path = os.path.join(self.tmp.name, "from.json")
        params.save(path, record)
        with mock.patch("PySide6.QtWidgets.QFileDialog.getOpenFileName",
                        return_value=(path, "JSON (*.json)")):
            errors = self.window.import_into_device()
        self.assertEqual(self.written, [], "没启动采集不该下发")
        self.assertIn("启动采集", self.window.status.text())
        self.assertTrue(errors)

    def test_import_reports_mismatched_device(self) -> None:
        record = params.ParamRecord(entries=[params.ParamEntry(point="不存在", value=1)])
        path = os.path.join(self.tmp.name, "bad.json")
        params.save(path, record)
        with mock.patch("PySide6.QtWidgets.QFileDialog.getOpenFileName",
                        return_value=(path, "JSON (*.json)")):
            errors = self.window.import_into_device()
        self.assertEqual(errors, [])
        self.assertEqual(self.written, [])
        self.assertIn("没有能对到", self.window.status.text())

    def test_txt_import_is_refused_with_reason(self) -> None:
        path = os.path.join(self.tmp.name, "r.txt")
        params.save(path, params.ParamRecord(entries=[params.ParamEntry(point="kp", value=1)]))
        with mock.patch("PySide6.QtWidgets.QFileDialog.getOpenFileName",
                        return_value=(path, "TXT (*.txt)")):
            self.window.import_into_device()
        self.assertIn("JSON 或 CSV", self.window.status.text())

    def test_clear_only_touches_selected_device(self) -> None:
        self.window.snapshot_button.click()
        self.recorder.record(
            "2号整流器", self.project.device("2号整流器").point("kp"), 41
        )
        self.window.clear_button.click()
        self.assertEqual(self.recorder.entries("1号整流器"), [])
        self.assertEqual(len(self.recorder.entries("2号整流器")), 1)


class MainWindowRecordingTest(TempCase):
    """写值成功才记；失败不记。"""

    def setUp(self) -> None:
        super().setUp()
        self.project = tuning_project()
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

    def test_successful_write_is_recorded(self) -> None:
        self.window._last_write_values["1号整流器.kp"] = 40
        self.window._on_write_finished("1号整流器.kp", True, "")
        entries = self.window.param_recorder.entries("1号整流器")
        self.assertEqual([(e.point, e.value) for e in entries], [("kp", 40)])

    def test_failed_write_is_not_recorded(self) -> None:
        self.window._last_write_values["1号整流器.kp"] = 40
        with mock.patch("PySide6.QtWidgets.QMessageBox.warning"):
            self.window._on_write_finished("1号整流器.kp", False, "超时")
        self.assertEqual(self.window.param_recorder.entries("1号整流器"), [])

    def test_tool_context_exposes_recorder(self) -> None:
        context = self.window.tool_context()
        self.assertIs(context.param_recorder, self.window.param_recorder)


if __name__ == "__main__":
    unittest.main()
