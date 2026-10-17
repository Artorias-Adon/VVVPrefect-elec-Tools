"""工程仓库测试：工程目录模式 / 松散模式 / .iprj 打包与打开。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile

from icomm import APP_SHORT
from icomm.core import csv_table, project as project_io, project_store
from icomm.core.models import ProjectStorage
from icomm.core.models import Project


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.project_file = os.path.join(self.root, "project.json")

    def test_directory_mode(self):
        project = project_io.sst_project()
        paths = project_store.resolve(project, self.project_file)
        self.assertEqual(paths.mode, "directory")
        self.assertEqual(paths.db_file, os.path.join(self.root, "history.db"))
        self.assertEqual(paths.csv_file, os.path.join(self.root, "parameters.csv"))

    def test_loose_mode_with_relative_paths(self):
        # 用临时目录拼绝对路径：Windows 上是 D:\...，macOS 上是 /var/... 都能跑
        loose_db = os.path.join(self.root, "elsewhere", "icomm.db")
        project = project_io.sst_project()
        project.storage = ProjectStorage(
            db_in_project=False, db_path=loose_db,
            csv_in_project=False, csv_path="params/points.csv",
        )
        paths = project_store.resolve(project, self.project_file)
        self.assertEqual(paths.mode, "loose")
        self.assertEqual(paths.db_file, os.path.normpath(loose_db))
        self.assertEqual(paths.csv_file, os.path.join(self.root, "params", "points.csv"))

    def test_mixed_mode_is_loose(self):
        project = project_io.sst_project()
        project.storage = ProjectStorage(db_in_project=False, db_path="net/machine.db")
        paths = project_store.resolve(project, self.project_file)
        self.assertEqual(paths.mode, "loose")

    def test_empty_path_falls_back_to_default_name(self):
        project = Project(name="x")
        project.storage = ProjectStorage(db_path="", csv_path="")
        paths = project_store.resolve(project, self.project_file)
        self.assertEqual(paths.db_file, os.path.join(self.root, "history.db"))
        self.assertEqual(paths.csv_file, os.path.join(self.root, "parameters.csv"))


class SaveAndOpenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workdir = os.path.join(self.tmp.name, "work")

    def test_create_project_dir_writes_both_files(self):
        """新建工程目录 = config / data / reports 三件套（英文名），配置与测试结果分开。"""
        project = project_io.sst_project()
        root = os.path.join(self.tmp.name, "SST工程")
        paths = project_store.create_project_dir(root, project)

        self.assertTrue(os.path.exists(os.path.join(root, "config", "project.json")))
        self.assertTrue(os.path.exists(os.path.join(root, "config", "parameters.csv")))
        self.assertTrue(os.path.isdir(os.path.join(root, "data")))
        self.assertTrue(os.path.isdir(os.path.join(root, "reports")))
        # 根目录里不该再散着这几个文件
        for name in ("project.json", "parameters.csv", "history.db"):
            self.assertFalse(
                os.path.exists(os.path.join(root, name)),
                f"{name} 应该收进子目录，不要散在工程根目录里",
            )
        self.assertEqual(paths.mode, "directory")
        self.assertEqual(paths.layout, "organized")
        self.assertEqual(paths.config_dir, os.path.join(root, "config"))
        self.assertEqual(paths.data_dir, os.path.join(root, "data"))
        self.assertEqual(paths.report_dir, os.path.join(root, "reports"))
        self.assertEqual(paths.db_file, os.path.join(root, "data", "history.db"))

    def test_subdir_names_are_ascii(self):
        """磁盘上的目录名一律英文（命令行 / ZIP / 英文系统上不出乱码）。"""
        for name in (
            project_store.CONFIG_DIR,
            project_store.DATA_DIR,
            project_store.REPORT_DIR,
            project_store.OPENED_DIR,
        ):
            self.assertTrue(name.isascii(), f"{name} 应该是英文名")

    def test_report_subdir_is_created(self):
        """测试结果导出目录按分类建在 reports/ 下，分类目录名也是英文。"""
        root = os.path.join(self.tmp.name, "报表工程")
        paths = project_store.create_project_dir(root, project_io.sst_project())
        target = paths.report_subdir("截图")
        self.assertTrue(os.path.isdir(target))
        self.assertEqual(os.path.dirname(target), paths.report_dir)
        self.assertEqual(os.path.basename(paths.report_dir), "reports")
        self.assertEqual(os.path.basename(target), "shots", "中文标签也要落成英文目录")
        self.assertEqual(paths.report_subdir("shots"), target, "英文名指同一个目录")

    def test_report_kind_dirs_are_english(self):
        for key in project_store.REPORT_KINDS:
            self.assertTrue(key.isascii(), f"{key} 应该是英文名")
        paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "分类工程"), project_io.sst_project()
        )
        self.assertEqual(
            sorted(name for name in os.listdir(paths.report_dir)),
            sorted(project_store.REPORT_KINDS),
        )

    def test_upgrade_layout_moves_files(self):
        """老的扁平布局能整理成三件套，且内容不丢。"""
        root = os.path.join(self.tmp.name, "老工程")
        os.makedirs(root, exist_ok=True)
        project = project_io.sst_project()
        # 手工造一个老布局
        with open(os.path.join(root, "project.json"), "w", encoding="utf-8") as fh:
            json.dump(project.to_dict(), fh, ensure_ascii=False)
        for name in ("parameters.csv", "history.db", "history.db-wal"):
            with open(os.path.join(root, name), "w", encoding="utf-8") as fh:
                fh.write(name)
        with open(os.path.join(root, "别动我.txt"), "w", encoding="utf-8") as fh:
            fh.write("用户自己的东西")

        paths = project_store.upgrade_layout(root)
        self.assertIsNotNone(paths)
        assert paths is not None
        self.assertEqual(paths.layout, "organized")
        self.assertTrue(os.path.exists(os.path.join(root, "config", "project.json")))
        self.assertTrue(os.path.exists(os.path.join(root, "config", "parameters.csv")))
        # 库的 -wal 伴生文件必须一起搬，否则库状态会不一致
        self.assertTrue(os.path.exists(os.path.join(root, "data", "history.db")))
        self.assertTrue(os.path.exists(os.path.join(root, "data", "history.db-wal")))
        self.assertTrue(
            os.path.exists(os.path.join(root, "别动我.txt")), "不认识的用户文件不要动"
        )
        # 再整理一次应当是空操作
        self.assertIsNone(project_store.upgrade_layout(root))

    def test_upgrade_layout_renames_chinese_subdirs(self):
        """老工程的中文子目录名（配置/数据/报表）就地改成英文。"""
        root = os.path.join(self.tmp.name, "中文子目录工程")
        os.makedirs(os.path.join(root, "配置"), exist_ok=True)
        os.makedirs(os.path.join(root, "数据"), exist_ok=True)
        os.makedirs(os.path.join(root, "报表"), exist_ok=True)
        project = project_io.sst_project()
        with open(os.path.join(root, "配置", "project.json"), "w", encoding="utf-8") as fh:
            json.dump(project.to_dict(), fh, ensure_ascii=False)
        with open(os.path.join(root, "数据", "history.db"), "w", encoding="utf-8") as fh:
            fh.write("db")

        # 改名前也能正常打开（认别名）
        opened, paths = project_store.open_project(root)
        self.assertEqual(opened.name, project.name)
        self.assertEqual(os.path.basename(paths.config_dir), "配置")
        self.assertEqual(paths.db_file, os.path.join(root, "数据", "history.db"))

        project_store.upgrade_layout(root)
        self.assertEqual(
            sorted(name for name in os.listdir(root) if os.path.isdir(os.path.join(root, name))),
            ["config", "data", "reports"],
        )
        after, paths_after = project_store.open_project(root)
        self.assertEqual(after.name, project.name)
        self.assertEqual(paths_after.config_dir, os.path.join(root, "config"))
        self.assertEqual(paths_after.db_file, os.path.join(root, "data", "history.db"))

    def test_upgrade_layout_ignores_already_organized(self):
        root = os.path.join(self.tmp.name, "新工程")
        paths = project_store.create_project_dir(root, project_io.sst_project())
        self.assertIsNone(project_store.upgrade_layout(root))
        self.assertTrue(os.path.exists(paths.project_file))

    def test_open_directory(self):
        root = os.path.join(self.tmp.name, "proj")
        project_store.create_project_dir(root, project_io.sst_project())

        project, paths = project_store.open_project(root)
        self.assertEqual(project.name, "SST 固态变压器")
        self.assertEqual(len(project.devices), 3)
        self.assertEqual(len(project.diagram.nodes), 8)
        self.assertEqual(paths.mode, "directory")
        self.assertFalse(paths.origin_iprj)

    def test_save_updates_project_file(self):
        root = os.path.join(self.tmp.name, "proj2")
        project = project_io.sst_project()
        paths = project_store.create_project_dir(root, project)

        project.name = "改过名的工程"
        project.devices[0].poll_interval_ms = 500
        project_store.save_project(project, paths)

        reopened, _ = project_store.open_project(root)
        self.assertEqual(reopened.name, "改过名的工程")
        self.assertEqual(reopened.devices[0].poll_interval_ms, 500)

    def test_csv_external_change_detected(self):
        root = os.path.join(self.tmp.name, "proj3")
        project = project_io.sst_project()
        paths = project_store.create_project_dir(root, project)

        self.assertFalse(csv_table.csv_differs_from_disk(project, paths.csv_file))

        # 模拟用户用 Excel 改过参数表
        with open(paths.csv_file, "a", encoding="utf-8-sig") as fh:
            fh.write("是,SST控制链,级联整流DSP,11,手工加的,手工加的,默认,holding,999,uint16,1,big,1,0,,2,否,是,0,card,0,100,否,0,否,,,,,Excel 手工添加\n")
        self.assertTrue(csv_table.csv_differs_from_disk(project, paths.csv_file))

        # 不覆盖时：文件保持用户改过的样子，且结果里标记出来
        result = project_store.save_project(project, paths, overwrite_external_csv=False)
        self.assertTrue(result.csv_external_changes)
        self.assertFalse(result.csv_overwritten)
        self.assertTrue(csv_table.csv_differs_from_disk(project, paths.csv_file))

        # 覆盖时：文件回到工程当前状态
        result = project_store.save_project(project, paths, overwrite_external_csv=True)
        self.assertTrue(result.csv_overwritten)
        self.assertFalse(csv_table.csv_differs_from_disk(project, paths.csv_file))


class ProjectHomeTest(unittest.TestCase):
    """工程仓库定位：软件目录只放软件，工程放在旁边的 VVPproject 里。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 造一个"软件目录"：<tmp>/VSCodeWorkspace/VVVPrefect-elec-Tools
        self.workspace = os.path.join(self.tmp.name, "VSCodeWorkspace")
        self.base = os.path.join(self.workspace, "VVVPrefect-elec-Tools")
        os.makedirs(self.base)
        # 查找会从"当前工作目录"再找一遍，这里切到临时目录，
        # 免得测试受真实仓库位置影响（本机上 SoftwareWorkspace/VVPproject 是真实存在的）
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.workspace)

    def test_creates_home_outside_software_dir(self):
        home, why = project_store.discover_project_home(self.base)
        self.assertEqual(home, os.path.join(self.workspace, "VVPproject"))
        self.assertFalse(
            home.startswith(self.base), "工程仓库不能在软件目录里面"
        )
        self.assertTrue(os.path.isdir(home))
        self.assertIn("新建", why)

    def test_reuses_existing_home_not_inside(self):
        """外面已经有仓库就用外面的，别在软件目录里另建一个。"""
        existing = os.path.join(self.workspace, "VVPproject")
        os.makedirs(existing)
        marker = os.path.join(existing, "SST 固态变压器", "配置")
        os.makedirs(marker)
        with open(os.path.join(marker, "project.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")

        home, why = project_store.discover_project_home(self.base)
        self.assertEqual(home, existing)
        self.assertIn("向外", why)
        self.assertEqual(
            project_store.list_projects(home), [os.path.join(existing, "SST 固态变压器")]
        )

    def test_prefers_existing_home_inside_when_no_outside(self):
        """便携式布置：只有软件目录里有，就用它。"""
        inside = os.path.join(self.base, "VVPproject")
        os.makedirs(inside)
        home, why = project_store.discover_project_home(self.base)
        self.assertEqual(home, inside)
        self.assertIn("程序目录里找到", why)

    def test_env_var_wins(self):
        custom = os.path.join(self.tmp.name, "自定义仓库")
        os.makedirs(custom)
        old = os.environ.get("VVPHMI_PROJECT_HOME")
        os.environ["VVPHMI_PROJECT_HOME"] = custom
        self.addCleanup(
            lambda: os.environ.pop("VVPHMI_PROJECT_HOME", None)
            if old is None
            else os.environ.__setitem__("VVPHMI_PROJECT_HOME", old)
        )
        home, why = project_store.discover_project_home(self.base)
        self.assertEqual(home, custom)
        self.assertIn("环境变量", why)

    def test_explicit_preferred_wins_over_env(self):
        explicit = os.path.join(self.tmp.name, "手填仓库")
        os.makedirs(explicit)
        home, why = project_store.discover_project_home(self.base, explicit)
        self.assertEqual(home, explicit)
        self.assertIn("设置", why)

    def test_list_projects_skips_system_dirs(self):
        home = os.path.join(self.workspace, "VVPproject")
        os.makedirs(os.path.join(home, project_store.OPENED_DIR, "包"), exist_ok=True)
        os.makedirs(os.path.join(home, "_本机归档"), exist_ok=True)
        os.makedirs(os.path.join(home, "不是工程"), exist_ok=True)
        good = os.path.join(home, "真工程", "配置")
        os.makedirs(good)
        with open(os.path.join(good, "project.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        self.assertEqual(
            [os.path.basename(p) for p in project_store.list_projects(home)], ["真工程"]
        )


class FolderNameTest(unittest.TestCase):
    """磁盘上的工程文件夹名一律英文，界面上的中文工程名照旧。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "VVPproject")
        os.makedirs(self.home)

    def test_slug_keeps_ascii_and_drops_non_ascii(self):
        self.assertEqual(project_store.folder_name("My Project 2"), "My-Project-2")
        self.assertEqual(project_store.folder_name("bess-storage"), "bess-storage")
        self.assertEqual(project_store.folder_name("通道1"), "1")
        self.assertEqual(project_store.folder_name("配电柜"), "project", "纯中文用兜底名")
        self.assertEqual(project_store.folder_name(""), "project")
        self.assertEqual(project_store.folder_name("  "), "project")

    def test_slug_blocks_windows_reserved_names(self):
        for name in ("CON", "con", "NUL", "COM1", "LPT9"):
            self.assertEqual(
                project_store.folder_name(name), "project", f"{name} 是 Windows 保留名"
            )

    def test_slug_replaces_path_separators(self):
        self.assertEqual(project_store.folder_name('a/b\\c:d*e?f"g<h>i|j'), "a-b-c-d-e-f-g-h-i-j")

    def test_slug_is_ascii_for_every_sample(self):
        for name, builder in project_io.BUILTIN_SAMPLES.items():
            with self.subTest(sample=name):
                project = builder()
                folder = project_store.project_folder_name(project)
                self.assertTrue(folder.isascii(), f"{folder} 应该是英文名")
                self.assertNotEqual(folder, "project", f"{name} 应该有像样的英文名")

    def test_samples_use_expected_english_names(self):
        self.assertEqual(
            project_store.project_folder_name(project_io.bess_project()), "bess-storage"
        )
        self.assertEqual(
            project_store.project_folder_name(project_io.sst_project()), "sst-transformer"
        )
        self.assertEqual(
            project_store.project_folder_name(project_io.sample_project()), "cabinet-monitor"
        )

    def test_declared_folder_name_wins(self):
        project = project_io.bess_project()
        project.folder_name = "my-plant"
        self.assertEqual(project_store.project_folder_name(project), "my-plant")

    def test_folder_name_survives_save_and_open(self):
        project = project_io.bess_project()
        project.folder_name = "plant-a"
        root = os.path.join(self.home, "plant-a")
        project_store.create_project_dir(root, project)

        again, _paths = project_store.open_project(root)
        self.assertEqual(again.folder_name, "plant-a", "文件夹名要跟着工程文件走")

    def test_suggest_avoids_collision(self):
        project = project_io.bess_project()
        os.makedirs(os.path.join(self.home, "bess-storage"))
        self.assertEqual(
            project_store.suggest_folder_name(self.home, project), "bess-storage-2"
        )

    def test_rename_project_dir(self):
        project = project_io.sst_project()
        root = os.path.join(self.home, "sst-transformer")
        paths = project_store.create_project_dir(root, project)

        target, note = project_store.rename_project_dir(paths, "shop-floor-1", self.home)
        self.assertEqual(os.path.basename(target), "shop-floor-1")
        self.assertIn("已改名", note)
        self.assertTrue(os.path.exists(os.path.join(target, "config", "project.json")))

    def test_rename_refuses_collision_and_keeps_original(self):
        project = project_io.sst_project()
        paths = project_store.create_project_dir(
            os.path.join(self.home, "sst-transformer"), project
        )
        os.makedirs(os.path.join(self.home, "taken"))
        target, note = project_store.rename_project_dir(paths, "taken", self.home)
        self.assertEqual(target, paths.root, "撞名时不该动原目录")
        self.assertIn("已经有一个", note)

    def test_rename_rejects_pure_chinese(self):
        project = project_io.sst_project()
        paths = project_store.create_project_dir(
            os.path.join(self.home, "sst-transformer"), project
        )
        target, note = project_store.rename_project_dir(paths, "配电柜", self.home)
        self.assertEqual(target, paths.root, "纯中文名不该被接受")
        self.assertTrue(note)

    def test_rename_same_name_is_noop(self):
        project = project_io.sst_project()
        paths = project_store.create_project_dir(
            os.path.join(self.home, "sst-transformer"), project
        )
        target, note = project_store.rename_project_dir(paths, "sst-transformer", self.home)
        self.assertEqual(target, paths.root)
        self.assertIn("没变", note)

    def test_opened_iprj_goes_to_ascii_dir(self):
        project = project_io.bess_project()
        paths = project_store.create_project_dir(
            os.path.join(self.home, "bess-storage"), project
        )
        packed = os.path.join(self.tmp.name, "包.iprj")
        project_store.pack_iprj(paths, packed)

        _project, opened = project_store.open_project(packed, workdir=self.home)
        relative = os.path.relpath(opened.root, self.home)
        self.assertTrue(
            relative.startswith(project_store.OPENED_DIR), f"应在 {project_store.OPENED_DIR}/ 下"
        )
        self.assertTrue(project_store.OPENED_DIR.isascii())
        self.assertEqual(os.path.basename(opened.config_dir), "config")


class IprjTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "proj")
        self.iprj = os.path.join(self.tmp.name, "SST.iprj")
        self.workdir = os.path.join(self.tmp.name, "work")

    def _make(self) -> tuple[Project, project_store.ProjectPaths]:
        project = project_io.sst_project()
        paths = project_store.create_project_dir(self.root, project)
        return project, paths

    def test_pack_creates_zip_with_expected_entries(self):
        _project, paths = self._make()
        project_store.pack_iprj(paths, self.iprj)

        self.assertTrue(os.path.exists(self.iprj))
        with zipfile.ZipFile(self.iprj) as zf:
            names = set(zf.namelist())
        self.assertIn("manifest.json", names)
        self.assertIn("project.json", names)
        self.assertIn("parameters.csv", names)

    def test_manifest_content(self):
        _project, paths = self._make()
        project_store.pack_iprj(paths, self.iprj)
        manifest = project_store.iprj_manifest(self.iprj)
        self.assertEqual(manifest["application"], APP_SHORT)
        self.assertEqual(manifest["format"], project_store.FORMAT_VERSION)

    def test_open_iprj_unpacks_and_remembers_origin(self):
        _project, paths = self._make()
        project_store.pack_iprj(paths, self.iprj)

        project, opened = project_store.open_project(self.iprj, workdir=self.workdir)
        self.assertEqual(project.name, "SST 固态变压器")
        self.assertEqual(opened.origin_iprj, os.path.abspath(self.iprj))
        self.assertTrue(os.path.exists(opened.project_file))
        self.assertTrue(opened.project_file.startswith(os.path.abspath(self.workdir)))

    def test_save_writes_back_into_iprj(self):
        _project, paths = self._make()
        project_store.pack_iprj(paths, self.iprj)

        project, opened = project_store.open_project(self.iprj, workdir=self.workdir)
        project.name = "改名后回写"
        result = project_store.save_project(project, opened)
        self.assertEqual(result.iprj, os.path.abspath(self.iprj))

        # 重新打开 .iprj 应看到新名字
        again, _ = project_store.open_project(self.iprj, workdir=self.workdir)
        self.assertEqual(again.name, "改名后回写")

    def test_unpack_removes_stale_files(self):
        _project, paths = self._make()
        project_store.pack_iprj(paths, self.iprj)

        target = os.path.join(self.workdir, "target")
        os.makedirs(target, exist_ok=True)
        stale = os.path.join(target, "上一版残留.txt")
        with open(stale, "w", encoding="utf-8") as fh:
            fh.write("x")

        project_store.unpack_iprj(self.iprj, target)
        self.assertFalse(os.path.exists(stale), "解压前应清空目标目录，避免残留旧文件")

    def test_invalid_iprj_rejected(self):
        bad = os.path.join(self.tmp.name, "bad.iprj")
        with zipfile.ZipFile(bad, "w") as zf:
            zf.writestr("something.txt", "hello")
        with self.assertRaises(ValueError):
            project_store.unpack_iprj(bad, os.path.join(self.workdir, "bad"))

    def test_missing_iprj_raises(self):
        with self.assertRaises(FileNotFoundError):
            project_store.open_project(os.path.join(self.tmp.name, "nope.iprj"))

    def test_roundtrip_preserves_csv_and_db_paths(self):
        """工程包里的参数表路径设置要能带回来（松散模式也要能存进工程）。"""
        loose_db = os.path.join(self.root, "plant", "history.db")
        project = project_io.sst_project()
        project.storage = ProjectStorage(
            db_in_project=False, db_path=loose_db,
            csv_in_project=True, csv_path="parameters.csv",
        )
        paths = project_store.create_project_dir(self.root, project)
        project_store.pack_iprj(paths, self.iprj)

        again, opened = project_store.open_project(self.iprj, workdir=self.workdir)
        self.assertFalse(again.storage.db_in_project)
        self.assertEqual(again.storage.db_path, loose_db)
        self.assertEqual(opened.mode, "loose")
        self.assertEqual(opened.db_file, os.path.normpath(loose_db))


if __name__ == "__main__":
    unittest.main()
