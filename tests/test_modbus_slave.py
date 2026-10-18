"""Modbus 从机调试：从机侧应答逻辑、整帧封包、真跑一个 TCP 从机，以及那个界面面板。

盯住的是"当从机时最容易错的地方"：

* 异常码选得对不对（01 不支持功能 / 02 地址越界 / 03 数量或字节数不合法）；
* 从站号不匹配、广播、CRC 错、协议号非 0 —— 该**不应答**的时候一字节都不发；
* 写操作的回显与数据区真的被改了；
* 生成的应答帧能被自家的解析器读通（RTU 与 TCP 都要）；
* 本地从机服务真的能收发，界面能停下、不留后台端口。
"""

from __future__ import annotations

import os
import socket
import struct
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from icomm.core import modbus_slave as ms
from icomm.core import modbus_tools as mb

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def model(size: int = 20, *, writable: bool = True, fill: str = "increment") -> ms.SlaveModel:
    unit = ms.SlaveModel.uniform(unit_id=1, size=size, fill=fill)
    unit.writable = writable
    return unit


def pdu(*parts: int | bytes) -> bytes:
    body = b""
    for part in parts:
        body += part if isinstance(part, bytes) else bytes([part])
    return body


# --------------------------------------------------------------- 读


class ReadTest(unittest.TestCase):
    def test_read_holding_registers(self) -> None:
        unit = model()
        reply = ms.handle_pdu(pdu(0x03, struct.pack(">HH", 0, 4)), unit)
        self.assertIsNone(reply.exception)
        self.assertEqual(reply.pdu, pdu(0x03, 8, struct.pack(">HHHH", 0, 1, 2, 3)))
        self.assertEqual(reply.values, [0, 1, 2, 3])
        self.assertEqual(reply.quantity, 4)

    def test_read_coils_bit_packing(self) -> None:
        unit = model(fill="zero")
        unit.coils[0] = 1
        unit.coils[3] = 1
        reply = ms.handle_pdu(pdu(0x01, struct.pack(">HH", 0, 8)), unit)
        self.assertEqual(reply.pdu, pdu(0x01, 1, 0b00001001), "低位在前，bit0 与 bit3 置 1")

    def test_read_input_registers(self) -> None:
        unit = model()
        unit.input[2] = 0xABCD
        reply = ms.handle_pdu(pdu(0x04, struct.pack(">HH", 2, 1)), unit)
        self.assertEqual(reply.values, [0xABCD])

    def test_read_discrete_inputs(self) -> None:
        unit = model(fill="zero")
        unit.discrete[1] = 1
        reply = ms.handle_pdu(pdu(0x02, struct.pack(">HH", 0, 4)), unit)
        self.assertEqual(reply.pdu, pdu(0x02, 1, 0b0010))

    def test_address_out_of_range(self) -> None:
        reply = ms.handle_pdu(pdu(0x03, struct.pack(">HH", 18, 5)), model(size=20))
        self.assertEqual(reply.exception, ms.ILLEGAL_DATA_ADDRESS)
        self.assertEqual(reply.pdu, pdu(0x83, 0x02))
        self.assertIn("超出", reply.summary)

    def test_quantity_zero_or_too_big(self) -> None:
        for quantity in (0, 126):
            with self.subTest(quantity=quantity):
                reply = ms.handle_pdu(pdu(0x03, struct.pack(">HH", 0, quantity)), model())
                self.assertEqual(reply.exception, ms.ILLEGAL_DATA_VALUE)
                self.assertEqual(reply.pdu, pdu(0x83, 0x03))

    def test_short_read_request(self) -> None:
        reply = ms.handle_pdu(pdu(0x03, b"\x00"), model())
        self.assertEqual(reply.exception, ms.ILLEGAL_DATA_VALUE)

    def test_unsupported_function(self) -> None:
        reply = ms.handle_pdu(pdu(0x2B, b"\x0e\x01\x00"), model())
        self.assertEqual(reply.exception, ms.ILLEGAL_FUNCTION)
        self.assertEqual(reply.function, 0xAB, "异常应答功能码最高位置 1")
        self.assertIn("不支持的功能码", reply.summary)

    def test_empty_pdu(self) -> None:
        reply = ms.handle_pdu(b"", model())
        self.assertIn("空帧", reply.summary)
        self.assertEqual(reply.pdu, b"")


