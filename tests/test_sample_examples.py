"""`examples/*_parameters.csv` 必须与内置示例工程**同步**。

这些 CSV 是"示例点表长什么样"的对外参考件（也常被当成模板改）。
它和代码里的示例很容易走样：改了量程/阈值/点位却忘了重导，
别人照 CSV 配出来的东西和软件里的示例对不上 —— 很难发现，但很烦。

所以这里直接比对"磁盘上的文件 == 现在导出的内容"：
改过示例之后跑一次 ``python tools/make_sample_examples.py`` 就能变绿。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from icomm.core import csv_table, project as project_io
from tools.make_sample_examples import SAMPLE_FILES, EXAMPLES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SampleExampleCsvTest(unittest.TestCase):
    def test_files_exist(self):
        for _sample, filename in SAMPLE_FILES:
            with self.subTest(file=filename):
                self.assertTrue(os.path.exists(os.path.join(EXAMPLES, filename)), filename)

    def test_files_match_current_samples(self):
        factories = project_io.BUILTIN_SAMPLES
        with tempfile.TemporaryDirectory() as tmp:
            for sample, filename in SAMPLE_FILES:
                with self.subTest(file=filename):
                    project = factories[sample]()
                    fresh = os.path.join(tmp, filename)
                    csv_table.export_csv(project, fresh)
                    on_disk = os.path.join(EXAMPLES, filename)
                    with open(fresh, encoding="utf-8-sig") as handle:
                        expected = handle.read()
                    with open(on_disk, encoding="utf-8-sig") as handle:
                        actual = handle.read()
                    self.assertEqual(
                        actual,
                        expected,
                        f"{filename} 与示例「{sample}」不同步，"
                        "请重跑 python tools/make_sample_examples.py",
                    )

    def test_csv_lists_every_sample(self):
        """示例菜单里的每个工程都要有对应的 CSV，别只做了几个。"""
        expected = {sample for sample, _name in SAMPLE_FILES}
        self.assertEqual(expected, set(project_io.BUILTIN_SAMPLES))

    def test_no_client_data_markers(self):
        """示例 CSV 里不该出现"公司/客户/保密/NDA"这类字样（开源合规守门）。"""
        banned = ("有限公司", "股份", "客户名", "保密", "NDA", "内部资料", "机密")
        for _sample, filename in SAMPLE_FILES:
            text = open(os.path.join(EXAMPLES, filename), encoding="utf-8-sig").read()
            for word in banned:
                with self.subTest(file=filename, word=word):
                    self.assertNotIn(word, text)


if __name__ == "__main__":
    unittest.main()
