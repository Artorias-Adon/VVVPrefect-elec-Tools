"""IEC 61850 仿真装置：拿内置 ICD 起一个真的 MMS 服务端，值还会动。

和 Modbus / CAN 的仿真器一个思路：不接真机也能把 61850 驱动、点表、报警、趋势
整条链路跑通。默认服务 ``assets/sim_iec61850.icd``，里面有一台小装置：

    MEAS/MMXU1   Hz（频率）、A（电流）      —— 测量值，周期波动
    MEAS/ASG1    SetPnt                    —— 定值（只读，见下）
    CTRL/XCBR1   Pos.stVal                 —— 开关位置，周期分合
    MEAS/LLN0   数据集 Measure + 报告控制块 RP1

**也可以换成装置厂家给你的 ICD/CID**：``Iec61850Simulator(scl_path="真机.icd", ied_name="XXX")``。
这时只做"静态服务"（值保持 ICD 里的初值），主要用来先把点表配好、
连上看看引用写对没有 —— 现场很实用：真机不在手边也能对点表。

写值说明：这套绑定（libiec61850）里，服务端对 CF/SP 属性的写权限要在模型里声明，
内置 ICD 的属性目前是**只读**的，写值会回 ``ObjectAccessDenied``。
所以这里定位是"只读仿真"：看数据、跑报警、验点表都够；验写值请接真机
（驱动侧的写值实现与真机完全一样）。
"""

from __future__ import annotations

import asyncio
import math
import os
import sys
import threading
import time

#: 内置 ICD 在项目里的相对位置
BUNDLED_ICD = os.path.join("assets", "sim_iec61850.icd")
#: 默认端口：不用 102（102 在 Windows 上要管理员权限才能绑）
DEFAULT_PORT = 8102
#: 内置 ICD 里的 IED 名
IED_NAME = "VVPSIM"
#: 线上逻辑设备名 = IED 名 + LD 名
LD_MEAS = f"{IED_NAME}MEAS"
LD_CTRL = f"{IED_NAME}CTRL"

#: 会周期性变化的测量量：(SCL 路径, 基准值, 波动幅度, 波动频率Hz)
FLOAT_TAGS: list[tuple[str, float, float, float]] = [
    ("MEAS/MMXU1.Hz.mag.f", 50.0, 0.08, 0.13),
    ("MEAS/MMXU1.A.mag.f", 250.0, 24.0, 0.07),
]
#: 开关位置（Dbpos：0 中间 1 合 2 分 3 坏），每 TOGGLE_SECONDS 变一次
POSITION_SCL = "CTRL/XCBR1.Pos.stVal"
TOGGLE_SECONDS = 4.0


def resource_roots() -> list[str]:
    """可能放资源（assets）的目录，打包后也能找到。"""
    roots: list[str] = []
    bundle = getattr(sys, "_MEIPASS", "")
    if bundle:
        roots.append(bundle)
    if getattr(sys, "frozen", False):
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    here = os.path.dirname(os.path.abspath(__file__))
    roots.append(os.path.dirname(here))
    roots.append(os.getcwd())
    return roots


def bundled_icd_path() -> str:
    """内置 ICD 的完整路径（找不到就返回期望位置，便于报错里显示）。"""
    candidates = [os.path.join(root, BUNDLED_ICD) for root in resource_roots()]
    for path in candidates:
        if os.path.exists(path):
            return path
    return candidates[0]


