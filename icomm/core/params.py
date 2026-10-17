"""下发参数记录：调试好一台设备，把这批设定值搬到同型号的下一台。

现场的常规流程是"先在一台装置上把参数试到位（比例、限幅、阈值、延时…），
再把同一批值下到另外几十台"。手工抄既慢又容易抄错，所以这里做三件事：

* **记**：每次成功写入一个可写的"设定项"点位，主窗口就记一条（点号、值、时间、地址）；
  命令点位（启动/停机那种**动作**）不算参数，不记；
* **存**：存文件时**默认 JSON**（带单位、地址、时间等完整信息），也可以选 CSV（Excel 里对表/改）
  或 TXT（纯文本看一眼）。读回来支持 JSON 与 CSV —— TXT 是给人看的，不做解析；
* **搬**：导入时按**点位名**匹配目标设备，所以同型号的另一台（站号不同、设备名不同）也能用。

注意：值按**工程值**存（就是界面上看到的那个数），换算成寄存器是写值链路自己做的事。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .models import DeviceDef, PointDef, Project, point_id

#: 记录文件的格式标记与版本
FILE_FORMAT = "vvphmi-params"
FILE_VERSION = 1

FORMAT_JSON = "json"
FORMAT_CSV = "csv"
FORMAT_TXT = "txt"

#: 保存对话框里的下拉项（顺序即下拉顺序，第一个是默认）
FORMATS: dict[str, str] = {
    FORMAT_JSON: "JSON 记录（默认，信息最全）",
    FORMAT_CSV: "CSV（Excel 可编辑）",
    FORMAT_TXT: "TXT（纯文本，只用于查看）",
}

#: 默认保存格式
DEFAULT_FORMAT = FORMAT_JSON

#: 这些格式能读回来（TXT 只用于人看）
READABLE_FORMATS = (FORMAT_JSON, FORMAT_CSV)

#: CSV 的表头（也是读回时的识别依据）
CSV_HEADER = ["点号", "显示名", "值", "单位", "地址", "下发时间"]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clean(value: Any) -> Any:
    """存进记录的值：bool 保留、整数不写成 1.0、其它照旧。"""
    if isinstance(value, (bool, int, str)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    return int(number) if number.is_integer() else number


def address_text(point: PointDef) -> str:
    """点位的地址描述：Modbus 是 ``holding#40200``，其它协议带上路径。"""
    body = f"{point.register}#{point.address}"
    return f"{body} {point.path}".strip() if point.path else body


def is_setting(point: PointDef) -> bool:
    """能算"参数"的点位：可写、且不是命令（命令是动作，不是设定值）。"""
    return bool(point.writable) and not point.command_items()


# --------------------------------------------------------------- 记录


@dataclass
class ParamEntry:
    """一条参数记录。"""

    #: 点位名（**不含设备名**：搬到另一台设备时按它匹配）
    point: str
    label: str = ""
    value: Any = None
    unit: str = ""
    address: str = ""
    data_type: str = ""
    decimals: int = 2
    #: 最近一次下发时间
    time: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "point": self.point,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "address": self.address,
            "data_type": self.data_type,
            "decimals": self.decimals,
            "time": self.time,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParamEntry":
        if not isinstance(data, dict) or not data.get("point"):
            raise ValueError("参数记录里有一条缺「点号」")
        return cls(
            point=str(data["point"]),
            label=str(data.get("label") or ""),
            value=data.get("value"),
            unit=str(data.get("unit") or ""),
            address=str(data.get("address") or ""),
            data_type=str(data.get("data_type") or ""),
            decimals=int(data.get("decimals") or 0),
            time=str(data.get("time") or ""),
        )

    def display(self) -> str:
        """值 + 单位（表格最后一列/CSV 都按这个写）。"""
        text = "" if self.value is None else str(self.value)
        return f"{text} {self.unit}".strip()


