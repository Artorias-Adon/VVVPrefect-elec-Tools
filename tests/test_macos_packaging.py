"""macOS 打包相关：.icns 图标、.app/dmg 构建配置、跨平台打开文件、数据目录。

这些都不依赖 macOS 就能验证：
* ``assets/icon.icns`` 是我们自己按 ICNS 格式拼的（不用 macOS 的 iconutil），能解析回来；
* ``VVPhmi.spec`` 里有 macOS 的 BUNDLE 段（且不能在 Windows 上误触发）；
* GitHub Actions 的 macOS 工作流存在、调的是对的命令（hdiutil / gh release upload）；
* ``shell.open_path`` 在 macOS 上走 ``open``，在 Windows 上走 ``os.startfile``；
* ``main.app_base_dir()`` 在 macOS 的 .app 里会把用户数据放到 Application Support。
"""

from __future__ import annotations

import os
import struct
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ICNS = ROOT / "assets" / "icon.icns"
SPEC = ROOT / "VVPhmi.spec"
WORKFLOW = ROOT / ".github" / "workflows" / "macos-dmg.yml"
REQUIREMENTS = ROOT / "requirements.txt"


class IcnsTest(unittest.TestCase):
    def test_file_exists_and_header(self) -> None:
        self.assertTrue(ICNS.is_file(), "缺少 assets/icon.icns（macOS 图标）")
        blob = ICNS.read_bytes()
        self.assertEqual(blob[:4], b"icns")
        total = struct.unpack(">I", blob[4:8])[0]
        self.assertEqual(total, len(blob), "icns 头里的总长要与文件长度一致")

    def test_members_are_png_with_expected_sizes(self) -> None:
        blob = ICNS.read_bytes()
        position = 8
        found: dict[str, int] = {}
        while position + 8 <= len(blob):
            kind = blob[position:position + 4].decode("ascii")
            length = struct.unpack(">I", blob[position + 4:position + 8])[0]
            payload = blob[position + 8:position + 8 + length - 8]
            self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n", f"{kind} 不是 PNG")
            width, height = struct.unpack(">II", payload[16:24])
            self.assertEqual(width, height, f"{kind} 不是正方形")
            found[kind] = width
            position += length
        self.assertEqual(position, len(blob), "成员长度累加应正好等于文件长度")
        # macOS 需要 128/256/512/1024 这几档，Retina 的 32pt 用 ic11
        self.assertEqual(found.get("ic07"), 128)
        self.assertEqual(found.get("ic08"), 256)
        self.assertEqual(found.get("ic09"), 512)
        self.assertEqual(found.get("ic10"), 1024)
        self.assertEqual(found.get("ic11"), 64)

    def test_png_sources_exist(self) -> None:
        for size in (16, 32, 64, 128, 256, 512, 1024):
            with self.subTest(size=size):
                self.assertTrue((ROOT / "assets" / f"icon_{size}.png").is_file())


class SpecTest(unittest.TestCase):
    def test_bundle_for_macos_only(self) -> None:
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn("BUNDLE(", text, "spec 里要有 macOS 的 .app 包定义")
        self.assertIn('sys.platform == "darwin"', text, "BUNDLE 只在 macOS 上生效")
        self.assertIn("icon.icns", text)
        self.assertIn('"CFBundleShortVersionString": APP_VERSION', text, "版本号要进 Info.plist")

    def test_windows_icon_only_on_windows(self) -> None:
        text = SPEC.read_text(encoding="utf-8")
        self.assertIn('_exe_icon = "assets/icon.ico" if os.name == "nt" else None', text)

    def test_platform_aware_binary_filter(self) -> None:
        """剔除 Qt Qml/Quick 的正则要认三个平台的库名（否则 macOS 包会胖一圈）。"""
        import re

        namespace: dict[str, object] = {"re": re}
        text = SPEC.read_text(encoding="utf-8")
        start = text.index("_DROP_MODULES")
        end = text.index("a.binaries =")
        code = text[start:end]
        exec(compile(code, "VVPhmi.spec", "exec"), namespace)  # noqa: S102
        pattern = namespace["_DROP_BINARY"]
        assert isinstance(pattern, re.Pattern)
        dropped = [
            r"PySide6\Qt6Qml.dll",
            "PySide6/Qt/lib/libQt6Qml.6.dylib",
            "PySide6/Qt/lib/libQt6Quick.so.6",
            "PySide6/Qt/lib/QtQml.framework/Versions/A/QtQml",
            "PySide6/Qt6Multimedia.dll",
            "opengl32sw.dll",
        ]
        kept = [
            "PySide6\\Qt6Core.dll",
            "PySide6/Qt/lib/libQt6Core.6.dylib",
            "PySide6/Qt/plugins/imageformats/qjpeg.dll",
            "icomm/ui/main_window.py",
        ]
        for path in dropped:
            with self.subTest(drop=path):
                self.assertTrue(pattern.search(path), f"{path} 应该被剔除")
        for path in kept:
            with self.subTest(keep=path):
                self.assertFalse(pattern.search(path), f"{path} 不该被删")


