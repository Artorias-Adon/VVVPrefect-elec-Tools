"""高频报文到达时保持表格增量刷新。"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.ui.pages.monitor import MonitorPage  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


class MonitorFramesTest(unittest.TestCase):
    def setUp(self) -> None:
        with patch("icomm.ui.pages.monitor.MAX_FRAMES", 4):
            self.page = MonitorPage()
        self.addCleanup(self.page.deleteLater)
        self.page.set_channels(["TCP", "RTU"])

    def displayed_frames(self) -> list[str]:
        table = self.page.frame_table
        return [table.item(row, 3).text() for row in range(table.rowCount())]

    def test_new_frames_keep_existing_cells(self) -> None:
        self.page.on_frame("TCP", "TX", "request")
        original = self.page.frame_table.item(0, 3)
        self.page.on_frame("TCP", "RX", "response")
        self.page.on_frame("RTU", "RX", "other response")

        self.assertIs(self.page.frame_table.item(0, 3), original)
        self.assertEqual(self.displayed_frames(), ["request", "response", "other response"])

    def test_eviction_respects_active_channel_and_direction_filters(self) -> None:
        self.page.channel_combo.setCurrentIndex(1)
        self.page.direction_combo.setCurrentIndex(2)
        self.page.on_frame("RTU", "RX", "hidden channel")
        self.page.on_frame("TCP", "RX", "old response")
        self.page.on_frame("TCP", "TX", "hidden direction")
        self.page.on_frame("TCP", "RX", "kept response")

        self.page.on_frame("TCP", "RX", "new response")
        self.assertEqual(self.displayed_frames(), ["old response", "kept response", "new response"])
        self.page.on_frame("RTU", "RX", "another hidden channel")
        self.assertEqual(self.displayed_frames(), ["kept response", "new response"])
        self.assertEqual(self.page.counter.text(), "报文 4 条 / 显示 2 条")

    def test_resume_reflects_new_filter_and_events_buffered_while_paused(self) -> None:
        self.page.on_frame("TCP", "TX", "visible before pause")
        self.page.pause_box.setChecked(True)
        self.page.direction_combo.setCurrentIndex(2)
        for index in range(5):
            self.page.on_frame("TCP", "RX", f"response {index}")

        self.assertEqual(self.displayed_frames(), ["visible before pause"])
        self.page.pause_box.setChecked(False)
        self.assertEqual(self.displayed_frames(), [f"response {index}" for index in range(1, 5)])

        self.page.on_frame("TCP", "RX", "response 5")
        self.assertEqual(self.displayed_frames(), [f"response {index}" for index in range(2, 6)])

    def test_filter_change_and_clear_rebuild_from_current_buffer(self) -> None:
        self.page.on_frame("TCP", "TX", "tcp")
        self.page.on_frame("RTU", "TX", "rtu")
        self.page.channel_combo.setCurrentIndex(2)
        self.assertEqual(self.displayed_frames(), ["rtu"])

        self.page._clear()
        self.assertEqual(self.displayed_frames(), [])
        self.assertEqual(self.page.counter.text(), "报文 0 条")
        self.page.on_frame("RTU", "RX", "new rtu")
        self.assertEqual(self.displayed_frames(), ["new rtu"])
