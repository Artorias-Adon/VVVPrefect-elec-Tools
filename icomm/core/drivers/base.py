"""驱动抽象层。

新增一种协议只需：
1. 在本目录新建 ``xxx.py``；
2. 继承 :class:`ChannelDriver`，声明 ``driver_name`` / ``link_types`` / ``option_schema``；
3. 用 ``@register_driver`` 装饰；
4. 在 ``drivers/__init__.py`` 末尾 import 该模块。

界面会根据 ``option_schema`` 自动生成连接参数表单，无需改动 UI 代码。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..models import ChannelDef, DeviceDef, PointDef, default_channel_options

FrameCallback = Callable[[str, str], None]
LogCallback = Callable[[str, str], None]
#: 驱动侧主动上送值：``{点号: 值}``。
#: 轮询型协议（Modbus/CAN）用不到，但 IEC 61850 的报告是设备推上来的，
#: 不走"问一次答一次"，只能由驱动主动交给引擎。
ValueCallback = Callable[[dict[str, Any]], None]


class DriverError(Exception):
    """驱动层通用异常。"""


class DriverNotOpen(DriverError):
    """连接未建立。"""


@dataclass(frozen=True)
class OptionField:
    """驱动连接参数的一项，用于自动生成配置界面。"""

    key: str
    label: str
    #: str | int | float | choice | bool | port（port = 可下拉选也能手打的串口号）
    type: str = "str"
    default: Any = ""
    choices: tuple[Any, ...] = ()
    #: choice 的显示名（与 choices 一一对应；不写就直接显示值）
    choice_labels: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    hint: str = ""


@dataclass
class PointResult:
    """一个点位的读取结果（value 已换算为工程量）。"""

    tag: str
    value: float | bool | None = None
    ok: bool = True
    error: str = ""


class ChannelDriver(ABC):
    """通道级驱动：管理一条物理链路，服务其上的多台设备。

    一台 Modbus TCP 网关下挂多个 ``unit_id``，或一条 RS485 总线上的多台
    仪表，都对应「一个通道、多台设备」。
    """

    #: 驱动标识，工程文件中 ``ChannelDef.driver`` 存的就是它
    driver_name: str = "base"
    #: 支持的链路类型，用于界面下拉框
    link_types: tuple[str, ...] = ()
    #: 中文描述
    description: str = ""
    #: 是否支持同时挂多台从站设备
    multi_device: bool = True

    def __init__(self, channel: ChannelDef) -> None:
        self.channel = channel
        self._on_frame: FrameCallback | None = None
        self._on_log: LogCallback | None = None
        self._on_values: ValueCallback | None = None
        #: 整个工程（有的协议要用它，比如 61850 报告回填要靠点位表建映射）
        self._project = None

    # ------------------------------------------------------ 回调

    def set_callbacks(
        self,
        on_frame: FrameCallback | None = None,
        on_log: LogCallback | None = None,
        on_values: ValueCallback | None = None,
    ) -> None:
        """注入报文回调、日志回调与"主动上送值"回调。

        ``on_frame(direction, text)`` 的 direction 取 ``"TX"`` 或 ``"RX"``。
        ``on_log(level, message)`` 的 level 取 ``"info"``/``"warning"``/``"error"``。
        ``on_values({点号: 值})`` 供**设备主动上送**的协议使用（IEC 61850 报告）。
        回调可能在驱动的接收线程里被调用，实现方不要在里面做重活。
        """
        self._on_frame = on_frame
        self._on_log = on_log
        self._on_values = on_values

    def emit_frame(self, direction: str, text: str) -> None:
        if self._on_frame:
            self._on_frame(direction, text)

    def emit_log(self, level: str, message: str) -> None:
        if self._on_log:
            self._on_log(level, message)

    def emit_values(self, values: dict[str, Any]) -> None:
        """把设备主动上送的值交给引擎（报告、周期帧等）。"""
        if values and self._on_values:
            self._on_values(values)

    def set_project(self, project) -> None:
        """注入整个工程。

        轮询型协议用不到；但"设备主动上送"的协议（IEC 61850 报告）要靠
        全工程的点位表把上送的对象引用映射回点号 —— 只靠"读过哪些点位"不够，
        报告里可能带着点表里没在轮询的对象。
        """
        self._project = project

    # ------------------------------------------------------ 生命周期

    @abstractmethod
    def open(self) -> None:
        """建立连接，失败需抛 :class:`DriverError`。"""

    @abstractmethod
    def close(self) -> None:
        """释放连接，允许重复调用。"""

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """连接是否可用。"""

    # ------------------------------------------------------ 数据读写

    @abstractmethod
    def read_points(self, device: DeviceDef, points: Iterable[PointDef]) -> dict[str, PointResult]:
        """批量读取点位，返回 ``{点号: PointResult}``。

        实现方内部应做批量合并读；单个点位失败不应影响其它点位。
        """

    @abstractmethod
    def write_point(self, device: DeviceDef, point: PointDef, value: Any) -> None:
        """写单个点位，失败需抛 :class:`DriverError`。"""

    def describe(self) -> str:
        return f"{self.driver_name}@{self.channel.name}"

    # ------------------------------------------------------ 参数校验

    @classmethod
    def option_schema(cls, link: str) -> list[OptionField]:
        """某链路类型下的连接参数定义，默认返回空。"""
        return []

    @classmethod
    def point_areas(cls) -> list[tuple[str, str]]:
        """本协议里「寄存器区」这一列可选哪些，元素是 ``(值, 中文名)``。

        Modbus 是 coil/discrete/holding/input；IEC 61850 是 FC（MX/ST/…）；
        CAN 是 can_rx/can_tx/can_sdo。界面按当前通道的驱动来填这一列的下拉框，
        所以同一个字段在不同协议里有不同的含义 —— 都是"这个点位落在协议的哪一块"。
        返回空表示沿用 Modbus 的四个区（兼容老工程）。
        """
        return []

    @classmethod
    def point_area_hint(cls) -> str:
        """点位表单里给"寄存器区 / 协议区"的一句说明（可为空）。"""
        return ""


# --------------------------------------------------------------- 驱动注册表

_REGISTRY: dict[str, type[ChannelDriver]] = {}


def register_driver(cls: type[ChannelDriver]) -> type[ChannelDriver]:
    """类装饰器：把驱动注册进全局表。"""
    if not cls.driver_name or cls.driver_name == "base":
        raise ValueError(f"{cls.__name__} 必须声明 driver_name")
    _REGISTRY[cls.driver_name] = cls
    return cls


def driver_names() -> list[str]:
    return sorted(_REGISTRY)


def driver_class(name: str) -> type[ChannelDriver] | None:
    return _REGISTRY.get(name)


def create_driver(channel: ChannelDef) -> ChannelDriver:
    """按通道配置实例化驱动。"""
    cls = _REGISTRY.get(channel.driver)
    if cls is None:
        raise DriverError(
            f"未安装协议驱动 '{channel.driver}'，已注册驱动：{', '.join(driver_names()) or '无'}"
        )
    return cls(channel)


def driver_catalog() -> list[dict[str, Any]]:
    """给界面用的驱动清单。"""
    return [
        {
            "name": cls.driver_name,
            "description": cls.description,
            "link_types": list(cls.link_types),
            "multi_device": cls.multi_device,
        }
        for cls in (_REGISTRY[n] for n in driver_names())
    ]


def default_options(driver_name: str, link: str) -> dict[str, Any]:
    """某驱动在某链路类型下的默认连接参数。

    优先用驱动自己声明的参数规格，这样新增驱动无需再改界面代码，
    切换驱动/链路时也能自动得到正确的参数集（不会残留上一套参数）。
    """
    cls = _REGISTRY.get(driver_name)
    if cls is not None:
        schema = cls.option_schema(link)
        if schema:
            return {field.key: field.default for field in schema}
    return default_channel_options(link)
