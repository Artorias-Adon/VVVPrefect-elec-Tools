"""仿真器报文格式测试。

用裸 socket 直接校验 MBAP 头的字节布局。
历史上这里踩过一个坑：响应长度域被 struct 的 ``B`` 截断成 1 字节，
使整个 MBAP 头少了一个字节，所有客户端都收不到响应。
这里逐字节断言，避免同类问题再次发生。
"""

from __future__ import annotations

import socket
import struct
import threading
import time
import unittest

from tools.modbus_sim import ModbusTcpSimulator

PORT = 15030


class SimulatorFramingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sim = ModbusTcpSimulator("127.0.0.1", PORT)
        threading.Thread(
            target=cls.sim.serve_forever, kwargs={"freeze": False}, daemon=True
        ).start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", PORT), 0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("仿真器未就绪")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sim.stop()

    # -------------------------------------------------- 工具

    @staticmethod
    def _request(pdu: bytes, unit: int = 1, tid: int = 1) -> bytes:
        """发一个请求，返回完整 ADU。"""
        adu = struct.pack(">HHH", tid, 0, len(pdu) + 1) + bytes([unit]) + pdu
        with socket.create_connection(("127.0.0.1", PORT), 2) as sock:
            sock.sendall(adu)
            sock.settimeout(3)
            return sock.recv(1024)

    @staticmethod
    def _split(adu: bytes) -> tuple[int, int, int, int, bytes]:
        """拆出 MBAP 各字段与 PDU。"""
        tid, pid, length, uid = struct.unpack(">HHHB", adu[:7])
        return tid, pid, length, uid, adu[7:]

    def _check_framing(self, adu: bytes, expect_uid: int = 1, expect_tid: int = 1) -> bytes:
        """断言 MBAP 头合法，返回 PDU。"""
        self.assertGreaterEqual(len(adu), 9, f"ADU 太短：{adu.hex(' ')}")
        tid, pid, length, uid, pdu = self._split(adu)
        self.assertEqual(pid, 0, "协议号必须为 0")
        self.assertEqual(uid, expect_uid)
        self.assertEqual(tid, expect_tid, "事务号必须原样回显")
        self.assertEqual(length, len(pdu) + 1, "长度域 = 从站号(1) + PDU 长度")
        self.assertEqual(len(adu), 6 + length, "ADU 总长 = 6 + 长度域")
        return pdu

    # -------------------------------------------------- 读

    def test_read_holding_framing(self):
        pdu = self._check_framing(self._request(bytes([0x03]) + struct.pack(">HH", 0, 2)))
        self.assertEqual(pdu[0], 0x03)
        self.assertEqual(pdu[1], 4, "字节数应为 2 个寄存器 = 4")
        self.assertEqual(len(pdu), 2 + 4)

    def test_read_holding_returns_float32_temperature(self):
        pdu = self._check_framing(self._request(bytes([0x03]) + struct.pack(">HH", 0, 2)))
        regs = struct.unpack(">HH", pdu[2:6])
        temperature = struct.unpack(">f", struct.pack(">HH", *regs))[0]
        self.assertGreater(temperature, 15.0)
        self.assertLess(temperature, 75.0)

    def test_read_input_register(self):
        pdu = self._check_framing(self._request(bytes([0x04]) + struct.pack(">HH", 0, 1)))
        self.assertEqual(pdu[0], 0x04)
        self.assertEqual(pdu[1], 2)

    def test_read_coils(self):
        pdu = self._check_framing(self._request(bytes([0x01]) + struct.pack(">HH", 0, 2)))
        self.assertEqual(pdu[0], 0x01)
        self.assertEqual(pdu[1], 1, "2 位只需 1 个字节")

    def test_read_discrete_inputs(self):
        pdu = self._check_framing(self._request(bytes([0x02]) + struct.pack(">HH", 0, 1)))
        self.assertEqual(pdu[0], 0x02)
        self.assertEqual(pdu[1], 1)

    # -------------------------------------------------- 写

    def test_write_single_coil_echo(self):
        body = struct.pack(">HH", 0, 0xFF00)
        pdu = self._check_framing(self._request(bytes([0x05]) + body))
        self.assertEqual(pdu, bytes([0x05]) + body)

    def test_write_single_register_echo(self):
        body = struct.pack(">HH", 8, 600)
        pdu = self._check_framing(self._request(bytes([0x06]) + body))
        self.assertEqual(pdu, bytes([0x06]) + body)

    def test_write_multiple_registers_echo(self):
        data = struct.pack(">HH", 0x0001, 0x0002)
        request = bytes([0x10]) + struct.pack(">HHB", 5, 2, 4) + data
        pdu = self._check_framing(self._request(request))
        self.assertEqual(pdu, bytes([0x10]) + struct.pack(">HH", 5, 2))

    def test_write_multiple_coils_echo(self):
        request = bytes([0x0F]) + struct.pack(">HHB", 0, 3, 1) + bytes([0b00000110])
        pdu = self._check_framing(self._request(request))
        self.assertEqual(pdu, bytes([0x0F]) + struct.pack(">HH", 0, 3))

    # -------------------------------------------------- 异常响应

    def test_out_of_range_address_returns_exception(self):
        pdu = self._check_framing(self._request(bytes([0x03]) + struct.pack(">HH", 2000, 1)))
        self.assertEqual(pdu[0], 0x83, "异常响应的功能码需置最高位")
        self.assertEqual(pdu[1], 0x02)

    def test_illegal_function_returns_exception(self):
        pdu = self._check_framing(self._request(bytes([0x08]) + struct.pack(">HH", 0, 1)))
        self.assertEqual(pdu[0], 0x88)
        self.assertEqual(pdu[1], 0x01)

    def test_unknown_unit_id_returns_exception(self):
        pdu = self._check_framing(
            self._request(bytes([0x03]) + struct.pack(">HH", 0, 1), unit=9), expect_uid=9
        )
        self.assertEqual(pdu[0], 0x83)

    def test_too_many_registers_returns_exception(self):
        pdu = self._check_framing(self._request(bytes([0x03]) + struct.pack(">HH", 0, 200)))
        self.assertEqual(pdu[0], 0x83)
        self.assertEqual(pdu[1], 0x03)

    # -------------------------------------------------- 多帧复用同一连接

    def test_multiple_requests_on_one_connection(self):
        """长连接连续请求不应出现粘包/错帧。"""
        with socket.create_connection(("127.0.0.1", PORT), 2) as sock:
            sock.settimeout(3)
            for tid in (1, 2, 3, 5, 8):
                pdu = bytes([0x03]) + struct.pack(">HH", 2, 1)
                sock.sendall(
                    struct.pack(">HHH", tid, 0, len(pdu) + 1) + bytes([1]) + pdu
                )
                adu = sock.recv(1024)
                self._check_framing(adu, expect_tid=tid)


if __name__ == "__main__":
    unittest.main()
