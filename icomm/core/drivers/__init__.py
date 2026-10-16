"""驱动注册入口。

新增协议：新建模块并在末尾按 ``modbus`` 的方式 import 即可。
"""

from .base import (
    ChannelDriver,
    DriverError,
    DriverNotOpen,
    OptionField,
    PointResult,
    create_driver,
    default_options,
    driver_catalog,
    driver_class,
    driver_names,
    register_driver,
)
from . import modbus  # noqa: F401  —— 触发注册
from . import can  # noqa: F401  —— 触发注册
from . import iec61850  # noqa: F401  —— 触发注册

__all__ = [
    "ChannelDriver",
    "DriverError",
    "DriverNotOpen",
    "OptionField",
    "PointResult",
    "create_driver",
    "default_options",
    "driver_catalog",
    "driver_class",
    "driver_names",
    "register_driver",
    "can",
    "iec61850",
    "modbus",
]
