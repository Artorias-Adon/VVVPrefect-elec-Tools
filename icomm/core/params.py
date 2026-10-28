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

import csv
import io
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .models import DeviceDef, PointDef, Project, point_id
from . import sheetio

#: 记录文件的格式标记与版本
FILE_FORMAT = "vvphmi-params"
FILE_VERSION = 1

FORMAT_JSON = "json"
FORMAT_CSV = "csv"
FORMAT_XLSX = "xlsx"
FORMAT_XLS = "xls"
FORMAT_TXT = "txt"

#: 保存对话框里的下拉项（顺序即下拉顺序，第一个是默认）
FORMATS: dict[str, str] = {
    FORMAT_JSON: "JSON 记录（默认，信息最全）",
    FORMAT_CSV: "CSV（Excel 可编辑）",
    FORMAT_XLSX: "Excel 工作簿（.xlsx）",
    FORMAT_TXT: "TXT（纯文本，只用于查看）",
}

#: 默认保存格式
DEFAULT_FORMAT = FORMAT_JSON

#: 这些格式能读回来（TXT 只用于人看；.xls 能读不能写）
READABLE_FORMATS = (FORMAT_JSON, FORMAT_CSV, FORMAT_XLSX, FORMAT_XLS)

#: CSV 的表头（也是读回时的识别依据）
CSV_HEADER = ["点号", "显示名", "值", "单位", "地址", "下发时间"]

# --------------------------------------------------------------- 下发历史

#: 历史文件的格式标记与版本
HISTORY_FORMAT = "vvphmi-param-history"
HISTORY_VERSION = 1

#: 自动留底的文件名：跟着工程走（``<工程>/data/params_history.jsonl``），一行一条、只追加
HISTORY_FILE_NAME = "params_history.jsonl"

#: 流水行格式（只给 ``HISTORY_FILE_NAME`` 用；导出表格仍走 xlsx / csv）
FORMAT_JSONL = "jsonl"

#: 历史表格的表头（导出用；导入按**列名**识别，列的顺序可以变）
HISTORY_HEADER = [
    "设备", "点号", "显示名", "值", "单位", "数据类型", "地址", "下发时间", "来源",
]

#: 历史最多留多少条（超出丢最旧的，免得长时间开着把内存撑大）
HISTORY_LIMIT = 5000

#: 同一个点位的同一个值在这几秒内重复下发只算一条
#: （持续发送、导入后主窗口回写一次，都不该把历史灌满）
MERGE_SECONDS = 5.0

SOURCE_MANUAL = "手动"
SOURCE_IMPORT = "导入"
SOURCE_AUTO = "自动"
SOURCE_SNAPSHOT = "记录当前值"

#: 这些来源只更新「最新值」，不进「下发历史」—— 回读不是下发
NO_HISTORY_SOURCES = (SOURCE_SNAPSHOT,)

