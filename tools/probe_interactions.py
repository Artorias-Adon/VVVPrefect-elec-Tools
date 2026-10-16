"""新交互功能的界面探针：双击改参数、右键菜单、持续发送、加到示波器、设置。

真开窗口、真采集、真写设备、真截图，用来确认改界面没改坏。
跑法（必须存成 UTF-8 文件再运行，PowerShell 管道会毁中文）：
    python tools/probe_interactions.py <输出目录>
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import QPoint, Qt  # noqa: E402
from PySide6.QtGui import QContextMenuEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store, settings as app_settings  # noqa: E402
from icomm.ui.dialogs.settings_dialog import SettingsDialog  # noqa: E402
from icomm.ui.main_window import NAV_ITEMS, MainWindow  # noqa: E402
from tools.modbus_sim import ModbusTcpSimulator  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
PORT = 15061
NAV = {name: index for index, name in enumerate(NAV_ITEMS)}
BUS_PID = "储能变流器PCS.有功功率给定"
CTRL_PID = "储能变流器PCS.控制字"


def pick_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            return 0


def pump(app, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def wait_until(app, predicate, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        pump(app, 0.15)
    return False


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {'OK ' if ok else '失败'} {label}" + (f"　{detail}" if detail else ""))
    return ok


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    results: list[bool] = []

    # 每次从干净的设置开始，否则上一次退出时存下的值会让"默认值"检查失真
    settings_file = app_settings.settings_path(OUT)
    if os.path.exists(settings_file):
        os.remove(settings_file)

    port = pick_port(PORT)
    sim = ModbusTcpSimulator("127.0.0.1", port, verbose=False)
    threading.Thread(target=sim.serve_forever, kwargs={"freeze": False}, daemon=True).start()
    time.sleep(0.4)
    print(f"仿真器监听 127.0.0.1:{port}")

    project = project_io.bess_project()
    project.channels[0].options["port"] = port
    device = project.devices[0]
    device.poll_interval_ms = 100

    workdir = os.path.join(OUT, "projects")
    root = os.path.join(workdir, project_store.safe_name(project.name))
    if os.path.exists(root):
        shutil.rmtree(root)
    paths = project_store.create_project_dir(root, project)

    app = QApplication(sys.argv[:1])
    window = MainWindow(project, paths, workdir=workdir, base_dir=OUT)
    # 持续发送的二次确认会弹模态框卡住探针，这里先关掉（产品默认是开的）
    window.settings.periodic_confirm = False
    window.resize(1560, 940)
    window.show()
    pump(app, 0.6)

    window.nav.setCurrentRow(NAV["运行监视"])
    window.start_engine()
    tags = window.engine.tags
    print("\n[0] 采集就绪")
    results.append(check(
        "采到数据",
        wait_until(app, lambda: tags.get("储能变流器PCS.A相电压").quality == "good", 25.0),
        f"A相电压={tags.get('储能变流器PCS.A相电压').value:.1f}V",
    ))
    pump(app, 0.5)
    window.grab().save(os.path.join(OUT, "01_monitor.png"))

    # ---------------- 1. 双击点位卡片打开参数对话框 ----------------
    print("\n[1] 双击点位卡片 -> 参数对话框")
    card = next(
        (w for pid, w in window.dashboard_page._items.items() if pid == BUS_PID), None
    )
    results.append(check("监视页上找到有功功率给定卡片", card is not None))
    if card is None:
        window.close()
        sim.stop()
        return 1
    card.show()
    pump(app, 0.2)
    QTest.mouseDClick(card, Qt.LeftButton, pos=QPoint(30, 20))
    opened = wait_until(app, lambda: window._point_dialog is not None, 3.0)
    dialog = window._point_dialog
    results.append(check("双击弹出了参数对话框", opened, dialog.windowTitle() if dialog else ""))
    if dialog is not None:
        pump(app, 0.4)
        dialog.grab().save(os.path.join(OUT, "02_point_dialog.png"))

    # ---------------- 2. 在对话框里单发写值 ----------------
    print("\n[2] 对话框里改参数并单发")
    if dialog is not None:
        dialog.value_box.setValue(208.0)
        dialog._emit_single()
        results.append(check(
            "单发写入后设备值变了",
            wait_until(app, lambda: abs(float(tags.get(BUS_PID).value or 0) - 208.0) < 1e-3, 6.0),
            f"回读={tags.get(BUS_PID).value}",
        ))

    # ---------------- 3. 持续发送 ----------------
    print("\n[3] 持续发送")
    if dialog is not None:
        dialog.value_box.setValue(216.0)
        dialog.interval_box.setValue(200)
        dialog._emit_periodic()
        results.append(check(
            "开始持续发送",
            wait_until(app, lambda: window._periodic_of(BUS_PID) is not None, 3.0),
        ))
        task = window._periodic_of(BUS_PID)
        results.append(check(
            "持续发送真的在发",
            wait_until(app, lambda: (window._periodic_of(BUS_PID) or task).sent >= 4, 8.0),
            f"已发 {window._periodic_of(BUS_PID).sent if window._periodic_of(BUS_PID) else 0} 次",
        ))
        results.append(check(
            "写入值被设备接受",
            abs(float(tags.get(BUS_PID).value or 0) - 216.0) < 1e-3,
            f"回读={tags.get(BUS_PID).value}",
        ))
        pump(app, 0.5)
        dialog.grab().save(os.path.join(OUT, "03_periodic_running.png"))

    # ---------------- 4. 右键菜单 ----------------
    print("\n[4] 右键菜单")
    seen_menus: list[tuple[str, list[str]]] = []
    card.contextRequested.connect(lambda pid, pos: seen_menus.append((pid, [])))
    card.show()
    pump(app, 0.2)
    event = QContextMenuEvent(
        QContextMenuEvent.Mouse, QPoint(20, 20), card.mapToGlobal(QPoint(20, 20))
    )
    QApplication.sendEvent(card, event)
    pump(app, 0.2)
    results.append(check("右键发出了菜单请求", bool(seen_menus), f"{seen_menus[:1]}"))

    menu = window.build_point_menu(BUS_PID)
    labels = [a.text() for a in menu.actions()] if menu else []
    print(f"       菜单项：{labels}")
    results.append(check("菜单含单发写入", any("单发写入" in t for t in labels)))
    results.append(check("菜单含停止持续发送", any("停止持续发送" in t for t in labels)))
    results.append(check("菜单含添加到示波器", any("添加到示波器" in t for t in labels)))
    results.append(check("菜单含编辑点位定义", any("编辑点位定义" in t for t in labels)))

    readonly_menu = window.build_point_menu("储能变流器PCS.直流母线电压")
    readonly_labels = [a.text() for a in readonly_menu.actions()]
    results.append(check(
        "只读点位菜单提示不可写",
        any("只读" in t for t in readonly_labels),
        str(readonly_labels),
    ))
    results.append(check(
        "只读点位也能加到示波器",
        any("添加到示波器" in t for t in readonly_labels),
    ))

    # 命令点位（控制字）的菜单
    cmd_menu = window.build_point_menu(CTRL_PID)
    cmd_labels = [a.text() for a in cmd_menu.actions()]
    results.append(check(
        "可写命令点位菜单含持续发送",
        any("持续发送" in t for t in cmd_labels),
        str(cmd_labels),
    ))

    # 通过菜单项真正执行"单发"
    action = next((a for a in menu.actions() if a.text() == "停止持续发送" or "停止持续发送" in a.text()), None)
    if action is not None:
        action.trigger()
        results.append(check(
            "菜单停止持续发送生效",
            wait_until(app, lambda: window._periodic_of(BUS_PID) is None, 3.0),
        ))
        results.append(check(
            "配置已从工程里移除",
            project.periodic_write(BUS_PID) is None,
        ))

    # ---------------- 5. 添加到示波器 ----------------
    print("\n[5] 添加到示波器（任意点位）")
    readonly_pid = "储能变流器PCS.直流母线电压"
    point = project.find_point(readonly_pid)[1]
    results.append(check("该点位本来没标示波器", not point.scope))
    scope_page = window.scope_page
    before = scope_page.channel_list.count()
    window._open_point_in_scope(readonly_pid)
    pump(app, 0.6)
    results.append(check("已切到示波器页", window.nav.currentRow() == NAV["示波器"]))
    results.append(check("通道列表里出现了它", readonly_pid in scope_page._channels))
    results.append(check("画布上可见", readonly_pid in [c.pid for c in scope_page.canvas.visible_channels()]))
    print(f"       通道数 {before} -> {scope_page.channel_list.count()}")
    pump(app, 1.2)
    window.grab().save(os.path.join(OUT, "04_scope_added.png"))

    print("\n[6] 示波器「显示全部点位」")
    scope_page.show_all_box.setChecked(True)
    pump(app, 0.8)
    total = scope_page.channel_list.count()
    results.append(check(
        "列出了全部点位", total == len(project.all_point_ids()),
        f"{total} / {len(project.all_point_ids())} 个",
    ))
    window.grab().save(os.path.join(OUT, "05_scope_all.png"))

    # ---------------- 7. 设置对话框 ----------------
    print("\n[7] 设置对话框")
    settings_dialog = SettingsDialog(window.settings, project, list(project_io.BUILTIN_SAMPLES), parent=window)
    settings_dialog.resize(620, 700)
    tabs = [settings_dialog.tabs.tabText(i) for i in range(settings_dialog.tabs.count())]
    results.append(check("两个标签页", tabs == ["软件设置", "工程设置"], str(tabs)))
    settings_dialog.show()
    pump(app, 0.5)
    settings_dialog.grab().save(os.path.join(OUT, "06_settings_app.png"))
    settings_dialog.tabs.setCurrentIndex(1)
    pump(app, 0.4)
    settings_dialog.grab().save(os.path.join(OUT, "07_settings_project.png"))

    settings_dialog.periodic_interval_box.setValue(350)
    settings_dialog.reconnect_box.setValue(1.25)
    settings_dialog.scope_all_points.setChecked(True)
    settings_dialog.archive_box.setValue(4)
    settings_dialog.retention_box.setValue(120)
    settings_dialog.accept()
    pump(app, 0.3)
    window.apply_settings(settings_dialog)
    pump(app, 0.4)

    saved = app_settings.load_settings(OUT)
    results.append(check(
        "软件设置已落盘",
        saved.periodic_default_interval_ms == 350,
        f"默认周期={saved.periodic_default_interval_ms} ms",
    ))
    results.append(check(
        "重连间隔已落盘",
        abs(saved.reconnect_interval_s - 1.25) < 1e-6,
        f"{saved.reconnect_interval_s}s",
    ))
    results.append(check(
        "工程设置已改",
        project.history_interval_s == 4 and project.history_retention_days == 120,
        f"间隔={project.history_interval_s}s 保留={project.history_retention_days}天",
    ))
    results.append(check(
        "示波器套用了新设置",
        scope_page.show_all_box.isChecked(),
    ))
    results.append(check(
        "引擎套用了新的超时设置",
        window.engine is not None
        and abs(window.engine.reconnect_interval_s - 1.25) < 1e-6,
        f"{window.engine.reconnect_interval_s if window.engine else '-'}s",
    ))

    # ---------------- 8. 持续发送面板 ----------------
    print("\n[8] 持续发送面板")
    window._start_periodic(BUS_PID, 220.0, 250)
    pump(app, 0.5)
    results.append(check("重新开始一条持续发送", window._periodic_of(BUS_PID) is not None))
    from icomm.ui.dialogs.periodic_panel import PeriodicPanel

    panel = PeriodicPanel(
        project,
        list(window.engine.periodic_tasks()),
        engine_running=window.engine.running,
        parent=window,
    )
    panel.resize(800, 380)
    panel.show()
    pump(app, 0.4)
    rows = panel.table.rowCount()
    results.append(check("面板列出了任务", rows >= 1, f"{rows} 行"))
    panel.grab().save(os.path.join(OUT, "08_periodic_panel.png"))

    # 走一遍"全部停止"
    panel._stop_all()
    for pid in panel.stopped:
        window._stop_periodic(pid)
    pump(app, 0.4)
    results.append(check(
        "全部停止后没有任务在发",
        window.engine.periodic_tasks() == [],
    ))
    results.append(check("工程配置也清空了", project.periodic_writes == []))

    # ---------------- 9. 工程保存后配置仍在 ----------------
    print("\n[9] 保存工程（RTU/TCP 配置 + 持续发送配置）")
    window.stop_engine()
    project.channels[0].switch_link("rtu", {"port": "COM5", "baudrate": 19200})
    project.channels[0].options["port"] = "COM5"
    project.channels[0].remember_options()
    project.set_periodic_write(BUS_PID, 200.0, 900)
    window.save_project()
    pump(app, 0.5)
    reloaded, _paths = project_store.open_project(paths.project_file)
    channel = reloaded.channels[0]
    results.append(check("工程存下了 rtu 链路", channel.link == "rtu"))
    results.append(check(
        "串口号记住了", channel.options.get("port") == "COM5", str(channel.options.get("port"))
    ))
    channel.switch_link("tcp", {"host": "127.0.0.1", "port": port})
    results.append(check(
        "切回 tcp 时端口还在", channel.options.get("port") == port, str(channel.options.get("port"))
    ))
    entry = reloaded.periodic_write(BUS_PID)
    results.append(check(
        "持续发送配置存下来了",
        entry is not None and entry.interval_ms == 900,
        f"{entry.value if entry else None} / {entry.interval_ms if entry else None} ms",
    ))
    results.append(check("工程里只有这一条配置", len(reloaded.periodic_writes) == 1))

    print("\n[10] 退出确认")
    results.append(check("默认开启退出确认", window.settings.confirm_on_exit))

    window.grab().save(os.path.join(OUT, "09_final.png"))
    window.settings.confirm_on_exit = False
    window.close()
    sim.stop()
    pump(app, 0.4)

    passed = sum(1 for r in results if r)
    print(f"\n结果：{passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
