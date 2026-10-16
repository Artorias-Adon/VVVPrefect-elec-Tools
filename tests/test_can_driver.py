"""CAN 驱动与内置 CAN 仿真器的测试。

用 python-can 的 ``virtual`` 后端真开两条总线互发（不 mock python-can），
所以收帧线程、帧缓存、读-改-写、SDO 等应答这些环节都是真的在跑。
不需要真硬件、不弹窗，可以在无人值守下跑。
"""

from __future__ import annotations

import struct
import threading
import time
import unittest
import uuid
from unittest import mock

import can

from icomm.core.drivers import driver_names
from icomm.core.drivers.base import DriverError
from icomm.core.drivers.can import (
    CanDriver,
    create_bus,
    decode_bytes,
    encode_bytes,
    format_frame,
    parse_frame_id,
    parse_sdo_object,
)
from icomm.core.models import ChannelDef, DeviceDef, PointDef
from tools.can_sim import CanSimulator


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def make_channel(name: str, **options) -> ChannelDef:
    """虚拟总线上的 CAN 通道（收发双方用同一个总线名）。"""
    values = {
        "backend": "virtual",
        "channel": name,
        "bitrate": 500000,
        "index": 0,
        "receive_own": False,
        "timeout_ms": 500,
    }
    values.update(options)
    return ChannelDef(name="CAN测试", driver="can", link="can", options=values)


class SdoResponder:
    """测试用的最小 SDO 从站：只给一个节点应答上传 / 下载。

    这里把命令字写成字面量而不是引用驱动里的常量 —— 驱动发错了正是要被这里抓到。
    """

    #: 命令字 -> 字节数（上传应答 0x4F/0x4B/0x43，下载请求 0x2F/0x2B/0x23）
    UPLOAD = {1: 0x4F, 2: 0x4B, 4: 0x43}
    DOWNLOAD = {0x2F: 1, 0x2B: 2, 0x23: 4}

    def __init__(self, bus, node: int = 5, objects=None, writable=(), respond: bool = True) -> None:
        self.bus = bus
        self.node = node
        #: ``{(索引, 子索引): (字节数, 值)}``
        self.objects = dict(objects or {})
        #: 允许下载的对象；其余对象按只读处理，回中止码 0x06010000
        self.writable = set(writable)
        self.respond = respond
        #: 收到过的下载请求：``[(命令字, 索引, 子索引, 值)]``
        self.downloads: list[tuple[int, int, int, int]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="sdo-responder", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)

    def _serve(self) -> None:
        while not self._stop.is_set():
            message = self.bus.recv(timeout=0.05)
            if message is None or not self.respond:
                continue
            if int(message.arbitration_id) != 0x600 + self.node:
                continue
            data = bytes(message.data or b"")
            if len(data) < 4:
                continue
            command = data[0]
            index = int.from_bytes(data[1:3], "little")
            subindex = data[3]
            if command == 0x40:
                response = self._upload(index, subindex)
            elif command in self.DOWNLOAD:
                response = self._download(command, index, subindex, data)
            else:
                continue
            self.bus.send(
                can.Message(
                    arbitration_id=0x580 + self.node, is_extended_id=False, data=response
                )
            )

    def _upload(self, index: int, subindex: int) -> bytes:
        if (index, subindex) not in self.objects:
            return self._abort(index, subindex, 0x06020000)
        size, value = self.objects[(index, subindex)]
        raw = int(value) & ((1 << (size * 8)) - 1)
        return (
            bytes([self.UPLOAD[size]])
            + struct.pack("<HB", index, subindex)
            + raw.to_bytes(size, "little")
            + bytes(4 - size)
        )

    def _download(self, command: int, index: int, subindex: int, data: bytes) -> bytes:
        if (index, subindex) not in self.objects:
            return self._abort(index, subindex, 0x06020000)
        if (index, subindex) not in self.writable:
            return self._abort(index, subindex, 0x06010000)
        size = self.DOWNLOAD[command]
        self.downloads.append(
            (command, index, subindex, int.from_bytes(data[4 : 4 + size], "little", signed=True))
        )
        return bytes([0x60]) + struct.pack("<HB", index, subindex) + bytes(4)

    @staticmethod
    def _abort(index: int, subindex: int, code: int) -> bytes:
        return bytes([0x80]) + struct.pack("<HB", index, subindex) + code.to_bytes(4, "little")


# =============================================================== 参数与编解码


