"""把几份协议点表模板落成真正的工程目录，放进工程仓库（VVPproject）。

这样装好软件以后不用手配：从「文件 → 打开工程」里直接能看到
「CAN 储能变流器」「IEC 61850 变电站」「S7 储能变流器」，点「内置仿真器」就能出数据
—— 通道的协议/参数都在工程里配好了，而 CSV 点表只带点位（这是 CSV 格式的本分）。

文件夹名固定用英文（``CAN`` / ``IEC-61850`` / ``Siemens-S7``）：工程仓库里混着中文目录名，
在别的机器 / 命令行里看着乱，现场同事也不好记。已存在的目录默认不动，
要按模板重刷配置就加 ``--force``（会覆盖该工程的 project.json 与点表）。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icomm.core import project_store  # noqa: E402
from tools.make_protocol_examples import (  # noqa: E402
    can_project,
    iec61850_project,
    s7_project,
)

#: (工程仓库下的文件夹名, 生成函数, 说明)
EXAMPLES = [
    ("CAN", can_project, "CAN 储能变流器（虚拟 CAN 总线）"),
    ("IEC-61850", iec61850_project, "IEC 61850 变电站（内置 MMS 仿真装置）"),
    ("Siemens-S7", s7_project, "S7 储能变流器（内置 S7 仿真 PLC）"),
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成协议示例工程")
    parser.add_argument(
        "--force",
        action="store_true",
        help="已存在的工程目录也按模板重刷配置与点表（默认保留现场改动）",
    )
    parser.add_argument("--home", default="", help="工程仓库位置，默认自动查找 VVPproject")
    args = parser.parse_args(argv)

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = args.home or project_store.discover_project_home(base)[0]
    print(f"工程仓库：{home}")
    os.makedirs(home, exist_ok=True)

    for folder, factory, note in EXAMPLES:
        root = os.path.join(home, folder)
        project = factory()
        existed = os.path.exists(os.path.join(root, "config", "project.json"))
        if existed and not args.force:
            print(f"  已存在，跳过：{root}（{note}）")
            continue
        paths = project_store.create_project_dir(root, project)
        count = sum(len(device.points) for device in project.devices)
        print(f"  {'已重刷' if existed else '已新建'}：{paths.root}（{note}，{count} 个点位）")
        print(f"    配置文件：{paths.project_file}")
        print(f"    参数表　：{paths.csv_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