#: 认得的几种时间写法（导出的就是第一种；手工在 Excel 里改了格式也尽量认）
TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
)


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def parse_time(text: Any) -> float:
    """把 ``2026-10-28 21:15:00`` 这类文本转成时间戳；认不出返回 ``-1``。"""
    body = str(text or "").strip()
    if not body:
        return -1.0
    for pattern in TIME_FORMATS:
        try:
            return time.mktime(time.strptime(body, pattern))
        except ValueError:
            continue
    return -1.0


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
class ParamEvent:
    """一条**下发记录**：谁（哪台设备）在什么时候把哪个参数下成了多少。

    与 :class:`ParamEntry` 的区别：Entry 是"这个点位现在是多少"（一台一份，会被覆盖），
    Event 是"这一次下发"（只追加、不覆盖），所以能查历史、能导出成带时间的表格。
    """

    device: str = ""
    point: str = ""
    label: str = ""
    value: Any = None
    unit: str = ""
    data_type: str = ""
    address: str = ""
    time: str = ""
    source: str = SOURCE_MANUAL

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "point": self.point,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "data_type": self.data_type,
            "address": self.address,
            "time": self.time,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "ParamEvent":
        if not isinstance(data, dict):
            raise ValueError("下发记录里的一条应该是对象")
        point = str(data.get("point") or "").strip()
        if not point:
            raise ValueError("下发记录里有一条缺「点号」")
        return cls(
            device=str(data.get("device") or ""),
            point=point,
            label=str(data.get("label") or ""),
            value=data.get("value"),
            unit=str(data.get("unit") or ""),
            data_type=str(data.get("data_type") or ""),
            address=str(data.get("address") or ""),
            time=str(data.get("time") or ""),
            source=str(data.get("source") or SOURCE_MANUAL),
        )

    def when(self) -> float:
        """时间戳（排序/取最后一次用；认不出算 ``-1``，排在最前）。"""
        return parse_time(self.time)

    def display(self) -> str:
        text = "" if self.value is None else str(self.value)
        return f"{text} {self.unit}".strip()

    def to_row(self) -> list[str]:
        return [
            self.device,
            self.point,
            self.label,
            "" if self.value is None else str(self.value),
            self.unit,
            self.data_type,
            self.address,
            self.time,
            self.source,
        ]


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
    """记下发过的参数（按设备 → 点号），同时留一份**下发历史**。"""

    def __init__(self, limit: int = HISTORY_LIMIT) -> None:
        self._by_device: dict[str, dict[str, ParamEntry]] = {}
        self._history: list[ParamEvent] = []
        self._last_event: dict[tuple[str, str], ParamEvent] = {}
        self._limit = max(1, int(limit))

    # -------------------------------------------------- 记

    def record(
        self,
        device_name: str,
        point: PointDef,
        value: Any,
        when: str | None = None,
        source: str = SOURCE_MANUAL,
    ) -> ParamEntry | None:
        """记一条；不是"参数"的点位（只读 / 命令）返回 None。"""
        if not is_setting(point):
            return None
        stamp = when or _now()
        cleaned = _clean(value)
        entry = ParamEntry(
            point=point.name,
            label=point.title,
            value=cleaned,
            unit=point.unit,
            address=address_text(point),
            data_type=point.data_type,
            decimals=point.decimals,
            time=stamp,
        )
        self._by_device.setdefault(device_name, {})[point.name] = entry
        self._remember(
            ParamEvent(
                device=device_name,
                point=point.name,
                label=point.title,
                value=cleaned,
                unit=point.unit,
                data_type=point.data_type,
                address=address_text(point),
                time=stamp,
                source=source,
            )
        )
        return entry

    def _remember(self, event: ParamEvent) -> None:
        """把一条下发记进历史（同一个值挨着重复下发只留一条）。"""
        if event.source in NO_HISTORY_SOURCES:
            return
        key = (event.device, event.point)
        last = self._last_event.get(key)
        if (
            last is not None
            and last.value == event.value
            and 0 <= event.when() - last.when() <= MERGE_SECONDS
        ):
            last.time = event.time
            return
        self._history.append(event)
        self._last_event[key] = event
        if len(self._history) > self._limit:
            del self._history[: len(self._history) - self._limit]
            self._last_event = {(item.device, item.point): item for item in self._history}

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
            if self.record(
                device.name, point, value, when, source=SOURCE_SNAPSHOT
            ) is not None:
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

    # -------------------------------------------------- 下发历史

    def events(
        self,
        device_name: str | None = None,
        point: str | None = None,
        time_from: str | None = None,
        time_to: str | None = None,
        source: str | None = None,
    ) -> list[ParamEvent]:
        """查询下发历史，**最新的在前**；条件留空表示不限。"""
        low = parse_time(time_from)
        high = parse_time(time_to)
        result: list[ParamEvent] = []
        for event in reversed(self._history):
            if device_name and event.device != device_name:
                continue
            if point and event.point != point:
                continue
            if source and event.source != source:
                continue
            when = event.when()
            if low >= 0 and when < low:
                continue
            if high >= 0 and when > high:
                continue
            result.append(event)
        return result

    def history_count(self, device_name: str | None = None) -> int:
        if not device_name:
            return len(self._history)
        return sum(1 for event in self._history if event.device == device_name)

    def last_event(self, device_name: str, point: str) -> ParamEvent | None:
        """某个点位最近一次下发（记完就知道该往文件里追加哪一条）。"""
        return self._last_event.get((device_name, point))

    def restore(self, events: Iterable[ParamEvent]) -> int:
        """把文件里留的历史读回内存（按时间排好，只留最新的 ``limit`` 条）。"""
        ordered = sorted(events, key=lambda event: event.when())
        self._history = ordered[-self._limit :]
        self._last_event = {
            (event.device, event.point): event for event in self._history
        }
        return len(self._history)

    def clear_history(self, device_name: str | None = None) -> None:
        """只清下发历史，「最新值」保留（查完历史想清干净但还要接着往下导时用）。"""
        if device_name is None:
            self._history.clear()
            self._last_event.clear()
            return
        self._history = [e for e in self._history if e.device != device_name]
        self._last_event = {
            key: value for key, value in self._last_event.items() if key[0] != device_name
        }

    # -------------------------------------------------- 清

    def clear(self, device_name: str | None = None) -> None:
        if device_name is None:
            self._by_device.clear()
        else:
            self._by_device.pop(device_name, None)
        self.clear_history(device_name)

    def __len__(self) -> int:
        return sum(len(entries) for entries in self._by_device.values())


