"""软件级设置。

分两层：

* **软件级**（本模块）—— 界面行为、示波器默认值、通信超时、持续发送默认周期、
  工程仓库位置等，存在 ``<程序目录>/settings.json``，换工程也生效；
* **工程级** —— 归档间隔、保留天数、库与参数表路径、通道连接参数等，
  存在工程文件里（见 :class:`icomm.core.models.Project`），跟着工程走。

软件目录只放软件：工程（配置 / 数据 / 报表）全部放在工程仓库 ``VVPproject`` 里，
位置由 :attr:`AppSettings.project_home` 指定，留空则启动时自动查找。

这里的默认值要保证"什么都不配也能正常用"，避免空配置文件导致行为异常。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from typing import Any

#: 配置文件相对程序目录的位置
SETTINGS_RELATIVE = "settings.json"
#: 老版本的设置文件位置（``data/settings.json``），新版本会读它做一次迁移
LEGACY_SETTINGS_RELATIVE = os.path.join("data", "settings.json")

#: 持续发送周期允许的范围（毫秒）。下限取 50ms，低于这个值在 Modbus 上也发不出来。
PERIODIC_MIN_MS = 50
PERIODIC_MAX_MS = 3_600_000


@dataclass
class AppSettings:
    """软件级设置。"""

    # ---- 外观 ----
    #: 界面外观（配色/字体/字号/密度）。放在核心层只是为了随设置一起存；
    #: 具体结构定义在 icomm.ui.theme，这里按普通字典存，避免核心层依赖 UI。
    appearance: dict[str, Any] = field(default_factory=dict)

    # ---- 工程存放 ----
    #: 工程仓库（VVPproject）目录。留空 = 启动时自己找：
    #: 环境变量 → 从程序目录逐级向外找现成的 VVPproject → 文档目录 → 在软件目录旁新建。
    #: 软件目录本身不存工程，工程与测试结果都放在这里。
    project_home: str = ""
    #: 最近打开的工程；启动时默认重新打开它（命令行参数优先）。
    last_project: str = ""
    #: 最近工程列表，按最近使用顺序排列。
    recent_projects: list[str] = field(default_factory=list)
    #: 输出文件的默认保存位置：project = 当前工程报表目录，custom = 自选目录。
    export_location: str = "project"
    custom_export_directory: str = ""

    # ---- 窗口 ----
    #: 记住窗口大小与位置（关窗口时存下来，下次启动还原）
    remember_window: bool = True
    #: 上次的窗口几何（Qt saveGeometry 的 base64，进制字符串；空 = 没记过）
    window_geometry: str = ""

    # ---- 启动行为 ----
    #: 首启载入哪个内置示例（留空则用第一个示例）
    default_sample: str = ""
    #: 启动后自动拉起内置仿真器（装在现场的机器应关掉）
    auto_start_simulator: bool = False
    #: 启动后自动开始采集
    auto_start_engine: bool = False
    #: 退出时若有未保存的改动，先询问
    confirm_on_exit: bool = True

    # ---- 通信 ----
    #: 断线后的最小重连间隔（秒）
    reconnect_interval_s: float = 3.0
    #: 单次写值的等待上限（秒）
    write_timeout_s: float = 3.0

    # ---- 持续发送 ----
    #: 持续发送的默认周期（毫秒）
    periodic_default_interval_ms: int = 1000
    #: 开始持续发送前二次确认（防止手滑往设备持续写值）
    periodic_confirm: bool = True

    # ---- 示波器与趋势 ----
    #: 示波器默认时基（秒/格）
    scope_timebase_s: float = 5.0
    #: 示波器默认显示全部点位（关掉则只显示工程里标记过"示波器=是"的点位）
    scope_show_all_points: bool = False
    #: 运行监视页每个点位的趋势缓存上限
    trend_max_points: int = 6000

    # ---- 表格与报文 ----
    #: 各列表页默认显示的最大行数
    table_rows_limit: int = 500
    #: 通信监视页保留的报文条数
    monitor_frames_limit: int = 2000

    # ---- 控制台 ----
    #: 用户手动加进设备控制台的点位（默认只显示参数表里带「命令」列的点位，
    #: 那些带位掩码的配置类点位一次展开上百个键太乱，需要点名加）
    panel_points: dict[str, Any] = field(default_factory=dict)

    #: 运行监视页里各分组的显示顺序（分组名 -> 点号列表，中键拖出来的）
    display_order: dict[str, Any] = field(default_factory=dict)

    # ---- 数据 ----
    #: 自动清理过久的历史数据，按工程的「历史保留天数」来（默认开）。
    #: 现场机器一跑几个月，历史库不清理迟早把硬盘写满；
    #: 要留底就把这里关掉（库会一直长大），或者把工程的「历史保留天数」调大。
    archive_auto_purge: bool = True
    #: 自动清理的检查间隔（分钟）
    archive_purge_interval_minutes: int = 30

    # -------------------------------------------------- 存取

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AppSettings":
        """按字段名过滤后构造，忽略多余键以兼容旧版配置文件。"""
        if not data:
            return cls()
        allowed = {f.name for f in fields(cls)}
        known = {k: v for k, v in data.items() if k in allowed}
        # 老版本只有"启动时清理"一个开关，搬到新的自动清理开关上（老用户的选择不丢）
        if "purge_on_start" in data and "archive_auto_purge" not in data:
            known["archive_auto_purge"] = bool(data["purge_on_start"])
        settings = cls(**known)
        settings.clamp()
        return settings

    def clamp(self) -> None:
        """把数值限制在合理区间，避免配置文件里写出离谱的值。"""
        if not isinstance(self.appearance, dict):
            self.appearance = {}
        if not isinstance(self.project_home, str):
            self.project_home = ""
        if not isinstance(self.last_project, str):
            self.last_project = ""
        if not isinstance(self.recent_projects, (list, tuple)):
            self.recent_projects = []
        else:
            seen_projects: set[str] = set()
            recent: list[str] = []
            for path in self.recent_projects:
                if not isinstance(path, str) or not path.strip():
                    continue
                absolute = os.path.abspath(os.path.expanduser(path.strip()))
                key = os.path.normcase(absolute)
                if key not in seen_projects:
                    seen_projects.add(key)
                    recent.append(absolute)
                if len(recent) >= 8:
                    break
            self.recent_projects = recent
        if self.export_location not in ("project", "custom"):
            self.export_location = "project"
        if not isinstance(self.custom_export_directory, str):
            self.custom_export_directory = ""
        self.custom_export_directory = os.path.expanduser(
            self.custom_export_directory.strip()
        )
        if self.export_location == "custom" and not self.custom_export_directory:
            self.export_location = "project"
        if not isinstance(self.window_geometry, str):
            self.window_geometry = ""
        self.remember_window = bool(self.remember_window)
        # panel_points: 设备名 -> [点号]，顺手清掉不合形状的内容
        if not isinstance(self.panel_points, dict):
            self.panel_points = {}
        else:
            self.panel_points = {
                str(device): [str(pid) for pid in pids if isinstance(pid, str)]
                for device, pids in self.panel_points.items()
                if isinstance(pids, (list, tuple))
            }
        self.reconnect_interval_s = _clamp_float(self.reconnect_interval_s, 0.2, 600.0, 3.0)
        self.write_timeout_s = _clamp_float(self.write_timeout_s, 0.2, 60.0, 3.0)
        self.periodic_default_interval_ms = _clamp_int(
            self.periodic_default_interval_ms, PERIODIC_MIN_MS, PERIODIC_MAX_MS, 1000
        )
        self.scope_timebase_s = _clamp_float(self.scope_timebase_s, 0.1, 3600.0, 5.0)
        self.trend_max_points = _clamp_int(self.trend_max_points, 100, 200_000, 6000)
        self.table_rows_limit = _clamp_int(self.table_rows_limit, 20, 100_000, 500)
        self.monitor_frames_limit = _clamp_int(
            self.monitor_frames_limit, 100, 200_000, 2000
        )
        if not isinstance(self.display_order, dict):
            self.display_order = {}
        else:
            self.display_order = {
                str(group): [str(pid) for pid in pids if isinstance(pid, str)]
                for group, pids in self.display_order.items()
                if isinstance(pids, (list, tuple))
            }
        self.archive_auto_purge = bool(self.archive_auto_purge)
        self.archive_purge_interval_minutes = _clamp_int(
            self.archive_purge_interval_minutes, 1, 24 * 60, 30
        )


def _clamp_float(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if number != number:  # NaN
        return fallback
    return min(high, max(low, number))


def _clamp_int(value: Any, low: int, high: int, fallback: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return min(high, max(low, number))


def settings_path(base_dir: str) -> str:
    """软件设置文件路径。"""
    return os.path.join(base_dir, SETTINGS_RELATIVE)


def load_settings(base_dir: str) -> AppSettings:
    """读设置；文件不存在或损坏时回落到默认值（不抛异常，保证程序能起来）。

    新位置 ``<程序目录>/settings.json`` 没有、但老位置
    ``<程序目录>/data/settings.json`` 有时，读老文件（改一次保存就迁到新位置）。
    """
    path = settings_path(base_dir)
    if not os.path.exists(path):
        legacy = os.path.join(base_dir, LEGACY_SETTINGS_RELATIVE)
        if os.path.exists(legacy):
            path = legacy
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return AppSettings()
    if not isinstance(data, dict):
        return AppSettings()
    return AppSettings.from_dict(data)


def save_settings(settings: AppSettings, base_dir: str) -> str:
    """写设置，返回落盘路径。写入用临时文件 + 替换，避免写一半掉电留下坏文件。"""
    path = settings_path(base_dir)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        json.dump(settings.to_dict(), fh, ensure_ascii=False, indent=2)
    os.replace(temp, path)
    return path
