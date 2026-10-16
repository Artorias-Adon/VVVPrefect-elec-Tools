"""另存为对话框：先选格式，再选路径。

格式清单来自 :mod:`icomm.core.writers` 的注册表，所以新增一种保存格式
（参数表的 txt / json、工程的 iprj……）界面会自动多出一项，不用改这里。

选中的格式会用原生文件对话框去选路径，并且**跟着格式联动**：
换格式时默认文件名与过滤器一起变，不会出现"选了 CSV 却存成 .json"。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ...core.writers import ProjectWriter


class SaveAsDialog(QDialog):
    """「保存参数设置 / 保存整个工程」的格式与路径选择。"""

    def __init__(
        self,
        title: str,
        available: list,
        default_name: str = "",
        part: str = "工程",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._writers = list(available)
        self._default_name = default_name or "untitled"
        self._part = part
        self._path = ""

        self.setWindowTitle(title)
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QLabel(f"把{part}保存成哪种格式？")
        head.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
        root.addWidget(head)

        box = QGroupBox("格式与位置")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.format_combo = QComboBox()
        for writer in self._writers:
            self.format_combo.addItem(str(writer), writer)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)
        form.addRow("格式", self.format_combo)

        self.detail = QLabel("")
        self.detail.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.detail.setWordWrap(True)
        form.addRow("", self.detail)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("点「浏览…」选择保存位置")
        row.addWidget(self.path_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        wrapper = QWidget()
        wrapper.setLayout(row)
        form.addRow("保存到", wrapper)
        root.addWidget(box)

        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.hint.setWordWrap(True)
        root.addWidget(self.hint)

        buttons = QDialogButtonBox()
        self.ok_button = buttons.addButton("保存", QDialogButtonBox.AcceptRole)
        self.ok_button.setObjectName("primary")
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._on_format_changed()

    # -------------------------------------------------- 格式

    def selected_writer(self):
        return self.format_combo.currentData()

    def target_path(self) -> str:
        return self._path

    def _suggested(self, writer) -> str:
        suffix = writer.suffix or ""
        return os.path.join(os.path.expanduser("~"), f"{self._default_name}{suffix}")

    def _on_format_changed(self) -> None:
        writer = self.selected_writer()
        if writer is None:
            return
        self.detail.setText(writer.description or "")
        if not self._path:
            self.path_edit.setText(self._suggested(writer))
        else:
            # 换格式时顺手把扩展名对齐，避免"选了 JSON 但文件名还是 .csv"
            stem, old = os.path.splitext(self._path)
            if writer.suffix and old.lower() not in (
                writer.suffix.lower(),
                *[s.lower() for s in getattr(writer, "extra_suffixes", ())],
            ):
                self._path = stem + writer.suffix
                self.path_edit.setText(self._path)

        if isinstance(writer, ProjectWriter) and not writer.single_file:
            self.hint.setText(
                "工程目录格式会新建一个文件夹，里面放 project.json、参数表与历史库，"
                "可以整个文件夹拷给同事。"
            )
        else:
            self.hint.setText("")

    # -------------------------------------------------- 路径

    def _browse(self) -> None:
        writer = self.selected_writer()
        if writer is None:
            return
        current = self.path_edit.text().strip() or self._suggested(writer)
        default_dir = os.path.dirname(current) or os.path.expanduser("~")

        if isinstance(writer, ProjectWriter) and not writer.single_file:
            chosen = QFileDialog.getExistingDirectory(
                self, "选择工程文件夹（不存在会自动创建）", default_dir
            )
            if chosen:
                self._path = chosen
                self.path_edit.setText(chosen)
            return

        filters = [writer.file_filter, "所有文件 (*)"]
        chosen, _selected = QFileDialog.getSaveFileName(
            self, "选择保存位置", current, ";;".join(filters)
        )
        if chosen:
            self._path = chosen
            self.path_edit.setText(chosen)

    def _accept(self) -> None:
        text = self.path_edit.text().strip()
        if not text:
            self.hint.setText("请先选择保存位置。")
            return
        writer = self.selected_writer()
        if writer is None:
            return
        # 用户手敲路径时也要补扩展名（目录格式不补）
        if not (isinstance(writer, ProjectWriter) and not writer.single_file):
            if writer.suffix and not text.lower().endswith(writer.suffix.lower()):
                text += writer.suffix
        self._path = text
        self.accept()
