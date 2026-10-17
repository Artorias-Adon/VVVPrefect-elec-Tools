"""截屏辅助：某个通道达到某个值时自动把界面截下来。

现场最有用的场景是"抓现场"：某个量一超限就截一张图，事后翻图找原因，
比翻历史曲线快得多（图上有当时全部画面）。

实现完全建立在 :mod:`icomm.core.automation` 上：截屏动作只是一个回调，
触发条件的判断、去抖、冷却、次数限制都由自动化核心负责。
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import automation
from ...core.automation import Compare, Task, Trigger, TriggerMode
from .. import theme
from ..spin import no_arrows
from .base import DialogTool, ToolContext, register_tool
from .variable_trigger import (
    CAPTURE_SERVICE,
    build_pid_combo,
    build_trigger_fields,
    read_trigger_fields,
    trigger_summary,
)

#: 文件名里不能出现的字符
_BAD_CHARS = '<>:"/\\|?*'


class ScreenshotDialog(QDialog):
    """配置"到什么值就截屏"。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.service: automation.AutomationService | None = context.service(CAPTURE_SERVICE)

        self.setWindowTitle("截屏辅助")
        self.setMinimumSize(760, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        hint = QLabel(
            "设定条件，满足时自动把主窗口截一张图存下来。\n"
            "触发判断由「自动化核心」负责，支持边沿/电平方​​式、连续次数去抖与冷却时间。"
        )
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        root.addWidget(self._build_trigger())
        root.addWidget(self._build_output())
        root.addWidget(self._build_list(), 1)

        buttons = QDialogButtonBox()
        add = buttons.addButton("添加任务", QDialogButtonBox.ActionRole)
        add.setObjectName("primary")
        add.clicked.connect(self.add_task)
        buttons.addButton("关闭", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        root.addWidget(buttons)

    # -------------------------------------------------- 界面

    def _build_trigger(self) -> QWidget:
        box = QGroupBox("触发条件")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.fields = build_trigger_fields(self.context, default_compare=Compare.GE)
        for row in self.fields.rows():
            form.addRow(*row)
        return box

    def _build_output(self) -> QWidget:
        box = QGroupBox("保存")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        row = QHBoxLayout()
        row.setSpacing(6)
        default_dir = self.context.report_subdir("shots")
        self.dir_edit = QLineEdit(default_dir)
        row.addWidget(self.dir_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._pick_dir)
        row.addWidget(browse)
        wrapper = QWidget()
        wrapper.setLayout(row)
        form.addRow("保存目录", wrapper)

        self.prefix_edit = QLineEdit("shot")
        self.prefix_edit.setPlaceholderText("文件名前缀")
        form.addRow("文件名前缀", self.prefix_edit)

        note = QLabel("文件名会自动带时间戳，例如 shot_20261016_014530.png")
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        form.addRow("", note)

        self.max_box = QSpinBox()
        self.max_box.setRange(0, 9999)
        self.max_box.setValue(20)
        self.max_box.setSuffix(" 张")
        self.max_box.setToolTip("0 = 不限（小心把磁盘塞满）")
        no_arrows(self.max_box)
        form.addRow("最多截", self.max_box)

        self.window_only = QCheckBox("只截主窗口（否则截整个屏幕）")
        self.window_only.setChecked(True)
        form.addRow("", self.window_only)
        return box

    def _build_list(self) -> QWidget:
        box = QGroupBox("已配置的截屏任务")
        layout = QVBoxLayout(box)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["名称", "触发条件", "状态", "保存目录"])
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)
        for index, width in enumerate((140, 300, 150, 260)):
            self.table.setColumnWidth(index, width)
        layout.addWidget(self.table)

        row = QHBoxLayout()
        row.setSpacing(6)
        manual = QPushButton("立即手动触发一次")
        manual.setToolTip("不用等条件，先试一下能不能正常截图")
        manual.clicked.connect(self._manual_shot)
        row.addWidget(manual)
        row.addStretch(1)
        remove = QPushButton("删除选中")
        remove.setObjectName("danger")
        remove.clicked.connect(self._remove_selected)
        row.addWidget(remove)
        layout.addLayout(row)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(self.status)
        return box

    # -------------------------------------------------- 动作

    def _pick_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择截屏保存目录", self.dir_edit.text())
        if chosen:
            self.dir_edit.setText(chosen)

    def preselect_pid(self, pid: str) -> None:
        """从右键菜单「用这个变量做触发条件」进来时预选好点位。"""
        index = self.fields.pid_combo.findData(pid)
        if index < 0:
            self.fields.pid_combo.addItem(self.context.title_of(pid) if self.context.title_of else pid, pid)
            index = self.fields.pid_combo.count() - 1
        self.fields.pid_combo.setCurrentIndex(index)
        self.fields.sync_visibility()
        self.raise_()
        self.activateWindow()

    def _shot_path(self, prefix: str) -> str:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        safe = "".join(ch for ch in (prefix or "shot") if ch not in _BAD_CHARS).strip() or "shot"
        directory = self.dir_edit.text().strip() or "."
        path = os.path.join(directory, f"{safe}_{stamp}.png")
        # 一秒内触发两次（例如去抖没设、量抖得厉害）会撞名，撞了就排队 ——
        # 抓现场图被悄悄覆盖掉是不可接受的
        index = 2
        while os.path.exists(path):
            path = os.path.join(directory, f"{safe}_{stamp}_{index}.png")
            index += 1
        return path

    def take_screenshot(self, prefix: str = "") -> str:
        """截屏并返回文件路径（空串表示失败）。

        默认截主窗口而不是整个屏幕：现场机器上可能还开着别的程序，
        截全屏反而把无关内容记进来了。
        """
        target = self.context.window.window()
        pixmap = target.grab() if self.window_only.isChecked() else target.screen().grabWindow(0)
        if pixmap is None or pixmap.isNull():
            self.status.setText("截屏失败：拿不到画面")
            return ""
        path = self._shot_path(prefix or self.prefix_edit.text())
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        if not pixmap.save(path, "PNG"):
            self.status.setText(f"保存失败：{path}")
            return ""
        self.status.setText(f"已保存 {path}")
        self.context.report(f"已截屏：{path}")
        self.context.announce("已截屏", os.path.basename(path), "info")
        return path

    def add_task(self) -> None:
        if self.service is None:
            self.status.setText("自动化核心不可用（应从主窗口打开本工具）")
            return
        try:
            trigger = read_trigger_fields(self.fields)
        except ValueError as exc:
            self.status.setText(str(exc))
            return

        prefix = self.prefix_edit.text().strip() or "shot"
        name = f"截屏：{trigger_summary(trigger, self.context)}"
        if self.service.find(name) is not None:
            # 同名条件重复添加时自动加序号，免得把上一条悄悄覆盖掉
            index = 2
            while self.service.find(f"{name} #{index}") is not None:
                index += 1
            name = f"{name} #{index}"

        limit = self.max_box.value()
        task = Task(
            name=name,
            trigger=trigger,
            max_fires=limit,
            action=lambda task, _values: self.take_screenshot(prefix),
        )
        task.data["dir"] = self.dir_edit.text().strip()
        self.service.add(task)
        self.refresh()
        self.context.report(f"已添加截屏任务：{name}")

    def _manual_shot(self) -> None:
        # 手动触发也走服务，这样"触发次数"统计一致
        if self.service is not None and self.table.currentRow() >= 0:
            name = self.table.item(self.table.currentRow(), 0).text()
            self.service.manual_trigger(name)
            self.refresh()
        else:
            self.take_screenshot()

    def _remove_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0 or self.service is None:
            return
        name = self.table.item(row, 0).text()
        self.service.remove(name)
        self.refresh()

    def refresh(self) -> None:
        if self.service is None:
            self.table.setRowCount(0)
            return
        rows = [task for task in self.service.tasks if task.name.startswith("截屏：")]
        self.table.setRowCount(len(rows))
        for index, task in enumerate(rows):
            cells = [
                task.name,
                task.trigger.describe(),
                task.status_text(),
                str(task.data.get("dir", "")),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column == 2 and task.last_error:
                    item.setForeground(QColor(theme.QUALITY_BAD))
                self.table.setItem(index, column, item)


@register_tool
class ScreenshotTool(DialogTool):
    name = "截屏辅助"
    group = "自动化"
    order = 10
    description = "某通道达到某值时自动截屏保存"

    def build(self, context: ToolContext) -> QDialog:
        return ScreenshotDialog(context)
