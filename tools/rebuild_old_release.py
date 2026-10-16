"""按 tag 在本地重建老版本的免安装包（留档用）。

GitHub 的资产下载在这个网络环境里几乎是断的（实测 4 分钟只下来 0.06 MB，
而且不支持断点续传），所以"把老版本留一份到本地"改用**从 tag 重建**：

    python tools/rebuild_old_release.py v1.0.0 v1.0.1 v1.0.2

每个 tag 建一个临时工作树，用当时的源码 + 本机当前的依赖打一次包，
产物与各版本发布时同名，放在仓库根目录（已被 .gitignore 忽略）。
**注意**：重建用的是今天的依赖版本（PySide6 / pymodbus …），
所以它不是当年那个二进制文件的原样副本，而是"那个版本的源码打出来的包"。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def run(args: list[str], cwd: str) -> int:
    print(f"\n$ {' '.join(args)}  （cwd={os.path.relpath(cwd, ROOT)}）", flush=True)
    return subprocess.run(args, cwd=cwd).returncode


def spec_name(worktree: str) -> str:
    for name in ("VVPhmi.spec", "iComm.spec"):
        if os.path.exists(os.path.join(worktree, name)):
            return name
    raise FileNotFoundError("工作树里找不到打包 spec（VVPhmi.spec / iComm.spec）")


def rebuild(tag: str, keep_worktree: bool = False) -> int:
    worktree = os.path.join(ROOT, "_old_builds", tag)
    os.makedirs(os.path.dirname(worktree), exist_ok=True)
    if os.path.exists(worktree):
        shutil.rmtree(worktree, ignore_errors=True)
    if run(["git", "worktree", "add", "--detach", worktree, tag], ROOT) != 0:
        print(f"{tag}: 建工作树失败")
        return 1
    try:
        spec = spec_name(worktree)
        if run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", spec], worktree) != 0:
            print(f"{tag}: 打包失败")
            return 1
        from tools import make_release_zip

        dist_root = os.path.join(worktree, "dist")
        candidates = [os.path.join(dist_root, name) for name in sorted(os.listdir(dist_root))]
        dist_dir = next(
            (
                path
                for path in candidates
                if os.path.isdir(path)
                and any(name.lower().endswith(".exe") for name in os.listdir(path))
            ),
            candidates[0] if candidates else dist_root,
        )
        target = os.path.join(ROOT, f"VVPhmi-{tag.lstrip('v')}-win11-x64.zip")
        if os.path.exists(target):
            os.remove(target)
        count, dropped = make_release_zip.make_zip(dist_dir, target)
        size = os.path.getsize(target) / 1024 / 1024
        print(f"{tag}: 已生成 {os.path.basename(target)}（{count} 条目 / {size:.1f} MB），"
              f"剔除 {dropped or '无'}")
        return 0
    finally:
        if not keep_worktree:
            subprocess.run(["git", "worktree", "remove", worktree, "--force"], cwd=ROOT,
                           capture_output=True, text=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按 tag 本地重建老版本发布包")
    parser.add_argument("tags", nargs="+", help="例如 v1.0.0 v1.0.1 v1.0.2")
    parser.add_argument("--keep-worktree", action="store_true", help="保留临时工作树（调试用）")
    args = parser.parse_args(argv)

    failed: list[str] = []
    for tag in args.tags:
        if rebuild(tag, args.keep_worktree) != 0:
            failed.append(tag)
    print("\n结果：", "全部完成" if not failed else f"失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