# --------------------------------------------------------------- 存 / 读


def format_for_path(path: str) -> str:
    """按扩展名判断格式（不认识就当 JSON）。"""
    suffix = os.path.splitext(path)[1].lower().lstrip(".")
    if suffix in FORMATS:
        return suffix
    if suffix == FORMAT_XLS:
        # .xls 能读不能写：返回它，让 save() 给出明确提示，别偷偷写成 JSON
        return FORMAT_XLS
    return DEFAULT_FORMAT


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
    if chosen == FORMAT_XLS:
        raise ValueError(
            "不支持写 .xls（Excel 97-2003 的旧格式）。请存成 .xlsx 或 .csv。"
        )
    if chosen not in FORMATS:
        chosen = DEFAULT_FORMAT
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    if chosen == FORMAT_XLSX:
        sheetio.write_rows(path, to_rows(record), sheet_title=record.device or "参数记录")
        return path
    writers = {FORMAT_JSON: to_json_text, FORMAT_CSV: to_csv_text, FORMAT_TXT: to_txt_text}
    text = writers[chosen](record)
    with open(path, "w", encoding="utf-8-sig" if chosen == FORMAT_CSV else "utf-8", newline="") as handle:
        handle.write(text.lstrip("\ufeff") if chosen == FORMAT_CSV else text)
    return path


