"""Modbus 报文与 CRC 工具的测试。

重点是"用真实已知值验证"，而不是自洽：CRC 用**公认向量**钉死，
帧结构按协议手算出的期望值比对（尤其 MBAP 长度域，写错一位从站就会截断报文）。
界面部分在 offscreen 平台跑，验证"填参数 -> 出报文 -> 粘回去 -> 解析结论"整条链路。
"""

from __future__ import annotations

import os
import struct
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from icomm.core import modbus_tools as mb  # noqa: E402
from icomm.ui import qtboot, theme  # noqa: E402
from icomm.ui.tools import ModbusFrameTool, find_tool, tools_by_group  # noqa: E402
from icomm.ui.tools.modbus_frame import ModbusFrameDialog  # noqa: E402

#: 公认 CRC 向量：(数据段, 期望 CRC 数值)。
#: 前三条来自 Modbus 规范/常见文档里的完整收发例子 —— 例子本身就把帧尾两字节
#: 写出来了（C5 CD / 76 87 / 49 AD），所以能当前三条"外部真值"；
#: 后四条是自己用**两套独立算法**（逐位反射 0xA001 与 MSB 优先 + 逐位反转）
#: 互相校对出来的，数值同样钉死，防止以后改坏实现。
KNOWN_VECTORS: list[tuple[str, int]] = [
    ("01 03 00 00 00 0A", 0xCDC5),  # 帧尾 C5 CD
    ("11 03 00 6B 00 03", 0x8776),  # 帧尾 76 87
    ("11 03 06 AE 41 56 52 43 40", 0xAD49),  # 帧尾 49 AD（上一条的响应）
    ("01 04 02 FF FF", 0x80B8),  # 帧尾 B8 80
    ("01 05 00 00 FF 00", 0x3A8C),  # 帧尾 8C 3A（写单个线圈 ON）
    ("01 06 00 01 00 03", 0x0B98),  # 帧尾 98 0B（写单个寄存器）
    ("01 0F 00 00 00 0A 02 CD 01", 0x6870),  # 帧尾 70 68（写 10 个线圈）
    ("01 10 00 01 00 02 04 00 0A 01 02", 0x3092),  # 帧尾 92 30（写 2 个寄存器）
]

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])
    # 系统 locale 是 zh_HK 时 Qt 会用 CJK 数字（〡〢〇），数字框就没法看了
    qtboot.prepare_qt()


class TestCRC(unittest.TestCase):
    """CRC16：公认向量 + 传输字节序。"""

    def test_known_vectors(self):
        for text, expect in KNOWN_VECTORS:
            data = bytes.fromhex(text.replace(" ", ""))
            actual = mb.crc16(data)
            print(
                f"  [CRC] {text} -> 0x{actual:04X}"
                f"（帧尾 {mb.to_hex_text(mb.crc16_bytes(data))}）"
            )
            self.assertEqual(actual, expect, f"{text} 的 CRC 应为 0x{expect:04X}")

    def test_transport_is_little_endian(self):
        """CRC 在报文里低字节在前 —— 写反了是现场最容易犯的错。"""
        data = bytes.fromhex("01030000000A".replace(" ", ""))
        self.assertEqual(mb.crc16_bytes(data), b"\xC5\xCD")
        self.assertEqual(mb.append_crc(data), bytes.fromhex("01030000000AC5CD"))
        # 高字节在前是错的：明确断言它不成立，避免后人"顺手改回去"
        self.assertNotEqual(mb.crc16_bytes(data), b"\xCD\xC5")

    def test_empty_and_initial_value(self):
        self.assertEqual(mb.crc16(b""), 0xFFFF)

    def test_crc_over_body_plus_crc_is_zero(self):
        """经典判据：数据段 + 它的 CRC 再算一遍，结果是 0x0000。"""
        for text, _expect in KNOWN_VECTORS:
            frame = mb.append_crc(bytes.fromhex(text.replace(" ", "")))
            self.assertEqual(mb.crc16(frame), 0, f"{text} 的完整帧 CRC 应为 0")

    def test_crc_detail_lists_parameters(self):
        detail = mb.crc_detail(bytes.fromhex("01030000000A"))
        print(f"  [CRC 明细] {detail.splitlines()[0]} / {detail.splitlines()[-1]}")
        self.assertIn("0xA001", detail)
        self.assertIn("0xFFFF", detail)
        self.assertIn("0xCDC5", detail)
        self.assertIn("低字节在前", detail)


