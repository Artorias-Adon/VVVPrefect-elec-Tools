"""程序员计算器的测试。

重点是**用已知真值核对**，而不是只验证自己和自己一致：

* 进制往返覆盖 8/16/32/64 位 × 有符号/无符号，含 0、最大值、最小值、-1；
* IEEE-754 位模式拿教科书上的已知常量对（1.0 / -2.5 / 350.0），
  并核对符号位、指数位、尾数位、构成公式这些**字段**，不是只看整体数值；
* 0.1 这类 float32 存不下的数要真的报出误差；
* +0 / -0 / ±inf / NaN / 次正规数都要能拆对；
* 溢出与非法输入必须报错，不能静默截断；
* 顺带验证界面四个进制栏真的联动（这种 bug 光读代码是看不出来的）。

跑法（本机 sysytem locale 是 zh_HK，Qt 数字框会显示成 CJK 数字，所以先在
``QApplication`` 之后调 ``qtboot.prepare_qt()``）::

    set QT_QPA_PLATFORM=offscreen
    python -m unittest tests.test_programmer_calc -v
"""

from __future__ import annotations

import math
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from icomm.core import programmer_calc as pc  # noqa: E402


def _show(title: str, lines: list[str]) -> None:
    """把实测值打出来（人工核对用，unittest 不会吞 stdout）。"""
    print(f"\n---- {title} ----")
    for line in lines:
        print(f"  {line}")


class ParseTests(unittest.TestCase):
    """文本 -> 位模式：前缀、分隔符、全角、范围检查。"""

    def test_hex_prefix_and_bare_hex(self) -> None:
        self.assertEqual(pc.parse_int("0x43AF0000"), 0x43AF0000)
        self.assertEqual(pc.parse_int("3f800000", 32, base=16), 0x3F800000)
        self.assertEqual(pc.parse_int("0XFF", 8), 0xFF)

    def test_tolerates_separators_and_full_width(self) -> None:
        # 工程师是从报文/表格/文档里连空格一起复制的，这些写法都得认
        self.assertEqual(pc.parse_int("0x43AF_0000"), 0x43AF0000)
        self.assertEqual(pc.parse_int("0x 43 AF 00 00"), 0x43AF0000)
        self.assertEqual(pc.parse_int("1_000", 16), 1000)
        self.assertEqual(pc.parse_int("０ｘ１Ｆ", 8), 31)
        self.assertEqual(pc.parse_int(" 255 ", 8), 255)

    def test_binary_and_octal_prefix(self) -> None:
        self.assertEqual(pc.parse_int("0b1010_1010", 8), 0xAA)
        self.assertEqual(pc.parse_int("0o377", 8), 255)

    def test_forced_base(self) -> None:
        # 八进制框里写 377 是八进制，不是十进制
        self.assertEqual(pc.parse_int("377", 16, base=8), 0o377)
        with self.assertRaises(pc.CalcError):
            pc.parse_int("0x10", 16, base=8)  # 前缀与强制进制不符

    def test_signed_negative(self) -> None:
        self.assertEqual(pc.parse_int("-1", 8, True), 0xFF)
        self.assertEqual(pc.parse_int("-128", 8, True), 0x80)
        self.assertEqual(pc.parse_int("-2147483648", 32, True), 0x80000000)
        # 有符号下也允许直接写满位宽的位模式写法（0xFFFFFFFF == -1 的位）
        self.assertEqual(pc.parse_int("0xFFFFFFFF", 32, True), 0xFFFFFFFF)
        self.assertEqual(pc.to_signed(0xFFFFFFFF, 32), -1)

    def test_negative_in_unsigned_is_an_error(self) -> None:
        with self.assertRaises(pc.CalcError):
            pc.parse_int("-1", 8, False)

    def test_overflow_is_reported_not_truncated(self) -> None:
        # 需求里点名的用例：32 位框里填 0x1FFFFFFFF
        with self.assertRaises(pc.CalcError) as ctx:
            pc.parse_int("0x1FFFFFFFF", 32)
        self.assertIn("32", str(ctx.exception))
        self.assertEqual(pc.parse_int("0x1FFFFFFFF", 64), 0x1FFFFFFFF)  # 64 位放得下

        with self.assertRaises(pc.CalcError):
            pc.parse_int("256", 8)
        with self.assertRaises(pc.CalcError):
            pc.parse_int("-129", 8, True)
        with self.assertRaises(pc.CalcError):
            pc.parse_int("0x10000", 16)

    def test_bad_text_is_an_error(self) -> None:
        for text in ("", "   ", "abc", "1.5", "0x", "-", "0xZZ", "12ab"):
            with self.subTest(text=text):
                with self.assertRaises(pc.CalcError):
                    pc.parse_int(text, 16, base=0)

    def test_parse_float(self) -> None:
        self.assertEqual(pc.parse_float("0x10"), 16.0)
        self.assertEqual(pc.parse_float("1_000.5"), 1000.5)
        self.assertAlmostEqual(pc.parse_float("1e-3"), 0.001)
        self.assertAlmostEqual(pc.parse_float("-2.5"), -2.5)
        self.assertTrue(math.isinf(pc.parse_float("inf")))
        self.assertTrue(math.isnan(pc.parse_float("nan")))
        with self.assertRaises(pc.CalcError):
            pc.parse_float("")


