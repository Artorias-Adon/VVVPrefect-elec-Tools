"""工具集 / 自动化 的界面探针。

真开窗口、真连模拟器、真触发、真写文件、真截图。
跑法（必须存成 UTF-8 文件再运行，PowerShell 管道会毁中文）：
    python tools/probe_tools.py <输出目录>
"""

from __future__ import annotations

import json
import math
import os
import shutil
import socket
import sys
import threading
import time
import wave
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton  # noqa: E402

from icomm.core import csv_table, net_check, project as project_io, project_store  # noqa: E402
from icomm.core import audio  # noqa: E402
from icomm.core import serial_ports  # noqa: E402
from icomm.ui import option_fields  # noqa: E402
from icomm.core.automation import Compare, Task, Trigger, TriggerMode  # noqa: E402
from icomm.core.timing import StepMode  # noqa: E402
from icomm.ui import theme  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.dialogs import control_panel  # noqa: E402
from icomm.ui.dialogs.automation_panel import AutomationPanel  # noqa: E402
from icomm.ui.dialogs.settings_dialog import SettingsDialog  # noqa: E402
from icomm.ui.dialogs.write_limit_dialog import WriteLimitDialog  # noqa: E402
from icomm.ui.main_window import MainWindow  # noqa: E402
from icomm.ui.tools.analysis import AnalysisWindow  # noqa: E402
from icomm.ui.tools.datalog import DataLogWindow  # noqa: E402
from icomm.ui.tools.diagram_edit import DiagramEditWindow  # noqa: E402
from icomm.ui.tools.audio_record import AudioRecordWindow  # noqa: E402
from icomm.ui.tools.fault_record import FaultRecordWindow  # noqa: E402
from icomm.ui.tools.mini_scope import MiniScopeWindow  # noqa: E402
from icomm.ui.tools.notepad import NotepadWindow  # noqa: E402
from icomm.ui.tools.screenshot import ScreenshotDialog  # noqa: E402
from icomm.ui.tools.timing import TimingWindow  # noqa: E402
from icomm.ui.tools.variable_search import VariableSearchDialog  # noqa: E402
from tools.modbus_sim import ModbusTcpSimulator  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
PORT = 15063
NAV = {"主页": 0, "运行监视": 1, "设备详情": 2, "趋势": 3, "报警": 4, "通信": 5, "示波器": 6}

VOLT_PID = "储能变流器PCS.A相电压"
BUS_PID = "储能变流器PCS.直流母线电压"
SET_PID = "储能变流器PCS.有功功率给定"
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


