"""「关于」对话框：软件说明 + 标准与规范（条目索引 + 本地标准库）+ 操作手册。

标准页分两半：

* 左边是**条目索引**（编号 / 名称 / 跟软件哪块对得上），双击去官方系统查；
* 右边是**本地标准库** —— 用户自己从正规渠道拿到的正文，导入到工程的
  ``standards/`` 目录，跟着工程走；软件**不附带任何正文**（版权在发布机构）。

手册页的内容全在 :mod:`icomm.core.manual`（纯数据、Qt 无关），这里只负责摆：
左边目录、下面搜索框、右边正文，还能整本另存成 HTML / Markdown。
"""

from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...core import manual, standards
from .. import theme
from ..dialogs.sizing import scrollable_page

#: 标准清单的列（说明放在表格下面的详情行里，省得列被挤成一串省略号）
STANDARD_COLUMNS = ("分组", "编号", "名称")


def about_html() -> str:
    """「关于本软件」那一页的文字（主窗口不再自己拼这段）。"""
    from ... import (
        APP_DESCRIPTION,
        APP_NAME,
        APP_VERSION,
        __copyright__,
        __license__,
    )

    return (
        "<b>" + APP_NAME + "</b><br>"
        f"版本 {APP_VERSION}　·　Python + PySide6<br><br>"
        "面向电气行业的开源调试与监视软件。<br>"
        f"{APP_DESCRIPTION}<br>"
        "协议层插件化，当前内置 Modbus TCP / RTU / RTU over TCP、CAN（含 CANopen）、"
        "IEC 61850、西门子 S7 驱动。<br>"
        "工程文件集中放在工程仓库 <b>VVPproject</b> 下（一个工程一个文件夹，"
        "配置 / 数据 / 报表分开），软件目录本身不存工程。<br><br>"
        "<b>数据与画面</b><br>"
        "· 点位于参数表中定义（地址/类型/字序/缩放/报警/录波），可导入导出为 CSV / TXT / JSON<br>"
        "· 数组/块点位用于录波，示波器支持触发、时基、双光标测量<br>"
        "· 主页示意图由工程文件数据驱动，节点可右键挂电压电流、合闸/分闸、看故障<br>"
        "· 点位可双击改参数、右键单发 / 持续发送 / 加到示波器<br>"
        "· 通信工具里有按协议的连通性测试，计算工具里有电能与电费（单一/分时电价）<br>"
        "· 工程可保存为工程目录或 .iprj 单文件包，库与参数表路径可自定义<br>"
        "· 界面配色 / 字体 / 字号 / 密度可自定义（默认天蓝主题，含两套深浅蓝调预设），"
        "含逐项颜色的高级设置<br><br>"
        "<b>扩展方式</b>：驱动放 icomm/core/drivers/，保存格式放 icomm/core/writers.py，"
        "主题预设放 icomm/ui/theme.py，都会自动出现在界面上。<br><br>"
        f"<b>开源协议</b>：{__license__}（全文见软件目录下的 LICENSE）<br>"
        f"{__copyright__}<br>"
        "这是<b>个人开源项目</b>，不是任何公司/客户的产品，示例数据均为自造。<br>"
        "第三方组件（PySide6 / python-can / pymodbus / pyserial / python-snap7 / iec61850）"
        "的协议见 THIRD-PARTY-LICENSES.md —— 其中 PySide6 与 python-can 为 LGPL-3.0，"
        "以独立文件形式随程序分发，未做修改，可自行替换。"
    )