# --------------------------------------------------------------- 写


class WriteTest(unittest.TestCase):
    def test_write_single_coil(self) -> None:
        unit = model()
        reply = ms.handle_pdu(pdu(0x05, struct.pack(">HH", 3, 0xFF00)), unit)
        self.assertIsNone(reply.exception)
        self.assertEqual(unit.coils[3], 1)
        self.assertTrue(reply.written)
        self.assertEqual(reply.pdu, pdu(0x05, struct.pack(">HH", 3, 0xFF00)), "标准要求原样回显")

        ms.handle_pdu(pdu(0x05, struct.pack(">HH", 3, 0x0000)), unit)
        self.assertEqual(unit.coils[3], 0)

    def test_write_single_coil_bad_value(self) -> None:
        reply = ms.handle_pdu(pdu(0x05, struct.pack(">HH", 3, 0x0001)), model())
        self.assertEqual(reply.exception, ms.ILLEGAL_DATA_VALUE)
        self.assertIn("0xFF00", reply.summary)

    def test_write_single_register(self) -> None:
        unit = model()
        reply = ms.handle_pdu(pdu(0x06, struct.pack(">HH", 5, 0x1234)), unit)
        self.assertEqual(unit.holding[5], 0x1234)
        self.assertEqual(reply.pdu, pdu(0x06, struct.pack(">HH", 5, 0x1234)))

    def test_write_multiple_coils(self) -> None:
        unit = model(fill="zero")
        data = struct.pack(">HHB", 2, 10, 2) + bytes([0b00000101, 0b00000010])
        reply = ms.handle_pdu(pdu(0x0F) + data, unit)
        self.assertIsNone(reply.exception)
        self.assertEqual(unit.coils[2:12], [1, 0, 1, 0, 0, 0, 0, 0, 0, 1])
        self.assertEqual(reply.pdu, pdu(0x0F, struct.pack(">HH", 2, 10)))

    def test_write_multiple_coils_byte_count_mismatch(self) -> None:
        data = struct.pack(">HHB", 0, 10, 1) + bytes([0xFF])
        reply = ms.handle_pdu(pdu(0x0F) + data, model())
        self.assertEqual(reply.exception, ms.ILLEGAL_DATA_VALUE)
        self.assertIn("字节数", reply.summary)

    def test_write_multiple_registers(self) -> None:
        unit = model()
        data = struct.pack(">HHB", 4, 2, 4) + struct.pack(">HH", 111, 222)
        reply = ms.handle_pdu(pdu(0x10) + data, unit)
        self.assertEqual(unit.holding[4:6], [111, 222])
        self.assertEqual(reply.pdu, pdu(0x10, struct.pack(">HH", 4, 2)))

    def test_write_multiple_registers_bounds(self) -> None:
        data = struct.pack(">HHB", 19, 2, 4) + struct.pack(">HH", 1, 2)
        reply = ms.handle_pdu(pdu(0x10) + data, model(size=20))
        self.assertEqual(reply.exception, ms.ILLEGAL_DATA_ADDRESS)

    def test_write_multiple_registers_incomplete_body(self) -> None:
        data = struct.pack(">HHB", 0, 2, 4) + struct.pack(">H", 1)
        reply = ms.handle_pdu(pdu(0x10) + data, model())
        self.assertEqual(reply.exception, ms.ILLEGAL_DATA_VALUE)

    def test_read_only_model_rejects_writes(self) -> None:
        unit = model(writable=False)
        for frame in (
            pdu(0x06, struct.pack(">HH", 0, 1)),
            pdu(0x05, struct.pack(">HH", 0, 0xFF00)),
            pdu(0x10) + struct.pack(">HHB", 0, 1, 2) + struct.pack(">H", 1),
        ):
            with self.subTest(function=frame[0]):
                reply = ms.handle_pdu(frame, unit)
                self.assertEqual(reply.exception, ms.ILLEGAL_DATA_ADDRESS)
                self.assertIn("只读", reply.summary)

    def test_mask_write(self) -> None:
        unit = model()
        unit.holding[0] = 0b1100
        # (current AND and_mask) OR (or_mask AND NOT and_mask)
        reply = ms.handle_pdu(pdu(0x16, struct.pack(">HHH", 0, 0b1010, 0b0101)), unit)
        self.assertEqual(unit.holding[0], 0b1101, "0x1100&0x1010=0x1000，再或上 0x0101 → 0x1101")
        self.assertEqual(reply.pdu, pdu(0x16, struct.pack(">HHH", 0, 0b1010, 0b0101)))

    def test_read_write_multiple(self) -> None:
        unit = model()
        data = struct.pack(">HHHHB", 0, 2, 10, 2, 4) + struct.pack(">HH", 777, 888)
        reply = ms.handle_pdu(pdu(0x17) + data, unit)
        self.assertIsNone(reply.exception)
        self.assertEqual(unit.holding[10:12], [777, 888])
        self.assertEqual(reply.values, [0, 1], "读的是读地址那段（这里还没被写）")


