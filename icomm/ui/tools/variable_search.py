"""变量搜索：一个变量在软件里到底出现在哪些地方。

现场调试点表时最常问的三件事：「这个点在哪个设备/哪条通道？」「地址是多少？」
「哪个页面看得到它？」。这个工具一次答完，并且能直接跳过去。

匹配逻辑在 :mod:`icomm.core.search`（纯逻辑、可单测），这里只做界面与跳转。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core.search import SearchHit, search_points
from .. import theme
from .base import DialogTool, ToolContext, register_tool

COLUMNS = ["点号", "显示名", "设备", "从站号", "通道", "链路", "寄存器", "地址", "类型", "单位", "分组", "出现位置", "说明"]


class VariableSearchDialog(QDialog):
    """搜工程里的点位定义。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self._hits: list[SearchHit] = []

        self.setWindowTitle("变量搜索")
        self.setMinimumSize(1080, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("输入点号 / 显示名 / 地址 / 分组 / 说明里的任意片段")
        self.query_edit.returnPressed.connect(self.do_search)
        self.query_edit.textChanged.connect(self.do_search)
        top.addWidget(self.query_edit, 1)
        root.addLayout(top)

        options = QHBoxLayout()
        options.setSpacing(10)
        self.match_case = QCheckBox("区分大小写")
        self.match_case.toggled.connect(self.do_search)
        options.addWidget(self.match_case)
        self.writable_only = QCheckBox("只看可写点位")
        self.writable_only.toggled.connect(self.do_search)
        options.addWidget(self.writable_only)
        self.by_address = QCheckBox("把查询当地址（十进制或 0x 开头）")
        self.by_address.setToolTip("点表核对时按地址找点位最常用")
        self.by_address.toggled.connect(self.do_search)
        options.addWidget(self.by_address)
        options.addStretch(1)
        self.summary = QLabel("")
        self.summary.setStyleSheet(f"color: {theme.TEXT_DIM};")
        options.addWidget(self.summary)
        root.addLayout(options)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        for index, width in enumerate((170, 150, 130, 70, 120, 100, 80, 70, 80, 60, 110, 220, 260)):
            self.table.setColumnWidth(index, width)
        self.table.doubleClicked.connect(self._on_activated)
        root.addWidget(self.table, 1)

        hint = QLabel("双击一行：跳到该点位的定义（「设备与点位」页选中它）")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(hint)

        buttons = QDialogButtonBox()
        add_scope = buttons.addButton("加到示波器", QDialogButtonBox.ActionRole)
        add_scope.clicked.connect(self._add_to_scope)
        reveal = buttons.addButton("跳到定义", QDialogButtonBox.ActionRole)
        reveal.clicked.connect(lambda: self._on_activated())
        buttons.addButton("关闭", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        root.addWidget(buttons)

        self.do_search()

    # -------------------------------------------------- 搜索

    def do_search(self) -> None:
        text = self.query_edit.text()
        self._hits = search_points(
            self.context.project,
            text,
            case_sensitive=self.match_case.isChecked(),
            writable_only=self.writable_only.isChecked(),
            as_address=self.by_address.isChecked(),
            # 「出现位置」需要知道哪些点位被放到了监视页/示波器，写值与报警也算"出现"
            scope_pids=None,
        )
        self._fill()

    def _fill(self) -> None:
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._hits))
        for row, hit in enumerate(self._hits):
            for column, value in enumerate(hit.row()):
                item = QTableWidgetItem(str(value))
                if column in (3, 7):
                    item.setTextAlignment(Qt.AlignCenter)
                if column == 0:
                    item.setData(Qt.UserRole, hit.pid)
                if column in (2, 4) and hit.chain_issues:
                    item.setForeground(QColor(theme.QUALITY_STALE))
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(sorting)

        total = len(self._hits)
        if not self.query_edit.text().strip():
            self.summary.setText(f"工程共 {total} 个点位")
        else:
            self.summary.setText(f"匹配 {total} 个")

    # -------------------------------------------------- 跳转

    def _selected_pid(self) -> str:
        row = self.table.currentRow()
        if row < 0:
            return ""
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item is not None else ""

    def _on_activated(self) -> None:
        pid = self._selected_pid()
        if pid and self.context.reveal_point is not None:
            self.context.reveal_point(pid)
            self.context.report(f"已定位到 {pid}")

    def _add_to_scope(self) -> None:
        pid = self._selected_pid()
        if pid and self.context.add_to_scope is not None:
            self.context.add_to_scope(pid)
            self.context.report(f"已把 {pid} 加到示波器")


@register_tool
class VariableSearchTool(DialogTool):
    name = "变量搜索"
    group = "工程"
    order = 10
    description = "按点号/显示名/地址/分组找它在哪台设备、哪个页面"

    def build(self, context: ToolContext) -> QDialog:
        return VariableSearchDialog(context)
