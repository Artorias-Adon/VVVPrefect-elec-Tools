"""小窗口按键控制面板。

现场常见的用法：一边盯着趋势/波形，一边按按键启停设备。
模态对话框要"关掉才能继续看"，很碍事，所以这里做成**非模态浮动窗口**，
可以同时开好几个（一台设备的、单点位的），点完不消失。

两种用法：

* :class:`DeviceControlPanel` —— 一台设备的控制台：自动收集该设备上所有
  **带命令/位定义的可写点位**（例如整流 DSP 的控制字），做成按键组。
  从设备详情页、示意图节点或右键菜单打开。
* :class:`PointControlPanel` —— 单个点位的按键窗口：用于参数表里
  只定义了命令或位定义的那一个点位。

按键来源全部来自参数表（``命令`` 列与 ``位定义`` 列），所以在表里改按钮、
加按钮，这里自动就变，不用改代码。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.codec import format_value
from ...core.models import DeviceDef, Project, point_id
from ...core.tags import TagValue
from .. import theme

#: 每行放几个按键
KEYS_PER_ROW = 3

#: 面板刷新间隔（毫秒）
REFRESH_MS = 500


class _BasePanel(QFrame):
    """浮动控制面板的公共部分：非模态、置顶、可多个同时存在。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("controlPanel")
        # 浮动工具窗：独立于主窗口，不会盖住主窗口的其它部分，可同时开多个
        self.setWindowFlags(
            Qt.Tool | Qt.WindowStaysOnTopHint | Qt.WindowCloseButtonHint
        )
        self.setWindowTitle(title)
        self.setFrameShape(QFrame.NoFrame)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)

    #: 写入请求 / 持续发送请求，由主窗口接走（与点位对话框同一套出口）
    writeRequested = Signal(str, object)
    periodicRequested = Signal(str, object, int)
    periodicStopRequested = Signal(str)
    dialogRequested = Signal(str)

    def value_of(self, pid: str) -> TagValue:
        """取实时值。子类必须提供（由外部注入取值函数）。"""
        raise NotImplementedError

    def periodic_of(self, pid: str):
        """取正在运行的持续发送任务，没有则 None。"""
        return None

    def engine_running(self) -> bool:
        return False

    def refresh(self) -> None:  # 子类按需覆写
        pass

    def closeEvent(self, event) -> None:  # noqa: item2
        self._timer.stop()
        super().closeEvent(event)

    def stop_timer(self) -> None:
        """安全地停掉刷新定时器（对象可能已被 Qt 释放）。"""
        try:
            self._timer.stop()
        except RuntimeError:
            pass  # 底层 C++ 对象已销毁

    # -------------------------------------------------- 小工具

    def _quality_text(self, tag: TagValue) -> tuple[str, str]:
        text = format_value(tag.value, 2) if tag.value is not None else "--"
        colour = theme.TEXT if tag.quality == "good" else theme.QUALITY_BAD
        if tag.quality != "good":
            text = tag.error or "通信异常"
        return text, colour

    def _make_key(self, label: str, tooltip: str, slot) -> QPushButton:
        button = QPushButton(label)
        button.setObjectName("controlKey")
        button.setMinimumHeight(34)
        button.setToolTip(tooltip)
        button.clicked.connect(slot)
        return button