class FormatTests(unittest.TestCase):
    """格式化：大写、补零、二进制分组，同时要能拿到不带分隔符的原始串。"""

    def test_hex(self) -> None:
        self.assertEqual(pc.format_hex(0x43AF0000, 32), "0x43AF0000")
        self.assertEqual(pc.format_hex(0x1F), "0x1F")  # 不给位宽就取最短
        self.assertEqual(pc.format_hex(0x1F, 8, prefix=False), "1F")

    def test_binary_grouped_and_raw(self) -> None:
        self.assertEqual(
            pc.format_binary(0x43AF0000, 32),
            "0100 0011 1010 1111 0000 0000 0000 0000",
        )
        # 分组只是为了好读，解析要能吃这两种写法（BIN 栏就是这么解析的）
        self.assertEqual(
            pc.format_binary(0x43AF0000, 32, group=False),
            "01000011101011110000000000000000",
        )
        self.assertEqual(
            pc.parse_int("0100 0011 1010 1111 0000 0000 0000 0000", 32, base=2), 0x43AF0000
        )
        self.assertEqual(pc.parse_int("0b0100_0011", 8), 0x43)
        self.assertEqual(pc.format_binary(5), "101")
        self.assertEqual(pc.format_binary(0b10101111), "1010 1111")

    def test_octal_and_decimal(self) -> None:
        self.assertEqual(pc.format_octal(0xFFFF, 16), "0o177777")
        self.assertEqual(pc.format_octal(0xFF, 8), "0o377")
        self.assertEqual(pc.format_decimal(0xFFFFFFFF, 32), "4294967295")
        self.assertEqual(pc.format_decimal(0xFFFFFFFF, 32, signed=True), "-1")
        self.assertEqual(pc.format_decimal(0x80000000, 32, signed=True), "-2147483648")

    def test_dispatch(self) -> None:
        bits = 0x43AF0000
        self.assertEqual(pc.format_value(bits, 32, 16), "0x43AF0000")
        self.assertEqual(pc.format_value(bits, 32, 10), "1135542272")
        self.assertEqual(pc.format_value(bits, 32, 8), pc.format_octal(bits, 32))
        self.assertEqual(pc.format_value(bits, 32, 2), pc.format_binary(bits, 32))
        with self.assertRaises(pc.CalcError):
            pc.format_value(bits, 32, 7)


class RoundTripTests(unittest.TestCase):
    """进制往返：4 种位宽 × 两种符号 × 边界值 × 4 个进制。"""

    def test_round_trip(self) -> None:
        checked = 0
        for width in pc.WIDTHS:
            limit = 1 << width
            values = [
                0,
                1,
                limit - 1,
                limit >> 1,
                (limit >> 1) - 1,
                -(limit >> 1),  # 有符号最小值
                (limit >> 1) - 1,  # 有符号最大值
                -1,
            ]
            for signed in (False, True):
                for value in values:
                    bits = pc.to_unsigned(value, width)
                    for base in (2, 8, 10, 16):
                        with self.subTest(width=width, signed=signed, value=value, base=base):
                            text = pc.format_value(bits, width, base, signed)
                            self.assertEqual(
                                pc.parse_int(text, width, signed, base=base),
                                bits,
                                f"{text} 回读不一致",
                            )
                            checked += 1
        _show("进制往返", [f"共核对 {checked} 组（8/16/32/64 位 × 有符号/无符号 × 边界值 × 4 种进制）"])

    def test_boundary_text(self) -> None:
        self.assertEqual(pc.format_value(0xFFFFFFFF, 32, 10, signed=True), "-1")
        self.assertEqual(pc.format_value(0x7FFFFFFF, 32, 10, signed=True), "2147483647")
        self.assertEqual(pc.format_value(0xFFFF, 16, 16), "0xFFFF")
        self.assertEqual(pc.format_value(0xFFFF, 16, 10, signed=True), "-1")
        self.assertEqual(pc.format_value(0xFFFF, 16, 10), "65535")
        self.assertEqual(pc.format_value(1 << 63, 64, 10, signed=True), "-9223372036854775808")