@dataclass
class ParamRecord:
    """一台设备的一份参数记录。"""

    device: str = ""
    unit_id: int = 0
    saved_at: str = ""
    entries: list[ParamEntry] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def values(self) -> dict[str, Any]:
        return {entry.point: entry.value for entry in self.entries}

    def sorted_entries(self, order: Iterable[str] | None = None) -> list[ParamEntry]:
        """按给定点号顺序排（导出时跟参数表顺序一致，看着顺眼）。"""
        keys = list(order or [])
        if not keys:
            return list(self.entries)
        rank = {name: index for index, name in enumerate(keys)}
        return sorted(self.entries, key=lambda e: (rank.get(e.point, len(rank)), e.point))

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": FILE_FORMAT,
            "version": FILE_VERSION,
            "device": self.device,
            "unit_id": self.unit_id,
            "saved_at": self.saved_at or _now(),
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ParamRecord":
        if isinstance(data, list):  # 也认"就是一个数组"的旧式写法
            data = {"entries": data}
        if not isinstance(data, dict):
            raise ValueError("参数记录文件的顶层应该是对象")
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, list):
            raise ValueError("参数记录文件里没有 entries 列表")
        return cls(
            device=str(data.get("device") or ""),
            unit_id=int(data.get("unit_id") or 0),
            saved_at=str(data.get("saved_at") or ""),
            entries=[ParamEntry.from_dict(item) for item in raw_entries],
        )


class ParamRecorder:
    """记下发过的参数（按设备 → 点号）。"""

    def __init__(self) -> None:
        self._by_device: dict[str, dict[str, ParamEntry]] = {}

    # -------------------------------------------------- 记

    def record(
        self,
        device_name: str,
        point: PointDef,
        value: Any,
        when: str | None = None,
    ) -> ParamEntry | None:
        """记一条；不是"参数"的点位（只读 / 命令）返回 None。"""
        if not is_setting(point):
            return None
        entry = ParamEntry(
            point=point.name,
            label=point.title,
            value=_clean(value),
            unit=point.unit,
            address=address_text(point),
            data_type=point.data_type,
            decimals=point.decimals,
            time=when or _now(),
        )
        self._by_device.setdefault(device_name, {})[point.name] = entry
        return entry

    def snapshot(
        self,
        project: Project,
        device_name: str,
        value_of: Callable[[str], Any],
        when: str | None = None,
    ) -> int:
        """把该设备所有设定项点位的**当前值**记一遍，返回记了几条。

        给"参数不是在本软件里改的"（比如装置面板上设的）用：
        读回来的值就是现场想要的那套，照样能导出给下一台。
        """
        device = project.device(device_name)
        if device is None:
            raise ValueError(f"工程里没有设备 {device_name}")
        count = 0
        for point in device.points:
            if not is_setting(point):
                continue
            tag = value_of(point_id(device.name, point.name))
            value = getattr(tag, "value", tag)
            if value is None:
                continue
            if self.record(device.name, point, value, when) is not None:
                count += 1
        return count

    # -------------------------------------------------- 查

    def devices(self) -> list[str]:
        return [name for name, entries in self._by_device.items() if entries]

    def entries(self, device_name: str) -> list[ParamEntry]:
        return list(self._by_device.get(device_name, {}).values())

    def entry(self, device_name: str, point_name: str) -> ParamEntry | None:
        return self._by_device.get(device_name, {}).get(point_name)

    def record_for(
        self,
        device_name: str,
        project: Project | None = None,
        when: str | None = None,
    ) -> ParamRecord:
        """把某台设备记下的东西打成本子（可存文件）。"""
        device = project.device(device_name) if project is not None else None
        entries = self.entries(device_name)
        order = [p.name for p in device.points] if device is not None else None
        record = ParamRecord(
            device=device_name,
            unit_id=device.unit_id if device is not None else 0,
            saved_at=when or _now(),
            entries=entries,
        )
        record.entries = record.sorted_entries(order)
        return record

    # -------------------------------------------------- 清

    def clear(self, device_name: str | None = None) -> None:
        if device_name is None:
            self._by_device.clear()
        else:
            self._by_device.pop(device_name, None)

    def __len__(self) -> int:
        return sum(len(entries) for entries in self._by_device.values())


# --------------------------------------------------------------- 存 / 读


def format_for_path(path: str) -> str:
    """按扩展名判断格式（不认识就当 JSON）。"""
    suffix = os.path.splitext(path)[1].lower().lstrip(".")
    return suffix if suffix in FORMATS else DEFAULT_FORMAT


