"""表格文件读写：CSV / TXT / Excel（.xlsx / .xls）统一入口。

界面里"导出 CSV"「导入 CSV」的地方都走这里，所以：

* **导出**：``.csv``（UTF-8 BOM，Excel 直接双击可开）与 ``.xlsx``；
  ``.xls``（BIFF8）是微软的旧二进制格式，没有可靠的写入库，**不提供导出**，
  选了就给明确提示让用户换 ``.xlsx``。
* **导入**：``.csv`` / ``.txt``（自动试 UTF-8 / GB18030）与 ``.xlsx`` / ``.xls``。

Excel 读写用 openpyxl 与 xlrd（都在 ``requirements.txt`` 里）；缺库时不是崩掉，
而是抛 :class:`SheetError`，界面据此提示"另存为 CSV"或安装依赖。
"""

from __future__ import annotations

import csv
import io
import os
from typing import Any, Iterable, Sequence

#: 能读的扩展名（顺序即对话框里的顺序）
READ_EXTS: tuple[str, ...] = (".csv", ".txt", ".xlsx", ".xls")
#: 能写的扩展名
WRITE_EXTS: tuple[str, ...] = (".csv", ".xlsx")

#: 读 CSV/TXT 时依次尝试的编码（中文现场常见 GBK 导出的表）
TEXT_ENCODINGS: tuple[str, ...] = ("utf-8-sig", "utf-8", "gb18030", "utf-16")


class SheetError(ValueError):
    """表格文件读不了 / 写不了（消息直接给用户看）。"""


# --------------------------------------------------------------- 依赖探测


def _openpyxl():
    try:
        import openpyxl  # noqa: PLC0415 - 可选依赖，用到才 import

        return openpyxl
    except ImportError:
        return None


def _xlrd():
    try:
        import xlrd  # noqa: PLC0415 - 可选依赖，用到才 import

        return xlrd
    except ImportError:
        return None


def excel_ready() -> bool:
    """xlsx/xls 能不能用（两个库都在才算完整）。"""
    return _openpyxl() is not None and _xlrd() is not None


def missing_libs() -> list[str]:
    missing = []
    if _openpyxl() is None:
        missing.append("openpyxl（.xlsx）")
    if _xlrd() is None:
        missing.append("xlrd（.xls）")
    return missing


# --------------------------------------------------------------- 值转换


