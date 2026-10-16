"""Qt 全局初始化。

放在单独模块里，保证不管从 main.py 启动还是测试/脚本直接建 MainWindow，
都会先做一遍这些全局设置。
"""

from __future__ import annotations

from PySide6.QtCore import QLocale


def fix_number_locale() -> bool:
    """把数字格式固定成 0-9，返回是否做过改动。

    Qt 的数字格式化跟系统 locale 走，而部分中文 locale（实测 **zh_HK**）
    会用 CJK 数字字符：``QLocale('zh_HK').toString(120) == '〡〢〇'``。
    结果是所有 QSpinBox / QDoubleSpinBox 都显示成苏州码子，参数根本没法读。

    界面里的中文文案都是我们自己拼的，不依赖 locale；
    日期时间也一律用 :func:`time.strftime` 自己格式化。所以把 locale 固定成
    英文数字格式最稳，不会影响中文显示。
    """
    if QLocale().toString(120) == "120" and QLocale().toString(1.5) == "1.5":
        return False
    QLocale.setDefault(QLocale(QLocale.English, QLocale.UnitedStates))
    return True


def prepare_qt() -> bool:
    """建立 QApplication 之前的全局准备。"""
    return fix_number_locale()
