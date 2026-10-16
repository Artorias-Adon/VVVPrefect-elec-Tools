"""CSV 参数表：点位定义的导入 / 导出。

设计要点
--------
* **固定列顺序 + 中文表头**，导出后可直接用 Excel 打开编辑再导回来。
* 文件写成 **UTF-8 with BOM**（``utf-8-sig``），否则 Excel 打开中文会乱码——
  这是 Windows 上最常见的坑。
* 导入**逐行校验**，把行号和原因收集成报告，不因为一行写错就整份失败。
* 布尔列兼容 ``是/否``、``1/0``、``true/false``、``Y/N``（Excel 各种写法）。
* 整数列兼容 Excel 把整数写成 ``10.0`` 的情况。
* 同时提供历史数据导出，便于把归档的"数据表"交出去做报表。
"""

from __future__ import annotations

import csv
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from .models import (
    DATA_TYPES,
    DISPLAY_TYPES,
    REGISTER_TYPES,
    WORD_ORDERS,
    AlarmDef,
    ChannelDef,
    DeviceDef,
    PointDef,
    Project,
    default_channel_options,
    normalize_base,
    point_id,
)

#: 固定列顺序（导出/导入共用，改这里要同步改 README）
CSV_COLUMNS = [
    "启用",
    "通道",
    "设备",
    "从站号",
    "点号",
    "显示名称",
    "分组",
    "寄存器区",
    "协议路径",
    "起始地址",
    "类型",
    "元素个数",
    "字序",
    "缩放",
    "偏移",
    "单位",
    "小数位",
    "显示进制",
    "可写",
    "写值下限",
    "写值上限",
    "限幅提示",
    "归档",
    "死区",
    "显示方式",
    "仪表下限",
    "仪表上限",
    "录波",
    "采样间隔us",
    "示波器",
    "位定义",
    "命令",
"状态码",
    "报警级别",
    "高高限",
    "高限",
    "低限",
    "低低限",
    "备注",
]

#: 中文表头 -> 内部字段名
_COLUMN_MAP = {
    "启用": "enabled",
    "通道": "channel",
    "设备": "device",
    "从站号": "unit_id",
    "点号": "name",
    "显示名称": "label",
    "分组": "group",
    "寄存器区": "register",
    "协议路径": "path",
    "起始地址": "address",
    "类型": "data_type",
    "元素个数": "count",
    "字序": "word_order",
    "缩放": "scale",
    "偏移": "offset",
    "单位": "unit",
    "小数位": "decimals",
    "显示进制": "display_base",
    "可写": "writable",
    "写值下限": "write_min",
    "写值上限": "write_max",
    "限幅提示": "write_limit_warn",
    "归档": "archive",
    "死区": "deadband",
    "显示方式": "display",
    "仪表下限": "gauge_min",
    "仪表上限": "gauge_max",
    "录波": "record",
    "采样间隔us": "sample_interval_us",
    "示波器": "scope",
    "位定义": "bits",
    "命令": "commands",
"状态码": "codes",
    "报警级别": "alarm_level",
    "高高限": "hi_hi",
    "高限": "hi",
    "低限": "lo",
    "低低限": "lo_lo",
    "备注": "description",
}

_TRUE_WORDS = {"是", "1", "true", "yes", "y", "on", "真"}
_FALSE_WORDS = {"否", "0", "false", "no", "n", "off", "假"}


# --------------------------------------------------------------- 报告


@dataclass
class ImportIssue:
    row: int
    level: str  # error | warning | info
    message: str

    def text(self) -> str:
        return f"第 {self.row} 行 [{self.level}] {self.message}" if self.row else f"[{self.level}] {self.message}"


