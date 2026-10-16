"""配置表单值转换与驱动默认参数测试。

覆盖两个实际出现过的问题：

1. ``alarm.hi_hi`` 在工程里是整数时，用户输入的 252.5 被截断成 252
   —— 原因是按「当前值的 Python 类型」推断目标类型；
2. tcp 切换成 rtu 后 ``options`` 里残留 int 类型的 ``port``（TCP 端口），
   串口驱动会把它当串口名用。
"""

from __future__ import annotations

import unittest

from icomm.core.drivers import default_options, driver_class
from icomm.ui.pages.devices import ALARM_FIELDS, coerce_value


class CoerceValueTest(unittest.TestCase):
    def test_optfloat_keeps_decimals(self):
        self.assertEqual(coerce_value(252.5, "optfloat"), 252.5)

    def test_optfloat_none_preserved(self):
        self.assertIsNone(coerce_value(None, "optfloat"))

    def test_int_truncates(self):
        self.assertEqual(coerce_value(252.5, "int"), 252)

    def test_float(self):
        value = coerce_value(7, "float")
        self.assertIsInstance(value, float)
        self.assertEqual(value, 7.0)

    def test_bool(self):
        self.assertTrue(coerce_value(1, "bool"))
        self.assertFalse(coerce_value(0, "bool"))

    def test_str(self):
        self.assertEqual(coerce_value(12, "str"), "12")

    def test_choice_passthrough(self):
        self.assertEqual(coerce_value("rtu", "choice"), "rtu")

    def test_alarm_threshold_fields_declared_as_optfloat(self):
        kinds = {spec[0]: spec[2] for spec in ALARM_FIELDS}
        for key in ("hi_hi", "hi", "lo", "lo_lo"):
            self.assertEqual(kinds[key], "optfloat", f"{key} 必须声明为 optfloat")

    def test_regression_int_valued_threshold_not_truncated(self):
        """回归：工程里 hi_hi 存整数时，输入 252.5 不应变成 252。"""
        kind = {spec[0]: spec[2] for spec in ALARM_FIELDS}["hi_hi"]
        self.assertEqual(coerce_value(252.5, kind), 252.5)


class DriverDefaultOptionsTest(unittest.TestCase):
    def test_tcp_defaults(self):
        options = default_options("modbus", "tcp")
        self.assertIn("host", options)
        self.assertEqual(options["port"], 502)
        self.assertNotIn("baudrate", options)

    def test_rtu_defaults(self):
        options = default_options("modbus", "rtu")
        self.assertEqual(options["port"], "COM1")
        self.assertEqual(options["baudrate"], 9600)
        self.assertEqual(options["parity"], "E", "现场默认 9600 8E1（偶校验）")
        self.assertEqual(options["bytesize"], 8)
        self.assertEqual(options["stopbits"], 1)
        self.assertNotIn("host", options)

    def test_rtu_does_not_reuse_tcp_port(self):
        """回归：切到 rtu 后不能把 TCP 端口当串口名。"""
        tcp = dict(default_options("modbus", "tcp"))
        tcp["port"] = 5021
        rtu = default_options("modbus", "rtu")
        self.assertIsInstance(rtu["port"], str)
        self.assertNotEqual(rtu["port"], tcp["port"])

    def test_defaults_cover_every_schema_key(self):
        for link in ("tcp", "rtu"):
            schema_keys = {field.key for field in driver_class("modbus").option_schema(link)}
            self.assertTrue(
                schema_keys <= set(default_options("modbus", link)),
                f"{link} 的默认参数未覆盖全部参数项",
            )

    def test_unknown_driver_falls_back_to_generic_defaults(self):
        options = default_options("not_installed", "tcp")
        self.assertIn("port", options)


if __name__ == "__main__":
    unittest.main()
