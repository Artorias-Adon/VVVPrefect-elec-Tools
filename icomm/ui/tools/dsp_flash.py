"""DSP 程序烧写（SCI 串口）。

现场需求：把编好的 DSP 程序（hex / bin）通过 SCI 串口烧进板子。文件有两种来源：

* **现有工程目录**：自动扫描工程根目录（以及软件目录）下的 ``*.hex`` / ``*.bin`` /
  ``*.s19`` …，按修改时间从新到旧列出来；
* **外部导入**：从任何位置选一个文件，记进"最近用过"。

核心逻辑都在 :mod:`icomm.core.flash_file`（解析/校验/找文件）与
:mod:`icomm.core.sci_flash`（协议/传输/模拟设备）里，界面只负责摆和跑。

**为什么要自带实现**：厂商烧写工具要么带版权、要么要注册，不能打包；
所以协议自己实现（参数可调），另外留一个「外部烧写器」入口 ——
用户自己装工具、自己填命令行，软件只负责调用（见 :mod:`icomm.core.flash_external`）。
"""

from __future__ import annotations

import os
import threading
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...core import flash_external, flash_file, sci_flash, serial_ports, shell
from .. import theme
from ..dialogs.sizing import fit_to_available_screen, scrollable_page
from ..spin import no_arrows
from .base import ToolContext, WindowTool, register_tool

_HINT_STYLE = f"color: {theme.TEXT_DIM}; font-size: 11px;"

#: 文件名过滤器（导入外部文件用）
FLASH_FILTER = (
    "烧写文件 (*.hex *.txt *.bin *.s19 *.s28 *.s37 *.mot *.ihex *.a00 *.e00);;"
    "所有文件 (*.*)"
)

#: 常用波特率（C2000 的 SCI 引导常用 9600 / 57600 / 115200）
BAUDRATES = ("9600", "19200", "38400", "57600", "115200", "230400", "460800", "921600")


class _FlashWorker(QObject):
    """把烧写放到后台线程：串口等待可能几秒，界面不能卡住。"""

    progress = Signal(int, int, str)
    finished = Signal(object)  # sci_flash.FlashResult

    def __init__(
        self,
        image: flash_file.FlashImage,
        profile: sci_flash.SciProfile,
        *,
        port: str,
        baudrate: int,
        simulate: bool,
    ) -> None:
        super().__init__()
        self.image = image
        self.profile = profile
        self.port = port
        self.baudrate = baudrate
        self.simulate = simulate
        self.stop = threading.Event()

    def cancel(self) -> None:
        self.stop.set()

    def run(self) -> None:
        transport = None
        try:
            transport = sci_flash.open_transport(
                self.port,
                baudrate=self.baudrate,
                simulate=self.simulate,
                profile=self.profile,
            )
            bootloader = sci_flash.SciBootloader(transport, self.profile)
            result = bootloader.flash(
                self.image,
                progress=lambda index, total, note: self.progress.emit(index, total, note),
                should_stop=self.stop.is_set,
            )
        except Exception as exc:  # noqa: BLE001 - 打开串口/线程异常都要报给用户
            result = sci_flash.FlashResult(
                ok=False, message=f"{exc.__class__.__name__}: {exc}"
            )
        finally:
            if transport is not None:
                transport.close()
        self.finished.emit(result)