@dataclass
class ImportReport:
    channels_added: int = 0
    devices_added: int = 0
    points_added: int = 0
    points_updated: int = 0
    skipped: int = 0
    issues: list[ImportIssue] = field(default_factory=list)

    @property
    def errors(self) -> list[ImportIssue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[ImportIssue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [
            f"通道 +{self.channels_added}",
            f"设备 +{self.devices_added}",
            f"点位 新增 {self.points_added} / 更新 {self.points_updated}",
        ]
        if self.skipped:
            parts.append(f"跳过 {self.skipped}")
        if self.issues:
            parts.append(f"问题 {len(self.issues)} 条（错误 {len(self.errors)}）")
        return "，".join(parts)

    def detail(self, limit: int = 30) -> str:
        lines = [i.text() for i in self.issues[:limit]]
        if len(self.issues) > limit:
            lines.append(f"... 另有 {len(self.issues) - limit} 条")
        return "\n".join(lines)


# --------------------------------------------------------------- 取值工具


def _to_bool(text: Any, default: bool = True) -> bool:
    """空单元格返回 default（让各字段自己的默认值生效），否则按文本解析。

    Excel 里的写法五花八门，这里统一兼容 是/否、1/0、true/false、Y/N、on/off。
    """
    if isinstance(text, bool):
        return text
    word = str(text or "").strip().lower()
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    return default


#: 导入时允许出现在「寄存器区」列里的值。
#: 不同协议里这一列含义不同（Modbus 是寄存器区、IEC 61850 是功能约束 FC、
#: CAN 是收发区），所以把三套都收进来 —— 校验只负责挡住明显的错别字。
def _point_area_names() -> set[str]:
    names = set(REGISTER_TYPES)
    try:
        from .drivers.iec61850 import FC_CHOICES

        names.update(value for value, _label in FC_CHOICES)
    except Exception:  # pragma: no cover - 库缺失时不该影响导入
        pass
    try:
        from .drivers.can import POINT_AREAS as CAN_AREAS

        names.update(value for value, _label in CAN_AREAS)
    except Exception:  # pragma: no cover
        pass
    try:
        from .drivers.s7 import POINT_AREAS as S7_AREAS

        names.update(value for value, _label in S7_AREAS)
    except Exception:  # pragma: no cover
        pass
    return names


KNOWN_POINT_AREAS = _point_area_names()


def _to_int(text: Any, default: int = 0) -> int:
    if text is None or str(text).strip() == "":
        return default
    try:
        return int(float(str(text).strip()))
    except ValueError:
        return default


def _to_float(text: Any, default: float = 0.0) -> float:
    if text is None or str(text).strip() == "":
        return default
    try:
        return float(str(text).strip())
    except ValueError:
        return default


def _to_opt_float(text: Any) -> float | None:
    if text is None or str(text).strip() == "":
        return None
    try:
        return float(str(text).strip())
    except ValueError:
        return None


def _cell(value: Any) -> str:
    """导出时的单元格文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


# --------------------------------------------------------------- 导出


def export_rows(project: Project) -> list[dict[str, Any]]:
    """把工程摊平成 CSV 行（一行一个点位）。"""
    rows: list[dict[str, Any]] = []
    for channel in project.channels:
        for device in project.devices:
            if device.channel != channel.name:
                continue
            for point in device.points:
                alarm = point.alarm
                rows.append(
                    {
                        "启用": device.enabled and point.kind != "",
                        "通道": channel.name,
                        "设备": device.name,
                        "从站号": device.unit_id,
                        "点号": point.name,
                        "显示名称": point.label or point.name,
                        "分组": point.group,
                        "寄存器区": point.register,
                        "协议路径": point.path,
                        "起始地址": point.address,
                        "类型": point.data_type,
                        "元素个数": point.count if point.is_array else 1,
                        "字序": point.word_order,
                        "缩放": point.scale,
                        "偏移": point.offset,
                        "单位": point.unit,
                        "小数位": point.decimals,
                        "显示进制": point.display_base,
                        "可写": point.writable,
                        "写值下限": point.write_min,
                        "写值上限": point.write_max,
                        "限幅提示": point.write_limit_warn,
                        "归档": point.archive,
                        "死区": point.deadband,
                        "显示方式": point.display,
                        "仪表下限": point.gauge_min,
                        "仪表上限": point.gauge_max,
                        "录波": point.record,
                        "采样间隔us": point.sample_interval_us,
                        "示波器": point.scope,
                        "位定义": point.bits,
                        "命令": point.commands,
"状态码": point.codes,
                        "报警级别": alarm.level if alarm.enabled else "",
                        "高高限": alarm.hi_hi,
                        "高限": alarm.hi,
                        "低限": alarm.lo,
                        "低低限": alarm.lo_lo,
                        "备注": point.description,
                    }
                )
    # 统一转成单元格文本：布尔写成 是/否，浮点去掉多余的 0，
    # 避免导出的表里出现 True/False 这种既不好看也不符合表约定写法
    return [{key: _cell(value) for key, value in row.items()} for row in rows]


def export_text(project: Project) -> str:
    """把参数表导出成 CSV 文本（不含 BOM），用于与磁盘上的表比对。"""
    import io

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=CSV_COLUMNS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(export_rows(project))
    return buffer.getvalue()


def export_csv(project: Project, path: str) -> int:
    """导出参数表，返回写出的行数。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    text = export_text(project)
    # utf-8-sig：带 BOM，Excel 才会正确识别中文
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        fh.write(text)
    return text.count("\n") - 1


def csv_differs_from_disk(project: Project, path: str) -> bool:
    """磁盘上的参数表是否与当前工程不一致（用户可能用 Excel 改过但没导入）。"""
    if not os.path.exists(path):
        return False
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            on_disk = fh.read()
    except OSError:
        return False
    return on_disk.replace("\r\n", "\n").rstrip("\n") != export_text(project).rstrip("\n")


def export_history_csv(
    storage,
    pids: Iterable[str],
    start_ts: float,
    end_ts: float,
    path: str,
) -> int:
    """导出历史数据表（宽表：一行一个时间点，一列一个点位），返回行数。"""
    pids = list(pids)
    data = storage.query_history(pids, start_ts, end_ts, limit=200000)

    # 归并到同一时间轴
    timeline: dict[float, dict[str, float]] = {}
    for pid, samples in data.items():
        for ts, value in samples:
            timeline.setdefault(round(ts, 6), {})[pid] = value

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["时间"] + pids)
        for ts in sorted(timeline):
            row = timeline[ts]
            writer.writerow(
                [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))]
                + [row.get(pid, "") for pid in pids]
            )
    return len(timeline)


