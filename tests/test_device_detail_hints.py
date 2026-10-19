"""设备详情页「通信状态」的提示文字，以及引擎对"通道被禁用"的日志。

现场最容易踩的一脚：通道或设备的「启用」没勾，点开始采集后引擎根本不会为它
建采集线程，页面却一直写「尚未启动采集」——用户会以为按钮没生效。
这里盯住三件事：
* 没启用通道/设备时，页面直接说明是哪一项被禁用、去哪里启用；
* 采集在跑但还没收到第一轮数据时，提示"等待数据"；
* 引擎启动时把跳过的通道写进通信日志，报告页/事后排查也看得到。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core.engine import AcquisitionEngine  # noqa: E402
from icomm.core.tags import DeviceStatus  # noqa: E402
from icomm.ui.pages.device_detail import DeviceDetailPage  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class DeviceDetailHintTest(unittest.TestCase):
    def setUp(self) -> None:
        self.project = project_io.bess_project()
        self.device = self.project.devices[0]
        self.page = DeviceDetailPage(self.project)
        self.addCleanup(self.page.deleteLater)
        self.running = False
        self.page.bind_engine_running(lambda: self.running)
        self.page.show_device(self.device.name)

    def text(self) -> str:
        return self.page.status_label.text()

    def test_not_started(self) -> None:
        self.assertIn("尚未启动采集", self.text())

    def test_started_waits_for_first_round(self) -> None:
        self.running = True
        self.page.refresh_status_hint()
        self.assertIn("等待第一轮数据", self.text())

    def test_disabled_channel_is_named(self) -> None:
        self.project.channel(self.device.channel).enabled = False
        self.running = True
        self.page.refresh_status_hint()
        self.assertIn("已禁用", self.text())
        self.assertIn(self.device.channel, self.text(), "要说清是哪条通道")
        self.assertIn("通道已禁用", self.page.state_label.text())

    def test_disabled_device_is_named(self) -> None:
        self.device.enabled = False
        self.running = True
        self.page.refresh_status_hint()
        self.assertIn("设备", self.text())
        self.assertIn("已禁用", self.text())
        self.assertIn("设备已禁用", self.page.state_label.text())

    def test_missing_channel_is_explained(self) -> None:
        self.device.channel = "不存在的通道"
        self.running = True
        self.page.refresh_status_hint()
        self.assertIn("不存在", self.text())

    def test_real_status_replaces_hint(self) -> None:
        self.project.channel(self.device.channel).enabled = False
        self.page.refresh_status_hint()
        status = DeviceStatus(name=self.device.name, state="正常", connected=True)
        status.mark_ok(1.0)
        self.page.on_device_state(status)
        self.assertIn("请求 / 成功 / 失败", self.text())
        self.assertNotIn("已禁用", self.text(), "收到真实状态后不该再显示提示")


class EngineSkipTest(unittest.TestCase):
    def test_disabled_channel_is_logged_and_has_no_worker(self) -> None:
        project = project_io.bess_project()
        channel = project.channels[0]
        channel.enabled = False
        logs: list[tuple[str, str, str]] = []
        engine = AcquisitionEngine(project)
        engine.on_log = lambda name, level, message: logs.append((name, level, message))
        engine.start()
        try:
            self.assertEqual(engine.running_channels(), [])
            self.assertTrue(
                any(
                    level == "warning" and "已禁用" in message
                    for _name, level, message in logs
                ),
                f"通道被禁用要写日志，实际：{logs}",
            )
            self.assertTrue(any("没有可运行的通道" in message for _n, _l, message in logs))
        finally:
            engine.stop()

    def test_channel_without_enabled_devices_is_logged(self) -> None:
        project = project_io.bess_project()
        for device in project.devices:
            device.enabled = False
        logs: list[tuple[str, str, str]] = []
        engine = AcquisitionEngine(project)
        engine.on_log = lambda name, level, message: logs.append((name, level, message))
        engine.start()
        try:
            self.assertEqual(engine.running_channels(), [])
            self.assertTrue(any("没有启用的设备" in message for _n, _l, message in logs))
        finally:
            engine.stop()


if __name__ == "__main__":
    unittest.main()