class BitOpTests(unittest.TestCase):
    """位操作：单位置位/清除/翻转、位段提取、位状态表。"""

    def test_single_bit(self) -> None:
        self.assertEqual(pc.set_bit(0, 3, 8), 0x08)
        self.assertEqual(pc.set_bit(0x00, 15, 16), 0x8000)
        self.assertEqual(pc.clear_bit(0xFF, 0, 8), 0xFE)
        self.assertEqual(pc.toggle_bit(0, 7, 8), 0x80)
        self.assertEqual(pc.toggle_bit(0x80, 7, 8), 0x00)
        self.assertEqual(pc.set_bit(0, 2, 8, on=False), 0x00)  # on=False 等价清位
        self.assertTrue(pc.test_bit(0x80, 7, 8))
        self.assertFalse(pc.test_bit(0x80, 6, 8))

    def test_bit_ops_stay_inside_width(self) -> None:
        # 翻转第 8 位后不能溢出到 16 位去
        self.assertEqual(pc.toggle_bit(1, 7, 8), 0x81)
        self.assertEqual(pc.set_bit(0, 7, 8), 0x80)

    def test_index_out_of_range(self) -> None:
        for call in (
            lambda: pc.test_bit(0, 8, 8),
            lambda: pc.set_bit(0, -1, 8),
            lambda: pc.toggle_bit(0, 32, 32),
        ):
            with self.assertRaises(pc.CalcError):
                call()

    def test_extract_bits(self) -> None:
        self.assertEqual(pc.extract_bits(0x12345678, 15, 8, 32), 0x56)
        self.assertEqual(pc.extract_bits(0x12345678, 31, 24, 32), 0x12)
        self.assertEqual(pc.extract_bits(0xFFFF, 3, 0, 16), 0xF)
        self.assertEqual(pc.extract_bits(0xABCD, 4, 4, 16), 0b0)  # 单个位段
        self.assertEqual(pc.extract_bits(0xABCD, 15, 0, 16), 0xABCD)  # 整个宽度
        self.assertEqual(pc.extract_bits(0xABCD, 3, 0, 16), 0xD)

    def test_extract_bits_errors(self) -> None:
        with self.assertRaises(pc.CalcError):  # high < low
            pc.extract_bits(0x1234, 3, 4, 16)
        with self.assertRaises(pc.CalcError):  # 超出位宽
            pc.extract_bits(0x1234, 16, 0, 16)
        with self.assertRaises(pc.CalcError):
            pc.extract_bits(0x1234, 3, -1, 16)

    def test_bit_field_mask(self) -> None:
        self.assertEqual(pc.bit_field_mask(15, 8, 32), 0xFF00)
        self.assertEqual(pc.bit_field_mask(3, 0, 32), 0x000F)
        # 掩码与提取要能对上：清场后填新值，读回来就是新值
        value = (0x1234 & ~pc.bit_field_mask(15, 8, 32)) | (0xAB << 8)
        self.assertEqual(pc.extract_bits(value, 15, 8, 32), 0xAB)
        self.assertEqual(pc.extract_bits(value, 7, 0, 32), 0x34)
        with self.assertRaises(pc.CalcError):
            pc.bit_field_mask(3, 4, 32)

    def test_bit_names(self) -> None:
        states = pc.bit_names(0b1010_0001, 8)
        self.assertEqual(len(states), 8)
        self.assertEqual(states[0].index, 0, "应从低位开始，bit0 在最前")
        self.assertEqual([s.value for s in states], [1, 0, 0, 0, 0, 1, 0, 1])
        self.assertTrue(states[0].is_set)
        self.assertEqual(states[5].name, "bit5")
        self.assertEqual(states[5].byte_index, 0)
        self.assertEqual(states[5].byte_bit, 5)
        self.assertEqual(states[5].hex_text, "0x20")

        # 给位定义表时用表里的名字（点位参数表的「位定义」列就是这个形状）
        named = pc.bit_names(0x10, 16, labels={4: "使能", 15: "启动"})
        self.assertEqual(named[4].name, "使能")
        self.assertEqual(named[15].name, "启动")
        self.assertEqual(named[9].name, "bit9")
        self.assertEqual(pc.bit_names(0x02, 16)[1].byte_index, 0)


