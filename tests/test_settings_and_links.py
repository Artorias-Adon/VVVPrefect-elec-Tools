"""软件设置与通道连接参数（RTU/TCP）测试。

两件事都跟"配置要记得住"有关：

* 软件设置写在 ``data/settings.json``，换工程也要生效，且文件坏了不能把程序带崩；
* 通道的 RTU 与 TCP 参数要各记一套，现场在串口与网关之间来回切时不用重填。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from icomm.core import csv_table, project as project_io, project_store
from icomm.core import settings as app_settings
from icomm.core.drivers import default_options
from icomm.core.models import ChannelDef, Project, ProjectStorage
from icomm.core.settings import AppSettings


class AppSettingsTest(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors：Windows 上临时目录里只要还有句柄没释放就删不掉（CI 偶发）
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name

    def test_defaults_when_file_missing(self):
        settings = app_settings.load_settings(self.base)
        self.assertIsInstance(settings, AppSettings)
        self.assertGreater(settings.periodic_default_interval_ms, 0)
        self.assertFalse(settings.auto_start_engine, "默认不该自动开始采集")

    def test_roundtrip(self):
        settings = AppSettings(
            default_sample="SST 固态变压器",
            auto_start_simulator=True,
            reconnect_interval_s=1.5,
            write_timeout_s=2.5,
            periodic_default_interval_ms=250,
            periodic_confirm=False,
            scope_timebase_s=10.0,
            scope_show_all_points=True,
            table_rows_limit=200,
            archive_auto_purge=True,
        )
        path = app_settings.save_settings(settings, self.base)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(path, app_settings.settings_path(self.base))

        loaded = app_settings.load_settings(self.base)
        for field in (
            "default_sample",
            "auto_start_simulator",
            "scope_timebase_s",
            "scope_show_all_points",
            "archive_auto_purge",
        ):
            self.assertEqual(getattr(loaded, field), getattr(settings, field), field)
        self.assertEqual(loaded.reconnect_interval_s, 1.5)
        self.assertEqual(loaded.write_timeout_s, 2.5)
        self.assertEqual(loaded.periodic_default_interval_ms, 250)
        self.assertFalse(loaded.periodic_confirm)

    def test_recent_projects_roundtrip_and_dedupe(self):
        """最近工程要记得住、去掉重复、最多留 8 个。"""
        paths = [os.path.join(self.base, f"p{index}", "config", "project.json") for index in range(10)]
        settings = AppSettings(
            last_project=paths[0],
            recent_projects=[paths[0], paths[1], paths[0], *paths[2:]],
        )
        app_settings.save_settings(settings, self.base)
        loaded = app_settings.load_settings(self.base)
        self.assertEqual(loaded.last_project, paths[0])
        self.assertEqual(len(loaded.recent_projects), 8, loaded.recent_projects)
        self.assertEqual(loaded.recent_projects[0], paths[0])
        self.assertEqual(len(set(loaded.recent_projects)), 8, "重复的路径要去掉")

    def test_export_location_defaults_to_project(self):
        settings = AppSettings.from_dict({"export_location": "乱填的"})
        self.assertEqual(settings.export_location, "project")
        self.assertEqual(AppSettings().custom_export_directory, "")

    def test_custom_export_without_directory_falls_back(self):
        """选了「自选目录」却没填路径时，不能把文件写到空路径上。"""
        settings = AppSettings.from_dict(
            {"export_location": "custom", "custom_export_directory": "   "}
        )
        self.assertEqual(settings.export_location, "project")

    def test_recent_projects_bad_shapes_are_dropped(self):
        good = os.path.join(self.base, "ok", "config", "project.json")
        settings = AppSettings.from_dict(
            {"recent_projects": ["", None, 42, good]}
        )
        self.assertEqual(settings.recent_projects, [good])
        self.assertEqual(AppSettings.from_dict({"recent_projects": "不是列表"}).recent_projects, [])
        self.assertEqual(AppSettings.from_dict({"last_project": 123}).last_project, "")

    def test_broken_file_falls_back_to_defaults(self):
        """配置文件坏了也要能起来，不能把程序带崩。"""
        path = app_settings.settings_path(self.base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ 这不是 JSON")
        self.assertEqual(app_settings.load_settings(self.base), AppSettings())

        with open(path, "w", encoding="utf-8") as fh:
            json.dump([1, 2, 3], fh)
        self.assertEqual(
            app_settings.load_settings(self.base), AppSettings(), "顶层不是对象时也该回落"
        )

    def test_unknown_keys_ignored(self):
        """以后删掉某个字段时，旧配置文件不该让程序起不来。"""
        path = app_settings.settings_path(self.base)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"scope_timebase_s": 2.0, "字段已经删了": 123}, fh, ensure_ascii=False)
        settings = app_settings.load_settings(self.base)
        self.assertEqual(settings.scope_timebase_s, 2.0)

    def test_clamp_bad_numbers(self):
        settings = AppSettings.from_dict(
            {
                "reconnect_interval_s": -5,
                "write_timeout_s": "不是数字",
                "periodic_default_interval_ms": 1,
                "table_rows_limit": 0,
                "trend_max_points": 10 ** 9,
                "monitor_frames_limit": None,
            }
        )
        self.assertGreaterEqual(settings.reconnect_interval_s, 0.2)
        self.assertGreaterEqual(settings.write_timeout_s, 0.2)
        self.assertGreaterEqual(
            settings.periodic_default_interval_ms, app_settings.PERIODIC_MIN_MS
        )
        self.assertGreaterEqual(settings.table_rows_limit, 20)
        self.assertLessEqual(settings.trend_max_points, 200_000)
        self.assertGreaterEqual(settings.monitor_frames_limit, 100)

    def test_save_is_atomic_enough(self):
        """保存不该留下 .tmp 残file，否则下次读到半个文件。"""
        app_settings.save_settings(AppSettings(), self.base)
        directory = os.path.dirname(app_settings.settings_path(self.base))
        self.assertEqual(
            [n for n in os.listdir(directory) if n.endswith(".tmp")], []
        )


class ChannelLinkOptionsTest(unittest.TestCase):
    """通道的连接参数要在 RTU 与 TCP 之间各记一套。"""

    def make_channel(self) -> ChannelDef:
        channel = ChannelDef(name="整流器DSP", driver="modbus", link="tcp")
        channel.options = dict(default_options("modbus", "tcp"))
        channel.options["host"] = "192.168.1.50"
        channel.options["port"] = 5021
        channel.remember_options()
        return channel

    def test_switch_keeps_each_link_options(self):
        channel = self.make_channel()

        channel.switch_link("rtu", default_options("modbus", "rtu"))
        self.assertEqual(channel.link, "rtu")
        self.assertEqual(channel.options["port"], "COM1", "首次切到 rtu 该用驱动默认值")
        channel.options["port"] = "COM7"
        channel.options["baudrate"] = 19200
        channel.remember_options()

        # 切回 tcp：TCP 那套参数要原样还在
        channel.switch_link("tcp", default_options("modbus", "tcp"))
        self.assertEqual(channel.link, "tcp")
        self.assertEqual(channel.options["host"], "192.168.1.50")
        self.assertEqual(channel.options["port"], 5021)

        # 再切回 rtu：串口号与波特率也要记得
        channel.switch_link("rtu", default_options("modbus", "rtu"))
        self.assertEqual(channel.options["port"], "COM7")
        self.assertEqual(channel.options["baudrate"], 19200)

    def test_switch_same_link_is_noop(self):
        channel = self.make_channel()
        channel.options["port"] = 5022
        channel.switch_link("tcp", default_options("modbus", "tcp"))
        self.assertEqual(channel.options["port"], 5022, "切到同一链路不该重置参数")

    def test_options_by_link_survives_roundtrip(self):
        channel = self.make_channel()
        channel.switch_link("rtu", default_options("modbus", "rtu"))
        channel.options["port"] = "COM3"
        channel.remember_options()

        restored = ChannelDef.from_dict(channel.to_dict())
        self.assertEqual(restored.link, "rtu")
        self.assertEqual(restored.options["port"], "COM3")
        # 另一套（TCP）也还在
        restored.switch_link("tcp", default_options("modbus", "tcp"))
        self.assertEqual(restored.options["host"], "192.168.1.50")
        self.assertEqual(restored.options["port"], 5021)

    def test_legacy_project_without_options_by_link(self):
        """旧工程文件里没有 options_by_link：当前这套参数要归到当前链路名下。"""
        legacy = {
            "name": "旧工程",
            "channels": [
                {
                    "name": "1号网关",
                    "driver": "modbus",
                    "link": "rtu",
                    "options": {"port": "COM9", "baudrate": 9600, "parity": "E"},
                }
            ],
            "devices": [],
        }
        project = Project.from_dict(legacy)
        channel = project.channels[0]
        self.assertEqual(channel.options["port"], "COM9")
        self.assertEqual(channel.options_by_link["rtu"]["port"], "COM9")

        channel.switch_link("tcp", default_options("modbus", "tcp"))
        channel.switch_link("rtu", default_options("modbus", "rtu"))
        self.assertEqual(channel.options["port"], "COM9", "切回 rtu 应恢复旧参数")
        self.assertEqual(channel.options["parity"], "E")

    def test_switching_to_rtu_never_reuses_tcp_port(self):
        """回归：tcp 的 int 端口不能被串口驱动当串口名用。"""
        channel = self.make_channel()
        channel.switch_link("rtu", default_options("modbus", "rtu"))
        self.assertIsInstance(channel.options["port"], str)
        self.assertNotEqual(channel.options["port"], 5021)

    def test_project_file_keeps_both_link_options(self):
        """存盘再读，两套参数都还在 —— 工程确实"能保存为 RTU 或 TCP"。"""
        project = project_io.bess_project()
        channel = project.channels[0]
        channel.switch_link("rtu", default_options("modbus", "rtu"))
        channel.options["port"] = "COM11"
        channel.options["baudrate"] = 115200
        channel.remember_options()

        with tempfile.TemporaryDirectory() as tmp:
            paths = project_store.create_project_dir(
                os.path.join(tmp, "工程"), project
            )
            project_store.save_project(project, paths, export_csv=False)
            loaded, _paths = project_store.open_project(paths.project_file)

        restored = loaded.channel(channel.name)
        self.assertIsNotNone(restored)
        self.assertEqual(restored.link, "rtu")
        self.assertEqual(restored.options["port"], "COM11")
        self.assertEqual(restored.options["baudrate"], 115200)
        restored.switch_link("tcp", default_options("modbus", "tcp"))
        self.assertEqual(restored.options["port"], 5020, "TCP 端口该是储能示例里配的那个")


class ProjectSettingsTest(unittest.TestCase):
    """工程级设置（归档/保留/存放路径/持续发送配置）的读写。"""

    def test_project_fields_roundtrip(self):
        project = project_io.bess_project()
        project.name = "改造后的工程"
        project.history_interval_s = 3
        project.history_retention_days = 180
        project.storage = ProjectStorage(
            db_in_project=False,
            db_path=r"D:\data\plant.db",
            csv_in_project=True,
            csv_path="parameters.csv",
        )
        restored = Project.from_dict(project.to_dict())
        self.assertEqual(restored.name, "改造后的工程")
        self.assertEqual(restored.history_interval_s, 3)
        self.assertEqual(restored.history_retention_days, 180)
        self.assertFalse(restored.storage.db_in_project)
        self.assertEqual(restored.storage.db_path, r"D:\data\plant.db")

    def test_periodic_write_helpers(self):
        project = project_io.bess_project()
        self.assertEqual(project.periodic_writes, [])
        self.assertIsNone(project.periodic_write("X.Y"))

        project.set_periodic_write("储能变流器PCS.有功功率给定", 720.0, 500)
        entry = project.periodic_write("储能变流器PCS.有功功率给定")
        self.assertEqual(entry.value, 720.0)
        self.assertEqual(entry.interval_ms, 500)

        # 同一点位只保留最新一条
        project.set_periodic_write("储能变流器PCS.有功功率给定", 730.0, 200)
        self.assertEqual(len(project.periodic_writes), 1)
        self.assertEqual(project.periodic_write("储能变流器PCS.有功功率给定").value, 730.0)

        # 周期下限保护：太短的周期在 Modbus 上根本发不出来
        project.set_periodic_write("储能变流器PCS.有功功率给定", 730.0, 1)
        self.assertEqual(project.periodic_write("储能变流器PCS.有功功率给定").interval_ms, 50)

        self.assertTrue(project.clear_periodic_write("储能变流器PCS.有功功率给定"))
        self.assertFalse(project.clear_periodic_write("储能变流器PCS.有功功率给定"))
        self.assertEqual(project.periodic_writes, [])

    def test_periodic_writes_not_exported_to_csv(self):
        """CSV 参数表是"点位定义"，不该混进持续发送这类运行配置。"""
        project = project_io.bess_project()
        project.set_periodic_write("储能变流器PCS.有功功率给定", 700.0, 1000)
        columns = csv_table.CSV_COLUMNS
        self.assertNotIn("持续发送", columns)
        self.assertNotIn("周期", columns)


if __name__ == "__main__":
    unittest.main()
