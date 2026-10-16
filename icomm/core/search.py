"""变量搜索的匹配逻辑（不依赖 Qt，可单测）。

「这个变量在软件里都在哪」需要把三件事拼起来：
1. 点位自己的定义（点号/显示名/地址/类型/分组/说明）；
2. 它挂在哪（设备 → 从站号 → 通道 → 链路）；
3. 它**在界面上出现过的地方**（运行监视页、示波器通道、写了报警、参与示意图、正在持续发送）。

第 3 条是工程师最容易忘的：「我把显示方式改成 none 了，怎么监视页上还有？」
所以这里把"出现位置"一并算出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .models import DeviceDef, PointDef, Project, point_id

#: 链路类型的中文名
LINK_NAMES = {
    "tcp": "TCP",
    "rtu": "RTU",
    "rtu_over_tcp": "RTU over TCP",
}

#: 显示方式的中文名
DISPLAY_NAMES = {
    "none": "不显示",
    "card": "数值卡片",
    "gauge": "仪表",
    "lamp": "指示灯",
    "wave": "波形卡",
}


@dataclass
class SearchHit:
    """一条搜索结果。"""

    pid: str
    point_name: str
    title: str
    device_name: str
    unit_id: int
    channel_name: str
    link: str
    register: str
    address: int
    data_type: str
    unit: str
    group: str
    places: list[str] = field(default_factory=list)
    description: str = ""
    writable: bool = False
    #: 该点位所在链路/设备的问题（例如设备挂在不存在的通道上）
    chain_issues: list[str] = field(default_factory=list)

    def row(self) -> list[Any]:
        return [
            self.point_name,
            self.title,
            self.device_name,
            self.unit_id,
            self.channel_name,
            self.link,
            self.register,
            self.address,
            self.data_type,
            self.unit,
            self.group,
            "、".join(self.places) if self.places else "（未在任何页面显示）",
            self.description,
        ]

    def searchable(self) -> str:
        """参与匹配的全部文本（小写化由调用方处理）。"""
        return " ".join(
            [
                self.pid,
                self.point_name,
                self.title,
                self.device_name,
                self.channel_name,
                self.register,
                str(self.address),
                self.data_type,
                self.unit,
                self.group,
                self.description,
                " ".join(self.places),
                *self.chain_issues,
            ]
        )


def places_of(project: Project, device: DeviceDef, point: PointDef) -> list[str]:
    """这个点位在界面上出现在哪些地方。"""
    places: list[str] = []
    pid = point_id(device.name, point.name)

    if point.display != "none":
        places.append(f"运行监视（{DISPLAY_NAMES.get(point.display, point.display)}）")
    if point.scope:
        places.append("示波器（默认勾选）")
    elif point.is_array:
        places.append("示波器（数组自动可选）")
    if point.is_array and point.record:
        places.append("录波入库")
    if point.alarm.enabled:
        limits = []
        if point.alarm.hi_hi is not None:
            limits.append(f"高高限{point.alarm.hi_hi}")
        if point.alarm.hi is not None:
            limits.append(f"高限{point.alarm.hi}")
        if point.alarm.lo is not None:
            limits.append(f"低限{point.alarm.lo}")
        if point.alarm.lo_lo is not None:
            limits.append(f"低低限{point.alarm.lo_lo}")
        places.append("报警判定" + (f"（{'、'.join(limits)}）" if limits else ""))
    if point.commands:
        places.append("按键控制台（命令按键）")
    elif point.writable and point.bit_labels():
        places.append("按键控制台（可选加）")
    if point.has_write_limit:
        places.append(f"写值限幅（{point.write_limit_text()}）")

    for node in project.diagram.nodes:
        if node.value_point == pid:
            places.append(f"主页示意图（{node.label or node.id}）")
    for hotspot in project.diagram.hotspots:
        if hotspot.target == pid:
            places.append("主页示意图（热区）")

    if project.periodic_write(pid) is not None:
        places.append("已登记持续发送")

    return places


def chain_issues_of(project: Project, device: DeviceDef) -> list[str]:
    """设备/通道层面的问题，搜索时一并提示（现场点表对不上常见原因）。"""
    issues: list[str] = []
    channel = project.channel(device.channel)
    if channel is None:
        issues.append(f"设备挂在通道「{device.channel}」上，但工程里没有这条通道")
    elif not channel.enabled:
        issues.append(f"通道「{channel.name}」已停用")
    if not device.enabled:
        issues.append("设备已停用")
    if channel is not None and channel.link == "rtu":
        port = channel.options.get("port")
        if not port:
            issues.append("RTU 通道没有配串口号")
    return issues


def build_hits(project: Project) -> list[SearchHit]:
    """把整个工程摊平成可搜索的记录。"""
    hits: list[SearchHit] = []
    for device in project.devices:
        channel = project.channel(device.channel)
        issues = chain_issues_of(project, device)
        for point in device.points:
            hits.append(
                SearchHit(
                    pid=point_id(device.name, point.name),
                    point_name=point.name,
                    title=point.title,
                    device_name=device.name,
                    unit_id=device.unit_id,
                    channel_name=device.channel,
                    link=LINK_NAMES.get(channel.link, channel.link) if channel else "（通道缺失）",
                    register=point.register,
                    address=point.address,
                    data_type=point.data_type,
                    unit=point.unit,
                    group=point.group,
                    places=places_of(project, device, point),
                    description=point.description,
                    writable=point.writable,
                    chain_issues=issues,
                )
            )
    return hits


def parse_address_query(text: str) -> int | None:
    """把查询串当地址解析。``0x`` 开头按十六进制，否则按十进制。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        if text.lower().startswith("0x"):
            return int(text, 16)
        return int(text, 10)
    except ValueError:
        return None


def search_points(
    project: Project,
    query: str,
    case_sensitive: bool = False,
    writable_only: bool = False,
    as_address: bool = False,
    limit: int = 2000,
    scope_pids: Iterable[str] | None = None,
) -> list[SearchHit]:
    """按关键字搜点位。

    * 空查询返回全部（让界面直接当"变量总览"用）；
    * ``as_address=True`` 时只按地址精确匹配（点表核对时最常用）；
    * ``scope_pids`` 给的是"当前已经加到示波器"的点号，用来让「出现位置」更准。
    """
    hits = build_hits(project)
    if scope_pids is not None:
        extra = set(scope_pids)
        for hit in hits:
            if hit.pid in extra and not any("示波器" in p for p in hit.places):
                hit.places.append("示波器（手动加入）")

    if writable_only:
        hits = [hit for hit in hits if hit.writable]

    text = (query or "").strip()
    if not text:
        return hits[:limit]

    if as_address:
        wanted = parse_address_query(text)
        if wanted is None:
            return []
        return [hit for hit in hits if hit.address == wanted][:limit]

    needle = text if case_sensitive else text.lower()
    matched: list[SearchHit] = []
    for hit in hits:
        haystack = hit.searchable()
        if not case_sensitive:
            haystack = haystack.lower()
        if needle in haystack:
            matched.append(hit)
    return matched[:limit]