class FloatTests(unittest.TestCase):
    """IEEE-754：已知位模式、字段拆解、误差、特殊值。"""

    def test_known_bit_patterns(self) -> None:
        cases = [
            (1.0, 32, 0x3F800000),
            (-2.5, 32, 0xC0200000),
            (2.5, 32, 0x40200000),
            (350.0, 32, 0x43AF0000),
            (0.0, 32, 0x00000000),
            (1.0, 64, 0x3FF0000000000000),
            (-2.5, 64, 0xC004000000000000),
            (0.0, 64, 0x0000000000000000),
        ]
        lines = []
        for value, width, expected in cases:
            bits = pc.float_to_bits(value, width)
            lines.append(f"{value!r} 在 {width} 位 = 0x{bits:0{width // 4}X}（期望 0x{expected:0{width // 4}X}）")
            self.assertEqual(bits, expected, f"{value} 的 {width} 位位模式不对")
        _show("已知位模式", lines)

    def test_bits_to_float_350(self) -> None:
        # 需求里给的例子：0x43AF0000 到底是不是 350？核对过程写在注释里，防止将来算错还看不出来：
        #   0x43AF0000 = 0 10000111 01011110000000000000000
        #   指数 0x87 = 135 -> 135 - 127 = 8
        #   尾数 0x2F0000 / 2^23 = 0.3671875，隐含 1 后是 1.3671875
        #   1.3671875 × 2^8 = 350.0  ✓
        self.assertEqual(pc.bits_to_float(0x43AF0000, 32), 350.0)
        self.assertEqual(pc.bits_to_float(0x3F800000, 32), 1.0)
        self.assertEqual(pc.bits_to_float(0xC004000000000000, 64), -2.5)

    def test_decode_fields_for_350(self) -> None:
        info = pc.decode_float(350.0, 32)
        self.assertEqual(info.sign, 0)
        self.assertEqual(info.sign_text, "0")
        self.assertEqual(info.exponent_bits, 135)
        self.assertEqual(info.exponent_text, "10000111")
        self.assertEqual(info.exponent, 8)
        self.assertEqual(info.mantissa_bits, 0x2F0000)
        self.assertEqual(info.mantissa, 0.3671875)
        self.assertEqual(info.mantissa_text, "1.01011110000000000000000")
        self.assertEqual(info.hex_text, "0x43AF0000")
        self.assertEqual(info.binary_text, "0100 0011 1010 1111 0000 0000 0000 0000")
        self.assertTrue(info.is_normal)
        self.assertFalse(info.is_subnormal or info.is_zero or info.is_inf or info.is_nan)
        self.assertIn("(-1)^0", info.formula)
        self.assertIn("2^(135-127)", info.formula)
        self.assertIn("1.3671875", info.formula)
        self.assertIn("350", info.formula)
        self.assertTrue(info.exact, "350 是整数，float32 能精确表示")
        self.assertEqual(info.error, 0.0)
        _show(
            "350.0 的 float32 拆解",
            [
                f"位模式 {info.hex_text}　二进制 {info.binary_text}",
                f"符号 {info.sign}　指数 {info.exponent_bits}({info.exponent_text}) 真值 {info.exponent}",
                f"尾数 0x{info.mantissa_bits:X}（{info.mantissa_text}）小数 {info.mantissa}",
                f"构成 {info.formula}",
                f"误差 {info.error}（精确={info.exact}）",
            ],
        )

    def test_1_0_fields_32_and_64(self) -> None:
        info32 = pc.decode_float(1.0, 32)
        self.assertEqual((info32.exponent_bits, info32.exponent, info32.mantissa), (127, 0, 0.0))
        self.assertEqual(info32.formula, "(-1)^0 × 1.00000000000000000000000 × 2^(127-127) = 1 × 2^0 = 1")

        info64 = pc.decode_float(1.0, 64)
        self.assertEqual(pc.float_layout(64), (11, 52))
        self.assertEqual(pc.float_bias(64), 1023)
        self.assertEqual(info64.exponent_bits, 1023)
        self.assertEqual(info64.exponent, 0)
        self.assertEqual(info64.mantissa_bits, 0)
        self.assertEqual(info64.hex_text, "0x3FF0000000000000")
        self.assertTrue(info64.exact)

    def test_exact_values_round_trip(self) -> None:
        # 2 的幂与 0.15625（= 5/32）在 float32 里都是精确的
        for value in (0.5, 0.25, 0.15625, 1024.0, -3.0):
            info = pc.decode_float(value, 32)
            with self.subTest(value=value):
                self.assertTrue(info.exact, f"{value} 在 float32 里应能精确表示")
                self.assertEqual(info.value, value)
                self.assertEqual(pc.bits_to_float(pc.float_to_bits(value, 32), 32), value)

    def test_float32_cannot_represent_0_1(self) -> None:
        info = pc.decode_float(0.1, 32)
        # float32 里的 0.1 是 13421773 × 2^-27，比真正的 0.1 略大
        expected_error = 13421773 / 2 ** 27 - 0.1
        self.assertEqual(info.hex_text, "0x3DCCCCCD")
        self.assertFalse(info.exact)
        self.assertEqual(info.error, expected_error)
        self.assertGreater(info.error, 0)
        self.assertAlmostEqual(info.relative_error, expected_error / 0.1, places=20)
        # 而 64 位就能「精确」表示这个 Python float 本身
        self.assertTrue(pc.decode_float(0.1, 64).exact)
        _show(
            "0.1 的误差",
            [
                f"float32 位模式 {info.hex_text} → 实际值 {info.value!r}",
                f"误差 {info.error!r}（相对 {info.relative_error!r}）→ 精确表示={info.exact}",
                f"float64 位模式 {pc.decode_float(0.1, 64).hex_text} → 误差 {pc.decode_float(0.1, 64).error}（精确）",
            ],
        )

    def test_zero_inf_nan(self) -> None:
        plus_zero = pc.decode_bits(0x00000000, 32)
        self.assertTrue(plus_zero.is_zero and not plus_zero.is_subnormal)
        self.assertEqual(plus_zero.sign, 0)
        self.assertEqual(plus_zero.value, 0.0)
        self.assertEqual(plus_zero.exponent, -126)  # 零的指数按 IEEE 记 1-偏移
        self.assertTrue(plus_zero.exact)

        minus_zero = pc.decode_bits(0x80000000, 32)
        self.assertTrue(minus_zero.is_zero)
        self.assertEqual(minus_zero.sign, 1)
        self.assertEqual(minus_zero.value, 0.0)
        self.assertEqual(math.copysign(1.0, minus_zero.value), -1.0)
        self.assertEqual(pc.bits_to_float(0x80000000, 32), -0.0)

        inf = pc.decode_bits(0x7F800000, 32)
        self.assertTrue(inf.is_inf and not inf.is_nan)
        self.assertTrue(math.isinf(inf.value) and inf.value > 0)
        self.assertIn("∞", inf.formula)
        self.assertTrue(inf.exact, "±∞ 的位模式是精确的（误差记 0）")
        self.assertEqual(pc.float_to_bits(float("inf"), 32), 0x7F800000)

        neg_inf = pc.decode_bits(0xFF800000, 32)
        self.assertTrue(neg_inf.is_inf)
        self.assertEqual(neg_inf.sign, 1)
        self.assertEqual(pc.float_to_bits(float("-inf"), 32), 0xFF800000)

        nan = pc.decode_bits(0x7FC00000, 32)
        self.assertTrue(nan.is_nan)
        self.assertTrue(math.isnan(nan.value))
        self.assertFalse(nan.exact)
        self.assertTrue(math.isnan(nan.error))
        self.assertIsNone(nan.relative_error)
        self.assertIn("NaN", nan.formula)
        self.assertEqual(pc.float_to_bits(float("nan"), 32) & 0x7F800000, 0x7F800000)

        # 有效载荷非 0 的 NaN（信号 NaN）也算 NaN
        self.assertTrue(pc.decode_bits(0x7F800001, 32).is_nan)
        _show(
            "特殊值",
            [
                "+0 0x00000000 零={} 值={}".format(plus_zero.is_zero, plus_zero.value),
                f"-0 0x80000000 零={minus_zero.is_zero} 符号={minus_zero.sign} 值符号={math.copysign(1.0, minus_zero.value)}",
                f"+inf {inf.hex_text} 无穷={inf.is_inf} 公式 {inf.formula}",
                f"-inf {neg_inf.hex_text} 符号={neg_inf.sign} 值={neg_inf.value}",
                f"NaN {nan.hex_text} NaN={nan.is_nan} 误差={nan.error} 公式 {nan.formula}",
            ],
        )

    def test_subnormal(self) -> None:
        smallest = pc.decode_bits(0x00000001, 32)
        self.assertTrue(smallest.is_subnormal)
        self.assertFalse(smallest.is_normal)
        self.assertEqual(smallest.exponent_bits, 0)
        self.assertEqual(smallest.exponent, -126)  # 次正规数按 2^(1-偏移)
        self.assertEqual(smallest.mantissa_bits, 1)
        self.assertEqual(smallest.mantissa, 2 ** -23)
        self.assertEqual(smallest.mantissa_text, "0." + "0" * 22 + "1")
        self.assertEqual(smallest.value, 2.0 ** -149)
        self.assertAlmostEqual(smallest.value, 1.401298464324817e-45, places=60)
        self.assertIn("2^(1-127)", smallest.formula)
        self.assertEqual(pc.float_to_bits(smallest.value, 32), 0x00000001)

        # 最小的双精度次正规数：0x0000000000000001 = 2^-1074
        double = pc.decode_bits(0x0000000000000001, 64)
        self.assertTrue(double.is_subnormal, "0x0000000000000001 是双精度次正规数")
        self.assertEqual(double.exponent, -1022)
        self.assertEqual(double.value, 5e-324)
        # 顺带记住：0x0010000000000000 是最小的**正规**双精度数，不是次正规
        smallest_normal = pc.decode_bits(0x0010000000000000, 64)
        self.assertTrue(smallest_normal.is_normal)
        self.assertEqual(smallest_normal.exponent, -1022)
        _show(
            "次正规数",
            [
                f"float32 0x00000001 → {smallest.value!r}（尾数 {smallest.mantissa}，指数 {smallest.exponent}）",
                f"公式 {smallest.formula}",
                f"float64 0x0000000000000001 → {double.value!r}（次正规={double.is_subnormal}，指数 {double.exponent}）",
            ],
        )

    def test_range_checks(self) -> None:
        with self.assertRaises(pc.CalcError):
            pc.float_to_bits(1.0, 16)
        with self.assertRaises(pc.CalcError):
            pc.bits_to_float(-1, 32)
        with self.assertRaises(pc.CalcError):
            pc.bits_to_float(1 << 32, 32)
        with self.assertRaises(pc.CalcError):
            pc.decode_bits(1 << 64, 64)

    def test_float32_overflow_is_reported(self) -> None:
        # 超出 float32 范围时报错，而不是悄悄变成 inf
        with self.assertRaises(pc.CalcError) as ctx:
            pc.float_to_bits(1e300, 32)
        self.assertIn("32", str(ctx.exception))
        self.assertEqual(pc.float_to_bits(1e300, 64), pc.float_to_bits(1e300, 64))
        self.assertTrue(pc.decode_float(1e300, 64).exact)

    def test_number_text(self) -> None:
        self.assertEqual(pc.number_text(350.0), "350")
        self.assertEqual(pc.number_text(-0.5), "-0.5")
        self.assertEqual(pc.number_text(float("inf")), "∞")
        self.assertEqual(pc.number_text(float("nan")), "NaN")


