"""屏幕录制：把整个屏幕或本程序画面录成视频，发给同事复现问题。

现场最常见的需求是"我这边一操作就出错，你那边没见过" —— 录一段比写十句描述管用。
这里不引入 ffmpeg 之类的额外依赖：每帧压成 JPEG，再拼成 MJPEG 编码的 AVI
（Windows 自带播放器、VLC、剪辑软件都能直接打开），编码细节在
:mod:`icomm.core.screenrec`。
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QBuffer, QIODevice, Qt, QTimer
from PySide6.QtGui import QGuiApplication, QImage
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core import screenrec
from .. import theme
from .base import ToolContext, WindowTool, register_tool

#: 帧率选项
FPS_OPTIONS = (("5 帧/秒（最小最省）", 5), ("10 帧/秒（推荐）", 10),
               ("15 帧/秒", 15), ("25 帧/秒（最流畅）", 25))

#: 录制范围
SCOPE_SCREEN = "screen"
SCOPE_APP = "app"

#: 超过这个宽度先缩一下，免得 4K 屏录两分钟就几百兆
MAX_WIDTH = 1600

#: 画质（JPEG）
QUALITY = 80


def jpeg_bytes(image: QImage, quality: int = QUALITY) -> bytes:
    """把一帧编码成 JPEG 字节（Qt 自带编码器，打包里就有 qjpeg.dll）。"""
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, "JPEG", quality):
        raise RuntimeError("JPEG 编码失败：这个版本的 Qt 没带 qjpeg 插件")
    return bytes(buffer.data())


class ScreenRecordWindow(QWidget):
    """屏幕录制窗口。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.setWindowTitle(f"屏幕录制 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(640, 420)

        self._writer: screenrec.AviMjpegWriter | None = None
        self._started_at = 0.0
        self._path = ""
        self._last_error = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)

        info = QLabel(
            "录成 MJPEG 编码的 .avi：Windows 自带播放器、VLC 都能直接打开，"
            "不用装解码器。默认落工程的 <b>reports/video</b> 目录。"
        )
        info.setWordWrap(True)
        info.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(info)

        form_box = QGroupBox("录制设置")
        form = QFormLayout(form_box)
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("整个屏幕（主屏）", SCOPE_SCREEN)
        self.scope_combo.addItem("只录本程序窗口", SCOPE_APP)
        self.scope_combo.setToolTip("只录本程序窗口时，录的是画面内容，被别的窗口挡住也不影响")
        form.addRow("录制范围", self.scope_combo)

        self.fps_combo = QComboBox()
        for label, fps in FPS_OPTIONS:
            self.fps_combo.addItem(label, fps)
        self.fps_combo.setCurrentIndex(1)
        form.addRow("帧率", self.fps_combo)

        self.limit_box = QSpinBox()
        self.limit_box.setRange(1, 60)
        self.limit_box.setValue(10)
        self.limit_box.setSuffix(" 分钟")
        self.limit_box.setToolTip("到点自动停，免得忘了关把硬盘写满")
        form.addRow("最长时长", self.limit_box)

        self.dir_edit = QLineEdit(self.context.report_subdir("video"))
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._pick_directory)
        open_dir = QPushButton("打开目录")
        open_dir.clicked.connect(self._open_directory)
        dir_row = QHBoxLayout()
        dir_row.setSpacing(6)
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)
        dir_row.addWidget(open_dir)
        dir_wrap = QWidget()
        dir_wrap.setLayout(dir_row)
        form.addRow("保存目录", dir_wrap)
        root.addWidget(form_box)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.start_button = QPushButton("● 开始录制")
        self.start_button.setToolTip("开始录屏（会立刻抓第一帧）")
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.start_button)

        self.stop_button = QPushButton("■ 停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(lambda: self.stop("手动停止"))
        buttons.addWidget(self.stop_button)
        buttons.addStretch(1)
        root.addLayout(buttons)

        self.status = QLabel("未录制")
        self.status.setWordWrap(True)
        self.status.setStyleSheet(f"color: {theme.TEXT};")
        root.addWidget(self.status)

        self.hint = QLabel(
            "提示：录屏期间别把窗口关掉（关窗口会自动停止并保存）；"
            "整屏录制只录主屏，多屏请先在显示设置里把要录的那块设为主屏。"
        )
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.hint)
        root.addStretch(1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

        self._refresh_buttons()

    # -------------------------------------------------- 录制

    def recording(self) -> bool:
        return self._writer is not None

    def elapsed(self) -> float:
        return time.time() - self._started_at if self.recording() else 0.0

    def output_path(self) -> str:
        return self._path

    def _target_directory(self) -> str:
        directory = self.dir_edit.text().strip() or self.context.report_subdir("video")
        os.makedirs(directory, exist_ok=True)
        return directory

    def _capture_image(self) -> QImage:
        """抓一帧：整屏或本程序窗口。"""
        scope = self.scope_combo.currentData()
        if scope == SCOPE_APP:
            window = self.context.window
            if window is None:
                return QImage()
            image = window.grab().toImage()
        else:
            screen = QGuiApplication.primaryScreen()
            if screen is None:
                return QImage()
            image = screen.grabWindow(0).toImage()
        if image.isNull():
            return image
        if image.width() > MAX_WIDTH:
            image = image.scaledToWidth(MAX_WIDTH, Qt.SmoothTransformation)
        return image

    def start(self) -> bool:
        """开始录制（抓第一帧顺便定下分辨率）。"""
        if self.recording():
            return False
        directory = self._target_directory()
        name = time.strftime("录制_%Y%m%d_%H%M%S.avi")
        self._path = os.path.join(directory, name)
        image = self._capture_image()
        if image.isNull():
            self._last_error = "抓不到画面（没有可用的屏幕或窗口）"
            self.status.setText(self._last_error)
            return False
        fps = float(self.fps_combo.currentData() or 10)
        try:
            self._writer = screenrec.AviMjpegWriter(
                self._path, image.width(), image.height(), fps
            )
            self._writer.add_frame(jpeg_bytes(image))
        except Exception as exc:  # noqa: BLE001 - 开始失败要给用户明确原因
            self._last_error = str(exc)
            self._writer = None
            self.status.setText(f"开始录制失败：{exc}")
            return False

        self._started_at = time.time()
        self._timer.start(max(20, int(1000 / fps)))
        self._refresh_buttons()
        self._update_status("正在录制")
        return True

    def stop(self, reason: str = "手动停止") -> str:
        """停止并收尾（写完索引），返回文件路径。"""
        if not self.recording():
            return ""
        self._timer.stop()
        writer = self._writer
        self._writer = None
        path = self._path
        frames = 0
        seconds = max(0.0, time.time() - self._started_at)
        try:
            frames = writer.frames if writer is not None else 0
            if writer is not None:
                path = writer.close()
        except Exception as exc:  # noqa: BLE001 - 收尾失败也要把状态说清楚
            self._last_error = str(exc)
            self.status.setText(f"收尾失败：{exc}")
            self._refresh_buttons()
            return ""
        self._refresh_buttons()
        size_mb = os.path.getsize(path) / 1024 / 1024 if os.path.exists(path) else 0.0
        summary = f"{os.path.basename(path)}　{seconds:.1f} 秒 / {frames} 帧 / {size_mb:.1f} MB"
        self._update_status(f"已保存（{reason}）：{summary}")
        try:
            self.context.announce("录制完成", summary, "info")
        except Exception:  # noqa: BLE001 - 通知失败不能影响收尾
            pass
        return path

    def _on_tick(self) -> None:
        if not self.recording():
            return
        image = self._capture_image()
        if image.isNull():
            self._update_status("这一帧抓空了（画面暂时不可用），继续录")
            return
        writer = self._writer
        if writer is None:
            return
        if (image.width(), image.height()) != (writer.width, writer.height):
            image = image.scaled(writer.width, writer.height, Qt.IgnoreAspectRatio,
                                 Qt.SmoothTransformation)
        try:
            writer.add_frame(jpeg_bytes(image))
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            self.stop(f"编码失败：{exc}")
            return
        self._update_status("正在录制")

        limit = self.limit_box.value() * 60
        if limit and self.elapsed() >= limit:
            self.stop(f"达到最长时长 {self.limit_box.value()} 分钟")

    # -------------------------------------------------- 界面

    def _update_status(self, prefix: str) -> None:
        if not self.recording():
            self.status.setText(prefix)
            return
        writer = self._writer
        frames = writer.frames if writer is not None else 0
        seconds = self.elapsed()
        size_mb = (writer.frame_bytes if writer is not None else 0) / 1024 / 1024
        self.status.setText(
            f"{prefix}：{os.path.basename(self._path)}　{seconds:.1f} 秒 / "
            f"{frames} 帧 / 约 {size_mb:.1f} MB"
        )

    def _refresh_buttons(self) -> None:
        busy = self.recording()
        self.start_button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        for widget in (self.scope_combo, self.fps_combo, self.limit_box, self.dir_edit):
            widget.setEnabled(not busy)

    def _pick_directory(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择录像保存目录", self.dir_edit.text().strip()
        )
        if chosen:
            self.dir_edit.setText(os.path.normpath(chosen))

    def _open_directory(self) -> None:
        target = self.dir_edit.text().strip()
        if not target or not os.path.isdir(target):
            QMessageBox.information(self, "提示", "这个目录还不存在，先录一段就会自动建好。")
            return
        try:
            os.startfile(target)  # noqa: S606 - 用户主动点的「打开目录」
        except (AttributeError, OSError) as exc:
            QMessageBox.warning(self, "打不开", str(exc))

    def closeEvent(self, event) -> None:  # noqa: item2
        if self.recording():
            self.stop("关闭窗口")
        super().closeEvent(event)


@register_tool
class ScreenRecordTool(WindowTool):
    """菜单里的「屏幕录制」，紧跟在「截屏辅助」后面。"""

    name = "屏幕录制"
    group = "自动化"
    order = 15
    description = "把整个屏幕或本程序画面录成视频（MJPEG AVI），复现问题时发给同事"

    def build(self, context: ToolContext) -> QWidget:
        return ScreenRecordWindow(context)