def to_json_text(record: ParamRecord) -> str:
    return json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n"


def from_json_text(text: str) -> ParamRecord:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"不是合法的 JSON：{exc}") from exc
    return ParamRecord.from_dict(data)


def to_csv_text(record: ParamRecord) -> str:
    """带 BOM 的 UTF-8 CSV（Excel 双击不乱码），头一行是给人和程序看的备注。"""
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    for entry in record.entries:
        writer.writerow(
            [
                entry.point,
                entry.label,
                "" if entry.value is None else str(entry.value),
                entry.unit,
                entry.address,
                entry.time,
            ]
        )
    head = (
        f"# 参数记录\t设备={record.device}\t从站号={record.unit_id}"
        f"\t保存时间={record.saved_at or _now()}\n"
    )
    return "\ufeff" + head + buffer.getvalue()


def _coerce(text: str, data_type: str) -> Any:
    """把文件里的文本值转回合适类型（bool / 整数 / 浮点）。"""
    body = (text or "").strip()
    if data_type == "bool":
        return body.lower() in ("1", "true", "on", "yes", "是", "开")
    if data_type.startswith(("int", "uint")):
        try:
            return int(round(float(body)))
        except ValueError:
            return body
    try:
        number = float(body)
    except ValueError:
        return body
    return int(number) if number.is_integer() else number


def from_csv_text(text: str) -> ParamRecord:
    import csv
    import io

    record = ParamRecord()
    body_lines: list[str] = []
    for raw in text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not raw.strip():
            continue
        if raw.lstrip().startswith("#"):
            for part in raw.lstrip("# ").strip().split("\t"):
                key, _sep, value = part.partition("=")
                key = key.strip()
                if key == "设备":
                    record.device = value.strip()
                elif key == "从站号":
                    try:
                        record.unit_id = int(value)
                    except ValueError:
                        pass
                elif key == "保存时间":
                    record.saved_at = value.strip()
            continue
        body_lines.append(raw)
    rows = [row for row in csv.reader(io.StringIO("\n".join(body_lines))) if any(c.strip() for c in row)]
    if not rows:
        raise ValueError("CSV 里没有数据行")
    header = [cell.strip() for cell in rows[0]]
    if "点号" not in header or "值" not in header:
        raise ValueError("CSV 缺少「点号」或「值」列")
    index = {name: header.index(name) for name in header if name}

    def cell(row: list[str], name: str) -> str:
        position = index.get(name)
        if position is None or position >= len(row):
            return ""
        return row[position]

    for row in rows[1:]:
        point = cell(row, "点号").strip()
        if not point:
            continue
        record.entries.append(
            ParamEntry(
                point=point,
                label=cell(row, "显示名"),
                value=_coerce(cell(row, "值"), ""),
                unit=cell(row, "单位"),
                address=cell(row, "地址"),
                time=cell(row, "下发时间"),
            )
        )
    return record


def to_txt_text(record: ParamRecord) -> str:
    """纯文本对表用（左对齐，中文按两格宽算）。"""
    def width(text: str) -> int:
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)

    lines = [
        f"参数记录　设备：{record.device or '-'}　从站号：{record.unit_id or '-'}"
        f"　保存时间：{record.saved_at or _now()}",
        "",
    ]
    heads = ["点号", "显示名", "值", "单位", "地址", "下发时间"]
    rows = [
        [
            entry.point,
            entry.label,
            "" if entry.value is None else str(entry.value),
            entry.unit,
            entry.address,
            entry.time,
        ]
        for entry in record.entries
    ]
    widths = [max([width(head)] + [width(row[i]) for row in rows]) for i, head in enumerate(heads)]

    def line(cells: list[str]) -> str:
        return "  ".join(cell + " " * (widths[i] - width(cell)) for i, cell in enumerate(cells)).rstrip()

    lines.append(line(heads))
    lines.append("─" * min(120, sum(widths) + 2 * (len(widths) - 1)))
    lines.extend(line(row) for row in rows)
    lines.append("")
    lines.append(f"共 {len(record.entries)} 条（TXT 只用于查看，导入请用 JSON 或 CSV）")
    return "\n".join(lines) + "\n"