class AboutDialog(QDialog):
    """关于对话框：软件说明 + 标准与规范。"""

    def __init__(
        self,
        project_name: str = "",
        library_root: str = "",
        base_dir: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.project_name = project_name or ""
        self.library_root = library_root or ""
        self.base_dir = base_dir or ""
        self.setWindowTitle("关于")
        self.resize(1180, 780)

        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_about(), "关于本软件")
        self.tabs.addTab(self._build_standards(), "标准与规范")
        self.tabs.addTab(self._build_manual(), manual.MANUAL_TITLE)
        root.addWidget(self.tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        buttons.clicked.connect(lambda _button: self.accept())
        root.addWidget(buttons)

        self.fill_standards()
        self.refresh_library()
        self.fill_manual()

    # -------------------------------------------------- 关于本软件

    def _build_about(self) -> QWidget:
        from ..resources import app_pixmap

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)

        head = QHBoxLayout()
        pixmap = app_pixmap(72)
        if pixmap is not None:
            icon = QLabel()
            icon.setPixmap(pixmap)
            icon.setFixedSize(pixmap.size())
            head.addWidget(icon, 0, Qt.AlignTop)
        self.about_label = QLabel(about_html())
        self.about_label.setTextFormat(Qt.RichText)
        self.about_label.setWordWrap(True)
        self.about_label.setOpenExternalLinks(False)
        head.addWidget(self.about_label, 1)
        layout.addWidget(scrollable_page(self._wrap(head)), 1)
        return page

    @staticmethod
    def _wrap(layout: Any) -> QWidget:
        holder = QWidget()
        holder.setLayout(layout)
        return holder

    # -------------------------------------------------- 操作手册

    def _build_manual(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(6)
        self.manual_search = QLineEdit()
        self.manual_search.setPlaceholderText(
            "搜一句试试：丢包率 / 写值限幅 / 分时电价 / 只读 / 从机"
        )
        self.manual_search.setClearButtonEnabled(True)
        self.manual_search.textChanged.connect(self.filter_manual)
        top.addWidget(self.manual_search, 1)
        self.manual_count = QLabel("")
        self.manual_count.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        top.addWidget(self.manual_count)
        save_button = QPushButton("另存为…")
        save_button.setToolTip("整本存成 HTML（浏览器打开可打印）或 Markdown")
        save_button.clicked.connect(lambda: self.save_manual())
        top.addWidget(save_button)
        layout.addLayout(top)

        splitter = QSplitter(Qt.Horizontal)
        self.manual_list = QListWidget()
        self.manual_list.setAlternatingRowColors(True)
        self.manual_list.currentItemChanged.connect(self._on_manual_item)
        splitter.addWidget(self.manual_list)

        self.manual_view = QTextBrowser()
        self.manual_view.setOpenExternalLinks(False)
        splitter.addWidget(self.manual_view)
        splitter.setSizes([280, 700])
        layout.addWidget(splitter, 1)

        self.manual_status = QLabel("内容随软件版本更新；另存出来的 HTML 可以直接打印。")
        self.manual_status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.manual_status.setWordWrap(True)
        layout.addWidget(self.manual_status)
        return page

    def fill_manual(self) -> None:
        """把章节填进左边的目录，并显示第一章。"""
        self.manual_list.clear()
        for entry in manual.sections():
            item = QListWidgetItem(entry.title)
            item.setData(Qt.UserRole, entry.key)
            item.setToolTip(entry.summary)
            self.manual_list.addItem(item)
        self._sync_manual_count(len(manual.sections()))
        if self.manual_list.count():
            self.manual_list.setCurrentRow(0)

    def filter_manual(self, text: str = "") -> None:
        """按关键词过滤目录；命中的第一章自动选中。"""
        hits = [entry.key for entry in manual.search(text)]
        wanted = set(hits)
        first_visible = -1
        for row in range(self.manual_list.count()):
            item = self.manual_list.item(row)
            visible = str(item.data(Qt.UserRole)) in wanted
            item.setHidden(not visible)
            if visible and first_visible < 0:
                first_visible = row
        self._sync_manual_count(len(hits))
        current = self.manual_list.currentItem()
        if current is not None and not current.isHidden():
            return
        if first_visible >= 0:
            self.manual_list.setCurrentRow(first_visible)
        else:
            self.manual_view.setHtml(
                "<p>没有章节匹配这个关键词。</p><p>换个词试试，"
                "或者把搜索框清空看全部目录。</p>"
            )

    def _sync_manual_count(self, shown: int) -> None:
        total, _blocks = manual.stats()
        if shown >= total:
            self.manual_count.setText(f"共 {total} 章")
        else:
            self.manual_count.setText(f"筛出 {shown} / {total} 章")

    def _on_manual_item(self, current: QListWidgetItem | None, _previous: Any = None) -> None:
        if current is not None:
            self.show_manual_section(str(current.data(Qt.UserRole)))

    def current_manual_key(self) -> str:
        item = self.manual_list.currentItem()
        return str(item.data(Qt.UserRole)) if item is not None else ""

    def show_manual_section(self, key: str = "") -> None:
        target = key or self.current_manual_key()
        entry = manual.section(target) if target else None
        if entry is None:
            return
        self.manual_view.setHtml(manual.body_html(entry))
        self.manual_view.verticalScrollBar().setValue(0)

    def save_manual(self, path: str = "") -> str:
        """整本另存（HTML / Markdown）；给了路径就不再弹保存框。返回写入路径。"""
        from ... import APP_SHORT, APP_VERSION

        if not path:
            start = os.path.join(
                self.base_dir or os.getcwd(),
                manual.default_filename(".html", app_name=APP_SHORT),
            )
            path, chosen = QFileDialog.getSaveFileName(
                self, "保存操作手册", start, "网页 (*.html);;Markdown (*.md);;所有文件 (*)"
            )
            if not path:
                return ""
            if not path.lower().endswith((".html", ".htm", ".md")) and chosen.startswith("Markdown"):
                path += ".md"
        try:
            if path.lower().endswith(".md"):
                saved = manual.save_markdown(path, app_name=APP_SHORT, app_version=APP_VERSION)
            else:
                saved = manual.save_html(path, app_name=APP_SHORT, app_version=APP_VERSION)
        except OSError as exc:
            QMessageBox.warning(self, "保存失败", str(exc))
            return ""
        self.manual_status.setText(f"已保存：{saved}")
        return saved

    # -------------------------------------------------- 标准与规范

    def _build_standards(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        disclaimer = QLabel(standards.DISCLAIMER)
        disclaimer.setWordWrap(True)
        disclaimer.setTextFormat(Qt.RichText)
        disclaimer.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(disclaimer)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_catalog())
        splitter.addWidget(self._build_library())
        splitter.setSizes([760, 400])
        layout.addWidget(splitter, 1)
        return page

    def _build_catalog(self) -> QWidget:
        box = QGroupBox("国家标准 / 行业标准索引（只有条目，不含正文）")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        self.relevant_label = QLabel("")
        self.relevant_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.relevant_label.setWordWrap(True)
        layout.addWidget(self.relevant_label)

        self.standard_table = QTableWidget(0, len(STANDARD_COLUMNS))
        self.standard_table.setHorizontalHeaderLabels(list(STANDARD_COLUMNS))
        self.standard_table.verticalHeader().setVisible(False)
        self.standard_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.standard_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.standard_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.standard_table.setAlternatingRowColors(True)
        self.standard_table.doubleClicked.connect(lambda _index: self.open_standard_page())
        self.standard_table.itemSelectionChanged.connect(self._sync_standard_detail)
        header = self.standard_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        self.standard_table.setColumnWidth(0, 92)
        self.standard_table.setColumnWidth(1, 168)
        layout.addWidget(self.standard_table, 1)

        self.standard_detail = QLabel("选一条看它跟软件的哪块对得上（双击去官方系统查看）")
        self.standard_detail.setWordWrap(True)
        self.standard_detail.setMinimumHeight(48)
        self.standard_detail.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(self.standard_detail)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.open_standard_button = QPushButton("在官方系统查看")
        self.open_standard_button.setToolTip(
            "GB/GB-T 会打开「国家标准全文公开系统」的检索页；行业标准打开标准信息平台"
        )
        self.open_standard_button.clicked.connect(self.open_standard_page)
        copy_button = QPushButton("复制编号")
        copy_button.clicked.connect(self.copy_standard_code)
        row.addWidget(self.open_standard_button)
        row.addWidget(copy_button)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _build_library(self) -> QWidget:
        box = QGroupBox("本地标准库（随工程保存）")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        self.library_path_label = QLabel("")
        self.library_path_label.setWordWrap(True)
        self.library_path_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.library_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.library_path_label)

        self.library_list = QListWidget()
        self.library_list.setAlternatingRowColors(True)
        self.library_list.itemDoubleClicked.connect(lambda _item: self.open_library_file())
        layout.addWidget(self.library_list, 1)

        self.library_empty = QLabel("还没有导入文件。点「导入…」把手里的标准 PDF 放进来。")
        self.library_empty.setWordWrap(True)
        self.library_empty.setStyleSheet(f"color: {theme.QUALITY_UNCERTAIN}; font-size: 11px;")
        layout.addWidget(self.library_empty)

        row = QHBoxLayout()
        row.setSpacing(6)
        import_button = QPushButton("导入…")
        import_button.setObjectName("primary")
        import_button.clicked.connect(self.import_files)
        self.open_file_button = QPushButton("打开")
        self.open_file_button.clicked.connect(self.open_library_file)
        folder_button = QPushButton("打开文件夹")
        folder_button.clicked.connect(self.open_library_folder)
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh_library)
        for widget in (import_button, self.open_file_button, folder_button, refresh_button):
            row.addWidget(widget)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    # -------------------------------------------------- 行为

    def library(self) -> str:
        return standards.library_dir(self.library_root, self.base_dir)

    def refresh_library(self) -> None:
        folder = self.library()
        self.library_path_label.setText(f"目录：{folder}")
        self.library_list.clear()
        files = standards.scan_files(folder)
        for entry in files:
            item = QListWidgetItem(
                f"{entry['name']}　{standards.format_size(entry['size'])}"
                f"　{standards.format_time(entry['mtime'])}"
            )
            item.setData(Qt.UserRole, entry["path"])
            item.setToolTip(entry["path"])
            self.library_list.addItem(item)
        self.library_empty.setVisible(not files)
        self.open_file_button.setEnabled(bool(files))

    def selected_library_path(self) -> str:
        item = self.library_list.currentItem()
        return str(item.data(Qt.UserRole)) if item is not None else ""

    def import_files(self) -> list[str]:
        """选文件 → 复制进标准库 → 刷新列表，返回存下来的路径。"""
        paths, _filter = QFileDialog.getOpenFileNames(
            self, "导入标准文件（只复制到本工程，不会上传）", "", standards.file_filter()
        )
        return self.import_paths(paths)

    def import_paths(self, paths: list[str]) -> list[str]:
        if not paths:
            return []
        folder = standards.library_dir(self.library_root, self.base_dir, create=True)
        saved: list[str] = []
        failures: list[str] = []
        for path in paths:
            try:
                saved.append(standards.import_file(path, folder))
            except OSError as exc:
                failures.append(str(exc))
        self.refresh_library()
        if failures:
            QMessageBox.warning(self, "部分文件没导入成功", "\n".join(failures))
        elif saved:
            self.library_list.setCurrentRow(max(0, self.library_list.count() - 1))
        return saved

    def open_library_file(self) -> None:
        path = self.selected_library_path()
        if not path:
            return
        from ...core import shell

        try:
            shell.open_path(path)
        except OSError as exc:
            QMessageBox.warning(self, "打不开", str(exc))

    def open_library_folder(self) -> None:
        from ...core import shell

        folder = standards.library_dir(self.library_root, self.base_dir, create=True)
        try:
            shell.open_path(folder)
        except OSError as exc:
            QMessageBox.warning(self, "打不开", str(exc))

    # -------------------------------------------------- 清单

    def fill_standards(self) -> None:
        """按"跟本工程相关"排好序，把清单填进表格。"""
        table = self.standard_table
        table.setRowCount(0)
        groups = standards.ordered_groups(self.project_name)
        marked = [group for group, related in groups if related]
        if marked:
            self.relevant_label.setText(
                f"工程「{self.project_name}」相关：{'、'.join('★' + name for name in marked)}"
                "（这几组排在最前）"
            )
        else:
            self.relevant_label.setText(
                "按分组列出；工程名里带「储能 / 配变 / 光伏」这类词时，相关的组会自动排到最前"
            )
        for group, related in groups:
            for item in standards.by_group().get(group, []):
                row = table.rowCount()
                table.insertRow(row)
                cells = [("★ " if related else "") + item.group, item.code, item.title]
                for column, text in enumerate(cells):
                    cell = QTableWidgetItem(text)
                    if column in (0, 1):
                        cell.setForeground(
                            _colour(theme.ACCENT if related else theme.TEXT_DIM)
                        )
                    if column == 1:
                        cell.setData(Qt.UserRole, item.code)
                    cell.setToolTip(text)
                    table.setItem(row, column, cell)
        if table.rowCount():
            table.selectRow(0)  # 默认选中第一条，下面的说明行就有内容
        self._sync_standard_detail()

    def _sync_standard_detail(self) -> None:
        """把选中那条的"跟软件哪块对得上"显示在表格下面（列太窄放不下）。"""
        code = self.selected_standard_code()
        detail = next((item for item in standards.STANDARDS if item.code == code), None)
        if detail is None:
            self.standard_detail.setText("选一条看它跟软件的哪块对得上（双击去官方系统查看）")
            return
        self.standard_detail.setText(
            f"<b>{detail.code}　{detail.title}</b><br>{detail.note}"
        )

    def selected_standard_code(self) -> str:
        rows = self.standard_table.selectionModel().selectedRows() if self.standard_table.selectionModel() else []
        if not rows:
            return ""
        item = self.standard_table.item(rows[0].row(), 1)
        return str(item.data(Qt.UserRole) or item.text()) if item is not None else ""

    def open_standard_page(self) -> None:
        code = self.selected_standard_code()
        if not code:
            return
        QDesktopServices.openUrl(QUrl(standards.search_url(code)))

    def copy_standard_code(self) -> None:
        code = self.selected_standard_code()
        if not code:
            return
        QApplication.clipboard().setText(code)


def _colour(text: str) -> QColor:
    return QColor(text or theme.TEXT)


def standard_rows(dialog: AboutDialog) -> list[tuple[str, str]]:
    """测试/导出用：表格里的 ``[(编号, 名称)]``。"""
    rows: list[tuple[str, str]] = []
    for row in range(dialog.standard_table.rowCount()):
        code_item = dialog.standard_table.item(row, 1)
        title_item = dialog.standard_table.item(row, 2)
        rows.append(
            (
                str(code_item.data(Qt.UserRole) if code_item else ""),
                str(title_item.text() if title_item else ""),
            )
        )
    return rows


__all__ = ["AboutDialog", "STANDARD_COLUMNS", "about_html", "standard_rows"]