# --------------------------------------------------------------- 整帧


class RespondFrameTest(unittest.TestCase):
    def test_rtu_request_and_reply_roundtrip(self) -> None:
        unit = model()
        request = mb.build_rtu(1, 0x03, mb.read_request(0x03, 0, 4))
        result = ms.respond_frame(request, "rtu", unit)
        self.assertTrue(result.ok)
        self.assertEqual(result.unit, 1)
        self.assertEqual(result.exception, None)
        parsed = mb.parse_frame(result.reply, "rtu")
        self.assertTrue(parsed.is_ok)
        self.assertEqual(parsed.values, [0, 1, 2, 3])

    def test_bad_crc_gets_no_reply(self) -> None:
        request = bytearray(mb.build_rtu(1, 0x03, mb.read_request(0x03, 0, 1)))
        request[-1] ^= 0xFF
        result = ms.respond_frame(bytes(request), "rtu", model())
        self.assertEqual(result.reply, b"")
        self.assertIn("CRC", result.error)
        self.assertFalse(result.ok)

    def test_unit_mismatch_is_silent(self) -> None:
        request = mb.build_rtu(9, 0x03, mb.read_request(0x03, 0, 1))
        result = ms.respond_frame(request, "rtu", model())
        self.assertTrue(result.silent)
        self.assertEqual(result.reply, b"")
        self.assertIn("不应答", result.summary)

    def test_broadcast_write_applies_but_stays_silent(self) -> None:
        unit = model()
        request = mb.build_rtu(0, 0x06, struct.pack(">HH", 2, 0x0042))
        result = ms.respond_frame(request, "rtu", unit)
        self.assertTrue(result.silent)
        self.assertEqual(result.reply, b"", "广播按规范不应答")
        self.assertEqual(unit.holding[2], 0x0042, "但写要真的生效")

    def test_broadcast_read_is_silent(self) -> None:
        request = mb.build_rtu(0, 0x03, mb.read_request(0x03, 0, 1))
        result = ms.respond_frame(request, "rtu", model())
        self.assertTrue(result.silent)

    def test_tcp_keeps_transaction_and_fixes_length(self) -> None:
        unit = model()
        request = mb.build_tcp(0x1234, 1, 0x03, mb.read_request(0x03, 0, 2))
        result = ms.respond_frame(request, "tcp", unit)
        self.assertTrue(result.ok)
        transaction, protocol, length, unit_id = struct.unpack(">HHHB", result.reply[:7])
        self.assertEqual(transaction, 0x1234, "事务号必须原样回")
        self.assertEqual(protocol, 0)
        self.assertEqual(unit_id, 1)
        self.assertEqual(length, len(result.reply) - mb.MBAP_FIXED_LEN)
        parsed = mb.parse_frame(result.reply, "tcp")
        self.assertTrue(parsed.is_ok)
        self.assertEqual(parsed.values, [0, 1])

    def test_tcp_wrong_protocol_is_dropped(self) -> None:
        request = bytearray(mb.build_tcp(1, 1, 0x03, mb.read_request(0x03, 0, 1)))
        request[2:4] = struct.pack(">H", 5)
        result = ms.respond_frame(bytes(request), "tcp", model())
        self.assertEqual(result.reply, b"")
        self.assertIn("协议号", result.error)

    def test_empty_and_short_frames(self) -> None:
        self.assertIn("空", ms.respond_frame(b"", "rtu", model()).error)
        self.assertIn("太短", ms.respond_frame(b"\x01\x03", "rtu", model()).error)
        self.assertIn("太短", ms.respond_frame(b"\x00\x01\x00\x00\x00", "tcp", model()).error)

    def test_unknown_mode(self) -> None:
        self.assertIn("未知报文模式", ms.respond_frame(b"\x01", "ascii", model()).error)

    def test_illegal_address_reply_is_exception_frame(self) -> None:
        request = mb.build_rtu(1, 0x03, struct.pack(">HH", 100, 10))
        result = ms.respond_frame(request, "rtu", model(size=20))
        self.assertEqual(result.exception, ms.ILLEGAL_DATA_ADDRESS)
        parsed = mb.parse_frame(result.reply, "rtu")
        self.assertTrue(parsed.is_exception)
        self.assertEqual(parsed.function, 0x83)


