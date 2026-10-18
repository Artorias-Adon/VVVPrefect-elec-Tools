"""工程仓库：工程目录 / 松散模式 / ``.iprj`` 单文件打包。

工程仓库（VVPproject）
---------------------
**软件文件与工程文件分开放**：软件（源码、打包产物、测试）待在软件目录里，
而工程（配置 + 数据 + 报表）统一放在一个独立的**工程仓库**目录下，
目录名固定为 ``VVPproject``，里面一个工程一个子目录::

    VVPproject/
      sst-transformer/      工程目录名用英文，界面里显示中文工程名
        config/             工程配置（可交付、可版本管理）
          project.json      工程定义（通道 / 设备 / 点位 / 示意图 / 存储设置）
          parameters.csv    参数表，可用 Excel 直接打开编辑
          diagram.svg       可选：示意图底图
        data/               运行与测试数据
          history.db        历史 + 报警 + 通信日志 + 录波
        reports/            导出的测试结果
          datalog/  waveforms/  screenshots/  audio/

工程仓库的位置**不用手填**：启动时会自己找
（环境变量 ``VVPHMI_PROJECT_HOME`` → 软件设置里指定的目录 → 从程序目录逐级向上找现成的
``VVPproject`` → "文档"目录 → 最后才在程序目录旁新建），
也可以在「设置 → 软件设置 → 工程存放位置」里改成别的目录。

旧的**扁平布局**（``project.json`` 与 ``history.db`` 直接放在工程目录下）仍然能正常打开，
想整理成 ``config/data/reports`` 三件套就用 :func:`upgrade_layout`。

**松散模式** —— 工程文件、参数表、数据库三者路径各自指定，
例如库放网络盘、参数表放共享目录给同事一起编辑。

``.iprj`` 是 ZIP 容器，内含上述文件与 ``manifest.json``（版本与说明），
内部仍是扁平结构（单文件便于查看）。
打开 ``.iprj`` 时解压到 ``<工程仓库>/_opened/<工程名>``，
之后保存会**同时**更新该工作目录并重新打回原 ``.iprj``（原子替换），
符合"读取这个保存文件就打开对应工程"的用法。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import zipfile
from dataclasses import dataclass, field

from .. import APP_SHORT
from . import csv_table
from .models import Project

IPRJ_SUFFIX = ".iprj"
MANIFEST_NAME = "manifest.json"
PROJECT_NAME = "project.json"
CSV_NAME = "parameters.csv"
DB_NAME = "history.db"
SVG_NAME = "diagram.svg"

#: 工程仓库目录名（软件本身不带工程，工程统一放这里）
PROJECT_HOME_NAME = "VVPproject"
#: 工程目录内部的三个子目录：配置 / 数据（测试数据） / 报表（测试结果）
#:
#: **统一用英文**：中文目录名在命令行、ZIP、别的工具、同事的英文系统上
#: 都容易出乱码或转义问题，磁盘上的目录一律 ASCII，界面里显示的中文名照旧。
#: 老工程里的「配置 / 数据 / 报表」也认（见下面的别名表），打开时会自动改成英文。
CONFIG_DIR = "config"
DATA_DIR = "data"
REPORT_DIR = "reports"
#: 打开 .iprj 时解压到工程仓库下的这个子目录（带下划线，排在前面表示"系统用"）
OPENED_DIR = "_opened"

#: 认这些名字，是为了让手工改过名、或老版本建的中文目录也能被认出来
CONFIG_DIR_ALIASES = ("config", "conf", "configuration", "配置")
DATA_DIR_ALIASES = ("data", "db", "database", "数据")
REPORT_DIR_ALIASES = ("reports", "report", "报表")

#: ``reports/`` 下的分类目录：英文名 -> 界面上的中文说明。
#: 磁盘上一律英文（同上面的理由），工具里显示的中文标签照旧。
REPORT_KINDS: dict[str, str] = {
    "datalog": "数据记录",
    "faults": "故障录波",
    "shots": "截图",
    "audio": "录音",
    # 生成的报告（``报表/documents``：报表根目录本身就叫 reports，别套同名子目录）
    "documents": "报告",
}
#: 老版本用过中文分类目录名，认出来就沿用（新工程一律用英文名）
_LEGACY_REPORT_KINDS = {label: key for key, label in REPORT_KINDS.items()}

#: 打进 .iprj 的文件（存在才打）；值是"在工程目录里的相对位置"
_PACK_FILES = {
    PROJECT_NAME: CONFIG_DIR,
    CSV_NAME: CONFIG_DIR,
    SVG_NAME: CONFIG_DIR,
    DB_NAME: DATA_DIR,
}

FORMAT_VERSION = 1


@dataclass
class ProjectPaths:
    """一个工程当前的位置信息。"""

    mode: str = "directory"  # directory | loose
    #: 工程文件夹（工程仓库里的那个子目录）
    root: str = ""
    #: 工程配置所在目录（``root/配置``；扁平布局时就是 ``root``）
    config_dir: str = ""
    #: 运行与测试数据所在目录（``root/数据``）
    data_dir: str = ""
    #: 测试结果报表目录（``root/报表``）
    report_dir: str = ""
    project_file: str = ""
    csv_file: str = ""
    db_file: str = ""
    #: 目录布局：organized = 配置/数据/报表 三件套；flat = 老布局全在根目录
    layout: str = "flat"
    #: 若工程是从 .iprj 打开的，这里记住原文件路径，保存时会回写
    origin_iprj: str = ""

    @property
    def organized(self) -> bool:
        return self.layout == "organized"

    def summary(self) -> str:
        if self.mode == "directory":
            return f"工程目录：{self.root}"
        return f"松散模式：库 {self.db_file}｜表 {self.csv_file}"

    def report_subdir(self, kind: str) -> str:
        """取（并创建）报表下的一个分类目录，例如 ``reports/shots``。

        ``kind`` 收英文名（``shots``）或界面上的中文标签（``截图``），
        磁盘上一律落成英文目录名。
        """
        name = report_kind_dir(kind)
        base = self.report_dir or self.root or "."
        # 老工程里已经有中文分类目录的话，接着往里写，别突然换个地方
        for label, key in _LEGACY_REPORT_KINDS.items():
            if key == name and os.path.isdir(os.path.join(base, label)):
                name = label
                break
        target = os.path.join(base, name)
        try:
            os.makedirs(target, exist_ok=True)
        except OSError:
            return base
        return target

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "root": self.root,
            "config_dir": self.config_dir,
            "data_dir": self.data_dir,
            "report_dir": self.report_dir,
            "project_file": self.project_file,
            "csv_file": self.csv_file,
            "db_file": self.db_file,
            "layout": self.layout,
            "origin_iprj": self.origin_iprj,
        }


@dataclass
class SaveResult:
    files: list[str] = field(default_factory=list)
    iprj: str = ""
    csv_overwritten: bool = False
    #: 保存前发现参数表被外部（Excel）改过
    csv_external_changes: bool = False


# --------------------------------------------------------------- 路径解析


def _existing_dir(root: str, aliases: tuple[str, ...], canonical: str) -> str:
    """在 ``root`` 下找现成的子目录：先用别名里已有的，都没有就用规范名（英文）。"""
    for name in aliases:
        candidate = os.path.join(root, name)
        if os.path.isdir(candidate):
            return candidate
    return os.path.join(root, canonical)


def _find_project_file(root: str) -> str:
    """在工程目录里找 ``project.json``：``config/`` → 老的「配置」→ 根目录（扁平）。

    都找不到返回空串。
    """
    root = os.path.abspath(root)
    for name in CONFIG_DIR_ALIASES:
        candidate = os.path.join(root, name, PROJECT_NAME)
        if os.path.exists(candidate):
            return candidate
    flat = os.path.join(root, PROJECT_NAME)
    return flat if os.path.exists(flat) else ""


def layout_of(project_file: str) -> tuple[str, str, str]:
    """由工程文件路径反推 ``(工程文件夹, 配置目录, 布局)``。

    ``<root>/config/project.json`` 是整理过的布局（老工程是 ``<root>/配置/``，
    照样认）；``<root>/project.json`` 是老布局（仍然支持，只是没把测试数据分开）。
    """
    config_dir = os.path.dirname(os.path.abspath(project_file))
    parent = os.path.dirname(config_dir)
    marker = os.path.basename(config_dir)
    low = marker.lower()
    if low in [name.lower() for name in CONFIG_DIR_ALIASES] and parent:
        return parent, config_dir, "organized"
    if low in [name.lower() for name in DATA_DIR_ALIASES] and parent:
        # 手误把 project.json 放进了「数据」目录，也当成整理过的布局，
        # 这样配置目录就是父目录下的配置目录
        config = _find_config_dir(parent)
        if config:
            return parent, config, "organized"
    return config_dir, config_dir, "flat"


def _find_config_dir(root: str) -> str:
    """在 ``root`` 下找配置目录（``config`` 或老的「配置」），没有返回空串。"""
    for name in CONFIG_DIR_ALIASES:
        candidate = os.path.join(root, name)
        if os.path.isdir(candidate):
            return candidate
    return ""


def dirs_for(root: str, layout: str) -> tuple[str, str, str]:
    """``(配置目录, 数据目录, 报表目录)``。"""
    if layout == "organized":
        return (
            os.path.join(root, CONFIG_DIR),
            os.path.join(root, DATA_DIR),
            os.path.join(root, REPORT_DIR),
        )
    return root, root, root


def resolve(project: Project, project_file: str, origin_iprj: str = "") -> ProjectPaths:
    """由工程文件路径 + 工程内的存储设置，算出各文件实际位置。"""
    project_file = os.path.abspath(project_file)
    root, config_dir, layout = layout_of(project_file)
    if layout == "organized":
        # 老工程可能还叫「数据 / 报表」，认出来就用它，别指到一个不存在的英文目录上
        data_dir = _existing_dir(root, DATA_DIR_ALIASES, DATA_DIR)
        report_dir = _existing_dir(root, REPORT_DIR_ALIASES, REPORT_DIR)
    else:
        data_dir = report_dir = root
    storage = project.storage

    def locate(in_project: bool, path: str, default_name: str, folder: str) -> str:
        name = (path or default_name).strip() or default_name
        if in_project:
            # 工程内相对位置只取文件名：搬进 config/ data/ 也不会丢设置
            return os.path.join(folder, os.path.basename(name))
        # 自定义：绝对路径直接用，相对路径按工程目录解析
        # normpath 统一分隔符，避免 混合 / 与 \ 导致同一路径字符串比较不相等
        full = name if os.path.isabs(name) else os.path.join(root, name)
        return os.path.normpath(full)

    db_file = locate(storage.db_in_project, storage.db_path, DB_NAME, data_dir)
    csv_file = locate(storage.csv_in_project, storage.csv_path, CSV_NAME, config_dir)
    mode = "directory" if (storage.db_in_project and storage.csv_in_project) else "loose"
    return ProjectPaths(
        mode=mode,
        root=root,
        config_dir=config_dir,
        data_dir=data_dir,
        report_dir=report_dir,
        project_file=project_file,
        csv_file=csv_file,
        db_file=db_file,
        layout=layout,
        origin_iprj=origin_iprj,
    )


# --------------------------------------------------------------- 工程仓库


def _walk_out(base: str, levels: int = 4) -> list[str]:
    """从 ``base`` 的名字开始逐级向外，返回 ``[父目录, 祖父, …]``。

    先看"软件目录外面"（用户通常把工程仓库放在软件目录旁边），
    再看软件目录自己（便携式布置：工程仓库与程序放一起）。
    """
    base = os.path.abspath(base)
    chain: list[str] = []
    current = os.path.dirname(base)
    for _ in range(levels):
        if not current:
            break
        chain.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    chain.append(base)  # 最后才是软件目录自己
    return chain


def project_home_candidates(base_dir: str = "", preferred: str = "") -> list[tuple[str, str]]:
    """列出"从哪儿找工程仓库"，按优先级返回 ``[(目录, 说明)]``。

    顺序：显式指定 → 环境变量 → 软件目录外的上级目录（由近到远）→ 软件目录自己 →
    当前工作目录同上 → 「文档」目录 → 用户主目录。
    **先找现成的**，这样一个软件目录 + 打包后的 exe 会指向同一个工程仓库。
    """
    seen: set[str] = set()
    found: list[tuple[str, str]] = []

    def add(path: str, reason: str) -> None:
        if not path:
            return
        full = os.path.abspath(os.path.expanduser(path))
        key = os.path.normcase(full)
        if key in seen:
            return
        seen.add(key)
        found.append((full, reason))

    add(preferred, "软件设置里指定的目录")
    add(os.environ.get("VVPHMI_PROJECT_HOME", ""), "环境变量 VVPHMI_PROJECT_HOME")

    for start, label in ((base_dir, "程序目录"), (os.getcwd(), "当前工作目录")):
        if not start:
            continue
        chain = _walk_out(start)
        for index, folder in enumerate(chain):
            inside = index == len(chain) - 1
            reason = (
                f"在{label}里找到" if inside else f"从{label}向外第 {index + 1} 层找到"
            )
            add(os.path.join(folder, PROJECT_HOME_NAME), reason)

    home = os.path.expanduser("~")
    for folder in ("Documents", "文档"):
        add(os.path.join(home, folder, PROJECT_HOME_NAME), f"「{folder}」目录")
    add(os.path.join(home, PROJECT_HOME_NAME), "用户主目录")
    return found


def default_project_home(base_dir: str) -> str:
    """没有现成仓库时该在哪儿新建：优先软件目录**外面**（与程序平级）。"""
    base_dir = os.path.abspath(base_dir or ".")
    parent = os.path.dirname(base_dir)
    if parent and parent != base_dir:
        return os.path.join(parent, PROJECT_HOME_NAME)
    return os.path.join(base_dir, PROJECT_HOME_NAME)


def discover_project_home(
    base_dir: str = "", preferred: str = "", create: bool = True
) -> tuple[str, str]:
    """找到工程仓库目录，返回 ``(目录, 说明)``。

    优先复用已经存在的 ``VVPproject``；一个都找不到才在软件目录**旁边**新建，
    保证"软件目录里只放软件"。
    """
    candidates = project_home_candidates(base_dir, preferred)
    for path, reason in candidates:
        if os.path.isdir(path):
            return path, reason

    fallback = default_project_home(base_dir)
    if create:
        try:
            os.makedirs(fallback, exist_ok=True)
        except OSError:
            alias = os.path.join(base_dir or ".", PROJECT_HOME_NAME)
            try:
                os.makedirs(alias, exist_ok=True)
                fallback = alias
            except OSError:
                fallback = os.path.join(tempfile.gettempdir(), PROJECT_HOME_NAME)
                os.makedirs(fallback, exist_ok=True)
    return fallback, "没有现成的工程仓库，已在软件目录旁新建"


def list_projects(home: str) -> list[str]:
    """列出工程仓库里已有的工程目录（用于"打开工程"时直接挑一个）。"""
    try:
        names = sorted(os.listdir(home))
    except OSError:
        return []
    result = []
    for name in names:
        if name.startswith(".") or name.startswith("_"):
            continue
        folder = os.path.join(home, name)
        if not os.path.isdir(folder):
            continue
        if _find_project_file(folder):
            result.append(folder)
    return result


def default_workdir() -> str:
    return os.path.join(tempfile.gettempdir(), "icomm-projects")


def report_kind_dir(kind: str) -> str:
    """把报表分类名规范成磁盘上的英文目录名（``截图`` 与 ``shots`` 都收）。"""
    text = (kind or "").strip()
    if text in REPORT_KINDS:
        return text
    if text in _LEGACY_REPORT_KINDS:
        return _LEGACY_REPORT_KINDS[text]
    return folder_name(text, fallback="misc")


def find_project_in(folder: str) -> str:
    """这个文件夹是不是一个工程目录？是就返回它的 ``project.json`` 路径（否则空串）。"""
    return _find_project_file(folder) if os.path.isdir(folder) else ""


def rename_project_dir(
    paths: ProjectPaths, new_folder_name: str, home: str = ""
) -> tuple[str, str]:
    """把工程文件夹改名（英文名）。

    返回 ``(新的工程目录, 说明)``；改不动（目标已存在、文件被占用、名字没变）
    就原样返回旧目录，**不会丢东西**。
    """
    current = os.path.abspath(paths.root or ".")
    # fallback="" ：纯中文名不静默变成 "project"，而是明确拒绝（界面那边也会先校验）
    wanted = folder_name(new_folder_name, fallback="")
    if not wanted:
        return current, "文件夹名只能包含英文字母、数字、- 和 _"
    parent = home or os.path.dirname(current)
    target = os.path.join(parent, wanted)
    if os.path.normcase(target) == os.path.normcase(current):
        return current, "文件夹名没变"
    if os.path.exists(target):
        return current, f"已经有一个叫「{wanted}」的文件夹了，换个名字"
    try:
        os.replace(current, target)
    except OSError as exc:
        return current, f"改名失败（{exc}），已保持原名"
    return target, f"已改名为 {wanted}"


def open_project(
    path: str, workdir: str | None = None, upgrade: bool = False
) -> tuple[Project, ProjectPaths]:
    """打开工程。

    ``path`` 可以是 ``.iprj`` 单文件、``project.json``、或含有 ``project.json`` 的目录。

    * ``.iprj`` 会被解压到 ``<workdir>/_已打开的工程包/<工程名>``（``workdir`` 默认临时目录），
      解压出的目录即工程目录；``origin_iprj`` 记住原文件，保存时回写。
      解压目标固定在专用子目录里，**不会**覆盖同名的正式工程目录。
    * ``upgrade=True`` 时，若打开的是老式扁平布局的工程目录，顺手整理成
      ``配置/数据/报表`` 三件套（移动失败就保持原样，不会丢文件）。
    """
    path = os.path.abspath(path)

    if path.lower().endswith(IPRJ_SUFFIX):
        root = os.path.join(
            workdir or default_workdir(),
            OPENED_DIR,
            _safe_name(os.path.splitext(os.path.basename(path))[0]),
        )
        unpack_iprj(path, root)
        project_file = os.path.join(root, CONFIG_DIR, PROJECT_NAME)
        project = _load_project_file(project_file)
        return project, resolve(project, project_file, origin_iprj=path)

    if os.path.isdir(path):
        project_file = _find_project_file(path)
        if not project_file:
            raise FileNotFoundError(f"目录里没有 {PROJECT_NAME}：{path}")
        if upgrade:
            upgrade_layout(path)
        project = _load_project_file(project_file)
        return project, resolve(project, project_file)

    project = _load_project_file(path)
    return project, resolve(project, path)


def _load_project_file(path: str) -> Project:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return Project.from_dict(data)


# --------------------------------------------------------------- 名字

#: Windows 上不能用作文件/目录名的保留名
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def safe_name(name: str) -> str:
    """把工程名转成可用作目录/文件名的字符串（允许中文，只挡非法字符）。"""
    return _safe_name(name)


def _safe_name(name: str) -> str:
    bad = '<>:"/\\|?*'
    cleaned = "".join("_" if ch in bad else ch for ch in name).strip()
    return cleaned or "project"


def folder_name(name: str, fallback: str = "project") -> str:
    """把工程名转成**英文（ASCII）文件夹名**。

    磁盘上的目录一律用英文：中文目录名在命令行、ZIP、同事的英文系统上
    容易出乱码或转义问题。规则：非 ASCII 字符直接丢掉、空格与非法字符变 ``-``，
    结果为空或撞上 Windows 保留名时用 ``fallback``（调用方一般传 ``project-1`` 这种）。

    界面上显示的仍是工程本名（中文照旧），只有目录名是英文。
    """
    bad = '<>:"/\\|?*'
    out: list[str] = []
    for ch in name:
        if ord(ch) > 127:
            out.append("-")  # 中文等非 ASCII 占位，稍后压缩掉
        elif ch in bad or ch.isspace():
            out.append("-")
        else:
            out.append(ch)
    text = "".join(out)
    while "--" in text:
        text = text.replace("--", "-")
    text = text.strip("-. ")
    if not text or text.upper() in _RESERVED_NAMES:
        return fallback
    return text


def project_folder_name(project) -> str:
    """一个工程该用的文件夹名：优先用它自己填的英文名，否则由显示名生成。"""
    declared = (getattr(project, "folder_name", "") or "").strip()
    if declared:
        return folder_name(declared, fallback=declared or "project")
    return folder_name(project.name)


def unique_folder_name(home: str, wanted: str, ignore: str = "") -> str:
    """在 ``home`` 下找个不撞名的文件夹名（``wanted`` 被占就加 ``-2``/``-3``）。"""
    candidate = wanted or "project"
    index = 2
    while True:
        path = os.path.join(home, candidate)
        if not os.path.exists(path) or os.path.normcase(path) == os.path.normcase(ignore):
            return candidate
        candidate = f"{wanted or 'project'}-{index}"
        index += 1


def suggest_folder_name(home: str, project, ignore: str = "") -> str:
    """按工程名给一个不撞名的英文文件夹名。"""
    wanted = project_folder_name(project)
    if wanted == "project":  # 名字里一个 ASCII 字符都没有，用 project-N
        wanted = ""
    return unique_folder_name(home, wanted or "project", ignore=ignore)


# --------------------------------------------------------------- 保存


def save_project(
    project: Project,
    paths: ProjectPaths,
    export_csv: bool = True,
    overwrite_external_csv: bool = True,
) -> SaveResult:
    """保存工程。

    ``overwrite_external_csv=False`` 时，若发现参数表被外部改过就不覆盖它，
    并在结果里置 ``csv_external_changes`` 让界面提示用户（避免 Excel 里的改动被无声覆盖）。
    """
    result = SaveResult()

    if not paths.project_file:
        raise ValueError("没有工程文件路径，请先「另存为」")

    os.makedirs(os.path.dirname(paths.project_file) or ".", exist_ok=True)
    payload = {"version": FORMAT_VERSION, **project.to_dict()}
    with open(paths.project_file, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    result.files.append(paths.project_file)

    if export_csv:
        result.csv_external_changes = csv_table.csv_differs_from_disk(project, paths.csv_file)
        if result.csv_external_changes and not overwrite_external_csv:
            pass
        else:
            csv_table.export_csv(project, paths.csv_file)
            result.files.append(paths.csv_file)
            result.csv_overwritten = True

    if paths.origin_iprj:
        pack_iprj(paths, paths.origin_iprj)
        result.iprj = paths.origin_iprj

    return result


def create_project_dir(root: str, project: Project) -> ProjectPaths:
    """在指定目录新建工程（工程目录模式，配置/数据/报表 三件套）。"""
    root = os.path.abspath(root)
    config_dir, data_dir, report_dir = dirs_for(root, "organized")
    for folder in (config_dir, data_dir, report_dir):
        os.makedirs(folder, exist_ok=True)
    project_file = os.path.join(config_dir, PROJECT_NAME)
    paths = ProjectPaths(
        mode="directory",
        root=root,
        config_dir=config_dir,
        data_dir=data_dir,
        report_dir=report_dir,
        project_file=project_file,
        csv_file=os.path.join(config_dir, CSV_NAME),
        db_file=os.path.join(data_dir, DB_NAME),
        layout="organized",
    )
    save_project(project, paths)
    # 测试结果的分类目录一次建好，导出的东西不用临时找地方放
    for key in REPORT_KINDS:
        try:
            os.makedirs(os.path.join(report_dir, key), exist_ok=True)
        except OSError:
            pass
    return paths


def _english_dirs(root: str) -> list[tuple[str, str]]:
    """把老的中文子目录名改成英文，返回 ``[(旧路径, 新路径)]``。

    只改「配置 / 数据 / 报表」这三个自己建的目录，别的目录一概不碰；
    目标已存在就不动（宁可留着旧目录，也不去合并/覆盖用户的东西）。
    """
    moves: list[tuple[str, str]] = []
    for aliases, wanted in (
        (CONFIG_DIR_ALIASES, CONFIG_DIR),
        (DATA_DIR_ALIASES, DATA_DIR),
        (REPORT_DIR_ALIASES, REPORT_DIR),
    ):
        old = next(
            (
                name
                for name in aliases
                if name != wanted and os.path.isdir(os.path.join(root, name))
            ),
            "",
        )
        if not old:
            continue
        source = os.path.join(root, old)
        target = os.path.join(root, wanted)
        if os.path.exists(target):
            continue  # 英文目录已经有了，不硬合并
        moves.append((source, target))
    return moves


def upgrade_layout(root: str) -> ProjectPaths | None:
    """整理工程目录：老式扁平布局 → ``config/data/reports``，中文子目录名 → 英文。

    只动本软件自己生成的那几个文件与目录，其它东西一概不碰；
    搬不动（某个文件被别的程序占着，例如库正被另一个实例打开）就整体放弃，
    **保持原样不丢数据**。返回整理后的路径信息；本来就是整理好的布局则返回 ``None``。
    """
    root = os.path.abspath(root)
    changed = False

    # 第一步：中文子目录名改成英文（只认自己建的那三个）
    for source, target in _english_dirs(root):
        try:
            os.replace(source, target)
            changed = True
        except OSError:
            pass  # 改不动就算了，后面按别名照样能打开

    # 第二步：扁平布局收进子目录
    project_file = os.path.join(root, PROJECT_NAME)
    if os.path.exists(project_file):
        os.makedirs(os.path.join(root, REPORT_DIR), exist_ok=True)
        moves = [
            (PROJECT_NAME, CONFIG_DIR),
            (CSV_NAME, CONFIG_DIR),
            (SVG_NAME, CONFIG_DIR),
        ]
        # 数据库还有 -wal / -shm 两个伴生文件，必须一起搬，否则库状态会不一致
        for suffix in ("", "-wal", "-shm"):
            moves.append((DB_NAME + suffix, DATA_DIR))

        done: list[tuple[str, str]] = []
        try:
            for name, folder in moves:
                source = os.path.join(root, name)
                if not os.path.exists(source):
                    continue
                target_dir = os.path.join(root, folder)
                os.makedirs(target_dir, exist_ok=True)
                os.replace(source, os.path.join(target_dir, name))
                done.append((source, os.path.join(target_dir, name)))
        except OSError:
            # 搬了一半失败：把已经搬过去的搬回来，保持"打开前什么样就是什么样"
            for source, target in reversed(done):
                try:
                    os.replace(target, source)
                except OSError:
                    pass
            return None
        changed = changed or bool(done)

    config_file = _find_project_file(root)
    if not config_file:
        return None
    if not changed:
        return None
    return resolve(_load_project_file(config_file), config_file)


def create_in_workdir(project: Project, workdir: str | None = None) -> ProjectPaths:
    """在工程仓库里按工程名建一个英文文件夹（用于「另存为 .iprj」的工作副本）。"""
    home = workdir or default_workdir()
    root = os.path.join(home, suggest_folder_name(home, project))
    return create_project_dir(root, project)


def save_project_to_dir(
    project: Project, target: str, db_source: str = ""
) -> ProjectPaths:
    """把整个工程另存到一个**新的工程目录**里（「保存为工程目录」）。

    会连历史库一起复制：现场常见的用法是"把这个工程拷给同事"，
    只给定义不给历史的话，趋势页打开就是空的。
    """
    root = os.path.abspath(target)
    paths = create_project_dir(root, project)
    if db_source and os.path.exists(db_source):
        source = os.path.abspath(db_source)
        if source != os.path.abspath(paths.db_file):
            # 只拷已经落盘的库文件，不含 -wal/-shm：那两个是运行时临时文件，
            # 复制过来反而可能让目标库状态不一致
            try:
                shutil.copy2(source, paths.db_file)
            except OSError:
                pass
    return paths


def save_project_as_iprj(
    project: Project, paths: ProjectPaths, target: str, db_source: str = ""
) -> str:
    """把整个工程打成 ``.iprj`` 单文件包。

    先确保磁盘上的工程目录是最新的，再打包。
    """
    target = os.path.abspath(target)
    if not target.lower().endswith(IPRJ_SUFFIX):
        target += IPRJ_SUFFIX
    working = paths
    if not working.project_file or not os.path.exists(working.project_file):
        # 还没有工程目录（例如刚新建的空白工程）就先建一个工作副本
        working = create_in_workdir(project)
    if db_source and os.path.exists(db_source):
        source = os.path.abspath(db_source)
        if source != os.path.abspath(working.db_file):
            try:
                shutil.copy2(source, working.db_file)
            except OSError:
                pass
    return pack_iprj(working, target)


# --------------------------------------------------------------- .iprj 打包


def pack_iprj(paths: ProjectPaths, iprj_path: str) -> str:
    """把工程目录打包成 .iprj（ZIP 容器，内部扁平），原子替换目标文件。"""
    iprj_path = os.path.abspath(iprj_path)
    os.makedirs(os.path.dirname(iprj_path) or ".", exist_ok=True)
    tmp = iprj_path + ".tmp"

    manifest = {
        "format": FORMAT_VERSION,
        "application": APP_SHORT,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": (
            f"{APP_SHORT} 工程包：project.json 为工程定义，parameters.csv 为参数表，"
            "history.db 为历史与录波；打开后会自动解压成 配置/数据/报表 目录结构"
        ),
    }

    sources = {
        PROJECT_NAME: paths.project_file,
        CSV_NAME: paths.csv_file,
        DB_NAME: paths.db_file,
        SVG_NAME: os.path.join(paths.config_dir or paths.root, SVG_NAME),
    }

    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))
        for name in _PACK_FILES:
            candidate = sources.get(name, "")
            if candidate and os.path.exists(candidate):
                zf.write(candidate, name)
    os.replace(tmp, iprj_path)
    return iprj_path


def unpack_iprj(iprj_path: str, target_dir: str) -> str:
    """解压 .iprj 到目标目录（先清空，保证不会残留上一版文件）。

    解出来的目录是整理过的布局（``配置/`` + ``数据/``），
    这样打开工程包和打开普通工程目录得到的结构完全一致。
    """
    if not os.path.exists(iprj_path):
        raise FileNotFoundError(iprj_path)
    if os.path.isdir(target_dir):
        shutil.rmtree(target_dir, ignore_errors=True)
    os.makedirs(target_dir, exist_ok=True)

    with zipfile.ZipFile(iprj_path, "r") as zf:
        names = set(zf.namelist())
        if PROJECT_NAME not in names:
            raise ValueError(f"{os.path.basename(iprj_path)} 不是有效的 {APP_SHORT} 工程包（缺少 {PROJECT_NAME}）")
        for name in names:
            # 只认自己打进去的那几个文件，防止包里夹带路径穿越的内容
            folder = _PACK_FILES.get(os.path.basename(name))
            if folder is None:
                continue
            if os.path.basename(name) != name:
                continue
            os.makedirs(os.path.join(target_dir, folder), exist_ok=True)
            with zf.open(name) as source:
                with open(os.path.join(target_dir, folder, name), "wb") as target:
                    shutil.copyfileobj(source, target)
    return target_dir


def iprj_manifest(iprj_path: str) -> dict:
    """读取工程包的清单信息（用于打开前预览）。"""
    with zipfile.ZipFile(iprj_path, "r") as zf:
        if MANIFEST_NAME in zf.namelist():
            return json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
    return {}
