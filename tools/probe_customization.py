"""界面自定义 / 保存格式 / 通信配置 / 按键控制面板的界面探针。

真开窗口、真截图、真切换主题、真保存文件、真按按键。
跑法（必须存成 UTF-8 文件再运行，PowerShell 管道会毁中文）：
    python tools/probe_customization.py <输出目录>
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store, settings as app_settings, writers  # noqa: E402
from icomm.ui import theme  # noqa: E402
from icomm.ui.dialogs.control_panel import close_all  # noqa: E402
from icomm.ui.dialogs.save_as_dialog import SaveAsDialog  # noqa: E402
from icomm.ui.main_window import NAV_ITEMS, MainWindow  # noqa: E402
from tools.modbus_sim import ModbusTcpSimulator  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
PORT = 15062
NAV = {name: index for index, name in enumerate(NAV_ITEMS)}
CTRL_PID = "储能变流器PCS.控制字"
STATUS_PID = "储能变流器PCS.运行状态字"

results: list[bool] = []


def check(label: str, ok: bool, detail: str = "") -> bool:
    results.append(bool(ok))
    print(f"  {'OK ' if ok else '失败'} {label}" + (f"　{detail}" if detail else ""))
    return bool(ok)


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


def shot(widget, name: str) -> str:
    path = os.path.join(OUT, name)
    widget.grab().save(path)
    print(f"       截图 -> {os.path.basename(path)}")
    return path


def pick_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            return 0


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    settings_file = app_settings.settings_path(OUT)
    if os.path.exists(settings_file):
        os.remove(settings_file)

    port = pick_port(PORT)
    sim = ModbusTcpSimulator("127.0.0.1", port, verbose=False)
    threading.Thread(target=sim.serve_forever, kwargs={"freeze": False}, daemon=True).start()
    time.sleep(0.4)

    project = project_io.bess_project()
    project.channels[0].options["port"] = port
    project.devices[0].poll_interval_ms = 150

    workdir = os.path.join(OUT, "projects")
    root = os.path.join(workdir, project_store.safe_name(project.name))
    if os.path.exists(root):
        shutil.rmtree(root)
    paths = project_store.create_project_dir(root, project)

    app = QApplication(sys.argv[:1])
    window = MainWindow(project, paths, workdir=workdir, base_dir=OUT)
    window.settings.confirm_on_exit = False
    window.resize(1560, 940)
    window.show()
    pump(app, 0.6)

    print("\n[1] 程序图标")
    from icomm.ui.resources import app_icon

    icon = app_icon()
    check("窗口图标已套用", window.windowIcon() is not None and not window.windowIcon().isNull())
    check("图标含全部尺寸", icon is not None and len(icon.availableSizes()) == 7,
          f"{len(icon.availableSizes()) if icon else 0} 个尺寸")

    print("\n[2] 设置窗口：四个标签页")
    from icomm.ui.dialogs.settings_dialog import SettingsDialog

    dialog = SettingsDialog(window.settings, project, list(project_io.BUILTIN_SAMPLES), parent=window)
    dialog.appearanceChanged.connect(window._refresh_appearance)
    dialog.resize(1000, 760)
    dialog.show()
    pump(app, 0.5)
    tabs = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
    check("标签页齐全", tabs == ["界面外观", "通信", "软件设置", "工程设置"], str(tabs))
    shot(dialog, "01_settings_appearance.png")

    print("\n[3] 界面自定义：预设主题实时预览")
    for name in theme.preset_names():
        index = dialog.appearance_editor.preset_combo.findData(name)
        dialog.appearance_editor.preset_combo.setCurrentIndex(index)
        pump(app, 0.5)
        applied = theme.appearance().palette.name == name
        check(f"切到「{name}」并生效", applied, f"bg={theme.BG}")
        shot(window, f"02_theme_{theme.preset_names().index(name) + 1}_{name}.png")

    print("\n[4] 字号 / 密度 / 逐项自定义颜色")
    window.nav.setCurrentRow(NAV["运行监视"])
    pump(app, 0.4)
    dialog.appearance_editor.size_box.setValue(16)
    dialog.appearance_editor.density_combo.setCurrentIndex(
        dialog.appearance_editor.density_combo.findData("宽松")
    )
    pump(app, 0.6)
    check("字号已生效", "font-size: 16px" in theme.STYLESHEET)
    check("密度已生效", theme.appearance().density == "宽松")
    shot(window, "03_large_loose.png")

    button = dialog.appearance_editor._colour_buttons["accent"]
    button.set_colour("#ff8f1f")
    button.colorChanged.emit("#ff8f1f")
    pump(app, 0.4)
    check("高级逐项改色生效", theme.ACCENT == "#ff8f1f", f"accent={theme.ACCENT}")
    check("颜色项覆盖全部调色板字段",
          len(dialog.appearance_editor._colour_buttons) >= 25,
          f"{len(dialog.appearance_editor._colour_buttons)} 项")
    shot(dialog, "04_settings_advanced.png")

    # 还原，后面截图用默认主题好看点
    dialog.appearance_editor.preset_combo.setCurrentIndex(
        dialog.appearance_editor.preset_combo.findData("深色工业")
    )
    dialog.appearance_editor.size_box.setValue(13)
    dialog.appearance_editor.density_combo.setCurrentIndex(
        dialog.appearance_editor.density_combo.findData("标准")
    )
    pump(app, 0.5)

    print("\n[5] 通信标签页：Modbus / TCP / RTU 参数")
    dialog.tabs.setCurrentIndex(1)
    pump(app, 0.5)
    editor = dialog.channel_editor
    check("列出了全部通道", editor.list.count() == len(project.channels),
          f"{editor.list.count()} 条")
    check("有 TCP 参数输入框",
          "host" in editor._option_widgets and "port" in editor._option_widgets,
          str(sorted(editor._option_widgets)))
    show_tcp = editor._option_widgets["host"].text()
    editor._option_widgets["host"].setText("192.168.30.11")
    editor._option_widgets["port"].setValue(1502)
    editor.apply_form(silent=True)
    pump(app, 0.4)
    shot(dialog, "05_settings_channels_tcp.png")

    # 注意：切换链路后表单会重建（参数集不一样），所以每次都要重新取控件，
    # 不能缓存 link_combo。设置窗口改的也是工程**副本**，确定后才提交回真工程。
    editor.link_combo.setCurrentIndex(editor.link_combo.findData("rtu"))
    pump(app, 0.6)
    edited_channel = editor.project.channels[0]
    check("切到 RTU 后有串口参数",
          "baudrate" in editor._option_widgets and "parity" in editor._option_widgets,
          str(sorted(editor._option_widgets)))
    check("RTU 端口是字符串（没沿用 TCP 端口）",
          isinstance(edited_channel.options.get("port"), str),
          repr(edited_channel.options.get("port")))
    editor._option_widgets["port"].setText("COM9")
    pump(app, 0.3)
    shot(dialog, "06_settings_channels_rtu.png")

    editor.link_combo.setCurrentIndex(editor.link_combo.findData("tcp"))
    pump(app, 0.5)
    check("切回 TCP 恢复了原参数",
          edited_channel.options.get("host") == "192.168.30.11"
          and edited_channel.options.get("port") == 1502,
          f"{edited_channel.options.get('host')}:{edited_channel.options.get('port')}")
    check("两套参数都记住了",
          set(edited_channel.options_by_link) >= {"tcp", "rtu"},
          str(sorted(edited_channel.options_by_link)))
    check("RTU 的参数也在", edited_channel.options_by_link["rtu"]["port"] == "COM9")

    before_channels = len(editor.project.channels)
    editor.add_channel()
    pump(app, 0.3)
    check("新建通道", len(editor.project.channels) == before_channels + 1)
    editor.duplicate_channel()
    pump(app, 0.3)
    check("复制通道", len(editor.project.channels) == before_channels + 2)
    shot(dialog, "07_settings_channels_multi.png")

    # 恢复成干净状态，免得后面启动采集时连不上
    editor.reload(keep=edited_channel.name)
    pump(app, 0.3)
    editor._option_widgets["host"].setText("127.0.0.1")
    editor._option_widgets["port"].setValue(port)
    editor.apply_form(silent=True)
    # 删掉刚加的两条测试通道
    while len(editor.project.channels) > 1:
        editor.project.channels.pop()
    editor.reload()

    dialog.accept()
    window.apply_settings(dialog)
    pump(app, 0.4)
    check("设置已落盘", os.path.exists(settings_file))
    check("通道改动写回真工程",
          len(project.channels) == 1 and project.channels[0].options["port"] == port,
          f"{len(project.channels)} 条通道，端口 {project.channels[0].options['port']}")
    check("真工程记住了两套链路参数",
          set(project.channels[0].options_by_link) >= {"tcp", "rtu"},
          str(sorted(project.channels[0].options_by_link)))

    print("\n[6] 保存功能细分：参数设置可选格式")
    save_dialog = SaveAsDialog(
        "保存参数设置", writers.parameter_writers(), default_name="储能参数", parent=window
    )
    save_dialog.resize(620, 420)
    save_dialog.show()
    pump(app, 0.4)
    formats = [w.label for w in writers.parameter_writers()]
    check("格式可选", len(formats) == 3, str(formats))
    shot(save_dialog, "08_save_parameters.png")

    saved_files = []
    total_points = len(window.project.all_point_ids())
    for writer in writers.parameter_writers():
        target = os.path.join(OUT, "导出参数" + writer.suffix)
        count = writer.write(window.project, target)
        size = os.path.getsize(target)
        saved_files.append((writer.label, os.path.basename(target), size))
        check(f"保存为「{writer.label}」", count == total_points and size > 0,
              f"{count} 点位 / {size} 字节")
    save_dialog.close()

    json_path = os.path.join(OUT, "导出参数.json")
    target = project_io.Project(name="回读校验")
    report = writers.JsonParameterWriter().read(target, json_path)
    check("JSON 能读回来", report.ok and report.points_added == total_points,
          f"+{report.points_added} 点位")

    print("\n[7] 保存功能细分：整个工程可选格式")
    proj_dialog = SaveAsDialog(
        "保存整个工程", writers.project_writers(), default_name="整包工程", parent=window
    )
    proj_dialog.resize(620, 420)
    proj_dialog.show()
    pump(app, 0.4)
    proj_formats = [w.label for w in writers.project_writers()]
    check("工程格式可选", len(proj_formats) == 2, str(proj_formats))
    shot(proj_dialog, "09_save_project.png")
    proj_dialog.close()

    iprj_path = os.path.join(OUT, "整包工程.iprj")
    packed = writers.IprjWriter().write(window.project, window.paths, iprj_path)
    check("打包 .iprj", os.path.exists(packed), f"{os.path.getsize(packed)} 字节")
    opened, _ = project_store.open_project(packed, workdir=os.path.join(OUT, "解开"))
    check("工程包可打开", len(opened.devices[0].points) == len(project.devices[0].points))
    check("控制字按钮还在",
          len(opened.device("储能变流器PCS").point("控制字").command_items()) == 5)

    dir_path = os.path.join(OUT, "保存为目录")
    dir_paths = writers.ProjectDirWriter().write(window.project, window.paths, dir_path)
    check("保存为工程目录",
          os.path.exists(dir_paths.project_file) and os.path.exists(dir_paths.csv_file),
          os.path.basename(dir_paths.root))

    print("\n[8] 按键控制小窗口")
    window.start_engine()
    check("采集已启动", window.engine is not None and window.engine.running)
    check("采到数据", wait_until(
        app, lambda: window.engine.tags.get("储能变流器PCS.A相电压").quality == "good", 25.0))

    window.show_device_control_panel("储能变流器PCS")
    pump(app, 0.8)
    from icomm.ui.dialogs import control_panel

    panel = next(p for key, p in control_panel._open_panels.items() if key.startswith("device::"))
    keys = [b.text() for b in panel.findChildren(QPushButton) if b.objectName() == "controlKey"]
    # 期望值直接由参数表推出：所有「命令」列的按键之和
    command_points = [
        point for point in project.devices[0].points if point.command_items()
    ]
    expected = sum(len(p.command_items()) for p in command_points)
    check("控制台按键数 = 参数表里「命令」列的总数", len(keys) == expected,
          f"{len(keys)} 个（{len(command_points)} 个命令点位）")
    for label in ("开机", "关机", "充电", "放电", "复位"):
        if label not in keys:
            check(f"控制台含「{label}」", False, str(keys))
            break
    # 关键：位掩码类点位（功能配置字的按位开关等）默认不展开，否则面板上键会很多
    mask_only = [
        point.title
        for point in project.devices[0].points
        if not point.command_items() and point.bit_labels()
    ]
    check("位掩码点位默认不展开到面板",
          not any(title in keys for title in mask_only),
          f"未展开：{mask_only[:3]}")
    shot(panel, "10_control_panel_device.png")

    print("\n[8b] 自选补充点位（位掩码类默认不展开）")
    picker = control_panel.PanelPointPicker(project.devices[0], [], parent=window)
    picker.resize(560, 420)
    picker.show()
    pump(app, 0.5)
    total_options = len(picker._boxes)
    controllable = [
        point
        for point in project.devices[0].points
        if point.command_items() or (point.writable and point.bit_labels())
    ]
    check("可选点位清单覆盖全部可控点位", total_options == len(controllable),
          f"{total_options} 个候选")
    shot(picker, "10b_panel_point_picker.png")
    bit_pid = "储能变流器PCS.功能配置字"
    picker._boxes[bit_pid].setChecked(True)
    chosen = picker.selected()
    picker.close()
    check("勾选生效", chosen == [bit_pid], str(chosen))

    window.settings.panel_points["储能变流器PCS"] = chosen
    control_panel.close_device_panel("储能变流器PCS")
    window.show_device_control_panel("储能变流器PCS")
    pump(app, 0.8)
    panel = next(p for key, p in control_panel._open_panels.items() if key.startswith("device::"))
    keys2 = [b.text() for b in panel.findChildren(QPushButton) if b.objectName() == "controlKey"]
    check("自选点位已加入控制台", 6 < len(keys2) < 30, f"{len(keys2)} 个按键")
    shot(panel, "10c_control_panel_extra.png")

    state_point = project.devices[0].point("运行状态字")
    labels = state_point.bit_labels()
    before = window.engine.tags.get(STATUS_PID).value
    run_key = next(b for b in panel.findChildren(QPushButton) if b.text() == "放电")
    run_key.click()
    # 状态字一位一个状态：写控制字 0x08（放电）后仿真器置 bit3，界面解出来就是「放电」
    running = wait_until(
        app,
        lambda: "放电"
        in state_point.decode_bits(window.engine.tags.get(STATUS_PID).value, labels),
        20.0,
    )
    after = window.engine.tags.get(STATUS_PID).value
    check("按「放电」后状态字显示「放电」", running, f"{before} -> {after}")
    pump(app, 0.4)
    shot(panel, "11_control_panel_running.png")

    # 单点位面板
    window.show_point_control_panel(CTRL_PID)
    pump(app, 0.8)
    point_panel = control_panel._open_panels.get(f"point::{CTRL_PID}")
    check("单点位面板已打开", point_panel is not None)
    if point_panel is not None:
        check("面板是非模态浮动窗口",
              not point_panel.isModal() and bool(point_panel.windowFlags() & Qt.Tool))
        shot(point_panel, "12_control_panel_point.png")

    # 可同时开多个
    window.show_point_control_panel("储能变流器PCS.功能配置字")
    pump(app, 0.6)
    check("可同时开多个面板", len(control_panel._open_panels) >= 3,
          f"{len(control_panel._open_panels)} 个")

    print("\n[9] 右键菜单里的「调出按键控制小窗口」")
    menu = window.build_point_menu(CTRL_PID)
    labels = [a.text() for a in menu.actions()]
    check("菜单含按键面板", any("按键控制小窗口" in t for t in labels), str(labels))
    readonly_menu = window.build_point_menu("储能变流器PCS.直流母线电压")
    readonly_labels = [a.text() for a in readonly_menu.actions()]
    check("只读点位不给按键面板", not any("按键控制小窗口" in t for t in readonly_labels))

    print("\n[10] 设备详情页的「调出控制台」按钮")
    window.nav.setCurrentRow(NAV["主页"])
    pump(app, 0.3)
    window.show_device_detail("储能变流器PCS")
    pump(app, 0.6)
    has_button = any(
        b.text() == "调出控制台" for b in window.device_detail_page.findChildren(QPushButton)
    )
    check("详情页有控制台按钮", has_button)
    shot(window, "13_device_detail.png")

    close_all()
    window.settings.confirm_on_exit = False
    window.engine.stop()
    window.grab().save(os.path.join(OUT, "14_final.png"))
    window.close()
    sim.stop()
    pump(app, 0.4)

    print("\n保存格式清单:")
    for entry in writers.writer_summary():
        print("   %-9s %-14s %-8s %s" % (
            "参数表" if entry["kind"] == "parameter" else "工程",
            entry["label"], entry["suffix"] or "(目录)", entry["description"]))

    passed = sum(1 for r in results if r)
    print(f"\n结果：{passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
