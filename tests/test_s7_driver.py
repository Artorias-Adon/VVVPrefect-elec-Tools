"""西门子 S7 驱动测试。

分两层：

* **逻辑层**（不依赖库）：地址解析（``DB1.DBX4.0`` / ``MW12`` / ``M10.0`` / ``IB0`` …）、
  批量合并、字节编解码、参数规格、错误分支 —— 这些是现场最容易填错又最难发现的地方；
* **端到端**：拿 ``tools/s7_sim.py`` 起一台真的 S7 服务端，驱动连上去读点表里那 22 个点位、
  写给定值/控制字/位，验证"点表按西门子地址写 + 通道填端口与机架槽号"这条常用路径真的通。
"""

from __future__ import annotations

import socket
import time
import unittest

from icomm.core.drivers import create_driver, driver_class, driver_names
from icomm.core.drivers.base import DriverError
from icomm.core.drivers.s7 import (
    AREA_LABELS,
    POINT_AREAS,
    S7Driver,
    _decode_value,
    _encode_value,
    _Item,
    item_width,
    parse_address,
    plan_spans,
)
from icomm.core.models import ChannelDef, DeviceDef, PointDef

try:
    import snap7  # noqa: F401

    HAVE_LIB = True
except Exception:  # pragma: no cover - 取决于运行环境
    HAVE_LIB = False

REQUIRES_LIB = unittest.skipUnless(HAVE_LIB, "未安装 python-snap7 库")


def _channel(port: int = 10102, **overrides) -> ChannelDef:
    options = {
        "host": "127.0.0.1",
        "port": port,
        "rack": 0,
        "slot": 1,
        "timeout": 3.0,
        "max_span_bytes": 200,
        "max_gap": 8,
        "retry": 0,
    }
    options.update(overrides)
    return ChannelDef(name="S7主站", driver="s7", link="tcp", options=options)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _point(**kwargs) -> PointDef:
    data = dict(name="p", register="db", path="DB1.DBD0", data_type="float32")
    data.update(kwargs)
    return PointDef(**data)


# --------------------------------------------------------------- 地址解析