# --------------------------------------------------------------- 导入


def read_rows(path: str) -> tuple[list[dict[str, Any]], list[ImportIssue]]:
    """读原始 CSV 行并做表头校验。"""
    issues: list[ImportIssue] = []
    if not os.path.exists(path):
        raise FileNotFoundError(path)

    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        missing = [c for c in ("设备", "点号", "起始地址") if c not in headers]
        if missing:
            raise ValueError(
                "参数表缺少必需列：" + "、".join(missing) + f"（当前表头：{'、'.join(headers)}）"
            )
        unknown = [h for h in headers if h and h not in _COLUMN_MAP]
        if unknown:
            issues.append(
                ImportIssue(0, "warning", "忽略无法识别的列：" + "、".join(unknown))
            )
        rows = []
        for raw in reader:
            rows.append({(k or "").strip(): v for k, v in raw.items()})
    return rows, issues


def import_csv(
    project: Project,
    path: str,
    mode: str = "merge",
    default_channel: str | None = None,
) -> ImportReport:
    """把 CSV 参数表导入工程（就地修改 project）。

    mode:
        ``merge``   按 (设备, 点号) 更新已有点位，不存在的新增（默认）
        ``replace`` 先清空工程里所有点位，再按表重建
    """
    rows, issues = read_rows(path)
    return import_rows(project, rows, mode=mode, default_channel=default_channel, issues=issues)


