"""点表模板：CAN / IEC 61850 两份示例要能原样导回来。

换协议最容易错的就是「寄存器区 / 协议路径 / 起始地址」这三列 —— 它们在
Modbus 里是寄存器区+地址，在 61850 里是 FC+对象引用，在 CAN 里是收发区+帧号+字节偏移。
这里让两份模板真过一遍导出-导入，等于把这三列的含义钉住。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from icomm.core import csv_table
from icomm.core.models import PointDef, Project

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")

CAN_CSV = os.path.join(EXAMPLES, "can_parameters.csv")
IEC61850_CSV = os.path.join(EXAMPLES, "iec61850_parameters.csv")
S7_CSV = os.path.join(EXAMPLES, "s7_parameters.csv")


def _import(path: str, base: Project | None = None) -> tuple[Project, list[str], list[str]]:
    """导入点表：返回 ``(工程, 错误, 警告)``。

    点表只带点位，**通道要另外配**（协议与参数在「设备与点位 → 通道属性」里设），
    所以"通道不存在，已按默认参数创建"这类是 info/警告，不算错误。
    """
    project = base or Project(name="导入目标")
    report = csv_table.import_csv(project, path)
    errors = [f"第{issue.row}行 {issue.message}" for issue in report.errors]
    others = [
        f"第{issue.row}行 {issue.message}"
        for issue in report.issues
        if issue.level != "error"
    ]
    return project, errors, others


class ExampleFilesTest(unittest.TestCase):
    def test_files_exist(self) -> None:
        self.assertTrue(os.path.exists(CAN_CSV), CAN_CSV)
        self.assertTrue(os.path.exists(IEC61850_CSV), IEC61850_CSV)

    def test_header_has_protocol_path_column(self) -> None:
        rows, issues = csv_table.read_rows(CAN_CSV)
        self.assertEqual(issues, [])
        self.assertIn("协议路径", rows[0])
        self.assertIn("寄存器区", rows[0])
        self.assertIn("协议路径", csv_table.CSV_COLUMNS)


class CanExampleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project, self.errors, self.notes = _import(CAN_CSV)

    def test_imports_without_errors(self) -> None:
        self.assertEqual(self.errors, [])

    def test_channel_is_created_for_manual_config(self) -> None:
        """点表只管点位：通道会被建出来（默认参数），协议要自己去通道属性里选。"""
        channel = self.project.channels[0]
        self.assertEqual(channel.name, "CAN总线")
        self.assertTrue(
            any("CAN总线" in text for text in self.notes), self.notes
        )

    def test_device_and_point_count(self) -> None:
        device = self.project.devices[0]
        self.assertEqual(device.name, "储能变流器")
        self.assertEqual(device.unit_id, 5, "CANopen 节点号取从站号")
        self.assertEqual(len(device.points), 13)

    def test_channels_can_be_configured_from_template(self) -> None:
        """模板里的通道参数（生成时写的那些）可以被照抄进工程。"""
        import tools.make_protocol_examples as maker

        expected = maker.can_project().channels[0]
        channel = self.project.channels[0]
        channel.driver = expected.driver
        channel.link = expected.link
        channel.options = dict(expected.options)
        self.assertEqual(channel.driver, "can")
        self.assertEqual(channel.options.get("backend"), "virtual")

    def test_three_point_areas(self) -> None:
        areas = {point.register for point in self.project.devices[0].points}
        self.assertLessEqual({"can_rx", "can_tx", "can_sdo"}, areas)

    def test_frame_mapping_fields(self) -> None:
        point = self.project.find_point("储能变流器.输出电流")[1]
        self.assertEqual(point.register, "can_rx")
        self.assertEqual(point.path, "0x181")
        self.assertEqual(point.address, 2)
        self.assertEqual(point.data_type, "uint16")
        self.assertAlmostEqual(point.scale, 0.1)
        self.assertEqual(point.unit, "A")

    def test_canopen_object_mapping(self) -> None:
        point = self.project.find_point("储能变流器.SDO状态字")[1]
        self.assertEqual(point.register, "can_sdo")
        self.assertEqual(point.path, "0x6041:0")

    def test_write_and_command_points_survive(self) -> None:
        writable = self.project.find_point("储能变流器.电压给定")[1]
        self.assertTrue(writable.writable)
        self.assertEqual(writable.register, "can_tx")
        command = self.project.find_point("储能变流器.控制字")[1]
        self.assertEqual([value for value, _name in command.command_items()], [1, 2, 4])

    def test_bit_definitions_survive(self) -> None:
        point = self.project.find_point("储能变流器.状态字")[1]
        self.assertIn((8, "运行"), point.bit_labels())

    def test_roundtrip_keeps_protocol_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "again.csv")
            csv_table.export_csv(self.project, path)
            again, errors, _notes = _import(path)
        self.assertEqual(errors, [])
        point = again.find_point("储能变流器.输出电流")[1]
        self.assertEqual((point.register, point.path, point.address), ("can_rx", "0x181", 2))


class Iec61850ExampleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project, self.errors, self.notes = _import(IEC61850_CSV)

    def test_imports_without_errors(self) -> None:
        self.assertEqual(self.errors, [])

    def test_channel_template_matches_bundled_simulator(self) -> None:
        """模板里给出的通道参数要对着内置仿真器（照抄就能连上）。"""
        import tools.make_protocol_examples as maker

        expected = maker.iec61850_project().channels[0]
        self.assertEqual(expected.driver, "iec61850")
        options = expected.options
        self.assertEqual(options.get("ied_name"), "VVPSIM")
        self.assertEqual(int(options.get("port")), 8102)
        self.assertTrue(options.get("reports"))
        self.assertIn("RP1", str(options.get("rcb_refs")))

    def test_fc_and_reference_mapping(self) -> None:
        point = self.project.find_point("测量.频率")[1]
        self.assertEqual(point.register, "MX")
        self.assertEqual(point.path, "MEAS/MMXU1.Hz.mag.f")
        self.assertEqual(point.data_type, "float32")

    def test_multiple_logical_devices(self) -> None:
        names = [device.name for device in self.project.devices]
        self.assertEqual(names, ["测量", "控制"])

    def test_status_code_point_survives(self) -> None:
        point = self.project.find_point("控制.开关位置")[1]
        self.assertEqual(point.register, "ST")
        self.assertEqual(point.path, "CTRL/XCBR1.Pos.stVal")
        self.assertEqual(point.code_labels()[0], (0, "中间"))

    def test_writable_setpoints_survive(self) -> None:
        for pid in ("测量.定值", "测量.死区"):
            self.assertTrue(self.project.find_point(pid)[1].writable, pid)

    def test_references_are_usable_by_driver(self) -> None:
        """模板里的引用要能直接被驱动解析（含"IED 名前缀"那条路径）。"""
        import tools.make_protocol_examples as maker
        from icomm.core.drivers.iec61850 import Iec61850Driver

        channel = maker.iec61850_project().channels[0]
        driver = Iec61850Driver(channel)
        device = self.project.devices[0]
        point = self.project.find_point("测量.频率")[1]
        reference, fc = driver._resolve(device, point)
        self.assertEqual(reference, "VVPSIMMEAS/MMXU1.Hz.mag.f")
        self.assertEqual(fc, "MX")

    def test_roundtrip_keeps_protocol_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "again.csv")
            csv_table.export_csv(self.project, path)
            again, errors, _notes = _import(path)
        self.assertEqual(errors, [])
        point = again.find_point("测量.频率")[1]
        self.assertEqual((point.register, point.path), ("MX", "MEAS/MMXU1.Hz.mag.f"))


class S7ExampleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project, self.errors, self.notes = _import(S7_CSV)

    def test_file_exists(self) -> None:
        self.assertTrue(os.path.exists(S7_CSV), S7_CSV)

    def test_imports_without_errors(self) -> None:
        self.assertEqual(self.errors, [])

    def test_channel_template_points_at_simulator(self) -> None:
        """模板里给出的通道参数要对着内置仿真 PLC（照抄就能连上）。"""
        import tools.make_protocol_examples as maker
        from tools.s7_sim import DEFAULT_PORT

        expected = maker.s7_project().channels[0]
        self.assertEqual(expected.driver, "s7")
        self.assertEqual(expected.link, "tcp")
        self.assertEqual(int(expected.options.get("port")), DEFAULT_PORT)
        self.assertEqual(int(expected.options.get("rack")), 0)
        self.assertEqual(int(expected.options.get("slot")), 1)

    def test_address_columns(self) -> None:
        point = self.project.find_point("S7储能变流器.voltage")[1]
        self.assertEqual(point.register, "db")
        self.assertEqual(point.path, "DB1.DBD0")
        self.assertEqual(point.address, 0)
        self.assertEqual(point.data_type, "float32")

    def test_all_four_areas_present(self) -> None:
        areas = {point.register for point in self.project.devices[0].points}
        self.assertLessEqual({"db", "i", "q", "m"}, areas)

    def test_byte_and_bit_points_survive(self) -> None:
        byte_point = self.project.find_point("S7储能变流器.device_code")[1]
        self.assertEqual(byte_point.data_type, "uint8", "8 位类型要能过 CSV 校验")
        bit_point = self.project.find_point("S7储能变流器.grid_allow")[1]
        self.assertEqual((bit_point.data_type, bit_point.path), ("bool", "DB1.DBX30.0"))
        self.assertTrue(bit_point.writable)

    def test_point_paths_match_bundled_simulator(self) -> None:
        """点表里的地址必须和仿真 PLC 的布局一一对应，否则演示时"连上了没数"。"""
        from tools.s7_sim import LAYOUT

        points = {point.name: point for point in self.project.devices[0].points}
        self.assertEqual(set(points), {field.key for field in LAYOUT})
        for field in LAYOUT:
            with self.subTest(key=field.key):
                self.assertEqual(points[field.key].path, field.path)

    def test_bit_and_command_definitions_survive(self) -> None:
        status = self.project.find_point("S7储能变流器.status_word")[1]
        self.assertIn((8, "交流接触器合"), status.bit_labels())
        control = self.project.find_point("S7储能变流器.control_word")[1]
        self.assertEqual([value for value, _name in control.command_items()], [1, 2, 4])

    def test_roundtrip_keeps_protocol_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "again.csv")
            csv_table.export_csv(self.project, path)
            again, errors, _notes = _import(path)
        self.assertEqual(errors, [])
        point = again.find_point("S7储能变流器.voltage")[1]
        self.assertEqual((point.register, point.path, point.address), ("db", "DB1.DBD0", 0))


class AreaValidationTest(unittest.TestCase):
    """四个协议的"点位区"都要能过导入校验，错别字要被挡住。"""

    def test_known_areas(self) -> None:
        known = csv_table.KNOWN_POINT_AREAS
        for value in ("holding", "input", "coil", "discrete"):
            self.assertIn(value, known)
        for value in ("MX", "ST", "CO", "SP", "CF", "DC"):
            self.assertIn(value, known)
        for value in ("can_rx", "can_tx", "can_sdo"):
            self.assertIn(value, known)
        for value in ("db", "i", "q", "m"):
            self.assertIn(value, known, "西门子 S7 的四个数据区")

    def test_typo_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "bad.csv")
            header = list(csv_table.CSV_COLUMNS)
            row = {name: "" for name in header}
            row.update(
                {
                    "启用": "是",
                    "通道": "C",
                    "设备": "D",
                    "点号": "p",
                    "寄存器区": "holdings",
                    "起始地址": "0",
                }
            )
            import csv as csv_module

            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                writer = csv_module.DictWriter(handle, fieldnames=header)
                writer.writeheader()
                writer.writerow(row)
            _project, errors, _notes = _import(path)
        self.assertTrue(any("寄存器区" in text for text in errors), errors)

    def test_path_column_is_optional(self) -> None:
        """老点表没有「协议路径」列也要能导入（Modbus 用不到这一列）。"""
        rows = [{"设备": "D", "点号": "p", "寄存器区": "holding", "起始地址": 0}]
        project = Project(name="x")
        report = csv_table.import_rows(
            project, rows, csv_table.ImportReport(), csv_table.CSV_COLUMNS
        )
        self.assertEqual(report.errors, [])
        self.assertEqual(project.devices[0].points[0].path, "")


if __name__ == "__main__":
    unittest.main()
