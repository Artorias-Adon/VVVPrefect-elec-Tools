"""用系统默认程序打开文件/文件夹（Windows / macOS / Linux 各用各的）。

界面里到处有「打开目录」「播放录音」「打开录波」这类按钮。Windows 上是
``os.startfile``，macOS 上是 ``open``，Linux 上是 ``xdg-open`` —— 而
``os.startfile`` 在非 Windows 上**根本不存在**（AttributeError），
所以统一收在这里，免得每个工具各写一份 try/except。
"""

from __future__ import annotations

import os
import subprocess
import sys

#: 支持"用系统程序打开"的平台（三种都用不同命令）
SUPPORTED = ("nt", "darwin", "linux")


def command_for(path: str) -> list[str] | None:
    """该用哪条命令打开（Windows 返回 None —— 走 ``os.startfile``）。"""
    if os.name == "nt":
        return None
    if sys.platform == "darwin":
        return ["open", path]
    return ["xdg-open", path]


def open_path(path: str) -> None:
    """用系统默认程序打开文件或文件夹。

    路径不存在或系统不支持时抛 :class:`OSError`（调用方可以提示用户）。
    """
    if not path:
        raise OSError("没有要打开的路径")
    if not os.path.exists(path):
        raise OSError(f"路径不存在：{path}")
    if os.name == "nt":
        os.startfile(path)  # noqa: S606 - 用户主动点的「打开」
        return
    command = command_for(path)
    if command is None:  # pragma: no cover - 上面的分支已经兜住
        raise OSError("这个系统不支持用默认程序打开")
    try:
        subprocess.Popen(  # noqa: S603 - 命令与参数都是本模块拼的，路径来自用户选择
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except (OSError, ValueError) as exc:
        raise OSError(f"打不开 {path}：{exc}") from exc