class TestFrames(unittest.TestCase):
    """拼帧与校验：长度、结构、坏帧判定。"""

    def test_build_rtu_structure(self):
        frame = mb.build_rtu(1, 0x03, mb.read_request(0x03, 0x0000, 10))
        print(f"  [RTU] {mb.to_hex_text(frame)}（{len(frame)} 字节）")
        self.assertEqual(frame, bytes.fromhex("01030000000AC5CD"))
        self.assertEqual(len(frame), 8)
        self.assertEqual(frame[0], 1)
        self.assertEqual(frame[1], 0x03)
        self.assertTrue(mb.check_crc(frame)[0])

    def test_check_crc_accepts_good_rejects_corrupted(self):
        good = mb.build_rtu(1, 0x03, mb.read_request(0x03, 0, 10))
        ok, text = mb.check_crc(good)
        self.assertTrue(ok)
        self.assertIn("CRC 正确", text)

        # 改数据段一个字节（数量 10 -> 11）
        broken = bytearray(good)
        broken[5] = 0x0B
        ok, text = mb.check_crc(bytes(broken))
        self.assertFalse(ok)
        self.assertIn("CRC 错误", text)
        self.assertIn("C5 CD", text)  # 帧内还是老 CRC
        print(f"  [坏帧-改数据] {text}")

        # 改 CRC 自身一个字节
        broken_crc = bytearray(good)
        broken_crc[-1] ^= 0x01
        ok, _text = mb.check_crc(bytes(broken_crc))
        self.assertFalse(ok)

        # CRC 高低字节写反（同一帧、字节序错）
        swapped = good[:-2] + good[-1:] + good[-2:-1]
        ok, text = mb.check_crc(swapped)
        self.assertFalse(ok)
        self.assertIn("CRC 错误", text)
        print(f"  [坏帧-字节序反] {mb.to_hex_text(swapped)} -> {text}")

    def test_check_crc_too_short_gives_reason(self):
        for frame, keyword in ((b"", "0 字节"), (b"\x01", "CRC"), (b"\x01\x03\x00", "最小 RTU 帧长")):
            ok, text = mb.check_crc(frame)
            self.assertFalse(ok)
            self.assertIn(keyword, text)
            print(f"  [短帧] {mb.to_hex_text(frame)!r} -> {text}")

    def test_build_tcp_mbap_header(self):
        frame = mb.build_tcp(0x0001, 0x11, 0x03, mb.read_request(0x03, 0x006B, 3))
        print(f"  [TCP] {mb.to_hex_text(frame)}（{len(frame)} 字节）")
        self.assertEqual(frame, bytes.fromhex("0001000000061103006B0003"))
        self.assertEqual(len(frame[:7]), 7, "MBAP 头必须是 7 字节（6 字节固定头 + 从站号）")
        self.assertEqual(frame[0:2], b"\x00\x01")
        self.assertEqual(frame[2:4], b"\x00\x00", "协议号固定 0")
        self.assertEqual(frame[4:6], b"\x00\x06")
        self.assertEqual(frame[6], 0x11)
        self.assertEqual(frame[7], 0x03)

    def test_build_tcp_length_field_is_not_truncated(self):
        """长度域 = 从站号(1) + PDU，且不能漏掉 16 位里的高位字节。"""
        for quantity in (1, 10, 100, mb.MAX_WRITE_REGISTERS):
            payload = mb.write_multiple(0x10, 0, [0x1234] * quantity)
            frame = mb.build_tcp(0x0001, 1, 0x10, payload)
            pdu_len = 1 + len(payload)  # 功能码 + 数据
            length = struct.unpack(">H", frame[4:6])[0]
            print(
                f"  [TCP 长度域] {quantity} 个寄存器：PDU {pdu_len} 字节 -> 长度域 {length}"
                f"，帧长 {len(frame)} 字节"
            )
            self.assertEqual(length, 1 + pdu_len)
            self.assertEqual(length, len(frame) - mb.MBAP_FIXED_LEN)
            self.assertEqual(len(frame), mb.MBAP_HEADER_LEN + pdu_len)
            self.assertEqual(struct.unpack(">H", frame[4:6])[0], length, "长度域不能截断成单字节")
            # 常见写错：把 6 字节固定头算进长度域
            self.assertNotEqual(length, mb.MBAP_FIXED_LEN + pdu_len)

    def test_max_legal_write_frame(self):
        """最长的合法写：123 个寄存器（PDU 252 字节）。"""
        payload = mb.write_multiple(0x10, 0, list(range(mb.MAX_WRITE_REGISTERS)))
        tcp = mb.build_tcp(1, 1, 0x10, payload)
        rtu = mb.build_rtu(1, 0x10, payload)
        print(f"  [最长帧] TCP {len(tcp)} 字节 / RTU {len(rtu)} 字节")
        self.assertEqual(struct.unpack(">H", tcp[4:6])[0], 253)
        self.assertTrue(mb.check_crc(rtu)[0])

    def test_oversized_pdu_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            mb.build_rtu(1, 0x10, bytes(253))
        self.assertIn("253", str(ctx.exception))
        with self.assertRaises(ValueError):
            mb.build_tcp(1, 1, 0x10, bytes(253))

    def test_out_of_range_units(self):
        with self.assertRaises(ValueError):
            mb.build_rtu(256, 0x03)
        with self.assertRaises(ValueError):
            mb.build_rtu(1, 0x100)
        with self.assertRaises(ValueError):
            mb.build_tcp(0x10000, 1, 0x03)


