"""VVPhmi 入口。

    python main.py                      # 首次运行会在工程仓库建一个示例工程并打开
    python main.py --sample "SST 固态变压器"
    python main.py --project my.iprj    # 打开工程包
    python main.py --project path/to/project.json
    python main.py --project-home D:/VVPproject   # 指定工程仓库目录
    python main.py --db D:/data/x.db    # 覆盖历史库路径（自定义路径 = 松散模式）
    python main.py --sim                # 演示模式：同时拉起内置仿真器并开始采集

工程仓库（VVPproject）默认不用手填：见 :func:`icomm.core.project_store.discover_project_home`。
软件目录里只放软件，工程与测试结果都落在工程仓库里：

    VVPproject/<工程名>/
        配置/   project.json + parameters.csv + diagram.svg
        数据/   history.db
        报表/   录波 / 截图 / 录音 / 数据记录 等导出的测试结果
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from icomm import APP_NAME, APP_SHORT  # noqa: E402
from icomm.core import project as project_io  # noqa: E402
from icomm.core import project_store  # noqa: E402
from icomm.core import settings as app_settings  # noqa: E402
from icomm.ui import qtboot  # noqa: E402
from icomm.ui.main_window import MainWindow  # noqa: E402

#: 首次运行默认载入的示例工程（软件设置里可以改；权威默认值在 core.project）
DEFAULT_SAMPLE = project_io.DEFAULT_SAMPLE


def _mac_app_support_dir() -> str:
    """macOS 上的用户数据目录（``~/Library/Application Support/VVPhmi``）。

    打包成 ``.app`` 之后 ``sys.executable`` 在包里面（``Contents/MacOS``），
    把 settings.json 写在那儿既别扭、签名后还可能不可写，所以按 macOS 的规矩来。
    """
    target = os.path.join(os.path.expanduser("~"), "Library", "Application Support", "VVPhmi")
    try:
        os.makedirs(target, exist_ok=True)
    except OSError:
        return os.path.expanduser("~")
    return target


def app_base_dir() -> str:
    """程序基准目录。

    打包成 exe 后取 exe 所在目录，源码运行时取 main.py 所在目录。
    这样双击 exe 启动时，工程与历史库会落在 exe 旁边，
    而不是跟着「当前工作目录」跑（从快捷方式启动时 cwd 常常不是程序目录）。

    macOS 的 ``.app`` 例外：exe 在包内部，用户数据改放
    ``~/Library/Application Support/VVPhmi``。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
        if sys.platform == "darwin" and ".app/Contents/MacOS" in base.replace("\\", "/"):
            return _mac_app_support_dir()
        return base
    return os.path.dirname(os.path.abspath(__file__))


def load_startup_project(args, workdir: str, settings):
    """按命令行参数决定启动时打开哪个工程。返回 (工程, 路径信息, 是否新建)。"""
    if args.project:
        project, paths = project_store.open_project(
            args.project, workdir=workdir, upgrade=True
        )
        return project, paths, False
    if settings.last_project and os.path.exists(settings.last_project):
        project, paths = project_store.open_project(
            settings.last_project, workdir=workdir, upgrade=True
        )
        return project, paths, False

    sample_name = args.sample or settings.default_sample or DEFAULT_SAMPLE
    builder = project_io.BUILTIN_SAMPLES.get(sample_name) or project_io.sst_project
    # 先按工程名（内置示例有英文名）算出目录；这个目录已经存在就直接打开
    project = builder()
    wanted = os.path.join(workdir, project_store.project_folder_name(project))
    marker = project_store.find_project_in(wanted)
    if marker:
        project, paths = project_store.open_project(wanted, workdir=workdir, upgrade=True)
        return project, paths, False

    root = os.path.join(workdir, project_store.suggest_folder_name(workdir, project))
    paths = project_store.create_project_dir(root, project)
    return project, paths, True


def main() -> int:
    base = app_base_dir()
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--project",
        default="",
        help="要打开的工程：.iprj 工程包 / project.json / 含 project.json 的目录",
    )
    parser.add_argument(
        "--sample",
        default="",
        choices=[""] + list(project_io.BUILTIN_SAMPLES),
        help="首次运行时载入哪个内置示例工程",
    )
    parser.add_argument(
        "--workdir",
        default="",
        help="工程仓库目录（一个工程一个子目录），默认自动查找 VVPproject",
    )
    parser.add_argument(
        "--project-home",
        default="",
        help="同 --workdir，写法更直白：指定工程仓库 VVPproject 的位置",
    )
    parser.add_argument("--db", default="", help="覆盖历史库路径（设为自定义路径即松散模式）")
    parser.add_argument(
        "--sim", action="store_true", help="演示模式：同时拉起内置 Modbus TCP 仿真器并自动开始采集"
    )
    args = parser.parse_args()

    settings = app_settings.load_settings(base)
    # 工程仓库：命令行 > 软件设置 > 自动查找（环境变量/向上找/文档目录/就近新建）
    preferred = args.project_home or args.workdir or settings.project_home
    workdir, why = project_store.discover_project_home(base, preferred)
    if not settings.project_home:
        # 把第一次找到的位置记下来，下次启动就不用再猜（用户之后也能在设置里改）
        settings.project_home = workdir
        try:
            app_settings.save_settings(settings, base)
        except OSError:
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_SHORT)

    # 必须在建窗口之前：某些中文 locale 下 Qt 会用 CJK 数字格式化数字框
    if qtboot.prepare_qt():
        print(f"[{APP_SHORT}] 系统 locale 的数字格式不是 0-9，已切换为英文数字格式")

    # 任务栏/窗口图标（源码运行时读 assets/，打包后读解包目录）
    from icomm.ui.resources import app_icon

    icon = app_icon()
    if icon is not None:
        app.setWindowIcon(icon)

    try:
        project, paths, created = load_startup_project(args, workdir, settings)
    except Exception as exc:
        QMessageBox.critical(None, "工程载入失败", str(exc))
        return 1

    if args.db:
        project.storage.db_in_project = False
        project.storage.db_path = os.path.abspath(args.db)
        paths = project_store.resolve(project, paths.project_file, origin_iprj=paths.origin_iprj)

    window = MainWindow(project, paths, workdir=workdir, base_dir=base)
    window.show()

    print(f"[{APP_SHORT}] 工程仓库：{workdir}（{why}）")
    if created:
        window._log_status(f"已生成示例工程「{project.name}」，可直接点「启动采集」")

    # 命令行参数优先于软件设置：--sim 一定开仿真器，没给参数才看设置
    if args.sim:
        window.action_sim.setChecked(True)
        window.start_engine()
    else:
        if settings.auto_start_simulator:
            window.action_sim.setChecked(True)
        if settings.auto_start_engine:
            window.start_engine()

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
