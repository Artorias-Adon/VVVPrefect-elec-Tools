"""报告生成（骨架）：把工程、设备、点位、实时值、通信状态、报警拼成一份可打印的文档。

**内容还没定稿**，所以先把结构立住，以后加内容不需要动别的文件：

* 每个小节是一个 :class:`Section`（key / 标题 / 说明 / 取数函数）；
* 取数函数只产出**数据**（键值对 + 表格 + 段落），不碰任何 HTML；
  渲染交给 :meth:`Report.to_html` / :meth:`Report.to_markdown`。
  以后想换模板（Word/PDF/Excel）只加一个渲染器，小节一行都不用改；
* 报告本身是纯数据，不依赖 Qt、不依赖界面状态 —— 可以单测，
  也可以被未来的外部脚本接口（``icomm.api``）直接调用来出报告。

加一节的方法：写一个 ``def _my_section(ctx) -> SectionResult``，
然后加进 :data:`SECTIONS`。界面上的勾选框会自动多一个（界面读的是同一张表）。
"""

from __future__ import annotations

import html
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .. import APP_VERSION
from .models import DeviceDef, PointDef, Project, point_id

#: 表格默认最多列多少行（几万个点位全塞进 HTML 会卡浏览器；截断会写注明）
DEFAULT_MAX_ROWS = 2000

#: 报告能存成哪些格式（扩展名 -> 中文说明）
FORMATS: dict[str, str] = {
    "html": "网页（可直接打印 / 另存 PDF）",
    "md": "Markdown（贴文档、进版本库）",
}


# --------------------------------------------------------------- 数据结构


@dataclass
class Table:
    """一张表。``rows`` 里已经是**字符串**，渲染器不用再懂业务。"""

    title: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    #: 截断/缺数据之类的说明，渲染在表格下面
    note: str = ""


@dataclass
class SectionResult:
    """一个小节的产出：键值对、表格、段落。三样都可以为空。"""

    key: str
    title: str
    kv: list[tuple[str, str]] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    paragraphs: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.kv or self.tables or self.paragraphs)


@dataclass
class ReportOptions:
    """生成报告的选项（界面上的那些勾选框就是它的字段）。"""

    title: str = "设备与点位报告"
    #: 要哪些小节（key 列表）；空 = 全部
    sections: tuple[str, ...] = ()
    #: 每张表最多多少行
    max_rows: int = DEFAULT_MAX_ROWS
    #: 报警/历史这类"带时间"的小节用这个范围（Unix 秒）；None = 不限
    time_from: float | None = None
    time_to: float | None = None
    #: 手写备注：现场写了"这次做了什么试验"，报告里会带上
    notes: str = ""


@dataclass
class ReportContext:
    """生成时要用的原料。全部可选 —— 缺什么就少哪一节的内容，不会报错。"""

    project: Project
    options: ReportOptions = field(default_factory=ReportOptions)
    #: 历史库（``icomm.storage.Storage``），没有就跳过报警小节的数据
    storage: Any = None
    #: 实时值：``{点号: TagValue}``（也接受 dict 形式）
    values: dict[str, Any] = field(default_factory=dict)
    #: 设备通信状态：``DeviceStatus`` 列表
    statuses: Sequence[Any] = ()
    #: 生成时间（可注入，方便测试）
    now: float = field(default_factory=time.time)
    app_version: str = APP_VERSION


