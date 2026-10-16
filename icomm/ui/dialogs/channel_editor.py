"""通信配置编辑器：通道列表 + 连接参数表单。

设置里的「通信」标签页用它，也可以被别处复用。

为什么单独做成控件：现场改 Modbus 参数最常干的事是"切 TCP / RTU 换地址"，
放在设置里就能集中管；同时「设备与点位」页也还能改，两边都指向同一批
:class:`~icomm.core.models.ChannelDef` 对象，所以改哪边都一致。

参数表单完全由驱动的 ``option_schema`` 生成 —— 以后加一种协议或给 Modbus
加一个参数，这里自动就有对应输入框。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.drivers import (
    default_options,
    driver_catalog,
    driver_class,
    driver_names,
)
from ...core.models import ChannelDef, Project
from .. import theme
from ..option_fields import apply_signals, make_option_widget, options_from_widgets

LINK_LABELS = {
    "tcp": "TCP（网口 / 网关）",
    "rtu": "RTU（串口）",
    "rtu_over_tcp": "RTU over TCP",
}


class ChannelEditor(QWidget):
    """通道的连接参数配置。"""

    #: 用户改了任何东西
    changed = Signal()

    def __init__(self, project: Project, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.project = project
        self._current: ChannelDef | None = None
        self._option_widgets: dict[str, QWidget] = {}
        self._loading = False

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        # ---------------- 左：通道列表
        left = QGroupBox("通道")
        left_layout = QVBoxLayout(left)
        self.list = QListWidget()
        self.list.setMinimumWidth(210)
        self.list.currentRowChanged.connect(self._on_selected)
        left_layout.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        add = QPushButton("新建")
        add.setToolTip("新建一条通道（默认 Modbus TCP）")
        add.clicked.connect(self.add_channel)
        buttons.addWidget(add)
        duplicate = QPushButton("复制")
        duplicate.setToolTip("复制当前通道的参数，便于加第二条总线")
        duplicate.clicked.connect(self.duplicate_channel)
        buttons.addWidget(duplicate)
        remove = QPushButton("删除")
        remove.setObjectName("danger")
        remove.clicked.connect(self.remove_channel)
        buttons.addWidget(remove)
        left_layout.addLayout(buttons)
        root.addWidget(left, 1)

        # ---------------- 右：参数表单
        right = QGroupBox("连接参数")
        right_layout = QVBoxLayout(right)
        self.form_host = QWidget()
        self.form = QFormLayout(self.form_host)
        self.form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setSpacing(8)
        right_layout.addWidget(self.form_host)
        right_layout.addStretch(1)
        root.addWidget(right, 2)

        self.status = QLabel("")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.status.setWordWrap(True)
        right_layout.addWidget(self.status)

        self.reload()

    # -------------------------------------------------- 列表

    def reload(self, keep: str = "") -> None:
        """重建通道列表；``keep`` 指定要选中的通道名。"""
        wanted = keep or (self._current.name if self._current else "")
        self._loading = True
        try:
            self.list.clear()
            for channel in self.project.channels:
                link = LINK_LABELS.get(channel.link, channel.link)
                item = QListWidgetItem(f"{channel.name}　[{channel.driver} / {link}]")
                item.setData(Qt.UserRole, channel.name)
                if not channel.enabled:
                    item.setForeground(Qt.gray)
                self.list.addItem(item)
            row = 0
            for index in range(self.list.count()):
                if self.list.item(index).data(Qt.UserRole) == wanted:
                    row = index
                    break
            if self.list.count():
                self.list.setCurrentRow(row)
            else:
                self._current = None
                self._clear_form()
        finally:
            self._loading = False
        self._on_selected(self.list.currentRow())

    def _clear_form(self) -> None:
        self._option_widgets.clear()
        while self.form.count():
            item = self.form.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _on_selected(self, row: int) -> None:
        if self._loading or row < 0:
            self._current = None
            return
        name = self.list.item(row).data(Qt.UserRole)
        channel = self.project.channel(name)
        if channel is None:
            return
        self._current = channel
        self._build_form(channel)

    # -------------------------------------------------- 表单

    def _build_form(self, channel: ChannelDef) -> None:
        self._loading = True
        try:
            self._clear_form()

            self.name_edit = QLineEdit(channel.name)
            self.name_edit.editingFinished.connect(self._apply_name)
            self.form.addRow("通道名", self.name_edit)

            self.enabled_box = QCheckBox("启用（关掉则本通道不采集）")
            self.enabled_box.setChecked(channel.enabled)
            self.enabled_box.toggled.connect(self._apply_simple)
            self.form.addRow("", self.enabled_box)

            self.driver_combo = QComboBox()
            for entry in driver_catalog():
                self.driver_combo.addItem(
                    f"{entry['name']}　{entry['description']}", entry["name"]
                )
            index = self.driver_combo.findData(channel.driver)
            if index < 0:
                # 未安装的驱动（别人工程里的协议）也要显示出来，不能悄悄改掉
                self.driver_combo.addItem(f"{channel.driver}（未安装）", channel.driver)
                index = self.driver_combo.count() - 1
            self.driver_combo.setCurrentIndex(index)
            self.driver_combo.currentIndexChanged.connect(self._on_driver_changed)
            self.form.addRow("协议", self.driver_combo)

            self.link_combo = QComboBox()
            cls = driver_class(channel.driver)
            links = list(cls.link_types) if cls and cls.link_types else [channel.link]
            for link in links:
                self.link_combo.addItem(LINK_LABELS.get(link, link), link)
            index = self.link_combo.findData(channel.link)
            if index < 0:
                self.link_combo.addItem(channel.link, channel.link)
                index = self.link_combo.count() - 1
            self.link_combo.setCurrentIndex(index)
            self.link_combo.currentIndexChanged.connect(self._on_link_changed)
            self.form.addRow("链路类型", self.link_combo)

            heading = QLabel(
                f"— {channel.driver} 连接参数（{channel.link}）—"
            )
            heading.setStyleSheet(f"color: {theme.BORDER}; font-weight: 600;")
            self.form.addRow(heading)

            schema = cls.option_schema(channel.link) if cls else []
            self._option_widgets.clear()
            for field in schema:
                widget = make_option_widget(field, channel.options.get(field.key, field.default))
                widget.setObjectName(f"opt::{field.key}")
                # 用 *_args 吞掉信号参数：currentIndexChanged 会把索引当参数传进来，
                # 直接连 apply_form 的话索引会被当成 silent，改了不通知外面。
                for signal in apply_signals(widget):
                    signal.connect(lambda *_args: self.apply_form())
                self._option_widgets[field.key] = widget
                self.form.addRow(field.label, widget)

            if not schema:
                note = QLabel("该协议没有可配置的连接参数。")
                note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
                self.form.addRow(note)

            self.description_edit = QLineEdit(channel.description)
            self.description_edit.setPlaceholderText("备注，例如「1 号网关下挂 3 台储能变流器」")
            self.description_edit.editingFinished.connect(self._apply_simple)
            self.form.addRow("备注", self.description_edit)

            self._refresh_status()
        finally:
            self._loading = False

    def _refresh_status(self) -> None:
        channel = self._current
        if channel is None:
            self.status.setText("")
            return
        remembered = [
            LINK_LABELS.get(link, link)
            for link in channel.options_by_link
            if link != channel.link
        ]
        text = f"当前连接：{_connection_text(channel)}"
        if remembered:
            text += (
                f"\n已记住其它链路类型的参数：{'、'.join(remembered)}"
                "（切回去时自动恢复，不用重填）"
            )
        self.status.setText(text)

    # -------------------------------------------------- 编辑

    def _apply_name(self) -> None:
        channel = self._current
        if channel is None or self._loading:
            return
        new_name = self.name_edit.text().strip()
        if not new_name or new_name == channel.name:
            self.name_edit.setText(channel.name)
            return
        if self.project.channel(new_name) is not None:
            QMessageBox.warning(self, "通道名重复", f"已经有叫「{new_name}」的通道了。")
            self.name_edit.setText(channel.name)
            return
        old_name = channel.name
        channel.name = new_name
        # 设备是按通道名挂载的，改名要一起搬过去
        for device in self.project.devices:
            if device.channel == old_name:
                device.channel = new_name
        self.reload(keep=new_name)
        self.changed.emit()

    def _apply_simple(self) -> None:
        channel = self._current
        if channel is None or self._loading:
            return
        channel.enabled = self.enabled_box.isChecked()
        channel.description = self.description_edit.text().strip()
        self.changed.emit()
        self.reload(keep=channel.name)

    def _on_driver_changed(self) -> None:
        channel = self._current
        if channel is None or self._loading:
            return
        new_driver = self.driver_combo.currentData()
        if new_driver == channel.driver:
            return
        channel.driver = new_driver
        cls = driver_class(new_driver)
        if cls and cls.link_types and channel.link not in cls.link_types:
            channel.switch_link(cls.link_types[0], default_options(new_driver, cls.link_types[0]))
        else:
            channel.options = default_options(channel.driver, channel.link)
        self._build_form(channel)
        self.reload(keep=channel.name)
        self.changed.emit()

    def _on_link_changed(self) -> None:
        channel = self._current
        if channel is None or self._loading:
            return
        new_link = self.link_combo.currentData()
        if new_link == channel.link:
            return
        # 先把当前这套参数存下来，再切过去恢复目标链路自己的参数
        self.apply_form(silent=True)
        channel.switch_link(new_link, default_options(channel.driver, new_link))
        self._build_form(channel)
        self.reload(keep=channel.name)
        self.changed.emit()

    def apply_form(self, silent: bool = False) -> bool:
        """把表单里的连接参数写回模型。"""
        channel = self._current
        if channel is None or self._loading:
            return False
        cls = driver_class(channel.driver)
        schema = cls.option_schema(channel.link) if cls else []
        channel.options = options_from_widgets(self._option_widgets, schema)
        if not silent:
            self.changed.emit()
            self.reload(keep=channel.name)
        return True

    # -------------------------------------------------- 增删

    def add_channel(self) -> None:
        name = self._unique_name("通道")
        channel = ChannelDef(
            name=name,
            driver="modbus",
            link="tcp",
            options=default_options("modbus", "tcp"),
        )
        channel.remember_options()
        self.project.channels.append(channel)
        self.reload(keep=name)
        self.changed.emit()

    def duplicate_channel(self) -> None:
        if self._current is None:
            QMessageBox.information(self, "提示", "请先选中一条通道。")
            return
        self.apply_form(silent=True)
        clone = ChannelDef.from_dict(self._current.to_dict())
        clone.name = self._unique_name(self._current.name)
        self.project.channels.append(clone)
        self.reload(keep=clone.name)
        self.changed.emit()

    def remove_channel(self) -> None:
        channel = self._current
        if channel is None:
            QMessageBox.information(self, "提示", "请先选中一条通道。")
            return
        attached = [d.name for d in self.project.devices if d.channel == channel.name]
        if attached:
            answer = QMessageBox.warning(
                self,
                "通道上还挂着设备",
                f"通道「{channel.name}」上还有 {len(attached)} 台设备：\n"
                + "、".join(attached[:8])
                + ("……" if len(attached) > 8 else "")
                + "\n\n删除通道后这些设备将无法采集（设备本身会保留）。确定删除吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        elif QMessageBox.question(
            self, "删除通道", f"确定删除通道「{channel.name}」吗？"
        ) != QMessageBox.Yes:
            return

        self.project.channels = [c for c in self.project.channels if c is not channel]
        self._current = None
        self.reload()
        self.changed.emit()

    def _unique_name(self, base: str) -> str:
        existing = {c.name for c in self.project.channels}
        index = 1
        while f"{base}{index}" in existing:
            index += 1
        candidate = f"{base}{index}"
        if candidate not in existing and not existing:
            return candidate
        return candidate


def _connection_text(channel: ChannelDef) -> str:
    """把连接参数拼成一行看得懂的文字。"""
    options = channel.options or {}
    if channel.link == "rtu":
        return (
            f"Modbus RTU　{options.get('port', '-')}，"
            f"{options.get('baudrate', '-')}，{options.get('bytesize', 8)}"
            f"{options.get('parity', 'N')}{options.get('stopbits', 1)}"
        )
    if channel.link == "rtu_over_tcp":
        return f"RTU over TCP　{options.get('host', '-')}:{options.get('port', '-')}"
    return f"Modbus TCP　{options.get('host', '-')}:{options.get('port', '-')}"
