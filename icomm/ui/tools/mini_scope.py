"""示波器小窗口模式：只观察用的浮动小窗。

为什么需要它：现场调试经常要"一边改参数一边看波形"，主窗口切成示波器页就看不
到监视页了。这个小窗把画布单独拎出来，浮在其它窗口上面，随时瞄一眼。

当前定位是**纯观察**：不带触发、时基、光标这些控件（那些在主窗口里用），
只显示选好的通道 + 一个极简的缩放/暂停。以后要扩展（例如把通道选择、触发
也搬进来，或者做成常驻置顶的"看板"）就在这个类上加 —— 它复用的是同一个
:class:`~icomm.ui.scope_canvas.ScopeCanvas`，加功能不用重写绘制。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.models import Project, point_id
from .. import theme
from ..scope_canvas import ScopeCanvas, ScopeChannel
from .base import ToolContext, WindowTool, register_tool

#: 刷新频率（毫秒）
REFRESH_MS = 120


class MiniScopeWindow(QWidget):
    """只观察用的浮动示波器。"""

    #: 用户在小窗里点了「在主窗口打开」
    openMainRequested = Signal(str)

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self._channels: dict[str, ScopeChannel] = {}
        self._paused = False

        self.setWindowTitle(f"小窗示波器 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint)
        self.resize(560, 340)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(6)

        root.addLayout(self._build_bar())

        self.canvas = ScopeCanvas()
        # 小窗只看，不响应双击等交互
        self.canvas.setMinimumHeight(200)
        root.addWidget(self.canvas, 1)

        self.hint = QLabel("")
        self.hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.hint)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.reload_channels()

    # -------------------------------------------------- 界面

    def _build_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self.channel_combo = QComboBox()
        self.channel_combo.setMinimumWidth(180)
        self.channel_combo.currentIndexChanged.connect(self._on_channel_changed)
        bar.addWidget(self.channel_combo)

        add = QPushButton("显示")
        add.clicked.connect(self._add_current)
        bar.addWidget(add)

        self.clear_button = QPushButton("清空")
        self.clear_button.clicked.connect(self.clear_channels)
        bar.addWidget(self.clear_button)

        bar.addStretch(1)

        bar.addWidget(QLabel("时基"))
        self.timebase_combo = QComboBox()
        for label, seconds in (
            ("1 秒/格", 1.0), ("2 秒/格", 2.0), ("5 秒/格", 5.0),
            ("10 秒/格", 10.0), ("30 秒/格", 30.0),
        ):
            self.timebase_combo.addItem(label, seconds)
        index = self.timebase_combo.findData(5.0)
        self.timebase_combo.setCurrentIndex(index if index >= 0 else 2)
        self.timebase_combo.currentIndexChanged.connect(
            lambda: self.canvas.set_timebase(float(self.timebase_combo.currentData()))
        )
        bar.addWidget(self.timebase_combo)

        self.pause_box = QCheckBox("暂停")
        bar.addWidget(self.pause_box)

        self.top_box = QCheckBox("置顶")
        self.top_box.setChecked(True)
        self.top_box.toggled.connect(self._on_top_toggled)
        bar.addWidget(self.top_box)
        return bar

    def _on_top_toggled(self, checked: bool) -> None:
        """切置顶：工作台上常开着好几个窗口，置顶要能随时关掉。"""
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()  # setWindowFlags 之后必须重新 show，否则窗口会消失

    # -------------------------------------------------- 通道

    def reload_channels(self) -> None:
        """把工程里可画的点位填进下拉框；默认勾上参数表里标过"示波器=是"的。

        "显示全部点位"跟着软件设置走 —— 主窗口示波器怎么列，小窗就怎么列，
        这样右键「加到示波器小窗」对任何点位都有效（要求：全部通道都能加）。
        """
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        self._channels.clear()
        show_all = bool(getattr(self.context.settings, "scope_show_all_points", False))
        entries = (
            [(device, point) for device, point in self.context.project.iter_points()
             if device.enabled]
            if show_all
            else self.context.project.scope_points()
        )
        if not entries:
            # 一个点位都没标「示波器」时兜底列全部 —— 现场多数设备只有有效值，
            # 没有波形缓冲区，列表空着会让人以为小窗坏了
            entries = [
                (device, point)
                for device, point in self.context.project.iter_points()
                if device.enabled
            ]
        for index, (device, point) in enumerate(entries):
            channel = self._make_channel(device, point, index)
            self._channels[channel.pid] = channel
            self.channel_combo.addItem(channel.name, channel.pid)
        self.channel_combo.blockSignals(False)
        self.canvas.set_channels(list(self._channels.values()))
        self.canvas.set_timebase(float(self.timebase_combo.currentData()))
        self._refresh_hint()

    def _make_channel(self, device, point, index: int) -> ScopeChannel:
        return ScopeChannel(
            pid=point_id(device.name, point.name),
            name=f"{device.name}.{point.title}",
            unit=point.unit,
            color=point.scope_color or theme.series_color(index),
            visible=bool(point.scope or point.is_array),
            kind="array" if point.is_array else "scalar",
            decimals=point.decimals,
            sample_interval_us=point.sample_interval_us,
        )

    def _refresh_hint(self) -> None:
        visible = [c for c in self._channels.values() if c.visible]
        if visible:
            kinds = {c.is_array for c in visible}
            mode = "整段波形" if kinds == {True} else (
                "瞬时值趋势（每个采集周期一个点）" if kinds == {False} else "波形 + 瞬时值"
            )
            self.hint.setText(f"{mode}　｜　显示：" + "、".join(c.name for c in visible))
        elif not self._channels:
            self.hint.setText("工程里还没有点位")
        else:
            self.hint.setText("还没有显示任何通道 —— 选一个通道点「显示」")

    def _on_channel_changed(self) -> None:
        pass  # 下拉框只用来挑，点了「显示」才加

    def _add_current(self) -> None:
        pid = self.channel_combo.currentData()
        if pid and pid in self._channels:
            self._channels[pid].visible = True
            self.canvas.update()
            self._refresh_hint()

    def show_point(self, pid: str) -> bool:
        """外部（右键「在小窗里看」）指定要显示的点位。

        列表里没有就现场补一个 —— 参数表没标"示波器"的点位也要能直接看，
        否则"全部通道都能加到示波器"这条对小窗就不成立。
        """
        channel = self._channels.get(pid)
        if channel is None:
            found = self.context.project.find_point(pid)
            if found is None:
                return False
            device, point = found
            channel = self._make_channel(device, point, len(self._channels))
            self._channels[pid] = channel
            self.channel_combo.blockSignals(True)
            self.channel_combo.addItem(channel.name, pid)
            self.channel_combo.blockSignals(False)
            self.canvas.set_channels(list(self._channels.values()))
        channel.visible = True
        self.canvas.update()
        index = self.channel_combo.findData(pid)
        if index >= 0:
            self.channel_combo.setCurrentIndex(index)
        self._refresh_hint()
        return True

    def clear_channels(self) -> None:
        for channel in self._channels.values():
            channel.visible = False
        self.canvas.update()
        self._refresh_hint()

    # -------------------------------------------------- 数据

    def refresh(self) -> None:
        if self.pause_box.isChecked():
            return
        visible = [c for c in self._channels.values() if c.visible]
        if not visible:
            return
        import time as _time

        changed = False
        for channel in visible:
            tag = self.context.tag_of(channel.pid)
            if tag.value is None:
                continue
            # 数组点位给整段波形，标量点位按时间戳追加一个点 ——
            # 与主窗口示波器走同一套画布接口，行为保持一致
            if channel.is_array and isinstance(tag.value, (list, tuple)):
                self.canvas.set_waveform(
                    channel.pid, [float(v) for v in tag.value], channel.sample_interval_us
                )
                changed = True
            elif not channel.is_array and not isinstance(tag.value, (list, tuple)):
                try:
                    self.canvas.append_scalar(
                        channel.pid, tag.timestamp or _time.time(), float(tag.value)
                    )
                    changed = True
                except (TypeError, ValueError):
                    continue
        if changed:
            self.canvas.update()

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        super().closeEvent(event)


@register_tool
class MiniScopeTool(WindowTool):
    name = "示波器小窗口"
    group = "观察"
    order = 10
    description = "只观察用的浮动小窗，一边改参数一边看波形"

    def build(self, context: ToolContext) -> QWidget:
        return MiniScopeWindow(context)
