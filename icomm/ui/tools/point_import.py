"""「点表识别」工具：把别家的点表猜成本应用格式，猜不动就明确要求客户改格式。

界面只做三件事：选文件 → 让人能改"哪列是什么" → 导出成能直接导入的点表 CSV。
所有识别逻辑在 :mod:`icomm.core.point_import` 里（纯逻辑、可单测）。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import point_import as pi
from .. import theme
from ..spin import no_arrows
from .base import DialogTool, ToolContext, register_tool

#: 预览表里显示的列（值都已在 core 里归一）
PREVIEW_COLUMNS = ("点号", "显示名称", "起始地址", "类型", "寄存器区", "单位", "缩放", "可写")

#: 预览最多显示多少行（再多用户也看不过来，导出仍然全量）
PREVIEW_ROWS = 100


class PointImportDialog(QDialog):
    """点表识别 / 列映射 / 导出。"""

    def __init__(self, context: ToolContext | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent or (context.window if context is not None else None))
        self.context = context
        self.preview: pi.ImportPreview | None = None
        self.error_text = ""
        self.path = ""
        self._loading = False

        self.setWindowTitle("点表识别")
        self.setMinimumSize(0, 0)
        self.resize(1000, 820)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        content = QWidget()
        scroll.setWidget(content)
        root = QVBoxLayout(content)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)
        root.addWidget(self._build_header())
        root.addWidget(self._build_source_box())
        root.addWidget(self._build_mapping_box())
        root.addWidget(self._build_preview_box(), 1)
        root.addWidget(self._build_log_box(), 1)
        root.addWidget(self._build_footer())

    # -------------------------------------------------- 骨架

    def _build_header(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        title = QLabel("点表识别")
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 15px; font-weight: 600;")
        layout.addWidget(title)
        hint = QLabel(
            "拿别家的点表（列名/地址写法都不一样）也不用一行行抄：选文件 → 自动认列 → 核对预览 → "
            "导出成本应用的点表 CSV，再走「文件 → 导入参数表」。"
            "认不出来的表会直接报错并说明缺什么 —— 宁可让你重写一遍，也不把错位的点表导进工程。"
        )
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        return box

    def _build_source_box(self) -> QWidget:
        box = QGroupBox("1. 选文件")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.path_edit = QLineEdit()
        self.path_edit.setReadOnly(True)
        self.path_edit.setPlaceholderText("支持 CSV / TXT / TSV；.xlsx 需要装 openpyxl（或在 Excel 里另存为 CSV）")
        row.addWidget(self.path_edit, 1)
        choose = QPushButton("选择点表文件…")
        choose.setObjectName("primary")
        choose.clicked.connect(lambda _checked=False: self.choose_file())
        row.addWidget(choose)
        layout.addLayout(row)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        row2.addWidget(QLabel("地址写法"))
        self.style_combo = QComboBox()
        for key, label in pi.ADDRESS_STYLES:
            self.style_combo.addItem(label, key)
        self.style_combo.setToolTip(
            '0x0001 这种写法有歧义：前缀写法里是「第一个线圈」，十六进制里是「第 1 个寄存器」。\n'
            "自动判断时：16# 或带 A~F 按十六进制；1x/3x/4x 按前缀；光秃秃的 0x0001 按十六进制。"
        )
        self.style_combo.currentIndexChanged.connect(lambda _index: self.reconvert())
        row2.addWidget(self.style_combo)

        row2.addSpacing(12)
        row2.addWidget(QLabel("表头在第几行"))
        self.header_spin = QSpinBox()
        self.header_spin.setRange(1, 500)
        self.header_spin.setValue(1)
        self.header_spin.setToolTip("自动认出来的；不对就手动改，改完会按这一行当表头重新识别")
        no_arrows(self.header_spin)
        self.header_spin.valueChanged.connect(lambda _value: self._on_header_changed())
        row2.addWidget(self.header_spin)

        row2.addSpacing(12)
        row2.addWidget(QLabel("默认设备名"))
        self.device_edit = QLineEdit()
        self.device_edit.setPlaceholderText("表里没有「设备」列时用这个名字")
        self.device_edit.setMaximumWidth(200)
        self.device_edit.editingFinished.connect(lambda: self.reconvert())
        row2.addWidget(self.device_edit)
        row2.addStretch(1)
        layout.addLayout(row2)
        return box

    def _build_mapping_box(self) -> QWidget:
        box = QGroupBox("2. 每列是什么（自动认的，可以改）")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        self.map_table = QTableWidget(0, 4)
        self.map_table.setHorizontalHeaderLabels(["列", "源表头", "识别为", "判断依据"])
        self.map_table.verticalHeader().setVisible(False)
        self.map_table.setEditTriggers(QTableWidget.NoEditTriggers)
        header = self.map_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        self.map_table.setMaximumHeight(240)
        layout.addWidget(self.map_table)

        self.detect_label = QLabel("还没选文件")
        self.detect_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.detect_label.setWordWrap(True)
        layout.addWidget(self.detect_label)
        return box

    def _build_preview_box(self) -> QWidget:
        box = QGroupBox("3. 转出来的点表（预览）")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        self.preview_table = QTableWidget(0, len(PREVIEW_COLUMNS))
        self.preview_table.setHorizontalHeaderLabels(list(PREVIEW_COLUMNS))
        self.preview_table.verticalHeader().setVisible(False)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.preview_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.preview_table.setMinimumHeight(180)
        layout.addWidget(self.preview_table)
        self.preview_label = QLabel("")
        self.preview_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.preview_label.setWordWrap(True)
        layout.addWidget(self.preview_label)
        return box

    def _build_log_box(self) -> QWidget:
        box = QGroupBox("问题与换算说明")
        layout = QVBoxLayout(box)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(110)
        self.log_view.setPlaceholderText("识别过程中的说明会显示在这里（换算假设、被跳过的行、需要人工确认的地方）")
        layout.addWidget(self.log_view)
        return box

    def _build_footer(self) -> QWidget:
        box = QWidget()
        layout = QHBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.export_button = QPushButton("导出为本应用点表 CSV…")
        self.export_button.setObjectName("primary")
        self.export_button.setEnabled(False)
        self.export_button.clicked.connect(lambda _checked=False: self.export())
        layout.addWidget(self.export_button)

        self.copy_button = QPushButton("复制错误信息")
        self.copy_button.setEnabled(False)
        self.copy_button.setToolTip("发回给客户/厂家，让他们照着改表头")
        self.copy_button.clicked.connect(lambda _checked=False: self.copy_error())
        layout.addWidget(self.copy_button)
        layout.addStretch(1)

        self.status = QLabel("还没选文件")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(self.status)
        return box

    # -------------------------------------------------- 读文件

    def choose_file(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择点表文件",
            "",
            "点表文件 (*.csv *.txt *.tsv *.xlsx *.xlsm);;所有文件 (*.*)",
        )
        if path:
            self.load_path(path)

    def load_path(self, path: str) -> None:
        """读一个点表文件并识别；认不出来就把错误显示出来（不抛给界面）。"""
        self.path = path
        self.path_edit.setText(path)
        self.error_text = ""
        self.preview = None
        self._clear()
        try:
            preview = pi.load_preview(
                path,
                default_device=self.device_edit.text().strip(),
                address_style=str(self.style_combo.currentData() or "auto"),
            )
        except pi.PointTableError as error:
            self._show_error(error)
            return
        except Exception as error:  # noqa: BLE001 - 界面不该被一个坏文件拖崩
            self._show_error(pi.PointTableError(f"读这个文件时出错：{error}"))
            return
        self.preview = preview
        self._loading = True
        try:
            self.header_spin.setValue(preview.table.header_line + 1)
            if not self.device_edit.text().strip():
                self.device_edit.setText(preview.table.name)
        finally:
            self._loading = False
        self._fill_mapping()
        self._refresh_preview()
        self.status.setText(preview.summary())
        self.status.setStyleSheet(f"color: {theme.QUALITY_GOOD}; font-size: 11px;")

    def _on_header_changed(self) -> None:
        if self._loading or not self.path:
            return
        if self.preview is not None:
            self.preview.table.header_line = max(0, int(self.header_spin.value()) - 1)
            self.preview.guesses = pi.guess_columns(self.preview.table)
            self.preview.rebuild_rows()
            self._fill_mapping()
            self._refresh_preview()
            self.status.setText(self.preview.summary())

    def reconvert(self) -> None:
        """改了地址写法/默认设备名：按当前映射重转一遍。"""
        if self.preview is None:
            return
        self.preview.default_device = self.device_edit.text().strip()
        self.preview.address_style = str(self.style_combo.currentData() or "auto")
        self.preview.rebuild_rows()
        self._refresh_preview()
        self.status.setText(self.preview.summary())

    def _clear(self) -> None:
        self.map_table.setRowCount(0)
        self.preview_table.setRowCount(0)
        self.log_view.clear()
        self.detect_label.setText("还没选文件")
        self.preview_label.setText("")
        self.export_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.status.setText("还没选文件")

    # -------------------------------------------------- 映射表

    def _fill_mapping(self) -> None:
        preview = self.preview
        if preview is None:
            return
        self._loading = True
        try:
            guesses = preview.guesses
            self.map_table.setRowCount(len(guesses))
            for row, guess in enumerate(guesses):
                self.map_table.setItem(row, 0, QTableWidgetItem(str(guess.index + 1)))
                self.map_table.setItem(row, 1, QTableWidgetItem(guess.header or "（空表头）"))
                combo = QComboBox()
                combo.addItem("（不导入）", "")
                for key, label in pi.FIELDS:
                    combo.addItem(label, key)
                index = combo.findData(guess.field_key)
                combo.setCurrentIndex(max(0, index))
                combo.currentIndexChanged.connect(
                    lambda _index, column=guess.index, box=combo: self._on_field_changed(column, box)
                )
                self.map_table.setCellWidget(row, 2, combo)
                reason = guess.reason
                if guess.field_key:
                    reason = f"{reason}（置信度 {guess.confidence:.0%}）" if reason else f"置信度 {guess.confidence:.0%}"
                self.map_table.setItem(row, 3, QTableWidgetItem(reason))
        finally:
            self._loading = False
        table = preview.table
        self.detect_label.setText(
            f"编码 {table.encoding}　分隔符 {_delimiter_label(table.delimiter)}　"
            f"表头第 {table.header_line + 1} 行　数据 {len(table.data_rows)} 行　"
            f"识别出 {len(preview.mapping())} 列"
        )

    def _on_field_changed(self, column: int, combo: QComboBox) -> None:
        if self._loading or self.preview is None:
            return
        self.preview.set_mapping(column, str(combo.currentData() or ""))
        self._fill_mapping()  # 重新排一遍（同一字段被挤掉的那列要显示出来）
        self._refresh_preview()
        self.status.setText(self.preview.summary())

    def set_mapping(self, column: int, field_key: str) -> None:
        """给测试/脚本用：直接改某列的映射。"""
        if self.preview is None:
            return
        self.preview.set_mapping(column, field_key)
        self._fill_mapping()
        self._refresh_preview()
        self.status.setText(self.preview.summary())

    # -------------------------------------------------- 预览

    def _refresh_preview(self) -> None:
        preview = self.preview
        if preview is None:
            self.export_button.setEnabled(False)
            return
        rows = preview.ok_rows[:PREVIEW_ROWS]
        self.preview_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            for column, name in enumerate(PREVIEW_COLUMNS):
                item = QTableWidgetItem(str(row.values.get(name, "")))
                self.preview_table.setItem(row_index, column, item)

        lines: list[str] = []
        if preview.issues:
            lines.append("⚠ 这些行没有被导入：")
            lines.extend(f"  · {item}" for item in preview.issues)
        notes = [note for row in preview.rows for note in row.notes]
        if notes:
            lines.append("")
            lines.append("ℹ 换算说明（确认一下对不对）：")
            seen: set[str] = set()
            for note in notes:
                key = note.split("（")[0]
                if key in seen:
                    continue
                seen.add(key)
                lines.append(f"  · {note}")
        if not lines:
            lines.append("没有发现问题。导出的 CSV 已经按本应用的点表列排好，可直接「文件 → 导入参数表」。")
        self.log_view.setPlainText("\n".join(lines))

        self.preview_label.setText(
            f"共转换 {len(preview.ok_rows)} 行"
            + (f"，跳过 {preview.skipped} 行" if preview.skipped else "")
            + (f"；预览只显示前 {PREVIEW_ROWS} 行，导出是全部" if len(preview.ok_rows) > PREVIEW_ROWS else "")
        )
        self.export_button.setEnabled(bool(preview.ok_rows))
        self.copy_button.setEnabled(False)

    # -------------------------------------------------- 出错

    def _show_error(self, error: pi.PointTableError) -> None:
        self.error_text = error.as_text()
        self.log_view.setPlainText(self.error_text)
        self.detect_label.setText("识别失败 —— 请按下面说的改名或改用模板")
        self.preview_label.setText("")
        self.export_button.setEnabled(False)
        self.copy_button.setEnabled(True)
        self.status.setText("❌ 认不出来，需要客户按模板改格式")
        self.status.setStyleSheet(f"color: {theme.QUALITY_BAD}; font-size: 11px;")

    def copy_error(self) -> None:
        if self.error_text:
            QApplication.clipboard().setText(self.error_text)

    # -------------------------------------------------- 导出

    def default_export_path(self) -> str:
        base = self.preview.table.name if self.preview is not None else "点表"
        folder = ""
        if self.context is not None:
            folder = self.context.report_subdir("datalog") if self.context.report_dir else ""
        if not folder:
            folder = os.path.dirname(os.path.abspath(self.path)) if self.path else os.getcwd()
        return os.path.join(folder, f"{base}_本应用点表.csv")

    def export(self, path: str = "") -> str | None:
        """导出成 CSV；``path`` 为空时弹保存框。返回写出的路径。"""
        if self.preview is None or not self.preview.ok_rows:
            return None
        if not path:
            path, _filter = QFileDialog.getSaveFileName(
                self, "导出为本应用点表", self.default_export_path(),
                "CSV 点表 (*.csv);;所有文件 (*.*)",
            )
            if not path:
                return None
            if not os.path.splitext(path)[1]:
                path += ".csv"
        try:
            self.preview.write_csv(path)
        except OSError as error:
            self.status.setText(f"❌ 写文件失败：{error}")
            self.status.setStyleSheet(f"color: {theme.QUALITY_BAD}; font-size: 11px;")
            return None
        self.status.setText(f"✅ 已导出：{path}（接着用「文件 → 导入参数表」导进工程）")
        self.status.setStyleSheet(f"color: {theme.QUALITY_GOOD}; font-size: 11px;")
        return path


def _delimiter_label(delimiter: str) -> str:
    return {",": "逗号", "\t": "制表符", ";": "分号", "|": "竖线"}.get(delimiter, repr(delimiter))


@register_tool
class PointImportTool(DialogTool):
    """菜单里的「点表识别」。"""

    name = "点表识别"
    group = "工程"
    order = 15
    description = "把别家的点表（CSV/TXT/xlsx）认出列名与地址，转成本应用的点表；认不出来会明确要求按模板改"

    def build(self, context: ToolContext | None = None) -> QDialog:
        return PointImportDialog(context)


__all__ = ["PREVIEW_COLUMNS", "PointImportDialog", "PointImportTool"]