# --------------------------------------------------------------- TCP 服务


class TcpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.unit = model(size=20)
        self.exchanges: list[ms.SlaveExchange] = []
        self.server = ms.TcpSlaveServer(
            self.unit, host="127.0.0.1", port=0, on_exchange=self.exchanges.append
        )
        self.port = self.server.start()
        self.addCleanup(self.server.stop)

    def client(self) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        sock.settimeout(3)
        self.addCleanup(sock.close)
        return sock

    def recv_reply(self, sock: socket.socket) -> bytes:
        """按 MBAP 的长度域读一帧完整应答。

        长度域算的是「从站号 + PDU」，整帧长度是 6 + 长度域；已经先读了 7 字节头，
        所以后面还要读 ``6 + 长度 - 7`` 字节 —— 少算这一字节就会一直等下去。
        """
        header = b""
        while len(header) < mb.MBAP_HEADER_LEN:
            chunk = sock.recv(mb.MBAP_HEADER_LEN - len(header))
            if not chunk:
                return b""
            header += chunk
        length = struct.unpack(">H", header[4:6])[0]
        remaining = mb.MBAP_FIXED_LEN + length - mb.MBAP_HEADER_LEN
        body = b""
        while len(body) < remaining:
            chunk = sock.recv(remaining - len(body))
            if not chunk:
                break
            body += chunk
        return header + body

    def test_read_over_tcp(self) -> None:
        sock = self.client()
        sock.sendall(mb.build_tcp(7, 1, 0x03, mb.read_request(0x03, 0, 3)))
        reply = self.recv_reply(sock)
        parsed = mb.parse_frame(reply, "tcp")
        self.assertTrue(parsed.is_ok)
        self.assertEqual(parsed.transaction, 7)
        self.assertEqual(parsed.values, [0, 1, 2])
        self.assertEqual(self.server.requests, 1)
        self.assertEqual(self.server.exceptions, 0)
        # 回调在 sendall 之后触发，客户端可能先收到帧 —— 等一小会儿
        deadline = time.time() + 3.0
        while time.time() < deadline and not self.exchanges:
            time.sleep(0.02)
        self.assertEqual(len(self.exchanges), 1)
        self.assertEqual(self.exchanges[0].quantity, 3)

    def test_two_requests_on_one_connection(self) -> None:
        sock = self.client()
        for value in (11, 22):
            request = mb.build_tcp(1, 1, 0x06, struct.pack(">HH", 3, value))
            sock.sendall(request)
            self.assertTrue(self.recv_reply(sock), "每次都应该有回显")
        self.assertEqual(self.unit.holding[3], 22)
        self.assertEqual(self.server.requests, 2)

    def test_exception_counted(self) -> None:
        sock = self.client()
        sock.sendall(mb.build_tcp(2, 1, 0x2B, b"\x0e\x01\x00"))
        reply = self.recv_reply(sock)
        parsed = mb.parse_frame(reply, "tcp")
        self.assertTrue(parsed.is_exception)
        self.assertEqual(self.server.exceptions, 1)

    def test_unit_mismatch_no_bytes_back(self) -> None:
        sock = self.client()
        sock.sendall(mb.build_tcp(3, 5, 0x03, mb.read_request(0x03, 0, 1)))
        sock.settimeout(0.6)
        with self.assertRaises(socket.timeout):
            sock.recv(16)
        self.assertEqual(self.server.requests, 1)
        deadline = time.time() + 3.0
        while time.time() < deadline and not self.exchanges:
            time.sleep(0.02)
        self.assertTrue(self.exchanges[0].silent)

    def test_stop_releases_port(self) -> None:
        port = self.port
        self.server.stop()
        self.assertFalse(self.server.running)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5)

    def test_garbage_length_does_not_kill_server(self) -> None:
        sock = self.client()
        sock.sendall(b"\x00\x01\x00\x00\xff\xff\x01")
        time.sleep(0.2)
        sock.sendall(mb.build_tcp(9, 1, 0x03, mb.read_request(0x03, 0, 1)))
        self.assertTrue(self.server.running, "服务不该因为一帧坏长度就退出")


