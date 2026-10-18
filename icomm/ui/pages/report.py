"""报告生成页（骨架）。

位置在左侧导航「设备与点位」**下面**：那里是"这一台电脑上有什么"的终点，
报告就是把它整理成一份能交出去的东西。

这一版刻意只做骨架 —— **内容还没定**，所以：

* 小节（勾选框）全部来自 :data:`icomm.core.report.SECTIONS`，加一节只改 core；
* 页面只负责"凑选项 → 取数 → 预览 → 存盘"，不参与任何拼表格的活；
* 预览用 QTextBrowser（样式是简化的），存下来的 HTML 用浏览器打开是完整排版；
* 报表目录走工程的 ``报表/reports``，跟截图/录波那些产物放在一起。

以后要加"某台设备一段时间的运行报表""试验前后对比"这类专门报告，
在这里加一个页签或换一套小节即可，不用动主窗口。
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...core import report as report_core
from ...core.models import Project
from .. import theme
from ..spin import no_arrows

#: 时间范围下拉：显示名 -> 往前多少秒（None = 不限）
TIME_RANGES: tuple[tuple[str, float | None], ...] = (
    ("不限时间", None),
    ("最近 1 小时", 3600.0),
    ("最近 24 小时", 86400.0),
    ("最近 7 天", 7 * 86400.0),
)


class ReportPage(QWidget):
    """报告生成 / 预览 / 存盘。"""

    #: 存盘成功（路径），主窗口据此弹通知、写状态栏
    saved = Signal(str)
    #: 生成或存盘失败（人话说明）
    failed = Signal(str)

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self.storage: Any = None
        #: 由主窗口注入：返回 (实时值 {点号: TagValue}, 设备状态列表)
        self.live_provider: Callable[[], tuple[dict[str, Any], list[Any]]] | None = None
        #: 由主窗口注入：报表目录（不存在会按需创建）
        self.output_dir_provider: Callable[[], str] | None = None
        self._report: report_core.Report | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        title = QLabel("报告生成")
        title.setStyleSheet("font-size: 15px; font-weight: 600;")
        root.addWidget(title)
        hint = QLabel(
            "把工程、设备、点位、实时值与报警整理成一份文档（HTML 可直接打印 / 另存 PDF）。"
            "这一版是骨架：小节可勾选，内容以后会逐步补。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(hint)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)
        # 左栏塞进滚动区：勾选框 + 备注加起来很高，直接摊开会把窗口最小高度顶得很大
        # （有回归测试盯着"窗口最小高度 ≤700"，小屏笔记本要放得下）
        options_scroll = QScrollArea()
        options_scroll.setWidgetResizable(True)
        options_scroll.setFrameShape(QScrollArea.NoFrame)
        options_scroll.setWidget(self._build_options())
        splitter.addWidget(options_scroll)
        splitter.addWidget(self._build_preview())
        splitter.setSizes([360, 700])

        self.status = QLabel("就绪：点「生成预览」看效果，或直接「保存报告…」")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.status)

    # -------------------------------------------------- 界面

    def _build_options(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(8)

        form = QGroupBox("报告")
        form_layout = QVBoxLayout(form)
        form_layout.setSpacing(6)

        self.title_edit = QLineEdit("设备与点位报告")
        self.title_edit.setToolTip("报告标题（存盘文件名也用它）")
        form_layout.addWidget(QLabel("标题"))
        form_layout.addWidget(self.title_edit)

        form_layout.addWidget(QLabel("报警等带时间的内容看哪一段"))
        self.range_combo = QComboBox()
        for label, _seconds in TIME_RANGES:
            self.range_combo.addItem(label)
        form_layout.addWidget(self.range_combo)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("表格最多"))
        self.rows_spin = QSpinBox()
        self.rows_spin.setRange(50, 50000)
        self.rows_spin.setSingleStep(500)
        self.rows_spin.setValue(report_core.DEFAULT_MAX_ROWS)
        self.rows_spin.setMinimumWidth(96)
        self.rows_spin.setToolTip("每张表最多列多少行；超了会截断并在表下注明，避免几十万行把浏览器卡死")
        no_arrows(self.rows_spin)
        row.addWidget(self.rows_spin)
        row.addWidget(QLabel("行"))
        row.addStretch(1)
        form_layout.addLayout(row)
        layout.addWidget(form)

        sections_box = QGroupBox("包含哪些内容")
        sections_layout = QVBoxLayout(sections_box)
        sections_layout.setSpacing(4)
        defaults = set(report_core.default_sections())
        self.section_boxes: dict[str, QCheckBox] = {}
        for key, section_title, doc in report_core.section_choices():
            box = QCheckBox(section_title)
            box.setToolTip(doc)
            box.setChecked(key in defaults)
            self.section_boxes[key] = box
            sections_layout.addWidget(box)
        sections_layout.addStretch(1)
        layout.addWidget(sections_box, 1)

        notes_box = QGroupBox("备注（会写进报告）")
        notes_layout = QVBoxLayout(notes_box)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText(
            "写点现场记录：这次做了什么试验、换了什么板子、下一步要注意什么…"
        )
        self.notes_edit.setFixedHeight(90)
        notes_layout.addWidget(self.notes_edit)
        layout.addWidget(notes_box)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.preview_button = QPushButton("生成预览")
        self.preview_button.setObjectName("primary")
        self.preview_button.clicked.connect(lambda _checked=False: self.generate())
        buttons.addWidget(self.preview_button)
        self.save_button = QPushButton("保存报告…")
        self.save_button.clicked.connect(lambda _checked=False: self.save())
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        open_row = QHBoxLayout()
        open_button = QPushButton("打开报表文件夹")
        open_button.setToolTip("报告默认存在工程的「报表/reports」里")
        open_button.clicked.connect(lambda _checked=False: self.open_folder())
        open_row.addWidget(open_button)
        open_row.addStretch(1)
        layout.addLayout(open_row)
        return host

    def _build_preview(self) -> QWidget:
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.preview = QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        self.preview.setPlaceholderText("还没有预览：先在左边勾好内容，再点「生成预览」。")
        layout.addWidget(self.preview, 1)
        note = QLabel("预览是简化排版；保存后的 HTML 用浏览器打开才是完整样式（可直接打印）")
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(note)
        return host

    # -------------------------------------------------- 外部注入

    def set_project(self, project: Project) -> None:
        self.project = project
        self._report = None

    def set_storage(self, storage: Any) -> None:
        self.storage = storage

    def set_live_provider(self, provider: Callable[[], tuple[dict[str, Any], list[Any]]] | None) -> None:
        self.live_provider = provider

    def set_output_dir_provider(self, provider: Callable[[], str] | None) -> None:
        self.output_dir_provider = provider

    # -------------------------------------------------- 生成

    def selected_sections(self) -> tuple[str, ...]:
        return tuple(key for key, box in self.section_boxes.items() if box.isChecked())

    def time_range(self) -> tuple[float | None, float | None]:
        index = max(0, self.range_combo.currentIndex())
        seconds = TIME_RANGES[index][1]
        if seconds is None:
            return (None, None)
        now = time.time()
        return (now - seconds, now)

    def make_options(self) -> report_core.ReportOptions:
        start, end = self.time_range()
        return report_core.ReportOptions(
            title=self.title_edit.text().strip() or "设备与点位报告",
            sections=self.selected_sections(),
            max_rows=int(self.rows_spin.value()),
            time_from=start,
            time_to=end,
            notes=self.notes_edit.toPlainText().strip(),
        )

    def build(self) -> report_core.Report:
        """按当前选项生成报告（不存盘）。出错时抛异常，由调用方（或本页）提示。"""
        options = self.make_options()
        if not options.sections:
            raise ValueError("一个内容都没勾：至少勾一项再生成。")
        values: dict[str, Any] = {}
        statuses: list[Any] = []
        if self.live_provider is not None:
            try:
                values, statuses = self.live_provider()
            except Exception:  # noqa: BLE001 - 取实时值失败不该挡住整份报告
                values, statuses = {}, []
        context = report_core.ReportContext(
            project=self.project,
            options=options,
            storage=self.storage,
            values=values or {},
            statuses=statuses or [],
        )
        self._report = report_core.build_report(context)
        return self._report

    def generate(self) -> report_core.Report | None:
        """生成并显示预览。"""
        try:
            report = self.build()
        except Exception as error:  # noqa: BLE001 - 界面不该被一次生成拖崩
            self.fail(f"生成失败：{error}")
            return None
        self.preview.setHtml(report.to_html())
        self.status.setText(
            f"已生成：{len(report.sections)} 节 · {report.time_text()}（未保存）"
        )
        return report

    def save(self) -> str | None:
        """选路径存盘；没有生成过就先按当前选项生成一份。"""
        try:
            report = self._report or self.build()
        except Exception as error:  # noqa: BLE001
            self.fail(f"生成失败：{error}")
            return None
        directory = self.default_output_dir()
        suggested = os.path.join(directory, report.default_name("html"))
        path, selected = QFileDialog.getSaveFileName(
            self,
            "保存报告",
            suggested,
            "网页 (*.html);;Markdown (*.md);;所有文件 (*.*)",
        )
        if not path:
            return None
        if not os.path.splitext(path)[1]:
            path += ".md" if "Markdown" in (selected or "") else ".html"
        try:
            report.write(path)
        except OSError as error:
            self.fail(f"保存失败：{error}")
            return None
        self.preview.setHtml(report.to_html())
        self.status.setText(f"已保存：{path}")
        self.saved.emit(path)
        return path

    def open_folder(self) -> str:
        directory = self.default_output_dir()
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as error:
            self.fail(f"建不了报表目录：{error}")
            return directory
        QDesktopServices.openUrl(QUrl.fromLocalFile(directory))
        return directory

    def default_output_dir(self) -> str:
        if self.output_dir_provider is not None:
            try:
                directory = self.output_dir_provider()
                if directory:
                    return directory
            except Exception:  # noqa: BLE001 - 目录取不到就退回当前目录
                pass
        return os.getcwd()

    def fail(self, message: str) -> None:
        self.status.setText(message)
        self.failed.emit(message)


__all__ = ["TIME_RANGES", "ReportPage"]