def import_rows(
    project: Project,
    rows: list[dict[str, Any]],
    mode: str = "merge",
    default_channel: str | None = None,
    issues: list[ImportIssue] | None = None,
    source: str = "",
) -> ImportReport:
    """按"已经读成字典的行"导入参数表。

    与 :func:`import_csv` 共用同一套逐行校验，所以别的格式（例如 JSON）
    只要把内容转成同样的行字典，行为就与 CSV 完全一致。
    """
    report = ImportReport(issues=list(issues or []))
    if source:
        report.issues.append(ImportIssue(0, "info", f"来源：{os.path.basename(source)}"))

    if mode == "replace":
        for device in project.devices:
            device.points = []
        report.issues.append(ImportIssue(0, "warning", "已按「替换」模式清空原有全部点位"))

    if default_channel is None:
        default_channel = project.channels[0].name if project.channels else ""

    seen: set[str] = set()
    fallback_channel = ""
    fallback_warned = False

    for index, raw in enumerate(rows, start=2):  # 第 1 行是表头
        name = (raw.get("点号") or "").strip()
        device_name = (raw.get("设备") or "").strip()
        if not name and not device_name:
            continue  # 整行空，跳过
        if not device_name:
            report.issues.append(ImportIssue(index, "error", "「设备」为空，无法归属"))
            report.skipped += 1
            continue
        if not name:
            report.issues.append(ImportIssue(index, "error", "「点号」为空"))
            report.skipped += 1
            continue

        channel_name = (raw.get("通道") or "").strip() or default_channel
        if not channel_name:
            # 表里没写通道、工程里也没有通道：自动建一条默认 TCP 通道，
            # 让"只给一张点位表"也能把工程搭起来（后续到配置页改成实际串口）
            channel_name = fallback_channel or "通道1"
            fallback_channel = channel_name
            if not fallback_warned:
                fallback_warned = True
                report.issues.append(
                    ImportIssue(
                        index,
                        "warning",
                        f"表里未指定「通道」且工程里没有通道，已自动创建「{channel_name}」（默认 TCP 参数），"
                        "请到配置页核对链路类型与端口",
                    )
                )

        # ---- 通道
        channel = project.channel(channel_name)
        if channel is None:
            channel = ChannelDef(
                name=channel_name,
                driver="modbus",
                link="tcp",
                options={**default_channel_options("tcp"), "host": "127.0.0.1", "port": 502},
                description="由参数表导入自动创建",
            )
            project.channels.append(channel)
            report.channels_added += 1
            report.issues.append(
                ImportIssue(
                    index, "warning", f"通道「{channel_name}」不存在，已按默认 TCP 参数创建，请到配置页核对串口/端口"
                )
            )

        # ---- 设备
        device = project.device(device_name)
        if device is None:
            device = DeviceDef(name=device_name, channel=channel_name)
            project.devices.append(device)
            report.devices_added += 1
        elif device.channel != channel_name:
            device.channel = channel_name
            report.issues.append(
                ImportIssue(index, "warning", f"设备「{device_name}」已改挂到通道「{channel_name}」")
            )
        unit = _to_int(raw.get("从站号"), instance_or(device.unit_id, 1))
        device.unit_id = max(0, min(255, unit))
        if _to_bool(raw.get("启用"), True) is False:
            device.enabled = False

        # ---- 点位
        point, created = _build_point(raw, index, report)
        if point is None:
            report.skipped += 1
            continue

        pid = point_id(device_name, point.name)
        if pid in seen:
            report.issues.append(ImportIssue(index, "error", f"点号重复：{pid}"))
            report.skipped += 1
            continue
        seen.add(pid)

        existing = device.point(point.name)
        if existing is None:
            device.points.append(point)
            report.points_added += 1
        else:
            _copy_into(existing, point)
            report.points_updated += 1

    if not report.points_added and not report.points_updated and not report.errors:
        report.issues.append(ImportIssue(0, "warning", "参数表里没有任何有效点位"))
    return report


def instance_or(value: int, default: int) -> int:
    return value if value else default


