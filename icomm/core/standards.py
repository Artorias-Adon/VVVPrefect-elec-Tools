"""电气/工业监控相关的国家标准与行业标准索引，以及"本地标准库"。

两件事：

1. **索引**：列出和本软件对得上的标准（电能质量、通信协议、变配电与保护、
   电能计量、储能与光伏、安全与接地），带编号、名称和"跟软件的哪块对应"。
2. **本地库**：用户自己（从正规渠道）拿到的标准正文，可以导入到工程的
   ``standards/`` 目录里，跟工程一起保存、一起搬走，在「关于」里直接打开。

.. important::
   **软件不附带任何标准正文**。GB/GB-T 由国家标准化管理委员会发布，
   DL/T 由电力行业标准化管理机构发布，正文版权归发布机构 —— 打包进安装包
   属于侵权。所以这里只做"条目索引 + 官方入口"，正文请用户通过
   「国家标准全文公开系统」在线阅读或走正规渠道获取，再自行导入本地库。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from urllib.parse import quote

#: 本地标准库目录名（放在工程根下，跟着工程走；磁盘上一律英文，界面显示"标准"）
STANDARDS_DIR = "standards"

#: 国家标准全文公开系统（GB/GB-T 可免费在线阅读）
OPENSTD_HOME = "https://openstd.samr.gov.cn/bzgk/gb/index"
#: 全国标准信息公共服务平台（查行业标准 DL/T、JJG 等的现行状态）
STD_SERVICE_HOME = "https://std.samr.gov.cn/"

#: 清单分组顺序（界面按这个顺序显示，跟本工程相关的组会排到最前并标 ★）
GROUPS = ("电能质量", "通信协议", "变配电与保护", "电能计量", "储能与光伏", "安全与接地")

#: 工程名里出现这些词时，对应的标准组算"跟本工程相关"
DOMAIN_GROUPS: dict[str, tuple[str, ...]] = {
    "储能": ("储能与光伏", "电能计量", "通信协议"),
    "电池": ("储能与光伏", "电能计量"),
    "光伏": ("储能与光伏", "电能质量", "通信协议"),
    "风电": ("储能与光伏", "电能质量", "通信协议"),
    "变压": ("变配电与保护", "电能质量"),
    "配电": ("变配电与保护", "电能质量"),
    "电网": ("电能质量", "变配电与保护", "通信协议"),
    "整流": ("电能质量", "通信协议"),
    "逆变": ("电能质量", "通信协议"),
    "SST": ("电能质量", "通信协议"),
    "modbus": ("通信协议",),
    "61850": ("通信协议",),
    "s7": ("通信协议",),
}


@dataclass(frozen=True)
class StandardRef:
    """一条标准索引（**只有元数据，不含正文**）。"""

    code: str
    title: str
    group: str
    #: 跟软件哪块对得上（现场判断"这条标准管什么"用）
    note: str

    @property
    def is_gb(self) -> bool:
        return self.code.upper().startswith("GB")


#: 和本软件功能对得上的标准（编号 + 名称是公开的条目信息；正文不在软件里）
STANDARDS: tuple[StandardRef, ...] = (
    # ---------------- 电能质量
    StandardRef("GB/T 14549-1993", "电能质量 公用电网谐波", "电能质量",
                "谐波分析工具算的 THD/各次谐波在这里有允许值（电压总畸变率分 0.38kV/10kV/35kV 等）"),
    StandardRef("GB/T 12325-2008", "电能质量 供电电压偏差", "电能质量",
                "母线电压的上下限报警可照这份设（35kV 及以上 ±10%、10kV ±7%、220V +7%/-10%）"),
    StandardRef("GB/T 15945-2008", "电能质量 电力系统频率偏差", "电能质量",
                "锁相/频率监视的允许偏差（正常 ±0.2Hz，小网 ±0.5Hz）"),
    StandardRef("GB/T 15543-2008", "电能质量 三相电压不平衡", "电能质量",
                "三相向量工具算的不平衡度限值（负序 2%、短时 4%）"),
    # ---------------- 通信协议
    StandardRef("GB/T 19582.1-2008", "基于 Modbus 协议的工业自动化网络规范 第1部分：概述", "通信协议",
                "Modbus 驱动的报文与 CRC 工具、数据模型都在这套规范的框架里"),
    StandardRef("GB/T 19582.2-2008", "基于 Modbus 协议的工业自动化网络规范 第2部分：串行链路实现指南", "通信协议",
                "RTU 串口（9600 8E1 之类）的帧格式与 CRC16 就在这里"),
    StandardRef("GB/T 19582.3-2008", "基于 Modbus 协议的工业自动化网络规范 第3部分：TCP/IP 实现指南", "通信协议",
                "Modbus TCP 的 MBAP 头、单元标识与端口号"),
    StandardRef("DL/T 860", "变电站通信网络和系统（等同采用 IEC 61850）", "通信协议",
                "IEC 61850 驱动的对象引用（LD/LN/DO/DA）、报告与控制模型"),
    StandardRef("DL/T 634.5104", "远动设备及系统 第5-104部分：传输规约（IEC 60870-5-104）", "通信协议",
                "现场调度常用的 104 规约（本软件暂未内置该驱动，索引留作接线/点表对照）"),
    StandardRef("DL/T 645", "多功能电能表通信协议", "通信协议",
                "电表串口抄表协议（本软件未内置；抄表数据可先落 CSV 再导入点表）"),
    # ---------------- 变配电与保护
    StandardRef("GB/T 50062", "电力装置的继电保护和自动装置设计规范", "变配电与保护",
                "保护动作字、遥控出口这些点位怎么定义，可照这份规范对齐命名"),
    StandardRef("GB/T 50063", "电力装置的电测量仪表装置设计规范", "变配电与保护",
                "测点配置（三相电压电流、有功无功、功率因数、电能）与准确级要求"),
    StandardRef("GB 50054", "低压配电设计规范", "变配电与保护",
                "0.4kV 侧的接线与保护配置，主回路图的拓扑对照"),
    StandardRef("GB 50150", "电气装置安装工程 电气设备交接试验标准", "变配电与保护",
                "交接试验与投运前的测量项目（录波/趋势可以用来留证）"),
    # ---------------- 电能计量
    StandardRef("GB/T 17215.321", "电测量设备（交流）特殊要求 第21部分：静止式有功电能表", "电能计量",
                "「电能与电费」读的累计电量点、准确级与电量含义"),
    StandardRef("DL/T 448", "电能计量装置技术管理规程", "电能计量",
                "计量点设置、CT/PT 倍率（软件里的「倍率」字段）与计量装置管理"),
    StandardRef("JJG 596", "电子式交流电能表检定规程", "电能计量",
                "现场比对电表读数时的检定依据（计量器具不是随便标定）"),
    # ---------------- 储能与光伏
    StandardRef("GB/T 36547", "电化学储能系统接入电网技术规定", "储能与光伏",
                "储能并网的电能质量、功率控制与保护要求（储能示例工程的对照）"),
    StandardRef("GB/T 34120", "电化学储能系统储能变流器技术规范", "储能与光伏",
                "PCS 的遥测遥控点表（有功/无功给定、状态字、故障字）与响应时间"),
    StandardRef("GB/T 19964", "光伏发电站接入电力系统技术规定", "储能与光伏",
                "光伏并网的电能质量、功率预测与低电压穿越（光伏示例工程的对照）"),
    # ---------------- 安全与接地
    StandardRef("GB/T 50065", "交流电气装置的接地设计规范", "安全与接地",
                "接地与等电位做法；现场查干扰、查地电位差时用得上"),
    StandardRef("GB 19517", "电气设备安全技术规范", "安全与接地",
                "电气设备安全通则（远程操作前的安全确认要求）"),
)


def by_group() -> dict[str, list[StandardRef]]:
    """按分组归好类，保持 :data:`GROUPS` 的顺序（空组不出现）。"""
    grouped: dict[str, list[StandardRef]] = {}
    for group in GROUPS:
        items = [item for item in STANDARDS if item.group == group]
        if items:
            grouped[group] = items
    return grouped


def search_url(code: str) -> str:
    """在官方系统里查这条标准的入口（GB 走全文公开系统，其它走标准信息平台）。"""
    body = (code or "").strip()
    if body.upper().startswith("GB"):
        return (
            "https://openstd.samr.gov.cn/bzgk/gb/std_list"
            "?p.p1=0&p.p90=circulation_date&p.p91=desc&p.p2=" + quote(body, safe="")
        )
    return STD_SERVICE_HOME


def relevant_groups(project_name: str) -> list[str]:
    """工程名里带了哪些域（储能/配电/光伏…）→ 对得上的标准组。"""
    text = str(project_name or "")
    if not text:
        return []
    out: list[str] = []
    for keyword, groups in DOMAIN_GROUPS.items():
        if keyword.lower() in text.lower():
            for group in groups:
                if group not in out:
                    out.append(group)
    return out


def ordered_groups(project_name: str = "") -> list[tuple[str, bool]]:
    """分组显示顺序：跟本工程相关的排前面，返回 ``[(组名, 是否相关)]``。"""
    relevant = relevant_groups(project_name)
    names = list(by_group())
    names.sort(key=lambda group: (0 if group in relevant else 1, GROUPS.index(group)))
    return [(group, group in relevant) for group in names]


def library_dir(root: str, base_dir: str = "", create: bool = False) -> str:
    """本地标准库目录：``<工程根>/standards``；工程没有目录时退回程序目录。"""
    base = root or base_dir or os.getcwd()
    folder = os.path.join(base, STANDARDS_DIR)
    if create:
        os.makedirs(folder, exist_ok=True)
    return folder


def scan_files(folder: str) -> list[dict]:
    """列出标准库里已有的文件（没有目录就是空的，不算错）。"""
    if not folder or not os.path.isdir(folder):
        return []
    entries: list[dict] = []
    for name in sorted(os.listdir(folder), key=str.lower):
        if name.startswith("."):
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        entries.append(
            {
                "name": name,
                "path": path,
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "kind": os.path.splitext(name)[1].lstrip(".").upper() or "文件",
            }
        )
    return entries


def unique_path(folder: str, name: str) -> str:
    """同名文件不覆盖：``xxx.pdf`` 已存在就存成 ``xxx (2).pdf``。"""
    target = os.path.join(folder, name)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(name)
    index = 2
    while True:
        candidate = os.path.join(folder, f"{stem} ({index}){ext}")
        if not os.path.exists(candidate):
            return candidate
        index += 1


def import_file(source: str, folder: str) -> str:
    """把一个文件复制进标准库，返回存下来的路径。

    只复制、不动源文件；同名不覆盖（现场"这份是甲方给的版本"不能悄悄被覆盖）。
    """
    src = os.path.abspath(source or "")
    if not src or not os.path.isfile(src):
        raise OSError(f"找不到要导入的文件：{source}")
    os.makedirs(folder, exist_ok=True)
    target = unique_path(folder, os.path.basename(src))
    shutil.copy2(src, target)
    return target


def format_size(size: int) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GB"


def format_time(stamp: float) -> str:
    import time as _time

    try:
        return _time.strftime("%Y-%m-%d %H:%M", _time.localtime(float(stamp)))
    except (TypeError, ValueError, OSError):
        return ""


def file_filter() -> str:
    return (
        "标准文件 (*.pdf *.doc *.docx *.xls *.xlsx *.txt *.md *.rtf *.zip *.rar *.7z)"
        ";;PDF (*.pdf);;所有文件 (*.*)"
    )


DISCLAIMER = (
    "<b>软件不附带任何标准正文</b>：GB/GB-T 由国家标准化管理委员会发布，"
    "DL/T、JJG 由相应行业机构发布，正文版权归发布机构。"
    "上表只是条目索引；正文请用「在官方系统查看」去国家标准全文公开系统在线阅读，"
    "或走正规渠道拿到后导入下面的本地库 —— 导入的文件只存在这个工程的 "
    "<code>standards/</code> 目录里，不会上传到任何地方。"
)


__all__ = [
    "DISCLAIMER",
    "DOMAIN_GROUPS",
    "GROUPS",
    "OPENSTD_HOME",
    "STANDARDS",
    "STANDARDS_DIR",
    "STD_SERVICE_HOME",
    "StandardRef",
    "by_group",
    "file_filter",
    "format_size",
    "format_time",
    "import_file",
    "library_dir",
    "ordered_groups",
    "relevant_groups",
    "scan_files",
    "search_url",
    "unique_path",
]