class ScaleTests(unittest.TestCase):
    """定标换算与速查表。"""

    def test_forward_and_backward(self) -> None:
        self.assertEqual(pc.raw_to_engineering(2.0, 0.5, 1.0), 2.0)
        self.assertAlmostEqual(pc.raw_to_engineering(100, 0.1, 0), 10.0)
        self.assertAlmostEqual(pc.engineering_to_raw(10.0, 0.1, 0), 100.0)
        # 双向要能对上：拿上面的结果反算回原始值
        self.assertEqual(pc.engineering_to_raw(pc.raw_to_engineering(1234, 0.25, -5), 0.25, -5), 1234)
        self.assertEqual(pc.raw_to_engineering(0, 0.1, 2.5), 2.5)

    def test_scale_zero_is_an_error(self) -> None:
        with self.assertRaises(pc.CalcError):
            pc.engineering_to_raw(1.0, 0.0, 0.0)

    def test_power_of_two(self) -> None:
        self.assertEqual(pc.power_of_two(-3), 0.125)
        self.assertEqual(pc.power_of_two(10), 1024.0)

    def test_scale_table(self) -> None:
        rows = pc.scale_table()
        by_name = {row.name: row for row in rows}
        self.assertEqual(len(rows), 41 + 5)  # 2^20..2^-20 加 5 个常用系数
        self.assertEqual(by_name["2^0"].factor, 1.0)
        self.assertEqual(by_name["2^20"].text, "1048576")
        self.assertEqual(by_name["2^-1"].hex32, "0x3F000000")
        self.assertEqual(by_name["2^-20"].hex32, "0x35800000")
        # 2 的幂在 float32 里全精确；十进制系数多数不精确 —— 这正是这张表要说的
        for name, row in by_name.items():
            if name.startswith("2^"):
                self.assertTrue(row.exact32, f"{name} 在 float32 里应精确")
        self.assertFalse(by_name["0.1（0..1 系数）"].exact32)
        self.assertEqual(by_name["0.1（0..1 系数）"].hex32, "0x3DCCCCCD")
        self.assertTrue(by_name["0.5（0..1 系数）"].exact32)
        self.assertFalse(by_name["1/3"].exact32)
        _show(
            "定标速查（抽样）",
            [
                f"2^-3 = {by_name['2^-3'].text}（{by_name['2^-3'].hex32}，精确）",
                f"2^-20 = {by_name['2^-20'].text}（{by_name['2^-20'].hex32}）",
                f"0.1 = {by_name['0.1（0..1 系数）'].hex32}（精确表示={by_name['0.1（0..1 系数）'].exact32}）",
                f"共 {len(rows)} 行",
            ],
        )