class AddressParseTest(unittest.TestCase):
    def test_db_bit_address(self) -> None:
        addr = parse_address("DB1.DBX4.0", is_bit=True)
        self.assertEqual((addr.area, addr.db, addr.byte, addr.bit), ("db", 1, 4, 0))
        self.assertEqual(addr.label, "DB1.4.0")

    def test_db_byte_word_dword(self) -> None:
        self.assertEqual(parse_address("DB1.DBB4").byte, 4)
        self.assertEqual(parse_address("DB1.DBW6").byte, 6)
        self.assertEqual(parse_address("DB1.DBD8").byte, 8)
        self.assertEqual(parse_address("DB1.DBD8").size, "D")

    def test_db_number_only_uses_address_column(self) -> None:
        addr = parse_address("DB1", byte=12)
        self.assertEqual((addr.area, addr.db, addr.byte), ("db", 1, 12))
        plain = parse_address("2", byte=6)
        self.assertEqual((plain.db, plain.byte), (2, 6))

    def test_db_shorthand_with_offset(self) -> None:
        addr = parse_address("DB3.14")
        self.assertEqual((addr.db, addr.byte, addr.bit), (3, 14, None))

    def test_marker_area_forms(self) -> None:
        bit = parse_address("M10.3", is_bit=True)
        self.assertEqual((bit.area, bit.byte, bit.bit), ("m", 10, 3))
        self.assertEqual((parse_address("MB10").area, parse_address("MB10").byte), ("m", 10))
        self.assertEqual((parse_address("MW12").area, parse_address("MW12").size), ("m", "W"))
        self.assertEqual(parse_address("MD14").byte, 14)

    def test_input_output_areas_both_languages(self) -> None:
        self.assertEqual(parse_address("IB0").area, "i")
        self.assertEqual(parse_address("E0.0", is_bit=True).area, "i")
        self.assertEqual(parse_address("IW64").byte, 64)
        self.assertEqual(parse_address("QB0").area, "q")
        self.assertEqual(parse_address("A0.0", is_bit=True).area, "q")
        self.assertEqual(parse_address("QD4").byte, 4)

    def test_case_and_space_insensitive(self) -> None:
        self.assertEqual(parse_address("db1.dbd8").byte, 8)
        self.assertEqual(parse_address(" db1 . dbx4 . 0 ", is_bit=True).bit, 0)
        self.assertEqual(parse_address("mw12").area, "m")

    def test_empty_path_falls_back_to_area_and_address(self) -> None:
        addr = parse_address("", area="m", byte=16)
        self.assertEqual((addr.area, addr.byte), ("m", 16))
        bit = parse_address("", area="i", byte=0, is_bit=True)
        self.assertEqual(bit.bit, 0, "位点位不写位号就是第 0 位")
        self.assertIsNone(parse_address("", area="q", byte=1).bit)

    def test_empty_path_without_db_number_is_rejected(self) -> None:
        with self.assertRaises(DriverError) as ctx:
            parse_address("", area="db", byte=8)
        self.assertIn("协议路径", str(ctx.exception))

    def test_area_aliases(self) -> None:
        for text, expected in (
            ("数据块", "db"),
            ("PE", "i"),
            ("输入", "i"),
            ("A", "q"),
            ("输出", "q"),
            ("MK", "m"),
            ("位存储", "m"),
        ):
            with self.subTest(text=text):
                parsed = parse_address("DB1" if expected == "db" else "", area=text, byte=0)
                self.assertEqual(parsed.area, expected)
                if expected == "db":
                    self.assertEqual(parsed.db, 1)

    def test_unknown_area_rejected(self) -> None:
        with self.assertRaises(DriverError) as ctx:
            parse_address("", area="holding", byte=0)
        self.assertIn("寄存器区", str(ctx.exception))

    def test_unknown_address_rejected(self) -> None:
        for text in ("XYZ", "T1", "DB1.DBQ4", "M", "1.2.3"):
            with self.subTest(text=text):
                with self.assertRaises(DriverError):
                    parse_address(text)

    def test_prefix_labels(self) -> None:
        self.assertEqual(parse_address("DB7.DBD0").prefix, "DB7")
        self.assertEqual(parse_address("MW12").prefix, "M")
        self.assertEqual(parse_address("IW4").area_label, "I")
        self.assertEqual(AREA_LABELS["q"], "Q")


# --------------------------------------------------------------- 宽度/合并


class WidthAndSpanTest(unittest.TestCase):
    def test_item_width_by_type(self) -> None:
        self.assertEqual(item_width("float32", False), 4)
        self.assertEqual(item_width("float64", False), 8)
        self.assertEqual(item_width("uint8", False), 1)
        self.assertEqual(item_width("int16", False), 2)
        self.assertEqual(item_width("uint32", False), 4)
        self.assertEqual(item_width("float32", True), 1, "位点位按 1 字节读回再取位")

    def test_unsupported_type_rejected(self) -> None:
        with self.assertRaises(DriverError) as ctx:
            item_width("string", False)
        self.assertIn("不支持", str(ctx.exception))

    def _item(self, path: str, data_type: str = "float32", count: int = 1, array: bool = False):
        point = _point(path=path, data_type=data_type, kind="array" if array else "scalar", count=count)
        addr = parse_address(path, is_bit=data_type == "bool")
        return _Item(point=point, addr=addr, width=item_width(data_type, data_type == "bool"), count=count if array else 1)

    def test_contiguous_points_merged_into_one_request(self) -> None:
        spans = plan_spans([self._item("DB1.DBD0"), self._item("DB1.DBD4"), self._item("DB1.DBD8")])
        self.assertEqual(len(spans), 1)
        area, db, start, size, members = spans[0]
        self.assertEqual((area, db, start, size), ("db", 1, 0, 12))
        self.assertEqual([offset for _item, offset in members], [0, 4, 8])

    def test_gap_beyond_threshold_splits(self) -> None:
        spans = plan_spans([self._item("DB1.DBD0"), self._item("DB1.DBD100")], max_gap=8)
        self.assertEqual(len(spans), 2)

    def test_small_gap_is_tolerated(self) -> None:
        spans = plan_spans([self._item("DB1.DBD0"), self._item("DB1.DBD10")], max_gap=8)
        self.assertEqual(len(spans), 1)
        self.assertEqual(spans[0][3], 14, "空洞也读回来，省一次请求")

    def test_max_span_splits(self) -> None:
        spans = plan_spans(
            [self._item("DB1.DBD0"), self._item("DB1.DBD100"), self._item("DB1.DBD200")],
            max_span=64,
            max_gap=8,
        )
        self.assertEqual(len(spans), 3)

    def test_different_areas_and_dbs_are_separate(self) -> None:
        spans = plan_spans(
            [
                self._item("DB1.DBD0"),
                self._item("DB2.DBD0"),
                self._item("MW0"),
                self._item("IW0"),
            ]
        )
        keys = sorted((span[0], span[1]) for span in spans)
        self.assertEqual(keys, [("db", 1), ("db", 2), ("i", 0), ("m", 0)])

    def test_array_gets_its_own_span(self) -> None:
        spans = plan_spans([self._item("DB1.DBD0"), self._item("DB1.DBD100", count=64, array=True)])
        self.assertEqual(len(spans), 2)


