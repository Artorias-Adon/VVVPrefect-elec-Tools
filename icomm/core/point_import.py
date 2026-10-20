"""点表识别：把别家的点表猜成本应用的点表，猜不动就明确要求客户改格式。

现场拿到的点表五花八门：列名一会儿"变量名"一会儿"Tag Name"，地址一会儿 `40001`
一会儿 `4x0001` 一会儿 `0x0010`，类型一会儿 `INT16` 一会儿 `有符号16位`，前面还常带
两三行标题/单位/空白。人工一行行抄最容易抄错（抄错地址 = 读错设备）。

这里的做法是三步，**每一步都可被人改**：

1. **认表头**：按同义词表匹配（"点号/变量名/点名/Tag/Name…"），认不出的列再看
   这一列的值长什么样（全是 `40001` 这种 → 地址列；全是 `INT16/REAL` → 类型列）；
2. **转值**：地址按 `4x/3x/1x/0x` 前缀与 `30001/40001` 编号换算成 0 基址偏移，
   类型/可写/单位/缩放按词表归一；认不出类型的按 `uint16` 并记一条提醒；
3. **出我们格式**：生成一份本应用 CSV（`设备/点号/显示名称/起始地址/…`），
   直接能走「文件 → 导入参数表」。

**猜不动就报错**（:class:`PointTableError`）：缺点号列、缺地址列、一行都转不出来时，
不硬猜 —— 报错里写清"看到了哪些表头、缺什么、本应用的列名有哪些"，
让客户照模板重写。这是刻意的：宁可让人重写一遍，也不能把错位的点表导进工程。

纯逻辑，不依赖 Qt；界面在 :mod:`icomm.ui.tools.point_import`。
"""

from __future__ import annotations

import csv
import io
import os
import re
import unicodedata
from dataclasses import dataclass, field

from . import sheetio
from typing import Any, Iterable, Sequence

from .models import DATA_TYPES, REGISTER_TYPES

#: 导出时的列（本应用参数表的必需三列在最前，后面带上常改的那些）
EXPORT_COLUMNS: tuple[str, ...] = (
    "设备",
    "点号",
    "显示名称",
    "起始地址",
    "类型",
    "寄存器区",
    "单位",
    "缩放",
    "偏移",
    "小数位",
    "可写",
    "分组",
    "从站号",
    "备注",
)

#: 目标字段 -> 中文名（界面上"目标字段"下拉用它，顺序也照这个来）
FIELDS: tuple[tuple[str, str], ...] = (
    ("name", "点号"),
    ("label", "显示名称"),
    ("device", "设备"),
    ("unit_id", "从站号"),
    ("address", "起始地址"),
    ("data_type", "类型"),
    ("register", "寄存器区"),
    ("unit", "单位"),
    ("scale", "缩放"),
    ("offset", "偏移"),
    ("decimals", "小数位"),
    ("writable", "可写"),
    ("group", "分组"),
    ("bits", "位定义"),
    ("commands", "命令"),
    ("codes", "状态码"),
    ("write_min", "写值下限"),
    ("write_max", "写值上限"),
    ("hi", "高限"),
    ("hi_hi", "高高限"),
    ("lo", "低限"),
    ("lo_lo", "低低限"),
    ("description", "备注"),
)

FIELD_LABELS: dict[str, str] = dict(FIELDS)

#: 必需字段（缺了就报错，让客户改格式，而不是瞎猜）
REQUIRED_FIELDS: tuple[str, ...] = ("name", "address")

