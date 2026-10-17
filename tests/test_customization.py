"""界面外观自定义、保存格式细分、通信配置页、按键控制面板的测试。

都是这轮新增的东西，且都容易"看起来对但实际没接上"：
* 外观改了要能立刻反映到样式表与模块级颜色常量上，取消要能还原；
* 保存格式要靠扩展名正确分派，JSON 要能原样读回；
* 通信页改通道要真的改到工程上（含改名时设备跟着搬）；
* 按键面板的按键要真发出正确的写入值。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QToolBar, QToolButton  # noqa: E402

from icomm import APP_SHORT  # noqa: E402
from icomm.core import project as project_io  # noqa: E402
from icomm.core import csv_table, project_store, writers  # noqa: E402
from icomm.core.models import Project  # noqa: E402
from icomm.core.settings import AppSettings  # noqa: E402
from icomm.core import settings as app_settings  # noqa: E402
from icomm.core.tags import TagValue  # noqa: E402
from icomm.ui import theme  # noqa: E402
from icomm.ui.main_window import NAV_ITEMS  # noqa: E402
from icomm.ui.dialogs.appearance_editor import ColorButton, AppearanceEditor  # noqa: E402
from icomm.ui.dialogs.channel_editor import ChannelEditor  # noqa: E402
from icomm.ui.dialogs.control_panel import (  # noqa: E402
    DeviceControlPanel,
    PointControlPanel,
    close_all,
)
from icomm.ui.dialogs.save_as_dialog import SaveAsDialog  # noqa: E402
from icomm.ui.dialogs.settings_dialog import SettingsDialog  # noqa: E402

_app: QApplication | None = None

PCS = "储能变流器PCS"
CTRL_PID = f"{PCS}.控制字"
CTRL_POINT = "控制字"
FUNC_PID = f"{PCS}.功能配置字"
BUS_PID = f"{PCS}.有功功率给定"

DEFAULT_APPEARANCE = theme.Appearance()


def _column_of(line: str, needle: str) -> int:
    """按显示宽度算 needle 在行里的起始列（中文算两格），用来验证 TXT 对齐。"""
    position = line.find(needle)
    if position < 0:
        return -1
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in line[:position])


def setUpModule() -> None:
    global _app
    _app = QApplication.instance() or QApplication([])


def tearDownModule() -> None:
    """把浮动控制面板收干净。

    它们是模块级全局（``control_panel._open_panels``）且是 Qt.Tool 窗口，
    跨测试类残留下来会在解释器退出时抢在 QApplication 之后析构而崩溃。
    """
    from icomm.ui.dialogs import control_panel

    control_panel.close_all()
    if _app is not None:
        _app.processEvents()


class PresetThemeTest(unittest.TestCase):
    """内置配色：默认是「天蓝」；另外两套深浅蓝调是自己调的色值，别被顺手改掉。"""

    def setUp(self):
        self.addCleanup(lambda: theme.apply_preset(theme.DEFAULT_PRESET))

    def test_default_preset_is_sky_blue(self):
        self.assertEqual(theme.DEFAULT_PRESET, "天蓝")
        self.assertIn(theme.DEFAULT_PRESET, theme.PRESETS)
        self.assertEqual(theme.Palette().name, theme.DEFAULT_PRESET)
        self.assertEqual(theme.Appearance().palette.name, theme.DEFAULT_PRESET)

    def test_sky_blue_is_default_colours(self):
        """默认主题是深色打底 + 天蓝强调色，且默认值就是它（改默认值要一起改这里）。"""
        sky = theme.PRESETS["天蓝"]
        self.assertEqual(sky.name, "天蓝")
        self.assertEqual(sky.bg, theme.Palette().bg)
        self.assertEqual(sky.accent, theme.Palette().accent)
        # 强调色是"天蓝"：蓝分量最高、绿分量居中偏高（#38bdf8）
        red, green, blue = (
            int(sky.accent[1:3], 16),
            int(sky.accent[3:5], 16),
            int(sky.accent[5:7], 16),
        )
        self.assertGreater(blue, green, "天蓝的蓝分量应该最高")
        self.assertGreater(green, red, "天蓝的绿分量应该高于红分量")
        # 深色打底：背景比文字暗得多
        self.assertLess(int(sky.bg[1:3], 16), 60)
        self.assertGreater(int(sky.text[1:3], 16), 200)

    def test_dark_blue_preset_anchors(self):
        """「纯黑蓝调」这几个锚点色是定死的，改了界面观感会变。"""
        dark = theme.PRESETS["纯黑蓝调"]
        self.assertEqual(dark.bg, "#000000")
        self.assertEqual(dark.text, "#f5f5f7")
        self.assertEqual(dark.text_dim, "#adb2b8")
        self.assertEqual(dark.accent, "#5686fe")
        self.assertEqual(dark.accent_alt, "#4d6bfe")
        self.assertEqual(dark.quality_good, "#22c55e")
        self.assertEqual(dark.quality_stale, "#f97316")

        light = theme.PRESETS["纯白蓝调"]
        self.assertEqual(light.bg, "#ffffff")
        self.assertEqual(light.text, "#1d1d1f")
        self.assertEqual(light.text_dim, "#81858c")
        self.assertEqual(light.accent, "#3964fe")

    def test_old_preset_names_still_work(self):
        """改过名：老 settings.json 里的旧名字要能自动认成新名字（配色不变）。"""
        for old, new in theme.PRESET_ALIASES.items():
            with self.subTest(old=old):
                self.assertEqual(theme.resolve_preset(old), new)
                self.assertIn(new, theme.PRESETS)
                theme.apply_preset(old)  # 旧名字不该抛错
                self.assertEqual(theme.PRESETS[new].bg, theme.BG)

    def test_preset_names_have_no_third_party_brand(self):
        """预设名里不许出现第三方产品名（开源前的品牌合规检查）。"""
        for name in theme.preset_names():
            with self.subTest(preset=name):
                self.assertNotIn("DeepSeek", name)
                self.assertNotIn("deepseek", name.lower())

    def test_light_variant_is_actually_light(self):
        light = theme.PRESETS["纯白蓝调"]
        bg = int(light.bg[1:3], 16)
        text = int(light.text[1:3], 16)
        self.assertGreater(bg, 200, "浅色主题的背景应该是浅的")
        self.assertLess(text, 100, "浅色主题的文字应该是深的")

    def test_legacy_presets_kept(self):
        """老主题要还在，用户能切回去。"""
        for name in ("深色工业", "深蓝夜间", "浅色办公", "高对比现场"):
            with self.subTest(preset=name):
                self.assertIn(name, theme.PRESETS)

    def test_legacy_industrial_preset_unchanged(self):
        """「深色工业」是独立的预设，不能因为改了字段默认值就跟着变。"""
        legacy = theme.PRESETS["深色工业"]
        self.assertEqual(legacy.bg, "#1b1f24")
        self.assertEqual(legacy.accent, "#2f9e6b")
        self.assertNotEqual(legacy.bg, theme.Palette().bg)

    def test_every_preset_is_complete_and_valid(self):
        for name, preset in theme.PRESETS.items():
            with self.subTest(preset=name):
                self.assertEqual(preset.name, name, "预设的 name 要和键一致")
                self.assertEqual(preset.normalize(), [], "预设里不该有非法颜色")
                self.assertGreaterEqual(len(preset.series), 4, "曲线配色太少不够用")

    def test_applying_preset_updates_module_constants(self):
        theme.apply_preset("纯白蓝调")
        self.assertEqual(theme.BG, "#ffffff")
        self.assertEqual(theme.ACCENT, "#3964fe")
        theme.apply_preset("纯黑蓝调")
        self.assertEqual(theme.BG, "#000000")
        self.assertEqual(theme.ACCENT, "#5686fe")


class AppearanceTest(unittest.TestCase):
    def setUp(self):
        self._before = theme.appearance()
        self.addCleanup(self._restore)

    def _restore(self):
        theme.apply(self._before)
        close_all()
        _app.processEvents()

    def test_presets_all_valid(self):
        for name in theme.preset_names():
            self.assertTrue(name)
            palette = theme.PRESETS[name]
            problems = palette.copy().normalize()
            self.assertEqual(problems, [], f"预设「{name}」有非法颜色：{problems}")

    def test_apply_changes_stylesheet_and_constants(self):
        theme.apply_preset("浅色办公")
        self.assertEqual(theme.BG, "#f2f4f7")
        self.assertIn("#f2f4f7", theme.STYLESHEET)
        self.assertEqual(theme.ALARM_LEVELS["info"], "#1f6feb")

        theme.apply_preset("高对比现场")
        self.assertEqual(theme.BG, "#000000")
        self.assertIn("#000000", theme.STYLESHEET)

    def test_font_size_and_density_change_paddings(self):
        compact = theme.Appearance(font_size=11, density="紧凑")
        theme.apply(compact)
        small_qss = theme.STYLESHEET
        self.assertIn("font-size: 11px", small_qss)

        loose = theme.Appearance(font_size=16, density="宽松")
        theme.apply(loose)
        self.assertIn("font-size: 16px", theme.STYLESHEET)
        # 宽松的按钮内边距应该比紧凑大
        self.assertGreater(loose.px(6), compact.px(6))

    def test_illegal_colour_is_repairable_not_fatal(self):
        bad = theme.Appearance(palette=theme.Palette.from_dict({"bg": "不是颜色"}))
        problems = theme.apply(bad)
        self.assertIn("bg", problems)
        self.assertEqual(
            theme.BG, theme.Palette().bg, "坏颜色要退回默认，不能把界面搞黑"
        )

    def test_strict_mode_raises(self):
        with self.assertRaises(ValueError):
            theme.apply(
                theme.Appearance(palette=theme.Palette.from_dict({"accent": "x"})),
                strict=True,
            )

    def test_series_colour_cycles(self):
        theme.apply_preset("深色工业")
        count = len(theme.SERIES_COLORS)
        self.assertEqual(theme.series_color(0), theme.SERIES_COLORS[0])
        self.assertEqual(theme.series_color(count), theme.SERIES_COLORS[0])

    def test_appearance_survives_settings_roundtrip(self):
        settings = AppSettings(appearance=theme.Appearance(font_size=15, density="宽松").to_dict())
        restored = AppSettings.from_dict(settings.to_dict())
        appearance = theme.Appearance.from_dict(restored.appearance)
        self.assertEqual(appearance.font_size, 15)
        self.assertEqual(appearance.density, "宽松")

    def test_editor_emits_and_applies(self):
        editor = AppearanceEditor(theme.Appearance())
        seen: list = []
        editor.changed.connect(seen.append)
        editor.size_box.setValue(15)
        self.assertTrue(seen, "字号改动应发出 changed 以便实时预览")
        self.assertEqual(editor.current_appearance().font_size, 15)

    def test_editor_preset_switch_keeps_font(self):
        editor = AppearanceEditor(theme.Appearance(font_size=17))
        index = editor.preset_combo.findData("浅色办公")
        editor.preset_combo.setCurrentIndex(index)
        appearance = editor.current_appearance()
        self.assertEqual(appearance.palette.bg, "#f2f4f7")
        self.assertEqual(appearance.font_size, 17, "换主题不该把字号也改掉")

    def test_colour_button_reports_change(self):
        button = ColorButton("accent", "#123456")
        self.assertEqual(button.colour, "#123456")
        seen: list[str] = []
        button.colorChanged.connect(seen.append)
        button.set_colour("#654321")
        button.colorChanged.emit(button.colour)  # 直接触发（不弹系统取色器）
        self.assertEqual(seen, ["#654321"])

    def test_every_palette_field_has_an_editor(self):
        """高级页要覆盖调色板的每一个颜色，否则有项改不了。"""
        editor = AppearanceEditor(theme.Appearance())
        expected = {
            spec.name
            for spec in theme.fields(theme.Palette)
            if spec.name not in ("name", "series")
        }
        self.assertEqual(set(editor._colour_buttons), expected)


class SaveFormatTest(unittest.TestCase):
    def setUp(self):
        # ignore_cleanup_errors：Windows 上临时目录里只要还有句柄没释放就删不掉（CI 偶发）
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.project = project_io.bess_project()
        #: 第一台设备（PCS）的点位数 —— 单设备口径的断言用它
        self.device_points = len(self.project.devices[0].points)
        #: 工程全部点位数（PCS + BMS）—— 导出/回读的行数用它
        self.total_points = len(self.project.all_point_ids())

    def test_registry_order_and_defaults(self):
        labels = [w.label for w in writers.parameter_writers()]
        self.assertEqual(labels[0], "CSV 参数表", "CSV 应是默认格式")
        self.assertEqual(writers.default_parameter_writer().label, "CSV 参数表")
        self.assertEqual(
            [w.suffix for w in writers.parameter_writers()], [".csv", ".txt", ".json"]
        )
        self.assertEqual(
            [w.label for w in writers.project_writers()], ["工程目录", ".iprj 工程包"]
        )

    def test_duplicate_registration_ignored(self):
        before = len(writers.parameter_writers())
        writers.register_parameter_writer(writers.CsvParameterWriter())
        self.assertEqual(len(writers.parameter_writers()), before)

    def test_dispatch_by_suffix(self):
        for suffix, label in (
            (".csv", "CSV 参数表"),
            (".CSV", "CSV 参数表"),
            (".json", "JSON 结构化"),
            (".txt", "TXT 对齐文本"),
        ):
            found = writers.parameter_writer_for("x" + suffix)
            self.assertIsNotNone(found, suffix)
            self.assertEqual(found.label, label)
        self.assertIsNone(writers.parameter_writer_for("x.xlsx"))
        self.assertEqual(
            writers.ensure_writer(self.project, "x.xlsx").label, "CSV 参数表"
        )

    def test_all_formats_export_every_point(self):
        for writer in writers.parameter_writers():
            path = os.path.join(self.tmp.name, "p" + writer.suffix)
            count = writer.write(self.project, path)
            self.assertEqual(count, self.total_points, writer.label)
            self.assertTrue(os.path.getsize(path) > 0)
            with open(path, "rb") as fh:
                head = fh.read(3)
            if writer.suffix == ".csv":
                self.assertEqual(head, b"\xef\xbb\xbf", "CSV 要带 BOM 供 Excel 识别")

    def test_json_roundtrip_preserves_definitions(self):
        path = os.path.join(self.tmp.name, "p.json")
        writers.JsonParameterWriter().write(self.project, path)

        target = Project(name="回读")
        report = writers.JsonParameterWriter().read(target, path)
        self.assertTrue(report.ok, report.issues)
        self.assertEqual(report.points_added, self.total_points)

        device = target.device(PCS)
        control = device.point(CTRL_POINT)
        self.assertEqual(control.address, 17)
        self.assertEqual(len(control.command_items()), 5)
        self.assertEqual(device.point("运行状态字").bit_labels()[5], (5, "维护"))
        self.assertEqual(device.point("日充电量").data_type, "uint32")
        self.assertEqual(device.point("告警字").bit_labels()[15], (15, "BMS告警"))

    def test_json_roundtrip_matches_csv_result(self):
        """两种格式读回来应该得到一样的点位定义（共用一套校验）。"""
        csv_path = os.path.join(self.tmp.name, "p.csv")
        json_path = os.path.join(self.tmp.name, "p.json")
        writers.CsvParameterWriter().write(self.project, csv_path)
        writers.JsonParameterWriter().write(self.project, json_path)

        from_csv, from_json = Project(name="a"), Project(name="b")
        writers.CsvParameterWriter().read(from_csv, csv_path)
        writers.JsonParameterWriter().read(from_json, json_path)

        self.assertEqual(
            [d.to_dict() for d in from_csv.devices],
            [d.to_dict() for d in from_json.devices],
        )

    def test_json_replace_mode(self):
        path = os.path.join(self.tmp.name, "p.json")
        writers.JsonParameterWriter().write(self.project, path)
        report = writers.JsonParameterWriter().read(self.project, path, mode="replace")
        self.assertTrue(report.ok)
        self.assertEqual(
            len(self.project.devices[0].points), self.device_points, "替换模式不该翻倍"
        )

    def test_txt_is_export_only(self):
        writer = writers.TxtParameterWriter()
        self.assertFalse(writer.supports_read())
        path = os.path.join(self.tmp.name, "p.txt")
        writer.write(self.project, path)
        self.assertIsNone(writer.read(self.project, path))

    def test_txt_content_is_readable(self):
        path = os.path.join(self.tmp.name, "p.txt")
        writers.TxtParameterWriter().write(self.project, path)
        with open(path, "r", encoding="utf-8-sig") as fh:
            lines = [line.rstrip("\n") for line in fh]
        self.assertIn(f"{APP_SHORT} 参数表", lines[0])
        self.assertIn("点号", lines[1], "第一行应是表头")
        self.assertIn("位定义", lines[1], "位定义列也该在 TXT 里")
        body = [line for line in lines if PCS in line]
        self.assertEqual(len(body), self.device_points)
        self.assertTrue(
            any("控制字" in line and "0x01:开机" in line for line in body),
            "TXT 里应能看到控制字的命令定义",
        )

    def test_txt_columns_are_aligned(self):
        """TXT 的价值在于列对齐（贴文档/打印），所以中文要按两字宽补齐。"""
        path = os.path.join(self.tmp.name, "p.txt")
        writers.TxtParameterWriter().write(self.project, path)
        with open(path, "r", encoding="utf-8-sig") as fh:
            lines = [line.rstrip("\n") for line in fh]
        # 表头与第一条数据行的"点号"列应该起在同一列
        header = lines[1]
        first_point = self.project.devices[0].points[0].name
        first = next(line for line in lines if first_point in line)
        self.assertEqual(_column_of(header, "点号"), _column_of(first, first_point))

    def test_bad_json_reports_error(self):
        path = os.path.join(self.tmp.name, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"format": "icomm-parameters"}, fh)
        with self.assertRaises(ValueError):
            writers.JsonParameterWriter().read(Project(name="x"), path)

    def test_project_dir_writer_creates_files(self):
        target = os.path.join(self.tmp.name, "新工程")
        paths = writers.ProjectDirWriter().write(self.project, None, target)
        self.assertTrue(os.path.exists(paths.project_file))
        self.assertTrue(os.path.exists(paths.csv_file))
        opened, _ = project_store.open_project(paths.project_file)
        self.assertEqual(len(opened.devices[0].points), self.device_points)

    def test_iprj_writer_roundtrip(self):
        working = project_store.create_in_workdir(self.project, self.tmp.name)
        target = os.path.join(self.tmp.name, "包.iprj")
        packed = writers.IprjWriter().write(self.project, working, target)
        self.assertTrue(os.path.exists(packed))
        opened, _ = project_store.open_project(packed, workdir=os.path.join(self.tmp.name, "解"))
        self.assertEqual(len(opened.devices[0].points), self.device_points)
        self.assertEqual(
            len(opened.device(PCS).point(CTRL_POINT).command_items()), 5
        )

    def test_save_as_dialog_switches_suffix(self):
        dialog = SaveAsDialog("保存参数设置", writers.parameter_writers(), default_name="表")
        dialog.path_edit.setText(os.path.join(self.tmp.name, "表.csv"))
        dialog._on_format_changed()
        index = dialog.format_combo.findData(
            next(w for w in writers.parameter_writers() if w.suffix == ".json")
        )
        dialog.format_combo.setCurrentIndex(index)
        self.assertTrue(
            dialog.path_edit.text().endswith(".json"),
            f"换格式后扩展名要跟着变：{dialog.path_edit.text()}",
        )

    def test_save_as_dialog_appends_missing_suffix(self):
        dialog = SaveAsDialog("保存参数设置", writers.parameter_writers(), default_name="表")
        dialog.path_edit.setText(os.path.join(self.tmp.name, "无扩展名"))
        dialog._accept()
        self.assertTrue(dialog.target_path().endswith(".csv"), dialog.target_path())

    def test_save_as_dialog_project_dir_keeps_path(self):
        dialog = SaveAsDialog("保存整个工程", writers.project_writers(), default_name="工程")
        # 用注册表里的那个实例（写入器没有自定义 __eq__，findData 是按身份比的）
        registered = writers.project_writers()[0]
        self.assertIsInstance(registered, writers.ProjectDirWriter)
        dialog.format_combo.setCurrentIndex(dialog.format_combo.findData(registered))
        raw = os.path.join(self.tmp.name, "目录工程")
        dialog.path_edit.setText(raw)
        dialog._accept()
        self.assertEqual(dialog.target_path(), raw, "工程目录格式不该被追加扩展名")

    def test_save_as_dialog_iprj_appends_suffix(self):
        dialog = SaveAsDialog("保存整个工程", writers.project_writers(), default_name="工程")
        iprj_writer = next(w for w in writers.project_writers() if w.suffix)
        dialog.format_combo.setCurrentIndex(dialog.format_combo.findData(iprj_writer))
        self.assertTrue(dialog.format_combo.currentData().single_file)
        dialog.path_edit.setText(os.path.join(self.tmp.name, "工程包"))
        dialog._accept()
        self.assertTrue(
            dialog.target_path().endswith(project_store.IPRJ_SUFFIX), dialog.target_path()
        )


class ChannelEditorTest(unittest.TestCase):
    def setUp(self):
        self.project = project_io.bess_project()
        self.editor = ChannelEditor(self.project)
        self.addCleanup(self.editor.deleteLater)

    def test_lists_all_channels(self):
        self.assertEqual(self.editor.list.count(), len(self.project.channels))
        self.assertIsNotNone(self.editor._current)

    def test_shows_modbus_options(self):
        channel = self.editor._current
        self.assertIn("host", self.editor._option_widgets)
        self.assertIn("port", self.editor._option_widgets)
        self.assertEqual(
            self.editor._option_widgets["port"].value(), channel.options["port"]
        )

    def test_edit_option_applies_to_project(self):
        self.editor._option_widgets["host"].setText("192.168.10.77")
        self.editor._option_widgets["port"].setValue(1502)
        self.assertTrue(self.editor.apply_form(silent=True))
        self.assertEqual(self.project.channels[0].options["host"], "192.168.10.77")
        self.assertEqual(self.project.channels[0].options["port"], 1502)

    def test_switch_link_remembers_both(self):
        """TCP 与 RTU 参数各记一套（设置页里也成立）。"""
        channel = self.project.channels[0]
        self.editor._option_widgets["host"].setText("10.0.0.5")
        self.editor._option_widgets["port"].setValue(5025)
        self.editor.apply_form(silent=True)

        rtu_index = self.editor.link_combo.findData("rtu")
        self.assertGreaterEqual(rtu_index, 0, "Modbus 应支持 RTU")
        self.editor.link_combo.setCurrentIndex(rtu_index)
        _app.processEvents()
        self.assertEqual(channel.link, "rtu")
        self.assertIn("baudrate", self.editor._option_widgets)
        self.editor._option_widgets["port"].lineEdit().setText("COM12")
        baud = self.editor._option_widgets["baudrate"]
        baud_index = baud.findData(19200)
        if baud_index >= 0:
            baud.setCurrentIndex(baud_index)
        self.editor.apply_form(silent=True)
        self.assertEqual(channel.options["port"], "COM12")
        if baud_index >= 0:
            self.assertEqual(channel.options["baudrate"], 19200)

        tcp_index = self.editor.link_combo.findData("tcp")
        self.editor.link_combo.setCurrentIndex(tcp_index)
        _app.processEvents()
        self.assertEqual(channel.link, "tcp")
        self.assertEqual(channel.options["host"], "10.0.0.5", "切回 TCP 应恢复原参数")
        self.assertEqual(channel.options["port"], 5025)

        # 存盘再读，两套参数都还在
        restored = Project.from_dict(self.project.to_dict())
        restored_channel = restored.channels[0]
        self.assertEqual(restored_channel.link, "tcp")
        restored_channel.switch_link("rtu", {})
        self.assertEqual(restored_channel.options["port"], "COM12")

    def test_add_and_remove_channel(self):
        before = len(self.project.channels)
        self.editor.add_channel()
        self.assertEqual(len(self.project.channels), before + 1)
        added = self.project.channels[-1]
        self.assertEqual(added.link, "tcp")
        self.assertIn("host", added.options, "新建通道要有可用的默认参数")
        self.assertEqual(self.editor._current.name, added.name)

        self.project.channels.remove(added)
        self.editor.reload()
        self.assertEqual(len(self.project.channels), before)

    def test_unique_names(self):
        names = set()
        for _ in range(3):
            self.editor.add_channel()
            names.add(self.project.channels[-1].name)
        self.assertEqual(len(names), 3, f"通道名不能重复：{names}")

    def test_duplicate_channel_copies_options(self):
        self.editor._option_widgets["host"].setText("10.1.1.1")
        self.editor.apply_form(silent=True)
        before = len(self.project.channels)
        self.editor.duplicate_channel()
        self.assertEqual(len(self.project.channels), before + 1)
        clone = self.project.channels[-1]
        self.assertNotEqual(clone.name, self.project.channels[0].name)
        self.assertEqual(clone.options["host"], "10.1.1.1")

    def test_rename_channel_moves_devices(self):
        """通道改名时，挂在它下面的设备要跟着搬，否则设备会"掉线"。"""
        channel = self.project.channels[0]
        old = channel.name
        self.assertTrue(any(d.channel == old for d in self.project.devices))

        self.editor.name_edit.setText("整流器总线")
        self.editor._apply_name()
        self.assertEqual(channel.name, "整流器总线")
        for device in self.project.devices:
            self.assertNotEqual(device.channel, old, "设备还挂在旧通道名上")

    def test_rename_rejects_duplicate(self):
        """重名要拒绝（会弹提示，测试里把提示拦下来免得卡住）。"""
        from unittest import mock

        from PySide6.QtWidgets import QMessageBox

        self.editor.add_channel()
        first = self.project.channels[0].name
        self.editor.list.setCurrentRow(1)
        _app.processEvents()
        second = self.editor._current.name
        self.editor.name_edit.setText(first)
        with mock.patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as warned:
            self.editor._apply_name()
        self.assertTrue(warned.called, "重名应提示")
        self.assertEqual(self.editor._current.name, second, "重名应被拒绝")
        self.assertEqual(len({c.name for c in self.project.channels}), 2)
        self.assertEqual(
            self.editor.name_edit.text(), second, "输入框要退回原名字"
        )

    def test_remove_channel_with_devices_needs_confirmation(self):
        """通道上还挂着设备时，删除要先确认（测试里模拟"取消"）。"""
        from unittest import mock

        from PySide6.QtWidgets import QMessageBox

        channel = self.project.channels[0]
        before = len(self.project.channels)
        with mock.patch.object(QMessageBox, "warning", return_value=QMessageBox.No):
            self.editor.remove_channel()
        self.assertEqual(len(self.project.channels), before, "选了「否」不该删掉通道")

        with mock.patch.object(QMessageBox, "warning", return_value=QMessageBox.Yes):
            self.editor.remove_channel()
        self.assertEqual(len(self.project.channels), before - 1)
        self.assertNotIn(channel, self.project.channels)

    def test_remove_last_channel_leaves_empty_selection(self):
        from unittest import mock

        from PySide6.QtWidgets import QMessageBox

        for channel in list(self.project.channels):
            self.editor.reload(keep=channel.name)
            with mock.patch.object(QMessageBox, "warning", return_value=QMessageBox.Yes):
                self.editor.remove_channel()
        self.assertEqual(self.project.channels, [])
        self.assertIsNone(self.editor._current)

    def test_legacy_engineer_writes_are_preserved(self):
        """切链路前正在编辑的字段不能被丢掉。"""
        channel = self.project.channels[0]
        self.editor._option_widgets["host"].setText("172.16.0.9")
        rtu_index = self.editor.link_combo.findData("rtu")
        self.editor.link_combo.setCurrentIndex(rtu_index)
        _app.processEvents()
        self.assertEqual(
            channel.options_by_link["tcp"]["host"], "172.16.0.9", "切链路前要先存下当前参数"
        )


class SettingsDialogTest(unittest.TestCase):
    def setUp(self):
        self.project = project_io.bess_project()
        self.dialog = SettingsDialog(
            AppSettings(), self.project, list(project_io.BUILTIN_SAMPLES)
        )
        self.addCleanup(self._cleanup)
        self._before = theme.appearance()

    def _cleanup(self):
        self.dialog.reject()
        theme.apply(self._before)
        _app.processEvents()

    def test_four_tabs(self):
        labels = [self.dialog.tabs.tabText(i) for i in range(self.dialog.tabs.count())]
        self.assertEqual(labels, ["界面外观", "通信", "软件设置", "工程设置"])

    def test_appearance_preview_applies_immediately(self):
        editor = self.dialog.appearance_editor
        editor.size_box.setValue(16)
        self.assertIn("font-size: 16px", theme.STYLESHEET, "外观改动应立即生效")

    def test_cancel_restores_appearance(self):
        before_size = theme.appearance().font_size
        self.dialog.appearance_editor.size_box.setValue(19)
        self.assertNotEqual(theme.appearance().font_size, before_size)
        self.dialog.reject()
        self.assertEqual(
            theme.appearance().font_size, before_size, "取消后外观要还原"
        )

    def test_accept_persists_appearance_and_channels(self):
        self.dialog.appearance_editor.size_box.setValue(15)
        self.dialog.channel_editor._option_widgets["port"].setValue(1502)
        self.dialog.accept()

        self.assertEqual(self.dialog.settings.appearance["font_size"], 15)
        self.assertEqual(
            theme.Appearance.from_dict(self.dialog.settings.appearance).font_size, 15
        )
        self.assertEqual(self.project.channels[0].options["port"], 1502)
        self.assertEqual(len(self.project.channels), len(self.dialog.project.channels))

    def test_accept_moves_devices_when_channel_renamed(self):
        self.dialog.channel_editor.name_edit.setText("主总线")
        self.dialog.channel_editor._apply_name()
        self.dialog.accept()
        self.assertEqual(self.project.channels[0].name, "主总线")
        self.assertTrue(
            all(d.channel == "主总线" for d in self.project.devices),
            "设备应跟着通道改名",
        )

    def test_accept_requires_project_name(self):
        """工程名为空时不该关闭窗口（会弹提示，这里把提示拦下来）。"""
        from unittest import mock

        from PySide6.QtWidgets import QMessageBox

        self.dialog.name_edit.setText("   ")
        with mock.patch.object(QMessageBox, "warning", return_value=QMessageBox.Ok) as warned:
            self.dialog.accept()
        self.assertTrue(warned.called, "应提示工程名为空")
        self.assertEqual(self.dialog.result(), 0, "工程名为空时不该关闭")

    def test_appearance_stored_as_plain_dict(self):
        """外观在核心层按字典存，核心层不该依赖 UI。"""
        self.assertIsInstance(self.dialog.settings.appearance, dict)
        AppSettings.from_dict({"appearance": "不是字典"})  # 不该抛异常


class ControlPanelTest(unittest.TestCase):
    """小窗口按键面板：按键要真发出正确的写入值。"""

    def setUp(self):
        self.project = project_io.bess_project()
        self.device = self.project.devices[0]
        self.addCleanup(close_all)
        self.addCleanup(lambda: _app.processEvents())

    def _values(self, pid: str, value=None) -> TagValue:
        return TagValue(pid=pid, value=value, quality="good")

    @staticmethod
    def _keys(panel) -> list[QPushButton]:
        """面板上的按键（按 objectName 认，避免和"更多…""停止发送"混在一起）。"""
        return [
            button
            for button in panel.findChildren(QPushButton)
            if button.objectName() == "controlKey"
        ]

    def _key(self, panel, label: str) -> QPushButton:
        for button in self._keys(panel):
            if button.text() == label:
                return button
        raise AssertionError(f"面板上没有「{label}」按键：{[b.text() for b in self._keys(panel)]}")

    def point_panel(self, point_name: str, value=None) -> PointControlPanel:
        pid = f"{PCS}.{point_name}"
        return PointControlPanel(
            self.project,
            pid,
            value_of=lambda p: self._values(p, value),
            periodic_of=lambda _p: None,
        )

    def test_command_panel_has_buttons(self):
        panel = self.point_panel(CTRL_POINT, 4)
        labels = [b.text() for b in self._keys(panel)]
        self.assertEqual(len(labels), 5)
        self.assertIn("开机", labels)

    def test_command_panel_emits_write(self):
        panel = self.point_panel(CTRL_POINT, 4)
        seen: list[tuple[str, object]] = []
        panel.writeRequested.connect(lambda pid, v: seen.append((pid, v)))
        self._key(panel, "开机").click()
        self.assertEqual(seen, [(CTRL_PID, 0x01)])

    def test_bit_panel_toggles_bit_by_name(self):
        """带位定义的可写点位：点按钮 = 翻转该位，其它位保持不变。"""
        device = self.project.devices[0]
        from icomm.core.models import PointDef

        device.points.append(
            PointDef(
                name="测试功能字",
                data_type="uint32",
                address=166,
                writable=True,
                bits="0:使能低电压穿越,1:使能无功调节,2:使能防孤岛",
            )
        )
        pid = f"{PCS}.测试功能字"
        panel = PointControlPanel(
            self.project,
            pid,
            value_of=lambda p: TagValue(pid=p, value=0b010, quality="good"),
            periodic_of=lambda _p: None,
        )
        seen: list[tuple[str, object]] = []
        panel.writeRequested.connect(lambda p, v: seen.append((p, v)))

        buttons = {b.text(): b for b in self._keys(panel)}
        self.assertIn("使能低电压穿越", buttons)
        buttons["使能低电压穿越"].click()
        self.assertEqual(seen, [(pid, 0b011)], "应翻 bit0 而保留 bit1")

    def test_device_panel_only_shows_command_points_by_default(self):
        """默认只列参数表里带「命令」列的点位；位掩码类不展开（否则上百个键没法用）。"""
        panel = DeviceControlPanel(
            self.project,
            PCS,
            value_of=lambda p: TagValue(pid=p, value=0x7FFF, quality="good"),
            periodic_of=lambda _p: None,
        )
        keys = [b.text() for b in self._keys(panel)]
        expected = sum(len(p.command_items()) for p in self.device.points if p.command_items())
        self.assertEqual(len(keys), expected)

        mask_only = [
            point.title
            for point in self.device.points
            if not point.command_items() and point.bit_labels()
        ]
        for title in mask_only:
            self.assertNotIn(title, keys, f"位掩码点位「{title}」不该默认展开")

    def test_device_panel_includes_picked_extra_points(self):
        """用户点名的位掩码点位要出现在面板上（可自选）。"""
        pid = FUNC_PID
        panel = DeviceControlPanel(
            self.project,
            PCS,
            value_of=lambda p: TagValue(pid=p, value=0, quality="good"),
            periodic_of=lambda _p: None,
            extra_pids=[pid],
        )
        keys = [b.text() for b in self._keys(panel)]
        for _bit, label in self.project.devices[0].point("功能配置字").bit_labels():
            self.assertIn(label, keys, f"自选的位「{label}」应出现")

    def test_device_without_command_points_falls_back(self):
        """一台设备一个命令点位都没有时，退化成列几个位域点位，免得面板空白。"""
        from icomm.core.models import PointDef

        project = project_io.blank_project()
        device = project.devices[0]
        device.points.clear()
        for index in range(5):
            device.points.append(
                PointDef(
                    name=f"掩码{index}",
                    data_type="uint32",
                    address=index * 2,
                    writable=True,
                    bits="0:A,1:B,2:C",
                )
            )
        panel = DeviceControlPanel(
            project,
            device.name,
            value_of=lambda p: TagValue(pid=p, value=0, quality="good"),
            periodic_of=lambda _p: None,
        )
        keys = [b.text() for b in self._keys(panel)]
        self.assertTrue(keys, "退化模式不该给出空面板")
        self.assertLessEqual(
            len(keys), DeviceControlPanel.FALLBACK_LIMIT * 3,
            "退化模式也只列少数几个点位",
        )

    def test_picker_lists_controllable_points(self):
        from icomm.ui.dialogs.control_panel import PanelPointPicker

        picker = PanelPointPicker(self.project.devices[0], [])
        controllable = [
            p
            for p in self.device.points
            if p.command_items() or (p.writable and p.bit_labels())
        ]
        self.assertEqual(len(picker._boxes), len(controllable))

        # 命令点位默认勾上且不可取消（参数表就是它的开关）
        ctrl_box = picker._boxes[CTRL_PID]
        self.assertTrue(ctrl_box.isChecked() and not ctrl_box.isEnabled())
        self.assertNotIn(CTRL_PID, picker.selected())

        picker._boxes[FUNC_PID].setChecked(True)
        self.assertEqual(picker.selected(), [FUNC_PID])

    def test_rebuilding_device_panel_keeps_new_one_registered(self):
        """回归：换点位会先关旧面板再开新面板，旧窗口的 destroyed
        信号不能被用来把新面板从注册表里删掉。"""
        from icomm.ui.dialogs import control_panel

        device_name = PCS
        first = control_panel.open_device_panel(
            None,
            project=self.project,
            device_name=device_name,
            value_of=lambda p: TagValue(pid=p, value=4, quality="good"),
            periodic_of=lambda _p: None,
        )
        control_panel.close_device_panel(device_name)
        second = control_panel.open_device_panel(
            None,
            project=self.project,
            device_name=device_name,
            value_of=lambda p: TagValue(pid=p, value=4, quality="good"),
            periodic_of=lambda _p: None,
        )
        self.assertIsNot(first, second)
        _app.processEvents()
        self.assertIs(
            control_panel._open_panels.get(f"device::{device_name}"),
            second,
            "新面板被旧面板的销毁信号误删了",
        )

    def test_readonly_point_has_no_keys(self):
        panel = self.point_panel("直流母线电压", 695.0)
        self.assertEqual(self._keys(panel), [], "只读点位不该有按键")

    def test_value_panel_nudges_from_current(self):
        panel = self.point_panel("有功功率给定", 200.0)
        seen: list[tuple[str, object]] = []
        panel.writeRequested.connect(lambda p, v: seen.append((p, v)))
        keys = {b.text(): b for b in self._keys(panel)}
        self.assertTrue(keys, "普通可写点位应给常用操作按键")
        plus = next(k for text, k in keys.items() if text.startswith("+"))
        plus.click()
        self.assertTrue(seen)
        self.assertGreater(float(seen[0][1]), 200.0)

    def test_device_panel_collects_command_points(self):
        panel = DeviceControlPanel(
            self.project,
            PCS,
            value_of=lambda p: TagValue(pid=p, value=4, quality="good"),
            periodic_of=lambda _p: None,
        )
        texts = [b.text() for b in self._keys(panel)]
        for label in ("开机", "关机", "充电", "放电", "复位"):
            self.assertIn(label, texts, f"控制台缺少「{label}」")

    def test_device_panel_emits_control_word(self):
        panel = DeviceControlPanel(
            self.project,
            PCS,
            value_of=lambda p: TagValue(pid=p, value=4, quality="good"),
            periodic_of=lambda _p: None,
        )
        seen: list[tuple[str, object]] = []
        panel.writeRequested.connect(lambda p, v: seen.append((p, v)))
        self._key(panel, "复位").click()
        self.assertEqual(seen, [(CTRL_PID, 0x10)])

    def test_device_panel_shows_status_bits(self):
        panel = DeviceControlPanel(
            self.project,
            PCS,
            value_of=lambda p: TagValue(pid=p, value=0x30, quality="good"),
            periodic_of=lambda _p: None,
        )
        panel.refresh()
        found = [
            label.text()
            for label in panel._value_labels.values()
            if "bit5" in label.text()
        ]
        self.assertTrue(found, "控制台应显示状态字的位含义")

    def test_panel_reports_periodic_state(self):
        class Task:
            pid = BUS_PID
            value = 700.0
            interval_ms = 250
            enabled = True
            sent = 7
            failed = 0
            last_error = ""

        panel = PointControlPanel(
            self.project,
            BUS_PID,
            value_of=lambda p: TagValue(pid=p, value=700.0, quality="good"),
            periodic_of=lambda p: Task() if p == BUS_PID else None,
        )
        panel.refresh()
        self.assertIn("每 250 ms", panel.periodic_label.text())
        self.assertIn("已发 7 次", panel.periodic_label.text())
        self.assertTrue(panel.stop_button.isEnabled())

        seen: list[str] = []
        panel.periodicStopRequested.connect(seen.append)
        panel.stop_button.click()
        self.assertEqual(seen, [BUS_PID])

    def test_panel_is_non_modal_floating(self):
        from PySide6.QtCore import Qt

        panel = self.point_panel(CTRL_POINT, 4)
        self.assertTrue(panel.windowFlags() & Qt.Tool, "应为浮动工具窗口")
        self.assertTrue(panel.windowFlags() & Qt.WindowStaysOnTopHint)
        self.assertFalse(panel.isModal(), "面板不该是模态的")

    def test_panel_timer_stops_on_close(self):
        panel = self.point_panel(CTRL_POINT, 4)
        panel.show()
        _app.processEvents()
        self.assertTrue(panel._timer.isActive())
        panel.close()
        self.assertFalse(panel._timer.isActive(), "关掉后不该继续定时刷新")


class MainWindowControlIntegrationTest(unittest.TestCase):
    """主窗口这一层：菜单项、可用点位清单、面板接线。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        from icomm.ui.main_window import MainWindow

        self.project = project_io.bess_project()
        paths = project_store.create_project_dir(
            os.path.join(self.tmp.name, "工程"), self.project
        )
        self.window = MainWindow(self.project, paths, workdir=self.tmp.name, base_dir=self.tmp.name)
        # 关掉退出确认：它会弹模态框，在无人值守的测试里会永久阻塞
        self.window.settings.confirm_on_exit = False
        self.addCleanup(self._cleanup)
        self.addCleanup(close_all)
        self.addCleanup(lambda: _app.processEvents())

    def _cleanup(self):
        if self.window.engine is not None:
            self.window.engine.stop()
        if self.window.storage is not None:
            self.window.storage.stop()
        self.window.settings.confirm_on_exit = False
        self.window.close()

    def test_controllable_points_lists_command_points(self):
        entries = dict(self.window.controllable_points())
        self.assertIn(CTRL_PID, entries)
        self.assertIn(FUNC_PID, entries, "位定义的可写点位也该算可控")
        self.assertNotIn("储能变流器PCS.直流母线电压", entries, "只读遥测不该算可控")

    def test_build_point_menu_offers_control_panel(self):
        menu = self.window.build_point_menu(CTRL_PID)
        labels = [a.text() for a in menu.actions()]
        self.assertTrue(
            any("按键控制小窗口" in text for text in labels), f"菜单里应有按键面板：{labels}"
        )

    def test_readonly_point_menu_has_no_control_panel(self):
        menu = self.window.build_point_menu("储能变流器PCS.直流母线电压")
        labels = [a.text() for a in menu.actions()]
        self.assertFalse(any("按键控制小窗口" in text for text in labels))
        self.assertTrue(any("添加到示波器" in text for text in labels))

    def test_device_panel_opens_and_writes(self):
        self.window.start_engine()
        self.window.show_device_control_panel(PCS)
        panel = next(
            p
            for key, p in __import__(
                "icomm.ui.dialogs.control_panel", fromlist=["_open_panels"]
            )._open_panels.items()
            if key.startswith("device::")
        )
        seen: list[tuple[str, object]] = []
        self.window._on_write_requested = lambda pid, value: seen.append((pid, value))
        panel.writeRequested.emit(CTRL_PID, 0x10)
        self.assertEqual(seen, [(CTRL_PID, 0x10)])

    def test_point_panel_reuses_existing_window(self):
        from icomm.ui.dialogs import control_panel

        first = self.window.show_point_control_panel(CTRL_PID) or control_panel._open_panels.get(
            f"point::{CTRL_PID}"
        )
        second = control_panel.open_point_panel(
            self.window, project=self.project, pid=CTRL_PID, **self.window._panel_kwargs()
        )
        self.assertIs(first, second, "同一个点位不该开出两个窗口")

    def test_settings_applies_appearance(self):
        before = theme.appearance()
        self.addCleanup(lambda: theme.apply(before))
        dialog = SettingsDialog(
            AppSettings(), self.project, list(project_io.BUILTIN_SAMPLES), parent=self.window
        )
        dialog.appearance_editor.size_box.setValue(14)
        dialog.accept()
        self.window.apply_settings(dialog)
        self.assertEqual(theme.appearance().font_size, 14)
        self.assertEqual(
            AppSettings.from_dict(self.window.settings.to_dict()).appearance["font_size"], 14
        )

    # -------------------------------------------------- 菜单栏布局

    def test_menu_bar_has_the_seven_menus_in_order(self):
        """左上角就是这七个菜单，顺序固定。"""
        titles = [action.text() for action in self.window.menuBar().actions()]
        self.assertEqual(
            titles,
            ["文件(&F)", "编辑(&E)", "选择(&S)", "工具(&T)", "控制台(&C)", "设置(&O)", "关于(&A)"],
        )

    def test_project_open_and_save_live_in_file_menu(self):
        """工程打开/保存统一在「文件」里，不再散在工具栏上。"""
        file_menu = self.window.menuBar().actions()[0].menu()
        texts = [action.text() for action in file_menu.actions() if not action.isSeparator()]
        self.assertIn("打开工程…", texts)
        self.assertIn("保存工程", texts)
        self.assertIn("保存为…", texts)
        self.assertIn("最近打开的工程", texts, "要有最近打开列表，不然每次都得翻目录")
        # 保存细分（参数设置 / 整个工程）挂在子菜单里
        save_as = next(
            action.menu()
            for action in file_menu.actions()
            if action.text() == "保存为…" and action.menu() is not None
        )
        save_texts = [action.text() for action in save_as.actions() if not action.isSeparator()]
        self.assertIn("保存参数设置…", save_texts)
        self.assertIn("保存整个工程…", save_texts)

    def test_current_project_is_remembered_for_next_start(self):
        """打开工程后要把路径记下来，下次启动直接回到它（而不是又回到默认储能示例）。"""
        path = self.window.settings.last_project
        self.assertTrue(path, "打开工程后要记下路径")
        self.assertEqual(
            os.path.normcase(path),
            os.path.normcase(os.path.abspath(self.window.paths.project_file)),
        )
        self.assertIn(path, self.window.settings.recent_projects)
        self.assertEqual(os.path.dirname(path), os.path.dirname(self.window.paths.project_file))
        loaded = app_settings.load_settings(self.tmp.name)
        self.assertEqual(loaded.last_project, path, "要真的写到 settings.json 里")
        self.assertIn(path, loaded.recent_projects)

    def test_opening_another_project_updates_recent_list(self):
        other = project_io.grid_project()
        target = project_store.create_project_dir(
            os.path.join(self.tmp.name, "另一个工程"), other
        )
        self.window._open_project_path(target.project_file)
        self.assertEqual(self.window.project.name, other.name)
        expected = os.path.abspath(target.project_file)
        self.assertEqual(
            os.path.normcase(self.window.settings.last_project), os.path.normcase(expected)
        )
        self.assertEqual(
            os.path.normcase(self.window.settings.recent_projects[0]),
            os.path.normcase(expected),
        )
        self.assertGreaterEqual(len(self.window.settings.recent_projects), 2)

    def test_recent_menu_lists_entries_and_clears(self):
        self.window._refresh_recent_projects_menu()
        texts = [action.text() for action in self.window.recent_menu.actions()]
        self.assertIn("清除最近记录", texts)
        self.assertTrue(
            any(text and text != "清除最近记录" for text in texts), f"菜单里应有工程：{texts}"
        )
        self.window._clear_recent_projects()
        self.assertEqual(self.window.settings.recent_projects, [])
        self.assertEqual(self.window.settings.last_project, "")
        actions = self.window.recent_menu.actions()
        self.assertTrue(actions)
        self.assertIn("暂无最近打开的工程", actions[0].text())
        self.assertFalse(actions[0].isEnabled())

    def test_output_path_defaults_into_project_reports(self):
        self.window.settings.export_location = "project"
        path = self.window.output_path("tables", "a.csv")
        self.assertTrue(os.path.isdir(os.path.dirname(path)), path)
        self.assertTrue(path.endswith(os.path.join("tables", "a.csv")), path)
        self.assertTrue(
            os.path.normcase(path).startswith(
                os.path.normcase(self.window.paths.report_dir)
            ),
            f"默认应当落在工程报表目录，实际 {path}",
        )

    def test_output_path_can_use_a_custom_directory(self):
        custom = os.path.join(self.tmp.name, "自选导出")
        self.window.settings.export_location = "custom"
        self.window.settings.custom_export_directory = custom
        self.addCleanup(self._restore_export_location)
        path = self.window.output_path("waveforms", "b.csv")
        self.assertTrue(os.path.isdir(os.path.dirname(path)), path)
        self.assertTrue(
            os.path.normcase(path).startswith(os.path.normcase(custom)),
            f"改成自选目录后要落在 {custom}，实际 {path}",
        )

    def _restore_export_location(self) -> None:
        self.window.settings.export_location = "project"
        self.window.settings.custom_export_directory = ""

    def test_select_menu_lists_every_page(self):
        select_menu = self.window.menuBar().actions()[2].menu()
        texts = [action.text() for action in select_menu.actions() if not action.isSeparator()]
        for name in NAV_ITEMS:
            self.assertIn(name, texts)
        self.assertIn("上一个页面", texts)
        self.assertIn("下一个页面", texts)

    def test_tools_menu_is_built_into_the_bar(self):
        """「工具」直接填进菜单栏，条目来自工具注册表（不是搬来的临时菜单动作）。"""
        tools_menu = self.window.menuBar().actions()[3].menu()
        entries = tools_menu.actions()
        self.assertTrue(entries, "工具菜单不该是空的")
        for entry in entries:
            if entry.isSeparator():
                continue
            submenu = entry.menu()
            if submenu is not None:
                self.assertTrue(submenu.actions(), f"子菜单「{entry.text()}」是空的")
            else:
                self.assertTrue(entry.text(), "顶层条目该有名字")

        from icomm.ui import tools as ui_tools

        registered = {
            tool.name for tools in ui_tools.tools_by_group().values() for tool in tools
        }
        shown = {
            action.text()
            for entry in entries
            for action in (entry.menu().actions() if entry.menu() else [entry])
        }
        self.assertTrue(
            registered & shown,
            f"工具菜单里一个注册表工具都没有，菜单里只有：{sorted(shown)[:6]}",
        )

    def test_run_controls_no_longer_sit_on_the_menu_bar(self):
        """采集启停只留在「控制台」菜单里，菜单栏上不再放那排按钮。

        之前放在菜单栏左上角的角落控件会压住「文件」菜单，而且点了没反应 ——
        入口重复又不管用，索性收回菜单。
        """
        self.assertEqual(
            self.window.findChildren(QToolBar), [], "不该再有单独一行工具栏"
        )
        bar = self.window.menuBar()
        self.assertIsNone(
            bar.cornerWidget(Qt.TopLeftCorner), "菜单栏左端不该再挂采集按钮"
        )
        self.assertFalse(hasattr(self.window, "run_controls"), "那排按钮已经删掉了")

    def test_run_actions_really_drive_the_engine(self):
        """菜单里的三个动作必须真的接到引擎上（上一版角落按钮点了没用，就是因为不止一处入口）。"""
        self.assertTrue(self.window.action_start.isEnabled())
        self.assertFalse(self.window.action_stop.isEnabled(), "没启动时「停止」该是灰的")
        seen: list[str] = []
        started, stopped = self.window.start_engine, self.window.stop_engine

        def fake_start() -> None:
            seen.append("start")

        def fake_stop() -> None:
            seen.append("stop")

        self.window.action_start.triggered.disconnect()
        self.window.action_start.triggered.connect(fake_start)
        self.window.action_stop.triggered.disconnect()
        self.window.action_stop.triggered.connect(fake_stop)
        try:
            self.window.action_start.trigger()
            self.assertEqual(seen, ["start"], "「启动采集」没有触发")
        finally:
            self.window.action_start.triggered.disconnect()
            self.window.action_start.triggered.connect(started)
            self.window.action_stop.triggered.disconnect()
            self.window.action_stop.triggered.connect(stopped)

    def test_run_state_label_is_at_the_right_end(self):
        label = self.window.menuBar().cornerWidget(Qt.TopRightCorner)
        self.assertIs(label, self.window.mode_label)

    def test_run_actions_still_reachable_from_console_menu(self):
        """挪位置不等于删入口：「控制台」菜单里那三条照旧。"""
        console_menu = self.window.menuBar().actions()[4].menu()
        texts = [action.text() for action in console_menu.actions() if action.text()]
        for expected in ("▶ 启动采集", "■ 停止采集", "⟳ 重启采集", "启动内置仿真器"):
            self.assertIn(expected, texts)

    def test_action_objects_survive_the_layout_change(self):
        """布局改了，但 action_* 这些对象还得在（老代码/探针/测试都按名字引用）。"""
        for name in (
            "action_start",
            "action_stop",
            "action_restart",
            "action_open",
            "action_save",
            "action_sim",
            "action_periodic",
            "action_automation",
            "action_control",
            "action_settings",
            "action_about",
        ):
            with self.subTest(action=name):
                self.assertTrue(hasattr(self.window, name), f"缺 {name}")

    def test_step_page_wraps_around(self):
        self.window.nav.setCurrentRow(0)
        self.window._step_page(-1)
        self.assertEqual(self.window.nav.currentRow(), len(NAV_ITEMS) - 1, "往前该绕到最后一页")
        self.window._step_page(1)
        self.assertEqual(self.window.nav.currentRow(), 0)

    def test_restore_default_appearance_returns_to_sky_blue(self):
        before = theme.appearance()
        self.addCleanup(lambda: theme.apply(before))
        theme.apply_preset("深色工业")
        self.window._restore_default_appearance()
        self.assertEqual(theme.PRESETS[theme.appearance().palette.name].accent, theme.PRESETS["天蓝"].accent)
        self.assertEqual(theme.appearance().palette.name, "天蓝")


if __name__ == "__main__":
    unittest.main()