class Iec61850Simulator:
    """一个会动的 IEC 61850 装置（真 MMS 服务端）。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        scl_path: str = "",
        ied_name: str = IED_NAME,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.scl_path = scl_path or bundled_icd_path()
        self.ied_name = ied_name
        #: 用内置 ICD 时才驱动那些量；换成用户的 ICD 就只做静态服务
        self.drive_values = not scl_path
        self._server = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._error = ""

    @property
    def running(self) -> bool:
        return bool(self._running and self._server is not None)

    @property
    def error(self) -> str:
        return self._error

    @property
    def address(self) -> str:
        """实际绑定到的地址（端口给 0 时会拿到系统分配的端口）。"""
        server = self._server
        if server is not None and self.running:
            try:
                return str(server.bound_addr)
            except Exception:
                pass
        return f"{self.host}:{self.port}"

    def start(self) -> "Iec61850Simulator":
        """起服务端与"装置行为"任务；失败抛异常（调用方负责提示）。"""
        if self.running:
            return self
        from iec61850 import IedServer

        if not os.path.exists(self.scl_path):
            raise FileNotFoundError(f"找不到 ICD 文件：{self.scl_path}")
        server = IedServer.from_scl(self.scl_path, ied_name=self.ied_name)
        server.bind(f"{self.host}:{self.port}")
        self._server = server
        self._running = True
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop, name="icomm-61850-sim", daemon=True
        )
        self._thread.start()
        # 等绑定完成（bound_addr 在 start() 之后才有意义）
        deadline = time.time() + 5.0
        while time.time() < deadline and not self._bound():
            time.sleep(0.05)
        return self

    def _bound(self) -> bool:
        try:
            return bool(self._server is not None and self._server.is_running)
        except Exception:
            return False

    def _run_loop(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:  # noqa: BLE001 - 线程里抛出去没人接，记下来给界面
            self._error = str(exc)
        finally:
            self._running = False

    async def _serve(self) -> None:
        server = self._server
        if server is None:
            return
        # start() 是同步方法，但必须在有事件循环的线程里调 —— 这是这套绑定的约定
        server.start()
        behaviour = asyncio.ensure_future(self._behave())
        try:
            while self._running:
                await asyncio.sleep(0.2)
        finally:
            behaviour.cancel()
            try:
                server.stop()
            except Exception:
                pass

    async def _behave(self) -> None:
        """让数据动起来：测量值周期波动、开关位置周期分合。"""
        if not self.drive_values:
            return
        server = self._server
        started = time.monotonic()
        toggled_at = started
        position = 1
        # 先刷一遍再进循环：刚起来的仿真装置马上就是"有值"的，
        # 客户端连上第一拍不至于读到 0
        for path, base, _swing, _rate in FLOAT_TAGS:
            try:
                server.update_float32(path, base)
            except Exception:
                pass
        try:
            server.update_int32(POSITION_SCL, position)
        except Exception:
            pass
        while self._running:
            await asyncio.sleep(0.2)
            elapsed = time.monotonic() - started
            for path, base, swing, rate in FLOAT_TAGS:
                value = base + swing * math.sin(2 * math.pi * rate * elapsed)
                try:
                    server.update_float32(path, value)
                except Exception:
                    pass
            if time.monotonic() - toggled_at >= TOGGLE_SECONDS:
                toggled_at = time.monotonic()
                position = 2 if position == 1 else 1
                try:
                    server.update_int32(POSITION_SCL, position)
                except Exception:
                    pass

    def stop(self) -> None:
        """停服务端并回收线程（允许重复调用）。"""
        self._running = False
        thread = self._thread
        loop = self._loop
        self._thread = None
        self._loop = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None:
            thread.join(timeout=5.0)
        if loop is not None:
            # 先把还挂着的任务收掉再关循环，否则解释器退出时会刷 "Event loop is closed"
            try:
                pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass
        self._server = None


def main() -> int:
    """命令行直接起一个仿真装置（不带界面），方便单独调试。"""
    import argparse

    parser = argparse.ArgumentParser(description="IEC 61850 仿真装置")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--scl", default="", help="用你自己的 ICD/CID 文件")
    parser.add_argument("--ied", default=IED_NAME, help="IED 名")
    args = parser.parse_args()

    sim = Iec61850Simulator(args.host, args.port, args.scl, args.ied)
    sim.start()
    print(f"[61850] 仿真装置已启动 {sim.address}（IED {args.ied}，ICD {sim.scl_path}）")
    if not sim.drive_values:
        print("[61850] 用的是自定义 ICD：只做静态服务，值不会动")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
        print("[61850] 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
