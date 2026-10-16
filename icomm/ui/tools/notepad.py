"""记事本：随手记现场信息。

调试点表、试参数的时候总要记点东西（"8 月 3 日把 Kp 从 0.4 改到 0.55 后振荡消失"、
"DSP 换过一块，序列号 xxx"）。用外部记事本还要切窗口，所以内置一个，
内容**随工程保存**（每台设备的记录跟工程走，不会张冠李戴）。

内容存在项目文件里（``Project.notes``），所以打包成 ``.iprj`` 发给同事时一起带走。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from .base import ToolContext, WindowTool, register_tool

#: 自动保存间隔：打字时别每敲一个字都写盘
AUTOSAVE_MS = 1200


class NotepadWindow(QWidget):
    """一个跟工程绑定的记事本。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self._dirty = False

        self.setWindowTitle(f"记事本 · {context.project.name}")
        # 浮动工具窗：可以和主窗口并排用
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(620, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel("现场记录")
        title.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
        head.addWidget(title)
        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        head.addWidget(self.status)
        head.addStretch(1)
        insert = QPushButton("插入时间戳")
        insert.setToolTip("在光标处插入当前时间，「什么时候改的」记起来很方便")
        insert.clicked.connect(self.insert_timestamp)
        head.addWidget(insert)
        save = QPushButton("立即保存")
        save.clicked.connect(self.save_now)
        head.addWidget(save)
        root.addLayout(head)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(getattr(context.project, "notes", "") or "")
        font = QFont()
        font.setPointSize(11)
        self.editor.setFont(font)
        self.editor.setPlaceholderText(
            "在这里记现场情况：改了什么参数、换过什么硬件、出现过什么现象……\n"
            "内容随工程保存（打包 .iprj 时会一起带走）。"
        )
        self.editor.textChanged.connect(self._on_changed)
        root.addWidget(self.editor, 1)

        hint = QLabel("内容随工程保存；关窗口时也会自动存一次。")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(hint)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(AUTOSAVE_MS)
        self._timer.timeout.connect(self.save_now)

    # -------------------------------------------------- 保存

    def _on_changed(self) -> None:
        self._dirty = True
        self.status.setText("未保存…")
        self._timer.start()

    def insert_timestamp(self) -> None:
        import time

        self.editor.insertPlainText(time.strftime("\n[%Y-%m-%d %H:%M] "))
        self.editor.setFocus()

    def save_now(self) -> None:
        """把文本写回工程并让工程标脏（这样"保存工程"会一起落盘）。"""
        if not self._dirty:
            self.status.setText("已保存")
            return
        self._timer.stop()
        self.context.project.notes = self.editor.toPlainText()
        self._dirty = False
        self.status.setText("已保存到工程（记得保存工程）")
        if self.context.project_changed is not None:
            self.context.project_changed()

    def closeEvent(self, event) -> None:  # noqa: item2
        self.save_now()
        self._timer.stop()
        super().closeEvent(event)


@register_tool
class NotepadTool(WindowTool):
    name = "记事本"
    group = "工程"
    order = 20
    description = "随手记现场信息，内容随工程保存"

    def build(self, context: ToolContext) -> QWidget:
        return NotepadWindow(context)
