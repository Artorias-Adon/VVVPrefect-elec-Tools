"""主窗口。

负责把各页面与采集引擎装起来，并管理工程的打开/保存：
* 工程用 :mod:`icomm.core.project_store` 打开保存，支持**工程目录模式**、
  **松散模式**（库/参数表各自指定路径）与 **.iprj 单文件工程包**；
* 主页示意图上的节点点击后跳到对应设备详情或页面；
* 采集引擎、存储、各页面在切换工程时统一重建。
"""

from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QSizePolicy,
    QStackedWidget,
    QWidget,
)

from .. import APP_DESCRIPTION, APP_NAME, APP_SHORT, APP_VERSION, __copyright__, __license__
from ..core import csv_table, project as project_io, project_store, writers
from ..core import settings as app_settings
from ..core.automation import AutomationService
from ..core.engine import AcquisitionEngine
from ..core.fault import FaultRecorder
from ..core.models import DISPLAY_BASES, Project, normalize_base, point_id
from ..core.tags import TagValue
from ..storage import Storage
from . import theme
from . import tools as ui_tools
from .bridge import EngineBridge
from .dialogs.appearance_editor import AppearanceEditor  # noqa: F401  (供外部引用)
from .dialogs.automation_panel import AutomationPanel
from .dialogs.channel_editor import ChannelEditor  # noqa: F401
from .dialogs import control_panel
from .dialogs.periodic_panel import PeriodicPanel
from .dialogs.point_dialog import PointDialog
from .dialogs.save_as_dialog import SaveAsDialog
from .dialogs.settings_dialog import SettingsDialog
from .dialogs.sizing import fit_to_available_screen
from .dialogs.write_limit_dialog import WriteLimitDialog
from .pages.alarms import AlarmPage
from .pages.dashboard import DashboardPage
from .pages.device_detail import DeviceDetailPage
from .pages.devices import DevicesPage
from .pages.monitor import MonitorPage
from .pages.overview import OverviewPage
from .pages.scope import ScopePage
from .pages.trend import TrendPage
from .qtboot import prepare_qt
from .tool_dock import TOOL_PAGE_PREFIX, NavList, install_page_grip
from .widgets import ElidedLabel

NAV_ITEMS = ["主页", "运行监视", "示波器", "历史趋势", "报警记录", "通信监视", "设备与点位"]

#: 设置窗口的四个页签（顺序要与 SettingsDialog 里 addTab 的顺序一致）。
#: 「设置」菜单按这个表逐条列出，点一条就直接打开对应的那一页。
SETTINGS_TABS: tuple[tuple[str, str], ...] = (
    ("界面外观", "配色主题、字体、字号、界面密度与逐项颜色"),
    ("通信", "各通道的协议与连接参数（TCP / RTU），新建与删除通道"),
    ("软件设置", "工程存放位置、最近工程、导出位置、窗口、通信超时、示波器默认值"),
    ("工程设置", "工程名与文件夹名、归档间隔、历史保留天数、库与参数表路径"),
)
IPRJ_FILTER = f"{APP_SHORT} 工程包 (*{project_store.IPRJ_SUFFIX});;工程文件 (project.json);;所有文件 (*)"


class _WriteEmitter(QObject):
    """写值结果从工作线程回到界面线程。"""

    finished = Signal(str, bool, str)