class SchemaAndCodecTest(unittest.TestCase):
    """不碰总线的部分：参数规格、帧号/对象解析、字节级编解码。"""

    # -------------------------------------------------- 参数规格

    def test_option_schema(self):
        schema = CanDriver.option_schema("can")
        fields = {field.key: field for field in schema}
        self.assertEqual(
            list(fields),
            ["backend", "channel", "bitrate", "index", "receive_own", "timeout_ms"],
        )

        backend = fields["backend"]
        self.assertEqual(backend.type, "choice")
        self.assertEqual(backend.default, "slcan")
        self.assertEqual(
            backend.choices,
            (
                "slcan",
                "pcan",
                "kvaser",
                "vector",
                "ixxat",
                "etas",
                "usb2can",
                "socketcan",
                "virtual",
            ),
        )
        self.assertEqual(len(backend.choice_labels), len(backend.choices))
        self.assertIn("USB-CAN 棒", backend.choice_labels[0])
        self.assertIn("PCAN", backend.choice_labels[1])
        self.assertIn("内置虚拟总线", backend.choice_labels[-1])

        self.assertEqual(fields["channel"].default, "COM3")
        self.assertEqual(fields["bitrate"].default, 500000)
        self.assertEqual(len(fields["bitrate"].choices), len(fields["bitrate"].choice_labels))
        index = fields["bitrate"].choices.index(125000)
        self.assertEqual(fields["bitrate"].choice_labels[index], "125 kbit/s")
        self.assertEqual(
            fields["bitrate"].choices,
            (10000, 20000, 50000, 100000, 125000, 250000, 500000, 800000, 1000000),
        )
        self.assertEqual(fields["index"].default, 0)
        self.assertEqual(fields["receive_own"].type, "bool")
        self.assertFalse(fields["receive_own"].default)
        self.assertEqual(fields["timeout_ms"].default, 200)
        self.assertEqual((fields["timeout_ms"].minimum, fields["timeout_ms"].maximum), (10, 5000))
        for field in schema:
            self.assertTrue(field.hint, f"{field.key} 少了 hint")

    def test_link_types_and_point_areas(self):
        self.assertEqual(CanDriver.link_types, ("can",))
        self.assertEqual(CanDriver.driver_name, "can")
        self.assertEqual(
            CanDriver.point_areas(),
            [
                ("can_rx", "监听帧（收到该帧即为值）"),
                ("can_tx", "发送帧（写值就发这一帧）"),
                ("can_sdo", "CANopen SDO 读写"),
            ],
        )
        hint = CanDriver.point_area_hint()
        for word in ("can_rx", "can_tx", "can_sdo", "0x181", "地址", "节点号"):
            self.assertIn(word, hint)

    def test_registered(self):
        self.assertIn("can", driver_names())

    # -------------------------------------------------- 帧号 / 对象解析

    def test_parse_frame_id(self):
        self.assertEqual(parse_frame_id("0x181"), (0x181, False))
        self.assertEqual(parse_frame_id("0X181"), (0x181, False))
        self.assertEqual(parse_frame_id(" 18F "), (0x18F, False))
        self.assertEqual(parse_frame_id("385"), (385, False))
        self.assertEqual(parse_frame_id("0x7FF"), (0x7FF, False))
        self.assertEqual(parse_frame_id("0x800"), (0x800, True))
        self.assertEqual(parse_frame_id("0x18FF50E5"), (0x18FF50E5, True))

    def test_parse_frame_id_errors(self):
        for text in ("", "   ", "0xZZ", "0x", "变流器测量帧", "0x181帧"):
            with self.assertRaises(DriverError, msg=text):
                parse_frame_id(text)
        with self.assertRaises(DriverError) as ctx:
            parse_frame_id("-5")
        self.assertIn("负", str(ctx.exception))
        with self.assertRaises(DriverError) as ctx:
            parse_frame_id("0x20000000")
        self.assertIn("29 位", str(ctx.exception))

    def test_parse_sdo_object(self):
        for text in ("0x6041:0", "6041:0", "0x6041.0", "6041/0", "0x6041"):
            self.assertEqual(parse_sdo_object(text), (0x6041, 0), text)
        self.assertEqual(parse_sdo_object("0x6041:3"), (0x6041, 3))
        self.assertEqual(parse_sdo_object("0x2000:0x10"), (0x2000, 0x10))

    def test_parse_sdo_object_errors(self):
        for text in ("", "   ", "0xZZ:0", "0x10000:0", "0x6041:256", "0x6041:0xZZ"):
            with self.assertRaises(DriverError, msg=text):
                parse_sdo_object(text)

    # -------------------------------------------------- 字节级编解码

    def test_decode_bytes_endianness(self):
        self.assertEqual(decode_bytes(bytes.fromhex("0102"), 0, "uint16", "big"), 0x0102)
        self.assertEqual(decode_bytes(bytes.fromhex("0102"), 0, "uint16", "little"), 0x0201)
        self.assertEqual(decode_bytes(bytes.fromhex("FFFE"), 0, "int16", "big"), -2)
        self.assertEqual(decode_bytes(bytes.fromhex("00010002"), 0, "uint32", "big"), 0x00010002)
        self.assertEqual(decode_bytes(bytes.fromhex("00010002"), 0, "uint32", "little"), 0x02000100)
        self.assertAlmostEqual(decode_bytes(struct.pack(">f", 12.5), 0, "float32", "big"), 12.5)
        self.assertAlmostEqual(decode_bytes(struct.pack("<f", 12.5), 0, "float32", "little"), 12.5)
        self.assertAlmostEqual(decode_bytes(struct.pack("<d", -3.25), 0, "float64", "little"), -3.25)
        # 字节序不认识的写法按大端处理（CAN 上最常见）
        self.assertEqual(decode_bytes(bytes.fromhex("0102"), 0, "uint16", "middle"), 0x0102)

    def test_decode_bytes_bool_and_offset(self):
        data = bytes([0x00, 0x01, 0x7F])
        self.assertFalse(decode_bytes(data, 0, "bool"))
        self.assertTrue(decode_bytes(data, 1, "bool"))
        self.assertTrue(decode_bytes(data, 2, "bool"))
        self.assertEqual(decode_bytes(bytes.fromhex("00000102"), 2, "uint16"), 0x0102)

    def test_decode_bytes_out_of_range(self):
        data = bytes(8)
        with self.assertRaises(DriverError) as ctx:
            decode_bytes(data, 7, "uint16")  # 7 + 2 > 8
        self.assertIn("越界", str(ctx.exception))
        with self.assertRaises(DriverError):
            decode_bytes(data, 4, "float64")  # 4 + 8 > 8
        with self.assertRaises(DriverError):
            decode_bytes(data, -1, "uint16")
        with self.assertRaises(DriverError) as ctx:
            decode_bytes(data, 0, "int64")
        self.assertIn("不支持的数据类型", str(ctx.exception))

    def test_encode_bytes(self):
        self.assertEqual(encode_bytes(0x0102, "uint16", "big"), bytes.fromhex("0102"))
        self.assertEqual(encode_bytes(0x0102, "uint16", "little"), bytes.fromhex("0201"))
        self.assertEqual(encode_bytes(-2, "int16", "big"), bytes.fromhex("FFFE"))
        self.assertEqual(encode_bytes(True, "bool"), b"\x01")
        self.assertEqual(encode_bytes(False, "bool"), b"\x00")
        self.assertEqual(encode_bytes(12.5, "float32"), struct.pack(">f", 12.5))
        with self.assertRaises(DriverError):
            encode_bytes(70000, "uint16", "big")  # 超出 uint16

    def test_driver_decode_applies_scale_and_offset(self):
        driver = CanDriver(make_channel("vvp-codec"))
        frame = bytes.fromhex("09C4") + bytes(6)
        scaled = PointDef(
            name="电压", register="can_rx", path="0x181", data_type="uint16", scale=0.1, offset=-10,
        )
        self.assertAlmostEqual(driver._decode(frame, 0, scaled), 250.0 - 10)
        plain = PointDef(name="计数", register="can_rx", path="0x181", data_type="uint16")
        self.assertEqual(driver._decode(frame, 0, plain), 2500)

    def test_driver_encode_keeps_other_bytes(self):
        driver = CanDriver(make_channel("vvp-codec"))
        current = PointDef(
            name="电流", register="can_tx", path="0x201", address=2, data_type="uint16", scale=0.1,
        )
        payload = bytearray(b"\x11\x22\x33\x44\x55\x66\x77\x88")
        driver._encode(12.3, current, payload)
        self.assertEqual(bytes(payload), b"\x11\x22\x00\x7B\x55\x66\x77\x88")

    def test_driver_decode_array(self):
        driver = CanDriver(make_channel("vvp-codec"))
        wave = PointDef(
            name="波形", register="can_rx", path="0x181", data_type="uint16", kind="array", count=4,
        )
        self.assertEqual(
            driver._decode(bytes.fromhex("0001000200030004"), 0, wave), [1, 2, 3, 4]
        )
        with self.assertRaises(DriverError):
            driver._decode(bytes(8), 6, wave)  # 6 + 4*2 > 8

    def test_format_frame(self):
        self.assertEqual(
            format_frame(0x181, b"\x01\x02\x03\x04\x05\x06\x07\x08"),
            "0x00000181 [8] 01 02 03 04 05 06 07 08",
        )
        self.assertEqual(format_frame(0x2, b""), "0x00000002 [0] ")

    def test_create_bus_without_python_can(self):
        with mock.patch("icomm.core.drivers.can.can", None):
            with self.assertRaises(DriverError) as ctx:
                create_bus(interface="virtual", channel="vvp")
        self.assertIn("python-can", str(ctx.exception))


