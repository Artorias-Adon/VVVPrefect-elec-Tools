"""可扩展的保存格式。

"保存"被拆成两条独立的链路，各自都能选格式：

* **参数表**（点位定义）—— 默认 CSV，另有 TXT / JSON；
* **整个工程** —— 默认工程目录（``project.json`` + 参数表 + 库），或打包成 ``.iprj``。

**扩展方式**：新增一种格式 = 继承 :class:`ParameterWriter` 或
:class:`ProjectWriter`，用 ``@register_parameter_writer`` /
``@register_project_writer`` 注册。界面（保存对话框的格式下拉框、
文件过滤器、扩展名）全部由注册表生成，不用改 UI 代码。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from typing import Any, Iterable

from .. import APP_SHORT
from . import csv_table, project_store
from .models import Project

# --------------------------------------------------------------- 参数表


class ParameterWriter(ABC):
    """把"点位定义"写成某种格式的文件。"""

    #: 显示在下拉框里的名字
    label: str = ""
    #: 主扩展名（含点），如 ``.csv``
    suffix: str = ""
    #: 也认这些扩展名（读取时用；写的时候仍用主扩展名）
    extra_suffixes: tuple[str, ...] = ()
    #: 下拉框里的补充说明
    description: str = ""
    #: 是否需要工程目录之外的信息（目前都没用到，留给未来格式）
    needs_project_dir: bool = False

    @abstractmethod
    def write(self, project: Project, path: str) -> int:
        """写入并返回写出的行数（点数）。"""

    #: 是否支持导入（False 时 read 返回 None）
    can_read: bool = False

    def read(self, project: Project, path: str, mode: str = "merge"):  # noqa: ANN201
        """读回参数表。

        ``mode`` 与 CSV 导入一致：``merge`` 按 (设备, 点号) 更新/新增，
        ``replace`` 先清空再重建。

        默认返回 ``None`` 表示该格式不支持导入；支持导入的格式返回
        :class:`icomm.core.csv_table.ImportReport`。
        """
        return None

    def supports_read(self) -> bool:
        return type(self).read is not ParameterWriter.read

    #: 保存对话框用的过滤器片段，如 ``参数表 CSV (*.csv)``
    @property
    def file_filter(self) -> str:
        patterns = " ".join(f"*{s}" for s in (self.suffix,) + self.extra_suffixes)
        return f"{self.label} ({patterns})"

    def __str__(self) -> str:  # 下拉框直接显示
        return f"{self.label}　{self.description}" if self.description else self.label


class CsvParameterWriter(ParameterWriter):
    """带 BOM 的 UTF-8 CSV，Excel 双击可直接打开不乱码。"""

    label = "CSV 参数表"
    suffix = ".csv"
    description = "Excel 可直接编辑"

    def write(self, project: Project, path: str) -> int:
        return csv_table.export_csv(project, path)

    def read(self, project: Project, path: str, mode: str = "merge"):
        return csv_table.import_csv(project, path, mode=mode)


class XlsxParameterWriter(ParameterWriter):
    """Excel 工作簿(.xlsx)：现场同事最常用的表格格式，可直接用 Excel 改完导回。

    读还兼容 ``.xlsm`` 与老的 ``.xls``（写 .xls 不支持，会提示另存为 .xlsx）。
    """

    label = "Excel 工作簿"
    suffix = ".xlsx"
    extra_suffixes = (".xlsm", ".xls")
    description = "Excel 直接编辑（.xls 也能读，导出请用 .xlsx）"

    def write(self, project: Project, path: str) -> int:
        return csv_table.export_table(project, path)

    def read(self, project: Project, path: str, mode: str = "merge"):
        return csv_table.import_csv(project, path, mode=mode)


class TxtParameterWriter(ParameterWriter):
    """制表符对齐的纯文本表，方便贴进工单、邮件或打印。"""

    label = "TXT 对齐文本"
    suffix = ".txt"
    description = "制表符分隔，便于打印或贴进文档"

    def write(self, project: Project, path: str) -> int:
        rows = csv_table.export_rows(project)
        columns = csv_table.CSV_COLUMNS
        widths = []
        for column in columns:
            widest = _display_width(column)
            for row in rows:
                widest = max(widest, _display_width(str(row.get(column, ""))))
            widths.append(min(widest, 28))

        lines = [f"# {APP_SHORT} 参数表　工程：{project.name}　共 {len(rows)} 个点位"]
        lines.append(_format_row(columns, widths))
        lines.append("-" * (sum(widths) + 3 * len(widths)))
        for row in rows:
            lines.append(
                _format_row([str(row.get(c, "")) for c in columns], widths)
            )
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write("\n".join(lines) + "\n")
        return len(rows)


class JsonParameterWriter(ParameterWriter):
    """结构化 JSON：点位定义 + 报警阈值都在，适合做版本管理或给别的程序读。"""

    label = "JSON 结构化"
    suffix = ".json"
    description = "含完整点位定义，便于程序读取与 diff"

    def write(self, project: Project, path: str) -> int:
        payload = {
            "format": "icomm-parameters",
            "version": 1,
            "project": project.name,
            "columns": list(csv_table.CSV_COLUMNS),
            "points": csv_table.export_rows(project),
        }
        with open(path, "w", encoding="utf-8", newline="") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return len(payload["points"])

    def read(self, project: Project, path: str, mode: str = "merge"):
        """读回 JSON 参数表：复用 CSV 的校验逻辑，保证两种格式行为一致。

        做法是把 JSON 里的行重新拼成 CSV 文本再交给 csv_table，
        这样逐行校验、合并/重建模式、问题报告全都共用一套实现。
        """
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        if not isinstance(payload, dict):
            raise ValueError("JSON 参数表顶层必须是对象")
        rows = payload.get("points")
        if not isinstance(rows, list):
            raise ValueError("JSON 参数表里缺少 points 数组")

        columns = payload.get("columns") or list(csv_table.CSV_COLUMNS)
        import csv as _csv
        import io

        buffer = io.StringIO()
        writer = _csv.writer(buffer)
        writer.writerow(columns)
        for row in rows:
            if isinstance(row, dict):
                writer.writerow([row.get(column, "") for column in columns])
        buffer.seek(0)
        return csv_table.import_rows(
            project,
            list(_csv.DictReader(buffer)),
            mode=mode,
            source=path,
        )


def _display_width(text: str) -> int:
    """中文按两个字宽算，这样 TXT 里列才对得齐。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)