# --------------------------------------------------------------- 编解码


class CodecTest(unittest.TestCase):
    def test_big_endian_decode(self) -> None:
        self.assertAlmostEqual(_decode_value("float32", bytes.fromhex("437a0000"), False, 0, False), 250.0)
        self.assertEqual(_decode_value("uint16", bytes.fromhex("1234"), False, 0, False), 0x1234)
        self.assertEqual(_decode_value("int32", bytes.fromhex("ffffffff"), False, 0, False), -1)
        self.assertEqual(_decode_value("uint8", b"\xa5", False, 0, False), 0xA5)

    def test_little_order_reverses_bytes(self) -> None:
        self.assertAlmostEqual(_decode_value("float32", bytes.fromhex("00007a43"), False, 0, True), 250.0)

    def test_bit_decode(self) -> None:
        self.assertTrue(_decode_value("bool", b"\x08", True, 3, False))
        self.assertFalse(_decode_value("bool", b"\x08", True, 2, False))

    def test_encode_roundtrip(self) -> None:
        for data_type, value in (
            ("float32", 380.25),
            ("float64", -1.5),
            ("uint16", 4321),
            ("int16", -1234),
            ("uint32", 123456),
            ("int32", -76543),
            ("uint8", 200),
            ("int8", -100),
        ):
            with self.subTest(data_type=data_type):
                payload = _encode_value(data_type, value, False)
                self.assertAlmostEqual(
                    _decode_value(data_type, payload, False, 0, False), value, places=4
                )


# --------------------------------------------------------------- 参数与注册


class DriverMetaTest(unittest.TestCase):
    def test_registered(self) -> None:
        self.assertIn("s7", driver_names())
        self.assertIs(driver_class("s7"), S7Driver)
        driver = create_driver(_channel())
        self.assertIsInstance(driver, S7Driver)
        self.assertFalse(driver.is_open)

    def test_point_areas(self) -> None:
        values = [value for value, _label in S7Driver.point_areas()]
        self.assertEqual(values, ["db", "i", "q", "m"])
        self.assertEqual(dict(POINT_AREAS)["m"], "位存储区 M")

    def test_driver_name_constant(self) -> None:
        self.assertEqual(S7Driver.driver_name, "s7")

    def test_option_schema_defaults(self) -> None:
        fields = {field.key: field for field in S7Driver.option_schema("tcp")}
        self.assertEqual(fields["port"].default, 102)
        self.assertEqual(fields["rack"].default, 0)
        self.assertEqual(fields["slot"].default, 1, "S7-1200/1500 用插槽 1")
        self.assertTrue(S7Driver.point_area_hint())

    def test_point_area_names_accept_s7_areas(self) -> None:
        from icomm.core import csv_table

        self.assertLessEqual({"db", "i", "q", "m"}, csv_table.KNOWN_POINT_AREAS)

    def test_read_without_connection_raises(self) -> None:
        driver = S7Driver(_channel())
        with self.assertRaises(DriverError):
            driver.read_points(DeviceDef(name="d"), [_point()])

    def test_write_requires_writable_flag(self) -> None:
        driver = S7Driver(_channel())
        with self.assertRaises(DriverError):
            driver.write_point(DeviceDef(name="d"), _point(writable=False), 1.0)

    def test_size_letter_mismatch_warns_once(self) -> None:
        driver = S7Driver(_channel())
        logs: list[str] = []
        driver.set_callbacks(on_log=lambda level, message: logs.append(f"{level}:{message}"))
        point = _point(path="DB1.DBX4.0", data_type="uint16")
        addr = parse_address("DB1.DBX4.0")
        driver._check_consistency(point, addr, False)
        driver._check_consistency(point, addr, False)
        self.assertEqual(len(logs), 1, "同一个点位只提醒一次")
        self.assertIn("warning", logs[0])

    def test_consistency_accepts_matching_size(self) -> None:
        driver = S7Driver(_channel())
        logs: list[str] = []
        driver.set_callbacks(on_log=lambda level, message: logs.append(message))
        driver._check_consistency(_point(path="DB1.DBD8"), parse_address("DB1.DBD8"), False)
        self.assertEqual(logs, [])

    def test_bit_array_rejected(self) -> None:
        driver = S7Driver(_channel())
        point = _point(path="DB1.DBX0.0", data_type="bool", kind="array", count=8)
        with self.assertRaises(DriverError) as ctx:
            driver._address_of(point)
        self.assertIn("数组", str(ctx.exception))