class ProfileDialog(QDialog):
    """协议参数：把 ``SciProfile`` 里可调的几项摆出来（不同 bootloader 细节不同）。"""

    def __init__(self, profile: sci_flash.SciProfile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.profile = profile
        self.setWindowTitle(f"协议参数 · {profile.label}")
        self.setMinimumWidth(460)

        root = QVBoxLayout(self)
        hint = QLabel(
            "这些参数要跟实机的 bootloader 对得上。改之前先看设备手册/引导程序说明；"
            "不确定就先用内置档案试，失败会明确告诉你是哪一步没对上。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(_HINT_STYLE)
        root.addWidget(hint)

        form = QFormLayout()
        form.setSpacing(6)
        self.autobaud_edit = QLineEdit(profile.autobaud.hex(" ").upper())
        self.autobaud_edit.setToolTip("例如 41（字符 A）；留空表示不发")
        form.addRow("自举字符", self.autobaud_edit)
        self.echo_edit = QLineEdit(profile.autobaud_echo.hex(" ").upper())
        self.echo_edit.setToolTip("自举后从机应回的字节；留空表示不等")
        form.addRow("自举回显", self.echo_edit)
        self.hs_send_edit = QLineEdit(profile.handshake_send.hex(" ").upper())
        form.addRow("握手发送", self.hs_send_edit)
        self.hs_expect_edit = QLineEdit(profile.handshake_expect.hex(" ").upper())
        form.addRow("握手应答", self.hs_expect_edit)

        self.framing_combo = QComboBox()
        self.framing_combo.addItem("C2000（8 字节包头 + 校验）", "c2000")
        self.framing_combo.addItem("原始数据流（无包头）", "raw")
        index = self.framing_combo.findData(profile.framing)
        self.framing_combo.setCurrentIndex(max(0, index))
        form.addRow("包头", self.framing_combo)

        self.checksum_combo = QComboBox()
        for label, key in (("16 位补码和", "sum16"), ("CRC-16/XMODEM", "crc16"), ("不校验", "none")):
            self.checksum_combo.addItem(label, key)
        index = self.checksum_combo.findData(profile.checksum)
        self.checksum_combo.setCurrentIndex(max(0, index))
        form.addRow("数据校验", self.checksum_combo)

        self.block_combo = QComboBox()
        for words in (32, 64, 128, 256, 512, 1024):
            self.block_combo.addItem(f"{words} 字", words)
        index = self.block_combo.findData(profile.block_words)
        if index < 0:
            self.block_combo.addItem(f"{profile.block_words} 字", profile.block_words)
            index = self.block_combo.count() - 1
        self.block_combo.setCurrentIndex(index)
        form.addRow("每块字数", self.block_combo)

        self.ack_edit = QLineEdit(profile.ack.hex(" ").upper())
        self.ack_edit.setToolTip("每块之后应回的字节；留空表示不等应答")
        form.addRow("块应答", self.ack_edit)
        root.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _bytes(text: str) -> bytes:
        cleaned = text.replace(",", " ").replace("0x", " ").replace("0X", " ")
        tokens = cleaned.split()
        if not tokens:
            return b""
        if len(tokens) == 1 and len(tokens[0]) > 2:
            return bytes.fromhex(tokens[0])
        return bytes(int(token, 16) & 0xFF for token in tokens)

    def result_profile(self) -> sci_flash.SciProfile:
        profile = self.profile
        try:
            profile.autobaud = self._bytes(self.autobaud_edit.text())
            profile.autobaud_echo = self._bytes(self.echo_edit.text())
            profile.handshake_send = self._bytes(self.hs_send_edit.text())
            profile.handshake_expect = self._bytes(self.hs_expect_edit.text())
            profile.ack = self._bytes(self.ack_edit.text())
        except ValueError as exc:
            raise ValueError(f"字节写法不对（用十六进制，如 41 / AA 55）：{exc}") from exc
        profile.framing = str(self.framing_combo.currentData() or "c2000")
        profile.checksum = str(self.checksum_combo.currentData() or "none")
        profile.block_words = int(self.block_combo.currentData() or 128)
        if profile.framing == "raw":
            profile.ack = b""
        return profile


class ExternalFlasherDialog(QDialog):
    """外部烧写器：安装引导 + 填路径/参数 + 试运行（软件不打包第三方工具）。"""

    def __init__(
        self,
        settings: Any,
        parent: QWidget | None = None,
        *,
        file: str = "",
        port: str = "",
        baud: int = 115200,
        profile: str = "",
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self._file = file
        self._port = port
        self._baud = baud
        self._profile = profile
        self.setWindowTitle("外部烧写器（插件方式）")
        self.resize(820, 620)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Horizontal)
        self.guide = QTextBrowser()
        self.guide.setMarkdown(flash_external.install_guide())
        splitter.addWidget(self.guide)

        form_box = QWidget()
        form = QVBoxLayout(form_box)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)

        row = QHBoxLayout()
        row.addWidget(QLabel("工具"))
        self.flasher_combo = QComboBox()
        for flasher in flash_external.KNOWN_FLASHERS:
            self.flasher_combo.addItem(flasher.label, flasher.key)
        index = self.flasher_combo.findData(getattr(settings, "flash_external_key", "custom"))
        self.flasher_combo.setCurrentIndex(max(0, index))
        self.flasher_combo.currentIndexChanged.connect(self._on_flasher_changed)
        row.addWidget(self.flasher_combo, 1)
        form.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("程序"))
        self.exe_edit = QLineEdit(getattr(settings, "flash_external_exe", ""))
        self.exe_edit.setPlaceholderText("外部烧写器的完整路径（.exe / .bat）")
        row.addWidget(self.exe_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        form.addLayout(row)

        row = QHBoxLayout()
        row.addWidget(QLabel("参数"))
        self.args_edit = QLineEdit(
            getattr(settings, "flash_external_args", "")
            or flash_external.KNOWN_FLASHERS[-1].args_template
        )
        self.args_edit.setToolTip("可用占位符：" + "、".join(flash_external.PLACEHOLDERS))
        row.addWidget(self.args_edit, 1)
        form.addLayout(row)

        found = flash_external.detect_installed()
        if found:
            hint = QLabel("在系统里找到：" + "；".join(found[:3]))
            hint.setWordWrap(True)
            hint.setStyleSheet(_HINT_STYLE)
            form.addWidget(hint)
            if not self.exe_edit.text().strip():
                self.exe_edit.setText(found[0])

        self.external_log = QPlainTextEdit()
        self.external_log.setReadOnly(True)
        self.external_log.setPlaceholderText("试运行时，外部工具的输出会贴在这里")
        form.addWidget(self.external_log, 1)
        splitter.addWidget(form_box)
        splitter.setSizes([420, 380])
        root.addWidget(splitter, 1)

        buttons = QDialogButtonBox()
        self.run_button = buttons.addButton("试运行", QDialogButtonBox.ActionRole)
        self.run_button.clicked.connect(self._run_external)
        buttons.addButton("保存", QDialogButtonBox.AcceptRole).clicked.connect(self.accept)
        buttons.addButton("关闭", QDialogButtonBox.RejectRole).clicked.connect(self.reject)
        root.addWidget(buttons)

        self._process = None

    # -------------------------------------------------- 事件

    def _on_flasher_changed(self, _index: int) -> None:
        flasher = flash_external.flasher_by_key(str(self.flasher_combo.currentData()))
        if flasher.args_template:
            self.args_edit.setText(flasher.args_template)

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择外部烧写器", "", "程序 (*.exe *.bat *.cmd);;所有文件 (*.*)"
        )
        if path:
            self.exe_edit.setText(path)

    def save_to(self, settings: Any) -> None:
        settings.flash_external_key = str(self.flasher_combo.currentData() or "custom")
        settings.flash_external_exe = self.exe_edit.text().strip()
        settings.flash_external_args = self.args_edit.text().strip()

    def command(self, file: str, port: str, baud: int, profile: str = "") -> list[str]:
        return flash_external.build_command(
            self.args_edit.text().strip(),
            exe=self.exe_edit.text().strip(),
            file=file,
            port=port,
            baud=baud,
            profile=profile,
        )

    def _run_external(self) -> None:
        from PySide6.QtCore import QProcess

        if self._process is not None and self._process.state() != QProcess.NotRunning:
            self._process.kill()
        exe = self.exe_edit.text().strip()
        if not exe:
            self.external_log.appendPlainText("先填外部烧写器的路径。")
            return
        if not self._file:
            self.external_log.appendPlainText(
                "先在主窗口选一个烧写文件，再来试运行（这样传过去的是真文件）。"
            )
            return
        argv = self.command(self._file, self._port or "COMx", self._baud, self._profile)
        self.external_log.appendPlainText(
            f"文件：{self._file}\n端口：{self._port or '（未选）'} @ {self._baud}"
        )
        self.external_log.appendPlainText("$ " + " ".join(argv))
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.MergedChannels)
        process.readyReadStandardOutput.connect(
            lambda: self.external_log.appendPlainText(
                bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace").rstrip()
            )
        )
        process.finished.connect(
            lambda code, _status: self.external_log.appendPlainText(f"[退出码 {code}]")
        )
        self._process = process
        process.start(argv[0], argv[1:])


