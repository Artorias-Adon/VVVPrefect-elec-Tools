"""把 ``dist/VVPhmi`` 打成发布用的 zip。

**为什么要单独写一个脚本**：`Compress-Archive` 会把"跑过一次"留下的运行时文件一起打进去 ——
最典型的是 ``settings.json``，它存的是**打包这台机器上的**工程仓库路径
（``D:\\Users\\...\\VVPproject`` 这种），发出去等于把本机路径和目录结构给了别人；
exe 也常因为正在运行而把文件锁住，压出来的包会少文件。这里：
* 只收 exe 与它自带的 ``_internal``；
* 明确踢掉运行时产物（settings.json、日志、缓存）；
* 打包前先检查 exe 没在跑（在跑就直接报错退出，不留半个包）；
* 把 ``LICENSE`` 与 ``THIRD-PARTY-LICENSES.md`` 放到包顶层 ——
  GPL / LGPL 都要求再分发时带上许可文本。

用法::

    python tools/make_release_zip.py                  # 版本号从 icomm/__init__.py 读
    python tools/make_release_zip.py --version 1.4.0  # 显式指定
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

#: 运行时产物：不进发布包
EXCLUDED_NAMES = {"settings.json", "settings.json.tmp"}
EXCLUDED_SUFFIXES = (".log", ".pyc", ".pyo")
EXCLUDED_DIRS = {"logs", "__pycache__", ".pytest_cache"}

#: 必须随包发出去的许可/说明文件（GPL 与 LGPL 都要求再分发时带上许可文本）
EXTRA_FILES = ("LICENSE", "THIRD-PARTY-LICENSES.md")


def app_version() -> str:
    """从 icomm/__init__.py 读版本号（软件版本只写一份）。"""
    with open(os.path.join(ROOT, "icomm", "__init__.py"), encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("__version__"):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return "0.0.0"


def _skip(relative: str) -> bool:
    parts = relative.replace("\\", "/").split("/")
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    name = parts[-1]
    if name in EXCLUDED_NAMES:
        return True
    return name.endswith(EXCLUDED_SUFFIXES)


def make_zip(dist_dir: str, target: str, extra_files: tuple[str, ...] = EXTRA_FILES) -> tuple[int, list[str]]:
    """把 ``dist_dir`` 的内容打进 ``target``，返回 ``(条目数, 被剔掉的文件)``。

    ``extra_files`` 是仓库根目录下必须一起打包的文件（许可文本等），
    它们会被放在 zip 的顶层，跟 exe 并排。
    """
    if not os.path.isdir(dist_dir):
        raise FileNotFoundError(f"找不到打包目录：{dist_dir}（先跑一次 pyinstaller）")

    dropped: list[str] = []
    count = 0
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for folder, dirs, files in os.walk(dist_dir):
            dirs[:] = [d for d in dirs if d not in EXCLUDED_DIRS]
            for name in sorted(files):
                full = os.path.join(folder, name)
                relative = os.path.relpath(full, dist_dir)
                if _skip(relative):
                    dropped.append(relative)
                    continue
                archive.write(full, relative)
                count += 1
        for name in extra_files:
            source = os.path.join(ROOT, name)
            if os.path.exists(source):
                archive.write(source, name)
                count += 1
    return count, dropped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="打发布 zip")
    parser.add_argument("--version", default="", help="版本号，默认读 icomm/__init__.py")
    parser.add_argument("--dist", default=os.path.join(ROOT, "dist", "VVPhmi"))
    parser.add_argument("--out", default="", help="输出 zip 路径")
    args = parser.parse_args(argv)

    version = args.version or app_version()
    exe = os.path.join(args.dist, "VVPhmi.exe")
    if not os.path.exists(exe):
        print(f"找不到 {exe}：先执行 pyinstaller --noconfirm VVPhmi.spec")
        return 1
    try:
        # 正在运行的 exe 会把 _internal 里的文件锁住，压出来的包可能缺文件
        with open(os.path.join(args.dist, "_internal", "base_library.zip"), "rb"):
            pass
    except OSError as exc:
        print(f"打包目录里有文件被占用（程序是不是还在运行？）：{exc}")
        return 1

    target = args.out or os.path.join(ROOT, f"VVPhmi-{version}-win11-x64.zip")
    if os.path.exists(target):
        os.remove(target)
    count, dropped = make_zip(args.dist, target)
    size_mb = os.path.getsize(target) / 1024 / 1024
    print(f"已生成 {target}")
    print(f"  条目 {count} 个，{size_mb:.1f} MB")
    for name in EXTRA_FILES:
        if os.path.exists(os.path.join(ROOT, name)):
            print(f"  已附加：{name}")
    if dropped:
        print(f"  已剔除运行时产物：{', '.join(sorted(dropped))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
