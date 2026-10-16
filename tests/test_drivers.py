"""批量读规划与驱动注册表测试。"""

from __future__ import annotations

import unittest

from icomm.core.drivers import (
    DriverError,
    create_driver,
    driver_catalog,
    driver_class,
    driver_names,
)
from icomm.core.drivers.modbus import _decode_array, plan_batches
from icomm.core.models import ChannelDef, DeviceDef, PointDef, default_channel_options


def _point(name: str, address: int, register: str = "holding", data_type: str = "uint16") -> PointDef:
    return PointDef(name=name, address=address, register=register, data_type=data_type)


class PlanBatchesTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(plan_batches([]), [])

    def test_contiguous_merged_into_one_request(self):
        points = [_point("a", 0), _point("b", 1), _point("c", 2)]
        batches = plan_batches(points)
        self.assertEqual(len(batches), 1)
        register, start, count, members = batches[0]
        self.assertEqual(register, "holding")
        self.assertEqual(start, 0)
        self.assertEqual(count, 3)
        self.assertEqual([p.name for p, _o in members], ["a", "b", "c"])

    def test_gap_within_threshold_merged(self):
        # 0 号点位宽 1，下一个起始 8：空洞 = 8 - 1 = 7 <= 8，应合并
        batches = plan_batches([_point("a", 0), _point("b", 8)], max_gap=8)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][2], 9)

    def test_gap_beyond_threshold_split(self):
        # 空洞 = 10 - 1 = 9 > 8，应拆成两次请求
        batches = plan_batches([_point("a", 0), _point("b", 10)], max_gap=8)
        self.assertEqual(len(batches), 2)
        self.assertEqual({b[1] for b in batches}, {0, 10})

    def test_max_regs_splits_request(self):
        points = [_point(f"p{i}", i) for i in range(200)]
        batches = plan_batches(points, max_regs=120)
        self.assertTrue(all(b[2] <= 120 for b in batches))
        covered = {p.name for _r, _s, _c, members in batches for p, _o in members}
        self.assertEqual(len(covered), 200)

    def test_register_types_never_mixed(self):
        points = [
            _point("h", 0, "holding"),
            _point("c", 0, "coil", "bool"),
            _point("d", 0, "discrete", "bool"),
            _point("i", 0, "input"),
        ]
        batches = plan_batches(points)
        self.assertEqual({b[0] for b in batches}, {"holding", "coil", "discrete", "input"})
        self.assertEqual(len(batches), 4)

    def test_offsets_for_32bit_points(self):
        points = [_point("wide", 4, data_type="float32"), _point("narrow", 6)]
        batches = plan_batches(points)
        self.assertEqual(len(batches), 1)
        _register, start, count, members = batches[0]
        self.assertEqual(start, 4)
        self.assertEqual(count, 3)
        offsets = {p.name: o for p, o in members}
        self.assertEqual(offsets["wide"], 0)
        self.assertEqual(offsets["narrow"], 2)

    def test_unsorted_input(self):
        points = [_point("c", 2), _point("a", 0), _point("b", 1)]
        batches = plan_batches(points)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0][1], 0)
        self.assertEqual([p.name for p, _o in batches[0][3]], ["a", "b", "c"])

    def test_unknown_register_skipped(self):
        batches = plan_batches([_point("x", 0, register="weird")])
        self.assertEqual(batches, [])


class RegistryTest(unittest.TestCase):
    def test_modbus_registered(self):
        self.assertIn("modbus", driver_names())
        self.assertIsNotNone(driver_class("modbus"))

    def test_catalog_has_description(self):
        catalog = {entry["name"]: entry for entry in driver_catalog()}
        self.assertIn("modbus", catalog)
        self.assertIn("tcp", catalog["modbus"]["link_types"])
        self.assertIn("rtu", catalog["modbus"]["link_types"])
        self.assertIn("rtu_over_tcp", catalog["modbus"]["link_types"])
        self.assertTrue(catalog["modbus"]["description"])

    def test_unknown_driver_raises(self):
        channel = ChannelDef(name="x", driver="not_installed", link="tcp")
        with self.assertRaises(DriverError):
            create_driver(channel)

    def test_rtu_option_schema(self):
        schema = driver_class("modbus").option_schema("rtu")
        keys = {field.key for field in schema}
        self.assertIn("port", keys)
        self.assertIn("baudrate", keys)
        self.assertIn("parity", keys)
        for field in schema:
            if field.key == "parity":
                self.assertEqual(field.type, "choice")
                self.assertIn("N", field.choices)

    def test_tcp_option_schema(self):
        schema = driver_class("modbus").option_schema("tcp")
        keys = {field.key for field in schema}
        self.assertIn("host", keys)
        self.assertIn("port", keys)
        self.assertIn("timeout", keys)

    def test_driver_can_be_constructed(self):
        channel = ChannelDef(name="ch", driver="modbus", link="tcp", options=default_channel_options("tcp"))
        driver = create_driver(channel)
        self.assertFalse(driver.is_open)
        self.assertEqual(driver.describe(), "modbus@ch")
        driver.close()  # 未连接时关闭不应抛异常