class TestRequestBuilders(unittest.TestCase):
    """功能码请求构造器：内容、边界与错误信息。"""

    def test_read_request_all_four_functions(self):
        for function in (0x01, 0x02, 0x03, 0x04):
            payload = mb.read_request(function, 0x006B, 3)
            self.assertEqual(payload, bytes.fromhex("006B0003"))
            frame = mb.build_rtu(0x11, function, payload)
            info = mb.parse_frame(frame, "rtu")
            print(f"  [读请求] {mb.to_hex_text(frame)} -> {info.summary}")
            self.assertEqual(info.address, 0x006B)
            self.assertEqual(info.quantity, 3)
            self.assertTrue(info.crc_ok)

    def test_read_request_limits(self):
        self.assertEqual(len(mb.read_request(0x01, 0, mb.MAX_READ_BITS)), 4)
        self.assertEqual(len(mb.read_request(0x03, 0, mb.MAX_READ_REGISTERS)), 4)
        for function, count, keyword in (
            (0x03, 0, "超出范围"),
            (0x03, 126, "125"),
            (0x01, 2001, "2000"),
            (0x04, -1, "超出范围"),
        ):
            with self.assertRaises(ValueError) as ctx:
                mb.read_request(function, 0, count)
            self.assertIn(keyword, str(ctx.exception))
            print(f"  [读请求边界] 0x{function:02X} count={count} -> {ctx.exception}")
        with self.assertRaises(ValueError) as ctx:
            mb.read_request(0x06, 0, 1)
        self.assertIn("不是读操作", str(ctx.exception))
        with self.assertRaises(ValueError):
            mb.read_request(0x03, 0x10000, 1)

    def test_write_single_coil_value_conversion(self):
        on = mb.write_single(0x05, 0, True)
        off = mb.write_single(0x05, 0, False)
        self.assertEqual(on, bytes.fromhex("0000FF00"), "线圈 ON 必须写 0xFF00")
        self.assertEqual(mb.write_single(0x05, 0, 1), on)
        self.assertEqual(mb.write_single(0x05, 0, "通"), on)
        self.assertEqual(mb.write_single(0x05, 0, 0xFF00), on)
        self.assertEqual(off, bytes.fromhex("00000000"), "线圈 OFF 必须写 0x0000")
        self.assertEqual(mb.write_single(0x05, 0, 0), off)
        frame_on = mb.build_rtu(1, 0x05, on)
        print(f"  [写单线圈 ON] {mb.to_hex_text(frame_on)} -> {mb.parse_frame(frame_on, 'rtu').summary}")
        with self.assertRaises(ValueError) as ctx:
            mb.write_single(0x05, 0, 2)
        self.assertIn("线圈值", str(ctx.exception))
        print(f"  [写单线圈 非法值] {ctx.exception}")

    def test_write_single_register(self):
        payload = mb.write_single(0x06, 1, 3)
        self.assertEqual(payload, bytes.fromhex("00010003"))
        frame = mb.build_rtu(1, 0x06, payload)
        info = mb.parse_frame(frame, "rtu")
        self.assertEqual(info.address, 1)
        self.assertEqual(info.values, [3])
        self.assertIn("写单个寄存器", info.summary)
        with self.assertRaises(ValueError):
            mb.write_single(0x06, 0, 0x10000)
        with self.assertRaises(ValueError) as ctx:
            mb.write_single(0x10, 0, 1)
        self.assertIn("不是写单个", str(ctx.exception))

    def test_write_multiple_coils_from_bits_string(self):
        payload = mb.write_multiple(0x0F, 0, "1011")
        self.assertEqual(payload, bytes.fromhex("0000000401") + b"\x0D")
        frame = mb.build_rtu(1, 0x0F, payload)
        info = mb.parse_frame(frame, "rtu")
        print(f"  [写多线圈] {mb.to_hex_text(frame)} -> {info.summary}")
        self.assertEqual(info.quantity, 4)
        self.assertEqual(info.values, [1, 0, 1, 1])
        self.assertEqual(info.byte_count, 1)
        # 也可以贴打包字节：1 字节 = 8 个线圈（低位在前），1+0+1+1 就是 0x0D
        packed = mb.write_multiple(0x0F, 0, b"\x0D")
        self.assertEqual(packed, struct.pack(">HHB", 0, 8, 1) + b"\x0D")

    def test_write_multiple_coils_bit_order(self):
        """10 个线圈：0xCD 0x01 -> 位序低位在前；整帧与已知向量对上。"""
        payload = struct.pack(">HHB", 0, 10, 2) + bytes.fromhex("CD01")
        frame = mb.build_rtu(1, 0x0F, payload)
        print(f"  [写多线圈 已知向量] {mb.to_hex_text(frame)}")
        self.assertEqual(mb.to_hex_text(frame), "01 0F 00 00 00 0A 02 CD 01 70 68")
        info = mb.parse_frame(frame, "rtu")
        self.assertEqual(info.quantity, 10)
        self.assertEqual(info.byte_count, 2)
        self.assertEqual(info.values, [1, 0, 1, 1, 0, 0, 1, 1, 1, 0])

    def test_write_multiple_coils_from_packed_bytes(self):
        """直接贴打包字节：每个字节 8 个线圈（低位在前）。"""
        payload = mb.write_multiple(0x0F, 0, bytes.fromhex("CD01"))
        print(f"  [写多线圈 打包字节] {mb.to_hex_text(mb.build_rtu(1, 0x0F, payload))}")
        self.assertEqual(payload, bytes.fromhex("0000001002CD01"))

    def test_write_multiple_registers(self):
        payload = mb.write_multiple(0x10, 1, [10, 258])
        self.assertEqual(payload, bytes.fromhex("0001000204000A0102"))
        frame = mb.build_rtu(1, 0x10, payload)
        info = mb.parse_frame(frame, "rtu")
        print(f"  [写多寄存器] {mb.to_hex_text(frame)} -> {info.summary}")
        self.assertEqual(info.address, 1)
        self.assertEqual(info.quantity, 2)
        self.assertEqual(info.values, [10, 258])
        self.assertEqual(info.byte_count, 4)

    def test_write_multiple_from_hex_text_list(self):
        self.assertEqual(
            mb.write_multiple(0x10, 1, "0x000A, 258"),
            mb.write_multiple(0x10, 1, [10, 258]),
        )

    def test_write_multiple_odd_byte_count_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            mb.write_multiple(0x10, 0, b"\x00\x0A\x01")
        print(f"  [写多寄存器 奇数字节] {ctx.exception}")
        self.assertIn("奇数", str(ctx.exception))

    def test_write_multiple_bounds(self):
        with self.assertRaises(ValueError) as ctx:
            mb.write_multiple(0x10, 0, [0] * (mb.MAX_WRITE_REGISTERS + 1))
        print(f"  [写多寄存器 超限] {ctx.exception}")
        self.assertIn("123", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            mb.write_multiple(0x0F, 0, "1" * (mb.MAX_WRITE_BITS + 1))
        print(f"  [写多线圈 超限] {ctx.exception}")
        self.assertIn("1968", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            mb.write_multiple(0x10, 0, [])
        self.assertIn("至少", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            mb.write_multiple(0x0F, 0, "1, 0, 2")
        self.assertIn("不是 0 或 1", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            mb.write_multiple(0x10, 0, [0, 0x10000])
        self.assertIn("16 位", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            mb.write_multiple(0x06, 0, [1])
        self.assertIn("不是写多个", str(ctx.exception))


class TestParseFrame(unittest.TestCase):
    """解析：请求 / 响应 / 写 / 异常 / 坏帧。"""

    def test_read_request(self):
        frame = mb.build_rtu(1, 0x03, mb.read_request(0x03, 0, 10))
        info = mb.parse_frame(frame, "rtu")
        print(f"  [解析读请求] {info.summary}")
        self.assertEqual(info.mode, "rtu")
        self.assertEqual((info.slave, info.unit), (1, 1))
        self.assertEqual(info.function, 0x03)
        self.assertEqual(info.function_name, "读保持寄存器")
        self.assertEqual(info.address, 0)
        self.assertEqual(info.quantity, 10)
        self.assertTrue(info.crc_ok)
        self.assertEqual(info.error, "")
        self.assertTrue(info.is_ok)

    def test_read_response(self):
        frame = bytes.fromhex("11 03 06 AE 41 56 52 43 40 49 AD".replace(" ", ""))
        info = mb.parse_frame(frame, "rtu")
        print(f"  [解析读响应] {info.summary}")
        self.assertEqual(info.values, [0xAE41, 0x5652, 0x4340])
        self.assertEqual(info.byte_count, 6)
        self.assertEqual(info.quantity, None)
        self.assertTrue(info.crc_ok)
        self.assertTrue(info.is_ok)

    def test_read_coils_response_bit_order(self):
        frame = mb.append_crc(bytes.fromhex("01010105"))
        info = mb.parse_frame(frame, "rtu")
        print(f"  [解析读线圈响应] {mb.to_hex_text(frame)} -> {info.summary}")
        self.assertEqual(info.byte_count, 1)
        self.assertEqual(info.values[:8], [1, 0, 1, 0, 0, 0, 0, 0])
        self.assertTrue(info.is_ok)

    def test_write_single_frames(self):
        coil = mb.parse_frame(mb.build_rtu(1, 0x05, mb.write_single(0x05, 0, True)), "rtu")
        print(f"  [解析写单线圈] {coil.summary}")
        self.assertEqual(coil.values, [0xFF00])
        self.assertEqual(coil.address, 0)
        self.assertIn("置位", coil.summary)

        off = mb.parse_frame(mb.build_rtu(1, 0x05, mb.write_single(0x05, 0, False)), "rtu")
        self.assertIn("复位", off.summary)

        reg = mb.parse_frame(mb.build_rtu(1, 0x06, mb.write_single(0x06, 1, 3)), "rtu")
        self.assertEqual(reg.values, [3])
        self.assertIn("写单个寄存器", reg.summary)

    def test_write_multiple_request_and_response(self):
        request = mb.parse_frame(mb.build_rtu(1, 0x10, mb.write_multiple(0x10, 1, [10, 258])), "rtu")
        print(f"  [解析写多请求] {request.summary}")
        self.assertEqual(request.quantity, 2)
        self.assertEqual(request.values, [10, 258])
        self.assertTrue(request.is_ok)

        response = mb.parse_frame(mb.build_rtu(1, 0x10, bytes.fromhex("00010002")), "rtu")
        print(f"  [解析写多响应] {response.summary}")
        self.assertEqual(response.address, 1)
        self.assertEqual(response.quantity, 2)
        self.assertEqual(response.values, [])
        self.assertIn("响应", response.summary)

        coils = mb.parse_frame(mb.build_rtu(1, 0x0F, bytes.fromhex("0001000A")), "rtu")
        self.assertEqual(coils.quantity, 10)
        self.assertIn("写多个线圈", coils.summary)

    def test_exception_response(self):
        frame = mb.build_rtu(1, 0x83, b"\x02")
        info = mb.parse_frame(frame, "rtu")
        print(f"  [解析异常响应] {info.summary}")
        self.assertTrue(info.is_exception)
        self.assertEqual(info.exception_code, 2)
        self.assertEqual(info.function_name, "读保持寄存器")
        self.assertIn("非法数据地址", info.summary)
        self.assertTrue(info.is_ok)

        unknown = mb.parse_frame(mb.build_rtu(1, 0x91, b"\x7F"), "rtu")
        self.assertIn("未在标准异常码表里", unknown.summary)

    def test_crc_error_is_reported_but_still_parsed(self):
        broken = bytearray(mb.build_rtu(1, 0x03, mb.read_request(0x03, 0, 10)))
        broken[-1] ^= 0xFF
        info = mb.parse_frame(bytes(broken), "rtu")
        print(f"  [解析坏帧] {info.summary}")
        self.assertFalse(info.crc_ok)
        self.assertFalse(info.is_ok)
        self.assertEqual(info.quantity, 10, "CRC 错了也要尽量解析出结构")
        self.assertTrue(info.summary.startswith("CRC 错误"))

    def test_short_and_empty_frames(self):
        self.assertIn("没有数据", mb.parse_frame(b"", "rtu").error)
        short = mb.parse_frame(b"\x01\x03", "rtu")
        self.assertIn("帧太短", short.error)
        print(f"  [解析短帧] {short.summary}")
        self.assertIn("帧太短", mb.parse_frame(b"\x00\x01\x00", "tcp").error)

    def test_unknown_function_is_dumped_not_failed(self):
        frame = mb.append_crc(bytes.fromhex("010800000001"))
        info = mb.parse_frame(frame, "rtu")
        self.assertEqual(info.error, "")
        self.assertTrue(info.is_ok)
        self.assertIn("诊断", info.summary)

    def test_mask_write_and_read_write(self):
        mask = mb.parse_frame(mb.append_crc(bytes.fromhex("0116000100FF00F0")), "rtu")
        print(f"  [解析屏蔽写] {mask.summary}")
        self.assertEqual(mask.address, 1)
        self.assertEqual(mask.values, [0x00FF, 0x00F0])
        self.assertIn("屏蔽写寄存器", mask.summary)

        readwrite = mb.parse_frame(
            mb.append_crc(bytes.fromhex("0117000100020003000104 000A0102".replace(" ", ""))), "rtu"
        )
        self.assertEqual(readwrite.values, [10, 258])

    def test_tcp_frame(self):
        frame = mb.build_tcp(0x1234, 0x11, 0x03, mb.read_request(0x03, 0x006B, 3))
        info = mb.parse_frame(frame, "tcp")
        print(f"  [解析 TCP] {info.summary}")
        self.assertEqual(info.mode, "tcp")
        self.assertEqual(info.transaction, 0x1234)
        self.assertEqual(info.protocol, 0)
        self.assertEqual(info.unit, 0x11)
        self.assertEqual(info.function, 0x03)
        self.assertEqual(info.address, 0x006B)
        self.assertEqual(info.quantity, 3)
        self.assertIsNone(info.crc_ok, "TCP 不带 CRC，crc_ok 应为 None")
        self.assertIn("不带 CRC", info.crc_text)
        self.assertTrue(info.is_ok)

    def test_tcp_length_mismatch_detected(self):
        frame = bytearray(mb.build_tcp(1, 1, 0x03, mb.read_request(0x03, 0, 10)))
        # 长度域故意写错（这一帧真实应为 6 = 从站号 1 字节 + PDU 5 字节）
        frame[4:6] = b"\x00\x3E"
        info = mb.parse_frame(bytes(frame), "tcp")
        print(f"  [解析 TCP 长度错] {info.error}")
        self.assertIn("长度域", info.error)
        self.assertIn("6", info.error)
        self.assertFalse(info.is_ok)

    def test_tcp_protocol_must_be_zero(self):
        frame = bytearray(mb.build_tcp(1, 1, 0x03, mb.read_request(0x03, 0, 10)))
        frame[2:4] = b"\x01\x03"  # 把 RTU 帧粘到 TCP 模式
        info = mb.parse_frame(bytes(frame), "tcp")
        self.assertIn("协议号", info.error)
        self.assertIn("RTU", info.error)

    def test_rtu_frame_pasted_as_tcp_reports_error(self):
        """粘错模式时必须给出明确线索，而不是静默给出荒谬结论。"""
        rtu = mb.build_rtu(1, 0x03, mb.read_request(0x03, 0, 10))
        info = mb.parse_frame(rtu, "tcp")
        self.assertFalse(info.is_ok)
        print(f"  [粘错模式] {info.error}")

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            mb.parse_frame(b"\x01\x03", "ascii")

    def test_detail_lines_cover_everything(self):
        info = mb.parse_frame(mb.build_rtu(1, 0x03, mb.read_request(0x03, 0, 10)), "rtu")
        text = "\n".join(info.detail_lines())
        print(f"  [明细]\n{text}")
        for keyword in ("模式", "从站号", "功能码", "地址", "数量", "CRC", "帧长"):
            self.assertIn(keyword, text)


class TestHexText(unittest.TestCase):
    """十六进制文本的宽容解析与格式化。"""

    def test_tolerated_write_styles(self):
        expect = bytes.fromhex("01030000000A")
        samples = [
            "01 03 00 00 00 0A",
            "01030000000A",
            "0x01,0x03,0x00,0x00,0x00,0x0A",
            "0x01, 0X03,0x00,0x00,0x00,0x0A",
            "01, 03, 00, 00, 00, 0A",
            "01-03-00-00-00-0A",
            "01:03:00:00:00:0a",
            "01\n03\t00 00 00 0A",
        ]
        for text in samples:
            data, reason = mb.parse_hex_text(text)
            self.assertEqual(reason, "", f"{text!r} 不该报错：{reason}")
            self.assertEqual(data, expect, f"{text!r} 解析结果不对")
            print(f"  [十六进制宽容] {text!r} -> {mb.to_hex_text(data)}")

        fullwidth, reason = mb.parse_hex_text("01\u300003 00 00 00 0A")
        self.assertEqual(reason, "")
        self.assertEqual(fullwidth, expect)

    def test_bad_input_gives_reason(self):
        for text, keyword in (
            ("", "空"),
            ("   ", "空"),
            ("010300000", "奇数"),
            ("01 0G", "非十六进制字符"),
            ("hello", "非十六进制字符"),
            ("--", "没有剩下"),
        ):
            data, reason = mb.parse_hex_text(text)
            self.assertEqual(data, b"")
            self.assertIn(keyword, reason, f"{text!r} 的说明应提到 {keyword}")
            print(f"  [十六进制坏输入] {text!r} -> {reason}")

    def test_to_hex_text_grouping(self):
        data = bytes.fromhex("01030000000AC5CD")
        self.assertEqual(mb.to_hex_text(data), "01 03 00 00 00 0A C5 CD")
        self.assertEqual(mb.to_hex_text(data, 2), "0103 0000 000A C5CD")
        self.assertEqual(mb.to_hex_text(data, 4), "01030000 000AC5CD")
        self.assertEqual(mb.to_hex_text(data, 0), "01 03 00 00 00 0A C5 CD")
        self.assertEqual(mb.to_hex_text(b""), "")

    def test_parse_int_text(self):
        self.assertEqual(mb.parse_int_text("10"), 10)
        self.assertEqual(mb.parse_int_text("0x10"), 16)
        self.assertEqual(mb.parse_int_text(" 0X1F "), 31)
        self.assertEqual(mb.parse_int_text("0b1011"), 11)
        self.assertEqual(mb.parse_int_text("-5"), -5)
        for text in ("", "abc", "0x"):
            with self.assertRaises(ValueError) as ctx:
                mb.parse_int_text(text, "地址")
            self.assertIn("地址", str(ctx.exception))
            print(f"  [整数解析坏输入] {text!r} -> {ctx.exception}")

    def test_parse_int_list(self):
        self.assertEqual(mb.parse_int_list("0x0001, 2 3"), [1, 2, 3])
        self.assertEqual(mb.parse_int_list("1；0；1"), [1, 0, 1])
        with self.assertRaises(ValueError) as ctx:
            mb.parse_int_list("1, x")
        self.assertIn("第 2 项", str(ctx.exception))


class TestTables(unittest.TestCase):
    """功能码与异常码中文表。"""

    def test_function_names(self):
        self.assertEqual(mb.function_name(0x03), "读保持寄存器")
        self.assertEqual(mb.function_name(0x83), "读保持寄存器")
        self.assertEqual(mb.function_name(0x64), "未知功能码 0x64")
        self.assertIn(0x0F, mb.FUNCTION_NAMES)
        self.assertIn(0x10, mb.FUNCTION_NAMES)

    def test_exception_codes(self):
        for code in (1, 2, 3, 4, 5, 6, 7, 8, 0x0A, 0x0B):
            self.assertTrue(mb.EXCEPTION_CODES[code])
            print(f"  [异常码] 0x{code:02X} -> {mb.EXCEPTION_CODES[code]}")
        self.assertIn("未在标准异常码表里", mb.exception_text(0x63))
        self.assertTrue(mb.is_exception_function(0x81))
        self.assertFalse(mb.is_exception_function(0x01))


class TestDialog(unittest.TestCase):
    """界面：填参数出报文、模式换算、解析结论。"""

    def setUp(self):
        self.dialog = ModbusFrameDialog()

    def tearDown(self):
        self.dialog.close()
        self.dialog.deleteLater()
        self._process()

    def _process(self):
        assert _app is not None
        _app.processEvents()

    def test_default_is_rtu_read_coils(self):
        """默认：从站 1、读线圈、起始 0、数量 10。"""
        self.assertEqual(self.dialog.mode(), "rtu")
        frame = self.dialog.frame_bytes()
        print(f"  [界面默认帧] {self.dialog.frame_hex()}")
        self.assertEqual(frame, mb.build_rtu(1, 0x01, mb.read_request(0x01, 0, 10)))
        self.assertTrue(mb.check_crc(frame)[0])
        self.assertIn("读线圈 请求", self.dialog.frame_summary.text())

    def test_read_holding_registers_frame_and_highlight(self):
        self.dialog.set_function(0x03)
        self.dialog.address_edit.setText("0")
        self.dialog.count_edit.setText("10")
        self._process()
        frame = self.dialog.frame_bytes()
        print(f"  [界面 RTU 读保持寄存器] {self.dialog.frame_hex()}")
        self.assertEqual(self.dialog.frame_hex(), "01 03 00 00 00 0A C5 CD")
        self.assertEqual(frame, bytes.fromhex("01030000000AC5CD"))
        # CRC 两字节要真的高亮出来（用主题强调色，不是硬编码颜色）
        html = self.dialog.frame_view.toHtml()
        self.assertIn(theme.ACCENT, html)
        self.assertIn("C5", self.dialog.frame_view.toPlainText())
        self.assertIn("0xCDC5", self.dialog.crc_label.text())
        self.assertIn("0xA001", self.dialog.crc_label.text())

    def test_mode_switch_converts_frame(self):
        self.dialog.set_function(0x03)
        self.dialog.address_edit.setText("0")
        self.dialog.count_edit.setText("10")
        self._process()
        rtu = self.dialog.frame_bytes()

        self.dialog.transaction_box.setValue(1)
        self.dialog.set_mode("tcp")
        self._process()
        tcp_hex = self.dialog.frame_hex()
        print(f"  [界面 RTU->TCP] {self.dialog.frame_bytes().hex(' ').upper()}")
        self.assertEqual(tcp_hex, "00 01 00 00 00 06 01 03 00 00 00 0A")
        # 同一份功能码 + 数据段：去掉 CRC 补 MBAP
        self.assertEqual(self.dialog.frame_bytes()[7:], rtu[1:-2])

        self.dialog.set_mode("rtu")
        self._process()
        self.assertEqual(self.dialog.frame_bytes(), rtu)

    def test_write_multiple_registers_from_ui(self):
        self.dialog.set_function(0x10)
        self.dialog.address_edit.setText("1")
        self.dialog.regs_edit.setText("0x000A, 258")
        self._process()
        frame = self.dialog.frame_bytes()
        print(f"  [界面 写多寄存器] {self.dialog.frame_hex()}")
        self.assertEqual(frame, mb.build_rtu(1, 0x10, bytes.fromhex("0001000204000A0102")))
        self.assertEqual(self.dialog.frame_hex(), "01 10 00 01 00 02 04 00 0A 01 02 92 30")
        self.assertTrue(mb.check_crc(frame)[0])

    def test_write_single_coil_ui(self):
        self.dialog.set_function(0x05)
        self.dialog.coil_combo.setCurrentIndex(0)  # ON
        self._process()
        print(f"  [界面 写单线圈 ON] {self.dialog.frame_hex()}")
        self.assertEqual(self.dialog.frame_hex(), "01 05 00 00 FF 00 8C 3A")
        self.assertIn("置位", self.dialog.frame_summary.text())

    def test_illegal_input_shows_hint_without_crash(self):
        self.dialog.set_function(0x03)
        self.dialog.address_edit.setText("abc")
        self._process()
        print(f"  [界面 非法输入] {self.dialog.builder_hint.text()}")
        self.assertEqual(self.dialog.frame_bytes(), b"")
        self.assertIn("起始地址", self.dialog.builder_hint.text())
        self.assertIn("参数", self.dialog.frame_summary.text())
        self.assertIn(theme.QUALITY_BAD, self.dialog.frame_summary.styleSheet())

    def test_parse_roundtrip_through_ui(self):
        self.dialog.parse_input.setPlainText("11 03 06 AE 41 56 52 43 40 49 AD")
        self._process()
        summary = self.dialog.parse_summary_text()
        detail = self.dialog.parse_detail_text()
        print(f"  [界面 解析响应] {summary}")
        self.assertIn("读保持寄存器", summary)
        self.assertIn("响应", summary)
        self.assertIn("CRC 正确", detail)
        self.assertIn("0xAE41", detail)
        self.assertIn(theme.ACCENT, self.dialog.parse_summary.styleSheet())

    def test_parse_bad_crc_through_ui(self):
        self.dialog.parse_input.setPlainText("01 03 00 00 00 0A C5 CA")
        self._process()
        summary = self.dialog.parse_summary_text()
        print(f"  [界面 坏帧] {summary}")
        self.assertIn("CRC 错误", summary)
        self.assertIn(theme.QUALITY_BAD, self.dialog.parse_summary.styleSheet())

    def test_parse_garbage_through_ui(self):
        self.dialog.parse_input.setPlainText("01 0G")
        self._process()
        self.assertIn("解析失败", self.dialog.parse_summary_text())
        self.dialog.parse_input.setPlainText("")
        self._process()
        self.assertIn("粘贴", self.dialog.parse_summary_text())

    def test_parse_exception_through_ui(self):
        self.dialog.parse_input.setPlainText(mb.to_hex_text(mb.build_rtu(1, 0x83, b"\x02")))
        self._process()
        summary = self.dialog.parse_summary_text()
        print(f"  [界面 异常响应] {summary}")
        self.assertIn("异常响应", summary)
        self.assertIn("非法数据地址", summary)

    def test_parse_tcp_through_ui(self):
        self.dialog.set_mode("tcp")
        frame = mb.build_tcp(0x0001, 0x01, 0x03, mb.read_request(0x03, 0, 10))
        self.dialog.parse_input.setPlainText(mb.to_hex_text(frame))
        self._process()
        summary = self.dialog.parse_summary_text()
        detail = self.dialog.parse_detail_text()
        print(f"  [界面 解析 TCP] {summary}")
        self.assertIn("请求", summary)
        self.assertIn("事务号", detail)
        self.assertIn("不带 CRC", detail)

    def test_fill_button_copies_frame_into_parser(self):
        self.dialog.set_function(0x06)
        self.dialog.value_edit.setText("100")
        self._process()
        self.dialog.detect_button.click()
        self._process()
        print(f"  [界面 回填解析] {self.dialog.frame_hex()} -> {self.dialog.parse_summary_text()}")
        self.assertEqual(self.dialog.parse_input.toPlainText(), self.dialog.frame_hex())
        self.assertIn("写单个寄存器", self.dialog.parse_summary_text())

    def test_tool_registration(self):
        tool = find_tool("Modbus 报文与 CRC")
        self.assertIsInstance(tool, ModbusFrameTool)
        self.assertEqual(tool.group, "Modbus")
        self.assertEqual(tool.order, 10)
        self.assertIn("Modbus", tools_by_group())
        self.assertIn(ModbusFrameTool, [type(t) for t in tools_by_group()["Modbus"]])
        built = tool.build(None)
        self.assertIsInstance(built, QDialog)
        print(f"  [工具注册] {tool}（分组 {tool.group}，顺序 {tool.order}）")
        built.close()
        built.deleteLater()
        self._process()


if __name__ == "__main__":
    unittest.main(verbosity=2)