def load(path: str) -> ParamRecord:
    """读文件（按扩展名）。TXT 只是给人看的，读回会明确报错。"""
    fmt = format_for_path(path)
    if fmt not in READABLE_FORMATS:
        raise ValueError("TXT 记录只用于查看，导入请用 JSON 或 CSV（Excel 也可以）")
    if fmt in (FORMAT_XLSX, FORMAT_XLS):
        try:
            return from_rows(sheetio.read_rows(path))
        except sheetio.SheetError as exc:
            raise ValueError(str(exc)) from exc
    try:
        with open(path, encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError as exc:
        raise ValueError(f"读不了这个文件：{exc}") from exc
    return from_csv_text(text) if fmt == FORMAT_CSV else from_json_text(text)


def to_rows(record: ParamRecord) -> list[list[str]]:
    """记录 → 二维表（Excel 用；第一行是元信息，第二行是表头）。"""
    header = [
        "# 参数记录",
        f"设备={record.device}",
        f"从站号={record.unit_id}",
        f"保存时间={record.saved_at or _now()}",
    ]
    rows = [header, list(CSV_HEADER)]
    for entry in record.entries:
        rows.append(
            [
                entry.point,
                entry.label,
                "" if entry.value is None else str(entry.value),
                entry.unit,
                entry.address,
                entry.time,
            ]
        )
    return rows


def from_rows(rows: list[list[str]]) -> ParamRecord:
    """二维表 → 记录（与 :func:`from_csv_text` 同一套表头识别）。"""
    if not rows:
        raise ValueError("表格是空的")
    record = ParamRecord()
    body: list[list[str]] = []
    for row in rows:
        cells = [str(cell) for cell in row]
        if not any(cell.strip() for cell in cells):
            continue
        first = cells[0].strip()
        if first.startswith("#"):
            for part in cells[1:]:
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
        body.append(cells)
    if not body:
        raise ValueError("表格里没有数据行")
    header = [cell.strip() for cell in body[0]]
    if "点号" not in header or "值" not in header:
        raise ValueError("表格缺少「点号」或「值」列")
    index = {name: header.index(name) for name in header if name}

    def cell(row: list[str], name: str) -> str:
        position = index.get(name)
        if position is None or position >= len(row):
            return ""
        return row[position]

    for row in body[1:]:
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


def file_filter() -> str:
    """保存/打开对话框的过滤器（默认选中 JSON）。"""
    filters = [
        f"{FORMATS[FORMAT_JSON]} (*.json)",
        f"{FORMATS[FORMAT_CSV]} (*.csv)",
        f"{FORMATS[FORMAT_XLSX]} (*.xlsx)",
        f"{FORMATS[FORMAT_TXT]} (*.txt)",
    ]
    filters.append("所有支持的格式 (*.json *.csv *.xlsx *.xls *.txt)")
    return ";;".join(filters)


def format_from_filter(text: str) -> str:
    """从文件对话框选中的过滤器反推格式（``... (*.csv)`` → ``csv``）。"""
    body = (text or "").lower()
    for fmt in FORMATS:
        if f".{fmt}" in body:
            return fmt
    return DEFAULT_FORMAT


# --------------------------------------------------------------- 下发历史：存 / 读


def history_to_rows(
    events: Iterable[ParamEvent], saved_at: str | None = None
) -> list[list[str]]:
    """历史 → 二维表：第一行元信息、第二行表头、之后一条一行（Excel / CSV 共用）。"""
    materialised = list(events)
    rows = [
        ["# 参数下发历史", f"导出时间={saved_at or _now()}", f"条数={len(materialised)}"],
        list(HISTORY_HEADER),
    ]
    rows.extend(event.to_row() for event in materialised)
    return rows


def history_to_json_text(events: Iterable[ParamEvent], saved_at: str | None = None) -> str:
    materialised = list(events)
    payload = {
        "format": HISTORY_FORMAT,
        "version": HISTORY_VERSION,
        "saved_at": saved_at or _now(),
        "count": len(materialised),
        "events": [event.to_dict() for event in materialised],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def history_to_jsonl_text(events: Iterable[ParamEvent]) -> str:
    """一行一条（给自动留底用：追加写、断电也不会毁掉前面的行）。"""
    return "".join(
        json.dumps(event.to_dict(), ensure_ascii=False) + "\n" for event in events
    )


def history_from_jsonl_text(text: str) -> list[ParamEvent]:
    """读流水文件；坏行要说清第几行，别拿半个文件去下发。"""
    events: list[ParamEvent] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        body = raw.strip()
        if not body:
            continue
        try:
            events.append(ParamEvent.from_dict(json.loads(body)))
        except ValueError as exc:
            raise ValueError(f"第 {number} 行不是合法的下发记录：{exc}") from exc
    if not events:
        raise ValueError("这个文件里没有下发记录")
    return events


def history_to_txt(events: Iterable[ParamEvent], saved_at: str | None = None) -> str:
    """纯文本对表用（TXT 只给人看，导入请用 Excel / CSV / JSON）。"""
    materialised = list(events)

    def width(text: str) -> int:
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)

    heads = ["下发时间", "设备", "点号", "显示名", "值", "单位", "来源"]
    rows = [
        [
            event.time,
            event.device,
            event.point,
            event.label,
            "" if event.value is None else str(event.value),
            event.unit,
            event.source,
        ]
        for event in materialised
    ]
    widths = [max([width(head)] + [width(row[i]) for row in rows]) for i, head in enumerate(heads)]

    def line(cells: list[str]) -> str:
        return "  ".join(
            cell + " " * (widths[i] - width(cell)) for i, cell in enumerate(cells)
        ).rstrip()

    lines = [
        f"参数下发历史　导出时间：{saved_at or _now()}　共 {len(materialised)} 条",
        "",
        line(heads),
        "─" * min(120, sum(widths) + 2 * (len(widths) - 1)),
    ]
    lines.extend(line(row) for row in rows)
    lines.append("")
    lines.append("（TXT 只用于查看，导入请用 Excel / CSV / JSON）")
    return "\n".join(lines) + "\n"


def history_from_json_text(text: str) -> list[ParamEvent]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"不是合法的 JSON：{exc}") from exc
    if isinstance(data, dict) and "events" in data:
        raw = data.get("events")
        if not isinstance(raw, list):
            raise ValueError("下发历史文件里的 events 应该是列表")
        events = [ParamEvent.from_dict(item) for item in raw]
    elif isinstance(data, list):
        events = [ParamEvent.from_dict(item) for item in data]
    else:
        raise ValueError("这不是下发历史文件（缺少 events 列表）")
    if not events:
        raise ValueError("下发历史文件里没有记录")
    return events