# --------------------------------------------------------------- 端到端


@REQUIRES_LIB
class EndToEndTest(unittest.TestCase):
    """对着内置仿真 PLC 真读真写。"""

    @classmethod
    def setUpClass(cls) -> None:
        from tools.make_protocol_examples import s7_project

        cls.port = _free_port()
        cls.project = s7_project()
        cls.project.channels[0].options["port"] = cls.port
        cls.device = cls.project.devices[0]
        cls.points = list(cls.device.points)

    def setUp(self) -> None:
        from tools.s7_sim import S7Simulator

        self.sim = S7Simulator(port=self.port)
        self.sim.start()
        self.addCleanup(self.sim.stop)
        self.channel = self.project.channels[0]
        self.channel.options["port"] = self.port
        self.driver = S7Driver(self.channel)
        self.frames: list[tuple[str, str]] = []
        self.logs: list[str] = []
        self.driver.set_callbacks(
            on_frame=lambda direction, text: self.frames.append((direction, text)),
            on_log=lambda level, message: self.logs.append(f"{level}:{message}"),
        )
        self.driver.open()
        self.addCleanup(self.driver.close)

    def _read(self) -> dict:
        return self.driver.read_points(self.device, self.points)

    def _wait_until(self, predicate, note: str, timeout: float = 4.0) -> dict:
        """等仿真 PLC 刷新一拍再断言（它每 100ms 把控制字反映到状态位）。"""
        deadline = time.monotonic() + timeout
        results: dict = {}
        while time.monotonic() < deadline:
            results = self._read()
            if predicate(results):
                return results
            time.sleep(0.05)
        self.fail(f"{note}：等了 {timeout}s 仍未满足")

    def _value(self, name: str) -> object:
        return self._read()[name].value

    def _point(self, name: str) -> PointDef:
        return next(point for point in self.points if point.name == name)

    def test_connect_and_read_every_template_point(self) -> None:
        results = self._read()
        self.assertEqual(len(results), len(self.points))
        failed = {name: r.error for name, r in results.items() if not r.ok}
        self.assertEqual(failed, {})

    def test_measured_values_are_plausible(self) -> None:
        results = self._read()
        self.assertGreater(results["voltage"].value, 700.0)
        self.assertLess(results["voltage"].value, 800.0)
        self.assertGreater(results["current"].value, 0.0)
        self.assertAlmostEqual(results["frequency"].value, 50.0, places=1)
        self.assertAlmostEqual(results["temperature"].value, 42.0, delta=3.0)
        self.assertIsInstance(results["runtime"].value, int)
        self.assertEqual(results["device_code"].value, 0x21, "uint8 点位")
        self.assertAlmostEqual(results["i_ai_word"].value, results["voltage"].value, places=0)

    def test_batching_keeps_request_count_low(self) -> None:
        self._read()
        reads = [text for direction, text in self.frames if direction == "RX"]
        self.assertLessEqual(len(reads), 8, f"22 个点位不该发这么多请求：{reads}")

    def test_write_setpoint_and_read_back(self) -> None:
        point = self._point("power_setpoint")
        self.driver.write_point(self.device, point, 123.5)
        self.assertAlmostEqual(self._value("power_setpoint"), 123.5, places=2)

    def test_write_control_word_turns_status_bits_on(self) -> None:
        self.driver.write_point(self.device, self._point("control_word"), 0x0009)
        self.assertAlmostEqual(self._value("control_word"), 9)
        results = self._wait_until(
            lambda r: r["m_run"].value and r["q_main_contactor"].value,
            "控制字启动后运行位应亮",
        )
        self.assertTrue(results["m_remote"].value, "M0.3 远程方式随控制字 bit3")
        self.assertTrue(int(results["status_word"].value) & (1 << 0), "状态字 bit0 运行")
        self.assertTrue(int(results["status_word"].value) & (1 << 8), "状态字 bit8 接触器合")

    def test_low_setpoint_raises_limit_bit(self) -> None:
        self.driver.write_point(self.device, self._point("control_word"), 0x0001)
        self.driver.write_point(self.device, self._point("power_setpoint"), 10.0)
        results = self._wait_until(
            lambda r: int(r["status_word"].value) & (1 << 4), "给定值小于 50kW 应进限功率"
        )
        self.assertLess(results["power"].value, results["voltage"].value, "限功率时功率明显变小")

    def test_write_uint16_and_uint8_areas(self) -> None:
        self.driver.write_point(self.device, self._point("m_power_pct"), 66)
        self.assertEqual(self._value("m_power_pct"), 66)
        custom = _point(
            name="字节量", register="m", path="MB24", data_type="uint8", writable=True
        )
        self.driver.write_point(self.device, custom, 0x5A)
        self.assertEqual(self.driver.read_points(self.device, [custom])["字节量"].value, 0x5A)

    def test_write_float64_in_db_area(self) -> None:
        custom = _point(
            name="双精度", register="db", path="DB2.DBD8", data_type="float64", writable=True
        )
        self.driver.write_point(self.device, custom, -1234.5)
        value = self.driver.read_points(self.device, [custom])["双精度"].value
        self.assertAlmostEqual(value, -1234.5, places=6)

    def test_write_bits_in_io_and_marker_areas(self) -> None:
        """位写走"读-改-写"：M / I / Q 三个区都要能改一位而不动旁边的位。"""
        for area, prefix in (("m", "M"), ("i", "I"), ("q", "Q")):
            low = _point(
                name=f"{prefix}_b0", register=area, path=f"{prefix}20.0",
                data_type="bool", writable=True,
            )
            high = _point(
                name=f"{prefix}_b1", register=area, path=f"{prefix}20.1",
                data_type="bool", writable=True,
            )
            self.driver.write_point(self.device, low, True)
            self.driver.write_point(self.device, high, True)
            results = self.driver.read_points(self.device, [low, high])
            self.assertTrue(results[low.name].value, f"{prefix}20.0 应置位")
            self.assertTrue(results[high.name].value, f"{prefix}20.1 应置位")

            self.driver.write_point(self.device, low, False)
            results = self.driver.read_points(self.device, [low, high])
            self.assertFalse(results[low.name].value, f"{prefix}20.0 应复位")
            self.assertTrue(
                results[high.name].value, "读-改-写不该把相邻位的状态带掉"
            )
            self.driver.write_point(self.device, high, False)

    def test_bit_point_double_write_state_is_kept(self) -> None:
        point = self._point("grid_allow")
        self.driver.write_point(self.device, point, True)
        self.assertIs(self._value("grid_allow"), True)
        self.driver.write_point(self.device, point, False)
        self.assertIs(self._value("grid_allow"), False)
        self.driver.write_point(self.device, point, True)
        self.assertIs(self._value("grid_allow"), True)

    def test_bad_point_reports_error_without_breaking_others(self) -> None:
        broken = _point(name="坏点位", register="db", path="T1", data_type="float32")
        results = self.driver.read_points(self.device, [*self.points, broken])
        self.assertFalse(results["坏点位"].ok)
        self.assertIn("看不懂", results["坏点位"].error)
        self.assertTrue(all(r.ok for name, r in results.items() if name != "坏点位"))

    def test_failed_read_drops_connection_and_reopen_recovers(self) -> None:
        """读失败要主动放弃旧连接，等引擎重连 —— 现场"断开一下就连不上"的老毛病。"""

        class _Boom:
            """模拟"网线拔了"的客户端：自称还在连，但一读就断。"""

            @staticmethod
            def get_connected() -> bool:
                return True

            @staticmethod
            def _fail(*_args, **_kwargs):
                raise OSError("连接被对端关闭")

            db_read = _fail
            eb_read = _fail
            ab_read = _fail
            mb_read = _fail

            @staticmethod
            def disconnect() -> None:
                pass

        self.channel.options["retry"] = 0  # 先不让驱动自己重连，单看"失败后收摊"
        self.assertTrue(self._read()["voltage"].ok)
        self.driver._client = _Boom()
        results = self.driver.read_points(self.device, self.points)
        self.assertFalse(results["voltage"].ok, "对端没了就该读失败")
        self.assertFalse(self.driver.is_open, "读失败后要放弃旧连接，等引擎重连")

        self.driver.open()  # 引擎的 _ensure_connected 就是这么做的
        self.assertTrue(self._read()["voltage"].ok, "重连后应恢复正常采集")

    def test_retry_reconnects_by_itself(self) -> None:
        """允许重试时，一次坏读会被"重连 + 重试"救回来（现场最常见的情形）。"""

        class _OnceBoom:
            """第一次读抛异常，之后由驱动自己重连（换成真客户端）就好。"""

            def __init__(self, inner) -> None:
                self._inner = inner
                self.failed = False

            def get_connected(self) -> bool:
                return not self.failed

            def db_read(self, *args):
                self.failed = True
                self._inner._client = None
                raise OSError("连接被对端关闭")

        self.channel.options["retry"] = 1
        self.driver._client = _OnceBoom(self.driver)
        results = self.driver.read_points(self.device, self.points)
        self.assertTrue(results["voltage"].ok, "重试过程中应重连成功并读到值")
        self.assertTrue(
            any("重试" in message for message in self.logs), f"应有重试日志：{self.logs}"
        )

    def test_dead_socket_makes_is_open_false(self) -> None:
        self.assertTrue(self.driver.is_open)
        socket_object = self.driver._client.connection.socket  # noqa: SLF001 - 就是模拟拔网线
        self.sim.stop()
        socket_object.close()
        self.assertFalse(self.driver.is_open, "socket 断了 is_open 就该变 False，引擎才会重连")

    def test_frames_are_logged_for_comm_page(self) -> None:
        self._read()
        self.assertTrue(any(direction == "RX" for direction, _t in self.frames))
        self.driver.write_point(self.device, self._point("power_setpoint"), 50.0)
        self.assertTrue(any(direction == "TX" for direction, _t in self.frames))