#: 表头同义词（键是目标字段；值里都写成"规范化"后的样子也无所谓，代码会再规范一次）
SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": (
        "点号", "点名", "变量名", "变量名称", "变量", "标识", "标识符", "符号名", "标签",
        "信号名", "信号名称", "tagname", "tag name", "tag", "name", "item name",
        "signal name", "symbol", "变量代号",
    ),
    "label": (
        "显示名称", "点位名称", "名称", "描述", "说明", "中文名", "中文描述", "注释",
        "点描述", "参数名称", "测点名称", "label", "description", "comment", "desc",
        "caption", "parameter name", "point name",
    ),
    "device": (
        "设备", "设备名", "设备名称", "装置", "装置名", "装置名称", "站名", "device",
        "device name", "station", "装置号",
    ),
    "unit_id": (
        "从站号", "站号", "从站地址", "站地址", "从机号", "slave", "slave id",
        "slaveid", "unit id", "unitid", "device id", "node id", "节点号",
    ),
    "address": (
        "起始地址", "地址", "寄存器地址", "寄存器号", "寄存器", "偏移", "偏移地址",
        "address", "addr", "reg", "register", "register address", "offset", "index",
        "modbus address", "寄存器偏移",
    ),
    "data_type": (
        "类型", "数据类型", "数据格式", "格式", "点位类型", "data type", "datatype",
        "type", "data format", "format",
    ),
    "register": (
        "寄存器区", "区域", "寄存器类型", "功能码", "区", "register type", "area",
        "function", "function code", "table", "寄存器区域",
    ),
    "unit": ("单位", "工程单位", "unit", "eu", "units", "量纲"),
    "scale": (
        "缩放", "缩放系数", "系数", "倍率", "比例", "比例系数", "变比", "gain",
        "scale", "factor", "multiplier", "ratio",
    ),
    "offset": ("偏移量", "零点", "加数", "offset value", "add", "bias"),
    "decimals": (
        "小数位", "小数位数", "小数", "精度", "小数点位数", "decimals", "decimal",
        "precision", "digits",
    ),
    "writable": (
        "可写", "读写", "读写属性", "读写权限", "属性", "访问权限", "access", "rw",
        "r/w", "readwrite", "read/write", "permission", "write",
    ),
    "group": ("分组", "组", "画面分组", "设备分组", "group", "category", "类别"),
    "bits": ("位定义", "位含义", "位说明", "位", "bits", "bit definition", "bit"),
    "commands": ("命令", "命令按钮", "commands", "command"),
    "codes": ("状态码", "状态码表", "codes", "code table"),
    "write_min": ("写值下限", "下限", "最小值", "最小", "min", "minimum", "min value"),
    "write_max": ("写值上限", "上限", "最大值", "最大", "max", "maximum", "max value"),
    "hi": ("高限", "上限报警", "hi", "high", "hi alarm"),
    "hi_hi": ("高高限", "高高限报警", "hi hi", "hihi", "high high"),
    "lo": ("低限", "下限报警", "lo", "low", "lo alarm"),
    "lo_lo": ("低低限", "低低限报警", "lo lo", "lolo", "low low"),
    "description": ("备注", "remark", "remarks", "note", "notes", "memo", "说明信息"),
}

#: 数据类型词表（规范化后的小写文本 -> 本应用的 data_type）
TYPE_WORDS: dict[str, str] = {
    "bool": "bool", "boolean": "bool", "bit": "bool", "coil": "bool", "di": "bool",
    "开关量": "bool", "布尔": "bool", "状态位": "bool", "开关": "bool",
    "uint8": "uint8", "usint": "uint8", "byte": "uint8", "无符号8": "uint8",
    "int8": "int8", "sint": "int8", "有符号8": "int8",
    "uint16": "uint16", "uint": "uint16", "word": "uint16", "u16": "uint16",
    "ushort": "uint16", "无符号16": "uint16", "无符号整数": "uint16",
    "16位无符号": "uint16", "正整数": "uint16",
    "int16": "int16", "int": "int16", "short": "int16", "s16": "int16",
    "integer": "int16", "有符号16": "int16", "有符号整数": "int16",
    "16位有符号": "int16", "整数": "int16", "整型": "int16",
    "uint32": "uint32", "udint": "uint32", "dword": "uint32", "u32": "uint32",
    "无符号32": "uint32", "32位无符号": "uint32",
    "int32": "int32", "dint": "int32", "long": "int32", "s32": "int32",
    "有符号32": "int32", "32位有符号": "int32", "长整型": "int32",
    "float32": "float32", "real": "float32", "float": "float32", "single": "float32",
    "f32": "float32", "浮点": "float32", "浮点数": "float32", "单精度": "float32",
    "float64": "float64", "lreal": "float64", "double": "float64", "f64": "float64",
    "双精度": "float64",
    "16位": "uint16", "32位": "uint32",
}

#: 单位词表（只是用来判断"这一列像不像单位列"，不改变值）
UNIT_WORDS: frozenset[str] = frozenset(
    {
        "v", "kv", "a", "ka", "ma", "w", "kw", "mw", "var", "kvar", "va", "kva",
        "wh", "kwh", "mwh", "hz", "khz", "℃", "°c", "c", "k", "s", "ms", "h", "min",
        "pa", "kpa", "mpa", "bar", "%", "rpm", "nm", "m3/h", "m³/h", "l", "m", "mm",
        "m/s", "kv", "pf", "cosφ", "ph", "次", "个", "位",
    }
)

#: 可写词表
WRITE_WORDS: dict[str, bool] = {
    "rw": True, "r/w": True, "readwrite": True, "read write": True, "读写": True,
    "可写": True, "可读写": True, "是": True, "y": True, "yes": True, "1": True,
    "w": True, "write": True, "writeonly": True, "只写": True,
    "r": False, "ro": False, "r/o": False, "read": False, "readonly": False,
    "只读": False, "读": False, "否": False, "n": False, "no": False, "0": False,
}