def history_from_rows(rows: list[list[str]]) -> list[ParamEvent]:
    """二维表 → 历史。

    既认本文件导出的历史表（有「设备」「下发时间」列），也认老的「参数记录」
    表格（``点号 / 显示名 / 值 / 单位 / 地址 / 下发时间``）—— 老文件也能拿来导入。
    """
    if not rows:
        raise ValueError("表格是空的")
    meta_device = ""
    saved_at = ""
    header: list[str] | None = None
    body: list[list[str]] = []
    for row in rows:
        cells = [str(cell) for cell in row]
        if not any(cell.strip() for cell in cells):
            continue
        first = cells[0].strip()
        if first.startswith("#"):
            for part in cells[1:]:
                key, _sep, value = part.partition("=")
                key = key.strip()
                if key == "设备":
                    meta_device = value.strip()
                elif key == "保存时间":
                    saved_at = value.strip()
            continue
        if header is None:
            if "点号" in cells and "值" in cells:
                header = [cell.strip() for cell in cells]
            continue
        body.append(cells)
    if header is None:
        raise ValueError("表格里找不到「点号 / 值」表头")
    if not body:
        raise ValueError("表格里没有数据行")
    index = {name: position for position, name in enumerate(header) if name}

    def cell(row: list[str], name: str) -> str:
        position = index.get(name)
        if position is None or position >= len(row):
            return ""
        return row[position]

    events: list[ParamEvent] = []
    for row in body:
        point = cell(row, "点号").strip()
        if not point:
            continue
        data_type = cell(row, "数据类型").strip()
        value_text = cell(row, "值")
        value = _coerce(value_text, data_type)
        if not data_type and value_text.strip().lower() in ("true", "false"):
            # 没有类型列时，别把 False 当成"非空字符串"（那样会写进 1）
            value = value_text.strip().lower() == "true"
        events.append(
            ParamEvent(
                device=cell(row, "设备").strip() or meta_device,
                point=point,
                label=cell(row, "显示名").strip(),
                value=value,
                unit=cell(row, "单位").strip(),
                data_type=data_type,
                address=cell(row, "地址").strip(),
                time=cell(row, "下发时间").strip() or saved_at,
                source=cell(row, "来源").strip() or SOURCE_MANUAL,
            )
        )
    if not events:
        raise ValueError("表格里没有数据行")
    return events


