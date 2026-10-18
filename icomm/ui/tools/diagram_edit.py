"""主回路图编辑工具。

主页那张主回路图以前只能改工程文件，现在可以在工具里直接编：
左边是**电气图元调色板**（电网/电池/光伏/开关/变压器/滤波/整流/逆变/DC-DC/母线/负载/电动机/
仪表/DSP…），右边是实时预览，**可以直接拖动图元**，下边是选中图元的属性和连线。

改的是工程里那份 ``DiagramDef``，点「应用到工程」后主页立刻重画 ——
坐标仍是 0~1 归一化，换分辨率不用改图。
"""

from __future__ import annotations

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...core import diagram_lib
from ...core.models import DiagramDef, DiagramLink, DiagramNode
from .. import theme
from ..spin import no_arrows
from ..pages.overview import DiagramCanvas
from .base import ToolContext, WindowTool, register_tool

LINK_COLUMNS = ["从", "到", "线型", "标注"]


class DiagramEditWindow(QWidget):
    """图元调色板 + 预览 + 属性 + 连线。"""

    def __init__(self, context: ToolContext, parent: QWidget | None = None) -> None:
        super().__init__(parent or context.window)
        self.context = context
        self.diagram: DiagramDef = DiagramDef.from_dict(
            context.project.diagram.to_dict() if context.project.diagram else None
        )
        self._dirty = False

        self.setWindowTitle(f"主回路图编辑 · {context.project.name}")
        self.setWindowFlags(Qt.Tool | Qt.WindowCloseButtonHint)
        self.resize(1240, 820)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        root.addWidget(self._build_hint())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_palette())
        splitter.addWidget(self._build_preview())
        splitter.addWidget(self._build_properties())
        splitter.setSizes([220, 620, 380])
        root.addWidget(splitter, 1)

        root.addWidget(self._build_links())
        root.addLayout(self._build_buttons())

        self.status = QLabel("就绪")
        self.status.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        root.addWidget(self.status)

        self.refresh_all()
        self.preview.nodeActivated.connect(self._on_activated)

    # -------------------------------------------------- 界面

    def _build_hint(self) -> QWidget:
        label = QLabel(
            "左边选图元双击添加，右边**直接拖动图元**调整位置；选中图元后可在右下改标题、"
            "绑定实时值、设点击行为（跳到某个设备详情）。改完点「应用到工程」。"
        )
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        return label

    def _build_palette(self) -> QWidget:
        box = QGroupBox("电气图元")
        layout = QVBoxLayout(box)
        self.palette = QListWidget()
        for category, items in diagram_lib.by_category().items():
            header = QListWidgetItem(f"— {category} —")
            header.setFlags(Qt.NoItemFlags)
            header.setForeground(Qt.gray)
            self.palette.addItem(header)
            for item in items:
                entry = QListWidgetItem(f"{item.label}　{item.symbol}")
                entry.setData(Qt.UserRole, item.symbol)
                entry.setToolTip(item.description)
                self.palette.addItem(entry)
        self.palette.itemDoubleClicked.connect(lambda _item: self.add_symbol())
        layout.addWidget(self.palette, 1)

        add = QPushButton("添加选中图元")
        add.setObjectName("primary")
        add.clicked.connect(lambda _checked=False: self.add_symbol())
        layout.addWidget(add)
        return box

    def _build_preview(self) -> QWidget:
        box = QGroupBox("预览（可拖动）")
        layout = QVBoxLayout(box)
        self.preview = DiagramCanvas()
        self.preview.editable = True
        self.preview.nodeMoved.connect(self._on_node_moved)
        layout.addWidget(self.preview, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        reset = QPushButton("复位视图")
        reset.clicked.connect(lambda _checked=False: self.preview.reset_view())
        row.addWidget(reset)
        row.addStretch(1)
        layout.addLayout(row)
        return box

    def _build_properties(self) -> QWidget:
        box = QGroupBox("选中图元")
        layout = QVBoxLayout(box)

        self.node_list = QListWidget()
        self.node_list.setMaximumHeight(150)
        self.node_list.currentRowChanged.connect(lambda _row: self._sync_form())
        layout.addWidget(self.node_list)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        self.symbol_combo = QComboBox()
        for item in diagram_lib.PALETTE:
            self.symbol_combo.addItem(f"{item.label}", item.symbol)
        self.symbol_combo.currentIndexChanged.connect(
            lambda _index: self._apply_form(silent=True)
        )
        form.addRow("图元", self.symbol_combo)

        self.label_edit = QLineEdit()
        self.label_edit.editingFinished.connect(self._apply_form)
        form.addRow("标题", self.label_edit)

        self.sub_edit = QLineEdit()
        self.sub_edit.editingFinished.connect(self._apply_form)
        form.addRow("副标题", self.sub_edit)

        self.value_combo = QComboBox()
        self.value_combo.addItem("（不显示实时值）", "")
        for pid, title in self.context.all_points():
            self.value_combo.addItem(title, pid)
        self.value_combo.setEditable(True)
        self.value_combo.currentIndexChanged.connect(lambda _index: self._apply_form())
        form.addRow("叠加实时值", self.value_combo)

        self.action_combo = QComboBox()
        for action, label in (
            ("none", "无"),
            ("device", "打开设备详情"),
            ("point", "看这个点位的历史"),
            ("page", "切换页面"),
        ):
            self.action_combo.addItem(label, action)
        self.action_combo.currentIndexChanged.connect(lambda _index: self._sync_action())
        form.addRow("点击行为", self.action_combo)

        self.target_combo = QComboBox()
        self.target_combo.setEditable(True)
        self.target_combo.currentIndexChanged.connect(lambda _index: self._apply_form())
        form.addRow("行为目标", self.target_combo)

        size_row = QHBoxLayout()
        size_row.setSpacing(6)
        self.w_box = self._ratio_box(0.05)
        self.h_box = self._ratio_box(0.05)
        size_row.addWidget(QLabel("宽"))
        size_row.addWidget(self.w_box)
        size_row.addWidget(QLabel("高"))
        size_row.addWidget(self.h_box)
        form.addRow("尺寸", size_row)

        pos_row = QHBoxLayout()
        pos_row.setSpacing(6)
        self.x_box = self._ratio_box(0.01)
        self.y_box = self._ratio_box(0.01)
        pos_row.addWidget(QLabel("X"))
        pos_row.addWidget(self.x_box)
        pos_row.addWidget(QLabel("Y"))
        pos_row.addWidget(self.y_box)
        form.addRow("位置", pos_row)

        layout.addLayout(form)

        row = QHBoxLayout()
        row.setSpacing(4)
        for text, delta, tip in (
            ("←", (-0.02, 0.0), "左移"),
            ("→", (0.02, 0.0), "右移"),
            ("↑", (0.0, -0.02), "上移"),
            ("↓", (0.0, 0.02), "下移"),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setMaximumWidth(40)
            button.clicked.connect(lambda _checked=False, d=delta: self.nudge(*d))
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(6)
        for text, slot in (
            ("左对齐一排", lambda: self.arrange("row")),
            ("按连线排序", lambda: self.arrange("links")),
            ("删除图元", self.remove_node),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda _checked=False, s=slot: s())
            row.addWidget(button)
        layout.addLayout(row)

        row = QHBoxLayout()
        row.setSpacing(6)
        for text, slot, tip in (
            ("模块下面显示…", self.edit_node_values,
             "主页上这个图元下面要挂哪些实时量（电压/电流/功率…）"),
            ("开关操作点…", self.edit_node_switch,
             "配了以后主页右键就能合闸/分闸"),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(lambda _checked=False, s=slot: s())
            row.addWidget(button)
        layout.addLayout(row)

        self.node_extra = QLabel("")
        self.node_extra.setWordWrap(True)
        self.node_extra.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        layout.addWidget(self.node_extra)
        layout.addStretch(1)
        return box

    @staticmethod
    def _ratio_box(step: float) -> QDoubleSpinBox:
        box = QDoubleSpinBox()
        box.setRange(0.0, 1.0)
        box.setDecimals(3)
        box.setSingleStep(step)
        no_arrows(box)
        return box

    def _build_links(self) -> QWidget:
        box = QGroupBox("连线")
        layout = QHBoxLayout(box)
        layout.setSpacing(8)

        self.link_table = QTableWidget(0, len(LINK_COLUMNS))
        self.link_table.setHorizontalHeaderLabels(LINK_COLUMNS)
        self.link_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.link_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.link_table.verticalHeader().setVisible(False)
        self.link_table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        for index, width in enumerate((150, 150, 110, 160)):
            self.link_table.setColumnWidth(index, width)
        self.link_table.setMaximumHeight(160)
        layout.addWidget(self.link_table, 1)

        right = QVBoxLayout()
        right.setSpacing(4)
        self.link_from = QComboBox()
        self.link_to = QComboBox()
        self.link_style = QComboBox()
        for style, label in diagram_lib.LINK_STYLES:
            self.link_style.addItem(label, style)
        for widget, label in ((self.link_from, "从"), (self.link_to, "到")):
            row = QHBoxLayout()
            row.addWidget(QLabel(label))
            row.addWidget(widget, 1)
            right.addLayout(row)
        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("线型"))
        style_row.addWidget(self.link_style, 1)
        right.addLayout(style_row)
        right.addStretch(1)
        layout.addLayout(right)

        buttons = QVBoxLayout()
        buttons.setSpacing(4)
        add = QPushButton("添加连线")
        add.clicked.connect(lambda _checked=False: self.add_link())
        buttons.addWidget(add)
        remove = QPushButton("删除连线")
        remove.clicked.connect(lambda _checked=False: self.remove_link())
        buttons.addWidget(remove)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        return box

    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        apply_button = QPushButton("应用到工程")
        apply_button.setObjectName("primary")
        apply_button.setToolTip("写回工程并刷新主页示意图")
        apply_button.clicked.connect(lambda _checked=False: self.apply_to_project())
        row.addWidget(apply_button)

        reload_btn = QPushButton("放弃改动重新载入")
        reload_btn.clicked.connect(lambda _checked=False: self.reload_from_project())
        row.addWidget(reload_btn)

        sample = QPushButton("载入内置示例图")
        sample.setToolTip("用当前工程类型的示例主回路图替换（可再改）")
        sample.clicked.connect(lambda _checked=False: self.load_sample())
        row.addWidget(sample)

        export = QPushButton("导出为 JSON…")
        export.clicked.connect(lambda _checked=False: self.export_json())
        row.addWidget(export)

        import_btn = QPushButton("从 JSON 导入…")
        import_btn.clicked.connect(lambda _checked=False: self.import_json())
        row.addWidget(import_btn)
        row.addStretch(1)
        return row

    # -------------------------------------------------- 数据

    def selected_index(self) -> int:
        return self.node_list.currentRow()

    def selected_node(self) -> DiagramNode | None:
        index = self.selected_index()
        if 0 <= index < len(self.diagram.nodes):
            return self.diagram.nodes[index]
        return None

    def refresh_all(self) -> None:
        self.refresh_nodes()
        self.refresh_links()
        self.preview.set_diagram(self.diagram, self.context.base_dir)
        self._sync_form()

    def refresh_nodes(self) -> None:
        current = self.selected_index()
        self.node_list.blockSignals(True)
        self.node_list.clear()
        for node in self.diagram.nodes:
            self.node_list.addItem(f"{node.label or node.id}　[{node.symbol}]")
        self.node_list.blockSignals(False)
        if self.diagram.nodes:
            self.node_list.setCurrentRow(
                min(max(0, current), len(self.diagram.nodes) - 1)
            )
        for combo in (self.link_from, self.link_to):
            combo.clear()
            for node in self.diagram.nodes:
                combo.addItem(node.label or node.id, node.id)
        self._sync_form()

    def refresh_links(self) -> None:
        self.link_table.setRowCount(len(self.diagram.links))
        for row, link in enumerate(self.diagram.links):
            cells = [
                link.source,
                link.target,
                diagram_lib.LINK_LABELS.get(link.style, link.style),
                link.label,
            ]
            for column, text in enumerate(cells):
                self.link_table.setItem(row, column, QTableWidgetItem(text))

    def _sync_form(self) -> None:
        node = self.selected_node()
        widgets = (
            self.symbol_combo, self.label_edit, self.sub_edit, self.value_combo,
            self.action_combo, self.target_combo, self.w_box, self.h_box,
            self.x_box, self.y_box,
        )
        for widget in widgets:
            widget.blockSignals(True)
        try:
            has = node is not None
            for widget in widgets:
                widget.setEnabled(has)
            if not has:
                return
            assert node is not None
            self._select_data(self.symbol_combo, node.symbol)
            self.label_edit.setText(node.label)
            self.sub_edit.setText(node.sub_label)
            self._select_value(node.value_point)
            self._select_data(self.action_combo, node.action or "none")
            self._sync_action()
            self._select_data(self.target_combo, node.target)
            if self.target_combo.findData(node.target) < 0 and node.target:
                self.target_combo.setEditText(node.target)
            for box, value in (
                (self.w_box, node.w), (self.h_box, node.h),
                (self.x_box, node.x), (self.y_box, node.y),
            ):
                box.setValue(float(value))
            self._sync_extra(node)
        finally:
            for widget in widgets:
                widget.blockSignals(False)

    def _sync_extra(self, node: DiagramNode | None) -> None:
        """"模块下面显示 / 开关"这一块的摘要文字。"""
        label = getattr(self, "node_extra", None)
        if label is None:
            return
        if node is None:
            label.setText("")
            return
        parts: list[str] = []
        if node.values:
            names = "、".join(
                (value.label or value.point.split(".", 1)[-1]) for value in node.values
            )
            parts.append(f"模块下面显示：{names}")
        if node.switch_point:
            parts.append(f"开关点：{node.switch_point}（合 {node.switch_on:g} / 分 {node.switch_off:g}）")
        if node.state_point:
            parts.append(f"状态点：{node.state_point}")
        label.setText("　｜　".join(parts) if parts else "还没有挂显示量与开关点（主页上点右键也能设）")

    def edit_node_values(self) -> None:
        node = self.selected_node()
        if node is None:
            QMessageBox.information(self, "先选图元", "请在列表或预览里选中一个图元。")
            return
        from ..dialogs.node_actions import NodeValuesDialog

        dialog = NodeValuesDialog(node, self.context.all_points(), self)
        if dialog.exec() != QDialog.Accepted:
            return
        node.values = dialog.values()
        self._after_node_extra_changed(node)

    def edit_node_switch(self) -> None:
        node = self.selected_node()
        if node is None:
            QMessageBox.information(self, "先选图元", "请在列表或预览里选中一个图元。")
            return
        from ..dialogs.node_actions import NodeSwitchDialog

        dialog = NodeSwitchDialog(node, self.context.all_points(), self)
        if dialog.exec() != QDialog.Accepted:
            return
        dialog.apply_to(node)
        self._after_node_extra_changed(node)

    def _after_node_extra_changed(self, node: DiagramNode) -> None:
        self._dirty = True
        self._sync_extra(node)
        self.preview.set_diagram(self.diagram, self.context.base_dir)
        self.status.setText(f"「{node.label or node.id}」的显示/开关设置已更新（记得应用到工程）")

    @staticmethod
    def _select_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        if index < 0 and combo.isEditable():
            combo.setEditText(value)
            return
        combo.setCurrentIndex(max(0, index))

    def _select_value(self, pid: str) -> None:
        index = self.value_combo.findData(pid)
        if index < 0:
            self.value_combo.setCurrentIndex(0)
            return
        self.value_combo.setCurrentIndex(index)

    def _sync_action(self) -> None:
        """行为目标的可选项跟着行为类型走（设备名 / 点号 / 页面名）。"""
        action = self.action_combo.currentData() or "none"
        keep = self.target_combo.currentData()
        self.target_combo.blockSignals(True)
        self.target_combo.clear()
        if action == "device":
            for device in self.context.project.devices:
                self.target_combo.addItem(device.name, device.name)
        elif action == "point":
            for pid, title in self.context.all_points():
                self.target_combo.addItem(title, pid)
        elif action == "page":
            # 页面名由主窗口定义；这里只在切换行为时才 import，避免工具被主窗口
            # 导入时形成循环（工具是可插拔的，不该反过来拖住主窗口）
            try:
                from ..main_window import NAV_ITEMS
            except ImportError:  # pragma: no cover
                NAV_ITEMS = ["主页", "运行监视", "示波器", "历史趋势", "报警记录", "通信监视", "设备与点位"]
            for name in NAV_ITEMS:
                self.target_combo.addItem(name, name)
        self.target_combo.setEditable(action != "none")
        self.target_combo.setEnabled(action != "none")
        if keep:
            index = self.target_combo.findData(keep)
            if index >= 0:
                self.target_combo.setCurrentIndex(index)
        self.target_combo.blockSignals(False)

    # -------------------------------------------------- 编辑

    def add_symbol(self) -> None:
        item = self.palette.currentItem()
        symbol = item.data(Qt.UserRole) if item is not None else None
        if not symbol:
            self.status.setText("先在左边选一个图元")
            return
        info = diagram_lib.info(str(symbol))
        node_id, label, sub = diagram_lib.suggest_label(
            str(symbol), [node.id for node in self.diagram.nodes]
        )
        positions = diagram_lib.auto_layout(len(self.diagram.nodes) + 1)
        x, y = positions[-1]
        node = DiagramNode(
            id=node_id,
            symbol=str(symbol),
            label=label,
            sub_label=sub,
            x=x,
            y=y,
            w=info.w if info else 0.14,
            h=info.h if info else 0.12,
        )
        self.diagram.nodes.append(node)
        self._dirty = True
        self.refresh_nodes()
        self.node_list.setCurrentRow(len(self.diagram.nodes) - 1)
        self.status.setText(f"已添加「{label}」—— 在预览里拖动它调整位置")

    def remove_node(self) -> None:
        index = self.selected_index()
        node = self.selected_node()
        if node is None:
            return
        self.diagram.nodes.pop(index)
        # 顺手把挂在它上面的连线也删掉，别留下指向空气的线
        dropped = [link for link in self.diagram.links if node.id in (link.source, link.target)]
        self.diagram.links = [
            link for link in self.diagram.links if node.id not in (link.source, link.target)
        ]
        self._dirty = True
        self.refresh_all()
        self.status.setText(
            f"已删除「{node.label or node.id}」"
            + (f"，同时删掉 {len(dropped)} 条相关连线" if dropped else "")
        )

    def _apply_form(self, *, silent: bool = False) -> None:
        node = self.selected_node()
        if node is None:
            return
        symbol = self.symbol_combo.currentData()
        if symbol:
            node.symbol = str(symbol)
        node.label = self.label_edit.text().strip()
        node.sub_label = self.sub_edit.text().strip()
        value = self.value_combo.currentData()
        node.value_point = str(value or "")
        node.action = str(self.action_combo.currentData() or "none")
        node.target = str(self.target_combo.currentData() or self.target_combo.currentText() or "")
        node.w = max(0.04, self.w_box.value())
        node.h = max(0.04, self.h_box.value())
        node.x = min(max(0.0, self.x_box.value()), max(0.0, 1.0 - node.w))
        node.y = min(max(0.0, self.y_box.value()), max(0.0, 1.0 - node.h))
        self._dirty = True
        self.refresh_nodes()
        self.preview.set_diagram(self.diagram, self.context.base_dir)
        if not silent:
            self.status.setText(f"已更新「{node.label or node.id}」")

    def _on_node_moved(self, node_id: str, x: float, y: float) -> None:
        self._dirty = True
        self.preview.update()
        node = self.diagram.node(node_id)
        if node is not None and self.selected_node() is node:
            self._sync_form()
        self.status.setText(f"「{node.label if node else node_id}」位置 {x:.3f}, {y:.3f}")

    def nudge(self, dx: float, dy: float) -> None:
        node = self.selected_node()
        if node is None:
            return
        node.x = round(min(max(0.0, node.x + dx), max(0.0, 1.0 - node.w)), 4)
        node.y = round(min(max(0.0, node.y + dy), max(0.0, 1.0 - node.h)), 4)
        self._dirty = True
        self._sync_form()
        self.preview.set_diagram(self.diagram, self.context.base_dir)

    def arrange(self, mode: str) -> None:
        """自动排布：一排铺开，或按连线顺序（从上游到下游）排。"""
        if not self.diagram.nodes:
            return
        if mode == "links" and self.diagram.links:
            order: list[str] = []
            targets = {link.target for link in self.diagram.links}
            starts = [node.id for node in self.diagram.nodes if node.id not in targets]
            queue = list(starts)
            while queue:
                current = queue.pop(0)
                if current in order:
                    continue
                order.append(current)
                queue.extend(
                    link.target for link in self.diagram.links if link.source == current
                )
            for node in self.diagram.nodes:
                if node.id not in order:
                    order.append(node.id)
            nodes = [self.diagram.node(node_id) for node_id in order]
        else:
            nodes = list(self.diagram.nodes)
        positions = diagram_lib.auto_layout(len(nodes))
        for node, (x, y) in zip(nodes, positions):
            if node is None:
                continue
            node.x, node.y = x, y
        self._dirty = True
        self.refresh_all()
        self.status.setText("已重新排布（一排从左到右）")

    def add_link(self) -> None:
        source = self.link_from.currentData()
        target = self.link_to.currentData()
        if not source or not target:
            self.status.setText("先加图元再来连线")
            return
        if source == target:
            self.status.setText("不能连到自己")
            return
        for link in self.diagram.links:
            if link.source == source and link.target == target:
                self.status.setText("这条连线已经有了")
                return
        style = self.link_style.currentData() or "ac"
        self.diagram.links.append(
            DiagramLink(source=str(source), target=str(target), style=str(style))
        )
        self._dirty = True
        self.refresh_links()
        self.preview.set_diagram(self.diagram, self.context.base_dir)
        self.status.setText(f"已连线：{source} → {target}")

    def remove_link(self) -> None:
        row = self.link_table.currentRow()
        if 0 <= row < len(self.diagram.links):
            link = self.diagram.links.pop(row)
            self._dirty = True
            self.refresh_links()
            self.preview.set_diagram(self.diagram, self.context.base_dir)
            self.status.setText(f"已删除连线 {link.source} → {link.target}")

    # -------------------------------------------------- 应用 / 存取

    def apply_to_project(self) -> None:
        # 存一份**副本**进工程：编辑器里的还接着改，不会"偷偷"改到工程上，
        # 「应用到工程」这个动作才有意义（脏标记也才说得清）
        self.context.project.diagram = DiagramDef.from_dict(self.diagram.to_dict())
        if self.context.project_changed is not None:
            self.context.project_changed()
        self._dirty = False
        self.status.setText(
            f"已应用到工程：{len(self.diagram.nodes)} 个图元 / {len(self.diagram.links)} 条连线"
            "（主页已刷新）"
        )
        self.context.report(f"主回路图已更新：{len(self.diagram.nodes)} 个图元")

    def reload_from_project(self) -> None:
        self.diagram = DiagramDef.from_dict(
            self.context.project.diagram.to_dict() if self.context.project.diagram else None
        )
        self._dirty = False
        self.refresh_all()
        self.status.setText("已重新载入工程里的图")

    def load_sample(self) -> None:
        """用内置示例主回路图替换当前这张图。"""
        from ...core import project as project_io

        answer = QMessageBox.question(
            self,
            "载入示例图",
            "会用内置示例主回路图替换当前的图（尚未应用到工程，可以再改）。继续？",
        )
        if answer != QMessageBox.Yes:
            return
        sample = project_io.sample_diagram_for(self.context.project)
        self.diagram = sample
        self._dirty = True
        self.refresh_all()
        self.status.setText(f"已载入示例图（{len(self.diagram.nodes)} 个图元），改完记得应用")

    def export_json(self) -> None:
        default = os.path.join(
            self.context.base_dir or ".", f"diagram_{self.context.project.name}.json"
        )
        path, _ = QFileDialog.getSaveFileName(
            self, "导出主回路图", default, "JSON (*.json)"
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.diagram.to_dict(), handle, ensure_ascii=False, indent=2)
        self.status.setText(f"已导出 {path}")

    def import_json(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入主回路图", self.context.base_dir or ".", "JSON (*.json)"
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "导入失败", f"读不了这个文件：\n{exc}")
            return
        self.diagram = DiagramDef.from_dict(data)
        self._dirty = True
        self.refresh_all()
        self.status.setText(f"已导入 {len(self.diagram.nodes)} 个图元，点「应用到工程」生效")

    def _on_activated(self, action: str, target: str) -> None:
        # 编辑模式下点图元只用来选中，不做跳转（免得改一半被导航走）
        for index, node in enumerate(self.diagram.nodes):
            if node.action == action and node.target == target:
                self.node_list.setCurrentRow(index)
                return

    def closeEvent(self, event) -> None:  # noqa: item2
        if self._dirty:
            answer = QMessageBox.question(
                self,
                "还有未应用的改动",
                "主回路图改过了但还没「应用到工程」，要丢弃这些改动吗？",
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        super().closeEvent(event)


@register_tool
class DiagramEditTool(WindowTool):
    name = "主回路图编辑"
    group = "工程"
    order = 20
    description = "用常见电气图元编辑主页主回路图（电网/电池/光伏/开关/滤波/整流/逆变/负载…）"

    def build(self, context: ToolContext) -> QWidget:
        return DiagramEditWindow(context)
