"""储能（BESS）示例的 GUI 端到端探针：真窗口 + 真采集 + 真截图。

用来确认「改界面没改坏」——走完一遍主流程：起仿真器、开窗口、等数据、
点控制字命令按钮（开机/放电/关机）、翻各个页面截图、点示意图跳设备详情、写给定值回读。

跑法（必须存成 UTF-8 文件再运行，PowerShell 管道会毁中文）：:

    python tools/probe_bess.py <输出目录>
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.ui.main_window import NAV_ITEMS, MainWindow  # noqa: E402
from tools.modbus_sim import ModbusTcpSimulator  # noqa: E402

OUT = sys.argv[1] if len(sys.argv) > 1 else "."
PORT = 15061
NAV = {name: index for index, name in enumerate(NAV_ITEMS)}

PCS = "储能变流器PCS"
BMS = "电池管理系统BMS"
STATE_PID = f"{PCS}.运行状态字"
BUS_PID = f"{PCS}.直流母线电压"
SET_PID = f"{PCS}.有功功率给定"
SOC_PID = f"{BMS}.SOC"


def pick_port(preferred: int) -> int:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            return 0


def shots(window, name: str) -> None:
    path = os.path.join(OUT, name)
    window.grab().save(path)
    print("       截图 ->", path)


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


def find_button(window, text: str) -> QPushButton | None:
    for button in window.findChildren(QPushButton):
        if button.text().strip() == text:
            return button
    return None


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    port = pick_port(PORT)
    sim = ModbusTcpSimulator("127.0.0.1", port, verbose=False)
    threading.Thread(target=sim.serve_forever, kwargs={"freeze": False}, daemon=True).start()
    time.sleep(0.4)
    print(f"仿真器监听 127.0.0.1:{port}")

    project = project_io.bess_project()
    project.channels[0].options["port"] = port
    device = project.devices[0]

    workdir = os.path.join(OUT, "projects")
    root = os.path.join(workdir, project_store.safe_name(project.name))
    if os.path.exists(root):
        shutil.rmtree(root)
    paths = project_store.create_project_dir(root, project)
    print("工程目录:", root)

    app = QApplication(sys.argv[:1])
    window = MainWindow(project, paths, workdir=workdir)
    window.resize(1560, 940)
    window.show()
    pump(app, 0.6)

    tags = window.engine.tags
    state_point = device.point("运行状态字")
    labels = state_point.bit_labels()
    window.start_engine()

    ready = wait_until(app, lambda: tags.get(f"{PCS}.A相电压").quality == "good", 25.0)
    print("数据就绪:", ready)
    for pid in (
        f"{PCS}.A相电压",
        BUS_PID,
        f"{PCS}.有功功率",
        f"{PCS}.功率模块温度",
        STATE_PID,
        SOC_PID,
    ):
        print("  %-24s = %s" % (pid, tags.get(pid).value))

    print("[1] 主页示意图")
    window.nav.setCurrentRow(NAV["主页"])
    pump(app, 1.5)
    shots(window, "01_overview.png")

    print("[2] 运行监视")
    window.nav.setCurrentRow(NAV["运行监视"])
    pump(app, 1.0)
    shots(window, "02_monitor.png")

    print("[3] 控制字命令按钮：点「放电」")
    discharge = find_button(window, "放电")
    print("       找到「放电」按钮:", discharge is not None)
    if discharge is not None:
        before = tags.get(STATE_PID).value
        discharge.click()
        ok = wait_until(
            app,
            lambda: "放电" in state_point.decode_bits(tags.get(STATE_PID).value, labels),
            20.0,
        )
        value = tags.get(STATE_PID).value
        print(f"       状态字 {before} -> {value}，置位 = "
              f"{state_point.decode_bits(value, labels)}（等到「放电」={ok}）")
        wait_until(app, lambda: float(tags.get(f"{PCS}.有功功率").value or 0) > 10.0, 10.0)
        print("       有功功率 =", tags.get(f"{PCS}.有功功率").value)
        print("       直流母线电压 =", tags.get(BUS_PID).value)
        print("       电池簇电流 =", tags.get(f"{BMS}.电池簇电流").value)
    pump(app, 0.8)
    shots(window, "03_discharging.png")

    print("[4] 控制字命令按钮：点「关机」")
    stop = find_button(window, "关机")
    print("       找到「关机」按钮:", stop is not None)
    if stop is not None:
        stop.click()
        ok = wait_until(
            app,
            lambda: "停机" in state_point.decode_bits(tags.get(STATE_PID).value, labels),
            20.0,
        )
        print(f"       状态字 = {tags.get(STATE_PID).value}（等到「停机」={ok}）")
    pump(app, 0.5)
    shots(window, "04_stopped.png")

    print("[5] 设备与点位（参数表）")
    window.nav.setCurrentRow(NAV["设备与点位"])
    pump(app, 1.2)
    shots(window, "05_devices.png")

    print("[6] 示波器")
    window.nav.setCurrentRow(NAV["示波器"])
    pump(app, 1.5)
    shots(window, "06_scope.png")

    print("[7] 报警记录")
    window.nav.setCurrentRow(NAV["报警记录"])
    pump(app, 0.8)
    shots(window, "07_alarms.png")

    print("[8] 历史趋势")
    window.nav.setCurrentRow(NAV["历史趋势"])
    pump(app, 1.2)
    shots(window, "08_trend.png")

    print("[9] 通信监视")
    window.nav.setCurrentRow(NAV["通信监视"])
    pump(app, 0.8)
    shots(window, "09_comm.png")

    print("[10] 示意图热区跳转")
    node = next((n for n in project.diagram.nodes if n.action == "device"), None)
    print("       可点击图元:", node.id, "=", node.label, "->", node.target)
    window.nav.setCurrentRow(NAV["主页"])
    pump(app, 0.4)
    window.overview_page.nodeActivated.emit(node.action, node.target)
    pump(app, 1.0)
    print("       已跳到设备详情:", window.stack.currentWidget() is window.device_detail_page)
    shots(window, "10_detail_from_diagram.png")

    print("[11] 写参数与归档")
    ok, error = window.engine.write(SET_PID, 300.0)
    print("       写有功给定=300:", ok, error)
    back = wait_until(
        app, lambda: abs(float(tags.get(SET_PID).value or 0) - 300.0) < 0.01, 6.0
    )
    print("       回读:", tags.get(SET_PID).value, "（一致 =", back, "）")
    print("       在线设备:", window.engine.online_count())
    print("       库统计:", window.storage.stats())

    window.close()
    sim.stop()
    pump(app, 0.5)
    print("完成")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