def save_history(
    path: str, events: Iterable[ParamEvent], saved_at: str | None = None
) -> str:
    """把下发历史存成表格 / JSON（按扩展名定格式）。返回实际路径。"""
    materialised = list(events)
    if not materialised:
        raise ValueError("没有可保存的下发记录")
    if os.path.splitext(path)[1].lower() == f".{FORMAT_JSONL}":
        _ensure_parent(path)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(history_to_jsonl_text(materialised))
        return path
    chosen = format_for_path(path)
    if chosen == FORMAT_XLS:
        raise ValueError(
            "不支持写 .xls（Excel 97-2003 的旧格式）。请存成 .xlsx 或 .csv。"
        )
    _ensure_parent(path)
    stamp = saved_at or _now()
    if chosen == FORMAT_JSON:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(history_to_json_text(materialised, stamp))
        return path
    if chosen == FORMAT_TXT:
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(history_to_txt(materialised, stamp))
        return path
    sheetio.write_rows(path, history_to_rows(materialised, stamp), sheet_title="下发历史")
    return path


def _ensure_parent(path: str) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)


def append_history(path: str, events: Iterable[ParamEvent]) -> int:
    """往流水文件追加几条（自动留底用）。只追加，不动已有内容。"""
    materialised = list(events)
    if not materialised:
        return 0
    _ensure_parent(path)
    with open(path, "a", encoding="utf-8", newline="") as handle:
        handle.write(history_to_jsonl_text(materialised))
    return len(materialised)


def load_history(path: str) -> list[ParamEvent]:
    """读一份下发历史（也认老「参数记录」文件，一条记录当一次下发）。"""
    if os.path.splitext(path)[1].lower() == f".{FORMAT_JSONL}":
        try:
            with open(path, encoding="utf-8") as handle:
                return history_from_jsonl_text(handle.read())
        except OSError as exc:
            raise ValueError(f"读不了这个文件：{exc}") from exc
    fmt = format_for_path(path)
    if fmt == FORMAT_TXT:
        raise ValueError("TXT 历史只用于查看，导入请用 Excel / CSV / JSON")
    if fmt in (FORMAT_XLSX, FORMAT_XLS):
        try:
            return history_from_rows(sheetio.read_rows(path))
        except sheetio.SheetError as exc:
            raise ValueError(str(exc)) from exc
    try:
        with open(path, encoding="utf-8-sig") as handle:
            text = handle.read()
    except OSError as exc:
        raise ValueError(f"读不了这个文件：{exc}") from exc
    if fmt == FORMAT_JSON:
        try:
            return history_from_json_text(text)
        except ValueError as history_exc:
            try:
                record = from_json_text(text)
            except ValueError:
                raise history_exc from None
            return [
                ParamEvent(
                    device=record.device,
                    point=entry.point,
                    label=entry.label,
                    value=entry.value,
                    unit=entry.unit,
                    data_type=entry.data_type,
                    address=entry.address,
                    time=entry.time or record.saved_at,
                    source=SOURCE_MANUAL,
                )
                for entry in record.entries
            ]
    return history_from_rows([list(row) for row in csv.reader(io.StringIO(text))])