class PointControlPanel(_BasePanel):
    """单个点位的按键控制窗口。"""

    def __init__(
        self,
        project: Project,
        pid: str,
        value_of,
        periodic_of,
        engine_running=lambda: False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(f"控制 · {pid}", parent)
        self.project = project
        self.pid = pid
        self._value_of = value_of
        self._periodic_of = periodic_of
        self._engine_running = engine_running
        self._bit_boxes: dict[int, QCheckBox] = {}

        found = project.find_point(pid)
        if found is None:
            raise ValueError(f"工程里没有点位 {pid}")
        self._device, self._point = found

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        head = QHBoxLayout()
        title = QLabel(self._point.title)
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 14px; font-weight: 600;")
        head.addWidget(title)
        head.addStretch(1)
        self.value_label = QLabel("--")
        head.addWidget(self.value_label)
        root.addLayout(head)

        meta = QLabel(f"{pid}　·　{self._point.register} #{self._point.address}")
        meta.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(meta)

        commands = self._point.command_items()
        bits = self._point.bit_labels()

        if commands:
            root.addWidget(self._build_commands(commands))
        elif bits and self._point.writable:
            root.addWidget(self._build_bit_keys(bits))
        elif self._point.writable:
            root.addWidget(self._build_value_keys())
        else:
            note = QLabel("该点位是只读遥测，没有可控按键。")
            note.setStyleSheet(f"color: {theme.TEXT_DIM};")
            root.addWidget(note)

        if bits:
            root.addWidget(self._build_bit_readout(bits))

        footer = QHBoxLayout()
        footer.setSpacing(6)
        self.periodic_label = QLabel("")
        self.periodic_label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        footer.addWidget(self.periodic_label, 1)
        stop = QPushButton("停止发送")
        stop.setObjectName("danger")
        stop.clicked.connect(lambda: self.periodicStopRequested.emit(self.pid))
        self.stop_button = stop
        footer.addWidget(stop)
        more = QPushButton("更多…")
        more.setToolTip("打开完整参数对话框（改值、持续发送、编辑定义）")
        more.clicked.connect(lambda: self.dialogRequested.emit(self.pid))
        footer.addWidget(more)
        root.addLayout(footer)

        self.refresh()

    # -------------------------------------------------- 区块

    def _build_commands(self, commands: list[tuple[int, str]]) -> QWidget:
        box = QWidget()
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        for index, (value, label) in enumerate(commands):
            key = self._make_key(
                label,
                f"{label}　写入 {value}（0x{value:X}）",
                lambda _checked=False, v=value: self._send(v),
            )
            grid.addWidget(key, index // KEYS_PER_ROW, index % KEYS_PER_ROW)
        for column in range(KEYS_PER_ROW):
            grid.setColumnStretch(column, 1)
        return box

    def _build_bit_keys(self, bits: list[tuple[int, str]]) -> QWidget:
        """位掩码点位：每个位一个开关按键，直接写该位的开/关。"""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        hint = QLabel("按位控制（点一下直接写该位）")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(6)
        for index, (bit, label) in enumerate(bits):
            key = self._make_key(
                label,
                f"bit{bit} {label}　点一下置位并写入",
                lambda _checked=False, b=bit: self._toggle_bit(b),
            )
            grid.addWidget(key, index // KEYS_PER_ROW, index % KEYS_PER_ROW)
        for column in range(KEYS_PER_ROW):
            grid.setColumnStretch(column, 1)
        layout.addLayout(grid)
        return box

    def _build_value_keys(self) -> QWidget:
        """普通可写点位：给"取当前值 ± 步长"这类常用操作，避免面板里塞键盘。"""
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        hint = QLabel("常用操作")
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(hint)

        grid = QGridLayout()
        grid.setSpacing(6)
        step = self._step()
        for index, delta in enumerate((-10 * step, -step, step, 10 * step)):
            text = f"{'+' if delta > 0 else ''}{_pretty(delta)}"
            key = self._make_key(
                text,
                f"在当前值基础上 {text} 并写入",
                lambda _checked=False, d=delta: self._nudge(d),
            )
            grid.addWidget(key, index // 4, index % 4)
            grid.setColumnStretch(index % 4, 1)
        layout.addLayout(grid)
        return box

    def _build_bit_readout(self, bits: list[tuple[int, str]]) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        caption = QLabel("位状态")
        caption.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(caption)
        self.bit_label = QLabel("--")
        self.bit_label.setWordWrap(True)
        self.bit_label.setTextFormat(Qt.RichText)
        layout.addWidget(self.bit_label)
        return box

    # -------------------------------------------------- 动作

    def _step(self) -> float:
        point = self._point
        if point.data_type.startswith(("int", "uint")):
            return 1.0
        return max(0.01, 10 ** -max(0, point.decimals))

    def _send(self, value) -> None:
        self.writeRequested.emit(self.pid, value)

    def _toggle_bit(self, bit: int) -> None:
        """把某一位翻过来再写下去（其它位保持当前值不变）。"""
        tag = self._value_of(self.pid)
        try:
            current = int(tag.value or 0)
        except (TypeError, ValueError):
            current = 0
        self._send(current ^ (1 << bit))

    def _nudge(self, delta: float) -> None:
        tag = self._value_of(self.pid)
        try:
            current = float(tag.value or 0.0)
        except (TypeError, ValueError):
            current = 0.0
        value = current + delta
        if self._point.data_type.startswith(("int", "uint")):
            value = int(round(value))
        self._send(value)

    # -------------------------------------------------- 刷新

    def value_of(self, pid: str) -> TagValue:  # type: ignore[override]
        return self._value_of(pid)

    def periodic_of(self, pid: str):  # type: ignore[override]
        return self._periodic_of(pid)

    def engine_running(self) -> bool:  # type: ignore[override]
        return bool(self._engine_running())

    def refresh(self) -> None:
        tag = self._value_of(self.pid)
        text, colour = self._quality_text(tag)
        unit = f" {self._point.unit}" if self._point.unit else ""
        if tag.quality == "good":
            text = f"{format_value(tag.value, self._point.decimals)}{unit}"
        self.value_label.setText(text)
        self.value_label.setStyleSheet(f"color: {colour}; font-size: 14px; font-weight: 600;")

        bits = self._point.bit_labels()
        if bits and hasattr(self, "bit_label"):
            active = set(self._point.decode_bits(tag.value, bits))
            self.bit_label.setText(
                "　".join(
                    f"<span style='color:{theme.ACCENT if label in active else theme.BORDER}'>"
                    f"bit{bit} {label}</span>"
                    for bit, label in bits
                )
            )

        task = self._periodic_of(self.pid)
        if task is None:
            self.periodic_label.setText("未持续发送")
            self.stop_button.setEnabled(False)
        else:
            state = "发送中" if task.enabled else "已暂停"
            self.periodic_label.setText(
                f"{state}　每 {task.interval_ms} ms 写 {task.value}　已发 {task.sent} 次"
            )
            self.stop_button.setEnabled(True)


class DeviceControlPanel(_BasePanel):
    """一台设备的控制台。

    **默认只列出"命令点位"**（参数表里填了「命令」列的，例如控制字的
    停机/充电/启动/复位）—— 那些才是真正"按一下就让设备动作"的按键。

    带位定义的可写点位（例如 27 位的保护使能掩码）属于"配置"，一次展开就是
    上百个键，面板会没法用，所以默认不列；需要时用面板上的「＋ 添加点位」
    自己挑，选过的记在软件设置里，下次自动带上。

    一台设备一个「命令」点位都没有时，退化成列出前几个可写位域点位，
    免得面板空白。
    """

    #: 退化模式下最多列出几个位域点位
    FALLBACK_LIMIT = 3

    def __init__(
        self,
        project: Project,
        device_name: str,
        value_of,
        periodic_of,
        engine_running=lambda: False,
        extra_pids: list[str] | None = None,
        on_pick_points=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(f"控制台 · {device_name}", parent)
        self.project = project
        self.device_name = device_name
        self._value_of = value_of
        self._periodic_of = periodic_of
        self._engine_running = engine_running
        self._extra_pids = list(extra_pids or [])
        self._on_pick_points = on_pick_points
        self._value_labels: dict[str, QLabel] = {}

        device = project.device(device_name)
        if device is None:
            raise ValueError(f"工程里没有设备 {device_name}")
        self._device = device

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel(f"{device.name}　控制台")
        title.setStyleSheet(f"color: {theme.TEXT}; font-size: 15px; font-weight: 600;")
        head.addWidget(title)
        head.addStretch(1)
        pick = QPushButton("＋ 添加点位")
        pick.setToolTip(
            "选择还要在这台控制台上显示哪些点位\n"
            "（参数表里给点位填「命令」列，它就会默认出现在这里）"
        )
        pick.clicked.connect(self._pick_points)
        head.addWidget(pick)
        root.addLayout(head)

        channel = project.channel(device.channel)
        meta = QLabel(
            f"通道 {device.channel}（{channel.link if channel else '-'}）　·　"
            f"从站号 {device.unit_id}　·　共 {len(device.points)} 个点位"
        )
        meta.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(meta)

        keys = device_keys(device, self._extra_pids)
        if keys:
            root.addWidget(self._build_keys(keys))
        else:
            note = QLabel(
                "这台设备没有可按键控制的点位。\n\n"
                "在参数表里给点位填上「命令」列（如 0x20:启动,0x04:停机）或\n"
                "「位定义」列（可写点位），它们就会出现在这里。"
            )
            note.setStyleSheet(f"color: {theme.TEXT_DIM};")
            note.setWordWrap(True)
            root.addWidget(note)

        status = _status_points(device)
        if status:
            root.addWidget(self._build_status(status))

        self.refresh()

    def _pick_points(self) -> None:
        if self._on_pick_points is None:
            return
        self._on_pick_points(self.device_name, list(self._extra_pids))

    # -------------------------------------------------- 区块

    def _build_keys(self, entries) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        for device, point, pid, commands, bits in entries:
            group = QWidget()
            group_layout = QVBoxLayout(group)
            group_layout.setContentsMargins(0, 0, 0, 0)
            group_layout.setSpacing(4)

            head = QHBoxLayout()
            name = QLabel(point.title)
            name.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
            head.addWidget(name)
            value = QLabel("--")
            self._value_labels[pid] = value
            head.addStretch(1)
            head.addWidget(value)
            group_layout.addLayout(head)

            grid = QGridLayout()
            grid.setSpacing(6)
            if commands:
                for index, (value_int, label) in enumerate(commands):
                    key = self._make_key(
                        label,
                        f"{label}　写入 {value_int}（0x{value_int:X}）",
                        lambda _checked=False, p=pid, v=value_int: self._send(p, v),
                    )
                    grid.addWidget(key, index // KEYS_PER_ROW, index % KEYS_PER_ROW)
            else:
                for index, (bit, label) in enumerate(bits):
                    key = self._make_key(
                        label,
                        f"bit{bit} {label}　点一下翻转并写入",
                        lambda _checked=False, p=pid, b=bit: self._toggle_bit(p, b),
                    )
                    grid.addWidget(key, index // KEYS_PER_ROW, index % KEYS_PER_ROW)
            for column in range(KEYS_PER_ROW):
                grid.setColumnStretch(column, 1)
            group_layout.addLayout(grid)
            layout.addWidget(group)
        return box

    def _build_status(self, entries) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        caption = QLabel("状态")
        caption.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(caption)
        for device, point, pid, _commands, bits in entries:
            label = QLabel("--")
            label.setWordWrap(True)
            label.setTextFormat(Qt.RichText)
            self._value_labels[pid] = label
            layout.addWidget(label)
        return box

    # -------------------------------------------------- 动作

    def _send(self, pid: str, value) -> None:
        self.writeRequested.emit(pid, value)

    def _toggle_bit(self, pid: str, bit: int) -> None:
        tag = self._value_of(pid)
        try:
            current = int(tag.value or 0)
        except (TypeError, ValueError):
            current = 0
        self._send(pid, current ^ (1 << bit))

    # -------------------------------------------------- 刷新

    def value_of(self, pid: str) -> TagValue:  # type: ignore[override]
        return self._value_of(pid)

    def periodic_of(self, pid: str):  # type: ignore[override]
        return self._periodic_of(pid)

    def engine_running(self) -> bool:  # type: ignore[override]
        return bool(self._engine_running())

    def refresh(self) -> None:
        for device, point, pid, commands, bits in device_keys(self._device, self._extra_pids) + (
            _status_points(self._device)
        ):
            label = self._value_labels.get(pid)
            if label is None:
                continue
            tag = self._value_of(pid)
            labels = point.bit_labels()
            if labels and not commands:
                active = set(point.decode_bits(tag.value, labels))
                if self._device.point(point.name) is point and len(labels) > 3:
                    # 状态字：列出所有位置（多位置位才看得全）
                    label.setText(
                        "　".join(
                            f"<span style='color:"
                            f"{theme.ACCENT if name in active else theme.BORDER}'>"
                            f"bit{bit} {name}</span>"
                            for bit, name in labels
                        )
                    )
                    continue
            text, colour = self._quality_text(tag)
            if tag.quality == "good":
                text = format_value(tag.value, point.decimals)
                unit = f" {point.unit}" if point.unit else ""
                text += unit
            label.setText(text)
            label.setStyleSheet(f"color: {colour};")


class PanelPointPicker(QDialog):
    """挑选要放进控制台的点位。

    默认只勾"命令点位"；带位定义的可写点位（例如 27 位的保护使能掩码）
    一次展开就是上百个键，所以要点名才加。
    """

    def __init__(self, device: DeviceDef, selected: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"选择点位 · {device.name}")
        self.setMinimumWidth(520)
        self._boxes: dict[str, QCheckBox] = {}
        chosen = set(selected)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(10)

        head = QLabel(
            "勾选后点「确定」，选中的点位会出现在控制台上（记在软件设置里，下次自动带上）。\n"
            "带「命令」列的点位默认就在控制台上。"
        )
        head.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        head.setWordWrap(True)
        root.addWidget(head)

        inner = QWidget()
        grid = QGridLayout(inner)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)
        for index, (_device, point, pid, commands, bits) in enumerate(
            _controllable_points(device)
        ):
            detail = (
                f"{len(commands)} 个命令" if commands else f"{len(bits)} 位掩码"
            )
            box = QCheckBox(f"{point.title}　（{detail}）")
            box.setChecked(bool(commands) or pid in chosen)
            if commands:
                box.setEnabled(False)
                box.setToolTip("参数表里填了「命令」列，默认就显示")
            self._boxes[pid] = box
            grid.addWidget(box, index, 0)

        if not self._boxes:
            empty = QLabel("这台设备没有可按键控制的点位。")
            empty.setStyleSheet(f"color: {theme.TEXT_DIM};")
            root.addWidget(empty)
        else:
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(inner)
            root.addWidget(scroll, 1)

        buttons = QDialogButtonBox()
        buttons.addButton("确定", QDialogButtonBox.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def selected(self) -> list[str]:
        """用户额外勾选的点位（不含默认显示的命令点位）。"""
        return [
            pid
            for pid, box in self._boxes.items()
            if box.isChecked() and box.isEnabled()
        ]


def _controllable_points(device: DeviceDef):
    """设备上"能用按键控制"的点位：有命令、或可写且带位定义。"""
    entries = []
    for point in device.points:
        commands = point.command_items()
        bits = point.bit_labels()
        if commands or (point.writable and bits):
            entries.append((device, point, point_id(device.name, point.name), commands, bits))
    return entries


def command_points(device: DeviceDef):
    """只挑"命令点位"：参数表里填了「命令」列的，按一下就让设备动作的那种。"""
    return [
        (device, point, point_id(device.name, point.name), point.command_items(), point.bit_labels())
        for point in device.points
        if point.command_items()
    ]


def device_keys(device: DeviceDef, extra_pids: list[str] | None = None):
    """控制台上要显示的点位。

    默认 = 命令点位（真按键）；``extra_pids`` 是用户自己挑的补充点位。
    一个命令点位都没有时退化成前几个可写位域点位，免得面板空白。
    """
    keys = command_points(device)
    seen = {pid for _d, _p, pid, _c, _b in keys}

    extra = set(extra_pids or [])
    if extra:
        for device_, point, pid, commands, bits in _controllable_points(device):
            if pid in extra and pid not in seen:
                keys.append((device_, point, pid, commands, bits))
                seen.add(pid)

    if not keys:
        fallback = [
            entry
            for entry in _controllable_points(device)
            if entry[3] == []  # 没有命令、只有位定义的那种
        ]
        keys = fallback[: DeviceControlPanel.FALLBACK_LIMIT]
    return keys


def _status_points(device: DeviceDef):
    """设备上适合在面板里当"状态一览"的只读位域点位。"""
    entries = []
    for point in device.points:
        bits = point.bit_labels()
        if bits and not point.writable:
            entries.append((device, point, point_id(device.name, point.name), [], bits))
    return entries[:3]


def _pretty(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


#: 已打开的面板（点号/设备名 -> 窗口），保证同一个对象不会开出两个窗口
_open_panels: dict[str, _BasePanel] = {}


def _register(key: str, panel: _BasePanel) -> None:
    """登记面板，并在它被销毁时**只清理自己**。

    注意不能无脑按 key 删除：换点位后会先关旧面板再开新面板，
    旧面板的 destroyed 信号晚一步到，若按 key 删就会把新面板也一起删掉。
    """
    _open_panels[key] = panel
    panel.setAttribute(Qt.WA_DeleteOnClose, True)
    panel.destroyed.connect(lambda *_args, k=key, p=panel: _unregister(k, p))


def _unregister(key: str, panel: _BasePanel) -> None:
    if _open_panels.get(key) is panel:
        _open_panels.pop(key, None)


def _reuse(key: str) -> _BasePanel | None:
    existing = _open_panels.get(key)
    if existing is not None and existing.isVisible():
        existing.raise_()
        existing.activateWindow()
        return existing
    return None


def open_point_panel(parent: QWidget, **kwargs) -> PointControlPanel:
    key = f"point::{kwargs['pid']}"
    reused = _reuse(key)
    if reused is not None:
        return reused
    panel = PointControlPanel(parent=parent, **kwargs)
    _register(key, panel)
    panel.show()
    return panel


def open_device_panel(parent: QWidget, **kwargs) -> DeviceControlPanel:
    key = f"device::{kwargs['device_name']}"
    reused = _reuse(key)
    if reused is not None:
        return reused
    panel = DeviceControlPanel(parent=parent, **kwargs)
    _register(key, panel)
    panel.show()
    return panel


def close_device_panel(device_name: str) -> None:
    """关掉某台设备的控制台（换点位后要重建面板时用）。"""
    panel = _open_panels.pop(f"device::{device_name}", None)
    if panel is not None:
        panel.close()


def close_all() -> None:
    """关掉所有控制面板。

    主窗口退出时必须调用：浮动工具窗口（Qt.Tool）会让进程一直不退出，
    表现为"关了主窗口程序还在后台"。
    """
    for key, panel in list(_open_panels.items()):
        _open_panels.pop(key, None)
        panel.stop_timer()
        try:
            panel.close()
        except RuntimeError:
            pass  # 对象已被 Qt 释放
    _open_panels.clear()
