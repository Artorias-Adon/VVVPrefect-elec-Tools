"""编解码单元测试。"""

from __future__ import annotations

import struct
import unittest

from icomm.core import codec


class CombineSplitTest(unittest.TestCase):
    def test_combine_registers(self):
        self.assertEqual(codec.combine_registers([0x1234, 0x5678]), b"\x12\x34\x56\x78")

    def test_split_registers(self):
        self.assertEqual(codec.split_registers(b"\x12\x34\x56\x78"), [0x1234, 0x5678])

    def test_split_requires_even_length(self):
        with self.assertRaises(ValueError):
            codec.split_registers(b"\x01\x02\x03")

    def test_roundtrip(self):
        regs = [0x0001, 0xFFFF, 0x8000]
        self.assertEqual(codec.split_registers(codec.combine_registers(regs)), regs)


class DecodeEncodeTest(unittest.TestCase):
    def test_uint16(self):
        self.assertEqual(codec.decode([2200], "uint16"), 2200)

    def test_int16_negative(self):
        self.assertEqual(codec.decode([0xFFFF], "int16"), -1)
        self.assertEqual(codec.decode([0x8000], "int16"), -32768)

    def test_uint32_big_word_order(self):
        # 高字在前：0x1234 0x5678 -> 0x12345678
        self.assertEqual(codec.decode([0x1234, 0x5678], "uint32", "big"), 0x12345678)

    def test_uint32_little_word_order(self):
        # 低字在前：0x1234 0x5678 -> 0x56781234
        self.assertEqual(codec.decode([0x1234, 0x5678], "uint32", "little"), 0x56781234)

    def test_int32_negative(self):
        regs = codec.encode(-123456, "int32")
        self.assertEqual(codec.decode(regs, "int32"), -123456)

    def test_float32(self):
        for value in (0.0, 25.5, -12.75, 3.14159):
            regs = codec.encode(value, "float32")
            self.assertEqual(len(regs), 2)
            self.assertAlmostEqual(codec.decode(regs, "float32"), value, places=5)

    def test_float32_known_bytes(self):
        # 25.5 = 0x41CC0000
        self.assertEqual(codec.encode(25.5, "float32"), [0x41CC, 0x0000])

    def test_float64(self):
        regs = codec.encode(1.0e100, "float64")
        self.assertEqual(len(regs), 4)
        self.assertAlmostEqual(codec.decode(regs, "float64"), 1.0e100, delta=1e90)

    def test_bool(self):
        self.assertTrue(codec.decode([1], "bool"))
        self.assertFalse(codec.decode([0], "bool"))
        self.assertEqual(codec.encode(True, "bool"), [1])
        self.assertEqual(codec.encode(False, "bool"), [0])

    def test_decode_insufficient_registers(self):
        with self.assertRaises(ValueError):
            codec.decode([0x0001], "uint32")

    def test_encode_uint_rounds(self):
        self.assertEqual(codec.encode(12.6, "uint16"), [13])

    def test_uint8_uses_low_byte(self):
        """8 位类型：一个寄存器 16 位，只用低字节（西门子 S7 的 DB1.DBB4 是这种点）。"""
        self.assertEqual(codec.decode([0x00A5], "uint8"), 0xA5)
        self.assertEqual(codec.decode([0xFFA5], "uint8"), 0xA5, "高字节是填充，忽略")
        self.assertEqual(codec.encode(0xA5, "uint8"), [0xA5])
        self.assertEqual(codec.decode(codec.encode(200, "uint8"), "uint8"), 200)

    def test_int8_signed(self):
        self.assertEqual(codec.decode([0x00FF], "int8"), -1)
        self.assertEqual(codec.decode([0x0080], "int8"), -128)
        self.assertEqual(codec.decode([0x007F], "int8"), 127)
        self.assertEqual(codec.decode(codec.encode(-100, "int8"), "int8"), -100)

    def test_byte_types_respect_scale(self):
        self.assertAlmostEqual(
            codec.registers_to_value([0x00C8], "uint8", scale=0.5), 100.0
        )

    def test_byte_type_without_registers(self):
        with self.assertRaises(ValueError):
            codec.decode([], "uint8")

    def test_unknown_type(self):
        with self.assertRaises(ValueError):
            codec.decode([0x0001], "uint24")


class EngineeringValueTest(unittest.TestCase):
    def test_scale_and_offset(self):
        # 1234 -> 123.4V
        self.assertAlmostEqual(codec.registers_to_value([1234], "uint16", scale=0.1), 123.4)

    def test_offset(self):
        self.assertAlmostEqual(
            codec.registers_to_value([100], "uint16", scale=1.0, offset=-50.0), 50.0
        )

    def test_write_path_inverts_read_path(self):
        for scale, offset in ((0.1, 0.0), (0.01, 0.0), (1.0, -50.0), (0.5, 10.0)):
            regs = codec.value_to_registers(42.0, "uint16", scale=scale, offset=offset)
            back = codec.registers_to_value(regs, "uint16", scale=scale, offset=offset)
            self.assertAlmostEqual(back, 42.0, delta=abs(scale) if scale else 1e-6)

    def test_zero_scale_rejected(self):
        with self.assertRaises(ValueError):
            codec.to_raw(10.0, scale=0.0)

    def test_bool_ignores_scale(self):
        self.assertTrue(codec.to_engineering(True, 100.0, 5.0))


class FormatValueTest(unittest.TestCase):
    def test_none(self):
        self.assertEqual(codec.format_value(None), "--")

    def test_bool(self):
        self.assertEqual(codec.format_value(True), "ON")
        self.assertEqual(codec.format_value(False), "OFF")

    def test_int(self):
        self.assertEqual(codec.format_value(50, 2), "50")

    def test_float_decimals(self):
        self.assertEqual(codec.format_value(50.0, 2), "50.00")
        self.assertEqual(codec.format_value(3.14159, 3), "3.142")

    def test_decimals_clamped(self):
        self.assertEqual(codec.format_value(1.5, 99), "1.500000")
        self.assertEqual(codec.format_value(1.5, -3), "2")


if __name__ == "__main__":
    unittest.main()
