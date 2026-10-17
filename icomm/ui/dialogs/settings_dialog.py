"""设置对话框：软件设置 + 工程设置。

分两页，因为这两类配置的生命周期不一样：

* **软件设置**存在 ``<程序目录>/settings.json``，换工程也生效；
* **工程设置**存在工程文件里（``project.json`` / ``.iprj``），跟着工程走。

对话框在**副本**上改，按下「确定」才把结果写回调用方，
这样中途取消不会留下改了一半的配置。
"""

from __future__ import annotations

import os
from dataclasses import replace

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ...core import project_store
from ...core.models import Project, ProjectStorage
from ...core.settings import PERIODIC_MAX_MS, PERIODIC_MIN_MS, AppSettings
from .. import theme
from .appearance_editor import AppearanceEditor
from .channel_editor import ChannelEditor
from .sizing import fit_to_available_screen, scrollable_page

#: 示波器时基选项（秒/格）
TIMEBASE_OPTIONS = [
    ("1 秒/格", 1.0),
    ("2 秒/格", 2.0),
    ("5 秒/格", 5.0),
    ("10 秒/格", 10.0),
    ("30 秒/格", 30.0),
    ("2 分/格", 120.0),
    ("5 分/格", 300.0),
]

NO_SAMPLE = "（用第一个示例）"


def open_in_file_manager(path: str) -> None:
    """在系统资源管理器里打开一个目录/文件（Windows 用 startfile）。"""
    try:
        os.startfile(path)  # noqa: S606 - 用户主动点的"打开文件夹"
    except (AttributeError, OSError):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def _double_spin(low: float, high: float, step: float, suffix: str = "") -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setDecimals(2)
    if suffix:
        box.setSuffix(suffix)
    return box