def cell_text(value: Any) -> str:
    """Excel 单元格 → 参数表里习惯的字符串。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        # 参数表里的布尔列只认 是/否（也认 true/false，但导出统一用中文）
        return "是" if value else "否"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return f"{value:g}"
    return str(value).strip()


def _rows_from_csv_text(text: str) -> list[list[str]]:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [list(row) for row in csv.reader(io.StringIO(text), dialect)]


def _decode_text(data: bytes) -> str:
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    # 最后一个兜底：不抛异常，坏字节替换掉，至少让用户看到内容
    return data.decode("utf-8", errors="replace")


# --------------------------------------------------------------- 读


def read_rows(path: str, *, sheet: int = 0) -> list[list[str]]:
    """读出第一个（或第 ``sheet`` 个）工作表的全部行，全部转成字符串。

    空行保留为空列表的占位（调用方自己决定要不要跳过），这样行号与文件对得上
    —— 点表识别报错时要告诉用户"第几行有问题"。
    """
    if not path or not os.path.isfile(path):
        raise SheetError(f"文件不存在：{path}")
    suffix = os.path.splitext(path)[1].lower()
    if suffix in (".csv", ".txt"):
        with open(path, "rb") as handle:
            return _rows_from_csv_text(_decode_text(handle.read()))
    if suffix == ".xlsx":
        return _read_xlsx(path, sheet)
    if suffix == ".xls":
        return _read_xls(path, sheet)
    raise SheetError(f"不认识的表格格式：{suffix or path}（支持 {', '.join(READ_EXTS)}）")


def _read_xlsx(path: str, sheet: int) -> list[list[str]]:
    openpyxl = _openpyxl()
    if openpyxl is None:
        raise SheetError("读 .xlsx 需要 openpyxl：请先 pip install openpyxl，或把表另存为 CSV")
    try:
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - 坏文件/加密/格式不符都归到这里
        raise SheetError(f"打不开 Excel：{exc}") from exc
    try:
        if not workbook.worksheets:
            raise SheetError("这个 Excel 里没有工作表")
        index = min(max(sheet, 0), len(workbook.worksheets) - 1)
        worksheet = workbook.worksheets[index]
        rows = [
            [cell_text(value) for value in row]
            for row in worksheet.iter_rows(values_only=True)
        ]
    finally:
        workbook.close()
    return rows


def _read_xls(path: str, sheet: int) -> list[list[str]]:
    xlrd = _xlrd()
    if xlrd is None:
        raise SheetError(
            "读 .xls（Excel 97-2003）需要 xlrd：请先 pip install xlrd，"
            "或在 Excel 里另存为 .xlsx / CSV"
        )
    try:
        book = xlrd.open_workbook(path)
    except Exception as exc:  # noqa: BLE001 - 同上
        raise SheetError(f"打不开 .xls：{exc}") from exc
    if book.nsheets == 0:
        raise SheetError("这个 Excel 里没有工作表")
    index = min(max(sheet, 0), book.nsheets - 1)
    table = book.sheet_by_index(index)
    rows: list[list[str]] = []
    for row_index in range(table.nrows):
        row: list[str] = []
        for cell in table.row(row_index):
            if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                row.append("")
                continue
            row.append(cell_text(cell.value))
        rows.append(row)
    return rows


def read_text(path: str) -> str:
    """按文本方式读（CSV/TXT 直接读；Excel 按行拼成 TSV，便于复用同一套解析）。"""
    with open(path, "rb") as handle:
        data = handle.read()
    return _decode_text(data)


# --------------------------------------------------------------- 写


def write_rows(
    path: str,
    rows: Iterable[Sequence[Any]],
    *,
    sheet_title: str = "Sheet1",
) -> str:
    """把二维数据写成 CSV 或 XLSX，返回实际写入的路径。"""
    suffix = os.path.splitext(path)[1].lower()
    data = [[cell_text(value) if not isinstance(value, str) else value for value in row] for row in rows]
    if suffix in (".csv", ".txt"):
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerows(data)
        return path
    if suffix == ".xlsx":
        return _write_xlsx(path, data, sheet_title)
    if suffix == ".xls":
        raise SheetError(
            "不支持写 .xls（Excel 97-2003 的旧二进制格式）。请另存为 .xlsx 或 .csv。"
        )
    raise SheetError(
        f"不认识的扩展名：{suffix or path}（可写 {', '.join(WRITE_EXTS)}）"
    )


def _write_xlsx(path: str, rows: list[list[str]], sheet_title: str) -> str:
    openpyxl = _openpyxl()
    if openpyxl is None:
        raise SheetError("写 .xlsx 需要 openpyxl：请先 pip install openpyxl，或改存 CSV")
    from openpyxl.styles import Alignment, Font  # noqa: PLC0415
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    title = (sheet_title or "Sheet1")[:31]
    # Excel 不允许这些字符出现在工作表名里
    for bad in "\\/*?:[]":
        title = title.replace(bad, "-")
    worksheet.title = title or "Sheet1"

    for row in rows:
        worksheet.append(list(row))

    if rows:
        for cell in worksheet[1]:
            cell.font = Font(bold=True)
            cell.alignment = Alignment(vertical="center")
        worksheet.freeze_panes = "A2"
    widths: dict[int, int] = {}
    for row in rows:
        for column, value in enumerate(row, start=1):
            # 中文按两个字符宽估算，够用就行
            width = sum(2 if ord(char) > 127 else 1 for char in str(value))
            widths[column] = max(widths.get(column, 8), min(width + 2, 60))
    for column, width in widths.items():
        worksheet.column_dimensions[get_column_letter(column)].width = width
    try:
        workbook.save(path)
    except OSError as exc:
        raise SheetError(f"写不了 {path}：{exc}") from exc
    return path


# --------------------------------------------------------------- 对话框


def read_filter() -> str:
    """导入对话框的过滤器（第一项是"全部支持的格式"）。"""
    return (
        f"表格文件 (*.csv *.txt *.xlsx *.xls);;"
        f"CSV / 文本 (*.csv *.txt);;"
        f"Excel (*.xlsx *.xls);;"
        f"所有文件 (*.*)"
    )


def write_filter(default: str = "csv") -> str:
    """导出对话框的过滤器。"""
    if default == "xlsx":
        return "Excel 工作簿 (*.xlsx);;CSV 表格 (*.csv);;所有文件 (*.*)"
    return "CSV 表格 (*.csv);;Excel 工作簿 (*.xlsx);;所有文件 (*.*)"


def ensure_suffix(path: str, chosen_filter: str = "") -> str:
    """用户在对话框里只填空文件名时，按选中的过滤器补扩展名。"""
    suffix = os.path.splitext(path)[1].lower()
    if suffix in READ_EXTS:
        return path
    if "xlsx" in chosen_filter.lower() or "excel" in chosen_filter.lower():
        return path + ".xlsx"
    return path + ".csv"
