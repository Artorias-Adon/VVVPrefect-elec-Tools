"""归档自动清理测试。

用户要求：归档能自动清掉过久的数据；想留底可以手动设（关掉开关或把保留天数调大）；
默认打开、也可以关。

覆盖三层：设置项的默认值与老配置迁移、存储层真的删对了行、
主窗口按开关启停定时器和启动时清一次。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import settings as app_settings  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.storage.db import Storage  # noqa: E402
from icomm.ui.main_window import MainWindow  # noqa: E402

_app: QApplication | None = None

DAY = 86400.0


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def _wait_flush(storage: Storage, want: int, timeout: float = 5.0) -> dict:
    """等归档线程把队列写进库（异步，不能立刻断言）。"""
    deadline = time.time() + timeout
    stats = storage.stats()
    while time.time() < deadline and stats["history"] < want:
        time.sleep(0.05)
        stats = storage.stats()
    return stats


class SettingsDefaultTest(unittest.TestCase):
    def test_auto_purge_on_by_default(self) -> None:
        """默认打开：现场机器一跑几个月，不清理迟早把硬盘写满。"""
        self.assertTrue(AppSettings().archive_auto_purge)

    def test_interval_clamped(self) -> None:
        settings = AppSettings.from_dict({"archive_purge_interval_minutes": 99999})
        self.assertLessEqual(settings.archive_purge_interval_minutes, 24 * 60)
        settings = AppSettings.from_dict({"archive_purge_interval_minutes": 0})
        self.assertGreaterEqual(settings.archive_purge_interval_minutes, 1)

    def test_legacy_switch_is_migrated(self) -> None:
        """老配置里的"启动时清理"要搬到新开关上，老用户的选择不能丢。"""
        self.assertFalse(
            AppSettings.from_dict({"purge_on_start": False}).archive_auto_purge
        )
        self.assertTrue(
            AppSettings.from_dict({"purge_on_start": True}).archive_auto_purge
        )

    def test_new_value_wins_over_legacy(self) -> None:
        settings = AppSettings.from_dict(
            {"purge_on_start": False, "archive_auto_purge": True}
        )
        self.assertTrue(settings.archive_auto_purge)

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            saved = AppSettings(archive_auto_purge=False)
            app_settings.save_settings(saved, base)
            self.assertFalse(app_settings.load_settings(base).archive_auto_purge)


class PurgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(os.path.join(self.tmp.name, "history.db"))
        self.storage.start()
        self.addCleanup(self.storage.stop)

    def _seed(self) -> None:
        now = time.time()
        self.storage.archive("d.p", 1.0, now - 100 * DAY)  # 很旧
        self.storage.archive("d.p", 2.0, now - 40 * DAY)  # 过 30 天
        self.storage.archive("d.p", 3.0, now - 1 * DAY)  # 新的
        self.storage.log("d", "info", "很旧的日志", now - 50 * DAY)
        self.storage.log("d", "info", "新日志", now)
        self.storage.add_waveform("d.w", [1.0, 2.0], 1000, now - 60 * DAY)
        self.storage.add_waveform("d.w", [3.0, 4.0], 1000, now)
        _wait_flush(self.storage, 3)

    def test_purge_keeps_recent_rows(self) -> None:
        self._seed()
        before = self.storage.stats()
        self.assertGreaterEqual(before["history"], 3)

        removed = self.storage.purge(30)
        self.assertGreater(removed, 0)

        after = self.storage.stats()
        self.assertEqual(after["history"], 1, "只该留下 1 天前那条")
        self.assertEqual(after["logs"], 1)
        self.assertEqual(after["waveforms"], 1)

    def test_purge_zero_days_does_nothing(self) -> None:
        """保留天数 <= 0 当作"不清理"，不能把库清空。"""
        self._seed()
        before = self.storage.stats()
        self.assertEqual(self.storage.purge(0), 0)
        self.assertEqual(self.storage.stats(), before)

    def test_purge_long_retention_keeps_everything(self) -> None:
        """把保留天数调大就是"留底"。"""
        self._seed()
        before = self.storage.stats()
        self.assertEqual(self.storage.purge(3650), 0)
        self.assertEqual(self.storage.stats(), before)


class WindowPurgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self) -> None:
        from icomm.core import project_store

        # 关窗口会把设置写回 base_dir，所以每个用例单独一个目录，
        # 否则前一个用例改过的开关会漏到后一个用例里
        self.base = tempfile.mkdtemp(prefix="purge-", dir=self.tmp.name)
        self.project = project_io.bess_project()

        paths = project_store.create_project_dir(
            os.path.join(self.base, "工程"), self.project
        )
        self.window = MainWindow(
            self.project, paths, workdir=self.base, base_dir=self.base
        )
        # 关掉退出确认：它会弹模态框，在无人值守的测试里会永久阻塞
        self.window.settings.confirm_on_exit = False
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        if self.window.engine is not None:
            self.window.engine.stop()
        if self.window.storage is not None:
            self.window.storage.stop()
        self.window.settings.confirm_on_exit = False
        self.window.close()

    def test_timer_runs_when_enabled(self) -> None:
        """默认就是打开的：装上就该自动清。"""
        self.assertTrue(self.window.settings.archive_auto_purge)
        self.assertTrue(self.window.purge_timer.isActive())
        expected = self.window.settings.archive_purge_interval_minutes * 60 * 1000
        self.assertEqual(self.window.purge_timer.interval(), expected)

    def test_timer_stops_when_disabled(self) -> None:
        """关掉开关立刻停，不用等下次启动。"""
        self.window.settings.archive_auto_purge = False
        self.window._sync_purge_timer()
        self.assertFalse(self.window.purge_timer.isActive())

    def test_startup_purge_drops_old_rows(self) -> None:
        """启动时按工程的保留天数清一次（顺带验证真的连上了存储）。"""
        self.window.project.history_retention_days = 1
        self.window.storage.archive("d.p", 1.0, time.time() - 10 * DAY)
        _wait_flush(self.window.storage, 1)
        self.window._auto_purge()
        self.assertEqual(self.window.storage.stats()["history"], 0)

    def test_disabled_switch_keeps_old_rows(self) -> None:
        """关掉开关就一条不删，哪怕已经很旧（想留底就关掉它）。"""
        self.window.settings.archive_auto_purge = False
        self.window.project.history_retention_days = 1
        self.window.storage.archive("d.p", 1.0, time.time() - 10 * DAY)
        _wait_flush(self.window.storage, 1)
        self.window._auto_purge()
        self.assertEqual(self.window.storage.stats()["history"], 1)

    def test_purge_failure_does_not_raise(self) -> None:
        """清理失败只记状态，不能把采集/界面带崩（库被占用是现场常见情况）。"""
        import sqlite3

        class Broken:
            def purge(self, _days):
                raise sqlite3.OperationalError("database is locked")

            def log(self, *_args, **_kwargs):
                return None

        real = self.window.storage
        self.window.storage = Broken()  # type: ignore[assignment]
        try:
            self.window._auto_purge()  # 不抛异常即通过
        finally:
            self.window.storage = real


if __name__ == "__main__":
    unittest.main()
