"""通讯状态里的丢包率：发出去没人应答的比例。

口径说清楚（避免把"包到了但内容不对"算成丢包）：
* 每轮询一次算 1 次请求（成功 + 失败）；
* 其中异常文案是"无响应 / 超时 / timed out…"的，才算**丢包**；
* CRC 错、异常响应说明包到了，只算失败，不算丢包；
* 另外看最近 100 轮（链路刚坏/刚恢复时更灵）。
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core.tags import (  # noqa: E402
    RECENT_WINDOW,
    DeviceStatus,
    format_loss_rate,
    looks_like_no_response,
)
from icomm.ui.main_window import MainWindow  # noqa: E402
from icomm.ui.pages.device_detail import DeviceDetailPage  # noqa: E402
from icomm.ui.pages.monitor import MonitorPage  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class TimeoutClassificationTest(unittest.TestCase):
    def test_driver_wording_is_recognised(self) -> None:
        for text in (
            "读取 无响应",
            "轮询超时",
            "Read timed out",
            "no response from device",
            "请求 1500 ms 内没有收到应答",
            "SDO 超时：500 ms 内没有收到 0x581 应答",
        ):
            with self.subTest(text=text):
                self.assertTrue(looks_like_no_response(text))

    def test_answered_but_wrong_is_not_loss(self) -> None:
        for text in (
            "返回异常响应: IllegalAddress",
            "响应长度不足：需要 4 个寄存器，只收到 2 个",
            "CRC 校验失败",
            "",
        ):
            with self.subTest(text=text):
                self.assertFalse(looks_like_no_response(text), f"{text} 不是丢包")

    def test_link_level_failures_count_as_loss(self) -> None:
        for text in ("连接失败：127.0.0.1:1", "连接不可用", "连接未建立", "Connection refused"):
            with self.subTest(text=text):
                self.assertTrue(looks_like_no_response(text), "链路不通就是 100% 丢包")

    def test_link_level_failures_count_as_loss(self) -> None:
        for text in ("连接失败：127.0.0.1:1", "连接不可用", "Connection refused"):
            with self.subTest(text=text):
                self.assertTrue(looks_like_no_response(text), "链路不通就是 100% 丢包")


class LossRateMathTest(unittest.TestCase):
    def test_no_request_yet(self) -> None:
        status = DeviceStatus(name="d")
        self.assertIsNone(status.loss_rate)
        self.assertIsNone(status.recent_loss_rate)
        self.assertEqual(status.loss_text(), "--")

    def test_all_good(self) -> None:
        status = DeviceStatus(name="d")
        for _ in range(5):
            status.mark_ok(3.0)
        self.assertEqual(status.request_count, 5)
        self.assertEqual(status.timeout_count, 0)
        self.assertEqual(status.loss_rate, 0.0)
        self.assertEqual(status.loss_text(), "0.0%")

    def test_timeouts_count_as_loss(self) -> None:
        status = DeviceStatus(name="d")
        for _ in range(3):
            status.mark_ok(2.0)
        status.mark_error("读取 无响应")
        self.assertEqual(status.request_count, 4)
        self.assertEqual(status.ok_count, 3)
        self.assertEqual(status.error_count, 1)
        self.assertEqual(status.timeout_count, 1)
        self.assertAlmostEqual(status.loss_rate, 0.25)
        self.assertEqual(status.loss_text(), "25.0%")
        self.assertEqual(status.recent_loss_text(), "25.0%")

    def test_content_errors_are_not_loss(self) -> None:
        status = DeviceStatus(name="d")
        status.mark_ok(2.0)
        status.mark_error("返回异常响应: IllegalAddress")
        self.assertEqual(status.error_count, 1, "失败次数还是要 +1")
        self.assertEqual(status.timeout_count, 0, "包到了就不算丢包")
        self.assertEqual(status.loss_rate, 0.0)
        self.assertFalse(status.connected)

    def test_recent_window_only_looks_back(self) -> None:
        status = DeviceStatus(name="d")
        for _ in range(RECENT_WINDOW + 50):
            status.mark_ok(1.0)
        for _ in range(RECENT_WINDOW):
            status.mark_error("读取 无响应", 900.0)
        self.assertEqual(status.request_count, 2 * RECENT_WINDOW + 50)
        self.assertAlmostEqual(status.loss_rate, RECENT_WINDOW / (2 * RECENT_WINDOW + 50))
        self.assertAlmostEqual(status.recent_loss_rate, 1.0, "最近一轮全是丢包")

    def test_recovery_lowers_recent_rate(self) -> None:
        status = DeviceStatus(name="d")
        for _ in range(10):
            status.mark_error("无响应")
        for _ in range(10):
            status.mark_ok(2.0)
        self.assertAlmostEqual(status.loss_rate, 0.5)
        self.assertAlmostEqual(status.recent_loss_rate, 0.5)
        for _ in range(80):
            status.mark_ok(2.0)
        self.assertLess(status.recent_loss_rate, 0.2, "恢复以后最近丢包率要降下来")

    def test_format_helpers(self) -> None:
        self.assertEqual(format_loss_rate(None), "--")
        self.assertEqual(format_loss_rate(0), "0.0%")
        self.assertEqual(format_loss_rate(0.0123), "1.2%")
        self.assertEqual(format_loss_rate(0.5), "50.0%")
        self.assertEqual(format_loss_rate(-1), "0.0%")
        self.assertEqual(format_loss_rate(1.234), "123.4%")


class DeviceDetailLossTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = project_io.bess_project()
        self.page = DeviceDetailPage(self.project)
        self.addCleanup(self.page.deleteLater)
        self.page.show_device(self.project.devices[0].name)

    def status(self) -> DeviceStatus:
        status = DeviceStatus(name=self.project.devices[0].name, state="正常", connected=True)
        for _ in range(3):
            status.mark_ok(4.0)
        status.mark_error("读取 无响应", 1000.0)
        return status

    def test_shows_loss_rate(self) -> None:
        self.page.on_device_state(self.status())
        text = self.page.status_label.text()
        self.assertIn("丢包率（累计）", text)
        self.assertIn("25.0%", text)
        self.assertIn(f"最近 {RECENT_WINDOW} 次", text)
        self.assertIn("无响应（丢包）", text)

    def test_no_requests_shows_dash(self) -> None:
        self.page.on_device_state(DeviceStatus(name=self.project.devices[0].name))
        self.assertIn("--", self.page.status_label.text())

    def test_switching_device_shows_cached_status(self) -> None:
        """切到某台设备时不该空着等下一轮轮询（状态是缓存下来的）。"""
        other = self.project.devices[1].name
        self.page.on_device_state(DeviceStatus(name=other, connected=True, state="正常"))
        self.page.show_device(other)
        self.assertIn("请求 / 成功 / 失败", self.page.status_label.text())


class MonitorStatusTabTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = project_io.bess_project()
        self.page = MonitorPage()
        self.addCleanup(self.page.deleteLater)

    def status(self, name: str, timeouts: int = 1) -> DeviceStatus:
        status = DeviceStatus(name=name, state="正常", connected=True)
        for _ in range(3):
            status.mark_ok(3.0)
        for _ in range(timeouts):
            status.mark_error("读取 无响应", 900.0)
        return status

    def test_tab_and_columns(self) -> None:
        labels = [self.page.tabs.tabText(i) for i in range(self.page.tabs.count())]
        self.assertIn("设备状态", labels)
        headers = [
            self.page.status_table.horizontalHeaderItem(i).text()
            for i in range(self.page.status_table.columnCount())
        ]
        self.assertIn("丢包率（累计）", headers)
        self.assertIn("无响应", headers)

    def test_row_shows_rate(self) -> None:
        self.page.on_device_state(self.status("1号整流器"), "储能EMS链")
        row = self.page.status_row("1号整流器")
        self.assertIsNotNone(row)
        self.assertEqual(row[1], "储能EMS链")
        self.assertEqual(row[3], "4", "请求 = 3 成功 + 1 失败")
        self.assertEqual(row[4], "1", "无响应次数")
        self.assertEqual(row[5], "25.0%")
        self.assertEqual(row[6], "25.0%")

    def test_set_and_clear(self) -> None:
        self.page.set_statuses([(self.status("甲"), "A"), (self.status("乙"), "B")])
        self.assertEqual(self.page.status_table.rowCount(), 2)
        self.page.clear_statuses()
        self.assertEqual(self.page.status_table.rowCount(), 0)
        self.assertIsNone(self.page.status_row("甲"))

    def test_pause_freezes_table(self) -> None:
        self.page.pause_box.setChecked(True)
        self.page.on_device_state(self.status("甲"), "A")
        self.assertEqual(self.page.status_table.rowCount(), 0, "暂停时不刷新")
        self.page.pause_box.setChecked(False)
        self.page.on_device_state(self.status("甲"), "A")
        self.assertEqual(self.page.status_table.rowCount(), 1)


class MainWindowLossWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()
        paths = project_store.create_project_dir(os.path.join(self.tmp.name, "proj"), self.project)
        self.window = MainWindow(
            self.project, paths, workdir=self.tmp.name, base_dir=self.tmp.name
        )
        self.window.settings.confirm_on_exit = False
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if self.window.engine is not None:
            self.window.engine.stop()
        if self.window.storage is not None:
            self.window.storage.stop()
        self.window.settings.confirm_on_exit = False
        self.window.close()

    def test_status_reaches_monitor_tab_with_channel(self) -> None:
        device = self.project.devices[0]
        status = DeviceStatus(name=device.name, state="正常", connected=True)
        status.mark_ok(2.0)
        status.mark_error("读取 无响应")
        self.window._on_device_state(status)
        row = self.window.monitor_page.status_row(device.name)
        self.assertIsNotNone(row, "通信监视的「设备状态」页要能看到这台设备")
        self.assertEqual(row[1], device.channel, "通道名要填上")
        self.assertEqual(row[5], "50.0%")

    def test_detail_page_also_shows_rate(self) -> None:
        device = self.project.devices[0]
        status = DeviceStatus(name=device.name, connected=True, state="正常")
        status.mark_ok(2.0)
        status.mark_error("无响应")
        self.window._on_device_state(status)
        self.window.device_detail_page.show_device(device.name)
        self.window._on_device_state(status)
        self.assertIn("50.0%", self.window.device_detail_page.status_label.text())


if __name__ == "__main__":
    unittest.main()
