"""程序资源定位（图标等）。

打包成 exe 后资源被放进 ``sys._MEIPASS``（PyInstaller 单文件）或
``_internal`` 目录，源码运行时就在项目根下。这里把两种情况统一起来，
调用方只需说"我要 app_icon()"。
"""

from __future__ import annotations

import os
import sys

from PySide6.QtGui import QIcon, QPixmap

#: 图标文件在项目里的相对位置
ICON_RELATIVE = os.path.join("assets", "icon.ico")
#: 没有 .ico 时的兜底（PNG 也能被 QIcon 读）
ICON_FALLBACK = os.path.join("assets", "icon_64.png")

_cached: QIcon | None = None
_cached_pixmap: QPixmap | None = None


def resource_roots() -> list[str]:
    """按优先级列出可能存放资源的目录。"""
    roots: list[str] = []
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        roots.append(bundle)
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    roots.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    roots.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    return roots


def resource_path(relative: str) -> str:
    """找一个存在的资源路径；都找不到就返回第一个候选（便于报错时显示期望位置）。"""
    candidates = [os.path.join(root, relative) for root in resource_roots()]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


def app_icon() -> QIcon | None:
    """程序图标。找不到就返回 None（界面照常可用，只是没有图标）。"""
    global _cached
    if _cached is not None:
        return _cached if not _cached.isNull() else None

    for relative in (ICON_RELATIVE, ICON_FALLBACK):
        path = resource_path(relative)
        if os.path.exists(path):
            icon = QIcon(path)
            if not icon.isNull():
                _cached = icon
                return icon
    _cached = QIcon()
    return None


def app_pixmap(size: int = 64) -> QPixmap | None:
    """取某个尺寸的图标位图（"关于"对话框里显示用）。"""
    global _cached_pixmap
    icon = app_icon()
    if icon is None:
        return None
    if _cached_pixmap is None or _cached_pixmap.isNull():
        _cached_pixmap = icon.pixmap(256, 256)
    return icon.pixmap(size, size)
