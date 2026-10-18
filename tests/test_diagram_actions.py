"""主界面图元的右键功能：模块下面挂电压电流、合闸/分闸、看故障信息。

盯住的是"现场点错会出事"的地方：

* 开关操作**必须**走软件的写值通道（写哪个点、写什么值要对，点取消不能真写）；
* 图元关联的设备要能从点号和点击行为里推出来，故障信息才过滤得对；
* 图元定义（挂哪些量、开关点）要能存进工程文件，老工程读回来不能崩；
* 示例工程里挂的点位必须真的存在（写错点号现场是一片 `--`）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core.models import DiagramDef, DiagramNode, DiagramValue, Project  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.dialogs.node_actions import (  # noqa: E402
    FaultInfoDialog,
    MAX_VALUES,
    NodeSwitchDialog,
    NodeValuesDialog,
)
from icomm.ui.pages.overview import DiagramCanvas  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class FakeAlarm:
    """够 AlarmPage.on_alarm 用的事件。"""

    def __init__(self, pid: str, device: str, tag: str, message: str,
                 level: str = "warning", value=1, active: bool = True) -> None:
        self.pid = pid
        self.device = device
        self.tag = tag
        self.message = message
        self.level = level
        self.value = value
        self.active = active
        self.time_text = "2026-10-18 09:00:00"


def make_node(**kwargs) -> DiagramNode:
    defaults = dict(id="n1", symbol="bus", label="10kV 母线", x=0.2, y=0.3, w=0.2, h=0.15)
    defaults.update(kwargs)
    return DiagramNode(**defaults)


# --------------------------------------------------------------- 模型


class DiagramValueModelTest(unittest.TestCase):
    def test_roundtrip_through_dict(self) -> None:
        node = make_node(
            values=[
                DiagramValue("d.u", "Uab", "kV", 2),
                DiagramValue("d.i", "Ia", "A", 1),
            ],
            switch_point="d.ctrl",
            switch_on=1,
            switch_off=2,
            state_point="d.pos",
            state_on=1,
        )
        data = node.to_dict()
        self.assertEqual(data["switch_point"], "d.ctrl")
        self.assertEqual([item["point"] for item in data["values"]], ["d.u", "d.i"])
        clone = DiagramNode.from_dict(data)
        self.assertEqual([value.point for value in clone.values], ["d.u", "d.i"])
        self.assertEqual(clone.values[0].unit, "kV")
        self.assertEqual(clone.values[0].decimals, 2)
        self.assertEqual(clone.switch_point, "d.ctrl")
        self.assertEqual(clone.switch_off, 2)

    def test_old_project_without_new_fields_still_loads(self) -> None:
        data = {"id": "x", "symbol": "grid", "label": "电网", "value_point": "d.u"}
        node = DiagramNode.from_dict(data)
        self.assertEqual(node.values, [])
        self.assertEqual(node.switch_point, "")
        self.assertEqual(node.value_point, "d.u")

    def test_shorthand_string_value_is_accepted(self) -> None:
        node = DiagramNode.from_dict({"id": "x", "values": ["d.u", {"point": "d.i"}, {"label": "没点号"}]})
        self.assertEqual([value.point for value in node.values], ["d.u", "d.i"])

    def test_bound_points_and_devices(self) -> None:
        node = make_node(
            action="device",
            target="变电站",
            value_point="d.u",
            values=[DiagramValue("d.i"), DiagramValue("d.u")],
            switch_point="d.ctrl",
            state_point="d.pos",
        )
        self.assertEqual(node.bound_points, ["d.u", "d.pos", "d.ctrl", "d.i"], "去重且保序")
        self.assertEqual(node.devices, ["变电站", "d"], "设备名 + 点号前缀里的设备")


# --------------------------------------------------------------- 画布


class CanvasMenuTest(unittest.TestCase):
    def setUp(self) -> None:
        self.written: list[tuple[str, object]] = []
        self.faults: list[list[str]] = []
        self.canvas = DiagramCanvas()
        self.addCleanup(self.canvas.deleteLater)
        self.canvas.set_actions(
            write_value=lambda pid, value: self.written.append((pid, value)),
            engine_running=lambda: True,
            active_faults=self._faults,
            open_editor=lambda: None,
            node_changed=lambda: None,
        )
        self.node = make_node(
            values=[DiagramValue("d.u", "Uab", "kV", 2)],
            switch_point="d.ctrl",
            switch_on=1,
            switch_off=2,
            state_point="d.pos",
            state_on=1,
            action="device",
            target="变电站",
        )
        self.canvas.diagram = DiagramDef(nodes=[self.node])

    def _faults(self, devices):
        self.faults.append(list(devices or []))
        return [{"time": "09:00", "device": "变电站", "tag": "u", "level": "critical",
                 "message": "过压", "value": 260, "state": "未复位"}]

    def texts(self, menu) -> list[str]:
        return [action.text() for action in menu.actions()]

    def test_menu_offers_values_switch_and_faults(self) -> None:
        menu = self.canvas.build_context_menu(self.node)
        labels = self.texts(menu)
        self.assertTrue(any("模块下面显示" in text for text in labels), labels)
        self.assertIn("合闸", labels)
        self.assertIn("分闸", labels)
        self.assertTrue(any("故障信息（1 条未复位）" in text for text in labels), labels)
        self.assertTrue(any("打开绑定的设备" in text for text in labels), labels)

    def test_switch_items_disabled_without_acquisition(self) -> None:
        self.canvas.engine_running = lambda: False
        menu = self.canvas.build_context_menu(self.node)
        by_text = {action.text(): action for action in menu.actions()}
        self.assertFalse(by_text["合闸"].isEnabled())
        self.assertFalse(by_text["分闸"].isEnabled())

    def test_state_label_follows_bound_state_point(self) -> None:
        self.canvas.values = {"d.pos": (1, "good")}
        menu = self.canvas.build_context_menu(self.node)
        self.assertIn("当前开关状态：合闸", self.texts(menu))
        self.canvas.values = {"d.pos": (2, "good")}
        menu = self.canvas.build_context_menu(self.node)
        self.assertIn("当前开关状态：分闸", self.texts(menu))

    def test_state_bit_reads_a_bit_field(self) -> None:
        """遥信字是位域（bit0 合位 / bit1 分位），不能拿整值和 1 比。"""
        node = make_node(state_point="d.pos", state_bit=0)
        self.canvas.values = {"d.pos": (21, "good")}  # 合位+储能+远方
        self.assertTrue(self.canvas.switch_state(node))
        self.canvas.values = {"d.pos": (22, "good")}  # 分位+储能+远方
        self.assertFalse(self.canvas.switch_state(node))
        node.state_bit = 1
        self.assertTrue(self.canvas.switch_state(node))
        node.state_bit = -1  # 不按位就退回按值比较
        self.assertFalse(self.canvas.switch_state(node))

    def test_close_writes_on_value_and_open_writes_off_value(self) -> None:
        with mock.patch.object(QMessageBox, "exec", lambda self: QMessageBox.Yes):
            self.canvas.operate_switch(self.node, True)
            self.canvas.operate_switch(self.node, False)
        self.assertEqual(self.written, [("d.ctrl", 1.0), ("d.ctrl", 0.0 + 2.0)])

    def test_cancel_writes_nothing(self) -> None:
        with mock.patch.object(QMessageBox, "exec", lambda self: QMessageBox.No):
            self.canvas.operate_switch(self.node, True)
        self.assertEqual(self.written, [], "点取消绝不能真写")

    def test_switch_state_without_state_point_is_none(self) -> None:
        node = make_node()
        self.assertIsNone(self.canvas.switch_state(node))
        node.state_point = "d.pos"
        self.assertIsNone(self.canvas.switch_state(node), "没有值也算不知道")
        self.canvas.values = {"d.pos": (1, "good")}
        self.assertTrue(self.canvas.switch_state(node))

    def test_empty_area_menu_is_short(self) -> None:
        menu = self.canvas.build_context_menu(None)
        self.assertEqual(self.texts(menu), ["复位视图", "编辑主回路图…"])

    def test_values_dialog_result_lands_on_node(self) -> None:
        dialog_values = [DiagramValue("d.p", "P", "kW", 0)]
        changed: list[bool] = []
        self.canvas.node_changed = lambda: changed.append(True)
        with mock.patch.object(NodeValuesDialog, "exec", lambda self: QDialog.Accepted), \
                mock.patch.object(NodeValuesDialog, "values", lambda self: dialog_values):
            self.canvas.edit_node_values(self.node)
        self.assertEqual(self.node.values, dialog_values)
        self.assertTrue(changed, "改了图元要通知工程变脏")


class CanvasFaultQueryTest(unittest.TestCase):
    def test_faults_are_queried_with_node_devices(self) -> None:
        canvas = DiagramCanvas()
        self.addCleanup(canvas.deleteLater)
        seen: list[list[str]] = []
        canvas.set_actions(active_faults=lambda devices: seen.append(list(devices)) or [])
        node = make_node(value_point="变电站测控装置.电压")
        canvas._faults_of(node)
        self.assertEqual(seen, [["变电站测控装置"]])
        canvas.active_faults = lambda devices: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertEqual(canvas._faults_of(node), [], "故障源出错不该把界面带崩")


# --------------------------------------------------------------- 对话框


class NodeValuesDialogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.points = [("d.u", "母线电压"), ("d.i", "母线电流"), ("d.p", "有功功率")]

    def test_existing_values_are_loaded_and_edited(self) -> None:
        node = make_node(values=[DiagramValue("d.u", "Uab", "kV", 2)])
        dialog = NodeValuesDialog(node, self.points)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.values()[0].point, "d.u")
        dialog.table.cellWidget(0, 1).setText("线电压")
        dialog.table.cellWidget(0, 3).setValue(3)
        value = dialog.values()[0]
        self.assertEqual(value.label, "线电压")
        self.assertEqual(value.decimals, 3)

    def test_add_and_remove_rows(self) -> None:
        dialog = NodeValuesDialog(make_node(), self.points)
        self.addCleanup(dialog.deleteLater)
        dialog._add_row(DiagramValue("d.i"))
        self.assertEqual(dialog.table.rowCount(), 1)
        dialog.table.selectRow(0)
        dialog._remove_selected()
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertEqual(dialog.values(), [])

    def test_row_limit(self) -> None:
        dialog = NodeValuesDialog(make_node(), self.points)
        self.addCleanup(dialog.deleteLater)
        for _ in range(MAX_VALUES + 3):
            dialog._add_row(DiagramValue("d.u"))
        self.assertEqual(dialog.table.rowCount(), MAX_VALUES)

    def test_rows_without_point_are_skipped_but_empty_table_is_fine(self) -> None:
        dialog = NodeValuesDialog(make_node(), self.points)
        self.addCleanup(dialog.deleteLater)
        dialog._add_row(DiagramValue(""))
        self.assertEqual(dialog.values(), [])
        with mock.patch.object(QMessageBox, "warning", mock.Mock(return_value=QMessageBox.Ok)) as warned:
            dialog._accept()
        self.assertTrue(warned.called, "有行却没点号要提醒")


class NodeSwitchDialogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.points = [("d.ctrl", "遥控命令字"), ("d.pos", "断路器位置")]

    def test_apply_to_writes_back_fields(self) -> None:
        node = make_node()
        dialog = NodeSwitchDialog(node, self.points)
        self.addCleanup(dialog.deleteLater)
        dialog.switch_combo.setCurrentIndex(dialog.switch_combo.findData("d.ctrl"))
        dialog.state_combo.setCurrentIndex(dialog.state_combo.findData("d.pos"))
        dialog.on_box.setValue(1)
        dialog.off_box.setValue(2)
        dialog.apply_to(node)
        self.assertEqual(node.switch_point, "d.ctrl")
        self.assertEqual(node.state_point, "d.pos")
        self.assertEqual((node.switch_on, node.switch_off), (1.0, 2.0))

    def test_clearing_switch_point(self) -> None:
        node = make_node(switch_point="d.ctrl", state_point="d.pos")
        dialog = NodeSwitchDialog(node, self.points)
        self.addCleanup(dialog.deleteLater)
        dialog.switch_combo.setCurrentIndex(0)
        dialog.state_combo.setCurrentIndex(0)
        dialog.apply_to(node)
        self.assertEqual(node.switch_point, "")
        self.assertEqual(node.state_point, "")


class FaultInfoDialogTest(unittest.TestCase):
    def test_rows_and_headline(self) -> None:
        rows = [
            {"time": "09:00", "device": "变电站", "tag": "u", "level": "critical",
             "message": "过压", "value": 260, "state": "未复位"},
            {"time": "09:01", "device": "变电站", "tag": "i", "level": "warning",
             "message": "过流", "value": 5, "state": "未复位"},
        ]
        dialog = FaultInfoDialog("QF1", rows, ["变电站"])
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.table.rowCount(), 2)
        self.assertIn("2 条", dialog.headline.text())
        self.assertIn("变电站", dialog.table.item(0, 1).text())

    def test_empty_state_says_all_clear(self) -> None:
        dialog = FaultInfoDialog("QF1", [], ["变电站"])
        self.addCleanup(dialog.deleteLater)
        self.assertIn("没有未复位", dialog.headline.text())
        self.assertEqual(dialog.table.rowCount(), 0)


# --------------------------------------------------------------- 示例工程


class SampleDiagramTest(unittest.TestCase):
    def test_every_bound_point_exists_in_the_sample_project(self) -> None:
        project = project_io.grid_project()
        missing: list[str] = []
        for node in project.diagram.nodes:
            for pid in node.bound_points:
                if project.find_point(pid) is None:
                    missing.append(f"{node.id}->{pid}")
        self.assertEqual(missing, [], f"示例图里挂了不存在的点：{missing}")

    def test_sample_shows_values_under_modules(self) -> None:
        project = project_io.grid_project()
        with_values = [node for node in project.diagram.nodes if node.values]
        self.assertGreaterEqual(len(with_values), 3, "示例里得有图元挂电压电流做示范")
        bus = project.diagram.node("bus10")
        self.assertIsNotNone(bus)
        self.assertGreaterEqual(len(bus.values), 2)

    def test_sample_breaker_can_be_operated(self) -> None:
        project = project_io.grid_project()
        breaker = project.diagram.node("qf1")
        self.assertIsNotNone(breaker)
        self.assertTrue(breaker.switch_point)
        self.assertEqual(breaker.devices, ["变电站测控装置"])
        control = project.find_point(breaker.switch_point)
        self.assertIsNotNone(control)
        _device, point = control
        self.assertTrue(point.writable, "开关操作点必须可写")
        self.assertIsNotNone(project.find_point(breaker.state_point))


# --------------------------------------------------------------- 主窗口接线


class MainWindowBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.grid_project()
        paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, project_store.safe_name(self.project.name)), self.project
        )
        from icomm.ui.main_window import MainWindow

        self.window = MainWindow(self.project, paths, workdir=self.tmp.name, base_dir=self.tmp.name)
        self.window.settings.confirm_on_exit = False
        self.addCleanup(self.window.close)

    def test_right_click_actions_are_bound(self) -> None:
        canvas = self.window.overview_page.canvas
        self.assertTrue(canvas.actions_ready(), "主页图元要接上写值/故障回调")
        self.assertTrue(canvas.point_titles, "点位标题要传进画布（模块下面显示名要用）")

    def test_active_alarms_reach_the_node(self) -> None:
        self.window.alarm_page.on_alarm(
            FakeAlarm("变电站测控装置.母线AB线电压", "变电站测控装置", "母线AB线电压", "过压", level="critical")
        )
        rows = self.window._node_faults(["变电站测控装置"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message"], "过压")
        self.assertEqual(self.window._node_faults(["别的设备"]), [])

    def test_offline_device_shows_up_as_a_fault(self) -> None:
        self.window._device_online["变电站测控装置"] = False
        rows = self.window._node_faults(["变电站测控装置"])
        self.assertTrue(any("通讯中断" in row["message"] for row in rows), rows)

    def test_reset_alarm_leaves_the_list(self) -> None:
        event = FakeAlarm("变电站测控装置.x", "变电站测控装置", "x", "过压")
        self.window.alarm_page.on_alarm(event)
        self.assertEqual(len(self.window.alarm_page.active_alarms(["变电站测控装置"])), 1)
        event.active = False
        event.value = 0
        self.window.alarm_page.on_alarm(event)
        self.assertEqual(self.window.alarm_page.active_alarms(["变电站测控装置"]), [])

    def test_editing_a_node_marks_project_dirty(self) -> None:
        self.window._dirty = False
        self.window._on_diagram_node_changed()
        self.assertTrue(self.window._dirty)

    def test_switch_operation_goes_through_the_write_channel(self) -> None:
        node = self.project.diagram.node("qf1")
        written: list[tuple[str, object]] = []
        self.window.overview_page.canvas.write_value = lambda pid, value: written.append((pid, value))
        with mock.patch.object(QMessageBox, "exec", lambda self: QMessageBox.Yes):
            self.window.overview_page.canvas.operate_switch(node, False)
        self.assertEqual(written, [(node.switch_point, node.switch_off)])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
