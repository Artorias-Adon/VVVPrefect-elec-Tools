"""数字框可见性回归测试。

用户反馈"有些窗口输入不显示"。实测根因：只要 QSS 里有一条规则匹配到
``QSpinBox`` / ``QDoubleSpinBox``（哪怕规则体是空的），带
``ButtonSymbols.NoButtons`` 的数字框，内层编辑区就会被算成宽 1px，
数字被裁得一干二净，界面上只剩一个空框 —— 所有"隐藏了箭头"的数字框都中招。

修法见 :mod:`icomm.ui.spin`：别自己调 ``setButtonSymbols``，用 :func:`no_arrows`。
"""

from __future__ import annotations

import os
import pathlib
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (  # noqa: E402
    QAbstractSpinBox,
    QApplication,
    QDoubleSpinBox,
    QSpinBox,
)

from icomm.ui import qtboot, theme  # noqa: E402
from icomm.ui.spin import no_arrows  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    qtboot.prepare_qt()
    _app = QApplication.instance() or QApplication([])
    _app.setStyleSheet(theme.STYLESHEET)


class SpinFieldWidthTest(unittest.TestCase):
    def _box(self, width: int = 318, use_helper: bool = True, decimals: int = 3):
        box = QDoubleSpinBox()
        box.setRange(-1e12, 1e12)
        box.setDecimals(decimals)
        box.setValue(12.5)
        box.setSuffix(" V")
        if use_helper:
            no_arrows(box)
        box.resize(width, 32)
        box.show()
        _app.processEvents()
        self.addCleanup(box.deleteLater)
        return box

    def test_helper_sets_no_buttons(self) -> None:
        box = self._box()
        self.assertEqual(
            box.buttonSymbols(), QAbstractSpinBox.ButtonSymbols.NoButtons
        )

    def test_helper_returns_same_widget(self) -> None:
        """顺手返回原控件，方便链式写法。"""
        box = QSpinBox()
        self.assertIs(no_arrows(box), box)

    def test_inner_edit_keeps_full_width(self) -> None:
        """核心回归：内层编辑区不能塌成 1px，否则数字整个看不见。"""
        box = self._box(318)
        inner = box.lineEdit()
        self.assertIsNotNone(inner)
        self.assertGreater(inner.width(), 0.8 * box.width(), "内层编辑区被压塌了")

    def test_inner_edit_grows_wider(self) -> None:
        box = self._box(500)
        self.assertGreater(box.lineEdit().width(), 400)

    def test_inner_edit_tracks_resize(self) -> None:
        """对话框拉伸/压缩后仍要跟得上。"""
        box = self._box(400)
        box.resize(220, 32)
        _app.processEvents()
        self.assertGreater(box.lineEdit().width(), 0.8 * box.width())

    def test_survives_stylesheet_reapply(self) -> None:
        """换主题（重新套样式）之后不能又塌回去。"""
        box = self._box()
        _app.setStyleSheet(theme.STYLESHEET)
        _app.processEvents()
        self.assertGreater(box.lineEdit().width(), 0.8 * box.width())

    def test_only_helper_avoids_the_trap(self) -> None:
        """反证：直接调 setButtonSymbols(NoButtons) 就是会塌 —— 所以别这么写。"""
        box = QDoubleSpinBox()
        box.setRange(-1e12, 1e12)
        box.setValue(1.0)
        box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        box.resize(318, 32)
        box.show()
        self.addCleanup(box.deleteLater)
        _app.processEvents()
        self.assertLess(box.lineEdit().width(), 10)

    def test_arrow_style_is_fine(self) -> None:
        """带箭头的数字框不受这个坑影响。"""
        box = QDoubleSpinBox()
        box.setRange(-1e12, 1e12)
        box.setValue(1.0)
        box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.UpDownArrows)
        box.resize(318, 32)
        box.show()
        self.addCleanup(box.deleteLater)
        _app.processEvents()
        self.assertGreater(box.lineEdit().width(), 0.5 * box.width())


class SourceGuardTest(unittest.TestCase):
    """防止以后又有人直接写 setButtonSymbols(NoButtons) 把数字弄没。"""

    def test_no_direct_no_buttons_outside_helper(self) -> None:
        root = pathlib.Path(__file__).resolve().parent.parent / "icomm"
        offenders = []
        for path in root.rglob("*.py"):
            if path.name == "spin.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "ButtonSymbols.NoButtons" in text:
                offenders.append(str(path.relative_to(root)))
        self.assertEqual(
            offenders,
            [],
            "这些文件直接用了 ButtonSymbols.NoButtons（数字会看不见），"
            "请改用 icomm.ui.spin.no_arrows",
        )

    def test_helper_is_used_by_no_arrow_widgets(self) -> None:
        """确认该用助手的地方真的用了（避免改了又被回退）。"""
        root = pathlib.Path(__file__).resolve().parent.parent / "icomm"
        users = [
            path
            for path in root.rglob("*.py")
            if "from ..spin import no_arrows" in path.read_text(encoding="utf-8")
        ]
        self.assertGreaterEqual(len(users), 10)


if __name__ == "__main__":
    unittest.main()