class MainWindow(QMainWindow):
    def __init__(
        self,
        project: Project,
        paths: project_store.ProjectPaths,
        workdir: str = "",
        base_dir: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        prepare_qt()
        self.project = project
        self.paths = paths
        self.workdir = workdir or project_store.default_workdir()
        #: 程序基准目录：软件设置（settings.json）放在这里
        self.base_dir = base_dir or self.workdir
        self.settings = app_settings.load_settings(self.base_dir)
        # 外观要在建控件之前套用，否则第一批控件用的是默认配色
        theme.apply(theme.Appearance.from_dict(self.settings.appearance))
        self._apply_window_icon()
        self.storage: Storage | None = None
        self.engine: AcquisitionEngine | None = None
        #: 定期清理过久的历史数据（按设置的开关启停，间隔见设置里的分钟数）
        self.purge_timer = QTimer(self)
        self.purge_timer.setInterval(30 * 60 * 1000)
        self.purge_timer.timeout.connect(self._auto_purge)
        self.bridge = EngineBridge()
        self._simulator = None
        #: CAN 虚拟总线装置 / IEC 61850 仿真装置（跟主仿真器开关一起起停）
        self._can_sim = None
        self._iec61850_sim = None
        self._s7_sim = None
        self._dirty = False
        self._point_dialog: PointDialog | None = None
        #: 自动化核心：截屏/数据记录/录音/故障录波/定时赋值共用
        self.automation = AutomationService()
        #: 故障录波的环形缓冲：每个采集批次喂一次（在 _on_tags 里），
        #: 触发与成段由「故障录波」工具配置
        self.fault_recorder = FaultRecorder()
        self._write_emitter = _WriteEmitter()
        self._write_emitter.finished.connect(self._on_write_finished)

        self.setWindowTitle("")
        self._fit_initial_size()
        self.setStyleSheet(theme.STYLESHEET)

        self._build_pages()
        self._build_menus()
        self._build_statusbar()
        self._connect_bridge()
        self._attach_storage()
        self._attach_engine()
        self.scope_page.set_preferences(self.settings)
        self._update_title()
        self._remember_project_path()
        # 上次钉在左侧栏的工具页恢复回来
        self.restore_embedded_tools()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(2000)

        # 时钟单独一个 1 秒的定时器：状态栏那个 2 秒刷新要查数据库，
        # 为了秒针不跳格就把它改成 1 秒不划算
        self._clock_timer = QTimer(self)
        self._clock_timer.timeout.connect(self._refresh_clock)
        self._clock_timer.start(1000)
        self._refresh_clock()

        # 自动化触发判断：300ms 一次足够（现场条件是通道量，不是高频信号），
        # 再快也只是白烧 CPU。动作跑在界面线程，方便直接操作界面。
        self._automation_timer = QTimer(self)
        self._automation_timer.timeout.connect(self._tick_automation)
        self._automation_timer.start(300)

    # -------------------------------------------------- 界面搭建

    def _apply_window_icon(self) -> None:
        """套用程序图标（assets/icon.ico，打包后随 exe 同级或内嵌）。"""
        from .resources import app_icon

        icon = app_icon()
        if icon is not None and not icon.isNull():
            self.setWindowIcon(icon)

    def _fit_initial_size(self) -> None:
        """决定窗口初始大小：能恢复上次的就恢复，否则按屏幕可用区域给个合适的大小。

        以前这里只"别要得太大"，但真正的下限是**布局**算出来的最小宽度 ——
        工具栏与页面里的长文字曾把最小宽度顶到 2800 像素，小屏上窗口会直接被撑出屏幕
        （实测用户 1920×1080 屏上窗口比屏幕还宽、右边被切掉）。
        现在长文字统一换成会省略/折行的 :class:`~icomm.ui.widgets.ElidedLabel`，
        最小宽度降到 1000 出头，这条路径才真正可控。
        """
        if self._restore_window_geometry():
            return
        preferred_width, preferred_height = 1440, 900
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            self.resize(preferred_width, preferred_height)
            return
        available = screen.availableGeometry()
        width = max(880, min(preferred_width, available.width() - 60))
        height = max(600, min(preferred_height, available.height() - 60))
        self.resize(width, height)
        self.move(
            available.x() + max(0, (available.width() - width) // 2),
            available.y() + max(0, (available.height() - height) // 2),
        )

    def _restore_window_geometry(self) -> bool:
        """恢复上次的窗口大小与位置。不合适（屏幕变了/跑到屏幕外）就当没有。"""
        if not self.settings.remember_window or not self.settings.window_geometry:
            return False
        from PySide6.QtCore import QByteArray

        raw = QByteArray.fromBase64(self.settings.window_geometry.encode("ascii", "ignore"))
        if not self.restoreGeometry(raw):
            return False
        # 屏幕换了（外接显示器拔了）可能落到屏幕外，这时退回居中默认大小
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            frame = self.frameGeometry()
            if not screen.availableGeometry().intersects(frame):
                self.settings.window_geometry = ""
                return False
            # 比屏幕还大的旧记录也要压回来（以前版本可能存下过超宽的窗口）
            available = screen.availableGeometry()
            width = min(self.width(), available.width())
            height = min(self.height(), available.height())
            if (width, height) != (self.width(), self.height()):
                self.resize(width, height)
        return True

    def reset_window_size(self) -> None:
        """忘掉记住的窗口大小，按屏幕重新给一个合适的（窗口被弄乱了时用）。"""
        self.settings.window_geometry = ""
        self.showNormal()
        self._fit_initial_size()
        self._save_settings_quietly()
        self._log_status("已恢复默认窗口大小")

    def _remember_window_geometry(self) -> None:
        if not self.settings.remember_window:
            return
        try:
            self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        except (ValueError, TypeError):
            self.settings.window_geometry = ""

    def _build_pages(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        #: 钉进左侧栏的工具页面：工具名 -> 页面控件
        self._embedded: dict[str, QWidget] = {}

        self.nav = NavList()
        self.nav.setObjectName("nav")
        self.nav.setFixedWidth(150)
        self.nav.setContextMenuPolicy(Qt.CustomContextMenu)
        self.nav.customContextMenuRequested.connect(self._show_nav_menu)
        self.nav.toolDropped.connect(self.embed_tool)
        for index, name in enumerate(NAV_ITEMS):
            self._add_nav_item(name, ("page", index))
        self.nav.setCurrentRow(0)
        self.nav.currentRowChanged.connect(self._on_nav)
        layout.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.overview_page = OverviewPage(self.project, self.paths.config_dir or self.paths.root)
        self.dashboard_page = DashboardPage(
            self.project, order=self.settings.display_order
        )
        self.scope_page = ScopePage(self.project, self.storage)
        self.scope_page.output_path = self.output_path
        self.trend_page = TrendPage(self.project, self.storage)
        self.alarm_page = AlarmPage(self.storage)
        self.monitor_page = MonitorPage(self.storage)
        self.devices_page = DevicesPage(self.project)
        self.device_detail_page = DeviceDetailPage(self.project)

        for page in (
            self.overview_page,
            self.dashboard_page,
            self.scope_page,
            self.trend_page,
            self.alarm_page,
            self.monitor_page,
            self.devices_page,
        ):
            self.stack.addWidget(page)

        # 设备详情不进导航栏，由示意图点击进入
        self.stack.addWidget(self.device_detail_page)

        self.overview_page.nodeActivated.connect(self._on_diagram_action)
        self.dashboard_page.writeRequested.connect(self._on_write_requested)
        self.dashboard_page.waveActivated.connect(self._open_point_in_scope)
        self.dashboard_page.orderChanged.connect(self._on_dashboard_order)
        self.device_detail_page.writeRequested.connect(self._on_write_requested)
        self.device_detail_page.backRequested.connect(lambda: self.nav.setCurrentRow(0))
        for page in (self.dashboard_page, self.device_detail_page):
            page.editRequested.connect(self.open_point_dialog)
            page.contextRequested.connect(self.show_point_menu)
        self.device_detail_page.controlRequested.connect(self.show_device_control_panel)
        self.devices_page.projectChanged.connect(self._on_project_edited)
        self.devices_page.exportCsvRequested.connect(lambda: self.export_csv())
        self.devices_page.importCsvRequested.connect(lambda: self.import_csv())
        self.devices_page.generateCsvRequested.connect(lambda: self.generate_csv_template())

        layout.addWidget(self.stack, 1)

    def _build_menus(self) -> None:
        """左上角菜单栏：文件 / 编辑 / 选择 / 工具 / 控制台 / 设置 / 关于。

        动作对象（``self.action_start`` 等）照旧挂在窗口上，菜单只管摆放位置 ——
        这样老代码、界面探针、测试里对 ``action_*`` 的引用都不会因为改布局而失效。
        """
        # ---------------------------------------------- 动作对象
        self.action_start = QAction("▶ 启动采集", self)
        self.action_start.triggered.connect(self.start_engine)

        self.action_stop = QAction("■ 停止采集", self)
        self.action_stop.triggered.connect(self.stop_engine)
        self.action_stop.setEnabled(False)

        self.action_restart = QAction("⟳ 重启采集", self)
        self.action_restart.triggered.connect(self.restart_engine)

        self.action_open = QAction("打开工程…", self)
        self.action_open.setToolTip("打开工程包 / 工程文件 / 工程目录")
        self.action_open.triggered.connect(self.open_project)

        self.action_save = QAction("保存工程", self)
        self.action_save.setToolTip("把整个工程保存到当前位置（含参数表、历史库）")
        self.action_save.triggered.connect(self.save_project)

        self.action_sim = QAction("启动内置仿真器", self)
        self.action_sim.setToolTip("不接硬件也能出数据：本地跑一台 Modbus TCP 从站")
        self.action_sim.setCheckable(True)
        self.action_sim.toggled.connect(self._toggle_simulator)

        self.action_periodic = QAction("持续发送…", self)
        self.action_periodic.setToolTip("查看并管理正在周期性写入设备的点位")
        self.action_periodic.triggered.connect(self.show_periodic_overview)

        self.action_automation = QAction("自动化任务…", self)
        self.action_automation.setToolTip(
            "查看所有「满足条件就做点什么」的任务（截屏、数据记录等）"
        )
        self.action_automation.triggered.connect(self.show_automation_overview)

        self.action_control = QAction("按键控制台…", self)
        self.action_control.setToolTip("调出小窗口按键控制面板（可同时开多个）")
        self.action_control.triggered.connect(self.show_control_panel_picker)

        self.action_settings = QAction("全部设置…", self)
        self.action_settings.setToolTip("界面外观、通信参数、软件与工程设置（打开上次停留的那一页）")
        # triggered 会带一个 bool，别让它被当成页签号
        self.action_settings.triggered.connect(lambda: self.open_settings())

        self.action_about = QAction("关于…", self)
        self.action_about.triggered.connect(self._show_about)

        # ---------------------------------------------- 菜单栏
        bar = self.menuBar()
        bar.setObjectName("mainMenuBar")

        # ---- 文件：工程的新建 / 打开 / 保存都收在这一处
        file_menu = bar.addMenu("文件(&F)")
        file_menu.addAction(self.action_open)
        file_menu.addAction(self.action_save)
        self.recent_menu = file_menu.addMenu("最近打开的工程")
        self.recent_menu.aboutToShow.connect(self._refresh_recent_projects_menu)
        self._refresh_recent_projects_menu()
        save_as_menu = file_menu.addMenu("保存为…")
        self._menu_action(
            save_as_menu,
            "保存参数设置…",
            self.save_parameters_as,
            "只存点位定义（地址/类型/报警…），可选 CSV / TXT / JSON",
        )
        self._menu_action(
            save_as_menu,
            "保存整个工程…",
            self.save_project_as,
            "工程定义 + 参数表 + 历史库，可选工程目录或 .iprj 单文件",
        )
        file_menu.addSeparator()
        new_menu = file_menu.addMenu("新建 / 示例")
        self._menu_action(new_menu, "新建空白工程", self.new_blank_project)
        new_menu.addSeparator()
        for name in project_io.BUILTIN_SAMPLES:
            self._menu_action(
                new_menu,
                f"载入示例：{name}",
                lambda _checked=False, sample=name: self.load_sample(sample),
            )
        file_menu.addSeparator()
        self._menu_action(
            file_menu,
            "打开工程所在文件夹",
            self._open_project_folder,
            "在文件资源管理器里定位当前工程目录",
        )
        file_menu.addSeparator()
        self._menu_action(file_menu, "退出", self.close, "退出前会先把设置与工程收好")

        # ---- 编辑：点位 / 参数表这一层
        edit_menu = bar.addMenu("编辑(&E)")
        self._menu_action(
            edit_menu, "导入参数表…", self.import_parameters, "按扩展名自动识别格式（CSV / JSON）"
        )
        self._menu_action(
            edit_menu, "导出参数表模板", self.generate_csv_template, "给同事照着填的空表"
        )
        self._menu_action(edit_menu, "导出参数表（当前点位）", self.export_csv)
        edit_menu.addSeparator()
        self._menu_action(
            edit_menu,
            "设备与点位配置",
            lambda: self.nav.setCurrentRow(NAV_ITEMS.index("设备与点位")),
            "增删通道 / 设备 / 点位，改地址与报警",
        )

        # ---- 选择：去哪个页面 / 看哪个点位
        select_menu = bar.addMenu("选择(&S)")
        for index, name in enumerate(NAV_ITEMS):
            self._menu_action(
                select_menu,
                name,
                lambda _checked=False, row=index: self.nav.setCurrentRow(row),
            )
        select_menu.addSeparator()
        self._menu_action(select_menu, "上一个页面", lambda: self._step_page(-1))
        self._menu_action(select_menu, "下一个页面", lambda: self._step_page(1))

        # ---- 工具：完全由注册表生成，加工具不用改这个文件
        tools_menu = bar.addMenu("工具(&T)")
        self.fill_tools_menu(tools_menu)

        # ---- 控制台：采集启停 + 各种"运行期窗口"
        console_menu = bar.addMenu("控制台(&C)")
        console_menu.addAction(self.action_start)
        console_menu.addAction(self.action_stop)
        console_menu.addAction(self.action_restart)
        console_menu.addSeparator()
        console_menu.addAction(self.action_sim)
        console_menu.addSeparator()
        console_menu.addAction(self.action_control)
        console_menu.addAction(self.action_periodic)
        console_menu.addAction(self.action_automation)
        console_menu.addSeparator()
        self._menu_action(
            console_menu,
            "通信监视",
            lambda: self.nav.setCurrentRow(NAV_ITEMS.index("通信监视")),
            "看原始请求/响应报文",
        )

        # ---- 设置：四个分块直接摆在菜单里，点哪块就打开哪一页
        settings_menu = bar.addMenu("设置(&O)")
        for index, (title, tip) in enumerate(SETTINGS_TABS):
            self._menu_action(
                settings_menu,
                f"{title}…",
                lambda _checked=False, tab=index: self.open_settings(tab),
                tip,
            )
        settings_menu.addSeparator()
        settings_menu.addAction(self.action_settings)
        settings_menu.addSeparator()
        self._menu_action(
            settings_menu, "恢复默认外观", self._restore_default_appearance, "换回「天蓝」主题与默认字号"
        )
        self._menu_action(settings_menu, "重置窗口大小", self.reset_window_size)

        # ---- 关于
        about_menu = bar.addMenu("关于(&A)")
        about_menu.addAction(self.action_about)

        # 采集控制只保留在「控制台」菜单，避免角落控件挡住菜单并造成按钮失效。
        self.mode_label = QLabel("未运行")
        self.mode_label.setStyleSheet(f"color: {theme.TEXT_DIM}; padding-right: 10px;")
        bar.setCornerWidget(self.mode_label, Qt.TopRightCorner)

    def _menu_action(self, menu: QMenu, text: str, slot, tip: str = "") -> QAction:
        """往菜单里加一条动作。

        菜单项一多，逐条 ``QAction(...) + connect + addAction`` 会写成一长串，
        这里收成一行，读起来就是"菜单里有哪些项"。
        """
        action = QAction(text, self)
        if tip:
            action.setToolTip(tip)
            action.setStatusTip(tip)
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    def _step_page(self, delta: int) -> None:
        """在导航栏那几页之间往前/往后翻（设备详情页不在循环里，工具页算在内）。"""
        count = self.nav.count()
        if count <= 0:
            return
        current = self.nav.currentRow()
        if current < 0:
            current = 0
        self.nav.setCurrentRow((current + delta) % count)

    def _open_project_folder(self) -> None:
        """在文件资源管理器里定位当前工程目录（交付/找文件时最常用的一步）。"""
        target = self.paths.root or self.base_dir or ""
        if not target or not os.path.isdir(target):
            self._log_status(f"找不到工程目录：{target or '（未保存）'}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def _restore_default_appearance(self) -> None:
        """把外观恢复成默认主题（改乱了、或者想回到出厂样子）。"""
        theme.apply_preset(theme.DEFAULT_PRESET)
        self.settings.appearance = theme.Appearance()
        self._save_settings_quietly()
        self._refresh_appearance()
        self._log_status(f"外观已恢复默认（{theme.DEFAULT_PRESET}）")

    def _build_statusbar(self) -> None:
        self.status_device = QLabel("设备 0/0 在线")
        self.status_storage = QLabel("归档 0 条")
        # 这里放的是完整工程路径，很长 —— 用会省略的标签，别让它把窗口顶宽
        self.status_path = ElidedLabel("")
        self.status_last = QLabel("就绪")
        # 现场对时间用得多：状态栏常驻一个时钟，做试验时不用切窗口看
        self.status_clock = QLabel("")
        self.status_clock.setToolTip("当前时间（右键或工具栏「工具 → 计时器与定时赋值」可以掐表）")
        for widget in (self.status_device, self.status_storage, self.status_path, self.status_last):
            widget.setStyleSheet(f"color: {theme.TEXT_DIM}; padding: 0 10px;")
        self.status_clock.setStyleSheet(f"color: {theme.TEXT_DIM}; padding: 0 10px;")
        bar = self.statusBar()
        bar.addWidget(self.status_device)
        bar.addWidget(self.status_storage)
        bar.addPermanentWidget(self.status_path, 1)
        bar.addPermanentWidget(self.status_last)
        bar.addPermanentWidget(self.status_clock)

    def _connect_bridge(self) -> None:
        self.bridge.tagsUpdated.connect(self._on_tags)
        self.bridge.deviceStateChanged.connect(self._on_device_state)
        self.bridge.alarmRaised.connect(self._on_alarm)
        self.bridge.frameReceived.connect(self.monitor_page.on_frame)
        self.bridge.logReceived.connect(self._on_log)

    # -------------------------------------------------- 存储与引擎

    def _attach_storage(self) -> None:
        self._detach_storage()
        self.storage = Storage(self.paths.db_file)
        self.storage.start()
        self._auto_purge()
        self._sync_purge_timer()

        for page in (self.scope_page, self.trend_page, self.alarm_page, self.monitor_page):
            page.set_storage(self.storage)

    def _detach_storage(self) -> None:
        self.purge_timer.stop()
        if self.storage is not None:
            self.storage.stop()
            self.storage = None

    def _sync_purge_timer(self) -> None:
        """按设置决定要不要周期性清理历史数据（关掉开关立刻停）。"""
        if not self.settings.archive_auto_purge:
            self.purge_timer.stop()
            return
        minutes = max(1, int(self.settings.archive_purge_interval_minutes))
        self.purge_timer.setInterval(minutes * 60 * 1000)
        if self.storage is not None:
            self.purge_timer.start()

    def _auto_purge(self) -> None:
        """按工程的「历史保留天数」清掉过久的历史数据。

        现场机器一跑几个月，历史库不清理迟早把硬盘写满。清理失败只记一条日志，
        不影响采集 —— 库被别的进程占着之类的情况不该把整个软件拖住。
        """
        if self.storage is None or not self.settings.archive_auto_purge:
            return
        days = int(self.project.history_retention_days or 0)
        if days <= 0:
            return
        try:
            removed = self.storage.purge(days)
        except Exception as error:  # noqa: BLE001 - 清理失败不能影响采集
            self._log_status(f"自动清理历史数据失败：{error}")
            return
        if removed:
            self.storage.log(
                "软件", "info", f"自动清理：删除 {removed} 条超过 {days} 天的历史数据"
            )
            self._log_status(f"已自动清理 {removed} 条历史数据（保留 {days} 天）")

    def _attach_engine(self) -> None:
        self.engine = AcquisitionEngine(
            self.project,
            storage=self.storage,
            reconnect_interval_s=self.settings.reconnect_interval_s,
            write_timeout_s=self.settings.write_timeout_s,
        )
        self.bridge.attach(self.engine)

    def start_engine(self) -> None:
        if self.engine is None or self.engine.running:
            return
        self.engine.restart(self.project)
        self.action_start.setEnabled(False)
        self.action_stop.setEnabled(True)
        self.mode_label.setText("采集中")
        self.mode_label.setStyleSheet(f"color: {theme.ACCENT}; padding-right: 10px;")
        self._log_status("采集已启动")

    def stop_engine(self) -> None:
        if self.engine is not None:
            self.engine.stop()
        self.action_start.setEnabled(True)
        self.action_stop.setEnabled(False)
        self.mode_label.setText("已停止")
        self.mode_label.setStyleSheet(f"color: {theme.TEXT_DIM}; padding-right: 10px;")
        self._log_status("采集已停止")

    def restart_engine(self) -> None:
        self.stop_engine()
        self.start_engine()

    # -------------------------------------------------- 数据分发

    def _on_tags(self, values: dict) -> None:
        # 故障录波要按采集节奏往回记，所以挂在标签更新上（比自动化那 300ms 更细）
        self.fault_recorder.feed(values)
        self.dashboard_page.on_tags(values)
        self.scope_page.on_tags(values)
        self.trend_page.on_tags(values)
        self.device_detail_page.on_tags(values)
        self.overview_page.set_values(values)

    def _on_device_state(self, status) -> None:
        self.dashboard_page.on_device_state(status)
        self.device_detail_page.on_device_state(status)

    def _on_alarm(self, event) -> None:
        self.alarm_page.on_alarm(event)
        self._log_status(event.message)

    def _on_log(self, channel: str, level: str, message: str) -> None:
        self.monitor_page.on_log(channel, level, message)
        if level in ("warning", "error"):
            self._log_status(f"[{channel}] {message}")

    def _log_status(self, text: str) -> None:
        self.status_last.setText(f"{time.strftime('%H:%M:%S')} {text}")

    # -------------------------------------------------- 导航

    def _add_nav_item(self, text: str, data: tuple) -> QListWidgetItem:
        """往左侧栏加一条。``data`` 决定这一条是什么：``("page", 序号)`` 或 ``("tool", 工具名)``。"""
        item = QListWidgetItem(text)
        item.setData(Qt.UserRole, data)
        if data[0] == "tool":
            item.setToolTip(f"工具页「{data[1]}」　右键可以移出左侧栏")
        self.nav.addItem(item)
        return item

    def _nav_row_of_tool(self, name: str) -> int:
        for row in range(self.nav.count()):
            item = self.nav.item(row)
            if item is not None and item.data(Qt.UserRole) == ("tool", name):
                return row
        return -1

    def _on_nav(self, row: int) -> None:
        item = self.nav.item(row) if row >= 0 else None
        if item is None:
            return
        data = item.data(Qt.UserRole)
        if isinstance(data, tuple) and len(data) == 2:
            kind, value = data
            if kind == "page":
                page = self.stack.widget(int(value))
                if page is not None:
                    self.stack.setCurrentWidget(page)
                return
            if kind == "tool":
                page = self._embedded.get(str(value))
                if page is not None:
                    self.stack.setCurrentWidget(page)
                return
        self.stack.setCurrentWidget(self.stack.widget(row))

    # -------------------------------------------------- 工具页（钉进左侧栏）

    def _show_nav_menu(self, pos) -> None:
        """左侧栏右键：移出工具页 / 把某个工具钉进来。"""
        row = self.nav.indexAt(pos).row()
        item = self.nav.item(row) if row >= 0 else None
        data = item.data(Qt.UserRole) if item is not None else None

        menu = QMenu(self)
        if isinstance(data, tuple) and data[0] == "tool":
            name = str(data[1])
            open_action = menu.addAction("切到这个工具页")
            open_action.triggered.connect(lambda: self.show_tool_page(name))
            remove = menu.addAction(f"把「{name}」移出左侧栏")
            remove.triggered.connect(lambda: self.remove_tool_page(name))
            menu.addSeparator()

        candidates = [tool for tool in ui_tools.tools() if tool.non_modal]
        if candidates:
            submenu = menu.addMenu("把工具钉到左侧栏")
            for tool in candidates:
                action = submenu.addAction(tool.name)
                if tool.name in self._embedded:
                    action.setEnabled(False)
                    action.setText(f"{tool.name}（已在左侧栏）")
                else:
                    action.triggered.connect(
                        lambda _checked=False, name=tool.name: self.embed_tool(name)
                    )
        if menu.actions():
            menu.exec(self.nav.mapToGlobal(pos))

    def embed_tool(self, name: str) -> bool:
        """把一个非模态工具钉成左侧栏里的页面（拖进来或点「钉住」都走这里）。"""
        tool = ui_tools.find_tool(name)
        if tool is None:
            self._log_status(f"没有叫「{name}」的工具")
            return False
        if not tool.non_modal:
            QMessageBox.information(
                self,
                "这个工具不能钉",
                f"「{name}」是对话框类工具（打开时不能操作主界面），\n所以只能单独打开，不能放到左侧栏。",
            )
            return False
        if name in self._embedded:
            self.show_tool_page(name)
            return True

        try:
            page = tool.build(self.tool_context())
        except Exception as exc:  # 单个工具出问题不该把主窗口带崩
            QMessageBox.critical(self, "工具出错", f"「{name}」打开失败：\n{exc}")
            return False

        self.stack.addWidget(page)
        self._embedded[name] = page
        self._add_nav_item(f"{TOOL_PAGE_PREFIX}{name}", ("tool", name))
        self._remember_embedded_tools()

        # 浮动窗口还开着就关掉，免得同一份内容有两处
        floating = type(tool)._window
        if floating is not None:
            try:
                floating.close()
            except RuntimeError:
                pass
            type(tool)._window = None

        self.show_tool_page(name)
        self._log_status(f"「{name}」已钉到左侧栏（右键可移出）")
        return True

    def show_tool_page(self, name: str) -> bool:
        page = self._embedded.get(name)
        row = self._nav_row_of_tool(name)
        if page is None or row < 0:
            return False
        self.nav.setCurrentRow(row)
        self.stack.setCurrentWidget(page)
        return True

    def remove_tool_page(self, name: str) -> bool:
        page = self._embedded.pop(name, None)
        row = self._nav_row_of_tool(name)
        if row >= 0:
            self.nav.takeItem(row)
        if page is not None:
            if self.stack.currentWidget() is page:
                self.nav.setCurrentRow(0)
            self.stack.removeWidget(page)
            page.deleteLater()
        self._remember_embedded_tools()
        if page is not None:
            self._log_status(f"「{name}」已移出左侧栏")
            return True
        return False

    def _remember_embedded_tools(self) -> None:
        self.settings.embedded_tools = list(self._embedded)
        self._save_settings_quietly()

    def restore_embedded_tools(self) -> None:
        """按设置里记的名单把工具页恢复回来（启动时、换工程后各调一次）。"""
        for name in list(self.settings.embedded_tools):
            if name in self._embedded:
                continue
            tool = ui_tools.find_tool(name)
            if tool is None or not tool.non_modal:
                continue
            try:
                page = tool.build(self.tool_context())
            except Exception:  # noqa: BLE001 - 工具坏了不该拖垮启动
                continue
            self.stack.addWidget(page)
            self._embedded[name] = page
            self._add_nav_item(f"{TOOL_PAGE_PREFIX}{name}", ("tool", name))

    def _rebuild_embedded_tools(self) -> None:
        """工程对象整个换掉后，工具页里拿着的是旧工程，重建一遍。"""
        names = list(self._embedded)
        for name in names:
            page = self._embedded.pop(name, None)
            row = self._nav_row_of_tool(name)
            if row >= 0:
                self.nav.takeItem(row)
            if page is not None:
                self.stack.removeWidget(page)
                page.deleteLater()
        self.restore_embedded_tools()

    def _on_diagram_action(self, action: str, target: str) -> None:
        if action == "device":
            self.show_device_detail(target)
        elif action == "page":
            if target in NAV_ITEMS:
                self.nav.setCurrentRow(NAV_ITEMS.index(target))
            else:
                QMessageBox.information(self, "提示", f"页面「{target}」不存在")
        elif action == "point":
            self._open_point_in_scope(target)
        else:
            QMessageBox.information(self, "提示", f"节点未配置跳转行为（action={action}）")

    def show_device_detail(self, device_name: str) -> None:
        self.device_detail_page.show_device(device_name)
        self.stack.setCurrentWidget(self.device_detail_page)
        self.nav.setCurrentRow(-1)  # 取消导航高亮，避免误导
        if self.device_detail_page.table.rowCount() == 0:
            self._log_status(f"设备「{device_name}」没有点位")

    # -------------------------------------------------- 点位操作（双击 / 右键）

    def _open_point_in_scope(self, pid: str) -> None:
        """在示波器里显示某个点位。

        任意点位都能加进来，不限于参数表里标了「示波器=是」的：
        现场临时想看某个量是常事。
        """
        self.nav.setCurrentRow(NAV_ITEMS.index("示波器"))
        if not self.scope_page.add_point(pid):
            self._log_status(f"工程里没有点位 {pid}")

    def _tag_of(self, pid: str) -> TagValue:
        if self.engine is not None:
            return self.engine.tags.get(pid)
        return TagValue(pid=pid)

    def _periodic_of(self, pid: str):
        """该点位正在运行的持续发送任务（没在发则为 None）。"""
        if self.engine is None:
            return None
        for task in self.engine.periodic_tasks():
            if task.pid == pid:
                return task
        return None

    def open_point_dialog(self, pid: str) -> None:
        """双击点位控件：打开参数对话框（改值 / 单发 / 持续发送 / 看位状态）。"""
        found = self.project.find_point(pid)
        if found is None:
            self._log_status(f"工程里没有点位 {pid}")
            return
        _device, point = found

        if self._point_dialog is not None:
            self._point_dialog.close()
            self._point_dialog = None

        dialog = PointDialog(
            pid,
            point,
            value_of=self._tag_of,
            periodic_of=self._periodic_of,
            default_interval_ms=self.settings.periodic_default_interval_ms,
            parent=self,
        )
        dialog.set_project_entry(self.project.periodic_write(pid))
        dialog.writeRequested.connect(self._on_write_requested)
        dialog.periodicRequested.connect(self._start_periodic)
        dialog.periodicStopRequested.connect(self._stop_periodic)
        dialog.definitionRequested.connect(self._reveal_point_definition)
        dialog.finished.connect(self._on_point_dialog_closed)
        self._point_dialog = dialog
        fit_to_available_screen(dialog)
        dialog.show()

    def _on_point_dialog_closed(self, _result: int) -> None:
        self._point_dialog = None

    def _reveal_point_definition(self, pid: str) -> None:
        """到「设备与点位」页选中这个点位的定义，方便改地址/类型/量程。"""
        self.nav.setCurrentRow(NAV_ITEMS.index("设备与点位"))
        if not self.devices_page.select_point(pid):
            self._log_status(f"在配置树里没找到 {pid}")

    # -------------------------------------------------- 工具（可插拔）

    def build_tools_menu(self, parent: QWidget | None = None) -> QMenu:
        """按注册表生成「工具」菜单（右键菜单、工具按钮等处用）。

        工具只在 ``icomm/ui/tools/`` 里注册，这里不出现任何具体工具的名字 ——
        以后加工具不用改这个文件。
        """
        menu = QMenu(parent or self)
        self.fill_tools_menu(menu)
        return menu

    def fill_tools_menu(self, menu: QMenu) -> None:
        """把工具注册表里的条目填进**已有的**菜单。

        菜单栏的「工具」直接调这个，而不是"先 build 一个再搬动作"：
        ``addAction(text)`` 建出来的动作，Qt 父对象是那个菜单，搬到别的菜单之后
        原来的菜单一析构就会把动作删掉，菜单栏上留下悬空指针 —— 退出时直接崩。
        """
        grouped = ui_tools.tools_by_group()
        if not grouped:
            empty = menu.addAction("（没有可用工具）")
            empty.setEnabled(False)
            return

        first = True
        for group, tools in grouped.items():
            if not first:
                menu.addSeparator()
            first = False
            submenu = menu
            if len(grouped) > 1:
                submenu = menu.addMenu(group)
            for tool in tools:
                action = submenu.addAction(tool.name)
                if tool.description:
                    action.setToolTip(tool.description)
                action.triggered.connect(
                    lambda _checked=False, t=tool: self.run_tool(t)
                )

    def tool_context(self) -> ui_tools.ToolContext:
        """组装工具上下文。

        工具只拿这个对象，不直接碰主窗口内部结构 —— 这样主窗口怎么改都不会
        把工具弄坏。新增工具需要的公共能力就加到 :class:`ToolContext` 上。
        """
        report_dir = self.paths.report_dir or self.base_dir
        if self.settings.export_location == "custom":
            report_dir = self.settings.custom_export_directory or report_dir
        return ui_tools.ToolContext(
            project=self.project,
            window=self,
            settings=self.settings,
            tag_of=self._tag_of,
            write=lambda pid, value: self._on_write_requested(pid, value),
            log=lambda level, message: self._log_status(f"[{level}] {message}"),
            engine_running=lambda: bool(self.engine is not None and self.engine.running),
            base_dir=self.base_dir,
            report_dir=report_dir,
            storage=self.storage,
            save_settings=self._save_settings_quietly,
            project_changed=self._on_project_edited,
            reveal_point=self._reveal_point_definition,
            add_to_scope=self._open_point_in_scope,
            set_write_limit=self.set_point_write_limit,
            set_decimals=self.set_point_decimals,
            title_of=self.point_title,
            pin_tool=self.embed_tool,
            services={"automation": self.automation, "fault": self.fault_recorder},
        )

    def run_tool(self, tool) -> None:
        """打开一个工具。

        已经钉在左侧栏的工具，这里只是切过去，不再另开一个浮动窗口。
        """
        if self.show_tool_page(tool.name):
            self._log_status(f"已切到工具页：{tool.name}")
            return
        try:
            window = tool.launch(self.tool_context())
        except Exception as exc:  # 单个工具出问题不该把主窗口带崩
            QMessageBox.critical(self, "工具出错", f"「{tool.name}」打开失败：\n{exc}")
            return
        if window is not None and tool.non_modal:
            self._log_status(f"已打开工具：{tool.name}")

    def _save_settings_quietly(self) -> None:
        try:
            app_settings.save_settings(self.settings, self.base_dir)
        except OSError as exc:
            self._log_status(f"设置未保存：{exc}")

    def output_path(self, kind: str, filename: str) -> str:
        root = self.paths.report_dir or self.paths.root or self.base_dir
        if self.settings.export_location == "custom":
            root = self.settings.custom_export_directory or root
        directory = os.path.join(root, project_store.report_kind_dir(kind))
        os.makedirs(directory, exist_ok=True)
        return os.path.join(directory, filename)

    def point_title(self, pid: str) -> str:
        found = self.project.find_point(pid)
        if found is None:
            return pid
        device, point = found
        return f"{device.name}.{point.title}"

    # -------------------------------------------------- 自动化

    def _tick_automation(self) -> None:
        """把实时值喂给自动化核心（触发判断跑在界面线程，动作可以碰界面）。"""
        if self.engine is None:
            return
        snapshot = self.engine.tags.snapshot()
        if not snapshot:
            return
        try:
            self.automation.tick(snapshot)
        except Exception as exc:  # 自动化出错不能影响采集显示
            self._log_status(f"自动化任务出错：{exc}")

    def show_automation_overview(self) -> None:
        """看有哪些自动化任务在跑，可手动触发或删除。"""
        panel = AutomationPanel(self.automation, parent=self)
        fit_to_available_screen(panel)
        panel.exec()
        if panel.changed and self.engine is not None:
            self._log_status(f"自动化任务：{len(self.automation.tasks)} 个")

    # -------------------------------------------------- 点位写值限幅 / 小数位

    def set_point_write_limit(self, pid: str, low, high) -> bool:
        """设置某个点位的写值上下限（None = 不限）。"""
        found = self.project.find_point(pid)
        if found is None:
            return False
        _device, point = found
        point.write_min = None if low is None else float(low)
        point.write_max = None if high is None else float(high)
        self._on_project_edited()
        self._log_status(f"{pid} 写值限幅设为 {point.write_limit_text()}")
        return True

    def set_point_decimals(self, pid: str, decimals: int) -> bool:
        """设置某个点位的显示小数位数。"""
        found = self.project.find_point(pid)
        if found is None:
            return False
        _device, point = found
        point.decimals = max(0, min(6, int(decimals)))
        self._on_project_edited()
        self._log_status(f"{pid} 显示小数位改为 {point.decimals}")
        return True

    def set_point_display_base(self, pid: str, base) -> bool:
        """设置某个点位的显示进制（10 / 16 / 2 / 8）。

        只改界面怎么显示，读到/写下去的原始值不变；非十进制按整数取整显示
        （看状态字、寄存器最常用）。
        """
        found = self.project.find_point(pid)
        if found is None:
            return False
        _device, point = found
        point.display_base = normalize_base(base)
        self._on_project_edited()
        self._log_status(
            f"{pid} 显示进制改为 {DISPLAY_BASES.get(point.display_base, '十进制')}"
        )
        return True

    def edit_point_write_limit(self, pid: str) -> None:
        """右键菜单入口：改写值限幅。"""
        found = self.project.find_point(pid)
        if found is None:
            return
        _device, point = found
        dialog = WriteLimitDialog(pid, point, parent=self)
        fit_to_available_screen(dialog)
        if dialog.exec() != WriteLimitDialog.Accepted:
            return
        low, high = dialog.values()
        self.set_point_write_limit(pid, low, high)

    def edit_point_decimals(self, pid: str) -> None:
        """右键菜单入口：改显示小数位数。"""
        found = self.project.find_point(pid)
        if found is None:
            return
        _device, point = found
        value, ok = QInputDialog.getInt(
            self,
            "显示小数位数",
            f"{point.title}（{pid}）\n当前 {point.decimals} 位",
            point.decimals,
            0,
            6,
            1,
        )
        if ok:
            self.set_point_decimals(pid, value)

    # -------------------------------------------------- 小窗口按键控制

    def _panel_kwargs(self) -> dict:
        """所有控制面板共用的取值与状态查询函数。"""
        return {
            "value_of": self._tag_of,
            "periodic_of": self._periodic_of,
            "engine_running": lambda: bool(self.engine is not None and self.engine.running),
        }

    def panel_points_for(self, device_name: str) -> list[str]:
        """软件设置里记着的、用户手动加进该设备控制台的点位。"""
        stored = self.settings.panel_points.get(device_name)
        return list(stored) if isinstance(stored, (list, tuple)) else []

    def _connect_panel(self, panel) -> None:
        panel.writeRequested.connect(self._on_write_requested)
        panel.periodicRequested.connect(self._start_periodic)
        panel.periodicStopRequested.connect(self._stop_periodic)
        panel.dialogRequested.connect(self.open_point_dialog)

    def show_point_control_panel(self, pid: str) -> None:
        """给单个点位调出小窗口按键面板。"""
        try:
            panel = control_panel.open_point_panel(
                self, project=self.project, pid=pid, **self._panel_kwargs()
            )
        except ValueError as exc:
            self._log_status(str(exc))
            return
        self._connect_panel(panel)
        self._log_status(f"已调出 {pid} 的按键面板")

    def show_device_control_panel(self, device_name: str) -> None:
        """调出某台设备的控制台（默认列出参数表里的命令点位）。"""
        try:
            panel = control_panel.open_device_panel(
                self,
                project=self.project,
                device_name=device_name,
                extra_pids=self.panel_points_for(device_name),
                on_pick_points=self.pick_panel_points,
                **self._panel_kwargs(),
            )
        except ValueError as exc:
            self._log_status(str(exc))
            return
        self._connect_panel(panel)
        self._log_status(f"已调出「{device_name}」控制台")

    def pick_panel_points(self, device_name: str, selected: list[str]) -> None:
        """选择控制台上还要显示哪些点位，选完重建面板。"""
        device = self.project.device(device_name)
        if device is None:
            return
        dialog = control_panel.PanelPointPicker(device, selected, parent=self)
        fit_to_available_screen(dialog)
        if dialog.exec() != control_panel.PanelPointPicker.Accepted:
            return
        self.settings.panel_points[device_name] = dialog.selected()
        try:
            app_settings.save_settings(self.settings, self.base_dir)
        except OSError as exc:
            self._log_status(f"控制台点位选择未保存：{exc}")
        control_panel.close_device_panel(device_name)
        self.show_device_control_panel(device_name)
        self._log_status(
            f"「{device_name}」控制台显示 {len(dialog.selected())} 个额外点位"
        )

    def show_control_panel_picker(self) -> None:
        """工具栏「控制台」：挑一台设备或一个点位开面板。"""
        menu = QMenu(self)
        header = menu.addAction("设备的控制台")
        header.setEnabled(False)
        for device in self.project.devices:
            action = menu.addAction(f"　{device.name}（unit {device.unit_id}）")
            action.triggered.connect(
                lambda _checked=False, name=device.name: self.show_device_control_panel(name)
            )

        candidates = self.controllable_points()
        if candidates:
            menu.addSeparator()
            header = menu.addAction("单个点位的按键面板")
            header.setEnabled(False)
            for pid, label in candidates[:40]:
                action = menu.addAction(f"　{label}")
                action.triggered.connect(
                    lambda _checked=False, p=pid: self.show_point_control_panel(p)
                )
        menu.addSeparator()
        close_all = menu.addAction("关闭全部控制面板")
        close_all.triggered.connect(control_panel.close_all)
        menu.exec(self.cursor().pos())

    def controllable_points(self) -> list[tuple[str, str]]:
        """能用按键控制的点位（有命令、或可写且带位定义）。

        供「控制台」菜单使用；实际生成按键的是
        :mod:`icomm.ui.dialogs.control_panel`。想让某个点位出现按键，
        在参数表里给它填「命令」列或「位定义」列即可。
        """
        entries: list[tuple[str, str]] = []
        for device, point in self.project.iter_points():
            has_buttons = bool(
                point.command_items() or (point.writable and point.bit_labels())
            )
            if has_buttons:
                entries.append(
                    (point_id(device.name, point.name), f"{device.name}.{point.title}")
                )
        return entries

    def build_point_menu(self, pid: str) -> QMenu | None:
        """构造点位右键菜单。

        写值是两种语义，菜单里分开给：**单发**写一次，**持续发送**按周期反复写。
        与弹出动作分开是为了能单独测菜单内容（``exec()`` 会阻塞）。
        """
        found = self.project.find_point(pid)
        if found is None:
            return None
        _device, point = found
        running = self._periodic_of(pid)
        running_engine = self.engine is not None and self.engine.running

        menu = QMenu(self)
        header = menu.addAction(f"{point.title}　（{pid}）")
        header.setEnabled(False)
        menu.addSeparator()

        if point.writable:
            single = menu.addAction("单发写入…")
            single.setEnabled(running_engine)
            single.triggered.connect(lambda: self.open_point_dialog(pid))
            if running is None:
                periodic = menu.addAction("持续发送…")
                periodic.setEnabled(running_engine)
                periodic.setToolTip("按周期反复写同一个值，直到手动停止")
                periodic.triggered.connect(lambda: self.open_point_dialog(pid))
            else:
                state = "发送中" if running.enabled else "已暂停"
                pause = menu.addAction(
                    "暂停持续发送" if running.enabled else "恢复持续发送"
                )
                pause.triggered.connect(
                    lambda: self._toggle_periodic(pid, not running.enabled)
                )
                stop = menu.addAction(
                    f"停止持续发送（{state}，每 {running.interval_ms} ms）"
                )
                stop.triggered.connect(lambda: self._stop_periodic(pid))
        else:
            readonly = menu.addAction("只读点位，不能写入")
            readonly.setEnabled(False)

        menu.addSeparator()
        scope_action = menu.addAction("添加到示波器")
        scope_action.triggered.connect(lambda: self._open_point_in_scope(pid))

        if point.command_items() or (point.writable and point.bit_labels()):
            keys = menu.addAction("调出按键控制小窗口")
            keys.setToolTip("非模态浮动面板，可同时开多个，一边看数据一边按")
            keys.triggered.connect(lambda: self.show_point_control_panel(pid))

        menu.addSeparator()
        limit = menu.addAction(
            f"写值限幅…（当前 {point.write_limit_text()}）"
            if point.has_write_limit
            else "写值限幅…"
        )
        limit.setToolTip("设定上下限，超出的值会被自动夹到边界再下发")
        limit.setEnabled(point.writable)
        limit.triggered.connect(lambda: self.edit_point_write_limit(pid))

        decimals = menu.addAction(f"显示小数位数…（当前 {point.decimals} 位）")
        decimals.setToolTip("只影响显示，不改设备里的值")
        decimals.triggered.connect(lambda: self.edit_point_decimals(pid))

        base_menu = menu.addMenu(
            f"显示进制（当前 {DISPLAY_BASES.get(point.display_base, '十进制')}）"
        )
        base_menu.setToolTip("只影响显示。非十进制按整数取整 —— 看状态字、寄存器最常用")
        for base, label in DISPLAY_BASES.items():
            pick = base_menu.addAction(label)
            pick.setCheckable(True)
            pick.setChecked(base == point.display_base)
            pick.triggered.connect(
                lambda _checked=False, chosen=base: self.set_point_display_base(pid, chosen)
            )

        watch = menu.addAction("用这个变量做触发条件…")
        watch.setToolTip("打开截屏辅助，条件默认选中这个点位")
        watch.triggered.connect(lambda: self._use_point_as_trigger(pid))

        menu.addSeparator()
        define = menu.addAction("编辑点位定义…")
        define.triggered.connect(lambda: self._reveal_point_definition(pid))
        return menu

    def _use_point_as_trigger(self, pid: str) -> None:
        """右键「用这个变量做触发条件」：打开截屏辅助并把点位预选好。"""
        tool = ui_tools.find_tool("截屏辅助")
        if tool is None:
            self._log_status("截屏辅助工具不可用")
            return
        window = tool.launch(self.tool_context())
        if window is not None and hasattr(window, "preselect_pid"):
            window.preselect_pid(pid)

    def show_point_menu(self, pid: str, global_pos) -> None:
        """右键点位控件：弹出菜单。"""
        menu = self.build_point_menu(pid)
        if menu is None:
            return
        if isinstance(global_pos, QPoint):
            menu.exec(global_pos)
        else:
            menu.exec(QPoint(int(global_pos[0]), int(global_pos[1])))

    # -------------------------------------------------- 持续发送

    def _start_periodic(self, pid: str, value, interval_ms: int) -> None:
        if self.engine is None or not self.engine.running:
            QMessageBox.information(
                self, "提示", "请先启动采集，再对该点位持续发送。"
            )
            return
        found = self.project.find_point(pid)
        if found is None:
            return
        _device, point = found

        if self.settings.periodic_confirm:
            per_second = 1000.0 / max(1, int(interval_ms))
            answer = QMessageBox.question(
                self,
                "确认持续发送",
                f"将对 <b>{point.title}</b>（{pid}）每 {interval_ms} ms 写入 <b>{value}</b>。\n\n"
                f"约每秒 {per_second:.1f} 次，会一直发到手动停止。\n"
                "确认要开始吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return

        ok, error = self.engine.set_periodic(pid, value, interval_ms)
        if not ok:
            QMessageBox.warning(self, "无法持续发送", error)
            return
        self._dirty = True
        self._update_title()
        self._log_status(f"{pid} 开始持续发送：每 {interval_ms} ms 写 {value}")

    def _stop_periodic(self, pid: str) -> None:
        if self.engine is not None:
            self.engine.clear_periodic(pid)
        self._dirty = True
        self._update_title()
        self._log_status(f"{pid} 已停止持续发送")

    def _toggle_periodic(self, pid: str, enabled: bool) -> None:
        if self.engine is not None and self.engine.set_periodic_enabled(pid, enabled):
            self._log_status(f"{pid} 持续发送已{'恢复' if enabled else '暂停'}")

    def show_periodic_overview(self) -> None:
        """工具栏「持续发送」：看有哪些在周期写值，并可以停止。"""
        panel = PeriodicPanel(
            self.project,
            list(self.engine.periodic_tasks()) if self.engine else [],
            engine_running=bool(self.engine and self.engine.running),
            default_interval_ms=self.settings.periodic_default_interval_ms,
            parent=self,
        )
        fit_to_available_screen(panel)
        panel.exec()
        if panel.stopped:
            self._dirty = True
            self._update_title()
        if panel.started:
            for pid, value, interval_ms in panel.started:
                self._start_periodic(pid, value, interval_ms)

    # -------------------------------------------------- 写值

    def _on_write_requested(self, pid: str, value) -> None:
        if self.engine is None or not self.engine.running:
            QMessageBox.information(self, "提示", "请先启动采集再写值")
            return
        found = self.project.find_point(pid)
        if found is None:
            return
        _device, point = found
        if point.data_type == "bool":
            value = bool(value)
        threading.Thread(
            target=self._do_write, args=(pid, value), name=f"vvphmi-write-{pid}", daemon=True
        ).start()

    def _do_write(self, pid: str, value) -> None:
        assert self.engine is not None
        ok, error = self.engine.write(pid, value)
        self._write_emitter.finished.emit(pid, ok, error)

    def _on_write_finished(self, pid: str, ok: bool, error: str) -> None:
        if ok:
            self._log_status(f"{pid} 写入成功")
        else:
            self._log_status(f"{pid} 写入失败：{error}")
            QMessageBox.warning(self, "写入失败", f"{pid}\n{error}")

    # -------------------------------------------------- 工程管理

    def _on_dashboard_order(self, group: str, pids: list) -> None:
        """记住运行监视里中键拖出来的顺序（软件级设置，不动工程）。"""
        self.settings.display_order[group] = list(pids)
        try:
            app_settings.save_settings(self.settings, self.base_dir)
        except OSError as exc:
            self._log_status(f"显示顺序没能保存：{exc}")
            return
        self._log_status(f"「{group}」顺序已更新（中键拖动）")

    def _rebuild_pages(self) -> None:
        """工程对象换掉（或整体改动）后，让各页面重新绑定。"""
        self.overview_page.set_project(self.project, self.paths.config_dir or self.paths.root)
        self.dashboard_page.rebuild(self.project)
        self.scope_page.set_project(self.project)
        self.scope_page.set_preferences(self.settings)
        self.trend_page.set_project(self.project)
        self.monitor_page.set_channels([c.name for c in self.project.channels])
        self.devices_page.set_project(self.project)
        self.device_detail_page.set_project(self.project)

    def _on_project_edited(self) -> None:
        self._dirty = True
        self._rebuild_pages()
        self._update_title()

    # -------------------------------------------------- 设置

    def settings_dialog(self, tab: int | None = None) -> SettingsDialog:
        """造一个设置窗口（不 ``exec``，方便菜单/测试单独取用）。

        ``tab`` 指定从哪一页打开：0 界面外观 / 1 通信 / 2 软件设置 / 3 工程设置；
        不指定就用上次停留的那一页。
        """
        if isinstance(tab, bool):  # QAction.triggered 会顺便递一个 bool 过来
            tab = None
        initial = self.settings.settings_tab if tab is None else tab
        dialog = SettingsDialog(
            self.settings,
            self.project,
            list(project_io.BUILTIN_SAMPLES),
            parent=self,
            base_dir=self.base_dir,
            project_root=self.paths.root,
            initial_tab=initial,
        )
        fit_to_available_screen(dialog)
        # 外观实时预览时重建页面：有些内联样式只在构造时取一次颜色
        dialog.appearanceChanged.connect(self._refresh_appearance)
        return dialog

    def open_settings(self, tab: int | None = None) -> None:
        """打开设置窗口：界面外观 + 通信 + 软件设置 + 工程设置。

        「设置」菜单里四块各有一条，点哪块就从哪一页打开。
        """
        dialog = self.settings_dialog(tab)
        if dialog.exec() != SettingsDialog.Accepted:
            return
        self.apply_settings(dialog)
        if dialog.folder_rename_to:
            self._rename_project_folder(dialog.folder_rename_to)
        if dialog.reset_window_requested:
            # 记的窗口大小已经清掉了，这里立刻给个合适的大小，不用等下次启动
            self.reset_window_size()

    def _rename_project_folder(self, wanted: str) -> None:
        """把工程文件夹改成英文名（搬不动就保持原名，不会丢东西）。

        Windows 上**目录里有打开的文件就改不了名**（历史库 history.db 正被归档线程
        和查询连接占着），所以先把存储关掉、改完名再按新路径接回来。
        """
        if not self.paths.root:
            return
        relative = self._relative_project_parts()
        self._detach_storage()  # 放开 history.db，否则 Windows 上必然拒绝访问
        target, note = project_store.rename_project_dir(self.paths, wanted, self.workdir)
        if os.path.normcase(target) == os.path.normcase(self.paths.root):
            self._attach_storage()  # 没改成，把存储接回来
            self._log_status(f"工程文件夹未改名：{note}")
            if note and "英文" in note:
                QMessageBox.information(self, "文件夹名要用英文", note)
            return
        self.paths = project_store.resolve(
            self.project, os.path.join(target, *relative), origin_iprj=self.paths.origin_iprj
        )
        self._attach_storage()
        self._update_title()
        self._log_status(f"工程文件夹已改名：{os.path.basename(target)}")

    def _relative_project_parts(self) -> list[str]:
        """工程文件相对工程文件夹的路径（``config/project.json`` 或 ``project.json``）。"""
        if not self.paths.project_file or not self.paths.root:
            return [project_store.CONFIG_DIR, project_store.PROJECT_NAME]
        relative = os.path.relpath(self.paths.project_file, self.paths.root)
        return relative.split(os.sep)

    def _refresh_appearance(self) -> None:
        """外观变了：重套样式表并重建页面（内联样式才会跟着更新）。"""
        self.setStyleSheet(theme.STYLESHEET)
        self._rebuild_pages()

    def apply_settings(self, dialog) -> None:
        """把设置对话框的结果应用到正在运行的程序。

        与弹出动作分开，便于测试与脚本化调用（``exec()`` 会阻塞）。
        """
        before_storage = self.project.storage.to_dict()

        # 对话框在副本上改，确定后把结果拷回原对象 ——
        # 保持对象身份不变，其他页面（示波器等）持有的引用才知道设置变了。
        for name, value in dialog.settings.to_dict().items():
            setattr(self.settings, name, value)

        try:
            app_settings.save_settings(self.settings, self.base_dir)
        except OSError as exc:
            QMessageBox.warning(self, "设置未保存", f"写入设置文件失败：{exc}")
        if self.engine is not None:
            self.engine.reconnect_interval_s = self.settings.reconnect_interval_s
            self.engine.write_timeout_s = self.settings.write_timeout_s
        self._sync_purge_timer()
        self.scope_page.set_preferences(self.settings)

        # 面板里可能移除了持续发送配置，正在发的那条要跟着停
        if self.engine is not None:
            configured = {p.pid for p in self.project.periodic_writes}
            for task in self.engine.periodic_tasks():
                if task.pid not in configured:
                    self.engine.clear_periodic(task.pid)

        storage_changed = self.project.storage.to_dict() != before_storage
        if storage_changed:
            try:
                self.paths = project_store.resolve(
                    self.project, self.paths.project_file, origin_iprj=self.paths.origin_iprj
                )
            except Exception as exc:
                QMessageBox.warning(self, "路径未生效", f"重新解析存放路径失败：{exc}")
            else:
                self.stop_engine()
                self._detach_storage()
                self._attach_storage()
                self._attach_engine()
                self.status_path.setText(self.paths.summary())

        self._dirty = True
        self._rebuild_pages()
        self._update_title()
        self._log_status(
            f"设置已更新"
            + ("（存放路径已切换，需要保存工程生效）" if storage_changed else "")
        )

    def _swap_project(self, project: Project, paths: project_store.ProjectPaths) -> None:
        self.stop_engine()
        self.project = project
        self.paths = paths
        self._detach_storage()
        self._attach_storage()
        self._attach_engine()
        self._rebuild_pages()
        # 工具页里拿着的是旧工程对象，换工程后重建一份（新工程的点位才对得上）
        self._rebuild_embedded_tools()
        self._dirty = False
        self.action_start.setEnabled(True)
        self.action_stop.setEnabled(False)
        self.mode_label.setText("未运行")
        self.mode_label.setStyleSheet(f"color: {theme.TEXT_DIM}; padding-right: 10px;")
        self._update_title()
        self.status_path.setText(self.paths.summary())
        self._remember_project_path()

    def _update_title(self) -> None:
        name = os.path.basename(self.paths.project_file) if self.paths.project_file else "未保存"
        origin = (
            f" ← {os.path.basename(self.paths.origin_iprj)}" if self.paths.origin_iprj else ""
        )
        dirty = " *" if self._dirty else ""
        self.setWindowTitle(
            f"{APP_NAME} — {self.project.name}  [{name}{origin}]{dirty}"
        )
        if self.paths.project_file:
            self.status_path.setText(self.paths.summary())

    def _project_open_path(self) -> str:
        return os.path.abspath(self.paths.origin_iprj or self.paths.project_file) if (
            self.paths.origin_iprj or self.paths.project_file
        ) else ""

    def _remember_project_path(self) -> None:
        path = self._project_open_path()
        if not path:
            return
        recent = [item for item in self.settings.recent_projects
                  if os.path.normcase(item) != os.path.normcase(path)]
        self.settings.recent_projects = [path, *recent][:8]
        self.settings.last_project = path
        self._save_settings_quietly()

    def _refresh_recent_projects_menu(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.clear()
        existing = [path for path in self.settings.recent_projects if os.path.exists(path)]
        self.settings.recent_projects = existing
        if not existing:
            action = self.recent_menu.addAction("（暂无最近打开的工程）")
            action.setEnabled(False)
        else:
            for path in existing:
                action = self.recent_menu.addAction(os.path.basename(path) or path)
                action.setToolTip(path)
                action.triggered.connect(
                    lambda _checked=False, target=path: self._open_project_path(target)
                )
            self.recent_menu.addSeparator()
            clear = self.recent_menu.addAction("清除最近记录")
            clear.triggered.connect(self._clear_recent_projects)

    def _clear_recent_projects(self) -> None:
        self.settings.recent_projects = []
        self.settings.last_project = ""
        self._save_settings_quietly()
        self._refresh_recent_projects_menu()

    def open_project(self) -> None:
        # 起始目录给工程仓库，省得每次从"上次去过的地方"开始翻
        start = self.workdir if os.path.isdir(self.workdir) else ""
        path, _ = QFileDialog.getOpenFileName(self, "打开工程", start, IPRJ_FILTER)
        if not path:
            return
        self._open_project_path(path)

    def _open_project_path(self, path: str) -> None:
        try:
            project, paths = project_store.open_project(
                path, workdir=self.workdir, upgrade=True
            )
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", str(exc))
            return
        self._swap_project(project, paths)
        self._remember_project_path()
        self._log_status(f"已打开 {os.path.basename(path)}")
        if paths.organized:
            self._log_status(f"工程位置：{paths.root}（配置 / 数据 / 报表 已分开存放）")

    # -------------------------------------------------- 保存（细分：参数 / 整个工程）

    def save_project(self) -> None:
        """保存整个工程到当前位置（用工程当前的格式，不弹格式选择）。"""
        if not self.paths.project_file:
            self.save_project_as()
            return
        self.devices_page.apply_form()
        export_csv = True
        if csv_table.csv_differs_from_disk(self.project, self.paths.csv_file):
            choice = QMessageBox.question(
                self,
                "参数表已被外部修改",
                f"磁盘上的参数表与当前工程不一致（可能你用 Excel 改过但还没导入）：\n"
                f"{self.paths.csv_file}\n\n"
                "「是」= 用当前工程覆盖它（Excel 里的改动会丢失）\n"
                "「否」= 保留磁盘上的表不动，稍后用「导入参数表」把改动读进来",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            export_csv = choice == QMessageBox.Yes

        try:
            result = project_store.save_project(self.project, self.paths, export_csv=export_csv)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        self._dirty = False
        self._update_title()
        detail = "、".join(os.path.basename(f) for f in result.files)
        if result.iprj:
            detail += f"、{os.path.basename(result.iprj)}"
        if not export_csv:
            detail += "（参数表未覆盖，保留磁盘版本）"
        self._log_status(f"已保存工程：{detail}")

    def save_project_as(self) -> None:
        """把**整个工程**另存为指定格式。

        格式来自 :mod:`icomm.core.writers` 的注册表，所以以后新增格式
        （数据库工程、纯 JSON 包、云端……）只要注册一个类，这个对话框自动就有。
        """
        self.devices_page.apply_form()
        dialog = SaveAsDialog(
            "保存整个工程",
            writers.project_writers(),
            # 工程目录的默认名用英文（磁盘目录一律 ASCII），单文件包无所谓
            default_name=self.project.folder_name or project_store.project_folder_name(self.project),
            parent=self,
        )
        fit_to_available_screen(dialog)
        if dialog.exec() != SaveAsDialog.Accepted:
            return
        writer = dialog.selected_writer()
        target = dialog.target_path()
        if not target:
            return

        if writer.single_file:
            if not target.lower().endswith(writer.suffix.lower()):
                target += writer.suffix
            try:
                packed = writers.IprjWriter().write(self.project, self.paths, target)
            except Exception as exc:
                QMessageBox.critical(self, "保存失败", str(exc))
                return
            # 后续保存继续回写这个包
            working = project_store.create_in_workdir(self.project, self.workdir)
            working.origin_iprj = os.path.abspath(packed)
            self.paths = working
            self._attach_storage()
            self._dirty = False
            self._update_title()
            self._log_status(f"已保存工程包：{os.path.basename(packed)}")
            return

        try:
            paths = project_store.save_project_to_dir(
                self.project, target, db_source=self.paths.db_file
            )
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.paths = paths
        self._attach_storage()
        self._dirty = False
        self._update_title()
        self._log_status(f"已保存工程目录：{paths.root}")

    def save_parameters_as(self) -> None:
        """只把**参数设置**（点位定义）存成文件，格式可选。

        与「保存整个工程」分开：现场常要把参数表单独交给同事或存档，
        不想连历史库一起给。
        """
        self.devices_page.apply_form()
        dialog = SaveAsDialog(
            "保存参数设置",
            writers.parameter_writers(),
            default_name=f"{self.project.name}-参数表",
            parent=self,
            part="参数表",
        )
        fit_to_available_screen(dialog)
        if dialog.exec() != SaveAsDialog.Accepted:
            return
        writer = dialog.selected_writer()
        target = dialog.target_path()
        if not target:
            return
        if not target.lower().endswith(writer.suffix.lower()):
            target += writer.suffix

        try:
            count = writer.write(self.project, target)
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return

        note = (
            "可继续用「导入参数表」读回来。" if writer.supports_read() else "该格式只用于导出。"
        )
        self._log_status(f"已保存参数表（{writer.label}）：{count} 个点位 -> {target}")
        QMessageBox.information(
            self,
            "保存完成",
            f"已按「{writer.label}」保存 {count} 个点位到：\n{target}\n\n{note}",
        )

    def _new_from(self, project: Project, title: str) -> None:
        if self._dirty and QMessageBox.question(
            self, "新建工程", "当前工程有未保存的修改，确定要新建吗？"
        ) != QMessageBox.Yes:
            return
        paths = project_store.create_in_workdir(project, self.workdir)
        self._swap_project(project, paths)
        self._log_status(f"已新建工程：{title}（位于 {paths.root}）")

    def new_blank_project(self) -> None:
        self._new_from(project_io.blank_project(), "空白工程")

    def load_sample(self, name: str) -> None:
        builder = project_io.BUILTIN_SAMPLES.get(name)
        if builder is None:
            return
        self._new_from(builder(), f"示例 - {name}")

    # -------------------------------------------------- 参数表

    def export_csv(self) -> None:
        """Export the current point table to the configured output directory."""
        filename = f"parameters_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path = self.output_path("tables", filename)
        try:
            count = csv_table.export_csv(self.project, path)
        except OSError as exc:
            QMessageBox.critical(self, "导出失败", f"参数表写入失败：\n{exc}")
            return
        self._log_status(f"已导出 {count} 个点位到：{path}")

    def import_parameters(self) -> None:
        """导入参数表，格式按扩展名识别。"""
        self.devices_page.apply_form()
        default = self.paths.csv_file or ""
        path, _ = QFileDialog.getOpenFileName(
            self, "导入参数表", default, writers.parameter_file_filter()
        )
        if not path:
            return
        self._import_parameters_from(path)

    def _import_parameters_from(self, path: str) -> None:
        """按扩展名选写入器读参数表。

        格式不支持的（比如只有导出实现的 TXT）要明确告诉用户，
        不能默默什么都不做。
        """
        writer = writers.parameter_writer_for(path)
        if writer is None:
            QMessageBox.warning(
                self,
                "认不出格式",
                f"认不出这个文件的格式：\n{path}\n\n"
                f"支持：{'、'.join(w.suffix for w in writers.parameter_writers())}",
            )
            return

        mode_label = self._ask_import_mode(os.path.basename(path))
        if mode_label is None:
            return
        mode = "replace" if mode_label == "replace" else "merge"

        try:
            report = writer.read(self.project, path, mode=mode)
        except Exception as exc:
            QMessageBox.critical(self, "导入失败", f"读取参数表出错：\n{exc}")
            return
        if report is None:
            QMessageBox.warning(
                self,
                "该格式不支持导入",
                f"「{writer.label}」目前只支持导出，不能作为导入来源。\n\n"
                "要改用 CSV 或 JSON 格式保存才能导回。",
            )
            return

        self._report_import(report, path)

    def _ask_import_mode(self, filename: str) -> str | None:
        box = QMessageBox(self)
        box.setWindowTitle("导入参数表")
        box.setText(f"要如何把「{filename}」里的点位并入当前工程？")
        box.setInformativeText(
            "更新/新增：保留表里没有的点位（推荐，适合增补）\n"
            "清空重建：先删掉当前全部点位再按表重建（适合整表替换）"
        )
        merge = box.addButton("更新 / 新增", QMessageBox.AcceptRole)
        replace = box.addButton("清空重建", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        fit_to_available_screen(box)
        box.exec()
        clicked = box.clickedButton()
        if clicked is merge:
            return "merge"
        if clicked is replace:
            return "replace"
        return None

    def _report_import(self, report, path: str) -> None:
        """把导入结果（含逐行问题）汇报给用户。"""
        if report.points_added or report.points_updated:
            self._on_project_edited()
            self._log_status(f"已导入参数表：{report.summary()}")

        lines = [report.summary(), ""]
        for issue in report.issues[:40]:
            lines.append(issue.text())
        if len(report.issues) > 40:
            lines.append(f"……还有 {len(report.issues) - 40} 条")
        text = "\n".join(lines)
        if report.ok:
            QMessageBox.information(self, "导入完成", text)
        else:
            QMessageBox.warning(self, "导入有问题", text)

    def import_csv(self) -> None:
        """保留旧入口（老信号连接），实际按扩展名走通用导入。"""
        self.import_parameters()

    def generate_csv_template(self) -> None:
        """导出一份带示例行的模板，方便现场照葫芦画瓢。"""
        path = self.output_path(
            "tables", f"参数表模板_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        )
        sample = project_io.sst_project()
        try:
            csv_table.export_csv(sample, path)
        except Exception as exc:
            QMessageBox.critical(self, "生成失败", str(exc))
            return
        QMessageBox.information(
            self,
            "模板已生成",
            f"已按 SST 示例工程生成模板（{len(sample.all_point_ids())} 行示例点位）：\n{path}\n\n"
            "三相电压这类成组点位就是一行一相，各自写自己的地址与类型；\n"
            "「元素个数」大于 1 即为数组/块点位（录波用）；\n"
            "报警限值留空表示不启用该限值。",
        )

    # -------------------------------------------------- 内置仿真器

    def _toggle_simulator(self, checked: bool) -> None:
        if not checked:
            if self._simulator is not None:
                self._simulator.stop()
                self._simulator = None
                self._log_status("内置仿真器已停止")
            self._stop_extra_simulators()
            return

        try:
            from tools.modbus_sim import ModbusTcpSimulator
        except ImportError as exc:
            self.action_sim.setChecked(False)
            QMessageBox.warning(self, "无法启动", f"找不到仿真器模块：{exc}")
            return

        port = 5020
        for channel in self.project.channels:
            if channel.driver == "modbus" and channel.link == "tcp":
                port = int(channel.options.get("port", 5020))
                break

        try:
            self._simulator = ModbusTcpSimulator("127.0.0.1", port, verbose=False)
            threading.Thread(
                target=self._simulator.serve_forever,
                kwargs={"freeze": False},
                name="vvphmi-sim",
                daemon=True,
            ).start()
        except OSError as exc:
            self._simulator = None
            self.action_sim.setChecked(False)
            QMessageBox.warning(self, "启动失败", f"端口 {port} 可能被占用：{exc}")
            return
        try:
            from icomm.core import samples as sample_pack

            units = "、".join(str(unit) for unit in sample_pack.SIM_UNIT_IDS)
        except Exception:  # 仿真器能起来最重要，列不列从站号无所谓
            units = "?"
        self._log_status(
            f"内置仿真器已启动，监听 127.0.0.1:{port}（从站号 {units}）"
        )
        self._start_extra_simulators()

    # -------------------------------------------------- CAN / 61850 / S7 仿真装置

    def _start_extra_simulators(self) -> None:
        """按工程里用到的协议，顺带把 CAN / 61850 / S7 的仿真装置也起起来。

        为什么要一起起：工程里通道的协议是配好的，演示时不该再让人手动开三个窗口。
        只起"工程里真有这种通道"的那些 —— Modbus 工程不会被塞一堆无关的仿真进程。
        """
        drivers = {channel.driver for channel in self.project.channels}
        notes: list[str] = []

        if "can" in drivers and self._can_sim is None:
            try:
                from tools.can_sim import CanSimulator

                channel = next(c for c in self.project.channels if c.driver == "can")
                options = dict(channel.options or {})
                self._can_sim = CanSimulator(
                    interface=str(options.get("backend") or "virtual"),
                    channel=str(options.get("channel") or "vvp"),
                    bitrate=int(options.get("bitrate") or 500000),
                )
                self._can_sim.start()
                notes.append("CAN 虚拟总线")
            except Exception as exc:  # noqa: BLE001 - 仿真器起不来不该挡采集
                self._can_sim = None
                self._log_status(f"CAN 仿真器没起来：{exc}")

        if "iec61850" in drivers and self._iec61850_sim is None:
            try:
                from tools.iec61850_sim import Iec61850Simulator

                channel = next(c for c in self.project.channels if c.driver == "iec61850")
                options = dict(channel.options or {})
                port = int(options.get("port") or 0)
                # 真机端口一般是 102：仿真器不要去抢（Windows 上 102 还要管理员权限），
                # 换一个邻近的端口，用户把通道端口改成它就能看数据
                listen = port if port not in (102, 0) else 8102
                self._iec61850_sim = Iec61850Simulator(
                    port=listen, ied_name=str(options.get("ied_name") or "VVPSIM")
                )
                self._iec61850_sim.start()
                notes.append(f"IEC 61850 装置（{self._iec61850_sim.address}）")
            except Exception as exc:  # noqa: BLE001
                self._iec61850_sim = None
                self._log_status(f"61850 仿真器没起来：{exc}")

        if "s7" in drivers and self._s7_sim is None:
            try:
                from tools.s7_sim import S7Simulator

                channel = next(c for c in self.project.channels if c.driver == "s7")
                options = dict(channel.options or {})
                port = int(options.get("port") or 0)
                # 端口 102 是 S7 的正经端口，仿真器不要去抢真机的位置
                # （Windows 上低端口还可能要管理员权限），换一个高位端口，
                # 用户把通道端口改成它就能看数据
                listen = port if port not in (102, 0) else 10102
                self._s7_sim = S7Simulator(
                    host=str(options.get("host") or "127.0.0.1"), port=listen
                )
                self._s7_sim.start()
                notes.append(f"S7 仿真 PLC（{self._s7_sim.address}，机架 0 插槽 1）")
            except Exception as exc:  # noqa: BLE001
                self._s7_sim = None
                self._log_status(f"S7 仿真器没起来：{exc}")

        if notes:
            self._log_status("已顺带启动：" + "、".join(notes))

    def _stop_extra_simulators(self) -> None:
        for name in ("_can_sim", "_iec61850_sim", "_s7_sim"):
            sim = getattr(self, name, None)
            if sim is not None:
                try:
                    sim.stop()
                except Exception:
                    pass
                setattr(self, name, None)

    # -------------------------------------------------- 状态刷新

    def _refresh_clock(self) -> None:
        """状态栏时钟（光标放上去有一次性的日期提示）。"""
        now = time.localtime()
        self.status_clock.setText(time.strftime("%H:%M:%S", now))
        self.status_clock.setToolTip(
            f"当前时间：{time.strftime('%Y-%m-%d %H:%M:%S', now)}"
            "　（工具栏「工具 → 计时器与定时赋值」可掐表并定时给点位写值）"
        )

    def _refresh_status(self) -> None:
        if self.engine is None:
            return
        online, total = self.engine.online_count()
        self.status_device.setText(f"设备 {online}/{total} 在线")
        if self.storage is not None:
            try:
                stats = self.storage.stats()
            except Exception:
                stats = {}
            text = (
                f"归档 {stats.get('history', 0)} / 报警 {stats.get('alarms', 0)}"
                f" / 录波 {stats.get('waveforms', 0)}"
            )
            if self.storage.dropped:
                text += f" / 丢弃 {self.storage.dropped}"
            self.status_storage.setText(text)

    # -------------------------------------------------- 收尾

    def _show_about(self) -> None:
        from .resources import app_pixmap

        box = QMessageBox(self)
        box.setWindowTitle(f"关于 {APP_SHORT}")
        box.setTextFormat(Qt.RichText)
        box.setText(
            "<b>" + APP_NAME + "</b><br>"
            f"版本 {APP_VERSION}　·　Python + PySide6<br><br>"
            f"面向电气行业的开源调试与监视软件。<br>"
            f"{APP_DESCRIPTION}<br>"
            "协议层插件化，当前内置 Modbus TCP / RTU / RTU over TCP、CAN（含 CANopen）、"
            "IEC 61850、西门子 S7 驱动。<br>"
            "工程文件集中放在工程仓库 <b>VVPproject</b> 下（一个工程一个文件夹，"
            "配置 / 数据 / 报表分开），软件目录本身不存工程。<br><br>"
            "<b>数据与画面</b><br>"
            "· 点位于参数表中定义（地址/类型/字序/缩放/报警/录波），可导入导出为 CSV / TXT / JSON<br>"
            "· 数组/块点位用于录波，示波器支持触发、时基、双光标测量<br>"
            "· 主页示意图由工程文件数据驱动，节点可点击进入设备详情<br>"
            "· 点位可双击改参数、右键单发 / 持续发送 / 加到示波器<br>"
            "· 控制台小窗口可一边看数据一边按键<br>"
            "· 工程可保存为工程目录或 .iprj 单文件包，库与参数表路径可自定义<br>"
            "· 界面配色 / 字体 / 字号 / 密度可自定义（默认天蓝主题，含两套深浅蓝调预设），"
            "含逐项颜色的高级设置<br><br>"
            "<b>扩展方式</b>：驱动放 icomm/core/drivers/，保存格式放 icomm/core/writers.py，"
            "主题预设放 icomm/ui/theme.py，都会自动出现在界面上。<br><br>"
            f"<b>开源协议</b>：{__license__}（全文见软件目录下的 LICENSE）<br>"
            f"{__copyright__}<br>"
            "这是<b>个人开源项目</b>，不是任何公司/客户的产品，示例数据均为自造。<br>"
            "第三方组件（PySide6 / python-can / pymodbus / pyserial / python-snap7 / iec61850）"
            "的协议见 THIRD-PARTY-LICENSES.md —— 其中 PySide6 与 python-can 为 LGPL-3.0，"
            "以独立文件形式随程序分发，未做修改，可自行替换。"
        )
        pixmap = app_pixmap(72)
        if pixmap is not None:
            box.setIconPixmap(pixmap)
        box.exec()

    def closeEvent(self, event) -> None:  # noqa: item2
        try:
            if self.settings.confirm_on_exit and self._dirty:
                answer = QMessageBox.question(
                    self,
                    "退出前保存？",
                    f"工程「{self.project.name}」有未保存的改动。\n\n要保存后再退出吗？",
                    QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                    QMessageBox.Save,
                )
                if answer == QMessageBox.Cancel:
                    event.ignore()
                    return
                if answer == QMessageBox.Save:
                    self.save_project()

            # 浮动控制面板要先关掉：Qt.Tool 窗口会让进程一直不退出
            control_panel.close_all()
            if self.engine is not None:
                self.engine.stop()
            if self._simulator is not None:
                self._simulator.stop()
            try:
                self._remember_window_geometry()
                app_settings.save_settings(self.settings, self.base_dir)
            except OSError:
                pass
            self._detach_storage()
        finally:
            super().closeEvent(event)