class SettingsDialog(QDialog):
    """设置窗口。

    标签页：
    * **界面外观** —— 配色/字体/字号/密度 + 高级逐项自定义（实时预览）
    * **通信** —— 通道的连接参数（TCP / RTU）、新建/删除通道
    * **软件设置** —— 启动行为、超时、示波器默认值等（存 data/settings.json）
    * **工程设置** —— 归档、保留天数、存放路径等（跟着工程文件走）
    """

    #: 外观变了（用于让主窗口重建页面 —— 有些内联样式只在构造时取一次颜色）
    appearanceChanged = Signal()

    def __init__(
        self,
        settings: AppSettings,
        project: Project,
        samples: list[str],
        parent: QWidget | None = None,
        base_dir: str = "",
        project_root: str = "",
        initial_tab: int | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumWidth(720)
        self.setMinimumHeight(620)

        #: 在副本上改，确定时才提交
        self.settings = replace(settings)
        self.project = Project.from_dict(project.to_dict())
        self._source_project = project
        self._samples = samples
        #: 工程当前所在的文件夹（确定时若改了文件夹名，由主窗口就地改名）
        self._project_root = project_root
        #: 确定后要不要把工程文件夹改名（空 = 不改）
        self.folder_rename_to = ""
        #: 当前实际用的工程仓库（设置里留空时显示它，让用户知道工程到底存哪了）
        self._active_home, self._active_home_why = project_store.discover_project_home(
            base_dir, settings.project_home, create=False
        )
        #: 用户点了「恢复默认大小」（确定时由主窗口执行）
        self.reset_window_requested = False
        #: 进入时的外观，取消时还原。
        #: 用 preference() 而不是 appearance()：开着「跟随系统」时后者是按系统偏好
        #: 换算过的配色，拿它还原会把用户手选的那套冲掉。
        self._original_appearance = theme.preference().copy()
        self.appearance_changed = False

        #: 外观重建防抖：连续调字号/拖色板时只在停手后重建一次页面
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(250)
        self._rebuild_timer.timeout.connect(self._emit_appearance_changed)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        self.tabs = QTabWidget()

        self.appearance_editor = AppearanceEditor(
            theme.Appearance.from_dict(self.settings.appearance),
            parent=self,
            # 导出配色默认存到这里：现场习惯把配置跟工程放一起
            base_dir=base_dir,
        )
        self.appearance_editor.changed.connect(self._on_appearance_changed)
        self.tabs.addTab(scrollable_page(self.appearance_editor), "界面外观")

        self.channel_editor = ChannelEditor(self.project, parent=self)
        self.channel_editor.changed.connect(self._on_channels_changed)
        self.tabs.addTab(scrollable_page(self.channel_editor), "通信")

        self.tabs.addTab(scrollable_page(self._build_app_tab()), "软件设置")
        self.tabs.addTab(scrollable_page(self._build_project_tab()), "工程设置")
        # 「设置」菜单里可以直接点到某一块，这里按调用方指定的页签打开
        if initial_tab is not None:
            try:
                wanted = int(initial_tab)
            except (TypeError, ValueError):
                wanted = 0
            self.tabs.setCurrentIndex(max(0, min(self.tabs.count() - 1, wanted)))
        root.addWidget(self.tabs, 1)

        hint = QLabel(
            "界面与通信配置立即生效；软件设置存 &lt;程序目录&gt;/settings.json，"
            "工程设置跟着工程文件走，改完记得保存工程。"
            f"工程本身放在工程仓库 <b>{self._active_home}</b>（{self._active_home_why}），"
            "软件目录只放软件。"
        )
        hint.setTextFormat(Qt.RichText)
        hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        hint.setWordWrap(True)
        root.addWidget(hint)

        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton("确定")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        root.addLayout(row)

        self._load_values()

    # -------------------------------------------------- 软件设置页

    def _build_app_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        home_box = QGroupBox("工程存放位置")
        home_form = QFormLayout(home_box)
        home_row = QHBoxLayout()
        home_row.setSpacing(6)
        self.project_home_edit = QLineEdit()
        self.project_home_edit.setPlaceholderText(
            f"留空 = 自动查找（当前用的：{self._active_home}）"
        )
        self.project_home_edit.setToolTip(
            "工程（配置 / 数据 / 报表）统一放在这个目录下的一个工程一个子文件夹里，\n"
            "软件目录本身不存工程。留空则启动时自动查找：\n"
            "环境变量 VVPHMI_PROJECT_HOME → 从程序目录逐级向上找现成的 VVPproject →\n"
            "「文档」目录 → 都没有就在程序目录旁新建 VVPproject。\n"
            "改完下次启动生效。"
        )
        home_row.addWidget(self.project_home_edit, 1)
        browse = QPushButton("浏览…")
        browse.clicked.connect(self._pick_project_home)
        home_row.addWidget(browse)
        open_button = QPushButton("打开文件夹")
        open_button.setToolTip("在资源管理器里打开工程仓库")
        open_button.clicked.connect(self._open_project_home)
        home_row.addWidget(open_button)
        home_form.addRow("工程仓库", home_row)
        layout.addWidget(home_box)
        layout.addWidget(self._build_home_hint())

        window_box = QGroupBox("窗口")
        window_form = QFormLayout(window_box)
        self.remember_window = QCheckBox("记住窗口大小与位置")
        self.remember_window.setToolTip(
            "关窗口时把大小和位置存下来，下次启动还原。\n"
            "换成小屏或外接显示器拔掉时会自动压回屏幕内。"
        )
        window_form.addRow("", self.remember_window)
        self.fullscreen_box = QCheckBox("全屏模式（不显示标题栏与边框）")
        self.fullscreen_box.setToolTip(
            "勾上并确定后进入全屏，整个屏幕都给画面；下次启动也直接全屏。\n"
            "运行中随时按 F11 切换、Esc 退出全屏（那样也会记住）。"
        )
        window_form.addRow("", self.fullscreen_box)
        window_row = QHBoxLayout()
        window_row.setSpacing(6)
        self.window_size_label = QLabel("")
        self.window_size_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        window_row.addWidget(self.window_size_label, 1)
        reset_button = QPushButton("恢复默认大小")
        reset_button.setToolTip("窗口被拉得太大／跑到屏幕外面时点这个")
        reset_button.clicked.connect(self._request_reset_window)
        window_row.addWidget(reset_button)
        window_form.addRow("", window_row)
        layout.addWidget(window_box)

        start_box = QGroupBox("启动行为")
        start_form = QFormLayout(start_box)
        self.sample_combo = QComboBox()
        self.sample_combo.addItem(NO_SAMPLE, "")
        for name in self._samples:
            self.sample_combo.addItem(name, name)
        self.sample_combo.setToolTip("仅影响「没带参数启动」时载入哪个示例工程")
        start_form.addRow("首启载入示例", self.sample_combo)

        self.auto_sim = QCheckBox("启动后自动拉起内置仿真器")
        self.auto_sim.setToolTip("现场机器建议关闭；打开后会占用仿真端口")
        start_form.addRow("", self.auto_sim)

        self.auto_start = QCheckBox("启动后自动开始采集")
        start_form.addRow("", self.auto_start)

        self.confirm_exit = QCheckBox("退出前询问未保存的改动")
        start_form.addRow("", self.confirm_exit)
        layout.addWidget(start_box)

        comm_box = QGroupBox("通信")
        comm_form = QFormLayout(comm_box)
        self.reconnect_box = _double_spin(0.2, 600.0, 0.5, " 秒")
        self.reconnect_box.setToolTip("断线后每隔多久尝试重连一次")
        comm_form.addRow("断线重连间隔", self.reconnect_box)
        self.write_timeout_box = _double_spin(0.2, 60.0, 0.5, " 秒")
        self.write_timeout_box.setToolTip("一次写值最多等多久，超时算失败")
        comm_form.addRow("写值等待上限", self.write_timeout_box)
        layout.addWidget(comm_box)

        periodic_box = QGroupBox("持续发送")
        periodic_form = QFormLayout(periodic_box)
        self.periodic_interval_box = QSpinBox()
        self.periodic_interval_box.setRange(PERIODIC_MIN_MS, PERIODIC_MAX_MS)
        self.periodic_interval_box.setSingleStep(100)
        self.periodic_interval_box.setSuffix(" ms")
        self.periodic_interval_box.setToolTip("新建持续发送任务时的默认周期")
        periodic_form.addRow("默认周期", self.periodic_interval_box)

        self.periodic_confirm = QCheckBox("开始持续发送前二次确认")
        self.periodic_confirm.setToolTip("防止手滑让上位机一直往设备写值")
        periodic_form.addRow("", self.periodic_confirm)
        layout.addWidget(periodic_box)

        scope_box = QGroupBox("示波器与趋势")
        scope_form = QFormLayout(scope_box)
        self.timebase_combo = QComboBox()
        for label, seconds in TIMEBASE_OPTIONS:
            self.timebase_combo.addItem(label, seconds)
        scope_form.addRow("默认时基", self.timebase_combo)

        self.scope_all_points = QCheckBox("通道列表默认显示全部点位")
        self.scope_all_points.setToolTip(
            "关掉则只显示参数表里标记过「示波器=是」的点位；\n"
            "不管开关如何，任意点位都能通过右键「添加到示波器」加进去"
        )
        scope_form.addRow("", self.scope_all_points)

        self.trend_points_box = QSpinBox()
        self.trend_points_box.setRange(100, 200_000)
        self.trend_points_box.setSingleStep(500)
        scope_form.addRow("趋势点数上限", self.trend_points_box)
        layout.addWidget(scope_box)

        table_box = QGroupBox("表格与报文")
        table_form = QFormLayout(table_box)
        self.table_rows_box = QSpinBox()
        self.table_rows_box.setRange(20, 100_000)
        self.table_rows_box.setSingleStep(100)
        table_form.addRow("列表显示上限", self.table_rows_box)

        self.frames_box = QSpinBox()
        self.frames_box.setRange(100, 200_000)
        self.frames_box.setSingleStep(500)
        self.frames_box.setToolTip("通信监视页在内存里保留的报文条数")
        table_form.addRow("报文保留条数", self.frames_box)
        layout.addWidget(table_box)

        self.purge_box = QCheckBox("自动清理过久的历史数据")
        self.purge_box.setToolTip(
            "按上面的「历史保留」天数定期清理，默认打开。\n"
            "现场机器一跑几个月，历史库不清理迟早把硬盘写满；\n"
            "要留底就关掉这里（库会一直长大），或者把保留天数调大。"
        )
        layout.addWidget(self.purge_box)

        purge_hint = QLabel(
            "清理只按「历史保留」天数删过期的历史/报警/通信/录波数据，"
            "刚采到的数据不受影响；关掉就一条不删。"
        )
        purge_hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        purge_hint.setWordWrap(True)
        layout.addWidget(purge_hint)

        export_box = QGroupBox("导出与保存默认位置")
        export_form = QFormLayout(export_box)
        self.export_location_combo = QComboBox()
        self.export_location_combo.addItem("当前工程 / 报表目录", "project")
        self.export_location_combo.addItem("自选固定目录", "custom")
        self.export_location_combo.currentIndexChanged.connect(self._sync_export_directory)
        export_form.addRow("默认位置", self.export_location_combo)
        export_row = QHBoxLayout()
        self.export_directory_edit = QLineEdit()
        self.export_directory_edit.setPlaceholderText("自选固定目录")
        export_row.addWidget(self.export_directory_edit, 1)
        export_browse = QPushButton("浏览…")
        export_browse.clicked.connect(self._pick_export_directory)
        export_row.addWidget(export_browse)
        export_wrap = QWidget()
        export_wrap.setLayout(export_row)
        export_form.addRow("自选目录", export_wrap)
        layout.addWidget(export_box)
        layout.addStretch(1)
        return page

    def _sync_export_directory(self) -> None:
        custom = self.export_location_combo.currentData() == "custom"
        self.export_directory_edit.setEnabled(custom)

    def _pick_export_directory(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择默认导出目录", self.export_directory_edit.text().strip()
        )
        if chosen:
            self.export_directory_edit.setText(os.path.normpath(chosen))

    def _build_home_hint(self) -> QWidget:
        """说明工程仓库里长什么样 + 列出已经找到的几个工程。"""
        box = QGroupBox("已有的工程")
        layout = QVBoxLayout(box)
        layout.setSpacing(6)
        self.home_hint = QLabel()
        self.home_hint.setWordWrap(True)
        layout.addWidget(self.home_hint)
        self.home_list = QListWidget()
        self.home_list.setMaximumHeight(120)
        self.home_list.itemDoubleClicked.connect(self._open_listed_project)
        layout.addWidget(self.home_list)
        return box

    def _current_home(self) -> str:
        return self.project_home_edit.text().strip() or self._active_home

    def _request_reset_window(self) -> None:
        """把「恢复默认窗口大小」记成待办，确定时由主窗口执行。

        对话框拿不到主窗口的可写引用（只拿到不可写的父窗口），
        所以这里只发个标记，别去直接动别人的窗口。
        """
        self.reset_window_requested = True
        self.window_size_label.setText("已排队：点「确定」后立即恢复默认大小")

    def _refresh_home_list(self) -> None:
        home = self._current_home()
        exists = bool(home) and os.path.isdir(home)
        self.home_list.clear()
        projects = project_store.list_projects(home) if exists else []
        for folder in projects:
            item = QListWidgetItem(os.path.basename(folder))
            item.setData(Qt.UserRole, folder)
            item.setToolTip(folder)
            self.home_list.addItem(item)
        self.home_hint.setText(
            (
                f"<b>{home}</b><br>"
                f"找到 {len(projects)} 个工程（双击在资源管理器里打开）。"
                "每个工程文件夹里：<b>配置</b>（工程定义 / 参数表）、"
                "<b>数据</b>（历史库）、<b>报表</b>（导出的录波 / 截图 / 录音 / 数据记录）。"
            )
            if exists
            else "目录还不存在，启动时会在程序目录旁新建一个 VVPproject。"
        )

    def _pick_project_home(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择工程仓库目录（一个工程一个子文件夹）", self._current_home() or ""
        )
        if chosen:
            self.project_home_edit.setText(os.path.normpath(chosen))
            self._refresh_home_list()

    def _open_project_home(self) -> None:
        home = self._current_home()
        if not home or not os.path.isdir(home):
            QMessageBox.information(self, "提示", "这个目录还不存在，启动软件时会自动新建。")
            return
        open_in_file_manager(home)

    def _open_listed_project(self, item: QListWidgetItem) -> None:
        folder = item.data(Qt.UserRole)
        if folder and os.path.isdir(folder):
            open_in_file_manager(folder)

    # -------------------------------------------------- 工程设置页

    def _build_project_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        basic_box = QGroupBox("工程")
        basic_form = QFormLayout(basic_box)
        self.name_edit = QLineEdit()
        basic_form.addRow("工程名", self.name_edit)

        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("留空 = 按工程名自动生成")
        self.folder_edit.setToolTip(
            "磁盘上的**文件夹名，一律用英文**（命令行 / ZIP / 英文系统上不会乱码）。\n"
            "留空就按工程名自动生成：有英文就取英文，纯中文名会生成 project-1 这种。\n"
            "填了新名字，点「确定」后会把文件夹就地改名（搬不动就保持原名，不会丢数据）。\n"
            "上面「工程名」显示给别人看，中文照旧。"
        )
        basic_form.addRow("文件夹名", self.folder_edit)

        self.folder_hint = QLabel("")
        self.folder_hint.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        self.folder_hint.setWordWrap(True)
        basic_form.addRow("", self.folder_hint)

        self.archive_box = QSpinBox()
        self.archive_box.setRange(1, 3600)
        self.archive_box.setSuffix(" 秒")
        self.archive_box.setToolTip("即使数值没变化，也至少这么多久存一条历史（兜底）")
        basic_form.addRow("归档间隔", self.archive_box)

        self.retention_box = QSpinBox()
        self.retention_box.setRange(1, 3650)
        self.retention_box.setSuffix(" 天")
        basic_form.addRow("历史保留", self.retention_box)
        layout.addWidget(basic_box)

        storage_box = QGroupBox("数据存放")
        storage_layout = QVBoxLayout(storage_box)
        note = QLabel(
            "放工程目录内 => 整个工程可以打包成一个 .iprj 带走；\n"
            "指向自定义路径 => 松散模式，例如库放网络盘、参数表放共享目录给同事改。"
        )
        note.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        note.setWordWrap(True)
        storage_layout.addWidget(note)

        self.db_form = QFormLayout()
        self.db_inside = QCheckBox("历史库放工程目录内")
        self.db_inside.toggled.connect(self._sync_storage_rows)
        self.db_form.addRow("", self.db_inside)
        self.db_path_edit = QLineEdit()
        db_row = QHBoxLayout()
        db_row.addWidget(self.db_path_edit, 1)
        db_browse = QPushButton("浏览…")
        db_browse.clicked.connect(lambda: self._pick_path(self.db_path_edit, "选择历史库文件"))
        db_row.addWidget(db_browse)
        db_wrapper = QWidget()
        db_wrapper.setLayout(db_row)
        self.db_form.addRow("历史库路径", db_wrapper)
        storage_layout.addLayout(self.db_form)

        self.csv_form = QFormLayout()
        self.csv_inside = QCheckBox("参数表放工程目录内")
        self.csv_inside.toggled.connect(self._sync_storage_rows)
        self.csv_form.addRow("", self.csv_inside)
        self.csv_path_edit = QLineEdit()
        csv_row = QHBoxLayout()
        csv_row.addWidget(self.csv_path_edit, 1)
        csv_browse = QPushButton("浏览…")
        csv_browse.clicked.connect(lambda: self._pick_path(self.csv_path_edit, "选择参数表文件"))
        csv_row.addWidget(csv_browse)
        csv_wrapper = QWidget()
        csv_wrapper.setLayout(csv_row)
        self.csv_form.addRow("参数表路径", csv_wrapper)
        storage_layout.addLayout(self.csv_form)
        layout.addWidget(storage_box)

        periodic_box = QGroupBox("持续发送配置")
        periodic_layout = QVBoxLayout(periodic_box)
        pnote = QLabel(
            "工程里记住的周期写值配置。出于安全，打开工程时不会自动开始发送，\n"
            "需要在点位上右键或双击手动启动。"
        )
        pnote.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 11px;")
        pnote.setWordWrap(True)
        periodic_layout.addWidget(pnote)

        self.periodic_list = QListWidget()
        self.periodic_list.setMaximumHeight(120)
        periodic_layout.addWidget(self.periodic_list)

        periodic_row = QHBoxLayout()
        periodic_row.addStretch(1)
        remove_button = QPushButton("移除选中")
        remove_button.clicked.connect(self._remove_periodic)
        periodic_row.addWidget(remove_button)
        periodic_layout.addLayout(periodic_row)
        layout.addWidget(periodic_box)

        layout.addStretch(1)
        return page

    @staticmethod
    def _pick_path(target: QLineEdit, title: str) -> None:
        path, _ = QFileDialog.getSaveFileName(target, title, target.text())
        if path:
            target.setText(path)

    def _sync_storage_rows(self) -> None:
        self.db_path_edit.setEnabled(not self.db_inside.isChecked())
        self.csv_path_edit.setEnabled(not self.csv_inside.isChecked())

    def _remove_periodic(self) -> None:
        row = self.periodic_list.currentRow()
        if row < 0:
            return
        pid = self.periodic_list.item(row).data(Qt.UserRole)
        self.project.clear_periodic_write(pid)
        self._fill_periodic_list()

    def _fill_periodic_list(self) -> None:
        self.periodic_list.clear()
        for entry in self.project.periodic_writes:
            item = QListWidgetItem(
                f"{entry.pid}　值 {entry.value}　每 {entry.interval_ms} ms（未启动）"
            )
            item.setData(Qt.UserRole, entry.pid)
            self.periodic_list.addItem(item)
        if not self.project.periodic_writes:
            hint = QListWidgetItem("（暂无配置）")
            hint.setFlags(Qt.NoItemFlags)
            self.periodic_list.addItem(hint)

    # -------------------------------------------------- 载入与提交

    def _load_values(self) -> None:
        s = self.settings
        self.project_home_edit.setText(s.project_home or "")
        self._refresh_home_list()
        self.remember_window.setChecked(s.remember_window)
        self.fullscreen_box.setChecked(bool(s.fullscreen))
        self.window_size_label.setText(
            "下次启动会还原上次的大小与位置"
            if s.remember_window
            else "每次启动都按屏幕给默认大小"
        )
        index = self.sample_combo.findData(s.default_sample)
        self.sample_combo.setCurrentIndex(index if index >= 0 else 0)
        self.auto_sim.setChecked(s.auto_start_simulator)
        self.auto_start.setChecked(s.auto_start_engine)
        self.confirm_exit.setChecked(s.confirm_on_exit)
        self.reconnect_box.setValue(s.reconnect_interval_s)
        self.write_timeout_box.setValue(s.write_timeout_s)
        self.periodic_interval_box.setValue(s.periodic_default_interval_ms)
        self.periodic_confirm.setChecked(s.periodic_confirm)
        timebase_index = self.timebase_combo.findData(s.scope_timebase_s)
        self.timebase_combo.setCurrentIndex(timebase_index if timebase_index >= 0 else 2)
        self.scope_all_points.setChecked(s.scope_show_all_points)
        self.trend_points_box.setValue(s.trend_max_points)
        self.table_rows_box.setValue(s.table_rows_limit)
        self.frames_box.setValue(s.monitor_frames_limit)
        self.purge_box.setChecked(s.archive_auto_purge)
        export_index = self.export_location_combo.findData(s.export_location)
        self.export_location_combo.setCurrentIndex(max(0, export_index))
        self.export_directory_edit.setText(s.custom_export_directory)
        self._sync_export_directory()

        p = self.project
        self.name_edit.setText(p.name)
        self.folder_edit.setText(
            os.path.basename(self._project_root.rstrip("\\/"))
            if self._project_root
            else (p.folder_name or "")
        )
        self.folder_hint.setText(
            f"工程在：{self._project_root}" if self._project_root else "工程还没有落盘到文件夹"
        )
        self.archive_box.setValue(max(1, int(p.history_interval_s)))
        self.retention_box.setValue(max(1, int(p.history_retention_days)))
        self.db_inside.setChecked(p.storage.db_in_project)
        self.db_path_edit.setText(p.storage.db_path)
        self.csv_inside.setChecked(p.storage.csv_in_project)
        self.csv_path_edit.setText(p.storage.csv_path)
        self._sync_storage_rows()
        self._fill_periodic_list()

    # -------------------------------------------------- 交互动效

    def _on_appearance_changed(self, appearance) -> None:
        """实时预览：直接把外观套到正在运行的程序上。

        注意有些控件用内联样式（构造时取一次颜色），只换全局样式表不会更新它们，
        所以再发一个信号让主窗口重建页面 —— 否则会出现"标题在浅色主题下几乎看不见"。
        重建有成本，所以防抖一下：连续拖字号时只在停下手之后重建一次。
        """
        problems = theme.apply(appearance)
        self.appearance_changed = True
        self.setStyleSheet(theme.STYLESHEET)
        parent = self.parent()
        if parent is not None:
            parent.setStyleSheet(theme.STYLESHEET)
        if problems:
            self.appearance_editor.hint.setText(
                f"有 {len(problems)} 个颜色值不合法，已按默认值处理："
                + "、".join(problems[:5])
            )
        else:
            self.appearance_editor.hint.setText("改动立即预览，按「取消」可整页还原。")
        self._rebuild_timer.start()

    def _emit_appearance_changed(self) -> None:
        try:
            self.appearanceChanged.emit()
        except RuntimeError:
            pass  # 窗口已关闭

    def _on_channels_changed(self) -> None:
        """通道改动后刷新工程设置页里的持续发送列表（点位可能已失效）。"""
        self._fill_periodic_list()

    def reject(self) -> None:  # noqa: D102
        """取消时把外观还原成进入窗口前的样子。"""
        self._rebuild_timer.stop()
        if self.appearance_changed:
            theme.apply(self._original_appearance)
            parent = self.parent()
            if parent is not None:
                parent.setStyleSheet(theme.STYLESHEET)
            self.appearanceChanged.emit()
        super().reject()

    def accept(self) -> None:  # noqa: D102
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "工程名不能为空", "请填写工程名。")
            return

        # 文件夹名必须是英文（磁盘目录一律 ASCII）
        typed = self.folder_edit.text().strip()
        wanted = project_store.folder_name(typed, fallback="") if typed else ""
        if typed and not wanted:
            QMessageBox.warning(
                self,
                "文件夹名要用英文",
                "磁盘上的文件夹名只能包含英文字母、数字、- 和 _。\n\n"
                "「工程名」可以继续用中文（界面上显示的就是它）。",
            )
            self.folder_edit.setFocus()
            return
        current = os.path.basename(self._project_root.rstrip("\\/")) if self._project_root else ""
        if wanted and current and wanted != current:
            self.folder_rename_to = wanted
        self.project.folder_name = wanted

        # 通信页的表单也要落盘（最后一个字段可能还在编辑中没触发 editingFinished）
        self.channel_editor.apply_form(silent=True)

        s = self.settings
        s.appearance = self.appearance_editor.current_appearance().to_dict()
        self.appearance_changed = False
        s.project_home = os.path.normpath(self.project_home_edit.text().strip()) if self.project_home_edit.text().strip() else ""
        s.remember_window = self.remember_window.isChecked()
        s.fullscreen = self.fullscreen_box.isChecked()
        if not s.remember_window:
            s.window_geometry = ""
        s.default_sample = self.sample_combo.currentData() or ""
        s.auto_start_simulator = self.auto_sim.isChecked()
        s.auto_start_engine = self.auto_start.isChecked()
        s.confirm_on_exit = self.confirm_exit.isChecked()
        s.reconnect_interval_s = self.reconnect_box.value()
        s.write_timeout_s = self.write_timeout_box.value()
        s.periodic_default_interval_ms = self.periodic_interval_box.value()
        s.periodic_confirm = self.periodic_confirm.isChecked()
        s.scope_timebase_s = float(self.timebase_combo.currentData() or 5.0)
        s.scope_show_all_points = self.scope_all_points.isChecked()
        s.trend_max_points = self.trend_points_box.value()
        s.table_rows_limit = self.table_rows_box.value()
        s.monitor_frames_limit = self.frames_box.value()
        s.archive_auto_purge = self.purge_box.isChecked()
        s.export_location = str(self.export_location_combo.currentData() or "project")
        s.custom_export_directory = self.export_directory_edit.text().strip()
        # 下次「全部设置…」从这次停留的页签打开
        s.settings_tab = self.tabs.currentIndex()
        s.clamp()

        p = self._source_project
        p.name = name
        p.folder_name = self.project.folder_name
        p.history_interval_s = self.archive_box.value()
        p.history_retention_days = self.retention_box.value()
        p.storage = ProjectStorage(
            db_in_project=self.db_inside.isChecked(),
            db_path=self.db_path_edit.text().strip() or "history.db",
            csv_in_project=self.csv_inside.isChecked(),
            csv_path=self.csv_path_edit.text().strip() or "parameters.csv",
        )
        # 通信页改的是副本，这里把通道写回真工程；设备也要一起搬 ——
        # 通道改名时设备是按通道名挂载的，只换通道不搬设备会让设备"掉线"
        p.channels = list(self.project.channels)
        for target, edited in zip(p.devices, self.project.devices):
            target.channel = edited.channel
        p.periodic_writes = list(self.project.periodic_writes)
        super().accept()
