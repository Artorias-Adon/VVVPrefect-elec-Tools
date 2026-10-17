"""连通性测试：每种通信方式都要能"真连一次"并给出能动手的结论。

盯住这几件事：

* **覆盖**：每种注册过的驱动都有对应的测试项（以后加协议漏了会红）；
* **三种结论分得清**：连不上 / 连上了但对方不答 / 答了但拒绝这个地址；
* **建议对得上协议**：测 S7 时不该给出"确认 Modbus 端口号"这种提示；
* **界面那条路**：参数按协议重建、从工程通道填入、TCP 的报文框只在该协议出现、
  ``run_check`` 的参数凑得对、结果能存成文件。
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import connectivity  # noqa: E402
from icomm.core import project as project_io  # noqa: E402
from icomm.core.connectivity import CheckResult, CheckStep  # noqa: E402
from icomm.core.drivers import driver_names  # noqa: E402
from icomm.core.models import DeviceDef, Project  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.tools.connectivity import ConnectivityDialog, _CheckWorker  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


# --------------------------------------------------------------- 测试用服务


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class FakeTcpServer:
    """一个只会"接受连接"的 TCP 服务，可选要不要回一句话。"""

    def __init__(self, reply: bytes = b"") -> None:
        self.reply = reply
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                conn.settimeout(1.0)
                if self.reply:
                    try:
                        conn.recv(64)
                        conn.sendall(self.reply)
                    except OSError:
                        pass
                else:
                    try:
                        conn.recv(64)  # 收下就不理，模拟"端口通但对方不答"
                    except OSError:
                        pass

    def close(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)


class SimulatorMixin:
    """真开一个 Modbus TCP 从站（和集成测试用的是同一个仿真器）。"""

    @classmethod
    def setUpClass(cls) -> None:
        from tools.modbus_sim import ModbusTcpSimulator

        cls.sim_port = free_port()
        cls.sim = ModbusTcpSimulator("127.0.0.1", cls.sim_port)
        threading.Thread(
            target=cls.sim.serve_forever, kwargs={"freeze": False}, daemon=True
        ).start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", cls.sim_port), 0.5):
                    return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("仿真器端口未就绪")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sim.stop()


# --------------------------------------------------------------- 覆盖与清单


class ProtocolCatalogTest(unittest.TestCase):
    def test_every_registered_driver_has_a_test_entry(self) -> None:
        """每种通信方式都要有连通性测试：新加驱动忘了登记这里就会红。"""
        covered = {spec.driver for spec in connectivity.PROTOCOLS if spec.driver}
        missing = [name for name in driver_names() if name not in covered]
        self.assertEqual(missing, [], f"这些驱动没有连通性测试项：{missing}")

    def test_specs_point_at_real_drivers_and_links(self) -> None:
        for spec in connectivity.PROTOCOLS:
            if not spec.driver:
                continue
            self.assertIn(spec.driver, driver_names(), spec.key)
            self.assertTrue(spec.label and spec.hint, spec.key)

    def test_unknown_protocol_raises_with_choices(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            connectivity.protocol_spec("profibus")
        self.assertIn("modbus_tcp", str(ctx.exception))

    def test_option_fields_follow_the_driver_schema(self) -> None:
        tcp_keys = [field.key for field in connectivity.option_fields("tcp")]
        self.assertEqual(tcp_keys, ["host", "port"])
        rtu_keys = [field.key for field in connectivity.option_fields("modbus_rtu")]
        for expected in ("port", "baudrate", "parity", "stopbits", "timeout"):
            self.assertIn(expected, rtu_keys)
        can_keys = [field.key for field in connectivity.option_fields("can")]
        self.assertIn("backend", can_keys)
        s7_keys = [field.key for field in connectivity.option_fields("s7")]
        self.assertIn("rack", s7_keys)
        self.assertIn("slot", s7_keys)

    def test_needs_unit_only_for_multi_drop_protocols(self) -> None:
        self.assertTrue(connectivity.protocol_spec("modbus_tcp").needs_unit)
        self.assertTrue(connectivity.protocol_spec("modbus_rtu").needs_unit)
        self.assertFalse(connectivity.protocol_spec("tcp").needs_unit)
        self.assertFalse(connectivity.protocol_spec("can").needs_unit)

    def test_defaults_come_from_the_driver(self) -> None:
        defaults = connectivity.default_options_for("modbus_rtu")
        self.assertIn("baudrate", defaults)
        self.assertEqual(connectivity.default_options_for("tcp")["port"], 502)

    def test_target_text_is_human_readable(self) -> None:
        self.assertEqual(
            connectivity.target_text("tcp", {"host": "10.1.2.3", "port": 502}), "10.1.2.3:502"
        )
        self.assertIn(
            "9600",
            connectivity.target_text(
                "modbus_rtu", {"port": "COM3", "baudrate": 9600, "bytesize": 8,
                               "parity": "E", "stopbits": 1}
            ),
        )
        self.assertEqual(
            connectivity.target_text("can", {"backend": "virtual", "channel": "vvp"}),
            "virtual@vvp",
        )

    def test_spec_for_channel_matches_project_channels(self) -> None:
        self.assertEqual(connectivity.spec_for_channel("modbus", "tcp").key, "modbus_tcp")
        self.assertEqual(connectivity.spec_for_channel("modbus", "rtu").key, "modbus_rtu")
        self.assertIsNone(connectivity.spec_for_channel("no-such-driver", "tcp"))


# --------------------------------------------------------------- 建议文案


class SuggestionTest(unittest.TestCase):
    def test_missing_library_hints(self) -> None:
        text = " ".join(connectivity.suggestions("s7", "python-snap7 不可用：No module named 'snap7'"))
        self.assertIn("pip install python-snap7", text)
        text = " ".join(connectivity.suggestions("iec61850", "未安装 iec61850 库，无法使用 61850 驱动"))
        self.assertIn("pip install iec61850", text)
        text = " ".join(connectivity.suggestions("can", "未安装 python-can（x），请执行 pip install python-can"))
        self.assertIn("pip install python-can", text)

    def test_protocol_specific_hints(self) -> None:
        self.assertIn(
            "从站号", " ".join(connectivity.suggestions("modbus_tcp", "连接被拒绝"))
        )
        self.assertIn("波特率", " ".join(connectivity.suggestions("modbus_rtu", "连接被拒绝")))
        self.assertIn("机架", " ".join(connectivity.suggestions("s7", "连接被拒绝")))
        self.assertIn("61850", " ".join(connectivity.suggestions("iec61850", "超时")))

    def test_modbus_specific_advice_not_shown_for_other_protocols(self) -> None:
        """测 S7 时提示"确认 Modbus 端口号"纯属误导，必须过滤掉。"""
        text = " ".join(connectivity.suggestions("s7", "连接被拒绝"))
        self.assertNotIn("Modbus", text)
        modbus_text = " ".join(connectivity.suggestions("modbus_tcp", "连接被拒绝"))
        self.assertIn("Modbus", modbus_text)

    def test_refusal_detection(self) -> None:
        self.assertTrue(connectivity.looks_like_refusal("返回异常响应: ExceptionResponse(2)"))
        self.assertTrue(connectivity.looks_like_refusal("illegal data address"))
        self.assertFalse(connectivity.looks_like_refusal("读取失败：ModbusIOException 无响应"))


# --------------------------------------------------------------- 裸 TCP


class RawTcpCheckTest(unittest.TestCase):
    def test_connects_to_open_port(self) -> None:
        server = FakeTcpServer()
        self.addCleanup(server.close)
        result = connectivity.run_check(
            "tcp", {"host": "127.0.0.1", "port": server.port, "timeout": 2.0}
        )
        self.assertTrue(result.ok, result.headline)
        self.assertIn("通信正常", result.headline)
        self.assertIn("[√]", result.detail())

    def test_refused_port_reports_hints(self) -> None:
        result = connectivity.run_check(
            "tcp", {"host": "127.0.0.1", "port": free_port(), "timeout": 1.0}
        )
        self.assertFalse(result.ok)
        self.assertIn("通信失败", result.headline)
        self.assertIn("排查建议", result.detail())
        self.assertTrue(result.hints)

    def test_payload_without_reply_is_a_warning(self) -> None:
        server = FakeTcpServer()  # 收下但一句不回
        self.addCleanup(server.close)
        result = connectivity.run_check(
            "tcp",
            {"host": "127.0.0.1", "port": server.port, "timeout": 2.0},
            payload=b"\x00\x01\x00\x00\x00\x06\x01\x03\x00\x00\x00\x01",
            read_timeout_s=0.5,
        )
        self.assertTrue(result.ok, "端口是通的，不该判成失败")
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("没收到回应", result.detail())
        self.assertIn("有 1 项提示", result.headline)

    def test_payload_with_reply_shows_bytes(self) -> None:
        server = FakeTcpServer(reply=b"\x00\x01\x00\x00\x00\x05\x01\x03\x02\x00\x0a")
        self.addCleanup(server.close)
        result = connectivity.run_check(
            "tcp",
            {"host": "127.0.0.1", "port": server.port, "timeout": 2.0},
            payload=b"\x00\x01\x00\x00\x00\x06\x01\x03\x00\x00\x00\x01",
        )
        self.assertTrue(result.ok)
        self.assertIn("收到 11 字节", result.detail())

    def test_repeat_attempts_each_get_a_step(self) -> None:
        server = FakeTcpServer()
        self.addCleanup(server.close)
        result = connectivity.run_check(
            "tcp",
            {"host": "127.0.0.1", "port": server.port, "timeout": 2.0},
            attempts=3,
        )
        self.assertEqual(len(result.steps), 3)
        self.assertTrue(result.ok)

    def test_empty_host_fails(self) -> None:
        result = connectivity.run_check("tcp", {"host": "", "port": 502, "timeout": 1.0})
        self.assertFalse(result.ok)
        self.assertIn("没有填写地址", result.detail())


# --------------------------------------------------------------- Modbus


class ModbusCheckTest(SimulatorMixin, unittest.TestCase):
    def test_modbus_tcp_reads_a_register(self) -> None:
        result = connectivity.run_check(
            "modbus_tcp",
            {"host": "127.0.0.1", "port": self.sim_port, "timeout": 1.0, "retry": 0},
            unit_id=1,
        )
        self.assertTrue(result.ok, result.headline)
        self.assertIn("读保持寄存器", result.detail())
        self.assertIn("RX", result.detail(), "明细里应该有真实报文")

    def test_modbus_tcp_wrong_unit_is_warning_not_failure(self) -> None:
        """站号不对时从站会回异常响应 —— 通信其实是通的，不能判失败。"""
        result = connectivity.run_check(
            "modbus_tcp",
            {"host": "127.0.0.1", "port": self.sim_port, "timeout": 1.0, "retry": 0},
            unit_id=99,
        )
        self.assertTrue(result.ok, result.headline)
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("异常响应", result.detail())
        self.assertIn("有 1 项提示", result.headline)

    def test_modbus_tcp_dead_port_fails_with_station_hints(self) -> None:
        result = connectivity.run_check(
            "modbus_tcp",
            {"host": "127.0.0.1", "port": free_port(), "timeout": 1.0, "retry": 0},
            unit_id=1,
        )
        self.assertFalse(result.ok)
        self.assertIn("从站号", " ".join(result.hints))

    def test_modbus_rtu_bad_serial_port_fails_with_wiring_hints(self) -> None:
        result = connectivity.run_check(
            "modbus_rtu",
            {
                "port": "COM_NOT_EXIST",
                "baudrate": 9600,
                "bytesize": 8,
                "parity": "E",
                "stopbits": 1,
                "timeout": 0.5,
                "retry": 0,
            },
            unit_id=1,
        )
        self.assertFalse(result.ok)
        self.assertIn("波特率", " ".join(result.hints))
        self.assertIn("打开连接", result.detail())


# --------------------------------------------------------------- CAN


class CanCheckTest(unittest.TestCase):
    """用 python-can 的虚拟总线真收发，不需要硬件。"""

    def setUp(self) -> None:
        try:
            import can  # noqa: F401
        except Exception as exc:  # pragma: no cover - 取决于运行环境
            self.skipTest(f"没有 python-can：{exc}")

    def test_receiving_a_frame_means_the_bus_is_alive(self) -> None:
        import can

        name = f"vvp-connectivity-{time.time_ns()}"
        stop = threading.Event()

        def sender() -> None:
            with can.Bus(interface="virtual", channel=name) as bus:
                while not stop.is_set():
                    bus.send(
                        can.Message(
                            arbitration_id=0x181, data=b"\x01\x02\x03\x04", is_extended_id=False
                        )
                    )
                    time.sleep(0.03)

        thread = threading.Thread(target=sender, daemon=True)
        thread.start()
        self.addCleanup(stop.set)
        result = connectivity.run_check(
            "can",
            {"backend": "virtual", "channel": name, "bitrate": 500000, "timeout_ms": 100},
            listen_s=2.0,
        )
        self.assertTrue(result.ok, result.headline)
        self.assertIn("收到帧", result.detail())
        self.assertIn("0x00000181", result.detail())

    def test_silent_bus_is_a_warning(self) -> None:
        result = connectivity.run_check(
            "can",
            {
                "backend": "virtual",
                "channel": f"vvp-silent-{time.time_ns()}",
                "bitrate": 500000,
                "timeout_ms": 100,
            },
            listen_s=0.3,
        )
        self.assertTrue(result.ok, "适配器打开了就不算失败")
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("没收到任何帧", result.detail())

    def test_unusable_backend_fails(self) -> None:
        result = connectivity.run_check(
            "can",
            {"backend": "slcan", "channel": "COM_NOT_EXIST", "bitrate": 500000,
             "timeout_ms": 100},
            listen_s=0.3,
        )
        self.assertFalse(result.ok)
        self.assertIn("波特率", " ".join(result.hints))

    def test_stop_signal_ends_the_listen_early(self) -> None:
        started = time.time()
        result = connectivity.run_check(
            "can",
            {
                "backend": "virtual",
                "channel": f"vvp-stop-{time.time_ns()}",
                "bitrate": 500000,
                "timeout_ms": 100,
            },
            listen_s=10.0,
            should_stop=lambda: True,
        )
        self.assertLess(time.time() - started, 5.0, "关窗口时不该卡在监听上")
        self.assertFalse(result.ok and not result.warnings)


# --------------------------------------------------------------- 其它协议


class OtherProtocolsTest(unittest.TestCase):
    def test_s7_dead_port_fails_with_rack_hint(self) -> None:
        result = connectivity.run_check(
            "s7",
            {"host": "127.0.0.1", "port": free_port(), "rack": 0, "slot": 1, "timeout": 0.5},
        )
        self.assertFalse(result.ok)
        self.assertIn("机架", " ".join(result.hints))
        self.assertNotIn("Modbus", " ".join(result.hints))

    def test_iec61850_dead_port_fails_with_mms_hint(self) -> None:
        result = connectivity.run_check(
            "iec61850",
            {
                "host": "127.0.0.1",
                "port": free_port(),
                "timeout_ms": 800,
                "request_timeout_ms": 800,
            },
        )
        self.assertFalse(result.ok)
        self.assertIn("61850", " ".join(result.hints))

    def test_result_text_contains_everything(self) -> None:
        result = CheckResult(
            key="modbus_tcp",
            target="10.0.0.5:502",
            steps=[
                CheckStep("打开连接", connectivity.STEP_OK, "10.0.0.5:502（3 ms）"),
                CheckStep("读保持寄存器 0", connectivity.STEP_FAIL, "没有读到数据：无响应"),
            ],
            hints=["· 先确认从站号"],
            elapsed_ms=12.5,
        )
        self.assertFalse(result.ok)
        self.assertIn("通信失败", result.headline)
        self.assertIn("10.0.0.5:502", result.headline)
        text = result.as_text()
        self.assertIn("[×]", text)
        self.assertIn("排查建议", text)
        self.assertIn("总耗时 12.5 ms", text)

    def test_empty_result_headline(self) -> None:
        self.assertIn("没有执行测试", CheckResult(key="tcp").headline)


# --------------------------------------------------------------- 界面


def test_project() -> Project:
    """两条通道：Modbus TCP（两台从站）+ CAN，够测"从工程通道填入"。"""
    project = Project(name="连通性")
    channel = project_io.sample_project().channels[0]
    channel.name = "车间网关"
    channel.driver = "modbus"
    channel.link = "tcp"
    channel.options = {"host": "10.9.8.7", "port": 5020, "timeout": 1.0}
    project.channels.append(channel)
    can_channel = project_io.sample_project().channels[0]
    can_channel.name = "输送线 CAN"
    can_channel.driver = "can"
    can_channel.link = "can"
    can_channel.options = {"backend": "virtual", "channel": "vvp", "bitrate": 500000}
    project.channels.append(can_channel)
    project.devices.append(
        DeviceDef(name="1号表", channel="车间网关", unit_id=7, points=[])
    )
    return project


class ConnectivityDialogTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.reported: list[tuple[str, str]] = []
        self.announced: list[tuple[str, str, str]] = []
        self.context = ui_tools.ToolContext(
            project=test_project(),
            window=None,
            settings=AppSettings(),
            report_dir=self.tmp.name,
            log=lambda level, message: self.reported.append((level, message)),
            notify=lambda level, title, text="": self.announced.append((level, title, text)),
        )
        self.dialog = ConnectivityDialog(self.context)
        self.addCleanup(self.dialog.deleteLater)

    def select(self, key: str) -> None:
        index = self.dialog.protocol_combo.findData(key)
        self.assertGreaterEqual(index, 0)
        self.dialog.protocol_combo.setCurrentIndex(index)

    def test_tool_is_registered_under_communication(self) -> None:
        tool = ui_tools.find_tool("连通性测试")
        self.assertIsNotNone(tool)
        self.assertEqual(tool.group, "通信")
        self.assertEqual(tool.order, 10)
        names = [item.name for item in ui_tools.tools_by_group()["通信"]]
        self.assertIn("连通性测试", names)

    def test_fields_rebuild_per_protocol(self) -> None:
        self.select("modbus_rtu")
        self.assertIn("baudrate", self.dialog._field_widgets)
        self.assertIsNotNone(self.dialog.unit_box, "Modbus 要能填从站号")
        self.select("can")
        self.assertIn("backend", self.dialog._field_widgets)
        self.assertIsNone(self.dialog.unit_box, "CAN 不该留从站号框")

    def test_payload_box_only_for_raw_tcp(self) -> None:
        self.select("tcp")
        self.assertTrue(self.dialog.payload_box.isVisibleTo(self.dialog))
        self.select("modbus_tcp")
        self.assertFalse(self.dialog.payload_box.isVisibleTo(self.dialog))

    def test_values_survive_switching_protocols(self) -> None:
        self.select("modbus_tcp")
        self.dialog._field_widgets["host"].setText("1.2.3.4")
        self.select("can")
        self.select("modbus_tcp")
        self.assertEqual(self.dialog._field_widgets["host"].text(), "1.2.3.4")

    def test_channel_prefill_fills_protocol_and_fields(self) -> None:
        self.dialog.channel_combo.setCurrentIndex(1)  # 第一条通道
        self.assertEqual(self.dialog.current_key(), "modbus_tcp")
        self.assertEqual(self.dialog._field_widgets["host"].text(), "10.9.8.7")
        self.assertEqual(self.dialog._field_widgets["port"].value(), 5020)
        self.assertEqual(self.dialog.unit_box.value(), 7, "从站号也要跟着通道的设备走")

    def test_channel_list_skips_unsupported_channels(self) -> None:
        labels = [
            self.dialog.channel_combo.itemText(i)
            for i in range(1, self.dialog.channel_combo.count())
        ]
        self.assertTrue(any("车间网关" in label for label in labels))
        self.assertTrue(any("输送线 CAN" in label for label in labels))

    def test_test_kwargs_parse_payload(self) -> None:
        self.select("tcp")
        self.dialog.payload_edit.setPlainText("01 03 00 00 00 0A")
        self.dialog.repeat_box.setValue(2)
        kwargs = self.dialog.test_kwargs()
        self.assertEqual(kwargs["payload"], b"\x01\x03\x00\x00\x00\x0a")
        self.assertEqual(kwargs["attempts"], 2)

    def test_test_kwargs_rejects_bad_hex(self) -> None:
        self.select("tcp")
        self.dialog.payload_edit.setPlainText("z1z1")
        with self.assertRaises(ValueError):
            self.dialog.test_kwargs()

    def test_bad_hex_is_shown_and_no_test_starts(self) -> None:
        self.select("tcp")
        self.dialog.payload_edit.setPlainText("z1")
        self.dialog.start_test()
        self.assertIsNone(self.dialog._thread)
        self.assertIn("十六进制", self.dialog.result_label.text())

    def test_worker_runs_a_real_check(self) -> None:
        server = FakeTcpServer()
        self.addCleanup(server.close)
        results: list[CheckResult] = []
        worker = _CheckWorker(
            "tcp", {"options": {"host": "127.0.0.1", "port": server.port, "timeout": 2.0}}
        )
        worker.finished.connect(results.append)
        worker.run()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].ok)

    def test_finished_result_is_displayed_and_reported(self) -> None:
        result = CheckResult(
            key="s7",
            target="10.0.0.9:102",
            steps=[CheckStep("打开连接", connectivity.STEP_FAIL, "连接被拒绝")],
            hints=["· 机架/插槽"],
        )
        self.dialog._on_finished(result)
        self.assertIn("通信失败", self.dialog.result_label.text())
        self.assertIn("[×]", self.dialog.detail_edit.toPlainText())
        self.assertTrue(self.dialog.save_button.isEnabled())
        self.assertTrue(self.reported, "结果要写一条日志")
        self.assertTrue(self.announced, "测试失败要进通知中心")

    def test_save_result_writes_a_file(self) -> None:
        result = CheckResult(
            key="tcp",
            target="127.0.0.1:502",
            steps=[CheckStep("第 1 次连接", connectivity.STEP_OK, "耗时 1.0 ms")],
        )
        self.dialog._result = result
        self.dialog.save_result()
        folder = os.path.join(self.tmp.name, "connectivity")
        files = os.listdir(folder)
        self.assertEqual(len(files), 1, files)
        with open(os.path.join(folder, files[0]), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("通信正常", text)
        self.assertIn("127.0.0.1:502", text)

    def test_close_while_running_does_not_hang(self) -> None:
        self.dialog._on_finished(CheckResult(key="tcp"))
        self.dialog.done(0)
        self.assertIsNone(self.dialog._thread)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