# =============================================================== 总线收发


class CanBusTestBase(unittest.TestCase):
    """每个用例一条独立的虚拟总线，用例之间不会互相看到帧。"""

    def setUp(self) -> None:
        self.channel_name = f"vvp-{uuid.uuid4().hex[:8]}"
        self.peer = can.Bus(interface="virtual", channel=self.channel_name)
        self.drivers: list[CanDriver] = []
        self.responders: list[SdoResponder] = []
        self.simulators: list[CanSimulator] = []

    def tearDown(self) -> None:
        for responder in self.responders:
            responder.stop()
        for simulator in self.simulators:
            simulator.stop()
        for driver in self.drivers:
            driver.close()
        self.peer.shutdown()

    # -------------------------------------------------- 夹具

    @staticmethod
    def device(unit_id: int = 5) -> DeviceDef:
        return DeviceDef(name="储能变流器", channel="CAN测试", unit_id=unit_id)

    def make_driver(self, **options) -> CanDriver:
        """只建驱动、不开总线（测参数与异常路径用）。"""
        return CanDriver(make_channel(self.channel_name, **options))

    def open_driver(self, **options) -> CanDriver:
        driver = self.make_driver(**options)
        driver.open()
        self.drivers.append(driver)
        return driver

    def start_responder(self, **kwargs) -> SdoResponder:
        responder = SdoResponder(self.peer, **kwargs)
        responder.start()
        self.responders.append(responder)
        return responder

    # -------------------------------------------------- 读帧辅助

    def read_one(self, driver: CanDriver, point: PointDef, device: DeviceDef | None = None):
        results = driver.read_points(device or self.device(), [point])
        return results[point.name]

    def read_until_ok(
        self, driver: CanDriver, points: list[PointDef], device: DeviceDef | None = None,
        timeout: float = 5.0,
    ) -> dict:
        """反复读直到全部 ok —— 帧还没到时不算错，等下一拍就是了。"""
        device = device or self.device()
        deadline = time.time() + timeout
        results = driver.read_points(device, points)
        while time.time() < deadline and not all(item.ok for item in results.values()):
            time.sleep(0.02)
            results = driver.read_points(device, points)
        return results

    def recv_id(self, can_id: int, timeout: float = 3.0):
        """从对端总线收一帧指定帧号（跳过别的帧）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = self.peer.recv(timeout=0.1)
            if message is not None and int(message.arbitration_id) == can_id:
                return message
        self.fail(f"{timeout}s 内没收到帧 0x{can_id:03X}")


class CanBusTest(CanBusTestBase):
    """驱动本身的总线行为（真收发）。"""

    # -------------------------------------------------- can_rx

    def test_read_can_rx_frame(self):
        driver = self.open_driver()
        points = [
            PointDef(name="母线电压", register="can_rx", path="0x181", address=0,
                     data_type="uint16", scale=0.1),
            PointDef(name="输出电流", register="can_rx", path="0x181", address=2,
                     data_type="uint16", scale=0.1),
            PointDef(name="散热器温度", register="can_rx", path="0x181", address=6,
                     data_type="int16", scale=0.1),
        ]
        self.peer.send(
            can.Message(
                arbitration_id=0x181,
                is_extended_id=False,
                data=struct.pack(">HHHh", 7003, 152, 106, -235),
            )
        )
        results = self.read_until_ok(driver, points)
        for name, result in results.items():
            self.assertTrue(result.ok, f"{name}: {result.error}")
        self.assertAlmostEqual(results["母线电压"].value, 700.3)
        self.assertAlmostEqual(results["输出电流"].value, 15.2)
        self.assertAlmostEqual(results["散热器温度"].value, -23.5)

    def test_read_frame_not_received_yet(self):
        driver = self.open_driver()
        missing = PointDef(name="没发过的帧", register="can_rx", path="0x321", address=0)
        result = self.read_one(driver, missing)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "还没收到该帧")
        self.assertIsNone(result.value)

    def test_extended_frame_id(self):
        driver = self.open_driver()
        point = PointDef(name="扩展帧", register="can_rx", path="0x18FF50E5", address=0)
        self.peer.send(
            can.Message(arbitration_id=0x18FF50E5, is_extended_id=True, data=struct.pack(">H", 42) + bytes(6))
        )
        result = self.read_until_ok(driver, [point])[point.name]
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value, 42)

    def test_bad_points_are_marked_not_raised(self):
        driver = self.open_driver()
        wrong_area = PointDef(name="写错的区", register="holding", path="0x181")
        wrong_path = PointDef(name="写错的帧号", register="can_rx", path="变流器测量帧")
        results = driver.read_points(self.device(), [wrong_area, wrong_path])
        self.assertFalse(results["写错的区"].ok)
        self.assertEqual(results["写错的区"].error, "CAN 点位区只能是 can_rx / can_tx / can_sdo")
        self.assertFalse(results["写错的帧号"].ok)
        self.assertIn("帧号写法不对", results["写错的帧号"].error)

    def test_one_bad_point_does_not_affect_others(self):
        driver = self.open_driver()
        self.peer.send(
            can.Message(
                arbitration_id=0x181, is_extended_id=False,
                data=struct.pack(">HHHh", 7003, 152, 106, -235),
            )
        )
        good = PointDef(name="母线电压", register="can_rx", path="0x181", address=0,
                        data_type="uint16", scale=0.1)
        broken = PointDef(name="越界的温度", register="can_rx", path="0x181", address=8,
                          data_type="int16")
        results = self.read_until_ok(driver, [good], timeout=3.0)
        self.assertTrue(results["母线电压"].ok, results["母线电压"].error)

        results = driver.read_points(self.device(), [broken, good])
        self.assertFalse(results["越界的温度"].ok)
        self.assertIn("越界", results["越界的温度"].error)
        self.assertTrue(results["母线电压"].ok, "坏点位把好点位带崩了")

    def test_array_point(self):
        driver = self.open_driver()
        self.peer.send(
            can.Message(arbitration_id=0x181, is_extended_id=False, data=struct.pack(">HHHH", 1, 2, 3, 4))
        )
        wave = PointDef(name="波形", register="can_rx", path="0x181", address=0,
                        data_type="uint16", kind="array", count=4)
        result = self.read_until_ok(driver, [wave])[wave.name]
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value, [1, 2, 3, 4])

    def test_array_point_out_of_frame(self):
        driver = self.open_driver()
        self.peer.send(
            can.Message(arbitration_id=0x181, is_extended_id=False, data=bytes(8))
        )
        wave = PointDef(name="越界波形", register="can_rx", path="0x181", address=6,
                        data_type="uint16", kind="array", count=2)
        result = self.read_until_ok(driver, [wave], timeout=1.0)[wave.name]
        self.assertFalse(result.ok)
        self.assertIn("数组点位", result.error)

    def test_emit_frame_reports_rx_and_tx(self):
        frames: list[tuple[str, str]] = []
        driver = self.open_driver()
        driver.set_callbacks(on_frame=lambda direction, text: frames.append((direction, text)))
        self.peer.send(
            can.Message(arbitration_id=0x181, is_extended_id=False, data=bytes(range(1, 9)))
        )
        self.assertTrue(wait_for(lambda: any(item[0] == "RX" for item in frames)))
        self.assertIn(("RX", "0x00000181 [8] 01 02 03 04 05 06 07 08"), frames)

        setpoint = PointDef(name="电压给定", register="can_tx", path="0x201", address=0,
                            data_type="uint16", scale=0.1)
        driver.write_point(self.device(), setpoint, 250.0)
        self.assertIn(("TX", "0x00000201 [8] 09 C4 00 00 00 00 00 00"), frames)

    # -------------------------------------------------- can_tx

    def test_write_can_tx_is_read_modify_write(self):
        driver = self.open_driver()
        setpoint = PointDef(name="电压给定", register="can_tx", path="0x201", address=0,
                            data_type="uint16", scale=0.1, writable=True)
        # 先让驱动见过这一帧：对端发一帧带标志字节的旧帧，别的字节就该一直留着
        self.peer.send(
            can.Message(
                arbitration_id=0x201, is_extended_id=False,
                data=bytes([0x11, 0x22, 0xFF, 0xFF, 0x55, 0x66, 0x77, 0x88]),
            )
        )
        self.assertTrue(self.read_until_ok(driver, [setpoint])[setpoint.name].ok)

        driver.write_point(self.device(), setpoint, 250.0)
        message = self.recv_id(0x201)
        self.assertEqual(
            bytes(message.data), bytes([0x09, 0xC4, 0xFF, 0xFF, 0x55, 0x66, 0x77, 0x88])
        )
        self.assertFalse(message.is_extended_id)

    def test_write_can_tx_without_base_frame(self):
        driver = self.open_driver()
        control = PointDef(name="控制字", register="can_tx", path="0x201", address=2,
                           data_type="uint16", writable=True)
        driver.write_point(self.device(), control, 0x0002)
        message = self.recv_id(0x201)
        self.assertEqual(bytes(message.data), bytes([0, 0, 0, 2, 0, 0, 0, 0]))
        self.assertEqual(len(message.data), 8, "没有底帧时应该凑满 8 字节")

    def test_write_can_rx_is_rejected(self):
        driver = self.open_driver()
        listener = PointDef(name="监听点", register="can_rx", path="0x181", writable=True)
        with self.assertRaises(DriverError) as ctx:
            driver.write_point(self.device(), listener, 1)
        self.assertIn("只读", str(ctx.exception))

    def test_write_bad_area_type_and_offset(self):
        driver = self.open_driver()
        device = self.device()
        with self.assertRaises(DriverError) as ctx:
            driver.write_point(device, PointDef(name="x", register="holding", path="0x181"), 1)
        self.assertIn("can_rx / can_tx / can_sdo", str(ctx.exception))
        with self.assertRaises(DriverError) as ctx:
            driver.write_point(
                device, PointDef(name="x", register="can_tx", path="0x181", data_type="int64"), 1
            )
        self.assertIn("不支持的数据类型", str(ctx.exception))
        wide = PointDef(name="越界", register="can_tx", path="0x201", address=7, data_type="uint16")
        with self.assertRaises(DriverError) as ctx:
            driver.write_point(device, wide, 1)
        self.assertIn("8 字节", str(ctx.exception))

    def test_not_open_raises(self):
        driver = self.make_driver()
        self.assertFalse(driver.is_open)
        with self.assertRaises(DriverError):
            driver.read_points(self.device(), [PointDef(name="x", register="can_rx", path="0x181")])
        with self.assertRaises(DriverError):
            driver.write_point(
                self.device(), PointDef(name="x", register="can_tx", path="0x181"), 1
            )

    def test_open_without_python_can(self):
        driver = self.make_driver()
        with mock.patch("icomm.core.drivers.can.can", None):
            with self.assertRaises(DriverError) as ctx:
                driver.open()
        self.assertIn("python-can", str(ctx.exception))
        self.assertFalse(driver.is_open)

    # -------------------------------------------------- CANopen SDO

    def test_sdo_upload(self):
        driver = self.open_driver()
        self.start_responder(node=5, objects={(0x6041, 0): (2, 0x0237)})
        status = PointDef(name="状态字", register="can_sdo", path="0x6041:0", data_type="uint16")
        result = self.read_one(driver, status)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value, 0x0237)

    def test_sdo_upload_with_scale_and_int16(self):
        driver = self.open_driver()
        self.start_responder(objects={(0x6042, 0): (2, -123)})
        current = PointDef(name="电流给定", register="can_sdo", path="0x6042:0", data_type="int16",
                           scale=0.1)
        result = self.read_one(driver, current)
        self.assertTrue(result.ok, result.error)
        self.assertAlmostEqual(result.value, -12.3)

    def test_sdo_upload_float32(self):
        driver = self.open_driver()
        raw = struct.unpack("<I", struct.pack("<f", 712.5))[0]
        self.start_responder(objects={(0x2001, 0): (4, raw)})
        voltage = PointDef(name="SDO 电压", register="can_sdo", path="0x2001:0", data_type="float32")
        result = self.read_one(driver, voltage)
        self.assertTrue(result.ok, result.error)
        self.assertAlmostEqual(result.value, 712.5, places=3)

    def test_sdo_upload_missing_object_aborts(self):
        driver = self.open_driver()
        self.start_responder(objects={})
        status = PointDef(name="不存在的对象", register="can_sdo", path="0x6041:0", data_type="uint16")
        result = self.read_one(driver, status)
        self.assertFalse(result.ok)
        self.assertIn("06020000", result.error)

    def test_sdo_timeout(self):
        driver = self.open_driver(timeout_ms=150)
        self.start_responder(objects={(0x6041, 0): (2, 1)}, respond=False)
        status = PointDef(name="没人应答", register="can_sdo", path="0x6041:0", data_type="uint16")
        started = time.time()
        result = self.read_one(driver, status)
        self.assertFalse(result.ok)
        self.assertIn("超时", result.error)
        self.assertLess(time.time() - started, 3.0)

    def test_sdo_bad_object_path(self):
        driver = self.open_driver()
        broken = PointDef(name="写错的对象", register="can_sdo", path="0xZZ:0", data_type="uint16")
        result = self.read_one(driver, broken)
        self.assertFalse(result.ok)
        self.assertIn("对象写法不对", result.error)

    def test_sdo_node_out_of_range(self):
        driver = self.open_driver()
        self.start_responder(objects={(0x6041, 0): (2, 1)})
        status = PointDef(name="状态字", register="can_sdo", path="0x6041:0", data_type="uint16")
        results = driver.read_points(self.device(unit_id=200), [status])
        self.assertFalse(results["状态字"].ok)
        self.assertIn("0..127", results["状态字"].error)
        with self.assertRaises(DriverError) as ctx:
            driver.write_point(self.device(unit_id=200), status, 1)
        self.assertIn("0..127", str(ctx.exception))

    def test_sdo_download(self):
        driver = self.open_driver()
        responder = self.start_responder(
            objects={(0x6042, 0): (2, 0)}, writable=[(0x6042, 0)]
        )
        current = PointDef(name="电流给定", register="can_sdo", path="0x6042:0", data_type="int16",
                           scale=0.1, writable=True)
        driver.write_point(self.device(), current, 12.3)
        self.assertTrue(wait_for(lambda: bool(responder.downloads)))
        self.assertEqual(responder.downloads[-1], (0x2B, 0x6042, 0, 123))

    def test_sdo_download_one_byte(self):
        driver = self.open_driver()
        responder = self.start_responder(objects={(0x2000, 0): (1, 0)}, writable=[(0x2000, 0)])
        switch = PointDef(name="使能", register="can_sdo", path="0x2000:0", data_type="bool",
                          writable=True)
        driver.write_point(self.device(), switch, True)
        self.assertTrue(wait_for(lambda: bool(responder.downloads)))
        self.assertEqual(responder.downloads[-1], (0x2F, 0x2000, 0, 1))

    def test_sdo_download_read_only_aborts(self):
        driver = self.open_driver()
        self.start_responder(objects={(0x1000, 0): (4, 0x00020192)})
        device_type = PointDef(name="设备类型", register="can_sdo", path="0x1000:0",
                              data_type="uint32", writable=True)
        with self.assertRaises(DriverError) as ctx:
            driver.write_point(self.device(), device_type, 1)
        self.assertIn("06010000", str(ctx.exception))

    def test_sdo_download_eight_byte_type_is_rejected(self):
        driver = self.open_driver()
        self.start_responder(objects={(0x2002, 0): (4, 0)}, writable=[(0x2002, 0)])
        wide = PointDef(name="双精度", register="can_sdo", path="0x2002:0", data_type="float64",
                        writable=True)
        with self.assertRaises(DriverError) as ctx:
            driver.write_point(self.device(), wide, 1.5)
        self.assertIn("4 字节", str(ctx.exception))


# =============================================================== 内置仿真器


class SimulatorTest(CanBusTestBase):
    """仿真器端到端：上位机用真驱动、真总线去读它。"""

    def start_sim(self, node_ids=(5,)) -> CanSimulator:
        simulator = CanSimulator(
            interface="virtual", channel=self.channel_name, node_ids=node_ids
        )
        simulator.start()
        self.simulators.append(simulator)
        return simulator

    @staticmethod
    def measurement_points() -> list[PointDef]:
        return [
            PointDef(name="母线电压", register="can_rx", path="0x181", address=0,
                     data_type="uint16", scale=0.1),
            PointDef(name="输出电流", register="can_rx", path="0x181", address=2,
                     data_type="uint16", scale=0.1),
            PointDef(name="输出功率", register="can_rx", path="0x181", address=4,
                     data_type="uint16", scale=0.1),
            PointDef(name="散热器温度", register="can_rx", path="0x181", address=6,
                     data_type="int16", scale=0.1),
            PointDef(name="运行", register="can_rx", path="0x182", address=0, data_type="bool"),
            PointDef(name="交流频率", register="can_rx", path="0x182", address=2,
                     data_type="uint16", scale=0.01),
            PointDef(name="运行时间", register="can_rx", path="0x182", address=4,
                     data_type="uint32"),
        ]

    def test_running_flag(self):
        simulator = CanSimulator(channel=self.channel_name)
        self.assertFalse(simulator.running)
        simulator.start()
        self.simulators.append(simulator)
        self.assertTrue(simulator.running)
        simulator.stop()
        self.assertFalse(simulator.running)

    def test_read_measurements_end_to_end(self):
        simulator = self.start_sim()
        driver = self.open_driver()
        points = self.measurement_points()
        results = self.read_until_ok(driver, points, timeout=8.0)
        for name, result in results.items():
            self.assertTrue(result.ok, f"{name}: {result.error}")

        self.assertAlmostEqual(results["母线电压"].value, simulator.setpoint_v, delta=20.0)
        self.assertGreater(results["输出电流"].value, 5.0)
        self.assertGreater(results["输出功率"].value, 1.0)
        self.assertGreater(results["散热器温度"].value, 20.0)
        self.assertTrue(results["运行"].value, "仿真器上电即运行，状态字 bit0 该是 1")
        self.assertAlmostEqual(results["交流频率"].value, 50.0, delta=0.2)
        self.assertGreater(results["输出功率"].value * 1000.0 / results["输出电流"].value,
                           results["母线电压"].value * 0.5)

        # 值要真的在动，不是定住的一帧
        first = results["母线电压"].value
        self.assertTrue(
            wait_for(
                lambda: self.read_one(driver, points[6]).value
                and self.read_one(driver, points[6]).value != results["运行时间"].value,
                timeout=5.0,
            ),
            "运行时间不再累加，说明仿真器只发了一帧",
        )
        self.assertIsInstance(first, float)

    def test_sdo_read_and_write_end_to_end(self):
        simulator = self.start_sim()
        driver = self.open_driver()
        device = self.device()
        device_type = PointDef(name="设备类型", register="can_sdo", path="0x1000:0",
                              data_type="uint32")
        result = self.read_one(driver, device_type, device)
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.value, 0x00020192)

        setpoint = PointDef(name="SDO 设定值", register="can_sdo", path="0x2000:0",
                            data_type="uint16", scale=0.1, writable=True)
        current_setpoint = PointDef(name="SDO 电流给定", register="can_sdo", path="0x6042:0",
                                   data_type="int16", scale=0.1, writable=True)
        driver.write_point(device, setpoint, 500.0)
        driver.write_point(device, current_setpoint, 30.0)
        self.assertTrue(wait_for(lambda: simulator.setpoint_v == 500.0))
        self.assertTrue(wait_for(lambda: simulator.current_setpoint_a == 30.0))

        read_back = self.read_one(driver, setpoint, device)
        self.assertTrue(read_back.ok, read_back.error)
        self.assertAlmostEqual(read_back.value, 500.0)

        # 只读对象被写要回中止码
        read_only = PointDef(name="写只读对象", register="can_sdo", path="0x1000:0",
                             data_type="uint32", writable=True)
        with self.assertRaises(DriverError) as ctx:
            driver.write_point(device, read_only, 1)
        self.assertIn("06010000", str(ctx.exception))

        # 没有的对象回 0x06020000
        missing = PointDef(name="不存在的对象", register="can_sdo", path="0x1234:0",
                           data_type="uint16")
        result = self.read_one(driver, missing, device)
        self.assertFalse(result.ok)
        self.assertIn("06020000", result.error)

    def test_command_frame_controls_simulator(self):
        simulator = self.start_sim()
        driver = self.open_driver()
        device = self.device()
        setpoint = PointDef(name="电压给定", register="can_tx", path="0x201", address=0,
                            data_type="uint16", scale=0.1, writable=True)
        control = PointDef(name="控制字", register="can_tx", path="0x201", address=2,
                           data_type="uint16", writable=True)
        voltage = PointDef(name="母线电压", register="can_rx", path="0x181", address=0,
                           data_type="uint16", scale=0.1)
        running = PointDef(name="运行", register="can_rx", path="0x182", address=0,
                           data_type="bool")

        driver.write_point(device, setpoint, 400.0)
        self.assertTrue(wait_for(lambda: simulator.setpoint_v == 400.0), "仿真器没收到电压给定")
        # 一阶惯性：给定降下来以后母线电压要跟着往下走
        self.assertTrue(
            wait_for(lambda: (self.read_one(driver, voltage).value or 9999) < 600.0, timeout=8.0),
            "写了给定以后母线电压没跟着变",
        )

        driver.write_point(device, control, 0x0002)  # bit1 停止
        self.assertTrue(wait_for(lambda: simulator.output_on is False), "仿真器没收到停止命令")
        self.assertTrue(
            wait_for(lambda: self.read_one(driver, running).value is False, timeout=5.0),
            "停机后状态字的运行位还是 1",
        )


if __name__ == "__main__":
    unittest.main()
