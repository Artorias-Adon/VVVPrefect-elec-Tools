"""主回路图的图元库（调色板）。

只放数据：有哪些图元、归哪一类、默认多大、默认写什么字。
真正的画法在 :mod:`icomm.ui.pages.overview` 的 ``DiagramCanvas._draw_symbol`` 里
（一处画、到处用：主页示意图和编辑器的预览是同一个画布）。

图元 ID 就是 ``DiagramNode.symbol``，所以想加图元 = 在这里加一条 + 在
``_draw_symbol`` 里加一个分支，编辑器与主页立刻都能用。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SymbolInfo:
    """一个电气图元。"""

    symbol: str
    label: str
    category: str
    #: 默认尺寸（0~1 归一化）
    w: float = 0.13
    h: float = 0.16
    #: 默认副标题（单位/说明）
    sub_label: str = ""
    description: str = ""
    #: 建议的连接方式：ac=交流、dc=直流、signal=信号（新节点默认用它）
    link: str = "ac"


#: 调色板：按类别排好，编辑器按这个顺序显示
PALETTE: tuple[SymbolInfo, ...] = (
    # 电源
    SymbolInfo("grid", "三相电网", "电源", 0.16, 0.20, "380V AC", "电网/进线", "ac"),
    SymbolInfo("battery", "电池/储能", "电源", 0.15, 0.16, "DC", "电池组、超级电容、储能变流器一侧", "dc"),
    SymbolInfo("pv", "光伏方阵", "电源", 0.16, 0.16, "PV", "光伏输入侧", "dc"),
    # 开关电器
    SymbolInfo("breaker", "断路器", "开关电器", 0.13, 0.13, "", "QF：带保护的开关", "ac"),
    SymbolInfo("switch", "隔离开关", "开关电器", 0.13, 0.13, "", "QS：无保护的刀开关", "ac"),
    SymbolInfo("fuse", "熔断器", "开关电器", 0.11, 0.13, "", "FU：过流熔断", "ac"),
    # 变换与滤波
    SymbolInfo("transformer", "变压器", "变换与滤波", 0.15, 0.18, "", "含隔离变压器、变流器变压器", "ac"),
    SymbolInfo("inductor", "电抗/电感", "变换与滤波", 0.13, 0.13, "L", "进线电抗、滤波电感", "ac"),
    SymbolInfo("capacitor", "电容", "变换与滤波", 0.13, 0.13, "C", "母线电容、滤波电容", "dc"),
    SymbolInfo("filter", "滤波器", "变换与滤波", 0.15, 0.14, "LC", "LC/LCL 滤波环节", "ac"),
    SymbolInfo("resistor", "电阻", "变换与滤波", 0.13, 0.13, "R", "泄放电阻、预充电阻", "dc"),
    SymbolInfo("rectifier", "整流", "变换与滤波", 0.16, 0.18, "AC/DC", "不控/可控整流桥", "ac"),
    SymbolInfo("inverter", "逆变", "变换与滤波", 0.16, 0.18, "DC/AC", "逆变桥、有源前端", "dc"),
    SymbolInfo("dcdc", "DC/DC", "变换与滤波", 0.16, 0.18, "DC/DC", "隔离或非隔离直流变换", "dc"),
    # 母线
    SymbolInfo("bus", "直流母线", "母线", 0.16, 0.20, "DC BUS", "公共直流环节", "dc"),
    # 负载
    SymbolInfo("load", "负载", "负载", 0.14, 0.16, "", "一般负载/后级", "dc"),
    SymbolInfo("motor", "电动机", "负载", 0.15, 0.16, "M", "拖动类负载", "ac"),
    # 测量与控制
    SymbolInfo("meter", "计量/仪表", "测量与控制", 0.13, 0.15, "METER", "电度表、变送器", "signal"),
    SymbolInfo("dsp", "控制 DSP", "测量与控制", 0.16, 0.18, "DSP", "控制器：可绑定设备详情", "signal"),
    SymbolInfo("text", "文字标注", "测量与控制", 0.14, 0.10, "", "纯说明文字，顶部可叠实时值", "signal"),
)

#: 连线线型
LINK_STYLES: tuple[tuple[str, str], ...] = (
    ("ac", "交流（黄）"),
    ("dc", "直流（蓝）"),
    ("signal", "信号（灰虚线）"),
)

LINK_LABELS = dict(LINK_STYLES)


def categories() -> list[str]:
    """调色板的分类顺序（保持 PALETTE 里的先后）。"""
    seen: list[str] = []
    for item in PALETTE:
        if item.category not in seen:
            seen.append(item.category)
    return seen


def by_category() -> dict[str, list[SymbolInfo]]:
    grouped: dict[str, list[SymbolInfo]] = {}
    for item in PALETTE:
        grouped.setdefault(item.category, []).append(item)
    return grouped


def info(symbol: str) -> SymbolInfo | None:
    for item in PALETTE:
        if item.symbol == symbol:
            return item
    return None


def known_symbols() -> list[str]:
    return [item.symbol for item in PALETTE]


def suggest_label(symbol: str, existing: list[str]) -> tuple[str, str, str]:
    """新节点的 ``(id, 主标题, 副标题)``。

    id 用图元 ID + 序号（``rectifier2``），避免和已有节点撞名 ——
    连线是按 id 指过去的，撞名会让线连到错的节点上。
    """
    item = info(symbol)
    base = symbol
    index = 1
    while f"{base}{index}" in existing:
        index += 1
    return f"{base}{index}", (item.label if item else symbol), (item.sub_label if item else "")


def auto_layout(count: int, *, left: float = 0.04, step: float = 0.16) -> list[tuple[float, float]]:
    """一排从左到右的坐标（``count`` 个）。图元多了自动折第二行。"""
    positions: list[tuple[float, float]] = []
    per_row = max(1, int((1.0 - left - 0.02) // step))
    for index in range(count):
        row, column = divmod(index, per_row)
        positions.append((round(left + column * step, 4), round(0.42 + row * 0.22, 4)))
    return positions