# --------------------------------------------------------------- 点表与仿真器一致


class SimulatorLayoutTest(unittest.TestCase):
    """点表模板必须和仿真 PLC 的地址布局一致 —— 否则演示时"连上了但没数"。"""

    def test_template_matches_simulator_layout(self) -> None:
        from tools.make_protocol_examples import s7_project
        from tools.s7_sim import LAYOUT

        project = s7_project()
        points = {point.name: point for point in project.devices[0].points}
        self.assertEqual(set(points), {field.key for field in LAYOUT})
        for field in LAYOUT:
            with self.subTest(key=field.key):
                point = points[field.key]
                self.assertEqual(point.register, field.area)
                self.assertEqual(point.path, field.path)
                self.assertEqual(point.address, field.offset)
                self.assertEqual(point.data_type, field.data_type)
                self.assertEqual(point.writable, field.writable)

    def test_channel_points_at_simulator_port(self) -> None:
        from tools.make_protocol_examples import s7_project
        from tools.s7_sim import DEFAULT_PORT

        channel = s7_project().channels[0]
        self.assertEqual(channel.driver, "s7")
        self.assertEqual(channel.link, "tcp")
        self.assertEqual(channel.options["port"], DEFAULT_PORT)
        self.assertNotEqual(DEFAULT_PORT, 102, "仿真器不要去抢真机端口")


if __name__ == "__main__":
    unittest.main()