def _format_row(cells: Iterable[str], widths: list[int]) -> str:
    parts = []
    for cell, width in zip(cells, widths):
        text = str(cell)
        pad = max(0, width - _display_width(text))
        parts.append(text + " " * pad)
    return "  ".join(parts)


# --------------------------------------------------------------- 工程


class ProjectWriter(ABC):
    """把整个工程写出去。"""

    label: str = ""
    suffix: str = ""
    description: str = ""
    #: 是否产出单独的单个文件（决定保存对话框问"文件"还是"目录"）
    single_file: bool = True

    @abstractmethod
    def write(self, project: Project, paths: project_store.ProjectPaths, target: str):
        """写入，返回 :class:`project_store.SaveResult`。"""

    @property
    def file_filter(self) -> str:
        return f"{self.label} (*{self.suffix})"

    def __str__(self) -> str:
        return f"{self.label}　{self.description}" if self.description else self.label


class ProjectDirWriter(ProjectWriter):
    """工程目录：``project.json`` + 参数表 + 历史库，可整体拷贝。"""

    label = "工程目录"
    suffix = ""
    description = "文件夹形式，可整体拷走"
    single_file = False

    def write(self, project: Project, paths, target: str):
        return project_store.save_project_to_dir(project, target)


class IprjWriter(ProjectWriter):
    """``.iprj`` 单文件工程包：方便发邮件/发给同事。"""

    label = ".iprj 工程包"
    suffix = project_store.IPRJ_SUFFIX
    description = "单文件，便于分发"
    single_file = True

    def write(self, project: Project, paths, target: str):
        return project_store.save_project_as_iprj(project, paths, target)


# --------------------------------------------------------------- 注册表

_PARAMETER_WRITERS: list[ParameterWriter] = []
_PROJECT_WRITERS: list[ProjectWriter] = []


def register_parameter_writer(writer: ParameterWriter) -> ParameterWriter:
    """注册一种参数表格式（重复注册同一个类会被忽略）。"""
    if any(type(item) is type(writer) for item in _PARAMETER_WRITERS):
        return writer
    _PARAMETER_WRITERS.append(writer)
    return writer


def register_project_writer(writer: ProjectWriter) -> ProjectWriter:
    if any(type(item) is type(writer) for item in _PROJECT_WRITERS):
        return writer
    _PROJECT_WRITERS.append(writer)
    return writer


def parameter_writers() -> list[ParameterWriter]:
    return list(_PARAMETER_WRITERS)


def project_writers() -> list[ProjectWriter]:
    return list(_PROJECT_WRITERS)


def parameter_writer_for(path: str) -> ParameterWriter | None:
    """按扩展名猜该用哪个参数表写入器（打开别人的文件时用）。"""
    suffix = os.path.splitext(path)[1].lower()
    for writer in _PARAMETER_WRITERS:
        if suffix == writer.suffix.lower() or suffix in (
            s.lower() for s in writer.extra_suffixes
        ):
            return writer
    return None


def parameter_file_filter() -> str:
    parts = [writer.file_filter for writer in _PARAMETER_WRITERS]
    parts.append("所有文件 (*)")
    return ";;".join(parts)


def project_file_filter() -> str:
    parts = [writer.file_filter for writer in _PROJECT_WRITERS if writer.single_file]
    parts.append("所有文件 (*)")
    return ";;".join(parts)


def default_parameter_writer() -> ParameterWriter:
    return _PARAMETER_WRITERS[0]


def default_project_writer() -> ProjectWriter:
    return _PROJECT_WRITERS[0]


def ensure_writer(project: Project, path: str) -> ParameterWriter:
    """给"保存参数表到某路径"选写入器：认不出扩展名就按默认格式写。"""
    return parameter_writer_for(path) or default_parameter_writer()


# 内置格式（顺序 = 下拉框顺序，第一个是默认）
register_parameter_writer(CsvParameterWriter())
register_parameter_writer(XlsxParameterWriter())
register_parameter_writer(TxtParameterWriter())
register_parameter_writer(JsonParameterWriter())
register_project_writer(ProjectDirWriter())
register_project_writer(IprjWriter())


def writer_summary() -> list[dict[str, Any]]:
    """给界面/文档用的一份清单。"""
    return [
        {
            "kind": "parameter",
            "label": writer.label,
            "suffix": writer.suffix,
            "description": writer.description,
        }
        for writer in _PARAMETER_WRITERS
    ] + [
        {
            "kind": "project",
            "label": writer.label,
            "suffix": writer.suffix,
            "description": writer.description,
        }
        for writer in _PROJECT_WRITERS
    ]
