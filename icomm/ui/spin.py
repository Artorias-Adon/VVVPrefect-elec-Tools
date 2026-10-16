"""数字框的一个救命补丁。

Qt 的坑（本机实测）：只要 QSS 里有一条规则匹配到 ``QSpinBox`` / ``QDoubleSpinBox`` ——
哪怕规则体是空的 —— 带 ``ButtonSymbols.NoButtons`` 的数字框，内层编辑区就会被
算成宽 1px，数字被裁得一干二净，界面上只剩一个空框。

本软件的主题给所有数字框都套了样式，于是所有"隐藏了上下箭头"的数字框
（数据记录、故障录波、变量触发、定时控制、TCP 测试、截屏辅助、限幅设置…）
在用户眼里就是一片空的输入框 —— 反馈里的"有些窗口输入不显示"就是这个。

QSS 里没有别的抓手（``min-width``、子选择器都试过，压不住），
只能在尺寸变化时把内层编辑框的最小宽度顶到整个框宽：``setGeometry`` 会被
``minimumWidth`` 夹住，1px 就被顶回来了。想隐藏箭头请用 :func:`no_arrows`，
不要自己调 ``setButtonSymbols``。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QAbstractSpinBox

#: 内层编辑框左右各留一点，别贴着边框
_SIDE_GAP = 6


class _FieldWidthKeeper(QObject):
    """跟着数字框的尺寸，顶住内层编辑区的最小宽度。"""

    def eventFilter(self, obj, event) -> bool:  # noqa: item2
        if event.type() in (QEvent.Resize, QEvent.Show, QEvent.StyleChange):
            inner = obj.lineEdit()
            if inner is not None:
                inner.setMinimumWidth(max(16, obj.width() - 2 * _SIDE_GAP))
        return False


_keeper = _FieldWidthKeeper()


def no_arrows(box: QAbstractSpinBox) -> QAbstractSpinBox:
    """隐藏数字框的上下箭头，同时保证数字仍然看得见。

    返回传入的控件，方便顺手链式调用。所有想"不要箭头"的地方都走这里。
    """
    box.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
    box.installEventFilter(_keeper)
    inner = box.lineEdit()
    if inner is not None:
        inner.setMinimumWidth(max(16, box.width() - 2 * _SIDE_GAP))
    return box