@dataclass
class Report:
    """生成好的报告：纯数据，可渲染、可存盘。"""

    options: ReportOptions
    project_name: str
    generated_at: float
    app_version: str
    sections: list[SectionResult] = field(default_factory=list)

    # -------------------------------------------------- 文件名
    def default_name(self, ext: str = "html") -> str:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.generated_at))
        base = (self.project_name or "工程").strip() or "工程"
        return f"{base}_报告_{stamp}.{ext.lstrip('.')}"

    # -------------------------------------------------- Markdown
    def to_markdown(self) -> str:
        lines = [f"# {self.options.title}", ""]
        lines.append(
            f"> 工程：{self.project_name}　生成时间：{self.time_text()}"
            f"　软件版本：{self.app_version}"
        )
        if self.options.notes.strip():
            lines.append("")
            lines.append("> 备注：" + self.options.notes.strip().replace("\n", " "))
        for section in self.sections:
            lines.append("")
            lines.append(f"## {section.title}")
            if section.kv:
                lines.append("")
                for key, value in section.kv:
                    lines.append(f"- **{key}**：{value}")
            for paragraph in section.paragraphs:
                lines.append("")
                lines.append(paragraph)
            for table in section.tables:
                lines.append("")
                if table.title:
                    lines.append(f"### {table.title}")
                    lines.append("")
                lines.append("| " + " | ".join(table.columns) + " |")
                lines.append("|" + "|".join("---" for _ in table.columns) + "|")
                for row in table.rows:
                    lines.append("| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |")
                if table.note:
                    lines.append("")
                    lines.append(f"*{table.note}*")
        lines.append("")
        return "\n".join(lines)

    # -------------------------------------------------- HTML
    def to_html(self) -> str:
        """自包含的 HTML：不引用任何外部 css/图片，双击就能看、浏览器可直接打印。"""
        escape = html.escape
        body: list[str] = [f"<h1>{escape(self.options.title)}</h1>"]
        meta = (
            f"工程：{escape(self.project_name)}　|　生成时间：{escape(self.time_text())}"
            f"　|　软件版本：{escape(self.app_version)}"
        )
        body.append(f'<p class="meta">{meta}</p>')
        if self.options.notes.strip():
            body.append(f'<div class="notes">{escape(self.options.notes.strip())}</div>')

        for section in self.sections:
            body.append(f"<h2>{escape(section.title)}</h2>")
            if section.kv:
                body.append('<table class="kv">')
                for key, value in section.kv:
                    body.append(
                        f"<tr><th>{escape(key)}</th><td>{escape(value)}</td></tr>"
                    )
                body.append("</table>")
            for paragraph in section.paragraphs:
                body.append(f"<p>{escape(paragraph)}</p>")
            for table in section.tables:
                if table.title:
                    body.append(f"<h3>{escape(table.title)}</h3>")
                body.append("<table><thead><tr>")
                body.extend(f"<th>{escape(column)}</th>" for column in table.columns)
                body.append("</tr></thead><tbody>")
                for row in table.rows:
                    body.append("<tr>")
                    body.extend(f"<td>{escape(str(cell))}</td>" for cell in row)
                    body.append("</tr>")
                body.append("</tbody></table>")
                if table.note:
                    body.append(f'<p class="note">{escape(table.note)}</p>')

        footer = (
            f"由 VVPhmi {escape(self.app_version)} 生成　　报告内容以现场实际装置为准"
        )
        return _HTML_TEMPLATE.format(
            title=escape(self.options.title), body="\n".join(body), footer=footer
        )

    # -------------------------------------------------- 存盘
    def write(self, path: str) -> str:
        """按扩展名选格式存盘，返回实际路径（目录会自动建）。"""
        ext = os.path.splitext(path)[1].lstrip(".").lower() or "html"
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        text = self.to_markdown() if ext in ("md", "markdown") else self.to_html()
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return path

    def time_text(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.generated_at))

    @property
    def is_empty(self) -> bool:
        return not self.sections


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  @page {{ size: A4; margin: 16mm; }}
  body {{ font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif;
         color: #1c1f26; background: #ffffff; line-height: 1.5; margin: 24px; }}
  h1 {{ font-size: 22px; border-bottom: 3px solid #2f7bd6; padding-bottom: 8px; }}
  h2 {{ font-size: 17px; margin-top: 26px; color: #1b4f8f; }}
  h3 {{ font-size: 14px; margin: 16px 0 6px; color: #333; }}
  .meta {{ color: #5b6270; font-size: 12px; }}
  .notes {{ background: #f4f7fb; border-left: 4px solid #2f7bd6; padding: 8px 12px;
            white-space: pre-wrap; font-size: 13px; }}
  table {{ border-collapse: collapse; width: 100%; margin: 8px 0 4px; font-size: 12px; }}
  th, td {{ border: 1px solid #c9d2de; padding: 4px 8px; text-align: left;
            vertical-align: top; }}
  thead th {{ background: #e8f0fa; }}
  table.kv th {{ width: 180px; background: #f4f7fb; font-weight: 600; }}
  tbody tr:nth-child(even) {{ background: #fafbfd; }}
  .note {{ color: #8a6d1f; font-size: 12px; }}
  footer {{ margin-top: 28px; padding-top: 8px; border-top: 1px solid #d7dde6;
            color: #6b7280; font-size: 11px; }}
</style>
</head>
<body>
{body}
<footer>{footer}</footer>
</body>
</html>
"""


# --------------------------------------------------------------- 小工具


def _tag_parts(tag: Any) -> tuple[Any, str, float, str]:
    """``TagValue`` 或 dict 都能拆出 ``(值, 质量, 时间戳, 错误)``。"""
    if tag is None:
        return (None, "", 0.0, "")
    if isinstance(tag, dict):
        return (
            tag.get("value"),
            str(tag.get("quality", "")),
            float(tag.get("timestamp") or 0.0),
            str(tag.get("error", "")),
        )
    return (
        getattr(tag, "value", None),
        str(getattr(tag, "quality", "")),
        float(getattr(tag, "timestamp", 0.0) or 0.0),
        str(getattr(tag, "error", "")),
    )


def _fmt_time(stamp: float) -> str:
    if not stamp:
        return "--"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))


def _fmt(value: Any) -> str:
    if value is None:
        return "--"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}"
    if isinstance(value, (list, tuple)):
        head = ", ".join(_fmt(item) for item in list(value)[:8])
        more = "" if len(value) <= 8 else f" …（共 {len(value)} 个）"
        return f"[{head}]{more}"
    return str(value)


def _limit(rows: list[list[str]], max_rows: int) -> tuple[list[list[str]], str]:
    """表格行数上限：超了截断并留一句说明（别静默少数据）。"""
    if max_rows <= 0 or len(rows) <= max_rows:
        return rows, ""
    return rows[:max_rows], f"共 {len(rows)} 行，本报告只列前 {max_rows} 行"


def _device_points(project: Project, device: str = "") -> Iterable[tuple[DeviceDef, PointDef]]:
    for dev, point in project.iter_points():
        if device and dev.name != device:
            continue
        yield dev, point


# --------------------------------------------------------------- 各个小节


def _section_overview(ctx: ReportContext) -> SectionResult:
    project = ctx.project
    points = list(project.iter_points())
    writable = sum(1 for _dev, point in points if point.writable)
    total = len(project.all_point_ids())
    alarms = sum(1 for _dev, point in points if point.alarm.enabled)
    archive = sum(1 for _dev, point in points if point.archive)
    notes = (getattr(project, "notes", "") or "").strip().splitlines()
    kv = [
        ("工程名称", project.name),
        ("文件夹名", getattr(project, "folder_name", "") or "--"),
        ("通道数", str(len(project.channels))),
        ("设备数", str(len(project.devices))),
        ("点位数", str(total)),
        ("其中可写", str(writable)),
        ("启用报警的点位", str(alarms)),
        ("历史归档的点位", str(archive)),
        ("历史归档间隔", f"{getattr(project, 'history_interval_s', 0)} s"),
        ("历史保留天数", f"{getattr(project, 'history_retention_days', 0)} 天"),
    ]
    if notes:
        kv.append(("记事本摘要", notes[0].strip()))
    options = ctx.options
    if options.time_from or options.time_to:
        kv.append((
            "统计时间范围",
            f"{_fmt_time(options.time_from or 0)} ~ {_fmt_time(options.time_to or 0)}",
        ))
    return SectionResult(key="overview", title="工程概况", kv=kv)


def _section_channels(ctx: ReportContext) -> SectionResult:
    project = ctx.project
    rows = []
    for channel in project.channels:
        devices = sum(1 for device in project.devices if device.channel == channel.name)
        options = ", ".join(f"{key}={_fmt(value)}" for key, value in (channel.options or {}).items()
                            if key not in ("password", "token"))
        rows.append([
            channel.name,
            channel.driver,
            channel.link,
            "启用" if channel.enabled else "停用",
            str(devices),
            options or "--",
        ])
    rows, note = _limit(rows, ctx.options.max_rows)
    return SectionResult(
        key="channels",
        title="通道",
        tables=[Table(
            title="",
            columns=["通道", "驱动", "链路", "状态", "设备数", "连接参数"],
            rows=rows,
            note=note,
        )],
    )


def _section_devices(ctx: ReportContext) -> SectionResult:
    rows = []
    for device in ctx.project.devices:
        rows.append([
            device.name,
            device.channel,
            getattr(device, "protocol", "") or "--",
            str(device.unit_id),
            f"{device.poll_interval_ms} ms",
            f"{device.timeout_ms} ms / 重试 {device.retries}",
            "启用" if device.enabled else "停用",
            (device.description or "").strip() or "--",
        ])
    rows, note = _limit(rows, ctx.options.max_rows)
    return SectionResult(
        key="devices",
        title="设备",
        tables=[Table(
            title="",
            columns=["设备", "通道", "协议", "从站号", "采集周期", "超时/重试", "状态", "备注"],
            rows=rows,
            note=note,
        )],
    )


def _section_points(ctx: ReportContext) -> SectionResult:
    rows = []
    for device, point in _device_points(ctx.project):
        rows.append([
            point_id(device.name, point.name),
            point.title,
            f"{point.address}",
            point.data_type,
            point.unit or "--",
            _fmt(point.scale),
            str(point.decimals),
            "可写" if point.writable else "只读",
            point.write_limit_text() if point.has_write_limit else "--",
            point.group or "--",
        ])
    rows, note = _limit(rows, ctx.options.max_rows)
    return SectionResult(
        key="points",
        title="点位",
        tables=[Table(
            title="",
            columns=["点号", "显示名", "地址", "类型", "单位", "缩放", "小数位",
                     "读写", "写值范围", "分组"],
            rows=rows,
            note=note,
        )],
    )


def _section_live(ctx: ReportContext) -> SectionResult:
    if not ctx.values:
        return SectionResult(
            key="live",
            title="实时值快照",
            paragraphs=["没有实时数据（采集未运行，或这一项是离线生成的）。"],
        )
    rows = []
    good = bad = 0
    for device, point in _device_points(ctx.project):
        pid = point_id(device.name, point.name)
        if pid not in ctx.values:
            continue
        value, quality, stamp, error = _tag_parts(ctx.values[pid])
        if quality == "good":
            good += 1
        else:
            bad += 1
        rows.append([
            pid,
            _fmt(value),
            point.unit or "",
            quality or "--",
            _fmt_time(stamp),
            error or "",
        ])
    rows, note = _limit(rows, ctx.options.max_rows)
    return SectionResult(
        key="live",
        title="实时值快照",
        kv=[("质量良好", str(good)), ("质量异常", str(bad)),
            ("快照时间", _fmt_time(ctx.now))],
        tables=[Table(
            title="",
            columns=["点号", "值", "单位", "质量", "更新时间", "错误"],
            rows=rows,
            note=note,
        )],
    )


def _section_comms(ctx: ReportContext) -> SectionResult:
    if not ctx.statuses:
        return SectionResult(
            key="comms",
            title="通信状态",
            paragraphs=["没有通信状态数据（采集未运行，或这一项是离线生成的）。"],
        )
    rows = []
    for status in ctx.statuses:
        rate = getattr(status, "loss_rate", None)
        recent = getattr(status, "recent_loss_rate", None)
        rows.append([
            str(getattr(status, "name", "--")),
            str(getattr(status, "state", "--")),
            str(getattr(status, "request_count", 0)),
            "--" if rate is None else f"{rate * 100:.1f}%",
            "--" if recent is None else f"{recent * 100:.1f}%",
            _fmt_time(float(getattr(status, "last_ok_ts", 0.0) or 0.0)),
            str(getattr(status, "error", "") or ""),
        ])
    rows, note = _limit(rows, ctx.options.max_rows)
    return SectionResult(
        key="comms",
        title="通信状态",
        tables=[Table(
            title="",
            columns=["设备", "状态", "请求轮数", "累计丢包", "最近丢包", "最后成功", "最后错误"],
            rows=rows,
            note=note,
        )],
    )


def _section_alarms(ctx: ReportContext) -> SectionResult:
    if ctx.storage is None:
        return SectionResult(
            key="alarms",
            title="报警",
            paragraphs=["没有历史库，报警记录这一节为空。"],
        )
    limit = max(1, int(ctx.options.max_rows))
    try:
        records = ctx.storage.query_alarms(limit=limit)
    except Exception as error:  # noqa: BLE001 - 报告不该因为一次查询失败整体崩掉
        return SectionResult(
            key="alarms",
            title="报警",
            paragraphs=[f"读取报警记录失败：{error}"],
        )
    if ctx.options.time_from or ctx.options.time_to:
        low = float(ctx.options.time_from or 0)
        high = float(ctx.options.time_to or time.time())
        records = [row for row in records if low <= float(row.get("ts") or 0) <= high]

    counts: dict[str, int] = {}
    for row in records:
        level = str(row.get("level", "")) or "未知"
        counts[level] = counts.get(level, 0) + 1
    active = sum(1 for row in records if row.get("active"))
    kv = [("记录条数", str(len(records))), ("未复位", str(active))]
    for level, count in sorted(counts.items()):
        kv.append((f"级别 {level}", str(count)))
    rows = []
    for row in records:
        rows.append([
            _fmt_time(float(row.get("ts") or 0)),
            str(row.get("device", "") or ""),
            str(row.get("tag", "") or row.get("pid", "") or ""),
            str(row.get("level", "") or ""),
            str(row.get("message", "") or ""),
            "未复位" if row.get("active") else "已复位",
        ])
    rows, note = _limit(rows, ctx.options.max_rows)
    return SectionResult(
        key="alarms",
        title="报警",
        kv=kv,
        tables=[Table(
            title="",
            columns=["时间", "设备", "点位", "级别", "内容", "状态"],
            rows=rows,
            note=note,
        )],
    )


def _section_notes(ctx: ReportContext) -> SectionResult:
    text = (ctx.options.notes or "").strip()
    if not text:
        return SectionResult(key="notes", title="备注", paragraphs=[])
    return SectionResult(key="notes", title="备注", paragraphs=text.splitlines())


#: 小节清单：**顺序就是报告里的顺序**。加内容只改这一张表。
SECTIONS: tuple[tuple[str, str, str, Callable[[ReportContext], SectionResult]], ...] = (
    ("overview", "工程概况", "工程名、通道/设备/点数统计", _section_overview),
    ("channels", "通道", "驱动、链路、连接参数", _section_channels),
    ("devices", "设备", "从属通道、从站号、采集周期、启停", _section_devices),
    ("points", "点位", "点号、地址、类型、单位、写值范围", _section_points),
    ("live", "实时值快照", "生成那一刻的各点位值与质量", _section_live),
    ("comms", "通信状态", "在线状态与丢包率", _section_comms),
    ("alarms", "报警", "报警统计与最近记录（需要历史库）", _section_alarms),
    ("notes", "备注", "手写的试验说明", _section_notes),
)


def section_keys() -> list[str]:
    return [key for key, _title, _doc, _func in SECTIONS]


def section_choices() -> list[tuple[str, str, str]]:
    """``[(key, 标题, 说明)]`` —— 界面上的勾选框直接读它。"""
    return [(key, title, doc) for key, title, doc, _func in SECTIONS]


def default_sections() -> tuple[str, ...]:
    """默认要哪几节（实时值与通信状态依赖采集在跑，默认仍然带上，缺数据会说明）。"""
    return ("overview", "channels", "devices", "points", "live", "comms")


# --------------------------------------------------------------- 入口


def build_report(ctx: ReportContext) -> Report:
    """按 :data:`SECTIONS` 依次取数，拼成 :class:`Report`（纯数据）。"""
    wanted = tuple(ctx.options.sections) or section_keys()
    chosen = [entry for entry in SECTIONS if entry[0] in wanted]
    if not chosen:
        # 勾了一个都不存在的小节：宁可报错，也别悄悄给一份空报告
        raise ValueError(f"没有这些小节：{', '.join(wanted)}")
    results: list[SectionResult] = []
    for key, title, _doc, func in chosen:
        try:
            section = func(ctx)
        except Exception as error:  # noqa: BLE001 - 一个小节坏掉不该毁掉整份报告
            section = SectionResult(
                key=key, title=title,
                paragraphs=[f"这一节生成失败：{type(error).__name__}: {error}"],
            )
        section.key = key
        section.title = title
        if section.empty and key not in ("live", "comms", "alarms"):
            continue  # 没内容的普通小节直接不出现（备注没写就不出"备注"空节）
        results.append(section)
    return Report(
        options=ctx.options,
        project_name=getattr(ctx.project, "name", ""),
        generated_at=ctx.now,
        app_version=ctx.app_version,
        sections=results,
    )


__all__ = [
    "DEFAULT_MAX_ROWS",
    "FORMATS",
    "SECTIONS",
    "Report",
    "ReportContext",
    "ReportOptions",
    "SectionResult",
    "Table",
    "build_report",
    "default_sections",
    "section_choices",
    "section_keys",
]
