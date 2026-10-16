"""写值限幅对话框。

现场很实际的一个需求：给定值不许超过某个范围。上位机手滑把 700 打成 70000，
如果下位机没有保护，可能直接炸设备。限幅设在这里，引擎在**单发与持续发送
两条路径**上都会夹紧，并且写一条日志说明"被限幅了"。

上限或下限留空表示那一侧不限。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ..spin import no_arrows


class WriteLimitDialog(QDialog):
    """设置某个点位的写值上下限。"""

    def __init__(self, pid: str, point, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.pid = pid
        self.point = point

        self.setWindowTitle(f"写值限幅 · {point.title}")
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QLabel(f"<b>{point.title}</b>　{pid}")
        head.setTextFormat(Qt.RichText)
        root.addWidget(head)

        hint = QLabel(
            "超出范围的值会被自动夹到边界再下发，并在通信日志里留一条说明。\n"
            "上限或下限留空表示那一侧不限。位与命令点位不受影响。"
        )
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        unit = f" {point.unit}" if point.unit else ""

        row = QHBoxLayout()
        row.setSpacing(6)
        self.low_check = QCheckBox("启用")
        self.low_check.setChecked(point.write_min is not None)
        row.addWidget(self.low_check)
        self.low_box = QDoubleSpinBox()
        self.low_box.setRange(-1e12, 1e12)
        self.low_box.setDecimals(3)
        self.low_box.setValue(float(point.write_min) if point.write_min is not None else 0.0)
        self.low_box.setSuffix(unit)
        no_arrows(self.low_box)
        self.low_box.setEnabled(self.low_check.isChecked())
        self.low_check.toggled.connect(self.low_box.setEnabled)
        row.addWidget(self.low_box, 1)
        wrapper = QWidget()
        wrapper.setLayout(row)
        form.addRow("下限", wrapper)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.high_check = QCheckBox("启用")
        self.high_check.setChecked(point.write_max is not None)
        row.addWidget(self.high_check)
        self.high_box = QDoubleSpinBox()
        self.high_box.setRange(-1e12, 1e12)
        self.high_box.setDecimals(3)
        self.high_box.setValue(float(point.write_max) if point.write_max is not None else 0.0)
        self.high_box.setSuffix(unit)
        no_arrows(self.high_box)
        self.high_box.setEnabled(self.high_check.isChecked())
        self.high_check.toggled.connect(self.high_box.setEnabled)
        row.addWidget(self.high_box, 1)
        wrapper = QWidget()
        wrapper.setLayout(row)
        form.addRow("上限", wrapper)

        root.addLayout(form)

        self.warn_box = QCheckBox("夹紧时写一条日志提示")
        self.warn_box.setChecked(point.write_limit_warn)
        root.addWidget(self.warn_box)

        self.current = QLabel("")
        self.current.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.current)
        for widget in (self.low_box, self.high_box):
            widget.valueChanged.connect(self._refresh_preview)
        for widget in (self.low_check, self.high_check):
            widget.toggled.connect(self._refresh_preview)
        self._refresh_preview()

        buttons = QDialogButtonBox()
        clear = buttons.addButton("清除限幅", QDialogButtonBox.ResetRole)
        clear.clicked.connect(self._clear)
        buttons.addButton("确定", QDialogButtonBox.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # -------------------------------------------------- 取值

    def values(self) -> tuple[float | None, float | None]:
        low = float(self.low_box.value()) if self.low_check.isChecked() else None
        high = float(self.high_box.value()) if self.high_check.isChecked() else None
        return low, high

    def _clear(self) -> None:
        self.low_check.setChecked(False)
        self.high_check.setChecked(False)

    def _refresh_preview(self) -> None:
        low, high = self.values()
        if low is not None and high is not None and low > high:
            self.current.setText(
                f"<span style='color:{theme.QUALITY_BAD}'>下限比上限大，限幅不会生效</span>"
            )
            self.current.setTextFormat(Qt.RichText)
            return
        low_text = "不限" if low is None else f"{low:g}"
        high_text = "不限" if high is None else f"{high:g}"
        self.current.setText(f"生效范围：{low_text} ~ {high_text}")

    def _accept(self) -> None:
        low, high = self.values()
        if low is not None and high is not None and low > high:
            # 允许保存（模型层会当作不限处理），但要让用户知道
            self.current.setText("下限比上限大，限幅不会生效")
        self.point.write_limit_warn = self.warn_box.isChecked()
        self.accept()
