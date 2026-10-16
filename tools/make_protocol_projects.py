"""把两份协议点表模板落成真正的工程目录，放进工程仓库（VVPproject）。

这样装好软件以后不用手配：从「文件 → 打开工程」里直接能看到
「CAN 储能变流器」「IEC 61850 变电站」，点「内置仿真器」就能出数据 ——
通道的协议/参数都在工程里配好了，而 CSV 点表只带点位（这是 CSV 格式的本分）。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icomm.core import project_store  # noqa: E402
from tools.make_protocol_examples import can_project, iec61850_project  # noqa: E402


def main() -> int:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home, note = project_store.discover_project_home(base)
    print(f"工程仓库：{home}（{note}）")
    os.makedirs(home, exist_ok=True)

    for project in (can_project(), iec61850_project()):
        folder = project_store.unique_folder_name(home, project_store.project_folder_name(project))
        root = os.path.join(home, folder)
        paths = project_store.create_project_dir(root, project)
        print(f"  已建工程：{paths.root}")
        print(f"    配置文件：{paths.project_file}")
        print(f"    参数表　：{paths.csv_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
