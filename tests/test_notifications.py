"""右上角通知中心：报警 / 故障 / 功能结束的提醒与闪烁红点。

用户要求："在右上角显示运行状态的旁边做一个通知功能，比如发生故障、报警、
某个功能结束。在这个位置让通知图标闪烁（重要内容比如故障），或者红点标记。
鼠标点击可以出现悬浮窗来显示简要内容。"
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core.alarms import AlarmEvent  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.main_window import MainWindow  # noqa: E402
from icomm.ui.notify import MAX_NOTICES, Notice, NotificationCenter  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class NoticeCenterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.center = NotificationCenter()
        self.addCleanup(self.center.deleteLater)

    def test_starts_empty(self) -> None:
        self.assertEqual(self.center.notices(), [])
        self.assertEqual(self.center.unread_count(), 0)
        self.assertFalse(self.center.button.is_blinking())

    def test_info_does_not_blink_but_shows_dot(self) -> None:
        self.center.add("info", "数据记录结束", "共 120 行")
        self.assertEqual(self.center.unread_count(), 1)
        self.assertEqual(self.center.important_count(), 0)
        self.assertFalse(self.center.button.is_blinking(), "普通提示不该闪")

    def test_critical_blinks_until_read(self) -> None:
        self.center.add("critical", "设备离线", "储能变流器PCS 通讯中断")
        self.assertEqual(self.center.important_count(), 1)
        self.assertTrue(self.center.button.is_blinking(), "故障/报警要让红点闪")

        self.center.mark_all_read()
        self.assertEqual(self.center.unread_count(), 0)
        self.assertFalse(self.center.button.is_blinking(), "读到就停闪")

    def test_newest_first_and_capped(self) -> None:
        for index in range(MAX_NOTICES + 20):
            self.center.add("info", f"第 {index} 条")
        notices = self.center.notices()
        self.assertEqual(len(notices), MAX_NOTICES)
        self.assertEqual(notices[0].title, f"第 {MAX_NOTICES + 19} 条")

    def test_unknown_level_falls_back_to_info(self) -> None:
        notice = self.center.add("胡说八道", "标题")
        self.assertEqual(notice.level, "info")

    def test_mark_single_read(self) -> None:
        first = self.center.add("warning", "报警", "A 相过压")
        self.center.add("info", "提示", "别的")
        self.center.mark_read(notice=first)
        self.assertTrue(first.read)
        self.assertEqual(self.center.unread_count(), 1)

    def test_clear(self) -> None:
        self.center.add("critical", "故障")
        self.center.clear()
        self.assertEqual(self.center.notices(), [])
        self.assertFalse(self.center.button.is_blinking())

    def test_popup_lists_notices_and_click_marks_read(self) -> None:
        self.center.add("critical", "设备离线", "PCS 掉线")
        self.center.add("info", "数据记录结束", "共 10 行")
        panel = self.center.popup()
        self.addCleanup(panel.deleteLater)
        self.assertEqual(panel.list.count(), 2)
        self.assertFalse(panel.empty.isVisible())
        panel.list.itemClicked.emit(panel.list.item(0))
        self.assertEqual(self.center.unread_count(), 1)

    def test_click_toggles_popup(self) -> None:
        self.center.add("info", "提示")
        self.center.button.click()
        _app.processEvents()  # type: ignore[union-attr]
        panel = self.center.popup()
        self.addCleanup(panel.deleteLater)
        self.assertTrue(panel.isVisible())
        self.center.button.click()
        _app.processEvents()  # type: ignore[union-attr]
        self.assertFalse(panel.isVisible())

    def test_notice_time_and_level_text(self) -> None:
        notice = Notice("critical", "故障")
        self.assertEqual(notice.level_text, "故障")
        self.assertEqual(len(notice.time_text), 8)
        self.assertTrue(notice.important)


class ToolAnnounceTest(unittest.TestCase):
    def test_announce_goes_to_notify(self) -> None:
        seen: list[tuple[str, str, str]] = []
        context = ui_tools.ToolContext(
            project=project_io.bess_project(),
            window=None,
            notify=lambda level, title, text="": seen.append((level, title, text)),
        )
        context.announce("录音完成", "a.wav　3.0 秒")
        self.assertEqual(seen, [("info", "录音完成", "a.wav　3.0 秒")])

    def test_announce_falls_back_to_log(self) -> None:
        seen: list[str] = []
        context = ui_tools.ToolContext(
            project=project_io.bess_project(),
            window=None,
            log=lambda level, message: seen.append(message),
        )
        context.announce("数据记录结束", "共 3 行")
        self.assertEqual(seen, ["数据记录结束：共 3 行"])


class MainWindowNotificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()
        self.paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "工程"), self.project
        )
        self.window = MainWindow(
            self.project, self.paths, workdir=self.tmp.name, base_dir=self.tmp.name
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

    def test_bell_sits_next_to_run_state(self) -> None:
        corner = self.window.menuBar().cornerWidget(Qt.TopRightCorner)
        self.assertIsNotNone(corner)
        self.assertTrue(corner.isAncestorOf(self.window.notifications))
        self.assertTrue(corner.isAncestorOf(self.window.mode_label))

    def test_alarm_event_becomes_notification(self) -> None:
        event = AlarmEvent(
            timestamp=0.0,
            pid="储能变流器PCS.直流母线电压",
            device="储能变流器PCS",
            tag="直流母线电压",
            level="critical",
            message="直流母线电压 高高限报警 (1250V ≥ 1200)",
            value=1250.0,
        )
        self.window._on_alarm(event)
        notices = self.window.notifications.notices()
        self.assertTrue(notices)
        self.assertEqual(notices[0].level, "critical")
        self.assertIn("直流母线电压", notices[0].text)
        self.assertTrue(self.window.notifications.button.is_blinking())

    def test_alarm_recovery_is_plain_info(self) -> None:
        clear = AlarmEvent(
            timestamp=0.0,
            pid="p",
            device="储能变流器PCS",
            tag="t",
            level="info",
            message="直流母线电压 恢复正常",
            active=False,
        )
        self.window._on_alarm(clear)
        self.assertEqual(self.window.notifications.notices()[0].level, "info")
        self.assertFalse(self.window.notifications.button.is_blinking())

    def test_device_offline_notifies_once(self) -> None:
        class _Status:
            def __init__(self, connected: bool, error: str = "") -> None:
                self.name = "储能变流器PCS"
                self.connected = connected
                self.state = "离线" if not connected else "在线"
                self.error = error

        self.window._on_device_state(_Status(True))
        self.assertEqual(self.window.notifications.unread_count(), 0, "上线不该打扰")
        self.window._on_device_state(_Status(False, "连接超时"))
        self.window._on_device_state(_Status(False, "连接超时"))
        notices = self.window.notifications.notices()
        self.assertEqual(len(notices), 1, "掉线只通知一次，别刷屏")
        self.assertEqual(notices[0].level, "critical")
        self.assertIn("连接超时", notices[0].text)

        self.window._on_device_state(_Status(True))
        self.assertEqual(self.window.notifications.unread_count(), 2)
        self.assertEqual(self.window.notifications.notices()[0].title, "设备恢复")

    def test_notify_is_reachable_from_tool_context(self) -> None:
        context = self.window.tool_context()
        self.assertIsNotNone(context.notify)
        context.announce("故障录波完成", "2 条曲线")
        self.assertEqual(
            self.window.notifications.notices()[0].title, "故障录波完成"
        )


class SettingsUnaffectedTest(unittest.TestCase):
    """通知是运行期的东西，不该往工程/设置里写。"""

    def test_settings_has_no_notice_field(self) -> None:
        self.assertNotIn("notifications", AppSettings().to_dict())
        self.assertNotIn("notices", AppSettings().to_dict())


if __name__ == "__main__":
    unittest.main()
