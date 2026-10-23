"""外部烧写器（插件）：只在"内置 SCI 协议对不上实机 bootloader"时用。

为什么不做成一个"自带厂商工具"的按钮
--------------------------------------
厂商的烧写工具要么带版权、要么要注册，**不能打包进本软件**。所以这里的做法是：

1. 软件自己实现一套通用的 SCI 引导流程（:mod:`icomm.core.sci_flash`），
   能对上就不用装任何东西；
2. 实在对不上时，允许用户**自己**装一个命令行烧写器，然后在这里填它的路径和参数，
   软件只负责"按模板拼命令行 + 起进程 + 把输出贴回来"；
3. 附一份安装引导（:func:`install_guide`），写清去哪装、怎么填。

拼命令行的占位符：

==================  ==============================================
``{exe}``           外部程序路径
``{file}``          烧写文件（绝对路径）
``{port}``          串口号，例如 ``COM3``
``{baud}``          波特率
``{profile}``       当前使用的协议档案名（仅作说明）
==================  ==============================================

`:class:`ExternalRunner`` 用 ``subprocess`` 流式读输出，支持中止；界面用
``QTimer`` 轮询或直接在工作线程里跑都可以（这里给了 ``run()`` 阻塞版）。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable

#: 用户在"外部烧写器"里能用的占位符
PLACEHOLDERS: dict[str, str] = {
    "{exe}": "外部程序路径",
    "{file}": "烧写文件（绝对路径）",
    "{port}": "串口号（如 COM3）",
    "{baud}": "波特率",
    "{profile}": "协议档案名（只作说明）",
}


@dataclass(frozen=True)
class ExternalFlasher:
    """一个"已知"的外部烧写器（只给安装引导与建议参数，不打包）。"""

    key: str
    label: str
    description: str
    install_url: str
    #: 常见安装位置（只用来"找一找"，找不到就让用户手填）
    detect_names: tuple[str, ...] = ()
    #: 建议的参数模板（具体选项请以该工具当前版本的文档为准）
    args_template: str = ""
    notes: tuple[str, ...] = ()


#: 只列"命令行可用、有公开文档"的常见工具；参数模板给的是**骨架**，
#: 版本不同选项会变，所以界面上始终可改，并且不预填任何未经验证的开关。
KNOWN_FLASHERS: tuple[ExternalFlasher, ...] = (
    ExternalFlasher(
        key="ti-uniflash",
        label="TI UniFlash（dslite 命令行）",
        description="TI 官方的 C2000/MSP430 等器件烧写工具，支持命令行。",
        install_url="https://www.ti.com/tool/UNIFLASH",
        detect_names=("dslite", "dslite.bat", "dslite.exe"),
        args_template="{exe} -f {file}",
        notes=(
            "安装后在安装目录里找 dslite.bat / dslite.exe，把完整路径填到「外部程序」。",
            "不同版本/器件的命令行参数不一样，请对照 UniFlash 的 CLI 文档补全参数。",
            "本软件不打包、不修改该工具，只按你填的模板调用。",
        ),
    ),
    ExternalFlasher(
        key="custom",
        label="自定义命令行烧写器",
        description="任何能把「文件 + 串口」交给它烧写的命令行程序（公司内部工具也可以）。",
        install_url="",
        args_template="{exe} --port {port} --baud {baud} --file {file}",
        notes=(
            "参数里的占位符见上表；程序退出码 0 视为成功。",
            "工具自己的输出会原样贴到下方日志里，方便对厂商文档排错。",
        ),
    ),
)


def flasher_by_key(key: str) -> ExternalFlasher:
    for flasher in KNOWN_FLASHERS:
        if flasher.key == key:
            return flasher
    return KNOWN_FLASHERS[-1]


def install_guide() -> str:
    """安装引导（界面里用 QTextBrowser 显示）。"""
    lines = [
        "内置的 SCI 烧写已经能覆盖常见的 C2000 引导流程：**先试内置的**，",
        "参数对不上再改协议参数（自举字符 / 握手 / 包头 / 校验都能调）。",
        "",
        "只有实机 bootloader 与内置两种档案都对不上时，才需要外部烧写器：",
        "",
        "1. 装一个**命令行**烧写工具（厂商工具或公司内部工具都行）；",
        "2. 打开「外部烧写器」页，把程序路径填进去；",
        "3. 按它的文档填参数模板，占位符：",
    ]
    lines.extend(f"   - {name}：{desc}" for name, desc in PLACEHOLDERS.items())
    lines.append("4. 点「试运行」看输出；退出码 0 才算成功。")
    lines.append("")
    lines.append("已知工具：")
    for flasher in KNOWN_FLASHERS:
        if flasher.install_url:
            lines.append(f"- **{flasher.label}** — {flasher.description} 安装页：{flasher.install_url}")
        else:
            lines.append(f"- **{flasher.label}** — {flasher.description}")
        for note in flasher.notes:
            lines.append(f"  - {note}")
    lines.append("")
    lines.append(
        "合规说明：本软件**不附带、不分发**任何第三方烧写工具，也不修改它们；"
        "这里只保存你填的路径与参数，并在你点「运行」时按模板启动它。"
    )
    return "\n".join(lines)


def detect_installed() -> list[str]:
    """在 PATH 与常见目录里找一找已知工具（找不到是正常的）。"""
    found: list[str] = []
    search_dirs = [os.environ.get("PATH", "")]
    for flasher in KNOWN_FLASHERS:
        for name in flasher.detect_names:
            for folder in search_dirs:
                for entry in folder.split(os.pathsep):
                    if not entry:
                        continue
                    candidate = os.path.join(entry, name)
                    if os.path.isfile(candidate):
                        found.append(candidate)
                        break
    # 去重并保序
    unique: list[str] = []
    for path in found:
        if path not in unique:
            unique.append(path)
    return unique


def build_command(template: str, *, exe: str, file: str, port: str, baud: int,
                  profile: str = "") -> list[str]:
    """把模板拼成 argv。模板为空时直接跑 ``exe`` 本身。"""
    text = (template or "").strip()
    values = {
        "{exe}": exe,
        "{file}": os.path.abspath(file) if file else "",
        "{port}": port,
        "{baud}": str(baud),
        "{profile}": profile,
    }
    if not text:
        return [exe]
    for name, value in values.items():
        # 用引号包住，路径里有空格也不会散架
        text = text.replace(name, f'"{value}"')
    try:
        argv = shlex.split(text, posix=False)
    except ValueError:
        argv = text.split()
    # shlex 在 Windows 下会保留引号，去掉首尾引号
    argv = [part[1:-1] if len(part) >= 2 and part[0] == part[-1] == '"' else part for part in argv]
    if not argv:
        return [exe]
    return argv


@dataclass
class ExternalRunResult:
    ok: bool
    message: str
    exit_code: int = -1
    lines: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        body = "\n".join(self.lines) if self.lines else "（没有输出）"
        return f"结果：{'成功' if self.ok else '失败'}\n{self.message}\n\n{body}\n"


class ExternalRunner:
    """跑外部烧写器：流式读输出、可中止、结束给出退出码。"""

    def __init__(
        self,
        argv: Iterable[str],
        *,
        cwd: str = "",
        timeout_s: float = 600.0,
        on_line: Callable[[str], None] | None = None,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd or None
        self.timeout_s = timeout_s
        self.on_line = on_line
        self.cancelled = False
        self._process: subprocess.Popen | None = None
        self._lock = threading.Lock()

    def cancel(self) -> None:
        self.cancelled = True
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    def run(self) -> ExternalRunResult:
        lines: list[str] = []
        if not self.argv:
            return ExternalRunResult(False, "没有可执行的命令（先填程序路径）", lines=lines)
        exe = self.argv[0]
        if not os.path.isfile(exe):
            return ExternalRunResult(
                False, f"找不到外部程序：{exe}\n先安装，再把完整路径填进来。", lines=lines
            )
        try:
            process = subprocess.Popen(
                self.argv,
                cwd=self.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            return ExternalRunResult(False, f"启动失败：{exc}", lines=lines)
        with self._lock:
            self._process = process

        def read_output() -> None:
            assert process.stdout is not None
            try:
                for line in process.stdout:
                    text = line.rstrip("\r\n")
                    lines.append(text)
                    if self.on_line is not None:
                        self.on_line(text)
            finally:
                process.stdout.close()

        reader = threading.Thread(target=read_output, name="flash-external-out", daemon=True)
        reader.start()
        try:
            exit_code = process.wait(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            self.cancel()
            reader.join(timeout=3)
            return ExternalRunResult(
                False, f"超过 {self.timeout_s:g} 秒还没结束，已中止", -1, lines
            )
        reader.join(timeout=3)
        if self.cancelled:
            return ExternalRunResult(False, "已中止", exit_code, lines)
        ok = exit_code == 0
        return ExternalRunResult(
            ok,
            f"退出码 {exit_code}" + ("（成功）" if ok else "（失败：看上面的输出）"),
            exit_code,
            lines,
        )


__all__ = [
    "KNOWN_FLASHERS",
    "PLACEHOLDERS",
    "ExternalFlasher",
    "ExternalRunResult",
    "ExternalRunner",
    "build_command",
    "detect_installed",
    "flasher_by_key",
    "install_guide",
]