class ArrayBatchTest(unittest.TestCase):
    """数组/块点位（录波）的批量读规划。"""

    @staticmethod
    def _array(name: str, address: int, count: int, data_type: str = "uint16", register: str = "holding") -> PointDef:
        return PointDef(
            name=name, address=address, register=register, data_type=data_type,
            kind="array", count=count,
        )

    def test_array_gets_its_own_batch(self):
        batches = plan_batches([self._array("wave", 0, 200), _point("scalar", 0)])
        self.assertEqual(len(batches), 2)
        array_batch = [b for b in batches if b[2] == 200][0]
        self.assertEqual(array_batch[1], 0)
        self.assertEqual(array_batch[3][0][0].name, "wave")

    def test_array_not_merged_with_neighbours(self):
        """即使地址紧邻，数组点位也不并入标量批次（避免一段波形失败连累其它点）。"""
        batches = plan_batches([_point("a", 0), self._array("wave", 1, 100), _point("b", 101)])
        names = [b[3][0][0].name for b in batches]
        self.assertIn("wave", names)
        wave_batch = [b for b in batches if b[3][0][0].name == "wave"][0]
        self.assertEqual(wave_batch[2], 100)
        self.assertEqual(len(wave_batch[3]), 1)

    def test_array_span_counts_words(self):
        batches = plan_batches([self._array("f32", 0, 10, "float32")])
        self.assertEqual(batches[0][2], 20)  # 10 个 float32 = 20 个寄存器

    def test_two_arrays_both_scheduled(self):
        batches = plan_batches([self._array("w1", 0, 50), self._array("w2", 100, 50)])
        self.assertEqual(len(batches), 2)
        self.assertEqual({b[1] for b in batches}, {0, 100})

    def test_decode_array_applies_scale(self):
        point = self._array("wave", 0, 4, "int16")
        point.scale = 0.5
        values = _decode_array(point, [10, 20, 0xFFFF, 0x8000], 0)
        self.assertEqual(values[0], 5.0)
        self.assertEqual(values[1], 10.0)
        self.assertEqual(values[2], -0.5)  # -1 * 0.5
        self.assertEqual(values[3], -16384.0)  # int16 最小值 * 0.5

    def test_decode_array_respects_offset(self):
        point = self._array("wave", 0, 2, "uint16")
        values = _decode_array(point, [0, 0, 100, 200], 2)
        self.assertEqual(values, [100, 200])

    def test_decode_array_insufficient_data(self):
        point = self._array("wave", 0, 4)
        with self.assertRaises(Exception):
            _decode_array(point, [1, 2], 0)

    def test_decode_array_float32(self):
        point = self._array("wave", 0, 2, "float32")
        payload = [0x41CC, 0x0000, 0xC1CC, 0x0000]  # 25.5, -25.5
        values = _decode_array(point, payload, 0)
        self.assertAlmostEqual(values[0], 25.5, places=4)
        self.assertAlmostEqual(values[1], -25.5, places=4)


class _StubResponse:
    def __init__(self, registers):
        self.registers = registers

    def isError(self):  # noqa: item2
        return False


class _StubClient:
    """记录每次请求的地址与数量，用来验证分块逻辑。"""

    def __init__(self, size: int = 4096):
        self.data = [i & 0xFFFF for i in range(size)]
        self.calls: list[tuple[int, int]] = []

    @property
    def connected(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def read_holding_registers(self, address, count=1, device_id=1):
        self.calls.append((address, count))
        return _StubResponse(self.data[address : address + count])


class ChunkedReadTest(unittest.TestCase):
    """一次读几百个寄存器时必须自动分块，否则设备会返回异常响应。"""

    def setUp(self):
        self.channel = ChannelDef(
            name="ch",
            driver="modbus",
            link="tcp",
            options={**default_channel_options("tcp"), "max_regs_per_read": 120},
        )
        self.driver = create_driver(self.channel)
        self.client = _StubClient()
        self.driver._client = self.client
        self.device = DeviceDef(name="dsp", channel="ch", unit_id=11)

    def test_exactly_one_frame(self):
        data = self.driver._read_once("holding", 0, 120, self.device, 0)
        self.assertEqual(len(data), 120)
        self.assertEqual(self.client.calls, [(0, 120)])

    def test_split_into_three_frames(self):
        data = self.driver._read_once("holding", 0, 300, self.device, 0)
        self.assertEqual(len(data), 300)
        self.assertEqual(self.client.calls, [(0, 120), (120, 120), (240, 60)])

    def test_each_frame_never_exceeds_limit(self):
        self.driver._read_once("holding", 10, 1000, self.device, 0)
        self.assertTrue(all(count <= 120 for _addr, count in self.client.calls))
        self.assertEqual(sum(count for _a, count in self.client.calls), 1000)

    def test_contiguous_coverage(self):
        self.driver._read_once("holding", 50, 250, self.device, 0)
        expected = 50
        for address, count in self.client.calls:
            self.assertEqual(address, expected)
            expected += count
        self.assertEqual(expected, 300)


if __name__ == "__main__":
    unittest.main()
