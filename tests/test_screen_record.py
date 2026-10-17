"""屏幕录制：工具注册位置，以及 MJPEG-AVI 的写出与回读。

用户要求："截屏辅助下面添加录制功能"。
这里盯住：菜单位置紧跟截屏辅助、录出来的 AVI 结构合法（能回读出帧数与尺寸）、
开始/停止/自动停/关窗都收得住、完成会发通知。
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from icomm.core import project as project_io  # noqa: E402
from icomm.core import screenrec  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.ui import tools as ui_tools  # noqa: E402
from icomm.ui.tools.screen_record import ScreenRecordWindow, jpeg_bytes  # noqa: E402

_app: QApplication | None = None


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def make_jpeg(width: int = 160, height: int = 120, shade: int = 0x33) -> bytes:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(0x102030 + shade)
    return jpeg_bytes(image)


class AviWriterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "rec.avi")

    def test_writes_valid_riff_header(self) -> None:
        writer = screenrec.AviMjpegWriter(self.path, 160, 120, fps=10)
        for index in range(4):
            writer.add_frame(make_jpeg(shade=index))
        writer.close()

        info = screenrec.probe_avi(self.path)
        self.assertEqual(info["frames"], 4)
        self.assertEqual((info["width"], info["height"]), (160, 120))
        self.assertEqual(info["fps"], 10)
        self.assertTrue(info["has_index"])
        self.assertEqual(info["riff_size"], info["file_size"] - 8, "RIFF 长度要等于文件长度减 8")

    def test_frames_are_real_jpegs(self) -> None:
        writer = screenrec.AviMjpegWriter(self.path, 64, 64, fps=5)
        payload = make_jpeg(64, 64)
        writer.add_frame(payload)
        writer.close()
        with open(self.path, "rb") as fh:
            data = fh.read()
        start = data.find(payload)
        self.assertGreater(start, 0, "帧数据应当原样写进文件")
        self.assertTrue(payload.startswith(b"\xff\xd8"), "JPEG 以 FFD8 开头")
        self.assertTrue(payload.endswith(b"\xff\xd9"), "JPEG 以 FFD9 结尾")

    def test_index_entries_point_at_chunks(self) -> None:
        writer = screenrec.AviMjpegWriter(self.path, 64, 48, fps=8)
        for _index in range(3):
            writer.add_frame(make_jpeg(64, 48))
        writer.close()

        with open(self.path, "rb") as fh:
            data = fh.read()
        movi = data.find(b"movi")
        self.assertGreater(movi, 0)
        idx1 = data.find(b"idx1")
        self.assertGreater(idx1, movi)
        count = struct.unpack_from("<I", data, idx1 + 4)[0] // 16
        self.assertEqual(count, 3)
        for index in range(count):
            _tag, _flags, offset, size = struct.unpack_from(
                "<4sIII", data, idx1 + 8 + index * 16
            )
            position = movi + offset
            self.assertEqual(data[position:position + 4], b"00dc")
            self.assertEqual(struct.unpack_from("<I", data, position + 4)[0], size)

    def test_odd_size_is_evened(self) -> None:
        writer = screenrec.AviMjpegWriter(self.path, 161, 121, fps=10)
        writer.add_frame(make_jpeg(160, 120))
        writer.close()
        info = screenrec.probe_avi(self.path)
        self.assertEqual((info["width"], info["height"]), (160, 120))

    def test_empty_frame_rejected(self) -> None:
        writer = screenrec.AviMjpegWriter(self.path, 32, 32, fps=10)
        with self.assertRaises(ValueError):
            writer.add_frame(b"")
        writer.close()

    def test_duration_and_counts(self) -> None:
        writer = screenrec.AviMjpegWriter(self.path, 32, 32, fps=10)
        for _index in range(20):
            writer.add_frame(make_jpeg(32, 32))
        self.assertEqual(writer.frames, 20)
        self.assertAlmostEqual(writer.duration, 2.0)
        writer.close()


class ScreenRecordToolTest(unittest.TestCase):
    def test_package_import_registers_tool(self) -> None:
        """只有 ``icomm/ui/tools/__init__.py`` 里 import 过，工具才会出现在菜单里。

        本文件为了测窗口会直接 import 子模块，那会让 ``@register_tool`` 顺带执行，
        所以这条必须另开进程、只 import 包本身，才算真的验证了注册。
        """
        code = (
            "import sys\n"
            "from icomm.ui import tools\n"
            "mods = [type(t).__module__ for t in tools.tools()]\n"
            "ok = 'icomm.ui.tools.screen_record' in mods\n"
            "print(sorted(mods))\n"
            "sys.exit(0 if ok else 1)\n"
        )
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONIOENCODING="utf-8")
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=root, env=env, capture_output=True
        )
        self.assertEqual(
            result.returncode, 0,
            "屏幕录制没在 tools/__init__.py 里 import：\n"
            + result.stdout.decode("utf-8", "replace")
            + result.stderr.decode("utf-8", "replace"),
        )

    def test_tool_sits_right_after_screenshot(self) -> None:
        names = [tool.name for tool in ui_tools.tools_by_group()["自动化"]]
        self.assertIn("屏幕录制", names)
        self.assertLess(names.index("截屏辅助"), names.index("屏幕录制"))
        self.assertLess(
            names.index("屏幕录制"), names.index("故障录波"), "要紧跟在截屏辅助后面"
        )

    def test_tool_is_non_modal_window(self) -> None:
        tool = ui_tools.find_tool("屏幕录制")
        self.assertIsNotNone(tool)
        self.assertTrue(tool.non_modal)


class ScreenRecordWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.announced: list[tuple[str, str, str]] = []
        self.context = ui_tools.ToolContext(
            project=project_io.bess_project(),
            window=None,
            settings=AppSettings(),
            report_dir=self.tmp.name,
            notify=lambda level, title, text="": self.announced.append((level, title, text)),
        )
        self.window = ScreenRecordWindow(self.context)
        self.addCleanup(self.window.deleteLater)
        # 不碰真屏幕：每次抓帧给一张固定尺寸的假图
        self.window._capture_image = lambda: self._fake_image()
        self.window.dir_edit.setText(self.tmp.name)

    def _fake_image(self) -> QImage:
        image = QImage(200, 150, QImage.Format_RGB32)
        image.fill(0x204060)
        return image

    def test_start_and_stop_writes_avi(self) -> None:
        self.assertTrue(self.window.recording() is False)
        self.assertTrue(self.window.start())
        self.assertTrue(self.window.recording())
        self.assertTrue(self.window.stop_button.isEnabled())
        self.assertFalse(self.window.start_button.isEnabled(), "录制中不能再点开始")

        for _index in range(3):
            self.window._on_tick()
        path = self.window.stop("测试停止")
        self.assertTrue(os.path.isfile(path), path)
        info = screenrec.probe_avi(path)
        self.assertGreaterEqual(info["frames"], 4, "第一帧 + 3 次 tick")
        self.assertEqual((info["width"], info["height"]), (200, 150))
        self.assertFalse(self.window.recording())
        self.assertTrue(self.window.start_button.isEnabled())

    def test_stop_announces_completion(self) -> None:
        self.window.start()
        self.window.stop("测试")
        self.assertTrue(self.announced, "录完要在右上角通知一声")
        level, title, text = self.announced[-1]
        self.assertEqual(level, "info")
        self.assertEqual(title, "录制完成")
        self.assertIn(".avi", text)

    def test_stop_without_recording_is_noop(self) -> None:
        self.assertEqual(self.window.stop("没事"), "")
        self.assertEqual(self.announced, [])

    def test_close_while_recording_saves_first(self) -> None:
        self.window.start()
        path = self.window.output_path()
        self.window.close()
        self.assertTrue(os.path.isfile(path), "关窗口前要把文件收好")
        self.assertFalse(self.window.recording())

    def test_max_duration_stops_automatically(self) -> None:
        self.window.limit_box.setValue(1)
        self.window.start()
        self.window._started_at -= 61  # 假装已经录了 61 秒
        self.window._on_tick()
        self.assertFalse(self.window.recording(), "超过最长时长要自动停")
        self.assertIn("最长时长", self.window.status.text())

    def test_start_fails_clearly_when_no_screen(self) -> None:
        self.window._capture_image = lambda: QImage()
        self.assertFalse(self.window.start())
        self.assertIn("抓不到画面", self.window.status.text())
        self.assertFalse(self.window.recording())

    def test_settings_widgets_locked_while_recording(self) -> None:
        self.window.start()
        self.assertFalse(self.window.fps_combo.isEnabled())
        self.assertFalse(self.window.dir_edit.isEnabled())
        self.window.stop("测试")
        self.assertTrue(self.window.fps_combo.isEnabled())


if __name__ == "__main__":
    unittest.main()