class WorkflowTest(unittest.TestCase):
    def test_workflow_exists_and_targets_releases(self) -> None:
        self.assertTrue(WORKFLOW.is_file(), "缺少 macOS 打包工作流")
        text = WORKFLOW.read_text(encoding="utf-8")
        for token in (
            "macos-15-intel",
            "macos-14",
            "hdiutil create",
            "gh release upload",
            'tags: ["v*"]',
            "workflow_dispatch",
            "contents: write",
        ):
            with self.subTest(token=token):
                self.assertIn(token, text)

    def test_requirements_skip_windows_only_deps_on_macos(self) -> None:
        text = REQUIREMENTS.read_text(encoding="utf-8")
        iec_line = [line for line in text.splitlines() if line.startswith("iec61850")]
        self.assertTrue(iec_line, "requirements 里应有 iec61850")
        self.assertIn('sys_platform == "win32"', iec_line[0], "它在 macOS/Linux 上装不了，要加平台标记")


class WorkflowYamlTest(unittest.TestCase):
    """工作流的 YAML 必须真的能被解析。

    踩过的坑：``run: |`` 里塞 heredoc 且正文顶格写，会把整个 YAML 弄坏 ——
    GitHub 于是认为这个工作流"没有任何触发条件"：推 tag 不跑、``workflow_dispatch``
    API 也报 422，界面里连 name 都显示成文件路径。
    """

    def _load(self, path: Path):
        try:
            import yaml
        except ImportError:  # pragma: no cover - 本地/CI 都可能没装
            self.skipTest("没装 PyYAML（本地想校验就先 pip install pyyaml）")
        return yaml.safe_load(path.read_text(encoding="utf-8"))

    @staticmethod
    def _triggers(data: dict):
        # YAML 1.1 里裸写的 on: 会被解析成布尔键 True
        return data.get("on", data.get(True))

    def test_macos_workflow_triggers(self) -> None:
        data = self._load(WORKFLOW)
        triggers = self._triggers(data)
        self.assertIsInstance(triggers, dict)
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(triggers["push"]["tags"], ["v*"])
        self.assertTrue(data.get("name"), "工作流要有 name，否则界面里显示成文件路径")

    def test_macos_workflow_matrix(self) -> None:
        data = self._load(WORKFLOW)
        job = data["jobs"]["dmg"]
        runners = [item["runner"] for item in job["strategy"]["matrix"]["include"]]
        self.assertEqual(runners, ["macos-15-intel", "macos-14"])
        arches = [item["arch"] for item in job["strategy"]["matrix"]["include"]]
        self.assertEqual(arches, ["x86_64", "arm64"])

    def test_workflow_avoids_retired_runners(self) -> None:
        """macos-13 已退役：写上去只会一直排队，不会报错，特别坑。"""
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("runner: macos-13", text)

    def test_existing_test_workflow_still_parses(self) -> None:
        data = self._load(ROOT / ".github" / "workflows" / "tests.yml")
        self.assertIn("test", data["jobs"])

    def test_install_notes_shipped(self) -> None:
        notes = (ROOT / "packaging" / "macos" / "install-notes.txt").read_text(encoding="utf-8")
        self.assertIn("com.apple.quarantine", notes, "未签名版本要告诉用户怎么绕过 Gatekeeper")
        self.assertIn("Application Support", notes)


class ShellOpenTest(unittest.TestCase):
    def setUp(self) -> None:
        from icomm.core import shell

        self.shell = shell
        self.tmp = Path(self.enterContext(_tempdir()))

    def test_command_for_platforms(self) -> None:
        with mock.patch.object(sys, "platform", "darwin"), mock.patch.object(os, "name", "posix"):
            self.assertEqual(self.shell.command_for("/tmp/x"), ["open", "/tmp/x"])
        with mock.patch.object(sys, "platform", "linux"), mock.patch.object(os, "name", "posix"):
            self.assertEqual(self.shell.command_for("/tmp/x"), ["xdg-open", "/tmp/x"])
        with mock.patch.object(os, "name", "nt"):
            self.assertIsNone(self.shell.command_for("C:/x"))

    def test_open_path_uses_open_on_macos(self) -> None:
        target = self.tmp / "a.txt"
        target.write_text("x", encoding="utf-8")
        with mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(os, "name", "posix"), \
                mock.patch("subprocess.Popen") as popen:
            self.shell.open_path(str(target))
        self.assertEqual(popen.call_args[0][0], ["open", str(target)])

    def test_open_path_rejects_missing(self) -> None:
        with self.assertRaises(OSError):
            self.shell.open_path(str(self.tmp / "没有这个"))

    def test_open_path_rejects_empty(self) -> None:
        with self.assertRaises(OSError):
            self.shell.open_path("")


class BaseDirTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows 路径规则只在 Windows 上成立")
    def test_windows_uses_exe_dir(self) -> None:
        import main

        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", r"C:\Apps\VVPhmi\VVPhmi.exe"), \
                mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(main.app_base_dir(), r"C:\Apps\VVPhmi")

    def test_macos_app_uses_application_support(self) -> None:
        import main

        home = Path(self.enterContext(_tempdir()))
        exe = "/Applications/VVPhmi.app/Contents/MacOS/VVPhmi"
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(sys, "executable", exe), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch("os.path.expanduser", lambda path: str(home) if path == "~" else path):
            base = main.app_base_dir()
        self.assertEqual(base, str(home / "Library" / "Application Support" / "VVPhmi"))
        self.assertTrue(os.path.isdir(base), "目录应被创建出来")

    def test_source_run_uses_file_dir(self) -> None:
        import main

        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(main.app_base_dir(), os.path.dirname(os.path.abspath(main.__file__)))


def _tempdir():
    import tempfile

    return tempfile.TemporaryDirectory(ignore_cleanup_errors=True)


if __name__ == "__main__":
    unittest.main()