_app = None
_theme_before = None


def setUpModule() -> None:
    """界面测试需要的 Qt 环境：先建 QApplication，再修数字 locale。"""
    global _app, _theme_before
    from PySide6.QtWidgets import QApplication

    from icomm.ui import qtboot, theme

    _app = QApplication.instance() or QApplication([])
    # zh_HK 下 Qt 会把数字渲染成 CJK 数字（〡〢〇），必须先修掉
    qtboot.prepare_qt()
    _theme_before = theme.appearance()
    theme.apply(theme.Appearance())  # 工具窗口直接读 theme 的模块级颜色常量


def tearDownModule() -> None:
    from icomm.ui import theme

    if _theme_before is not None:
        theme.apply(_theme_before)


class UiTests(unittest.TestCase):
    """界面联动：四个进制栏同步、位网格、位段、浮点、定标。"""

    def setUp(self) -> None:
        from icomm.core.models import Project
        from icomm.ui.tools.base import ToolContext
        from icomm.ui.tools.programmer_calc import ProgrammerCalcTool

        context = ToolContext(project=Project(name="测试"), window=None)
        self.window = ProgrammerCalcTool().build(context)
        self.addCleanup(self._destroy)

    def _destroy(self) -> None:
        self.window.close()
        self.window.deleteLater()

    def _type(self, edit, text: str) -> None:
        """模拟用户在该输入框里打字（发出 textEdited，而不是 setText）。"""
        edit.setText(text)
        edit.textEdited.emit(text)

    def test_tool_metadata(self) -> None:
        from icomm.ui.tools import find_tool, tool_names
        from icomm.ui.tools.programmer_calc import ProgrammerCalcTool

        tool = ProgrammerCalcTool()
        self.assertEqual(tool.name, "程序员计算器")
        self.assertEqual(tool.group, "计算")
        self.assertEqual(tool.order, 10)
        self.assertTrue(tool.non_modal, "要能一边看数据一边算，必须非模态")
        self.assertIn("程序员计算器", tool_names())

        registered = find_tool("程序员计算器")
        self.assertIsNotNone(registered, "工具要登记进注册表")

        # 走框架入口：同一个工具重复打开只应有一个窗口（single_instance）
        from icomm.core.models import Project
        from icomm.ui.tools.base import ToolContext

        context = ToolContext(project=Project(name="测试"), window=None)
        first = registered.launch(context)
        self.assertIsNotNone(first)
        self.addCleanup(self._destroy_widget, first)
        self.assertIs(registered.launch(context), first)

    @staticmethod
    def _destroy_widget(widget) -> None:
        widget.close()
        widget.deleteLater()

    def test_base_edits_are_synced(self) -> None:
        self._type(self.window.hex_edit, "0x43AF0000")
        # 0x43AF0000 = 1135542272：用真实数值核对，不看 format_value 自己的输出
        self.assertEqual(self.window.dec_edit.text(), "1135542272")
        self.assertEqual(self.window.oct_edit.text(), "0o10353600000")
        self.assertEqual(
            self.window.bin_edit.text(), "0100 0011 1010 1111 0000 0000 0000 0000"
        )
        self.assertEqual(self.window.value, 0x43AF0000)

        # 反过来从二进制栏改，其它三栏也要跟上
        self._type(self.window.bin_edit, "1111 0000")
        self.assertEqual(self.window.value, 0xF0)
        self.assertEqual(self.window.hex_edit.text(), "0x000000F0")
        self.assertEqual(self.window.dec_edit.text(), "240")
        self.assertEqual(self.window.oct_edit.text(), "0o00000000360")

        # 位网格状态跟着走
        self.assertTrue(self.window.bit_grid.button(7).isChecked())
        self.assertFalse(self.window.bit_grid.button(8).isChecked())

    def test_signed_only_changes_decimal(self) -> None:
        self._type(self.window.hex_edit, "0xFFFFFFFF")
        self.assertEqual(self.window.dec_edit.text(), "4294967295")
        self.window.sign_combo.setCurrentIndex(1)  # 有符号
        self.assertEqual(self.window.dec_edit.text(), "-1")
        self.assertEqual(self.window.hex_edit.text(), "0xFFFFFFFF")
        self.window.sign_combo.setCurrentIndex(0)
        self.assertEqual(self.window.dec_edit.text(), "4294967295")

    def test_bit_grid_click_flips_bit(self) -> None:
        self.window.bit_grid.button(0).click()
        self.assertEqual(self.window.value, 1)
        self.assertEqual(self.window.hex_edit.text(), "0x00000001")
        self.assertEqual(self.window.dec_edit.text(), "1")

        self.window.bit_grid.button(31).click()
        self.assertEqual(self.window.value, 0x80000001)
        self.assertEqual(self.window.hex_edit.text(), "0x80000001")

        self.window.bit_grid.button(31).click()  # 再点一次翻回来
        self.assertEqual(self.window.value, 1)
        self.assertFalse(self.window.bit_grid.button(31).isChecked())

    def test_invalid_input_keeps_value_and_reports(self) -> None:
        self._type(self.window.hex_edit, "0x1FFFFFFFF")  # 32 位放不下
        self.assertTrue(self.window.base_message.text(), "要给出错误提示")
        self.assertNotEqual(self.window.base_message.text(), "改任意一栏，其余三栏实时同步。")
        self.assertEqual(self.window.value, 0, "报错时不能改动数值")

    def test_width_change_resets_oversized_value(self) -> None:
        self._type(self.window.hex_edit, "0x43AF0000")
        self.window.width_combo.setCurrentIndex(0)  # 8 位
        self.assertEqual(self.window.value, 0)
        self.assertIn("超出", self.window.base_message.text())
        self.assertIsNotNone(self.window.bit_grid.button(7))
        self.assertIsNone(self.window.bit_grid.button(8), "8 位只应有 8 个方格")

        self.window.width_combo.setCurrentIndex(3)  # 64 位
        self.assertEqual(self.window.hex_edit.text(), "0x0000000000000000")
        self.assertIsNotNone(self.window.bit_grid.button(63))
        self.assertEqual(self.window.high_spin.maximum(), 63)

    def test_extract_bits_in_ui(self) -> None:
        self._type(self.window.hex_edit, "0x12345678")
        self.window.high_spin.setValue(15)
        self.window.low_spin.setValue(8)
        self.assertEqual(self.window.extract_label.text(), "86")  # 0x56 = 86
        self.assertEqual(self.window.extract_hex_label.text(), "0x56")
        self.assertEqual(self.window.extract_span_label.text(), "8 位（bit15:8）")
        self.assertEqual(self.window.extract_bits_label.text(), "0101 0110")

        self.window.high_spin.setValue(7)
        self.window.low_spin.setValue(7)
        self.assertEqual(self.window.extract_label.text(), "0")  # bit7 = 0
        self.assertEqual(self.window.extract_bits_label.text(), "0000")

    def test_float_page(self) -> None:
        self._type(self.window.float_input, "350")
        detail = self.window.float32_detail
        self.assertEqual(detail.text_of("位模式 (HEX)"), "0x43AF0000　（1135542272）")
        self.assertEqual(detail.text_of("指数真值"), "8")
        self.assertEqual(detail.text_of("精确表示"), "是")
        self.assertEqual(self.window.float64_detail.text_of("精确表示"), "是")
        self.assertEqual(self.window.float64_detail.text_of("位模式 (HEX)").split("　")[0], "0x4075E00000000000")

        self._type(self.window.float_input, "0.1")
        self.assertEqual(self.window.float32_detail.text_of("位模式 (HEX)").split("　")[0], "0x3DCCCCCD")
        self.assertEqual(self.window.float32_detail.text_of("精确表示"), "否")
        self.assertTrue(self.window.float32_detail.text_of("误差").startswith("1.4901161"))

        # 32 位放不下、64 位放得下：两种精度各算各的，不能因为 32 位报错就不算了
        self._type(self.window.float_input, "1e300")
        self.assertIn("超出", self.window.float_message.text())
        self.assertEqual(self.window.float32_detail.text_of("位模式 (HEX)"), "--")
        self.assertTrue(self.window.float64_detail.text_of("位模式 (HEX)").startswith("0x7E"))

        self._type(self.window.float_input, "abc")
        self.assertIn("abc", self.window.float_message.text())

    def test_float_reverse_and_send_to_bits(self) -> None:
        self._type(self.window.reverse_input, "0x43AF0000")
        self.assertIn("350", self.window.reverse_detail.text_of("位模式 (HEX)") + self.window.reverse_message.text())
        self.assertEqual(self.window.reverse_detail.text_of("精确表示"), "是")

        # 反推结果可以一键送回进制页，接着抠位段
        self.window.send_button.click()
        self.assertEqual(self.window.value, 0x43AF0000)
        self.assertEqual(self.window.tabs.currentIndex(), 0)
        self.assertEqual(self.window.hex_edit.text(), "0x43AF0000")

        self._type(self.window.reverse_input, "1135542272")  # 十进制写法也要认
        self.assertIn("350", self.window.reverse_message.text())

    def test_scale_page(self) -> None:
        self._type(self.window.raw_edit, "100")
        self._type(self.window.scale_edit, "0.1")
        self.assertEqual(self.window.engineering_edit.text(), "10")

        self._type(self.window.engineering_edit, "25")
        self.assertEqual(self.window.raw_edit.text(), "250")

        self._type(self.window.scale_edit, "0")
        self._type(self.window.engineering_edit, "25")
        self.assertIn("缩放系数不能为 0", self.window.scale_message.text())

    def test_scale_table_filled(self) -> None:
        table = self.window.scale_table
        self.assertEqual(table.rowCount(), 46)
        self.assertEqual(table.item(20, 0).text(), "2^0")
        self.assertEqual(table.item(20, 1).text(), "1")
        self.assertEqual(table.item(20, 3).text(), "精确")
        names = [table.item(row, 0).text() for row in range(table.rowCount())]
        self.assertIn("0.1（0..1 系数）", names)
        self.assertEqual(table.item(names.index("0.1（0..1 系数）"), 3).text(), "有误差")


if __name__ == "__main__":
    unittest.main(verbosity=2)