#: 寄存器区词表
REGISTER_WORDS: dict[str, str] = {
    "coil": "coil", "coils": "coil", "0x": "coil", "线圈": "coil", "线圈状态": "coil",
    "discrete": "discrete", "discreteinput": "discrete", "di": "discrete", "1x": "discrete",
    "离散输入": "discrete", "输入位": "discrete", "开入": "discrete",
    "holding": "holding", "holdingregister": "holding", "4x": "holding", "hr": "holding",
    "保持寄存器": "holding", "保持": "holding",
    "input": "input", "inputregister": "input", "3x": "input", "ir": "input",
    "输入寄存器": "input", "输入": "input", "模拟量输入": "input",
}

#: 认不出来的列头超过这个比例就认为"这表不是点表"
_MIN_RECOGNIZED = 0.34


class PointTableError(ValueError):
    """点表认不出来。**故意报错**：宁可让客户改格式，也不能把错位的点表导进工程。"""

    def __init__(
        self,
        message: str,
        *,
        headers: Sequence[str] = (),
        missing: Sequence[str] = (),
        detail: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.headers = list(headers)
        self.missing = list(missing)
        self.detail = list(detail)

    def as_text(self) -> str:
        """给界面/客户看的多行说明（含"下一步怎么办"）。"""
        lines = [f"❌ {self.message}"]
        if self.headers:
            shown = "、".join(str(h) for h in self.headers[:30])
            more = "" if len(self.headers) <= 30 else f" …（共 {len(self.headers)} 列）"
            lines.append("")
            lines.append(f"看到过的表头：{shown}{more}")
        if self.missing:
            names = "、".join(FIELD_LABELS.get(key, key) for key in self.missing)
            lines.append("")
            lines.append(f"缺什么：{names}")
        if self.detail:
            lines.append("")
            lines.extend(f"· {item}" for item in self.detail)
        lines.append("")
        lines.append("怎么办（二选一）：")
        lines.append(
            "  1. 用「参数表：生成模板」导出一份本应用的空点表，把内容贴进去再导回来（最稳）；"
        )
        lines.append(
            "  2. 或者把表头改成下面这些名字之一："
            + "、".join(FIELD_LABELS[key] for key, _label in FIELDS[:10])
            + "。"
        )
        lines.append("")
        lines.append("本应用点表列：" + "、".join(FIELD_LABELS[key] for key, _label in FIELDS))
        return "\n".join(lines)


@dataclass
class ColumnGuess:
    """一列的识别结论。"""

    index: int
    header: str
    field_key: str = ""
    confidence: float = 0.0
    reason: str = ""

    @property
    def field_label(self) -> str:
        return FIELD_LABELS.get(self.field_key, "（不导入）")


@dataclass
class ConvertedRow:
    """转换后的一行（值已按本应用的写法归一）。

    ``issues`` 只放"这一行不能用"的硬伤（地址认不出之类）；
    换算假设、默认值这类**说明**放 ``notes`` —— 不然每一行都有"问题"，
    用户会被一堆正常提示淹没，反而看不见真正要改的那行。
    """

    line_no: int
    values: dict[str, str] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SourceTable:
    """读进来的原始表。"""

    path: str = ""
    text: str = ""
    encoding: str = ""
    delimiter: str = ","
    header_line: int = 0
    rows: list[list[str]] = field(default_factory=list)

    @property
    def headers(self) -> list[str]:
        if not self.rows:
            return []
        return [str(cell).strip() for cell in self.rows[self.header_line]]

    @property
    def data_rows(self) -> list[list[str]]:
        start = self.header_line + 1
        return self.rows[start:]

    @property
    def name(self) -> str:
        return os.path.splitext(os.path.basename(self.path))[0] if self.path else ""

    def rebuild(self, text: str, delimiter: str | None = None) -> None:
        """用新文本（或新分隔符）重读，保留表头行下标。"""
        self.text = text
        self.delimiter = delimiter or self.delimiter
        self.rows = _parse_rows(text, self.delimiter)


# --------------------------------------------------------------- 读文件


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """尽量把字节解成文本，返回 ``(文本, 编码名)``。"""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:  # pragma: no cover - 极少见
            pass
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", "replace"), "latin-1"  # pragma: no cover


def detect_delimiter(text: str, sample_lines: int = 12) -> str:
    """看前几行里逗号/制表符/分号哪个最稳定，选它。"""
    lines = [line for line in text.splitlines() if line.strip()][:sample_lines]
    if not lines:
        return ","
    best, best_score = ",", 0.0
    for delimiter in (",", "\t", ";", "|"):
        counts = [line.count(delimiter) for line in lines]
        if max(counts) <= 0:
            continue
        common = max(set(counts), key=counts.count)
        stable = counts.count(common) / len(counts)
        score = common * stable
        if score > best_score:
            best, best_score = delimiter, score
    return best


def _parse_rows(text: str, delimiter: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows: list[list[str]] = []
    for row in reader:
        rows.append([str(cell).strip() for cell in row])
    return rows


def find_header_line(rows: Sequence[Sequence[str]], limit: int = 25) -> int:
    """在前若干行里找表头：**从上往下**，第一行"至少两格像字段名"的就是。

    为什么不挑"得分最高的行"：数据行里也常出现 `INT16`、`kW` 这种字眼，
    按分数挑会把最后一行数据当成表头（踩过）。点表的表头总在最上面，
    所以"从上往下第一个够像的"既准又简单。
    """
    for index, row in enumerate(rows[:limit]):
        cells = [str(cell).strip() for cell in row if str(cell).strip()]
        if len(cells) < 2:
            continue
        hits = sum(1 for cell in cells if _match_header(cell)[1] >= 0.5)
        if hits >= 2:
            return index
    # 一列都认不出来的表（表头是 A/B/C 这种）：退回第一行有 ≥2 个非空格的行
    for index, row in enumerate(rows[:limit]):
        cells = [str(cell).strip() for cell in row if str(cell).strip()]
        if len(cells) >= 2:
            return index
    return 0


def read_excel(path: str) -> list[list[str]]:
    """读 .xlsx/.xls 的第一个工作表（xlsx 用 openpyxl、xls 用 xlrd）。

    缺库时抛 :class:`PointTableError` 告诉用户怎么绕（另存 CSV 或装依赖）。
    """
    try:
        return sheetio.read_rows(path)
    except sheetio.SheetError as error:
        raise PointTableError(
            str(error),
            detail=[
                "在 Excel 里「另存为 → CSV UTF-8（逗号分隔）」再用本工具打开那个 csv；",
                "或者 pip install openpyxl xlrd 之后重试。",
            ],
        ) from error


def load_table(path: str, *, delimiter: str = "", header_line: int = -1) -> SourceTable:
    """读一个点表文件（CSV / TXT / TSV / Excel .xlsx / Excel 97-2003 .xls）。"""
    if not os.path.exists(path):
        raise PointTableError(f"找不到文件：{path}")
    extension = os.path.splitext(path)[1].lower()
    if extension in (".xlsx", ".xlsm", ".xltx", ".xls"):
        rows = read_excel(path)
        label = "xlsx（openpyxl）" if extension != ".xls" else "xls（xlrd）"
        table = SourceTable(path=path, encoding=label, rows=rows)
    else:
        with open(path, "rb") as handle:
            raw = handle.read()
        text, encoding = decode_bytes(raw)
        if not text.strip():
            raise PointTableError("这个文件是空的。")
        table = SourceTable(
            path=path,
            text=text,
            encoding=encoding,
            delimiter=delimiter or detect_delimiter(text),
            rows=_parse_rows(text, delimiter or detect_delimiter(text)),
        )
    if not table.rows:
        raise PointTableError("这个文件里没有可用的表格内容。")
    table.header_line = header_line if header_line >= 0 else find_header_line(table.rows)
    return table


# --------------------------------------------------------------- 认表头


def _normalize(text: Any) -> str:
    """表头/值的规范化：全角转半角、去空白与标点、转小写。"""
    raw = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    return re.sub(r"[\s_\-\./\\()（）\[\]【】:：,，;；\"'“”]+", "", raw)


_SYNONYM_INDEX: list[tuple[str, str]] = [
    (field_key, _normalize(word))
    for field_key, words in SYNONYMS.items()
    for word in words
    if _normalize(word)
]


def _match_header(header: str) -> tuple[str, float]:
    """表头 -> (目标字段, 置信度)。0 表示没认出来。"""
    normalized = _normalize(header)
    if not normalized:
        return ("", 0.0)
    best_field, best_score = "", 0.0
    for field_key, word in _SYNONYM_INDEX:
        if normalized == word:
            score = 1.0
        elif len(word) >= 2 and word in normalized:
            # 包含匹配：词越长越可信（"设备名称"要先于"名称"）
            score = 0.55 + min(0.3, len(word) * 0.04)
        elif len(normalized) >= 2 and normalized in word and len(normalized) >= 3:
            score = 0.5
        else:
            continue
        if score > best_score or (score == best_score and len(word) > 0):
            best_field, best_score = field_key, score
    return (best_field, best_score) if best_score >= 0.5 else ("", 0.0)


def _value_profile(rows: Sequence[Sequence[str]], index: int, limit: int = 40) -> list[str]:
    values: list[str] = []
    for row in rows:
        if index < len(row):
            cell = str(row[index]).strip()
            if cell:
                values.append(cell)
        if len(values) >= limit:
            break
    return values


def _guess_by_values(rows: Sequence[Sequence[str]], index: int) -> tuple[str, float, str]:
    """表头没认出来时，看这一列的值长什么样。"""
    values = _value_profile(rows, index)
    if len(values) < 3:
        return ("", 0.0, "")
    total = len(values)

    def ratio(predicate) -> float:
        return sum(1 for value in values if predicate(value)) / total

    if ratio(lambda v: parse_address(v)[0] is not None) >= 0.7:
        return ("address", 0.7, "这一列的值都像寄存器地址")
    if ratio(lambda v: _normalize(v) in TYPE_WORDS) >= 0.7:
        return ("data_type", 0.7, "这一列的值都是数据类型")
    if ratio(lambda v: _normalize(v) in WRITE_WORDS) >= 0.8:
        return ("writable", 0.65, "这一列的值都是读写属性（R/W/RW/只读…）")
    if ratio(lambda v: _normalize(v) in UNIT_WORDS) >= 0.8:
        return ("unit", 0.6, "这一列的值都是单位")
    if ratio(lambda v: _normalize(v) in REGISTER_WORDS) >= 0.8:
        return ("register", 0.6, "这一列都是寄存器区（线圈/保持/输入…）")
    non_numeric = ratio(lambda v: not re.fullmatch(r"[-+]?\d+(\.\d+)?", v))
    if non_numeric >= 0.9:
        ascii_ratio = ratio(lambda v: all(ord(ch) < 128 for ch in v))
        if ascii_ratio >= 0.8:
            return ("name", 0.55, "这一列是英文/点号样子的名字")
        return ("label", 0.55, "这一列是中文名称")
    return ("", 0.0, "")


def guess_columns(table: SourceTable, extra_rows: Sequence[Sequence[str]] = ()) -> list[ColumnGuess]:
    """给每一列一个结论；同一目标字段只保留最可信的那一列。"""
    headers = table.headers
    body = list(table.data_rows) + list(extra_rows)
    guesses: list[ColumnGuess] = []
    for index, header in enumerate(headers):
        field_key, score = _match_header(header)
        reason = "表头名字对上了" if field_key else ""
        if not field_key or score < 0.7:
            value_field, value_score, value_reason = _guess_by_values(body, index)
            if value_field and value_score > score:
                field_key, score, reason = value_field, value_score, value_reason
        guesses.append(ColumnGuess(index=index, header=header, field_key=field_key,
                                   confidence=score, reason=reason))

    # 同一个目标字段被多列命中：只留置信度最高的那列
    best: dict[str, ColumnGuess] = {}
    for guess in guesses:
        if not guess.field_key:
            continue
        current = best.get(guess.field_key)
        if current is None or guess.confidence > current.confidence:
            best[guess.field_key] = guess
    for guess in guesses:
        keeper = best.get(guess.field_key)
        if guess.field_key and keeper is not guess:
            guess.field_key = ""
            label = FIELD_LABELS.get(keeper.field_key, keeper.field_key)
            guess.reason = f"与第 {keeper.index + 1} 列（{label}）重复，已保留那列"
    return guesses


# --------------------------------------------------------------- 转值


_ADDRESS_PREFIX = re.compile(r"^(?P<area>[0-9])x(?P<digits>[0-9]+)$", re.IGNORECASE)
_ADDRESS_HEX = re.compile(r"^(?:0x|16#)(?P<hex>[0-9a-f]+)$", re.IGNORECASE)
_ADDRESS_DIGITS = re.compile(r"^[0-9]+$")


#: 地址写法：自动 / 前缀（4x0001）/ 十六进制（0x0010）
ADDRESS_STYLES: tuple[tuple[str, str], ...] = (
    ("auto", "自动判断"),
    ("prefix", "前缀写法（4x0001 / 3x0010）"),
    ("hex", "十六进制（0x0010 / 16#0010）"),
)


def parse_address(text: Any, style: str = "auto") -> tuple[int | None, str, str]:
    """把各种写法的地址转成 ``(0 基址偏移, 寄存器区或空, 说明)``。

    认识的写法：

    * ``40001`` / ``30001`` / ``00001`` / ``10001`` —— 传统 5 位编号，按 1 基址换算成偏移；
    * ``4x0001`` / ``3x0010`` / ``1x0001`` —— 前缀写法，按 1 基址换算；
    * ``0x0010`` / ``16#0010`` —— 十六进制数值（当作偏移，不推断区）；
    * ``16`` / ``128`` —— 直接当偏移。

    ``0x`` 开头是**真的有歧义**：前缀写法里 ``0x0001`` 是"第一个线圈"，
    十六进制里 ``0x0010`` 是"第 16 个寄存器"。所以：

    * ``16#`` 或带 A~F 的一律按十六进制；
    * ``1x / 3x / 4x`` 一律按前缀（它们不可能是十六进制）；
    * 光秃秃的 ``0x0001`` 按**十六进制**处理，并在说明里写清；
      如果这份表的 ``0x`` 其实是线圈区编号，把界面上「地址写法」改成"前缀写法"即可。
    """
    raw = str(text or "").strip().replace(" ", "")
    if not raw:
        return (None, "", "")
    kind = (style or "auto").lower()
    match = _ADDRESS_PREFIX.match(raw)
    if match:
        area_digit = match.group("area")
        digits_text = match.group("digits")
        is_hex = kind == "hex" or (
            kind == "auto"
            and (
                raw.lower().startswith("16#")
                or any(ch in "abcdefABCDEF" for ch in raw[2:])
                or area_digit == "0"  # 0x0001 更常见的是十六进制
            )
        )
        # 只有 0x 才可能在"前缀"和"十六进制"之间摇摆；1x/3x/4x 一定是前缀
        if not is_hex or area_digit != "0":
            area = {
                "0": "coil", "1": "discrete", "3": "input", "4": "holding",
            }.get(area_digit, "")
            digits = int(digits_text)
            offset = max(0, digits - 1)  # 4x0001 是第一个保持寄存器
            return (offset, area, f"按 1 基址前缀写法换算（{raw} → 偏移 {offset}）")
    match = _ADDRESS_HEX.match(raw)
    if match:
        return (int(match.group("hex"), 16), "", "按十六进制数值处理")
    if _ADDRESS_DIGITS.match(raw):
        number = int(raw)
        if 40001 <= number <= 49999:
            return (number - 40001, "holding", f"按 4xxxx 编号换算（{raw} → 偏移 {number - 40001}）")
        if 30001 <= number <= 39999:
            return (number - 30001, "input", f"按 3xxxx 编号换算（{raw} → 偏移 {number - 30001}）")
        if 10001 <= number <= 19999:
            return (number - 10001, "discrete", f"按 1xxxx 编号换算（{raw} → 偏移 {number - 10001}）")
        # 0xxxx 编号只在"明确写成 5 位、带前导 0"时才认（00005）；
        # 光秃秃的 16 / 128 一律当作偏移 —— 否则"地址 16"会被错算成线圈第 15 个
        if 1 <= number <= 9999 and len(raw) == 5 and raw.startswith("0"):
            return (number - 1, "coil", f"按 0xxxx 编号换算（{raw} → 偏移 {number - 1}）")
        return (number, "", "")
    return (None, "", "")


def parse_type(text: Any) -> str:
    """把各种数据类型写法归一成本应用的 data_type；认不出来返回空串。"""
    normalized = _normalize(text)
    if not normalized:
        return ""
    if normalized in TYPE_WORDS:
        return TYPE_WORDS[normalized]
    # "INT16（有符号）" 这种带括号说明的
    for word, data_type in TYPE_WORDS.items():
        if len(word) >= 2 and word in normalized:
            return data_type
    return ""


def parse_writable(text: Any) -> bool | None:
    normalized = _normalize(text)
    if not normalized:
        return None
    if normalized in WRITE_WORDS:
        return WRITE_WORDS[normalized]
    for word, writable in WRITE_WORDS.items():
        if len(word) >= 2 and word in normalized:
            return writable
    return None


def parse_register(text: Any) -> str:
    normalized = _normalize(text)
    if not normalized:
        return ""
    if normalized in REGISTER_WORDS:
        return REGISTER_WORDS[normalized]
    for word, register in REGISTER_WORDS.items():
        if len(word) >= 2 and word in normalized:
            return register
    return ""


def _number(text: Any) -> str:
    """把数字写法规整成纯数字文本（认不出就返回空串）。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        value = float(raw)
    except ValueError:
        return ""
    return str(int(value)) if value.is_integer() else f"{value:g}"


# --------------------------------------------------------------- 转换


@dataclass
class ImportPreview:
    """识别结果：每列怎么理解、转出来哪些行、有什么问题。"""

    table: SourceTable
    guesses: list[ColumnGuess]
    rows: list[ConvertedRow] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    default_device: str = ""
    skipped: int = 0
    #: 地址写法（auto / prefix / hex），界面上可改
    address_style: str = "auto"

    # -------------------------------------------------- 映射
    def mapping(self) -> dict[str, int]:
        return {guess.field_key: guess.index for guess in self.guesses if guess.field_key}

    def mapping_field(self, index: int) -> str:
        for guess in self.guesses:
            if guess.index == index:
                return guess.field_key
        return ""

    def set_mapping(self, index: int, field_key: str) -> None:
        """人工改一列的目标字段（同一字段只能占一列，后改的会把旧的挤掉）。"""
        for guess in self.guesses:
            if guess.field_key == field_key and guess.index != index:
                guess.field_key = ""
        for guess in self.guesses:
            if guess.index == index:
                guess.field_key = field_key
        self.rebuild_rows()

    def rebuild_rows(self) -> None:
        converted, issues, skipped = convert_table(
            self.table, self.mapping(), default_device=self.default_device,
            address_style=self.address_style,
        )
        self.rows = converted
        self.issues = issues
        self.skipped = skipped

    # -------------------------------------------------- 输出
    @property
    def ok_rows(self) -> list[ConvertedRow]:
        """能用的行（``issues`` 里只有"这行被跳过"的硬伤）。"""
        return [row for row in self.rows if not row.issues]

    @property
    def note_count(self) -> int:
        return sum(len(row.notes) for row in self.rows)

    def summary(self) -> str:
        return (
            f"表头在第 {self.table.header_line + 1} 行，识别 {len(self.mapping())} 列，"
            f"转换 {len(self.ok_rows)} 行"
            + (f"（跳过 {self.skipped} 行）" if self.skipped else "")
            + (f"，{self.note_count} 条换算说明" if self.note_count else "")
        )

    def csv_text(self) -> str:
        """导出成**本应用参数表**格式（可直接「文件 → 导入参数表」）。"""
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(list(EXPORT_COLUMNS))
        for row in self.ok_rows:
            writer.writerow([row.values.get(column, "") for column in EXPORT_COLUMNS])
        return buffer.getvalue()

    def write_csv(self, path: str) -> str:
        """按扩展名写：``.csv`` 或 ``.xlsx``（都是本应用参数表格式）。"""
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        if os.path.splitext(path)[1].lower() in (".xlsx", ".xls"):
            rows = [list(EXPORT_COLUMNS)]
            for row in self.ok_rows:
                rows.append([row.values.get(column, "") for column in EXPORT_COLUMNS])
            sheetio.write_rows(path, rows, sheet_title="参数表")
            return path
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            handle.write(self.csv_text())
        return path

    def fields(self) -> set[str]:
        return set(self.mapping())


def _cell(row: Sequence[str], index: int | None) -> str:
    if index is None or index < 0 or index >= len(row):
        return ""
    return str(row[index]).strip()


def convert_table(
    table: SourceTable,
    mapping: dict[str, int],
    *,
    default_device: str = "",
    address_style: str = "auto",
) -> tuple[list[ConvertedRow], list[str], int]:
    """按映射把数据行转成我们点表的行；返回 ``(行, 问题清单, 跳过行数)``。"""
    rows: list[ConvertedRow] = []
    issues: list[str] = []
    skipped = 0
    seen: set[tuple[str, str]] = set()
    device_fallback = default_device or table.name or "导入设备"

    name_index = mapping.get("name")
    address_index = mapping.get("address")
    for offset, raw in enumerate(table.data_rows):
        line_no = table.header_line + 2 + offset  # 给用户看的行号（1 基）
        if not any(str(cell).strip() for cell in raw):
            continue
        name = _cell(raw, name_index)
        address_text = _cell(raw, address_index)
        if not name and not address_text:
            skipped += 1
            issues.append(f"第 {line_no} 行：既没有点号也没有地址，跳过")
            continue
        address, area_from_address, address_note = parse_address(address_text, address_style)
        if address is None:
            skipped += 1
            issues.append(
                f"第 {line_no} 行：地址「{address_text}」认不出来（支持 40001 / 4x0001 / 0x0010 / 16 这几种写法）"
            )
            continue
        generated_name = ""
        if not name:
            name = f"P{address:04d}"
            generated_name = f"没有点号，按地址生成 {name}"

        device = _cell(raw, mapping.get("device")) or device_fallback
        key = (device, name)
        rename_note = ""
        if key in seen:
            counter = 2
            while (device, f"{name}_{counter}") in seen:
                counter += 1
            rename_note = f"点号重复，已改名为 {name}_{counter}"
            name = f"{name}_{counter}"
            key = (device, name)
        seen.add(key)

        values: dict[str, str] = {
            "设备": device,
            "点号": name,
            "显示名称": _cell(raw, mapping.get("label")) or name,
            "起始地址": str(address),
        }
        row_notes: list[str] = []
        for extra_note in (address_note, generated_name, rename_note):
            if extra_note:
                row_notes.append(extra_note)

        type_text = _cell(raw, mapping.get("data_type"))
        data_type = parse_type(type_text) if type_text else ""
        if type_text and not data_type:
            row_notes.append(f"类型「{type_text}」认不出来，按 uint16 处理")
        values["类型"] = data_type or "uint16"

        register = parse_register(_cell(raw, mapping.get("register"))) or area_from_address
        if not register and values["类型"] == "bool":
            register = "coil"  # 开关量没写区就按线圈，别塞进保持寄存器
        values["寄存器区"] = register or "holding"

        values["单位"] = _cell(raw, mapping.get("unit"))
        for column, field_key in (("缩放", "scale"), ("偏移", "offset"), ("小数位", "decimals")):
            text = _cell(raw, mapping.get(field_key))
            if text:
                number = _number(text)
                if number:
                    values[column] = number
                else:
                    row_notes.append(f"{column}「{text}」不是数字，已忽略")

        writable_text = _cell(raw, mapping.get("writable"))
        writable = parse_writable(writable_text) if writable_text else None
        if writable is None and writable_text:
            row_notes.append(f"读写属性「{writable_text}」认不出来，按只读处理")
        # 写"是/否"而不是"可写/只读"：导入器（csv_table._to_bool）只认 是/否/1/0/true/false，
        # 写"可写"会被当默认值 False —— 明明可写的点变成只读，这种错最难查
        values["可写"] = "是" if writable else "否"

        values["分组"] = _cell(raw, mapping.get("group"))
        unit_id = _cell(raw, mapping.get("unit_id"))
        values["从站号"] = _number(unit_id) if unit_id else ""

        extras: list[str] = []
        for field_key, label in (("bits", "位定义"), ("commands", "命令"), ("codes", "状态码")):
            text = _cell(raw, mapping.get(field_key))
            if text:
                values[field_key] = text
                extras.append(f"{label}：{text}")
        notes = [value for value in (
            _cell(raw, mapping.get("description")),
            *extras,
        ) if value]
        values["备注"] = "；".join(notes)

        for field_key in ("write_min", "write_max", "hi", "hi_hi", "lo", "lo_lo"):
            text = _cell(raw, mapping.get(field_key))
            if text and _number(text):
                values[field_key] = _number(text)

        rows.append(ConvertedRow(line_no=line_no, values=values, notes=row_notes))
    return rows, issues, skipped


# --------------------------------------------------------------- 入口


def load_preview(
    path: str, *, default_device: str = "", address_style: str = "auto"
) -> ImportPreview:
    """读文件 + 认列 + 转换，一步到位。认不出来抛 :class:`PointTableError`。"""
    table = load_table(path)
    guesses = guess_columns(table)
    mapping = {guess.field_key: guess.index for guess in guesses if guess.field_key}

    recognized = len(mapping)
    if recognized == 0:
        raise PointTableError(
            "这份表的表头一列都没认出来，看起来不是点表。",
            headers=table.headers,
            missing=REQUIRED_FIELDS,
        )
    missing = [key for key in REQUIRED_FIELDS if key not in mapping]
    if missing:
        raise PointTableError(
            "这份表缺了必须的两列之一，没法自动转换。",
            headers=table.headers,
            missing=missing,
            detail=[
                "点号那列决定每个变量叫什么（英文/下划线最好，工程里靠它当唯一标识）；",
                "地址那列决定读哪里（40001 / 4x0001 / 0x0010 / 16 这些写法都认）。",
            ],
        )

    rows, issues, skipped = convert_table(
        table, mapping, default_device=default_device,
        address_style=address_style,
    )
    preview = ImportPreview(
        table=table,
        guesses=guesses,
        rows=rows,
        issues=issues,
        default_device=default_device,
        skipped=skipped,
        address_style=address_style,
    )
    if not preview.ok_rows:
        raise PointTableError(
            "表头认出来了，但一行都转换不出来（地址全认不出？）。",
            headers=table.headers,
            missing=[],
            detail=issues[:10] or ["数据行是空的。"],
        )
    return preview


__all__ = [
    "ColumnGuess",
    "ConvertedRow",
    "EXPORT_COLUMNS",
    "FIELDS",
    "FIELD_LABELS",
    "ImportPreview",
    "PointTableError",
    "REQUIRED_FIELDS",
    "SourceTable",
    "convert_table",
    "decode_bytes",
    "detect_delimiter",
    "find_header_line",
    "guess_columns",
    "load_preview",
    "load_table",
    "parse_address",
    "parse_register",
    "parse_type",
    "parse_writable",
]
