"""开源相关文件的自检：许可、第三方清单、贡献者声明、发布包内容。

这些文件是"对外承诺"，最容易在后续迭代里被悄悄改坏：
* LICENSE 被换成别的协议、或漏掉 GPL 全文；
* `THIRD-PARTY-LICENSES.md` 少写了新加的依赖（依赖许可写漏，分发时就是风险）；
* 发布包里忘了带许可文本（GPL / LGPL 都要求再分发时带上）；
* 预设主题名里又混进第三方品牌名。

所以这里把它们钉成测试：改错了测试就红。
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
import zipfile

from icomm import __copyright__, __license__, __version__
from icomm.ui import theme

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


class LicenseFileTest(unittest.TestCase):
    def test_license_is_gpl3(self):
        text = read("LICENSE")
        self.assertIn("GNU GENERAL PUBLIC LICENSE", text)
        self.assertIn("Version 3, 29 June 2007", text)
        self.assertIn("END OF TERMS AND CONDITIONS", text)
        self.assertGreater(len(text), 30_000, "应该就是 GPLv3 全文")
        self.assertNotIn("\r\n", text, "许可文本按 LF 存，免得各处 diff 噪音")

    def test_module_metadata_matches_license_file(self):
        self.assertEqual(__license__, "GPL-3.0-or-later")
        self.assertIn("Artorias-Adon", __copyright__)
        # 版权年份跟仓库里其他地方保持一致
        self.assertRegex(__copyright__, r"Copyright \(C\) 20\d\d ")

    def test_readme_states_license_and_disclaimer(self):
        text = read("README.md")
        self.assertIn("GPL-3.0-or-later", text)
        self.assertIn("LICENSE", text)
        self.assertIn("## 安全与免责", text, "开源项目要有一段醒目的免责声明")
        self.assertIn("个人开源项目", text, "归属声明：不是公司产品")
        self.assertIn(__version__, text, "README 顶部的版本号要跟上")

    def test_contributing_keeps_relicensing_option(self):
        text = read("CONTRIBUTING.md")
        self.assertIn("GPL-3.0-or-later", text)
        self.assertIn("再授权", text, "保住双授权/换协议的口子，否则以后没法改")
        self.assertIn("保密", text, "要明确谢绝公司/客户的保密材料")


class ThirdPartyTest(unittest.TestCase):
    """依赖清单要和 requirements.txt 对得上。"""

    def test_every_dependency_is_listed(self):
        listed = read("THIRD-PARTY-LICENSES.md")
        requirements = read("requirements.txt")
        names = []
        for line in requirements.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            names.append(re.split(r"[<>=!~]", line)[0].strip())
        self.assertTrue(names, "requirements.txt 不该是空的")
        for name in names:
            with self.subTest(requirement=name):
                self.assertIn(name, listed, f"{name} 没写进第三方许可清单")
        self.assertTrue(names)

    def test_readme_points_at_the_license_files(self):
        text = read("README.md")
        self.assertIn("THIRD-PARTY-LICENSES.md", text)
        self.assertIn("CONTRIBUTING.md", text)

    def test_lgpl_components_documented(self):
        """LGPL 的组件要写清"以独立文件分发、可替换"，这是它的合规要求。"""
        listed = read("THIRD-PARTY-LICENSES.md")
        for name in ("PySide6", "python-can"):
            with self.subTest(package=name):
                self.assertIn(name, listed)
        self.assertIn("LGPL-3.0", listed)
        self.assertIn("可替换", listed)

    def test_pyinstaller_exception_mentioned(self):
        self.assertIn("bootloader", read("THIRD-PARTY-LICENSES.md").lower())


class ReleaseZipTest(unittest.TestCase):
    """发布包：排掉本机产物、带上许可文本。"""

    def test_zip_contents(self):
        from tools import make_release_zip

        with tempfile.TemporaryDirectory() as tmp:
            dist = os.path.join(tmp, "VVPhmi")
            internal = os.path.join(dist, "_internal")
            os.makedirs(internal)
            for name in ("VVPhmi.exe", "settings.json"):
                with open(os.path.join(dist, name), "w", encoding="utf-8") as handle:
                    handle.write("x")
            with open(os.path.join(dist, "run.log"), "w", encoding="utf-8") as handle:
                handle.write("x")
            with open(os.path.join(internal, "lib.dll"), "w", encoding="utf-8") as handle:
                handle.write("x")
            target = os.path.join(tmp, "out.zip")
            count, dropped = make_release_zip.make_zip(dist, target)

            with zipfile.ZipFile(target) as archive:
                names = set(archive.namelist())
            self.assertIn("VVPhmi.exe", names)
            self.assertIn("_internal/lib.dll", names)
            self.assertNotIn("settings.json", names, "本机偏好不能发出去")
            self.assertNotIn("run.log", names)
            self.assertIn("settings.json", dropped)
            self.assertIn("LICENSE", names, "GPL 要求再分发时带上许可文本")
            self.assertIn("THIRD-PARTY-LICENSES.md", names)
            self.assertEqual(count, len(names))


class ThemeNamingTest(unittest.TestCase):
    """开源合规：预设名与文档里都不该留第三方品牌。"""

    BRANDS = ("DeepSeek", "deepseek", "OpenAI", "ChatGPT")

    def test_preset_names_are_neutral(self):
        for name in theme.preset_names():
            with self.subTest(preset=name):
                for brand in self.BRANDS:
                    self.assertNotIn(brand, name)

    def test_docs_do_not_pretend_affiliation(self):
        """README 里提到第三方名称时必须带"无关联"这类说明。"""
        text = read("README.md")
        self.assertIn("没有关联", text)
        self.assertIn("商标", text)

    def test_old_preset_names_still_resolve(self):
        """兼容映射可以留着旧名字，但必须能生效（老设置不至于选不中主题）。"""
        for old, new in theme.PRESET_ALIASES.items():
            with self.subTest(old=old):
                self.assertNotIn(old, theme.PRESETS)
                self.assertIn(new, theme.PRESETS)
                self.assertEqual(theme.resolve_preset(old), new)


if __name__ == "__main__":
    unittest.main()