class DspFlashWindow(QWidget):
    """DSP 程序烧写主窗口。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.settings = context.settings
        self._files: list[flash_file.FlashFile] = []
        self._images: dict[str, flash_file.FlashImage] = {}
        self._errors: dict[str, str] = {}
        self._image: flash_file.FlashImage | None = None
        self._worker: _FlashWorker | None = None
        self._thread: QThread | None = None
        self.profile = self._load_profile()

        self.setObjectName("dspFlashWindow")
        self.setWindowTitle("DSP 程序烧写（SCI 串口）")
        # 浮动工具窗：必须是独立窗口（带标题栏与关闭按钮）。
        # 少了这一句就只是主窗口里的一个子控件 —— 显示出来没有标题栏，也就关不掉。
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(980, 780)

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_files())
        splitter.addWidget(self._build_flash())
        splitter.addWidget(self._build_log())
        splitter.setSizes([300, 260, 200])
        root.addWidget(splitter, 1)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        external = QPushButton("外部烧写器（插件）…")
        external.setToolTip(
            "内置协议对不上实机 bootloader 时用：先装一个命令行烧写器，再在这里填路径与参数"
        )
        external.clicked.connect(self.open_external_dialog)
        footer.addWidget(external)
        footer.addStretch(1)
        self.hint = QLabel("烧写会覆盖设备 Flash：确认设备已进入 SCI 引导模式、供电稳定。")
        self.hint.setStyleSheet(_HINT_STYLE)
        footer.addWidget(self.hint)
        footer.addSpacing(8)
        # 显式关闭按钮：某些主题/远程桌面下标题栏的 × 不明显，留一个总关得掉的
        close_button = QPushButton("关闭")
        close_button.setToolTip("关闭这个窗口（也可以按 Esc）")
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        root.addLayout(footer)

        # Esc 也能关（和别的工具一致）
        self._close_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._close_shortcut.activated.connect(self.close)

        self.refresh_files(select=self.settings.flash_last_file if self.settings else "")
        self._sync_buttons()

    # -------------------------------------------------- 构造界面

    def _build_files(self) -> QWidget:
        box = QGroupBox("烧写文件")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.find_button = QPushButton("从工程目录查找")
        self.find_button.setToolTip("扫描工程根目录与软件目录下的 hex / bin / s19 等文件")
        self.find_button.clicked.connect(lambda: self.refresh_files())
        row.addWidget(self.find_button)
        self.import_button = QPushButton("导入外部文件…")
        self.import_button.clicked.connect(self.import_file)
        row.addWidget(self.import_button)
        self.open_folder_button = QPushButton("打开所在文件夹")
        self.open_folder_button.clicked.connect(self.open_file_folder)
        row.addWidget(self.open_folder_button)
        row.addStretch(1)
        row.addWidget(QLabel("筛选"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("按文件名 / 路径筛选")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(lambda _text: self._apply_filter())
        row.addWidget(self.filter_edit, 1)
        layout.addLayout(row)

        self.file_table = QTableWidget(0, 5)
        self.file_table.setHorizontalHeaderLabels(["文件", "格式", "大小", "地址范围", "修改时间"])
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.setAlternatingRowColors(True)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.file_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.file_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        header = self.file_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.file_table.itemSelectionChanged.connect(self._on_file_selected)
        self.file_table.doubleClicked.connect(lambda _index: self.open_file_folder())
        layout.addWidget(self.file_table, 1)

        self.preview_label = QLabel("选一个文件看它会被写到哪儿；没选文件时不能烧写。")
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet(_HINT_STYLE)
        layout.addWidget(self.preview_label)
        return box

    def _build_flash(self) -> QWidget:
        box = QGroupBox("SCI 串口与协议")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("串口"))
        self.port_combo = QComboBox()
        self.port_combo.setEditable(True)
        self.port_combo.setMinimumWidth(150)
        self.port_combo.setToolTip("下拉里是系统枚举到的串口；虚拟串口枚举不出来时可以直接手打 COM 号")
        row.addWidget(self.port_combo, 1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh_ports)
        row.addWidget(refresh)

        row.addWidget(QLabel("波特率"))
        self.baud_combo = QComboBox()
        self.baud_combo.setEditable(True)
        self.baud_combo.addItems(BAUDRATES)
        self.baud_combo.setCurrentText(
            str(getattr(self.settings, "flash_baudrate", 115200) or 115200)
            if self.settings is not None
            else "115200"
        )
        row.addWidget(self.baud_combo)

        self.simulate_box = QCheckBox("模拟设备（自检，不用真串口）")
        self.simulate_box.setToolTip("用内置的模拟 bootloader 把整套流程跑一遍，验证文件与参数")
        self.simulate_box.setChecked(
            bool(getattr(self.settings, "flash_simulate", False))
        )
        self.simulate_box.toggled.connect(lambda _checked: self._sync_buttons())
        row.addWidget(self.simulate_box)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("协议"))
        self.profile_combo = QComboBox()
        for profile in sci_flash.builtin_profiles():
            self.profile_combo.addItem(profile.label, profile.key)
        index = self.profile_combo.findData(self.profile.key)
        self.profile_combo.setCurrentIndex(max(0, index))
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        row.addWidget(self.profile_combo, 1)
        params = QPushButton("参数…")
        params.setToolTip("自举字符、握手、包头、校验、块大小都能按实机 bootloader 调")
        params.clicked.connect(self.edit_profile)
        row.addWidget(params)
        layout.addLayout(row)

        self.profile_hint = QLabel(self._profile_text())
        self.profile_hint.setWordWrap(True)
        self.profile_hint.setStyleSheet(_HINT_STYLE)
        layout.addWidget(self.profile_hint)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.start_button = QPushButton("开始烧写")
        self.start_button.setObjectName("primary")
        self.start_button.clicked.connect(self.start_flash)
        row.addWidget(self.start_button)
        self.abort_button = QPushButton("中止")
        self.abort_button.setEnabled(False)
        self.abort_button.clicked.connect(self.abort_flash)
        row.addWidget(self.abort_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        row.addWidget(self.progress, 1)
        self.status_label = QLabel("就绪")
        self.status_label.setMinimumWidth(170)
        self.status_label.setWordWrap(False)
        row.addWidget(self.status_label)
        layout.addLayout(row)
        return box

    def _build_log(self) -> QWidget:
        box = QGroupBox("日志")
        layout = QVBoxLayout(box)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setPlaceholderText("烧写过程会写在这里（含每一步发了什么、设备回了什么）")
        layout.addWidget(self.log_edit, 1)
        return box

    # -------------------------------------------------- 设置

    def _load_profile(self) -> sci_flash.SciProfile:
        raw = getattr(self.settings, "flash_profile", None) if self.settings is not None else None
        if isinstance(raw, dict) and raw.get("key"):
            base = sci_flash.profile_by_key(str(raw["key"]))
            return sci_flash.SciProfile.from_dict(raw, base)
        return sci_flash.profile_by_key("ti-c2000")

    def _save_profile(self) -> None:
        if self.settings is None:
            return
        self.settings.flash_profile = self.profile.to_dict()
        self._save_settings()

    def _save_settings(self) -> None:
        if self.settings is None:
            return
        try:
            self.settings.flash_port = self.current_port()
            self.settings.flash_baudrate = self.current_baudrate()
            self.settings.flash_simulate = self.simulate_box.isChecked()
            callback = getattr(self.context, "save_settings", None)
            if callback is not None:
                callback()
        except Exception as exc:  # noqa: BLE001 - 存不上也不该影响烧写
            self.log(f"设置没能保存：{exc}")

    def _profile_text(self) -> str:
        text = self.profile.describe()
        if self.profile.framing == "raw":
            text += "　·　原始数据流不传地址，设备按接收顺序写入"
        return text

    def _on_profile_changed(self, _index: int) -> None:
        key = str(self.profile_combo.currentData() or "ti-c2000")
        raw = getattr(self.settings, "flash_profile", None) if self.settings is not None else None
        if isinstance(raw, dict) and raw.get("key") == key:
            self.profile = sci_flash.SciProfile.from_dict(raw, sci_flash.profile_by_key(key))
        else:
            self.profile = sci_flash.profile_by_key(key)
        self.profile_hint.setText(self._profile_text())

    def edit_profile(self) -> None:
        dialog = ProfileDialog(self.profile, self)
        fit_to_available_screen(dialog)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            self.profile = dialog.result_profile()
        except ValueError as exc:
            QMessageBox.warning(self, "参数不对", str(exc))
            return
        self.profile_hint.setText(self._profile_text())
        self._save_profile()
        self.log(f"协议参数已更新：{self.profile.describe()}")

    # -------------------------------------------------- 文件

    def search_roots(self) -> list[str]:
        """工程根目录（``reports`` 的上一级）与软件目录。"""
        roots: list[str] = []
        report_dir = getattr(self.context, "report_dir", "") or ""
        if report_dir:
            roots.append(os.path.dirname(os.path.abspath(report_dir)))
        base_dir = getattr(self.context, "base_dir", "") or ""
        if base_dir:
            roots.append(os.path.abspath(base_dir))
        unique: list[str] = []
        for root in roots:
            if root and root not in unique and os.path.isdir(root):
                unique.append(root)
        return unique

    def refresh_files(self, select: str = "") -> None:
        """重新扫描目录；``select`` 指定要选中的文件（绝对路径）。"""
        self._files = flash_file.discover_files(self.search_roots())
        recent = list(getattr(self.settings, "flash_recent_files", []) or [])
        known = {os.path.normcase(entry.path) for entry in self._files}
        for path in recent:
            if not os.path.isfile(path):
                continue
            if os.path.normcase(path) in known:
                continue
            try:
                stat = os.stat(path)
            except OSError:
                continue
            self._files.insert(
                0, flash_file.FlashFile(path=path, size=stat.st_size, mtime=stat.st_mtime)
            )
            known.add(os.path.normcase(path))
        self._fill_table(select)

    def _fill_table(self, select: str = "") -> None:
        self.file_table.blockSignals(True)
        self.file_table.setRowCount(len(self._files))
        for row, entry in enumerate(self._files):
            image = self._images.get(entry.path)
            cells = [
                entry.name,
                image.fmt if image is not None else entry.extension.lstrip(".").upper(),
                entry.size_text(),
                (f"0x{image.start_address:08X}–0x{image.end_address:08X}" if image else "—"),
                entry.time_text(),
            ]
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(entry.path if column == 0 else text)
                if column == 0:
                    item.setData(Qt.UserRole, entry.path)
                if column in (2, 4):
                    item.setTextAlignment(Qt.AlignCenter)
                self.file_table.setItem(row, column, item)
        self.file_table.blockSignals(False)
        self._apply_filter()
        if select:
            self.select_file(select)
        elif self._files:
            self.file_table.selectRow(0)
        else:
            self._image = None
            self.preview_label.setText(
                "这个工程目录里没找到烧写文件。点「导入外部文件…」选一个，"
                "或者把 hex / bin 放进工程目录再点「从工程目录查找」。"
            )
        # selectRow 在"这一行本来就是当前行"时不会发 itemSelectionChanged，
        # 所以这里显式刷一次预览（解析结果有缓存，重复调用很便宜）
        if self.current_file():
            self._on_file_selected()
        self._sync_buttons()

    def _apply_filter(self) -> None:
        needle = self.filter_edit.text().strip().lower()
        for row, entry in enumerate(self._files):
            visible = not needle or needle in entry.path.lower()
            self.file_table.setRowHidden(row, not visible)

    def select_file(self, path: str) -> None:
        wanted = os.path.normcase(os.path.abspath(path))
        for row, entry in enumerate(self._files):
            if os.path.normcase(os.path.abspath(entry.path)) == wanted:
                self.file_table.selectRow(row)
                self._on_file_selected()
                return
        # 不在列表里（例如刚导入的）：补一行再选
        if os.path.isfile(path):
            stat = os.stat(path)
            self._files.insert(0, flash_file.FlashFile(path=path, size=stat.st_size, mtime=stat.st_mtime))
            self._fill_table(select=path)

    def current_file(self) -> str:
        item = self.file_table.item(self.file_table.currentRow(), 0)
        return str(item.data(Qt.UserRole)) if item is not None else ""

    def import_file(self) -> str:
        """从任何位置导入一个烧写文件（记进最近用过）。"""
        path, _filter = QFileDialog.getOpenFileName(
            self, "选择烧写文件", getattr(self.settings, "flash_last_file", "") if self.settings else "",
            FLASH_FILTER,
        )
        if not path:
            return ""
        self._remember_file(path)
        self.select_file(path)
        return path

    def _remember_file(self, path: str) -> None:
        if self.settings is None:
            return
        recent = [item for item in getattr(self.settings, "flash_recent_files", []) if item != path]
        recent.insert(0, path)
        self.settings.flash_recent_files = recent[:10]
        self.settings.flash_last_file = path
        self._save_settings()

    def open_file_folder(self) -> None:
        path = self.current_file()
        if not path:
            return
        try:
            shell.open_path(os.path.dirname(path))
        except OSError as exc:
            QMessageBox.warning(self, "打不开", str(exc))

    def _on_file_selected(self) -> None:
        path = self.current_file()
        self._image = None
        if not path:
            self._sync_buttons()
            return
        try:
            image = self._images.get(path)
            if image is None:
                image = flash_file.parse_file(path)
                self._images[path] = image
                self._errors.pop(path, None)
        except flash_file.FlashError as exc:
            self._errors[path] = str(exc)
            self.preview_label.setText(
                f"<span style='color:{theme.QUALITY_BAD}'>这个文件不能烧：{exc}</span>"
            )
            self.log(f"解析失败：{os.path.basename(path)} —— {exc}")
            self._sync_buttons()
            return
        self._image = image
        self._update_row(path, image)
        lines = image.summary_lines()
        if image.end_address > 0x0080_0000:
            lines.append("警告：结束地址超过 8 MB，确认这个文件是给这颗 DSP 编的")
        self.preview_label.setText("<br>".join(line.replace(" ", "&nbsp;") for line in lines))
        self._sync_buttons()

    def _update_row(self, path: str, image: flash_file.FlashImage) -> None:
        """解析成功后把这一行的"格式 / 地址范围"补上（列表里先看到的是扩展名）。"""
        wanted = os.path.normcase(os.path.abspath(path))
        for row, entry in enumerate(self._files):
            if os.path.normcase(os.path.abspath(entry.path)) != wanted:
                continue
            fmt_item = self.file_table.item(row, 1)
            if fmt_item is not None:
                fmt_item.setText(image.fmt)
            range_item = self.file_table.item(row, 3)
            if range_item is not None:
                range_item.setText(
                    f"0x{image.start_address:08X}–0x{image.end_address:08X}"
                )
            return

    def _sync_buttons(self) -> None:
        running = self._thread is not None
        has_port = bool(self.current_port()) or self.simulate_box.isChecked()
        self.start_button.setEnabled(bool(self._image) and has_port and not running)
        self.abort_button.setEnabled(running)
        self.import_button.setEnabled(not running)
        self.find_button.setEnabled(not running)
        if running:
            return
        if self._image is None:
            current = self.current_file()
            error = self._errors.get(current) if current else ""
            self.status_label.setText("这个文件不能烧（看上面的提示）" if error else "先选一个文件")
        elif not has_port:
            self.status_label.setText("先选串口（或勾模拟设备）")
        else:
            self.status_label.setText("就绪")

    # -------------------------------------------------- 串口

    def refresh_ports(self) -> None:
        current = self.current_port()
        names = serial_ports.port_names()
        self.port_combo.clear()
        self.port_combo.addItems(names)
        if current:
            index = self.port_combo.findText(current)
            if index >= 0:
                self.port_combo.setCurrentIndex(index)
            else:
                self.port_combo.setEditText(current)
        elif names:
            self.port_combo.setCurrentIndex(0)
        self.log(
            f"枚举到 {len(names)} 个串口" + (f"：{'、'.join(names)}" if names else "（可以手打 COM 号）")
        )
        self._sync_buttons()

    def current_port(self) -> str:
        return self.port_combo.currentText().strip().split("　")[0]

    def current_baudrate(self) -> int:
        text = self.baud_combo.currentText().strip()
        try:
            return max(300, min(int(text), 3_000_000))
        except ValueError:
            return 115200

    # -------------------------------------------------- 烧写

    def start_flash(self) -> None:
        image = self._image
        if image is None:
            self.log("没有选中的烧写文件")
            return
        if self._thread is not None:
            return
        simulate = self.simulate_box.isChecked()
        port = self.current_port()
        if not simulate and not port:
            QMessageBox.information(self, "提示", "先选串口，或者勾上「模拟设备（自检）」。")
            return
        if not simulate:
            answer = QMessageBox.question(
                self,
                "确认烧写",
                f"要把「{os.path.basename(image.path)}」写进 {port} 的 DSP 吗？\n\n"
                f"地址：0x{image.start_address:08X}–0x{image.end_address:08X}\n"
                f"{image.byte_count} 字节　{self.profile.label}\n\n"
                "烧写会覆盖设备 Flash，过程中不要断电。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self.log("已取消烧写")
                return
        self._remember_file(image.path)
        self.log_edit.clear()
        self.log(f"文件：{image.path}")
        self.log(f"镜像：{image.describe()}")
        self.log(f"协议：{self.profile.describe()}")
        self.log(f"端口：{'模拟设备（自检）' if simulate else port} @ {self.current_baudrate()}")

        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status_label.setText("烧写中…")
        self._thread = QThread(self)
        self._worker = _FlashWorker(
            image,
            self.profile,
            port=port,
            baudrate=self.current_baudrate(),
            simulate=simulate,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._thread.start()
        self._sync_buttons()
        self._save_settings()

    def _on_progress(self, index: int, total: int, note: str) -> None:
        self.progress.setValue(int(index * 100 / max(1, total)))
        self.status_label.setText(f"烧写中… {index}/{total}　{note}")

    def _on_finished(self, result: sci_flash.FlashResult) -> None:
        self._teardown()
        self.progress.setValue(100 if result.ok else self.progress.value())
        colour = theme.QUALITY_GOOD if result.ok else theme.QUALITY_BAD
        prefix = "烧写成功"
        if result.cancelled:
            colour = theme.QUALITY_UNCERTAIN
            prefix = "已中止"
        elif not result.ok:
            prefix = "烧写失败"
        self.status_label.setText(
            f"<span style='color:{colour}'>{prefix}：{result.message}</span>"
        )
        self.log_edit.appendPlainText(result.as_text())
        self.context.report(f"DSP 烧写：{result.message}", "info" if result.ok else "warning")
        self.context.announce(
            "DSP 程序烧写完成" if result.ok else "DSP 程序烧写失败",
            result.message,
            "info" if result.ok else "warning",
        )

    def abort_flash(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self.status_label.setText("正在中止…")

    def _teardown(self) -> None:
        if self._worker is not None:
            self._worker.stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.quit()
            if thread.wait(6000):
                thread.deleteLater()
            else:
                thread.setParent(None)
                thread.finished.connect(thread.deleteLater)
        self._worker = None
        self._sync_buttons()

    # -------------------------------------------------- 外部烧写器

    def open_external_dialog(self) -> None:
        dialog = ExternalFlasherDialog(
            self.settings,
            self,
            file=self.current_file(),
            port=self.current_port(),
            baud=self.current_baudrate(),
            profile=self.profile.label,
        )
        fit_to_available_screen(dialog)
        if dialog.exec() == QDialog.Accepted:
            dialog.save_to(self.settings)
            self._save_settings()
            self.log(
                "外部烧写器已保存："
                + (getattr(self.settings, "flash_external_exe", "") or "（还没填路径）")
            )

    # -------------------------------------------------- 杂项

    def log(self, text: str) -> None:
        if not text:
            return
        self.log_edit.appendPlainText(text)

    def current_image(self) -> flash_file.FlashImage | None:
        return self._image

    def keyPressEvent(self, event) -> None:  # noqa: item2
        # 窗口没激活时 QShortcut 不会触发，这里兜一层，保证 Esc 总能关
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:  # noqa: item2
        self.abort_flash()
        self._teardown()
        super().closeEvent(event)


@register_tool
class DspFlashTool(WindowTool):
    """「工具 → 工程 → DSP 程序烧写」。"""

    name = "DSP 程序烧写"
    group = "工程"
    order = 25
    description = "SCI 串口把 hex/bin 烧进 DSP；文件可从工程目录找或外部导入，也可接外部烧写器"

    def build(self, context: ToolContext) -> QWidget:
        window = DspFlashWindow(context)
        window.refresh_ports()
        return window
