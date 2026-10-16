"""把内置示例工程导成 ``examples/*_parameters.csv``（点表参考文件）。

这些 CSV 是"示例的点表长什么样"的对外参考件，容易和代码里的示例走样
（改了量程/阈值忘了重导）。所以统一由这个脚本生成：

    python tools/make_sample_examples.py

它走的是软件自己的导出函数（`icomm.core.csv_table.export_csv`），
所以文件格式与「设备与点位 → 导出参数表」完全一致；
`tests/test_sample_examples.py` 会比对"文件 == 现在导出的内容"，
改过示例之后忘了重跑，测试就会红。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icomm.core import csv_table, project as project_io  # noqa: E402

EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples")

#: 示例工程名 -> 导出的文件名
SAMPLE_FILES: tuple[tuple[str, str], ...] = (
    ("储能电站（BESS）", "bess_parameters.csv"),
    ("光伏电站（PV）", "pv_parameters.csv"),
    ("电力系统（配电网）", "grid_parameters.csv"),
    ("SST 固态变压器", "sst_parameters.csv"),
    ("配电柜监控", "cabinet_parameters.csv"),
)


def sample_csv_names() -> list[str]:
    """示例工程导出的文件名（给测试用，免得两处各写一份）。"""
    return [name for _sample, name in SAMPLE_FILES]


def main() -> int:
    factories = project_io.BUILTIN_SAMPLES
    os.makedirs(EXAMPLES, exist_ok=True)
    missing = [sample for sample, _name in SAMPLE_FILES if sample not in factories]
    if missing:
        print(f"示例名对不上，BUILTIN_SAMPLES 里没有：{missing}")
        print(f"现有示例：{sorted(factories)}")
        return 1

    for sample, filename in SAMPLE_FILES:
        project = factories[sample]()
        path = os.path.join(EXAMPLES, filename)
        rows = csv_table.export_csv(project, path)
        points = sum(len(device.points) for device in project.devices)
        print(f"{filename}: {rows} 行（{sample}，{len(project.devices)} 台设备 / {points} 个点位）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