def history_file_filter() -> str:
    """下发历史保存 / 打开的过滤器（表格优先，现场多半要拿去 Excel 里查）。"""
    filters = [
        f"{FORMATS[FORMAT_XLSX]} (*.xlsx)",
        f"{FORMATS[FORMAT_CSV]} (*.csv)",
        f"{FORMATS[FORMAT_JSON]} (*.json)",
        f"{FORMATS[FORMAT_TXT]} (*.txt)",
        f"流水文件（自动留底） (*.{FORMAT_JSONL})",
        (
            "所有支持的格式 "
            f"(*.xlsx *.csv *.json *.xls *.txt *.{FORMAT_JSONL})"
        ),
    ]
    return ";;".join(filters)


def format_from_history_filter(text: str) -> str:
    """从历史保存对话框的过滤器反推格式（``.jsonl`` 要先判，否则会被 ``.json`` 抢走）。"""
    body = (text or "").lower()
    for fmt in (FORMAT_JSONL, FORMAT_XLSX, FORMAT_CSV, FORMAT_JSON, FORMAT_TXT):
        if f".{fmt}" in body:
            return fmt
    return FORMAT_CSV


# --------------------------------------------------------------- 下发历史：导入


@dataclass
class HistoryPlan:
    """把一份下发历史套到某台设备上的结果（每个点位只取**最后一次**）。"""

    device: str
    entries: list[ParamEvent] = field(default_factory=list)
    #: 记录里有、这台设备上没有的点号
    missing: list[str] = field(default_factory=list)
    #: 这台设备上只读、或者是命令的点号
    readonly: list[str] = field(default_factory=list)
    #: 同一个点位更早的那些下发被忽略了（只取最后一次）
    older: int = 0

    def __len__(self) -> int:
        return len(self.entries)

    def values(self) -> dict[str, Any]:
        return {event.point: event.value for event in self.entries}

    def summary(self) -> str:
        parts = [f"可下发 {len(self.entries)} 项"]
        if self.older:
            parts.append(f"{self.older} 条较早的下发按最后一次取（跳过）")
        if self.missing:
            parts.append(f"{len(self.missing)} 项这台设备没有（跳过）")
        if self.readonly:
            parts.append(f"{len(self.readonly)} 项只读或是命令（跳过）")
        return "，".join(parts)


def plan_history_import(
    events: Iterable[ParamEvent], project: Project, device_name: str
) -> HistoryPlan:
    """按**点号**把历史对到目标设备上，每个点位只留最后一次下发。

    文件里如果记了设备名，优先只取这台设备的记录；一条都没有（比如换了台
    同型号设备、名字不一样）就退回按点号匹配全部记录。
    """
    device: DeviceDef | None = project.device(device_name)
    if device is None:
        raise ValueError(f"工程里没有设备 {device_name}")
    materialised = list(events)
    pool = [event for event in materialised if event.device == device_name] or materialised
    latest: dict[str, ParamEvent] = {}
    for event in pool:
        current = latest.get(event.point)
        if current is None or event.when() >= current.when():
            latest[event.point] = event
    plan = HistoryPlan(device=device.name, older=max(0, len(pool) - len(latest)))
    for point in device.points:
        event = latest.pop(point.name, None)
        if event is None:
            continue
        if not is_setting(point):
            plan.readonly.append(point.name)
            continue
        plan.entries.append(event)
    plan.missing = sorted(latest)
    return plan


def apply_history_plan(
    plan: HistoryPlan,
    write: Callable[[str, Any], Any],
    on_written: Callable[[str, ParamEvent], None] | None = None,
) -> list[str]:
    """照计划逐个下发；返回出错说明（空列表 = 全部成功）。

    ``on_written(点号全名, 那次下发)`` 只在下发**成功后**调用（记录本据此记账）。
    """
    errors: list[str] = []
    for event in plan.entries:
        pid = point_id(plan.device, event.point)
        try:
            result = write(pid, event.value)
        except Exception as exc:  # noqa: BLE001 - 一条失败不该挡住其余
            errors.append(f"{pid}：{exc}")
            continue
        if result is False:
            errors.append(f"{pid}：写值未下发")
            continue
        if on_written is not None:
            on_written(pid, event)
    return errors


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