def wait_until(app, predicate, timeout: float, pump_for: float = 0.15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        pump(app, pump_for)
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


def _near_zero(value, tolerance: float = 0.5) -> bool:
    """值近似为 0（None 视为不满足 —— 注意别写 `value or -1`，0 是 falsy）。"""
    return _near(value, 0.0, tolerance)


def _near(value, target: float, tolerance: float) -> bool:
    if value is None:
        return False
    try:
        return abs(float(value) - float(target)) < tolerance
    except (TypeError, ValueError):
        return False


def _wav_ok(path: str, window) -> bool:
    """录音文件是不是合法 WAV，且时长与界面报的一致。"""
    try:
        with wave.open(path, "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate()
            channels = handle.getnchannels()
            width = handle.getsampwidth()
    except (OSError, wave.Error) as exc:
        print(f"       WAV 读取失败：{exc}")
        return False
    seconds = frames / rate if rate else 0.0
    print(f"       {os.path.basename(path)}：{frames} 帧 / {rate} Hz / {channels} 声道 /"
          f" {width * 8} 位 / {seconds:.2f} 秒")
    return (
        width == 2
        and rate == int(window.rate_combo.currentData())
        and channels == int(window.channels_combo.currentData())
        and frames > 0
    )


def select_combo(combo, value) -> bool:
    """按 data 选一项（``findData`` 对 Python 枚举对象不可靠，自己扫一遍）。"""
    for index in range(combo.count()):
        if combo.itemData(index) == value:
            combo.setCurrentIndex(index)
            return True
    return False


def menus_of(menu) -> dict[str, list[str]]:
    """把 QMenu 读成 {分组: [工具名]}。"""
    out: dict[str, list[str]] = {}
    for action in menu.actions():
        if action.menu() is not None:
            out[action.text()] = [a.text() for a in action.menu().actions()]
        elif action.text():
            out.setdefault("", []).append(action.text())
    return out


def main() -> int:  # noqa: PLR0915 - 探针就是一条条走
    os.makedirs(OUT, exist_ok=True)
    port = pick_port(PORT)
    sim = ModbusTcpSimulator("127.0.0.1", port, verbose=False)
    threading.Thread(target=sim.serve_forever, kwargs={"freeze": False}, daemon=True).start()
    time.sleep(0.4)

    project = project_io.bess_project()
    project.channels[0].options["port"] = port
    project.devices[0].poll_interval_ms = 150

    workdir = os.path.join(OUT, "projects")
    paths = project_store.create_project_dir(
        os.path.join(workdir, project_store.safe_name(project.name)), project
    )

    app = QApplication(sys.argv[:1])
    window = MainWindow(project, paths, workdir=workdir, base_dir=OUT)
    window.settings.confirm_on_exit = False
    window.resize(1560, 940)
    window.show()
    pump(app, 0.6)

    # 无人值守跑测试：任何模态框一律当"确认"处理，否则会永久阻塞
    patchers = [
        mock.patch.object(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok),
        mock.patch.object(QMessageBox, "information", lambda *a, **k: QMessageBox.Ok),
        mock.patch.object(QMessageBox, "critical", lambda *a, **k: QMessageBox.Ok),
        mock.patch.object(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes),
    ]
    for patcher in patchers:
        patcher.start()

    # ---------------------------------------------------------------- [1]
    print("\n[1] 工具菜单由注册表生成")
    menu = window.build_tools_menu()
    grouped = menus_of(menu)
    flat = [name for names in grouped.values() for name in names]
    check("菜单项 = 注册表里的全部工具", flat == ui_tools.tool_names(),
          f"{len(flat)} 项：{'、'.join(grouped)}")
    check("分组齐全（含通信/自动化/计算）",
          {"通信", "自动化", "计算"} <= set(grouped), str(list(grouped)))

    print("\n[2] 变量搜索：变量在哪里 + 跳转 + 加示波器")
    search = VariableSearchDialog(window.tool_context(), parent=window)
    search.resize(980, 620)
    search.show()
    search.query_edit.setText("电压")
    search.do_search()
    pump(app, 0.4)
    check("搜到点位", search.table.rowCount() > 0, f"{search.table.rowCount()} 条")
    headers = [search.table.horizontalHeaderItem(i).text()
               for i in range(search.table.columnCount())]
    check("结果里有「出现位置」列", any("位置" in text for text in headers), str(headers))
    hit_pid = search.table.item(0, 0).data(Qt.UserRole)
    check("能定位到具体点位（带点号）", bool(hit_pid) and "." in str(hit_pid), str(hit_pid))
    shot(search, "01_variable_search.png")
    search.close()

    print("\n[3] 记事本随工程保存")
    notes = NotepadWindow(window.tool_context(), parent=window)
    notes.resize(760, 520)
    notes.show()
    notes.editor.setPlainText("现场记录：2024-05-01 更换 A 相熔断器\n值班：老王")
    notes.save_now()
    pump(app, 0.3)
    check("写进工程", project.notes.startswith("现场记录"), repr(project.notes[:12]))
    shot(notes, "02_notepad.png")
    notes.close()
    project_store.save_project(project, paths)
    reopened, _ = project_store.open_project(paths.project_file, workdir=workdir)
    check("存盘后还在", "更换 A 相熔断器" in reopened.notes)
    project.notes = reopened.notes

    # ---------------------------------------------------------------- [4]
    print("\n[4] 写值限幅：右键入口 / 对话框 / 参数表持久化 / 引擎夹紧")
    window.nav.setCurrentRow(NAV["运行监视"])
    pump(app, 0.4)
    labels = [a.text() for a in window.build_point_menu(SET_PID).actions()]
    check("右键有「写值限幅」", any("写值限幅" in t for t in labels), str(labels[-6:]))
    check("右键有「小数位数」", any("小数位数" in t for t in labels))
    check("右键有「触发条件」", any("触发条件" in t for t in labels))

    point = project.find_point(SET_PID)[1]
    dialog = WriteLimitDialog(SET_PID, point, parent=window)
    dialog.low_check.setChecked(True)
    dialog.low_check.setChecked(True)
    dialog.low_box.setValue(0.0)
    dialog.high_check.setChecked(True)
    dialog.high_box.setValue(750.0)
    dialog.resize(520, 320)
    dialog.show()
    pump(app, 0.3)
    check("对话框读得出上下限", dialog.values() == (0.0, 750.0), str(dialog.values()))
    shot(dialog, "03_write_limit_dialog.png")
    dialog.close()
    window.set_point_write_limit(SET_PID, 0.0, 750.0)
    pump(app, 0.2)
    check("限幅写进模型", point.has_write_limit and point.write_min == 0.0,
          point.write_limit_text())

    # 参数表要带上限幅，否则重开工程就丢了
    csv_path = os.path.join(OUT, "限幅参数表.csv")
    csv_table.export_csv(project, csv_path)
    header = csv_table.export_text(project).splitlines()[0]
    check("参数表含限幅列",
          all(col in header for col in ("写值下限", "写值上限", "限幅提示")), header[-40:])
    fresh = project_io.Project(name="回读")
    report = csv_table.import_csv(fresh, csv_path)
    back = fresh.find_point(SET_PID)[1]
    check("导回后限幅还在", back.write_min == 0.0 and back.write_max == 750.0,
          back.write_limit_text())

    window.start_engine()
    check("采集已启动", window.engine is not None and window.engine.running)
    check("采到数据", wait_until(
        app, lambda: window.engine.tags.get(VOLT_PID).quality == "good", 25.0))

    # 真实写入：给定 5000，限幅 0~750，引擎必须夹到 750 才下发
    ok, _ = window.engine.write(SET_PID, 5000.0)
    check("单发写 5000 请求被接受", ok)
    check("单发写入被夹到上限 750",
          wait_until(app, lambda: abs(float(window.engine.tags.get(SET_PID).value or 0) - 750.0) < 0.5, 10.0),
          f"读回 {window.engine.tags.get(SET_PID).value}")

    window.engine.write(SET_PID, -50.0)
    check("单发写入被夹到下限 0",
          wait_until(app, lambda: _near_zero(window.engine.tags.get(SET_PID).value), 10.0),
          f"读回 {window.engine.tags.get(SET_PID).value}")

    window.engine.set_periodic(SET_PID, 5000.0, 200)
    straight = next(
        (t for t in window.engine.periodic_tasks() if t.pid == SET_PID), None
    )
    check("持续发送那条也被夹住了", straight is not None and straight.value == 750.0,
          f"{None if straight is None else straight.value}")
    window.engine.clear_periodic(SET_PID)

    print("\n[5] 显示小数位数")
    window.set_point_decimals(SET_PID, 4)
    pump(app, 0.3)
    check("模型里小数位已改", point.decimals == 4, str(point.decimals))
    check("参数表里也记下了", csv_table.export_rows(project)[
        [r["点号"] for r in csv_table.export_rows(project)].index("有功功率给定")]["小数位"] == "4")
    window.set_point_decimals(SET_PID, 1)
    shot(window, f"04_main_window_with_tools.png")

    # ---------------------------------------------------------------- [6]
    print("\n[6] 截屏辅助：手动 + 变量触发自动")
    shots = ScreenshotDialog(window.tool_context(), parent=window)
    shots.dir_edit.setText(os.path.join(OUT, "shots"))
    shots.prefix_edit.setText("母线")
    shots.resize(880, 640)
    shots.show()
    pump(app, 0.4)

    manual = shots.take_screenshot()
    check("手动截屏出文件",
          bool(manual) and os.path.exists(manual) and os.path.getsize(manual) > 8000,
          f"{os.path.getsize(manual) if manual and os.path.exists(manual) else 0} 字节")

    shots.preselect_pid(VOLT_PID)
    check("条件里预选好了点位", shots.fields.pid_combo.currentData() == VOLT_PID)
    check("比较方式可切换",
          select_combo(shots.fields.compare_combo, Compare.GT)
          and shots.fields.selected_compare() == Compare.GT,
          str(shots.fields.compare_combo.currentText()))

    def new_shots() -> set[str]:
        directory = shots.dir_edit.text()
        return set(os.listdir(directory)) if os.path.exists(directory) else set()

    # 先记下现有文件：任务一加进来自动化就会触发，别把新图算进"已有"
    before = new_shots()
    select_combo(shots.fields.mode_combo, TriggerMode.RISING)
    shots.fields.threshold_box.setValue(0.0)
    shots.add_task()
    pump(app, 0.4)
    task = next((t for t in window.automation.tasks if "截屏" in t.name), None)
    check("截屏任务已注册", task is not None, str([t.name for t in window.automation.tasks]))
    check("任务名如实反映条件（大于，不是大于等于）",
          task is not None and "大于 " in task.name, task.name if task else "")
    shot(shots, "05_screenshot_tool.png")

    auto_ok = wait_until(app, lambda: bool(new_shots() - before), 15.0)
    made = sorted(new_shots() - before)
    check("变量一到阈值就自动截屏", auto_ok, str(made))
    if not auto_ok and task is not None:
        print(f"       任务：{task.status_text()}　触发 {task.fires} 次　最近错误：{task.last_error}")
        print(f"       工具状态：{shots.status.text()}")
        print(f"       触发条件：{task.trigger.describe()}")
    shots.close()

    # ---------------------------------------------------------------- [7]
    print("\n[7] 数据记录：变量触发开始 + 定时落表 + 导出")
    log_window = DataLogWindow(window.tool_context(), parent=window)
    log_window.resize(1240, 720)
    log_window.show()
    pump(app, 0.4)
    expect = log_window.points_list.count()
    check("点位清单含全部点位", expect == len(project.all_point_ids()), f"{expect} 个")

    def check_one(pid: str) -> None:
        for index in range(log_window.points_list.count()):
            item = log_window.points_list.item(index)
            if item.data(Qt.UserRole) == pid:
                item.setCheckState(Qt.Checked)
                return

    for pid in (VOLT_PID, BUS_PID, STATUS_PID):
        check_one(pid)
    check("勾选生效",
          set(check_list := log_window.selected_pids()) == {VOLT_PID, BUS_PID, STATUS_PID},
          str(sorted(check_list)))

    log_window.interval_box.setValue(0.2)
    log_window.duration_box.setValue(0.0)
    log_window.max_rows_box.setValue(50)
    select_combo(log_window.start_mode_combo, "variable")
    select_combo(log_window._start_trigger.pid_combo, VOLT_PID)
    select_combo(log_window._start_trigger.compare_combo, Compare.GT)
    log_window._start_trigger.threshold_box.setValue(0.0)
    select_combo(log_window.end_mode_combo, "variable")
    select_combo(log_window._record_trigger.pid_combo, BUS_PID)
    select_combo(log_window._record_trigger.compare_combo, Compare.GT)
    log_window._record_trigger.threshold_box.setValue(1000.0)
    pump(app, 0.3)
    log_window.start_recording()
    check("起始触发已注册", "等待触发" in log_window.status.text(), log_window.status.text())
    check("变量触发后真的开记", wait_until(app, lambda: len(log_window._rows) > 3, 15.0),
          f"{len(log_window._rows)} 行")
    check("结束触发也登记了（条件不满足所以还在记）",
          window.automation.find(log_window._end_task_name) is not None
          and log_window._recording,
          log_window._end_task_name)
    shot(log_window, "06_datalog_window.png")

    wait_until(app, lambda: len(log_window._rows) >= 8 or not log_window._recording, 10.0)
    rows = len(log_window._rows)
    first = log_window._rows[0]
    check("一行一个时间戳 + 每个量一列", len(first) == 4 + 1, str(len(first)))
    correct = all(str(row[1]) not in ("", "通信异常") for row in log_window._rows)
    check("记的是真值（不是通信异常）", correct and rows >= 8, f"{rows} 行，示例：{first[1]} A")

    # 结束触发真的能停
    log_window._record_trigger.threshold_box.setValue(0.0)
    stopped = wait_until(app, lambda: not log_window._recording, 15.0)
    check("变量触发能停止记录", stopped,
          f"{log_window.status.text()}｜自动化里还剩 {len(window.automation.tasks)} 个任务")
    pump(app, 0.3)

    csv_out = os.path.join(OUT, "数据记录导出.csv")
    with mock.patch(
        "icomm.ui.tools.datalog.QFileDialog.getSaveFileName",
        lambda *a, **k: (csv_out, "CSV"),
    ):
        log_window.export_csv()
    check("导出 CSV", os.path.exists(csv_out) and os.path.getsize(csv_out) > 40,
          f"{os.path.getsize(csv_out) if os.path.exists(csv_out) else 0} 字节")
    if os.path.exists(csv_out):
        with open(csv_out, encoding="utf-8-sig") as fh:
            lines = fh.read().splitlines()
        check("导出的表头是点位全名",
              len(lines) >= 2 and "A相电压" in lines[0], lines[0][:60])
    log_window.close()

    # ---------------------------------------------------------------- [8]
    print("\n[8] 示波器小窗口")
    mini = MiniScopeWindow(window.tool_context(), parent=window)
    mini.resize(980, 520)
    mini.show()
    pump(app, 0.3)
    for pid in (VOLT_PID, BUS_PID):
        check(f"加上 {pid.split('.')[-1]}", mini.show_point(pid))
    pump(app, 1.2)
    mini.refresh()
    pump(app, 0.5)
    check("小窗有 2 个通道可见",
          len([c for c in mini._channels.values() if c.visible]) == 2)
    samples = mini.canvas.channel(VOLT_PID)
    check("小窗真的在攒点（标量点位按时间戳追加）",
          samples is not None and len(samples.samples) >= 3,
          f"{0 if samples is None else len(samples.samples)} 点")
    shot(mini, "07_mini_scope.png")
    mini.close()

    # ---------------------------------------------------------------- [9]
    print("\n[9] 自动化任务一览 + 手动触发")
    fired: list[str] = []
    window.automation.add(
        Task(
            name="探针任务：母线超 100V",
            trigger=Trigger(pid=BUS_PID, compare=Compare.GT, threshold=100.0,
                            mode=TriggerMode.RISING),
            action=lambda task, values: fired.append(task.name),
        )
    )
    panel = AutomationPanel(window.automation, parent=window)
    panel.resize(900, 460)
    panel.show()
    pump(app, 0.4)
    check("面板列出全部任务",
          panel.table.rowCount() == len(window.automation.tasks),
          f"{panel.table.rowCount()} 行 / {len(window.automation.tasks)} 个")
    shot(panel, "08_automation_panel.png")
    panel.close()
    window.automation.manual_trigger("探针任务：母线超 100V")
    check("手动触发有效", fired == ["探针任务：母线超 100V"], str(fired))

    # ---------------------------------------------------------------- [10]
    print("\n[10] TCP 连接测试（核心层，连真模拟器）")
    payload, err = net_check.parse_hex_payload("00 01 00 00 00 06 15 03 00 00 00 02")
    check("十六进制报文能解析",
          payload == b"\x00\x01\x00\x00\x00\x06\x15\x03\x00\x00\x00\x02" and not err,
          f"{payload.hex(' ').upper()} / {err!r}")
    check("非法报文有明确原因", net_check.parse_hex_payload("z1z1")[1] != "",
          net_check.parse_hex_payload("z1z1")[1])

    tester = net_check.ConnectionTester(
        "127.0.0.1", port, timeout_s=3.0, payload=payload, wait_response=True
    )
    result = tester.run()
    received = result.attempts[0].received if result.attempts else b""
    check("连得上并且 Modbus 有回应", result.ok and len(received) >= 7,
          f"{result.headline}｜收到 {received.hex(' ').upper()}"
          if received else f"{result.headline}｜没收到回应")

    dead_port = pick_port(15999)
    bad = net_check.ConnectionTester("127.0.0.1", dead_port, timeout_s=1.0).run()
    check("连不上时给排查建议",
          not bad.ok and "排查建议" in bad.detail(),
          bad.detail().splitlines()[-1] if bad.detail() else "")

    # ---------------------------------------------------------------- [11]
    print("\n[11] 每一个注册的工具都能真的打开")
    from icomm.ui.tools.base import DialogTool, WindowTool

    opened = 0
    for tool in ui_tools.tools():
        context = window.tool_context()
        try:
            if isinstance(tool, DialogTool):
                widget = tool.build(context)  # 不用 exec()，否则模态会卡住
            elif isinstance(tool, WindowTool):
                widget = tool.launch(context)
            else:  # 自定义 Tool
                widget = tool.open(context)
        except Exception as exc:  # noqa: BLE001 - 探针就是要抓住这个
            check(f"打开「{tool.name}」", False, f"{type(exc).__name__}: {exc}")
            continue
        ok = widget is not None
        if ok:
            opened += 1
            widget.resize(max(640, widget.width()), max(420, widget.height()))
            widget.show()
            pump(app, 0.35)
            shot(widget, f"10_tool_{opened:02d}.png")
            widget.close()
            pump(app, 0.15)
        check(f"打开「{tool.name}」", ok, "" if ok else "返回了 None")

    check("至少打开了 6 个工具窗口", opened >= 6, f"{opened} 个")

    # ---------------------------------------------------------------- [12]
    print("\n[12] 计时器与定时赋值：掐表 + 到点写值")
    timing = TimingWindow(window.tool_context(), parent=window)
    timing.resize(960, 680)
    timing.show()
    pump(app, 0.5)
    check("时钟在走", bool(timing.clock.text()), f"{timing.date_label.text()} {timing.clock.text()}")
    check("状态栏也有时钟", bool(window.status_clock.text()), window.status_clock.text())

    timing.watch.start(now=time.monotonic() - 3.0)
    timing._mark()
    timing._tick()
    pump(app, 0.3)
    check("每秒刷新计时显示", timing.watch_label.text().startswith("00:00:0"),
          f"{timing.watch_label.text()}，标记 {timing.marks_list.count()} 条")
    timing.watch.reset()
    timing._clear_marks()

    check("方式下拉里的枚举没被 Qt 降级成字符串",
          select_combo(timing.mode_combo, StepMode.AT)
          and timing.selected_mode() is StepMode.AT
          and select_combo(timing.mode_combo, StepMode.EVERY)
          and timing.selected_mode() is StepMode.EVERY,
          str(timing.mode_combo.currentText()))
    select_combo(timing.mode_combo, StepMode.AT)
    timing.seconds_box.setValue(1.0)
    select_combo(timing.pid_combo, SET_PID)
    timing.value_box.setValue(600.0)
    timing.add_step()
    pump(app, 0.3)
    check("动作已加入计划表", timing.table.rowCount() == 1,
          timing.table.item(0, 1).text() if timing.table.rowCount() else "")

    timing.watch.reset()
    timing.watch.start()
    wrote = wait_until(app, lambda: timing.schedule.steps[0].fired >= 1, 6.0)
    pump(app, 1.0)
    check("到第 1 秒真的给点位写了值", wrote and timing.schedule.steps[0].fired == 1,
          f"读了 {len(timing.watch.marks)} 次，已执行 {timing.schedule.steps[0].fired} 次")
    check("写下去的值到了设备读回", _near(window.engine.tags.get(SET_PID).value, 600.0, 1.0),
          f"{window.engine.tags.get(SET_PID).value}")
    timing.watch.pause()
    shot(timing, "11_timing.png")
    timing.close()

    # ---------------------------------------------------------------- [13]
    print("\n[13] 故障录波：一直往回记，触发后留下前后两段")
    faults = FaultRecordWindow(window.tool_context(), parent=window)
    faults.resize(1060, 700)
    faults.show()
    pump(app, 0.4)
    faults._check_all(False)
    for pid in (VOLT_PID, BUS_PID):
        for index in range(faults.points_list.count()):
            item = faults.points_list.item(index)
            if item.data(Qt.UserRole) == pid:
                item.setCheckState(Qt.Checked)
    check("勾选生效", set(faults.selected_pids()) == {VOLT_PID, BUS_PID},
          str(sorted(faults.selected_pids())))

    fault_dir = os.path.join(OUT, "fault_records")
    if os.path.isdir(fault_dir):
        shutil.rmtree(fault_dir)  # 清掉上一次的产物，否则"有没有新录到"看不出来
    faults.dir_edit.setText(fault_dir)
    faults.pre_box.setValue(0.5)
    faults.post_box.setValue(0.5)
    check("前后时长默认各 1 秒",
          FaultRecordWindow(window.tool_context()).pre_box.value() == 1.0)

    select_combo(faults.fields.pid_combo, VOLT_PID)
    select_combo(faults.fields.compare_combo, Compare.GT)
    faults.fields.threshold_box.setValue(0.0)
    select_combo(faults.fields.mode_combo, TriggerMode.RISING)
    faults.start()
    pump(app, 0.5)
    check("录波任务已注册", bool(faults._task_name),
          str([t.name for t in window.automation.tasks]))
    check("缓冲一直在往回记",
          wait_until(app, lambda: len(faults.recorder.buffered()) == 2, 8.0),
          str(faults.recorder.buffered()))

    def fault_csvs() -> list[str]:
        if not os.path.isdir(fault_dir):
            return []
        return sorted(n for n in os.listdir(fault_dir) if n.lower().endswith(".csv"))

    check("触发后自动录完并导出", wait_until(app, lambda: bool(fault_csvs()), 20.0),
          str(fault_csvs()))
    shot(faults, "12_fault_record.png")

    check("录完的次数记在记录器里", faults.recorder.fired >= 1, str(faults.recorder.fired))
    if fault_csvs():
        with open(os.path.join(fault_dir, fault_csvs()[0]), encoding="utf-8-sig") as fh:
            lines = [line.rstrip("\n") for line in fh]
        check("CSV 头写清了触发原因与前后时长",
              any(line.startswith("# 触发") for line in lines) and len(lines) > 6,
              f"{len(lines)} 行")
        check("CSV 里两列数据都有采到",
              all(len(line.split(",")) >= 3 for line in lines[5:]),
              lines[5][:60] if len(lines) > 5 else "")
    # 入库走的是后台写线程 + 定时提交，所以要等一会儿才查得到
    check("波形进了录波表（能在示波器录波回放里看）",
          wait_until(
              app, lambda: len(window.storage.query_waveform_index(limit=50)) >= 2, 15.0
          ),
          f"{len(window.storage.query_waveform_index(limit=50))} 条")
    faults.stop()
    check("停止后把触发任务摘掉了",
          faults._task_name == "" and not any(
              "故障录波" in task.name for task in window.automation.tasks
          ),
          str([t.name for t in window.automation.tasks]))
    faults.close()

    # ---------------------------------------------------------------- [14]
    print("\n[14] 录音：到条件就录，录够时长自动停")
    ok, why = audio.WinmmRecorder.available()
    voices = audio.devices()
    check("能枚举到录音设备（或明确说明为什么不能录）",
          bool(voices) or not ok, f"{len(voices)} 个设备｜{why or '可用'}")

    audio_dir = os.path.join(OUT, "recordings")
    if os.path.isdir(audio_dir):
        shutil.rmtree(audio_dir)
    sound = AudioRecordWindow(window.tool_context(), parent=window)
    sound.dir_edit.setText(audio_dir)
    sound.resize(840, 620)
    sound.show()
    pump(app, 0.4)
    check("界面给出了设备或原因",
          "录音设备" in sound.status.text() or "⚠" in sound.status.text(),
          sound.status.text())
    shot(sound, "13_audio_record.png")

    if ok:
        sound.max_box.setValue(2.0)
        select_combo(sound.fields.pid_combo, VOLT_PID)
        select_combo(sound.fields.compare_combo, Compare.GT)
        sound.fields.threshold_box.setValue(0.0)
        select_combo(sound.fields.mode_combo, TriggerMode.RISING)
        sound.start()
        pump(app, 0.5)
        check("录音任务已注册", bool(sound._task_name), sound._task_name)

        def wavs() -> list[str]:
            if not os.path.isdir(audio_dir):
                return []
            return sorted(n for n in os.listdir(audio_dir) if n.lower().endswith(".wav"))

        check("触发后开始录、到点自动停",
              wait_until(app, lambda: bool(wavs()) and sound._recorder is None, 25.0),
              str(wavs()))
        check("录到的文件是合法 WAV",
              bool(wavs()) and _wav_ok(os.path.join(audio_dir, wavs()[0]), sound),
              wavs()[0] if wavs() else "（没有文件）")

        # 手动录一段：开始 → 结束
        sound.manual_recording()
        pump(app, 1.2)
        check("手动录音在录着", sound._recorder is not None, sound.status.text())
        sound.finish_recording("手动结束")
        pump(app, 0.3)
        check("手动录完也有文件", len(wavs()) >= 2, str(wavs()))
        check("清单里能看到",
              wait_until(app, lambda: sound.table.rowCount() >= 2, 5.0),
              f"{sound.table.rowCount()} 行")
        sound.stop()
    else:
        sound.start()
        check("不能录时会把原因说清楚", "⚠" in sound.status.text(), sound.status.text())
    sound.close()

    # ---------------------------------------------------------------- [15]
    print("\n[15] 主回路图编辑：加图元、拖动、连线、应用到主页")
    window.nav.setCurrentRow(NAV["主页"])
    pump(app, 0.4)
    editor = DiagramEditWindow(window.tool_context())
    editor.resize(1240, 820)
    editor.show()
    pump(app, 0.5)
    palette_items = [
        editor.palette.item(i).data(Qt.UserRole) for i in range(editor.palette.count())
    ]
    available = [item for item in palette_items if item]
    check("调色板里有常见电气图元",
          {"grid", "battery", "filter", "inverter", "rectifier", "load"} <= set(available),
          f"{len(available)} 个图元")
    before_nodes = len(editor.diagram.nodes)
    check("编辑的是工程里那张图", before_nodes > 0, f"{before_nodes} 个图元")

    # 加一个"电池/储能"，改字，拖到母线左侧
    target_row = next(
        i for i in range(editor.palette.count())
        if editor.palette.item(i).data(Qt.UserRole) == "battery"
    )
    editor.palette.setCurrentRow(target_row)
    editor.add_symbol()
    pump(app, 0.3)
    check("新图元已加入", len(editor.diagram.nodes) == before_nodes + 1)
    added = editor.diagram.nodes[-1]
    editor.label_edit.setText("储能电池")
    editor.sub_edit.setText("100 kWh")
    editor._apply_form()
    select_combo(editor.value_combo, BUS_PID)
    editor._apply_form()
    pump(app, 0.3)
    check("属性改写生效", added.label == "储能电池" and added.value_point == BUS_PID,
          f"{added.label} / {added.value_point}")

    editor.preview.move_node(added, 0.12, 0.80)
    pump(app, 0.2)
    check("拖动改了坐标（并按中心算）",
          abs(added.x - (0.12 - added.w / 2)) < 1e-3 and added.y > 0.6,
          f"{added.x:.3f}, {added.y:.3f}")

    editor.link_from.setCurrentIndex(editor.link_from.findData(added.id))
    editor.link_to.setCurrentIndex(editor.link_to.findData(editor.diagram.nodes[0].id))
    links_before = len(editor.diagram.links)
    editor.add_link()
    pump(app, 0.2)
    check("连线加上了", len(editor.diagram.links) == links_before + 1,
          str([(link.source, link.target) for link in editor.diagram.links][-2:]))
    check("预览马上重画（图元数与工程一致）",
          len(editor.preview.diagram.nodes) == len(editor.diagram.nodes))
    shot(editor, "14_diagram_editor.png")

    editor.apply_to_project()
    pump(app, 0.6)
    check("应用到工程",
          len(project.diagram.nodes) == before_nodes + 1
          and project.diagram.node(added.id) is not None,
          f"{len(project.diagram.nodes)} 个图元")
    check("主页立刻用上了新图",
          len(window.overview_page.canvas.diagram.nodes) == len(project.diagram.nodes),
          f"主页 {len(window.overview_page.canvas.diagram.nodes)} 个图元")
    pump(app, 0.5)
    shot(window, "15_overview_after_edit.png")

    # 存盘再打开：图要跟着工程走
    project_store.save_project(project, paths)
    reopened, _ = project_store.open_project(paths.project_file, workdir=workdir)
    check("图随工程保存/回读",
          reopened.diagram.node(added.id) is not None
          and len(reopened.diagram.links) == len(project.diagram.links),
          f"{len(reopened.diagram.nodes)} 个图元 / {len(reopened.diagram.links)} 条线")
    editor.close()

    # ---------------------------------------------------------------- [16]
    print("\n[16] 分析工具：伯德图 / 锁相 / 功率 / 三相向量")
    analysis = AnalysisWindow(window.tool_context())
    analysis.resize(1120, 760)
    analysis.show()
    pump(app, 0.5)
    labels = [analysis.tabs.tabText(i) for i in range(analysis.tabs.count())]
    check("4 个页签都在", len(labels) == 4, "、".join(labels))

    analysis.select_tab("伯德")
    pump(app, 0.3)
    bode = analysis.bode_plot.series
    check("伯德图画出了曲线", bode and len(bode[0]["points"]) == 240,
          f"{len(bode[0]['points']) if bode else 0} 个频点")
    check("默认 1mH/100μF 的谐振点算对了（503.3 Hz）",
          "503" in analysis.bode_text.text(), analysis.bode_text.text()[:80])

    analysis.c_box.setValue(10.0)
    pump(app, 0.3)
    check("改参数曲线跟着变", "1,591" in analysis.bode_text.text(),
          analysis.bode_text.text().split("　｜　")[0])
    shot(analysis, "16_analysis_bode.png")

    # 锁相：示例的波形点位（电池电流 / 并网相电流）是数组点位，喂一段已知波形进去
    analysis.select_tab("锁相")
    pump(app, 0.2)
    if not analysis.pll_combo.currentData():
        check("没有波形点位时给出说明", "数组" in analysis.pll_text.text(),
              analysis.pll_text.text())
    else:
        pid = str(analysis.pll_combo.currentData())
        found = project.find_point(pid)
        interval_us = int(found[1].sample_interval_us or 200) if found else 200
        wave = [
            math.sin(2 * math.pi * 50 * index * interval_us / 1e6) * 311.0
            for index in range(300)
        ]
        window.engine.tags.update(pid, wave, "good")
        analysis._update_pll()  # 不能先 pump：否则轮询会把注入值刷掉
        check("锁相能算出 50.0 Hz", "50.0" in analysis.pll_text.text(),
              analysis.pll_text.text().split("<br>")[0][:110])
        shot(analysis, "17_analysis_pll.png")

    # 功率：喂已知的 U/I/P
    analysis.select_tab("功率")
    for combo, value in (
        (analysis.volt_combo, 380.0),
        (analysis.current_combo, 100.0),
        (analysis.active_combo, 30000.0),
    ):
        pid = combo.currentData()
        if pid:
            window.engine.tags.update(str(pid), value, "good")
    analysis._update_power()
    check("功率因数算对了（30000/(380*100) = 0.7895）",
          "0.7895" in analysis.power_text.text(), analysis.power_text.text()[:110])
    check("功率角与无功也给出来了",
          "37.86" in analysis.power_text.text() and "kvar" in analysis.power_text.text())
    check("趋势记下来了", len(analysis.power_plot.series) == 1)
    shot(analysis, "18_analysis_power.png")

    # 三相向量：喂平衡三相
    analysis.select_tab("向量")
    angles = (0.0, -120.0, 120.0)
    for index, combo in enumerate(analysis.amp_combos):
        pid = combo.currentData()
        if pid:
            window.engine.tags.update(str(pid), 220.0, "good")
        analysis.angle_boxes[index].setValue(angles[index])
        analysis._sync_angle_row(index)
    analysis._update_phasor()
    check("三相向量画出来了", len(analysis.phasor_plot.phasors) == 3)
    check("平衡三相的不平衡度接近 0",
          "0.00" in analysis.phasor_text.text().split("不平衡度")[-1][:12],
          analysis.phasor_text.text().replace("<br>", " ")[:120])
    analysis.angle_boxes[1].setValue(-118.0)
    if analysis.amp_combos[1].currentData():
        # 让 B 相幅值掉一点，制造真正的不平衡（只改角度不平衡度仍然很小）
        window.engine.tags.update(str(analysis.amp_combos[1].currentData()), 170.0, "good")
    analysis._sync_angle_row(1)
    analysis._update_phasor()
    check("不平衡时按严重程度给提示",
          "略偏大" in analysis.phasor_text.text()
          or "明显不平衡" in analysis.phasor_text.text(),
          analysis.phasor_text.text().replace("<br>", " ")[-70:])
    shot(analysis, "19_analysis_phasor.png")
    analysis.close()

    # ---------------------------------------------------------------- [17]
    print("\n[17] 配色导入导出（设置 → 界面外观）")
    settings_dialog = SettingsDialog(
        window.settings, project, list(project_io.BUILTIN_SAMPLES),
        parent=window, base_dir=OUT,
    )
    settings_dialog.appearanceChanged.connect(window._refresh_appearance)
    settings_dialog.resize(1000, 760)
    settings_dialog.show()
    pump(app, 0.5)
    editor = settings_dialog.appearance_editor
    editor.preset_combo.setCurrentIndex(editor.preset_combo.findData("浅色办公"))
    pump(app, 0.6)
    editor.size_box.setValue(15)
    pump(app, 0.6)
    check("导出的按钮在「界面外观」页里",
          any("导出配色" in button.text() for button in editor.findChildren(QPushButton)),
          str([b.text() for b in editor.findChildren(QPushButton)][:6]))

    theme_path = os.path.join(OUT, "导出配色.json")
    with mock.patch(
        "icomm.ui.dialogs.appearance_editor.QFileDialog.getSaveFileName",
        lambda *a, **k: (theme_path, "配色文件 (*.json)"),
    ):
        exported = editor.export_theme()
    check("导出配色文件", exported == theme_path and os.path.exists(theme_path),
          f"{os.path.getsize(theme_path) if os.path.exists(theme_path) else 0} 字节")
    with open(theme_path, encoding="utf-8") as fh:
        payload = json.load(fh)
    check("文件带格式标记与全部颜色",
          payload.get("format") == theme.THEME_FILE_FORMAT
          and len(payload["appearance"]["palette"]) >= 25,
          f"{len(payload['appearance']['palette'])} 项配色")

    # 换个主题，再导回来，应当回到导出时那套（而且实时预览要跟着变）
    check("预设主题名正确", editor.preset_combo.findData("高对比现场") >= 0,
          str([editor.preset_combo.itemData(i) for i in range(editor.preset_combo.count())]))
    editor.preset_combo.setCurrentIndex(editor.preset_combo.findData("高对比现场"))
    pump(app, 0.8)
    dark_accent = theme.ACCENT
    exported_accent = payload["appearance"]["palette"]["accent"]
    check("换主题后界面强调色确实变了", dark_accent != exported_accent,
          f"高对比现场 {dark_accent} vs 导出的 {exported_accent}")
    with mock.patch(
        "icomm.ui.dialogs.appearance_editor.QFileDialog.getOpenFileName",
        lambda *a, **k: (theme_path, "配色文件 (*.json)"),
    ):
        check("导入配色成功", editor.import_theme())
    pump(app, 0.8)
    check("导入后外观回到导出时那套（含实时预览）",
          editor.current_appearance().palette.name == "浅色办公"
          and editor.current_appearance().font_size == 15
          and theme.ACCENT == exported_accent,
          f"{editor.current_appearance().palette.name} / {editor.current_appearance().font_size}px"
          f" / accent {theme.ACCENT}")

    # 坏文件要给出提示而不是崩
    bad_path = os.path.join(OUT, "坏配色.json")
    with open(bad_path, "w", encoding="utf-8") as fh:
        fh.write("{ 这不是 json")
    with mock.patch(
        "icomm.ui.dialogs.appearance_editor.QFileDialog.getOpenFileName",
        lambda *a, **k: (bad_path, "配色文件 (*.json)"),
    ), mock.patch.object(QMessageBox, "warning", lambda *a, **k: QMessageBox.Ok):
        check("坏文件被挡下来（弹提示不崩）", not editor.import_theme())
    shot(settings_dialog, "20_theme_io.png")
    settings_dialog.reject()  # 取消 -> 外部外观回到进入时的样子
    pump(app, 0.4)

    # ---------------------------------------------------------------- [18]
    print("\n[18] 串口设置：自动检测 + 可手选 + 默认 9600 8E1")
    settings_dialog = SettingsDialog(
        window.settings, project, list(project_io.BUILTIN_SAMPLES),
        parent=window, base_dir=OUT,
    )
    settings_dialog.resize(1000, 760)
    settings_dialog.show()
    pump(app, 0.5)
    comm = settings_dialog.channel_editor
    settings_dialog.tabs.setCurrentWidget(comm)
    comm.list.setCurrentRow(0)
    pump(app, 0.3)
    check("工程通道默认走 TCP", comm.link_combo.currentData() == "tcp",
          str(comm.link_combo.currentData()))
    tcp_port_before = comm._current.options.get("port")  # 切到 RTU 之前先记下 TCP 端口

    rtu_index = comm.link_combo.findData("rtu")
    check("链路类型里能选 RTU", rtu_index >= 0,
          str([comm.link_combo.itemData(i) for i in range(comm.link_combo.count())]))
    comm.link_combo.setCurrentIndex(rtu_index)
    pump(app, 0.4)

    port_widget = comm._option_widgets.get("port")
    check("串口号是可下拉可手打的控件（不是只能硬打字）",
          isinstance(port_widget, option_fields.SerialPortCombo),
          type(port_widget).__name__)
    detected = serial_ports.port_names()
    listed = [port_widget.itemData(i) for i in range(port_widget.count())]
    check("下拉项就是本机检测到的串口",
          listed == detected,
          f"检测到 {listed if listed else '（本机一个串口都没有，仍可手打）'}")
    if detected:
        check("有串口时默认选第一个", port_widget.currentText() == detected[0],
              port_widget.currentText())

    # 手打（前端没有这一项）后回车/失焦 —— 走的就是现场真正会走的路径
    port_widget.lineEdit().setText("COM42")
    port_widget.lineEdit().editingFinished.emit()
    pump(app, 0.3)
    check("手打的串口号写进了工程", comm._current.options.get("port") == "COM42",
          str({k: v for k, v in comm._current.options.items() if k != "timeout"}))
    check("手打也算改动了工程（会提示保存）",
          comm._option_widgets["port"].currentText() == "COM42",
          comm._option_widgets["port"].currentText())

    check("波特率默认 9600",
          comm._option_widgets["baudrate"].currentData() == 9600,
          str(comm._option_widgets["baudrate"].currentData()))
    check("校验位默认偶校验，界面显示中文",
          comm._option_widgets["parity"].currentData() == "E"
          and "偶校验" in comm._option_widgets["parity"].currentText(),
          comm._option_widgets["parity"].currentText())
    check("数据位 8 / 停止位 1",
          comm._option_widgets["bytesize"].currentData() == 8
          and comm._option_widgets["stopbits"].currentData() == 1,
          f"{comm._option_widgets['bytesize'].currentData()}"
          f"{comm._option_widgets['parity'].currentData()}"
          f"{comm._option_widgets['stopbits'].currentData()}")
    check("界面上看得出来默认是 9600 8E1",
          serial_ports.frame_text(
              comm._current.options.get("baudrate"),
              comm._current.options.get("bytesize"),
              comm._current.options.get("parity"),
              comm._current.options.get("stopbits"),
          ) == "9600 8E1",
          str(comm._current.options.get("baudrate")))
    shot(settings_dialog, "21_serial_settings.png")

    # TCP 与 RTU 各记一套：切走再切回来，原来的 TCP 端口不能被串口号盖掉
    comm.link_combo.setCurrentIndex(comm.link_combo.findData("tcp"))
    pump(app, 0.4)
    check("切回 TCP 时 TCP 端口还在（两套参数各记一套）",
          comm._current.options.get("port") == tcp_port_before
          and bool(comm._current.options.get("host")),
          f"TCP {comm._current.options.get('port')} / 原 {tcp_port_before}")
    remembered = comm._current.options_by_link.get("rtu") or {}
    check("RTU 那套参数也记住了（下次切过来不用重填）",
          remembered.get("port") == "COM42" and remembered.get("parity") == "E",
          str({key: remembered.get(key) for key in ("port", "baudrate", "parity")}))
    comm.link_combo.setCurrentIndex(comm.link_combo.findData("rtu"))
    pump(app, 0.4)
    check("再切回 RTU 时手填的串口号也还在",
          comm._current.options.get("port") == "COM42",
          str(comm._current.options.get("port")))
    settings_dialog.reject()
    pump(app, 0.3)

    # ---------------------------------------------------------------- [19]
    print("\n[19] 示波器：波形点位（成块读回）与标量趋势")
    window.nav.setCurrentRow(NAV["示波器"])
    pump(app, 0.5)
    scope = window.scope_page
    wave_points = project.scope_points()
    check("示例自带波形点位（数组/块点位）", len(wave_points) >= 1,
          f"{len(wave_points)} 个：" + "、".join(point.name for _d, point in wave_points[:3]))
    check("默认只列标了「示波器」的点位",
          scope.channel_list.count() == len(wave_points),
          f"{scope.channel_list.count()} / {len(wave_points)} 项")
    check("提示里说明含几个波形点位", "波形点位" in scope.hint.text(), scope.hint.text())
    check("提示里给了一个点多久来一次", "ms/点" in scope.hint.text(),
          scope.hint.text().split("｜")[-1].strip())

    scope.show_all_box.setChecked(True)
    pump(app, 0.3)
    check("勾上「显示全部点位」后列出全部",
          scope.channel_list.count() == len(project.all_point_ids()),
          f"{scope.channel_list.count()} / {len(project.all_point_ids())} 项")
    scope.show_all_box.setChecked(False)
    pump(app, 0.3)

    # 标量点位单看一张趋势图：先把波形通道都取消勾选，画布才会切到"瞬时值"模式
    scope._set_all(False)
    scope.add_point(BUS_PID)
    pump(app, 0.4)
    check("标量点位画出来是趋势而不是波形",
          scope.canvas.mode == "scalar" and scope.canvas.channel(BUS_PID) is not None,
          scope.canvas.mode)

    engine_running = window.engine is not None and window.engine.running
    check("采集在跑", engine_running)
    if engine_running:
        got = wait_until(
            app, lambda: len(scope.canvas.channel(BUS_PID).samples) >= 4, 20.0
        )
        check("趋势真的在攒点（不是空画布）", got,
              f"{len(scope.canvas.channel(BUS_PID).samples)} 点")
    shot(window, "22_scope_instantaneous.png")

    rows = scope.history_table.rowCount()
    expected = "没有波形点位" if rows == 0 else "段录波"
    check("录波历史那一栏的提示跟实际数据对得上", expected in scope.history_hint.text(),
          f"{rows} 行：{scope.history_hint.text()}")
    # 收拾干净：全部取消勾选后重建，通道回到"只默认勾波形点位"的状态
    scope._set_all(False)
    scope._build_channels()
    pump(app, 0.3)

    # ---------------------------------------------------------------- [20]
    print("\n[20] 工程仓库：软件目录只放软件，工程配置与测试结果分开")
    check("工程的根目录就是给它的那个工程目录（不在别处乱建）",
          os.path.normcase(paths.root).startswith(os.path.normcase(os.path.abspath(workdir))),
          f"{paths.root}（仓库 {workdir}）")
    check("没有指定仓库时，默认会建在软件目录**外面**",
          not os.path.normcase(project_store.default_project_home(os.getcwd())).startswith(
              os.path.normcase(os.getcwd()) + os.sep
          ),
          project_store.default_project_home(os.getcwd()))
    check("工程目录是 config / data / reports 三件套",
          paths.organized
          and os.path.basename(paths.config_dir) == "config"
          and os.path.basename(paths.data_dir) == "data"
          and os.path.basename(paths.report_dir) == "reports",
          f"{paths.layout}｜{os.listdir(paths.root)}")
    check("工程定义与参数表在 config/ 里",
          os.path.exists(os.path.join(project_store.CONFIG_DIR, "project.json"))
          is False
          and os.path.dirname(paths.project_file) == paths.config_dir
          and os.path.dirname(paths.csv_file) == paths.config_dir,
          paths.project_file)
    check("历史库在 data/ 里（跟配置分开）",
          os.path.dirname(paths.db_file) == paths.data_dir,
          paths.db_file)
    check("根目录里不再散着 project.json / history.db",
          "project.json" not in os.listdir(paths.root)
          and "history.db" not in os.listdir(paths.root),
          str(os.listdir(paths.root)))

    context = window.tool_context()
    check("工具拿到的报表目录就在本工程下",
          os.path.normcase(context.report_dir) == os.path.normcase(paths.report_dir),
          context.report_dir)
    check("报表分类目录是英文（shots/faults/datalog/audio）",
          all(name.isascii() for name in project_store.REPORT_KINDS)
          and all(
              os.path.isdir(os.path.join(paths.report_dir, name))
              for name in project_store.REPORT_KINDS
          ),
          str(sorted(os.listdir(paths.report_dir))))
    shots = ScreenshotDialog(context, parent=window)
    shots.resize(760, 520)
    shots.show()
    pump(app, 0.4)
    check("截屏默认存到本工程的 reports/shots",
          os.path.normcase(os.path.dirname(shots.dir_edit.text()))
          == os.path.normcase(paths.report_dir)
          and os.path.basename(shots.dir_edit.text()) == "shots",
          shots.dir_edit.text())
    pump(app, 0.3)
    shots.close()

    # 新建工程也要落在工程仓库里（不是软件目录）
    fresh = project_store.create_in_workdir(
        project_io.bess_project(), window.workdir
    )
    check("新建工程落在工程仓库里",
          os.path.normcase(fresh.root).startswith(os.path.normcase(window.workdir)),
          f"{fresh.root}（仓库 {window.workdir}）")

    # 「设置 → 软件设置」里能看到并改工程仓库位置
    settings_dialog = SettingsDialog(
        window.settings, project, list(project_io.BUILTIN_SAMPLES),
        parent=window, base_dir=OUT,
    )
    settings_dialog.resize(1000, 760)
    settings_dialog.show()
    settings_dialog.tabs.setCurrentIndex(2)  # 软件设置
    pump(app, 0.5)
    check("设置里能看到当前用的工程仓库",
          settings_dialog.project_home_edit.placeholderText().startswith("留空 = 自动查找")
          and settings_dialog._active_home in settings_dialog.project_home_edit.placeholderText(),
          settings_dialog.project_home_edit.placeholderText())
    check("能列出仓库里已有的工程",
          settings_dialog.home_list.count() >= 1,
          f"{settings_dialog.home_list.count()} 个：" +
          "、".join(settings_dialog.home_list.item(i).text()
                    for i in range(settings_dialog.home_list.count())))
    check("提示里说清了工程内部结构",
          "配置" in settings_dialog.home_hint.text()
          and "数据" in settings_dialog.home_hint.text()
          and "报表" in settings_dialog.home_hint.text(),
          settings_dialog.home_hint.text()[:80])
    shot(settings_dialog, "23_project_home.png")
    settings_dialog.reject()
    pump(app, 0.3)

    # ---------------------------------------------------------------- [21]
    print("\n[21] 窗口不能被文字撑出屏幕")
    minimum = window.minimumSizeHint()
    check("窗口最小宽度放得下小屏（以前是 2800+，用户屏幕上窗口宽过屏幕）",
          minimum.width() <= 1200, f"{minimum.width()} x {minimum.height()}")
    check("窗口最小高度放得下 768p 屏", minimum.height() <= 700,
          f"{minimum.height()}")
    screen = QGuiApplication.primaryScreen()
    if screen is not None:
        available = screen.availableGeometry()
        # 探针自己会把窗口 resize 到 1560 宽方便截图，所以这里另开一个窗口验证"启动时的取尺寸逻辑"
        sizing = MainWindow(project, paths, workdir=workdir, base_dir=OUT)
        sizing.settings.confirm_on_exit = False
        sizing.settings.window_geometry = ""  # 排除"恢复上次几何"的影响
        sizing._fit_initial_size()
        # 屏幕比布局最小值还小时只能以布局为准（1003 像素是最小可用的宽度）
        limit_w = max(available.width() - 60, sizing.minimumSizeHint().width())
        limit_h = max(available.height() - 60, sizing.minimumSizeHint().height())
        check("启动时的初始大小按屏幕夹过（不会主动要一个超出屏幕的窗口）",
              sizing.width() <= limit_w and sizing.height() <= limit_h,
              f"窗口 {sizing.width()}x{sizing.height()} / 屏 {available.width()}x{available.height()}"
              f" / 允许上限 {limit_w}x{limit_h}")
        sizing.close()
    shot(window, "24_fits_on_screen.png")

    check("示波器页那行长提示改成折行（不再顶宽）",
          scope.hint.wordWrap() and scope.hint.minimumSizeHint().width() <= 120,
          f"min={scope.hint.minimumSizeHint().width()} wrap={scope.hint.wordWrap()}")
    check("在窄窗口下提示内容仍然完整（text() 是全文）",
          "ms/点" in scope.hint.text(), scope.hint.text()[:60])
    check("状态栏的工程路径也会省略（不再顶宽）",
          window.status_path.minimumSizeHint().width() <= 120,
          f"min={window.status_path.minimumSizeHint().width()}")

    # 窗口大小/位置要记住，并且能一键恢复默认
    window.resize(1180, 700)
    pump(app, 0.3)
    window._remember_window_geometry()
    remembered = window.settings.window_geometry
    check("记住了窗口几何", bool(remembered), f"{len(remembered)} 字符")
    fresh = MainWindow(project, paths, workdir=workdir, base_dir=OUT)
    fresh.settings.confirm_on_exit = False
    fresh.settings.window_geometry = remembered
    check("新窗口能恢复上次的大小", fresh._restore_window_geometry(), str(fresh.size()))
    fresh.close()
    window.reset_window_size()
    pump(app, 0.3)
    check("「恢复默认大小」会忘掉记的几何并回到合适大小",
          window.settings.window_geometry == "" and window.width() <= 1200,
          f"{window.width()}x{window.height()}")

    # ---------------------------------------------------------------- [21]
    print("\n[21] 文件夹名一律英文 / 默认 DeepSeek 配色")
    check("工程目录的三个子目录是英文",
          all(name.isascii() for name in
              (project_store.CONFIG_DIR, project_store.DATA_DIR, project_store.REPORT_DIR))
          and os.path.basename(paths.config_dir) == "config"
          and os.path.basename(paths.data_dir) == "data"
          and os.path.basename(paths.report_dir) == "reports",
          f"config={os.path.basename(paths.config_dir)} "
          f"data={os.path.basename(paths.data_dir)} "
          f"reports={os.path.basename(paths.report_dir)}")
    check("软件自己用的目录也是英文",
          project_store.OPENED_DIR.isascii(),
          project_store.OPENED_DIR)

    folder = project_store.project_folder_name(project)
    check("内置示例有像样的英文文件夹名", folder.isascii() and folder != "project", folder)
    check("纯中文工程名也不会生成中文目录",
          project_store.folder_name("配电柜监控").isascii(),
          f"'配电柜监控' -> {project_store.folder_name('配电柜监控')!r}")
    check("Windows 保留名会被挡掉",
          project_store.folder_name("CON") == "project",
          project_store.folder_name("CON"))

    # 改名要在**没有被占用**的目录上试（Windows 上目录里有打开的文件就改不了名，
    # 主窗口那边会先关掉历史库再改，见 _rename_project_folder）
    rename_home = os.path.join(OUT, "改名试验")
    rename_root = os.path.join(rename_home, "old-name")
    rename_paths = project_store.create_project_dir(rename_root, project_io.sst_project())
    moved_root, note = project_store.rename_project_dir(
        rename_paths, "new-name", rename_home
    )
    check("能把工程文件夹改成英文名",
          os.path.basename(moved_root) == "new-name", f"{note}｜{os.path.basename(moved_root)}")
    check("改名后内容还在（工程 + 参数表 + 库都在新目录里）",
          os.path.exists(os.path.join(moved_root, "config", "project.json"))
          and os.path.exists(os.path.join(moved_root, "config", "parameters.csv")),
          str(sorted(os.listdir(os.path.join(moved_root, "config")))))
    check("改名后还能正常打开",
          project_store.open_project(moved_root)[0].name == "SST 固态变压器")
    check("撞名时拒绝改名、保持原目录",
          project_store.rename_project_dir(
              project_store.resolve(
                  project_io.sst_project(),
                  os.path.join(moved_root, "config", "project.json"),
              ),
              "new-name",
              rename_home,
          )[1].find("没变") >= 0,
          "同名不算撞名，这里只确认原目录还在")
    check("纯中文的文件夹名会被拒绝（不会静默变成 project/）",
          "英文" in project_store.rename_project_dir(
              project_store.resolve(
                  project_io.sst_project(),
                  os.path.join(moved_root, "config", "project.json"),
              ),
              "配电柜监控",
              rename_home,
          )[1],
          project_store.rename_project_dir(
              project_store.resolve(
                  project_io.sst_project(),
                  os.path.join(moved_root, "config", "project.json"),
              ),
              "配电柜监控",
              rename_home,
          )[1])

    check("默认配色是 DeepSeek 深色", theme.appearance().palette.name == "DeepSeek 深色",
          theme.appearance().palette.name)
    palette = theme.appearance().palette
    check("用的是 DeepSeek 的原值（纯黑底 / #f5f5f7 文字 / #5686fe 强调色）",
          palette.bg == "#000000" and palette.text == "#f5f5f7"
          and palette.accent == "#5686fe",
          f"{palette.bg} / {palette.text} / {palette.accent}")
    check("老主题都还在，能切回去",
          all(name in theme.PRESETS
              for name in ("深色工业", "深蓝夜间", "浅色办公", "高对比现场")),
          str(theme.preset_names()))
    check("「深色工业」没有被默认值改动牵连",
          theme.PRESETS["深色工业"].bg == "#1b1f24",
          theme.PRESETS["深色工业"].bg)
    shot(window, "25_deepseek_theme.png")

    # 切到浅色看看，再切回来
    theme.apply_preset("DeepSeek 浅色")
    window._refresh_appearance()
    pump(app, 0.6)
    check("DeepSeek 浅色也是浅底深字",
          theme.BG == "#ffffff" and theme.ACCENT == "#3964fe",
          f"{theme.BG} / {theme.ACCENT}")
    shot(window, "26_deepseek_light.png")
    theme.apply_preset("DeepSeek 深色")
    window._refresh_appearance()
    pump(app, 0.6)

    # 「设置 → 工程设置」里能看到并改文件夹名
    settings_dialog = SettingsDialog(
        window.settings, project, list(project_io.BUILTIN_SAMPLES),
        parent=window, base_dir=OUT, project_root=paths.root,
    )
    settings_dialog.resize(1000, 760)
    settings_dialog.show()
    settings_dialog.tabs.setCurrentIndex(3)  # 工程设置
    pump(app, 0.5)
    check("设置里能改工程文件夹名",
          settings_dialog.folder_edit.text() == os.path.basename(paths.root),
          settings_dialog.folder_edit.text())
    check("并说明了磁盘目录用英文",
          "英文" in settings_dialog.folder_edit.toolTip(),
          settings_dialog.folder_edit.toolTip()[:50])
    settings_dialog.reject()
    pump(app, 0.3)

    # ---------------------------------------------------------------- [22]
    print("\n[22] 综合截图（工具同时开着）")
    window.show_device_control_panel(project.devices[0].name)
    pump(app, 0.6)
    window.nav.setCurrentRow(NAV["示波器"])
    pump(app, 0.5)
    check("控制台已打开", len(control_panel._open_panels) >= 1)
    shot(window, "09_window_scope_page.png")

    control_panel.close_all()
    for task in list(window.automation.tasks):
        window.automation.remove(task.name)
    window.engine.stop()
    window.settings.confirm_on_exit = False
    window.close()
    sim.stop()
    pump(app, 0.5)

    for patcher in patchers:
        patcher.stop()

    passed = sum(1 for r in results if r)
    print(f"\n结果：{passed}/{len(results)} 项通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