def _build_point(raw: dict[str, Any], row_no: int, report: ImportReport) -> tuple[PointDef | None, bool]:
    """从一行构造 PointDef，并做取值校验。"""
    name = (raw.get("点号") or "").strip()
    register = (raw.get("寄存器区") or "holding").strip()
    if register not in KNOWN_POINT_AREAS:
        report.issues.append(
            ImportIssue(
                row_no,
                "error",
                f"寄存器区「{register}」无效：Modbus 填 {'/'.join(REGISTER_TYPES)}；"
                "IEC 61850 填 FC（MX/ST/CO/SP/CF…）；CAN 填 can_rx/can_tx/can_sdo",
            )
        )
        return None, False

    data_type = (raw.get("类型") or "uint16").strip().lower()
    if data_type not in DATA_TYPES:
        report.issues.append(
            ImportIssue(row_no, "error", f"类型「{data_type}」无效，可选：{'/'.join(DATA_TYPES)}")
        )
        return None, False

    word_order = (raw.get("字序") or "big").strip().lower()
    if word_order not in WORD_ORDERS:
        word_order = "big"

    display = (raw.get("显示方式") or "card").strip().lower()
    if display not in DISPLAY_TYPES:
        report.issues.append(
            ImportIssue(row_no, "warning", f"显示方式「{display}」无效，已按 card 处理")
        )
        display = "card"

    address = _to_int(raw.get("起始地址"), -1)
    if address < 0 or address > 65535:
        report.issues.append(ImportIssue(row_no, "error", f"起始地址「{raw.get('起始地址')}」越界"))
        return None, False

    count = _to_int(raw.get("元素个数"), 1)
    if count < 1:
        count = 1
    # 元素个数 > 1 即视为数组/块点位（录波用）
    kind = "array" if count > 1 else "scalar"
    if kind == "array" and register.lower() in ("coil", "discrete"):
        report.issues.append(
            ImportIssue(row_no, "warning", "线圈/离散输入不建议做数组点位，已按数组读取")
        )

    point = PointDef(
        name=name,
        label=(raw.get("显示名称") or "").strip() or name,
        group=(raw.get("分组") or "").strip() or "默认",
        register=register,
        path=(raw.get("协议路径") or "").strip(),
        address=address,
        data_type=data_type,
        word_order=word_order,
        scale=_to_float(raw.get("缩放"), 1.0),
        offset=_to_float(raw.get("偏移"), 0.0),
        unit=(raw.get("单位") or "").strip(),
        decimals=max(0, min(6, _to_int(raw.get("小数位"), 2))),
        display_base=normalize_base(raw.get("显示进制")),
        writable=_to_bool(raw.get("可写"), False),
        write_min=_to_opt_float(raw.get("写值下限")),
        write_max=_to_opt_float(raw.get("写值上限")),
        write_limit_warn=_to_bool(raw.get("限幅提示"), True),
        archive=_to_bool(raw.get("归档"), True),
        deadband=_to_float(raw.get("死区"), 0.0),
        display=display,
        gauge_min=_to_float(raw.get("仪表下限"), 0.0),
        gauge_max=_to_float(raw.get("仪表上限"), 100.0),
        kind=kind,
        count=count,
        record=_to_bool(raw.get("录波"), False),
        sample_interval_us=max(0, _to_int(raw.get("采样间隔us"), 0)),
        scope=_to_bool(raw.get("示波器"), kind == "array"),
        bits=(raw.get("位定义") or "").strip(),
        commands=(raw.get("命令") or "").strip(),
codes=(raw.get("状态码") or "").strip(),
        description=(raw.get("备注") or "").strip(),
    )

    level = (raw.get("报警级别") or "").strip().lower()
    if level and level not in ("info", "warning", "critical"):
        report.issues.append(ImportIssue(row_no, "warning", f"报警级别「{level}」无效，按 warning 处理"))
        level = "warning"

    thresholds = {
        "hi_hi": _to_opt_float(raw.get("高高限")),
        "hi": _to_opt_float(raw.get("高限")),
        "lo": _to_opt_float(raw.get("低限")),
        "lo_lo": _to_opt_float(raw.get("低低限")),
    }
    if any(v is not None for v in thresholds.values()) or level:
        point.alarm = AlarmDef(
            enabled=True, level=level or "warning", deadband=0.0, **thresholds
        )

    if point.is_array and point.data_type == "bool":
        report.issues.append(ImportIssue(row_no, "error", "bool 类型不支持数组点位"))
        return None, False

    return point, True


def _copy_into(target: PointDef, source: PointDef) -> None:
    """把导入的点位内容覆盖到已有点位上（保留对象引用，界面无需重建）。"""
    for key, value in asdict(source).items():
        if key == "alarm":
            continue
        setattr(target, key, value)
    target.alarm = source.alarm
