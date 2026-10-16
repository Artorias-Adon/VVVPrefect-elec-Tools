"""设备与点位配置。

左侧「通道 → 设备 → 点位」三级树，右侧属性表单。
表单字段由规格表驱动，新增协议后连接参数会自动出现在通道表单里。
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import project as project_io
from ...core.drivers import default_options, driver_catalog, driver_class
from ...core.models import (
    ALARM_LEVELS,
    DATA_TYPES,
    DISPLAY_TYPES,
    LINK_TYPES,
    REGISTER_TYPES,
    WORD_ORDERS,
    ChannelDef,
    DeviceDef,
    PointDef,
    Project,
)
from .. import theme
from ..widgets import ElidedLabel

# 字段规格：(属性名, 中文标签, 控件类型, 额外参数)
CHANNEL_FIELDS = [
    ("name", "通道名称", "str"),
    ("driver", "协议驱动", "driver"),
    ("link", "链路类型", "link"),
    ("enabled", "启用", "bool"),
    ("description", "备注", "str"),
]

DEVICE_FIELDS = [
    ("name", "设备名称", "str"),
    ("channel", "所属通道", "channel"),
    ("protocol", "协议", "str"),
    ("unit_id", "从站号 / 站地址", "int", 0, 255),
    ("poll_interval_ms", "采集周期(毫秒)", "int", 50, 3600000),
    ("timeout_ms", "超时(毫秒)", "int", 50, 60000),
    ("retries", "重试次数", "int", 0, 10),
    ("enabled", "启用", "bool"),
    ("description", "备注", "str"),
]

POINT_FIELDS = [
    ("name", "点号(英文)", "str"),
    ("label", "显示名称", "str"),
    ("group", "画面分组", "str"),
    ("register", "寄存器区", "register"),
    ("address", "起始地址", "int", 0, 65535),
    ("data_type", "数据类型", "data_type"),
    ("word_order", "字序", "word_order"),
    ("scale", "缩放系数", "float"),
    ("offset", "偏移量", "float"),
    ("unit", "单位", "str"),
    ("decimals", "小数位", "int", 0, 6),
    ("writable", "可写", "bool"),
    ("archive", "历史归档", "bool"),
    ("deadband", "归档死区", "float"),
    ("display", "画面显示", "display"),
    ("gauge_min", "仪表下限", "float"),
    ("gauge_max", "仪表上限", "float"),
    ("bits", "位定义(位:名称,逗号分隔)", "str"),
    ("commands", "命令按钮(值:名称,逗号分隔)", "str"),
    ("description", "备注", "str"),
]

ALARM_FIELDS = [
    ("enabled", "启用报警", "bool"),
    ("level", "报警级别", "level"),
    ("hi_hi", "高高限", "optfloat"),
    ("hi", "高限", "optfloat"),
    ("lo", "低限", "optfloat"),
    ("lo_lo", "低低限", "optfloat"),
    ("deadband", "报警死区", "float"),
    ("message", "报警文本", "str"),
]

CHOICE_SOURCES = {
    "link": LINK_TYPES,
    "register": REGISTER_TYPES,
    "data_type": DATA_TYPES,
    "word_order": WORD_ORDERS,
    "display": DISPLAY_TYPES,
    "level": ALARM_LEVELS,
}


def coerce_value(value, kind: str):
    """按字段**声明的类型**转换表单值。

    不能按「当前值的 Python 类型」推断：例如 ``alarm.hi_hi`` 在工程里
    可能是整数 250，若按旧值类型把用户输入的 252.5 转成 int 就会被截断成 252。
    """
    if kind == "bool":
        return bool(value)
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "optfloat":
        return None if value is None else float(value)
    if kind == "str":
        return str(value)
    return value


class DevicesPage(QWidget):
    """通道/设备/点位配置页。"""

    projectChanged = Signal()
    #: 请求导出/导入参数表（具体的文件对话框与路径由主窗口处理）
    exportCsvRequested = Signal()
    importCsvRequested = Signal()
    generateCsvRequested = Signal()

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self._current: tuple[str, Any, Any] | None = None  # (kind, parent, obj)
        self._editors: dict[str, tuple[QWidget, str, Any]] = {}
        self._rebuilding = False

        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(6)
        for text, slot in (
            ("新增通道", self.add_channel),
            ("新增设备", self.add_device),
            ("新增点位", self.add_point),
            ("复制点位", self.duplicate_point),
            ("删除选中", self.remove_selected),
        ):
            button = QPushButton(text)
            button.clicked.connect(slot)
            toolbar.addWidget(button)

        toolbar.addSpacing(16)
        for text, signal, tip in (
            ("保存参数设置…", self.exportCsvRequested, "只存点位定义，可选 CSV / TXT / JSON"),
            ("导入参数表…", self.importCsvRequested, "按扩展名自动识别 CSV / JSON"),
            ("生成模板", self.generateCsvRequested, "导出一份带示例行的空表"),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(signal.emit)
            toolbar.addWidget(button)
        toolbar.addStretch(1)
        self.hint = ElidedLabel("修改后点击「应用修改」写入内存，再点主工具栏「保存工程」落盘")
        self.hint.setStyleSheet(f"color: {theme.TEXT_DIM};")
        toolbar.addWidget(self.hint)
        root.addLayout(toolbar)

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["对象", "说明"])
        self.tree.setColumnWidth(0, 230)
        self.tree.currentItemChanged.connect(self._on_selection)
        splitter.addWidget(self.tree)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(10, 0, 0, 0)
        right_layout.setSpacing(8)

        self.form_scroll = QScrollArea()
        self.form_scroll.setWidgetResizable(True)
        self.form_host = QWidget()
        self.form_layout = QVBoxLayout(self.form_host)
        self.form_layout.setContentsMargins(0, 0, 0, 0)
        self.form_scroll.setWidget(self.form_host)
        right_layout.addWidget(self.form_scroll, 1)

        buttons = QHBoxLayout()
        self.apply_button = QPushButton("应用修改")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(self.apply_form)
        buttons.addWidget(self.apply_button)
        buttons.addStretch(1)
        right_layout.addLayout(buttons)

        splitter.addWidget(right)
        splitter.setSizes([420, 620])

        self.refresh_tree()

    # -------------------------------------------------- 树

    def set_project(self, project: Project) -> None:
        self.project = project
        self.refresh_tree()

    def refresh_tree(self) -> None:
        """重建整棵树，并保留展开状态与当前选中。

        应用修改后会调用本方法；如果不保留状态，用户每改一个字段
        就要重新展开、重新选中，非常难受。
        """
        expanded = self._expanded_keys()
        self._rebuilding = True
        try:
            self.tree.clear()
            for channel in self.project.channels:
                item = QTreeWidgetItem([channel.name, f"{channel.driver} / {channel.link}"])
                item.setData(0, Qt.UserRole, ("channel", None, channel.name))
                if not channel.enabled:
                    item.setForeground(0, Qt.gray)
                self.tree.addTopLevelItem(item)
                for device in self.project.devices:
                    if device.channel != channel.name:
                        continue
                    child = QTreeWidgetItem([device.name, f"unit={device.unit_id} / {len(device.points)} 点位"])
                    child.setData(0, Qt.UserRole, ("device", channel.name, device.name))
                    if not device.enabled:
                        child.setForeground(0, Qt.gray)
                    item.addChild(child)
                    for point in device.points:
                        leaf = QTreeWidgetItem(
                            [point.name, f"{point.register}#{point.address} {point.data_type}"]
                        )
                        leaf.setData(0, Qt.UserRole, ("point", device.name, point.name))
                        child.addChild(leaf)
                item.setExpanded(True)

            for device in self.project.devices:
                if self.project.channel(device.channel) is None:
                    orphan = QTreeWidgetItem(["(!) " + device.name, f"通道 {device.channel} 不存在"])
                    orphan.setForeground(0, Qt.red)
                    orphan.setData(0, Qt.UserRole, ("device", device.channel, device.name))
                    self.tree.addTopLevelItem(orphan)

            for item in self._iter_items():
                key = item.data(0, Qt.UserRole)
                if key and ("/".join(str(part) for part in key)) in expanded:
                    item.setExpanded(True)
        finally:
            self._rebuilding = False

        self._reselect()

    # -------------------------------------------------- 树的展开/选中状态

    def _iter_items(self):
        stack = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item is None:
                continue
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    @staticmethod
    def _key_text(key) -> str:
        return "/".join("" if part is None else str(part) for part in key)

    def _expanded_keys(self) -> set[str]:
        keys = set()
        for item in self._iter_items():
            key = item.data(0, Qt.UserRole)
            if key and item.isExpanded():
                keys.add(self._key_text(key))
        return keys

    def _reselect(self) -> None:
        """按当前选中的模型对象找回对应节点并重新选中。"""
        if self._current is None:
            return
        kind, parent, obj = self._current
        if kind == "channel":
            wanted = ("channel", None, obj.name)
        elif kind == "device":
            wanted = ("device", obj.channel, obj.name)
        else:
            wanted = ("point", parent.name, obj.name)
        target = self._key_text(wanted)
        for item in self._iter_items():
            key = item.data(0, Qt.UserRole)
            if key and self._key_text(key) == target:
                self.tree.setCurrentItem(item)
                return

    def select_point(self, pid: str) -> bool:
        """按 ``设备名.点号`` 选中某个点位。

        从点位对话框的「编辑点位定义」跳过来时用：直接定位到那一行，
        不用自己在一棵树里翻。
        """
        device_name, _, tag = pid.partition(".")
        wanted = self._key_text(("point", device_name, tag))
        for item in self._iter_items():
            key = item.data(0, Qt.UserRole)
            if key and self._key_text(key) == wanted:
                parent = item.parent()
                if parent is not None:
                    parent.setExpanded(True)
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
                return True
        return False

    def _resolve(self, data) -> tuple[str, Any, Any] | None:
        """把树节点数据还原成 (kind, parent_obj, obj)。"""
        if not data:
            return None
        kind, parent_name, name = data
        if kind == "channel":
            channel = self.project.channel(name)
            return ("channel", None, channel) if channel else None
        if kind == "device":
            device = self.project.device(name)
            return ("device", self.project.channel(parent_name), device) if device else None
        for device in self.project.devices:
            if device.name == parent_name:
                point = device.point(name)
                if point is not None:
                    return ("point", device, point)
        return None

    def _on_selection(self, current, _previous) -> None:
        if self._rebuilding:
            return
        self._current = self._resolve(current.data(0, Qt.UserRole) if current else None)
        self._build_form()

    # -------------------------------------------------- 表单

    def _build_form(self) -> None:
        while self.form_layout.count():
            item = self.form_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._editors.clear()

        if self._current is None:
            label = QLabel("请在左侧选择一个通道、设备或点位")
            label.setStyleSheet(f"color: {theme.TEXT_DIM}; padding: 20px;")
            self.form_layout.addWidget(label)
            self.form_layout.addStretch(1)
            return

        kind, parent, obj = self._current
        if kind == "channel":
            self._add_group("通道属性", CHANNEL_FIELDS, obj)
            self._add_driver_options(obj)
        elif kind == "device":
            self._add_group("设备属性", DEVICE_FIELDS, obj)
        else:
            self._add_group("点位属性", POINT_FIELDS, obj)
            self._add_group("报警设置", ALARM_FIELDS, obj.alarm)

        self.form_layout.addStretch(1)

    def _add_group(self, title: str, specs: list, obj) -> None:
        box = QWidget()
        layout = QFormLayout(box)
        layout.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(6)

        heading = QLabel(f"— {title} —")
        heading.setStyleSheet(f"color: {theme.BORDER}; font-weight: 600;")
        self.form_layout.addWidget(heading)

        for spec in specs:
            attr, label, kind = spec[0], spec[1], spec[2]
            extra = spec[3:]
            widget = self._make_editor(kind, getattr(obj, attr, None), attr, extra)
            self._editors[attr] = (widget, kind, obj)
            layout.addRow(label, widget)
        self.form_layout.addWidget(box)

    def _make_editor(self, kind: str, value: Any, attr: str, extra: tuple) -> QWidget:
        if kind == "bool":
            widget = QCheckBox()
            widget.setChecked(bool(value))
            return widget
        if kind in ("int", "float"):
            spin = QSpinBox() if kind == "int" else QDoubleSpinBox()
            if kind == "float":
                spin.setDecimals(4)
                spin.setRange(-1e9, 1e9)
            else:
                spin.setRange(int(extra[0]) if extra else -1_000_000, int(extra[1]) if len(extra) > 1 else 1_000_000)
            spin.setValue(int(value or 0) if kind == "int" else float(value or 0.0))
            return spin
        if kind == "optfloat":
            edit = QLineEdit("" if value is None else f"{float(value):g}")
            edit.setPlaceholderText("留空表示不启用该限值")
            return edit
        if kind in CHOICE_SOURCES:
            combo = QComboBox()
            for option in CHOICE_SOURCES[kind]:
                combo.addItem(str(option), option)
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)
            if attr == "link":
                combo.currentIndexChanged.connect(self._on_link_changed)
            return combo
        if kind == "driver":
            combo = QComboBox()
            for entry in driver_catalog():
                combo.addItem(f"{entry['name']} — {entry['description'][:28]}", entry["name"])
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.currentIndexChanged.connect(self._on_driver_changed)
            return combo
        if kind == "channel":
            combo = QComboBox()
            for channel in self.project.channels:
                combo.addItem(channel.name, channel.name)
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)
            return combo
        edit = QLineEdit("" if value is None else str(value))
        return edit

    def _add_driver_options(self, channel: ChannelDef) -> None:
        cls = driver_class(channel.driver)
        if cls is None or not hasattr(cls, "option_schema"):
            return
        schema = cls.option_schema(channel.link)
        if not schema:
            return

        box = QWidget()
        layout = QFormLayout(box)
        layout.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(6)

        heading = QLabel(f"— {channel.driver} 连接参数（{channel.link}）—")
        heading.setStyleSheet(f"color: {theme.BORDER}; font-weight: 600;")
        self.form_layout.addWidget(heading)

        for field in schema:
            current = channel.options.get(field.key, field.default)
            if field.type == "choice":
                widget = QComboBox()
                for option in field.choices:
                    widget.addItem(str(option), option)
                index = widget.findData(current)
                if index < 0:
                    index = widget.findData(field.default)
                widget.setCurrentIndex(index if index >= 0 else 0)
            elif field.type in ("int", "float"):
                widget = QSpinBox() if field.type == "int" else QDoubleSpinBox()
                if field.type == "float":
                    widget.setDecimals(3)
                widget.setRange(
                    int(field.minimum) if field.minimum is not None else -1_000_000,
                    int(field.maximum) if field.maximum is not None else 1_000_000,
                )
                widget.setValue(type(current)(current))
            elif field.type == "bool":
                widget = QCheckBox()
                widget.setChecked(bool(current))
            else:
                widget = QLineEdit(str(current))
            if field.hint:
                widget.setToolTip(field.hint)
            key = f"opt::{field.key}"
            self._editors[key] = (widget, "option", (channel, field))
            layout.addRow(field.label, widget)

        self.form_layout.addWidget(box)

    def _on_driver_changed(self) -> None:
        """切换驱动后重建表单，让链路类型与连接参数跟着变。"""
        if self._current is None or self._current[0] != "channel":
            return
        channel: ChannelDef = self._current[2]
        combo = self._editors.get("driver", (None, None, None))[0]
        if combo is None or combo.currentData() == channel.driver:
            return
        self._apply_editors(exclude={"driver"})
        channel.driver = combo.currentData()
        cls = driver_class(channel.driver)
        if cls and cls.link_types and channel.link not in cls.link_types:
            target = cls.link_types[0]
            channel.switch_link(target, default_options(channel.driver, target))
        else:
            channel.options = default_options(channel.driver, channel.link)
        self._build_form()
        self.refresh_tree()
        self.projectChanged.emit()

    def _on_link_changed(self) -> None:
        """切换链路类型：恢复该链路上次用过的参数（没有才用驱动默认值）。

        工控现场常要在 RTU（插串口）与 TCP（接网关/仿真器）之间来回切，
        每切一次就把串口号或端口号清掉太费事，所以两套参数各自记住。
        这也避免了 tcp 切 rtu 时把 int 类型的 TCP 端口当串口名用。
        """
        if self._current is None or self._current[0] != "channel":
            return
        channel: ChannelDef = self._current[2]
        combo = self._editors.get("link", (None, None, None))[0]
        if combo is None or combo.currentData() == channel.link:
            return
        stale = {"link"} | {k for k in self._editors if k.startswith("opt::")}
        self._apply_editors(exclude=stale)
        new_link = combo.currentData()
        channel.switch_link(new_link, default_options(channel.driver, new_link))
        self._build_form()
        self.refresh_tree()
        self.projectChanged.emit()

    # -------------------------------------------------- 应用

    def _apply_editors(self, exclude: set[str] | None = None) -> None:
        """把表单里的值写回模型对象。"""
        exclude = exclude or set()
        for key, (widget, editor_kind, payload) in self._editors.items():
            if key in exclude:
                continue
            if editor_kind == "option":
                channel, field = payload
                channel.options[field.key] = self._coerce_option(
                    self._read_editor(widget, field.type), field
                )
                continue
            setattr(payload, key, coerce_value(self._read_editor(widget, editor_kind), editor_kind))

    @staticmethod
    def _coerce_option(value, field):
        """把表单值转换成参数规格声明的类型，异常时退回默认值。"""
        if value is None:
            return field.default
        try:
            if field.type == "float":
                return float(value)
            if field.type == "int":
                return int(value)
            if field.type == "bool":
                return bool(value)
            if field.type == "str":
                return str(value)
            return type(field.default)(value) if field.default not in ("", None) else str(value)
        except (TypeError, ValueError):
            return field.default

    def apply_form(self) -> bool:
        if self._current is None:
            return False
        self._apply_editors()
        self.refresh_tree()
        self.projectChanged.emit()
        return True

    @staticmethod
    def _read_editor(widget: QWidget, kind: str):
        if kind == "bool":
            return widget.isChecked()
        if kind in ("int", "float"):
            return widget.value()
        if kind == "optfloat":
            text = widget.text().strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return None
        if kind in CHOICE_SOURCES or kind in ("driver", "channel", "option", "choice"):
            data = widget.currentData()
            return data if data is not None else widget.currentText()
        return widget.text()

    # -------------------------------------------------- 增删

    def add_channel(self) -> None:
        name = self._unique_name([c.name for c in self.project.channels], "新通道")
        channel = ChannelDef(
            name=name, driver="modbus", link="tcp", options=default_options("modbus", "tcp")
        )
        self.project.channels.append(channel)
        self.refresh_tree()
        self.projectChanged.emit()

    def add_device(self) -> None:
        if not self.project.channels:
            QMessageBox.information(self, "提示", "请先新增一个通道")
            return
        parent_channel = self._current[2].name if self._current and self._current[0] == "channel" else None
        if parent_channel is None and self._current and self._current[0] == "device":
            parent_channel = self._current[2].channel
        if parent_channel is None:
            parent_channel = self.project.channels[0].name
        name = self._unique_name([d.name for d in self.project.devices], "新设备")
        device = DeviceDef(name=name, channel=parent_channel, unit_id=1)
        self.project.devices.append(device)
        self.refresh_tree()
        self.projectChanged.emit()

    def add_point(self) -> None:
        device = self._current_device()
        if device is None:
            QMessageBox.information(self, "提示", "请先选中一个设备（或该设备下的点位）")
            return
        name = self._unique_name([p.name for p in device.points], "新点位")
        next_address = 0
        if device.points:
            next_address = max(p.address for p in device.points) + 1
        device.points.append(PointDef(name=name, address=next_address))
        self.refresh_tree()
        self.projectChanged.emit()

    def duplicate_point(self) -> None:
        if self._current is None or self._current[0] != "point":
            QMessageBox.information(self, "提示", "请先选中一个点位")
            return
        device: DeviceDef = self._current[1]
        source: PointDef = self._current[2]
        clone = PointDef.from_dict(source.to_dict())
        clone.name = self._unique_name([p.name for p in device.points], source.name + "_copy")
        clone.address = source.address + (1 if source.data_type in ("uint16", "int16", "bool") else 2)
        clone.label = (source.label or source.name) + " 副本"
        device.points.append(clone)
        self.refresh_tree()
        self.projectChanged.emit()

    def remove_selected(self) -> None:
        if self._current is None:
            return
        kind, parent, obj = self._current
        if kind == "channel":
            count = len([d for d in self.project.devices if d.channel == obj.name])
            if count and QMessageBox.question(
                self, "确认", f"通道 {obj.name} 下还有 {count} 台设备，连同删除？"
            ) != QMessageBox.Yes:
                return
            self.project.devices = [d for d in self.project.devices if d.channel != obj.name]
            self.project.channels.remove(obj)
        elif kind == "device":
            self.project.devices.remove(obj)
        else:
            parent.points.remove(obj)
        self._current = None
        self.refresh_tree()
        self._build_form()
        self.projectChanged.emit()

    def _current_device(self) -> DeviceDef | None:
        if self._current is None:
            return None
        if self._current[0] == "device":
            return self._current[2]
        if self._current[0] == "point":
            return self._current[1]
        return None

    @staticmethod
    def _unique_name(existing: list[str], base: str) -> str:
        if base not in existing:
            return base
        index = 2
        while f"{base}{index}" in existing:
            index += 1
        return f"{base}{index}"


def new_project() -> Project:
    """空白工程（保留给脚本调用，实际等价于 ``project.blank_project()``）。"""
    return project_io.blank_project()


__all__ = ["DevicesPage", "new_project"]