def save(path: str, record: ParamRecord, fmt: str | None = None) -> str:
    """存文件（格式默认按扩展名，其次用 ``fmt``）。返回实际路径。"""
    chosen = (fmt or format_for_path(path) or DEFAULT_FORMAT).lower()
    if chosen not in FORMATS:
        chosen = DEFAULT_FORMAT
    writers = {FORMAT_JSON: to_json_text, FORMAT_CSV: to_csv_text, FORMAT_TXT: to_txt_text}
    text = writers[chosen](record)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig" if chosen == FORMAT_CSV else "utf-8", newline="") as handle:
        handle.write(text.lstrip("\ufeff") if chosen == FORMAT_CSV else text)
    return path


def load(path: str) -> ParamRecord:
    """读文件（按扩展名）。TXT 只是给人看的，读回会明确报错。"""
    fmt = format_for_path(path)
    if fmt not in READABLE_FORMATS:
        raise ValueError("TXT 记录只用于查看，导入请用 JSON 或 CSV")
    try:
        with open(path, encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError as exc:
        raise ValueError(f"读不了这个文件：{exc}") from exc
    return from_csv_text(text) if fmt == FORMAT_CSV else from_json_text(text)


def file_filter() -> str:
    """保存/打开对话框的过滤器（默认选中 JSON）。"""
    filters = [
        f"{FORMATS[FORMAT_JSON]} (*.json)",
        f"{FORMATS[FORMAT_CSV]} (*.csv)",
        f"{FORMATS[FORMAT_TXT]} (*.txt)",
    ]
    filters.append("所有支持的格式 (*.json *.csv *.txt)")
    return ";;".join(filters)


def format_from_filter(text: str) -> str:
    """从文件对话框选中的过滤器反推格式（``... (*.csv)`` → ``csv``）。"""
    body = (text or "").lower()
    for fmt in FORMATS:
        if f".{fmt}" in body:
            return fmt
    return DEFAULT_FORMAT


# --------------------------------------------------------------- 导入到另一台


@dataclass
class ImportPlan:
    """把一份记录套到某台设备上的结果（先算清楚再决定写不写）。"""

    device: str
    writes: list[tuple[str, str, Any]] = field(default_factory=list)  # (点号, 显示名, 值)
    missing: list[str] = field(default_factory=list)  # 记录里有、这台设备没有
    readonly: list[str] = field(default_factory=list)  # 这台设备上只读/是命令

    def __len__(self) -> int:
        return len(self.writes)

    def summary(self) -> str:
        parts = [f"可写入 {len(self.writes)} 项"]
        if self.missing:
            parts.append(f"{len(self.missing)} 项这台设备没有（跳过）")
        if self.readonly:
            parts.append(f"{len(self.readonly)} 项只读或是命令（跳过）")
        return "，".join(parts)


def plan_import(record: ParamRecord, project: Project, device_name: str) -> ImportPlan:
    """按**点位名**把记录对到目标设备上（站号/设备名不同都没关系）。"""
    device: DeviceDef | None = project.device(device_name)
    if device is None:
        raise ValueError(f"工程里没有设备 {device_name}")
    plan = ImportPlan(device=device.name)
    for entry in record.entries:
        target = device.point(entry.point)
        if target is None:
            plan.missing.append(entry.point)
            continue
        if not is_setting(target):
            plan.readonly.append(entry.point)
            continue
        plan.writes.append((target.name, target.title, entry.value))
    return plan


def apply_plan(
    plan: ImportPlan,
    write: Callable[[str, Any], Any],
    on_written: Callable[[str, str, Any], None] | None = None,
) -> list[str]:
    """照计划逐个下发；返回出错说明（空列表 = 全部成功）。

    ``on_written(点号全名, 点号, 值)`` 只在下发**成功**后调用（记录本据此记账）。
    """
    errors: list[str] = []
    for point_name, _label, value in plan.writes:
        pid = point_id(plan.device, point_name)
        try:
            result = write(pid, value)
        except Exception as exc:  # noqa: BLE001 - 一条失败不该挡住其余
            errors.append(f"{pid}：{exc}")
            continue
        if result is False:
            errors.append(f"{pid}：写值未下发")
            continue
        if on_written is not None:
            on_written(pid, point_name, value)
    return errors
