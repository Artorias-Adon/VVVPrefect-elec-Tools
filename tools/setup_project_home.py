"""准备工程仓库（VVPproject）：建示例工程 / 把老工程搬进来整理好。

软件目录只放软件，工程统一放在工程仓库里，**目录名一律英文**（界面里显示的中文工程名照旧）::

    VVPproject/
      sst-transformer/            文件夹名英文
        config/     project.json + parameters.csv + diagram.svg
        data/       history.db
        reports/    rec / shots / audio / logs 等导出的测试结果

用法::

    python tools/setup_project_home.py                       # 自动找仓库，建内置示例
    python tools/setup_project_home.py --list                # 只看看现在有什么
    python tools/setup_project_home.py --home D:/VVPproject  # 指定仓库位置
    python tools/setup_project_home.py --samples "储能电站（BESS）" "光伏电站（PV）"
    python tools/setup_project_home.py --migrate-old <目录>  # 把老布局的工程搬进来并整理
    python tools/setup_project_home.py --english-names       # 把仓库里中文文件夹名改成英文
    python tools/setup_project_home.py --archive <目录>      # 把跑测试留下的数据挪进 _archive

默认会建 **储能 / 光伏 / 电力系统 / SST** 四个示例，
已经存在的工程默认**不覆盖**（要覆盖加 ``--force``）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.storage.db import Storage  # noqa: E402

#: 默认准备的示例（储能 / 光伏 / 电力系统 / SST 四个内置例程）
DEFAULT_SAMPLES = (
    "储能电站（BESS）",
    "光伏电站（PV）",
    "电力系统（配电网）",
    "SST 固态变压器",
)

#: 跑测试 / 跑探针留下的数据，搬进仓库的 _archive 而不是留在软件目录里
ARCHIVE_DIR = "_archive"

#: 认识这些老名字，按里面的工程名给新文件夹起对的英文名
_LEGACY_FOLDER_HINTS = {
    "示例工程 - 配电柜监控": "cabinet-monitor",
    "配电柜监控": "cabinet-monitor",
    "SST 固态变压器": "sst-transformer",
    "储能电站（BESS）": "bess-storage",
    "光伏电站（PV）": "pv-plant",
    "电力系统（配电网）": "power-grid",
}


def repo_root() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_history_db(path: str) -> bool:
    """把历史库建出来（表结构建好，空库），有就不动。返回是否新建。"""
    if os.path.exists(path):
        return False
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    storage = Storage(path)
    try:
        storage.start()
    finally:
        storage.stop()
    return True


def make_sample(home: str, name: str, force: bool = False) -> str:
    """在仓库里准备一个内置示例工程，返回状态说明。"""
    builder = project_io.BUILTIN_SAMPLES.get(name)
    if builder is None:
        return f"跳过（没有这个内置示例）：{name}"

    project = builder()
    wanted = project_store.project_folder_name(project)
    root = os.path.join(home, wanted)
    config_dir, data_dir, report_dir = project_store.dirs_for(root, "organized")
    project_file = os.path.join(config_dir, project_store.PROJECT_NAME)

    if project_store.find_project_in(root):
        if not force:
            created = ensure_history_db(os.path.join(data_dir, project_store.DB_NAME))
            _make_report_dirs(report_dir)
            return f"已存在，未改动：{wanted}/" + ("（补建了历史库）" if created else "")
        shutil.rmtree(root, ignore_errors=True)
    else:
        # 老版本用过中文文件夹名：同名老目录还在就搬过来，别重新建一份
        legacy = os.path.join(home, project_store.safe_name(name))
        if legacy != root and project_store.find_project_in(legacy):
            moved, note = rename_existing(legacy, wanted, home)
            if moved:
                ensure_history_db(os.path.join(moved, project_store.DATA_DIR, project_store.DB_NAME))
                return f"老文件夹改名：{os.path.basename(legacy)} → {os.path.basename(moved)}"

    root = os.path.join(home, project_store.suggest_folder_name(home, project))
    project_store.create_project_dir(root, project)
    ensure_history_db(os.path.join(root, project_store.DATA_DIR, project_store.DB_NAME))
    _make_report_dirs(os.path.join(root, project_store.REPORT_DIR))
    return f"已新建：{os.path.basename(root)}/（{len(project.all_point_ids())} 个点位）"


def _make_report_dirs(report_dir: str) -> None:
    """在 reports/ 下按分类建好目录（导出的测试结果都归到这里）。"""
    for key in project_store.REPORT_KINDS:
        try:
            os.makedirs(os.path.join(report_dir, key), exist_ok=True)
        except OSError:
            pass


def rename_existing(folder: str, wanted: str, home: str) -> tuple[str, str]:
    """把一个已有的工程文件夹改成英文名（改不动就原样保留）。"""
    target = os.path.join(home, wanted)
    if os.path.normcase(target) == os.path.normcase(folder):
        return folder, "名字没变"
    if os.path.exists(target):
        return folder, f"已有 {wanted}/，跳过"
    try:
        os.replace(folder, target)
    except OSError as exc:
        return folder, f"改名失败（{exc}）"
    return target, f"{os.path.basename(folder)} → {wanted}"


def stamp_folder_name(folder: str, name: str) -> None:
    """把英文文件夹名写进工程文件，下次就不用再猜了。"""
    project_file = project_store.find_project_in(folder)
    if not project_file:
        return
    try:
        project = project_store._load_project_file(project_file)
        project.folder_name = name
        with open(project_file, "w", encoding="utf-8") as fh:
            import json

            json.dump(
                {"version": project_store.FORMAT_VERSION, **project.to_dict()},
                fh,
                ensure_ascii=False,
                indent=2,
            )
    except (OSError, ValueError):
        pass


def english_names(home: str) -> list[str]:
    """把仓库里所有中文（或含非 ASCII）的工程文件夹名改成英文。"""
    lines: list[str] = []
    try:
        names = sorted(os.listdir(home))
    except OSError as exc:
        return [f"读不了仓库：{exc}"]

    for name in names:
        if name.startswith((".", "_")):
            continue
        folder = os.path.join(home, name)
        if not os.path.isdir(folder):
            continue
        project_file = project_store.find_project_in(folder)
        if not project_file:
            continue
        if name.isascii():
            project_store.upgrade_layout(folder)  # 名字已是英文，只顺手把子目录也英文化
            lines.append(f"已是英文名：{name}/")
            continue

        # 起名优先级：老名字的对照表（最准）→ 工程自己记的英文名 → 按工程名推
        wanted = _LEGACY_FOLDER_HINTS.get(name, "")
        if not wanted:
            try:
                wanted = project_store.project_folder_name(
                    project_store._load_project_file(project_file)
                )
            except Exception:  # noqa: BLE001 - 工程文件坏了也不该让整个改名跑不动
                wanted = ""
        wanted = project_store.folder_name(wanted, fallback="")
        if not wanted or wanted == "project":
            wanted = project_store.folder_name(name, fallback="") or "project"
        wanted = project_store.unique_folder_name(home, wanted, ignore=folder)

        moved, note = rename_existing(folder, wanted, home)
        if moved != folder:
            project_store.upgrade_layout(moved)
            stamp_folder_name(moved, wanted)
        lines.append(f"{note if moved == folder else note}")
    return lines


def migrate_old(source: str, home: str, force: bool = False) -> list[str]:
    """把 ``source`` 下发现的工程搬进仓库并整理成 配置/数据/报表。

    认识两种写法：``source/<工程名>/project.json``（老工程目录）与
    ``source/project.json``（本身就当一个工程）。
    """
    lines: list[str] = []
    source = os.path.abspath(source)
    if not os.path.isdir(source):
        return [f"目录不存在：{source}"]

    candidates: list[str] = []
    if project_store.find_project_in(source):
        candidates.append(source)
    for name in sorted(os.listdir(source)):
        folder = os.path.join(source, name)
        if not os.path.isdir(folder) or name.startswith("_"):
            continue
        if project_store.find_project_in(folder):
            candidates.append(folder)

    for folder in candidates:
        project_file = project_store.find_project_in(folder)
        wanted = project_store.folder_name(os.path.basename(folder))
        try:
            wanted = project_store.project_folder_name(
                project_store._load_project_file(project_file)
            )
        except Exception:  # noqa: BLE001 - 工程文件坏了就按文件夹名推
            pass
        target = os.path.join(home, wanted or os.path.basename(folder))
        if os.path.normcase(folder) == os.path.normcase(target):
            project_store.upgrade_layout(folder)
            lines.append(f"就地整理：{os.path.basename(folder)}/")
            continue
        if os.path.exists(target) and not force:
            lines.append(
                f"仓库里已经有 {os.path.basename(target)}/，跳过（要覆盖加 --force）"
            )
            continue
        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(folder, target)
        project_store.upgrade_layout(target)
        lines.append(f"已搬入并整理：{os.path.basename(folder)}/ → {os.path.basename(target)}/")
    if not candidates:
        lines.append(f"没在这里找到工程：{source}")
    return lines


def archive_loose(paths: list[str], home: str) -> list[str]:
    """把跑测试/探针留下的数据挪进仓库的 ``_archive``（不删，只是不碍事）。"""
    lines: list[str] = []
    target = os.path.join(home, ARCHIVE_DIR)
    moved = False
    for path in paths:
        if not os.path.exists(path):
            continue
        os.makedirs(target, exist_ok=True)
        name = os.path.basename(path.rstrip("\\/"))
        destination = os.path.join(target, name)
        if os.path.exists(destination):
            stem, suffix = os.path.splitext(name)
            index = 2
            while os.path.exists(destination):
                destination = os.path.join(target, f"{stem}({index}){suffix}")
                index += 1
        shutil.move(path, destination)
        lines.append(f"已归档：{path} -> {destination}")
        moved = True
    if moved:
        lines.append(
            f"归档目录只是留个底，确认没用可以直接删掉：{target}"
        )
    return lines


def show(home: str) -> list[str]:
    lines = [f"工程仓库：{home}", f"（说明：{project_store.discover_project_home(repo_root(), home, create=False)[1]}）"]
    projects = project_store.list_projects(home)
    if not projects:
        lines.append("  还没有工程。")
    for folder in projects:
        config_file = project_store.find_project_in(folder)
        project_file = project_store._find_project_file(folder)
        paths = None
        try:
            seed = project_store._load_project_file(project_file)
            paths = project_store.resolve(seed, project_file)
        except Exception:  # noqa: BLE001 - 只为了显示，坏了也不该崩
            paths = None
        has_db = bool(paths and os.path.exists(paths.db_file))
        has_report = bool(paths and os.path.isdir(paths.report_dir))
        subdirs = sorted(
            name for name in os.listdir(folder) if os.path.isdir(os.path.join(folder, name))
        )
        cjk = "" if os.path.basename(folder).isascii() else "  ← 非英文名，建议 --english-names"
        lines.append(
            f"  · {os.path.basename(folder)}/"
            f"　[config {'有' if config_file else '缺'}"
            f" / data {'有' if has_db else '无'}"
            f" / reports {'有' if has_report else '无'}]"
            f"　子目录 {subdirs}{cjk}"
        )
    others = [
        name
        for name in sorted(os.listdir(home))
        if name.startswith("_") and os.path.isdir(os.path.join(home, name))
    ]
    for name in others:
        lines.append(f"  · {name}/（软件自己用的，不在「打开工程」里列出来）")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description="准备工程仓库 VVPproject")
    parser.add_argument("--home", default="", help="工程仓库目录（默认自动查找）")
    parser.add_argument(
        "--samples",
        nargs="*",
        default=list(DEFAULT_SAMPLES),
        help="要准备的内置示例（默认 储能 / 光伏 / 电力系统 / SST）",
    )
    parser.add_argument("--migrate-old", default="", help="把这里发现的老工程搬进仓库并整理")
    parser.add_argument(
        "--english-names",
        action="store_true",
        help="把仓库里中文的工程文件夹名改成英文（就地改名，改不动就保持原样）",
    )
    parser.add_argument(
        "--archive", nargs="*", default=[], help="跑测试留下的数据，挪进 _archive"
    )
    parser.add_argument("--force", action="store_true", help="同名工程已存在时覆盖")
    parser.add_argument("--list", action="store_true", help="只列出仓库里现有的工程")
    args = parser.parse_args()

    home, why = project_store.discover_project_home(repo_root(), args.home)
    print(f"工程仓库：{home}（{why}）")

    if args.list:
        for line in show(home):
            print(line)
        return 0

    if args.english_names:
        for line in english_names(home):
            print("·", line)
    for name in args.samples:
        print("·", make_sample(home, name, force=args.force))
    if args.migrate_old:
        for line in migrate_old(args.migrate_old, home, force=args.force):
            print("·", line)
    if args.archive:
        for line in archive_loose(args.archive, home):
            print("·", line)

    print()
    for line in show(home):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
