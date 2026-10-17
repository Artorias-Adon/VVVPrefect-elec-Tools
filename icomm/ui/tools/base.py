"""工具框架：把「工具」做成可插拔的。

**新增一个工具** = 在本目录新建一个模块，继承 :class:`Tool`，用 ``@register_tool``
装饰，然后在 ``icomm/ui/tools/__init__.py`` 里 import 一行。
工具栏的「工具」菜单、分组、快捷键、说明全部由注册表生成，不用改界面代码。

工具通过 :class:`ToolContext` 拿到需要的东西（工程、实时值、写值、日志……），
**不直接依赖 MainWindow**，所以工具可以单独测、也可以被别的宿主复用。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

import os

from PySide6.QtWidgets import QDialog, QWidget

from ...core.models import Project
from ...core.tags import TagValue

# --------------------------------------------------------------- 上下文


@dataclass
class ToolContext:
    """工具能用到的东西。

    只暴露必要的接口，工具不去翻主窗口内部结构 —— 这样主窗口怎么改都不会
    把工具弄坏，反过来也一样。
    """

    project: Project
    #: 父窗口（工具自己 new 控件时当 parent 用）
    window: QWidget
    #: 软件设置（工具要记自己的偏好就写进去，再调 save_settings）
    settings: Any = None
    #: 取实时值
    tag_of: Callable[[str], TagValue] = lambda pid: TagValue(pid=pid)
    #: 单发写值（``(点号, 值)``），走引擎的写值队列
    write: Callable[[str, Any], None] | None = None
    #: 写一条日志（界面状态栏 + 通信日志）
    log: Callable[[str, str], None] | None = None
    #: 采集是否在跑
    engine_running: Callable[[], bool] = lambda: False
    #: 程序基准目录（工具要存文件就放这下面）
    base_dir: str = ""
    #: 本工程的「报表」目录（测试结果放这里，和工程配置分开）
    report_dir: str = ""
    #: 存储（要读历史的工具用得上）
    storage: Any = None
    #: 把设置落盘（工具改了 settings 后调一下）
    save_settings: Callable[[], None] | None = None
    #: 工程内容变了（工具改了工程后调一下，让界面刷新）
    project_changed: Callable[[], None] | None = None
    #: 跳去某个点位（变量搜索之类的工具会用）
    reveal_point: Callable[[str], None] | None = None
    #: 把某个点位加到示波器
    add_to_scope: Callable[[str], None] | None = None
    #: 给某个点位设置写值限幅（工具/右键菜单用）
    set_write_limit: Callable[[str, Any, Any], None] | None = None
    #: 改某个点位的小数位数
    set_decimals: Callable[[str, int], None] | None = None
    #: 取某个点位的显示名（任务命名、列表显示用）
    title_of: Callable[[str], str] | None = None
    #: 把某个工具钉进左侧栏（主窗口提供；没有就只显示提示）
    pin_tool: Callable[[str], Any] | None = None
    #: 工具之间共享的字典：让自动化核心这类"服务"能被各工具取用
    services: dict[str, Any] = field(default_factory=dict)

    def report(self, message: str, level: str = "info") -> None:
        if self.log is not None:
            self.log(level, message)

    def send(self, pid: str, value: Any) -> bool:
        """写值；未接引擎时返回 False（工具应据此提示用户先启动采集）。"""
        if self.write is None:
            return False
        self.write(pid, value)
        return True

    def service(self, name: str) -> Any:
        return self.services.get(name)

    def report_subdir(self, kind: str) -> str:
        """取（并创建）本工程的"测试结果"分类目录，例如 ``<工程>/reports/shots``。

        工程没有固定位置时退回软件目录，保证工具总能存下东西。
        """
        from ...core import project_store

        root = self.report_dir or self.base_dir or "."
        if getattr(self.settings, "export_location", "project") == "custom":
            root = getattr(self.settings, "custom_export_directory", "") or root
        target = os.path.join(root, project_store.report_kind_dir(kind))
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            return root
        return target

    def output_path(self, kind: str, filename: str) -> str:
        """Return a configured output path and ensure its directory exists."""
        return os.path.join(self.report_subdir(kind), filename)

    def point_ids(self) -> list[str]:
        return self.project.all_point_ids()

    def writable_points(self) -> list[tuple[str, str]]:
        """``[(点号, 显示名)]``，工具的"选点位"下拉框用它。"""
        return [
            (f"{device.name}.{point.name}", f"{device.name}.{point.title}")
            for device, point in self.project.iter_points()
            if point.writable
        ]

    def all_points(self) -> list[tuple[str, str]]:
        """``[(点号, 显示名)]``，含只读点位。"""
        return [
            (f"{device.name}.{point.name}", f"{device.name}.{point.title}")
            for device, point in self.project.iter_points()
        ]


# --------------------------------------------------------------- 工具基类


class Tool(ABC):
    """一个工具。

    子类至少要给出 ``name`` 与 :meth:`open`。
    """

    #: 菜单里显示的名字
    name: str = ""
    #: 菜单分组（同组的排在一起）
    group: str = "其它"
    #: 菜单项提示
    description: str = ""
    #: 排序用（同组内按它排）
    order: int = 100
    #: True = 非模态浮动窗口（可和其它工具/主窗口同时用）；False = 模态对话框
    non_modal: bool = False
    #: 同一个工具是否只允许开一个窗口
    single_instance: bool = True

    #: 由框架填：上一次打开的窗口（single_instance 复用与防重复）
    _window: QWidget | None = None

    @abstractmethod
    def open(self, context: ToolContext) -> QWidget | None:
        """创建并返回要显示的控件。返回 None 表示什么都不显示（例如先弹了提示）。"""

    # -------------------------------------------------- 便捷

    def reuse(self, context: ToolContext) -> QWidget | None:
        """已开过且还活着就复用（置顶并返回它）。"""
        if not self.single_instance:
            return None
        window = type(self)._window
        if window is None:
            return None
        try:
            if not window.isVisible():
                return None
            window.raise_()
            window.activateWindow()
        except RuntimeError:
            type(self)._window = None
            return None
        return window

    def remember(self, window: QWidget | None) -> None:
        if window is None:
            return
        type(self)._window = window
        # 窗口销毁后清掉引用，免得下次拿到已释放的对象
        window.destroyed.connect(lambda *_a, cls=type(self): setattr(cls, "_window", None))

    def launch(self, context: ToolContext) -> QWidget | None:
        """框架调用这个入口（会处理复用）。"""
        existing = self.reuse(context)
        if existing is not None:
            return existing
        window = self.open(context)
        self.remember(window)
        return window

    def __str__(self) -> str:
        return f"{self.name}　{self.description}" if self.description else self.name


class DialogTool(Tool):
    """模态对话框类工具：:meth:`build` 只负责造对话框，框架负责 ``exec()``。"""

    @abstractmethod
    def build(self, context: ToolContext) -> QDialog:
        """创建对话框（不要自己 exec）。"""

    def open(self, context: ToolContext) -> QWidget | None:
        dialog = self.build(context)
        from ..dialogs.sizing import fit_to_available_screen

        fit_to_available_screen(dialog)
        dialog.exec()
        return None


class WindowTool(Tool):
    """非模态浮动窗口类工具：:meth:`build` 造窗口，框架负责 ``show()``。"""

    non_modal = True

    @abstractmethod
    def build(self, context: ToolContext) -> QWidget:
        """创建窗口（不要自己 show）。"""

    def open(self, context: ToolContext) -> QWidget | None:
        window = self.build(context)
        from ..dialogs.sizing import fit_to_available_screen
        from ..tool_dock import install_page_grip

        # 顶上挂一条"拖到左侧栏"，把复杂工具钉成主窗口的一个页面
        install_page_grip(window, self.name, on_pin=getattr(context, "pin_tool", None))
        fit_to_available_screen(window)
        window.show()
        return window


# --------------------------------------------------------------- 注册表

_TOOLS: list[Tool] = []


def register_tool(tool: Tool | type[Tool]) -> Tool | type[Tool]:
    """注册一个工具。

    ``@register_tool`` 直接装饰在**类**上最顺手，所以两种写法都收：
    给类就实例化一份再注册（工具本身不含状态，共用一个实例即可，
    这样 ``single_instance`` 的"复用已开窗口"才有意义）；给实例就原样注册。

    同名重复注册会被忽略，方便热重载。
    """
    if isinstance(tool, type):
        tool = tool()
    if not tool.name:
        raise ValueError(f"{type(tool).__name__} 必须声明 name")
    if any(existing.name == tool.name for existing in _TOOLS):
        return tool
    _TOOLS.append(tool)
    return tool


def tools() -> list[Tool]:
    """全部工具，按 (分组, 组内顺序, 名字) 排好。"""
    return sorted(_TOOLS, key=lambda t: (t.group, t.order, t.name))


def tools_by_group() -> dict[str, list[Tool]]:
    """按分组归类，保持顺序（供菜单生成）。"""
    grouped: dict[str, list[Tool]] = {}
    for tool in tools():
        grouped.setdefault(tool.group, []).append(tool)
    return grouped


def find_tool(name: str) -> Tool | None:
    for tool in _TOOLS:
        if tool.name == name:
            return tool
    return None


def tool_names() -> list[str]:
    return [tool.name for tool in tools()]