# --------------------------------------------------------------- 界面


class SlavePanelTest(unittest.TestCase):
    def setUp(self) -> None:
        from icomm.ui.tools.modbus_frame import ModbusFrameDialog

        self.dialog = ModbusFrameDialog()
        self.addCleanup(self.dialog.deleteLater)
        self.dialog.tabs.setCurrentIndex(1)
        self.panel = self.dialog.slave_panel

    def process(self) -> None:
        assert _app is not None
        _app.processEvents()

    def test_two_tabs_and_defaults(self) -> None:
        self.assertEqual(self.dialog.tabs.count(), 2)
        self.assertEqual(self.dialog.tabs.tabText(1), "从机调试")
        self.assertEqual(self.panel.unit_spin.value(), 1)
        self.assertEqual(self.panel.size_spin.value(), 100)
        self.assertEqual(self.panel.table.rowCount(), 100)

    def test_take_builder_frame_generates_reply(self) -> None:
        self.panel.take_builder_frame()
        self.assertTrue(self.panel.request_edit.toPlainText().startswith("01 01"))
        self.assertIn("应答帧：", self.panel.reply_label.text())
        self.assertNotIn("--", self.panel.reply_label.text())
        self.assertTrue(self.panel.copy_reply_button.isEnabled())

    def test_generate_reply_from_text(self) -> None:
        request = mb.build_rtu(1, 0x03, mb.read_request(0x03, 0, 2))
        self.panel.set_request_text(mb.to_hex_text(request))
        result = self.panel.generate_reply()
        self.assertIsNotNone(result)
        parsed = mb.parse_frame(result.reply, "rtu")
        self.assertTrue(parsed.is_ok)
        self.assertEqual(parsed.values, [0, 0], "面板默认数据区是全 0")

        # 换成"地址递增"再读一次，应该能读出 0,1,2
        self.panel.size_spin.setValue(10)
        self.panel.fill_combo.setCurrentIndex(1)
        self.panel._rebuild_model(ask=True)
        again = self.panel.generate_reply()
        self.assertEqual(mb.parse_frame(again.reply, "rtu").values, [0, 1])

    def test_bad_hex_shows_problem(self) -> None:
        self.panel.set_request_text("01 03 ZZZ")
        self.assertIsNone(self.panel.generate_reply())
        self.assertIn("❌", self.panel.reply_detail.toPlainText())

    def test_unit_mismatch_explains(self) -> None:
        request = mb.build_rtu(9, 0x03, mb.read_request(0x03, 0, 1))
        self.panel.set_request_text(mb.to_hex_text(request))
        self.panel.generate_reply()
        self.assertIn("不应答", self.panel.reply_label.text())
        self.assertIn("从站号", self.panel.reply_detail.toPlainText())

    def test_tcp_mode(self) -> None:
        self.panel.set_mode("tcp")
        request = mb.build_tcp(0x22, 1, 0x03, mb.read_request(0x03, 0, 1))
        self.panel.set_request_text(mb.to_hex_text(request))
        result = self.panel.generate_reply()
        self.assertEqual(result.reply[:2], b"\x00\x22", "事务号要跟着回")

    def test_rebuild_model(self) -> None:
        self.panel.size_spin.setValue(10)
        self.panel.fill_combo.setCurrentIndex(1)  # 地址递增
        self.panel.unit_spin.setValue(7)
        self.panel._rebuild_model(ask=True)
        self.assertEqual(len(self.panel.model.holding), 10)
        self.assertEqual(self.panel.model.unit_id, 7)
        self.assertEqual(self.panel.model.holding[3], 3)
        self.assertEqual(self.panel.table.rowCount(), 10)
        self.assertIn("地址递增", self.panel.model_state.text())

    def test_table_edit_updates_model(self) -> None:
        item = self.panel.table.item(2, 3)
        item.setText("1234")
        self.assertEqual(self.panel.model.holding[2], 1234)
        item.setText("坏值")  # 非法输入要回退，不能把模型写坏
        self.assertEqual(self.panel.model.holding[2], 1234)
        from PySide6.QtCore import Qt

        coil = self.panel.table.item(1, 1)
        coil.setCheckState(Qt.Checked)
        self.assertEqual(self.panel.model.coils[1], 1)
        coil.setCheckState(Qt.Unchecked)
        self.assertEqual(self.panel.model.coils[1], 0)

    def test_server_roundtrip_through_panel(self) -> None:
        self.panel.port_spin.setValue(0)
        port = self.panel.start_server()
        self.assertIsNotNone(port)
        self.addCleanup(self.panel.stop_server)
        self.assertIn("运行中", self.panel.server_state.text())

        sock = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.addCleanup(sock.close)
        sock.settimeout(3)
        request = mb.build_tcp(5, 1, 0x06, struct.pack(">HH", 2, 4321))
        sock.sendall(request)
        reply = sock.recv(64)
        self.assertTrue(reply)
        # 服务端是"先回帧、再发信号"，所以这里要等事件排队过来（别假设立刻到）
        deadline = time.time() + 3.0
        while time.time() < deadline and self.panel.log_table.rowCount() == 0:
            self.process()
            time.sleep(0.02)
        self.assertEqual(self.panel.log_table.rowCount(), 1)
        self.assertEqual(self.panel.log_table.item(0, 1).text().split(":")[0], "127.0.0.1")
        self.assertIn("已应答 1 次", self.panel.counter_label.text())
        self.assertEqual(self.panel.model.holding[2], 4321)
        self.assertEqual(self.panel.table.item(2, 3).text(), "4321", "主站写的值要刷回表格")

    def test_close_stops_server(self) -> None:
        self.panel.port_spin.setValue(0)
        port = self.panel.start_server()
        self.assertIsNotNone(port)
        self.dialog.close()
        self.assertIsNone(self.panel.server)
        self.assertFalse(self.panel.stop_button.isEnabled())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
