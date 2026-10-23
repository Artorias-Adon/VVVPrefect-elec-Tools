"""SCI 烧写协议：用模拟设备跑完整流程，覆盖成功/校验错/超时/中止/参数档案。"""

from __future__ import annotations

import unittest

from icomm.core import flash_external, flash_file, sci_flash


def image(text: str = "@1000\n01 02 03 04 05 06 07 08\nq\n") -> flash_file.FlashImage:
    return flash_file.parse_text(text, path="mem")


def c2000() -> sci_flash.SciProfile:
    return sci_flash.profile_by_key("ti-c2000")


class ProfileTest(unittest.TestCase):
    def test_builtin_profiles(self) -> None:
        keys = [profile.key for profile in sci_flash.builtin_profiles()]
        self.assertEqual(keys, ["ti-c2000", "raw"])
        self.assertIn("C2000", sci_flash.profile_by_key("ti-c2000").label)
        self.assertIn("TI C2000", c2000().describe())

    def test_round_trip_dict(self) -> None:
        profile = c2000()
        profile.block_words = 64
        profile.autobaud = b"\x41"
        data = profile.to_dict()
        self.assertEqual(data["autobaud"], "41")
        again = sci_flash.SciProfile.from_dict(data, c2000())
        self.assertEqual(again.autobaud, b"\x41")
        self.assertEqual(again.block_words, 64)

    def test_bad_values_fall_back(self) -> None:
        profile = sci_flash.SciProfile.from_dict(
            {"autobaud": "zz", "block_words": 99999, "timeout_s": "x"}, c2000()
        )
        self.assertEqual(profile.autobaud, b"\x41", "坏字节串保留默认值")
        self.assertLessEqual(profile.block_words, 4096)

    def test_header_is_eight_bytes(self) -> None:
        header = c2000().header_bytes(0x1234, 0x0010, 0xBEEF)
        self.assertEqual(len(header), 8)
        self.assertEqual(header[0:4], bytes.fromhex("00001234"))
        self.assertEqual(header[4:6], bytes.fromhex("0010"))
        self.assertEqual(header[6:8], bytes.fromhex("BEEF"))


class CrcTest(unittest.TestCase):
    def test_crc16_xmodem_vector(self) -> None:
        self.assertEqual(sci_flash.crc16(b"123456789"), 0x31C3)


class SimulatedFlashTest(unittest.TestCase):
    def test_success_writes_exactly_the_expected_words(self) -> None:
        source = image()
        result, device = sci_flash.flash_simulated(source)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.blocks, 1)
        self.assertEqual(result.words, source.word_count)
        self.assertGreater(result.bytes_sent, 0)
        self.assertEqual(device.expected_words(source), device.memory)
        self.assertEqual(device.errors, [])

    def test_block_splitting_respects_block_words(self) -> None:
        profile = c2000()
        profile.block_words = 2
        source = image("@2000\n" + " ".join(f"{value:02X}" for value in range(10)) + "\nq\n")
        result, _device = sci_flash.flash_simulated(source, profile)
        self.assertTrue(result.ok)
        self.assertEqual(result.blocks, 3, "10 字节 = 5 字，每块 2 字 → 3 块")

    def test_multiple_blocks_keep_their_addresses(self) -> None:
        source = flash_file.parse_text("@1000\n01 02\n@2000\n03 04\nq\n")
        _result, device = sci_flash.flash_simulated(source)
        self.assertEqual(sorted(device.memory), [0x800, 0x1000])

    def test_nack_reports_which_block_failed(self) -> None:
        source = image("@1000\n01 02\n@2000\n03 04\nq\n")
        device = sci_flash.SimulatedDsp(c2000())
        device.fail_after_blocks = 1
        device.fail_mode = "nack"
        transport = sci_flash.SimulatedTransport(device)
        result = sci_flash.SciBootloader(transport, c2000()).flash(source)
        self.assertFalse(result.ok)
        self.assertIn("应答", result.message)
        self.assertEqual(result.blocks, 1, "第一块成功了，第二块被拒")

    def test_silent_device_times_out(self) -> None:
        profile = c2000()
        profile.timeout_s = 0.05
        device = sci_flash.SimulatedDsp(profile)
        device.fail_after_blocks = 0
        device.fail_mode = "silent"
        transport = sci_flash.SimulatedTransport(device)
        result = sci_flash.SciBootloader(transport, profile).flash(image())
        self.assertFalse(result.ok)
        self.assertIn("应答", result.message)

    def test_corrupted_write_is_detectable_by_comparison(self) -> None:
        source = image()
        device = sci_flash.SimulatedDsp(c2000())
        device.fail_after_blocks = 0
        device.fail_mode = "corrupt"
        transport = sci_flash.SimulatedTransport(device)
        result = sci_flash.SciBootloader(transport, c2000()).flash(source)
        self.assertTrue(result.ok, "设备照常应答，问题在数据本身")
        problems = sci_flash.compare_words(device.expected_words(source), device.memory)
        self.assertTrue(problems, "回读比对要能发现写错")

    def test_cancel_stops_after_current_block(self) -> None:
        profile = c2000()
        profile.block_words = 1
        source = image("@1000\n01\n02\n03\n04\nq\n")
        calls = {"n": 0}

        def stop() -> bool:
            calls["n"] += 1
            return calls["n"] > 1

        result, device = sci_flash.flash_simulated(source, profile, should_stop=stop)
        self.assertFalse(result.ok)
        self.assertTrue(result.cancelled)
        self.assertLess(device.blocks_written, 4)

    def test_handshake_mismatch_explains_what_to_check(self) -> None:
        profile = c2000()
        # 设备用一份独立副本，否则"改设备参数"会把主机的期望值也一起改了
        device = sci_flash.SimulatedDsp(
            sci_flash.SciProfile.from_dict(profile.to_dict(), c2000())
        )
        device.profile.autobaud_echo = b"\x00"  # 从机回显不对
        transport = sci_flash.SimulatedTransport(device)
        result = sci_flash.SciBootloader(transport, profile).flash(image())
        self.assertFalse(result.ok)
        self.assertIn("波特率", result.message)

    def test_raw_profile_sends_data_without_header(self) -> None:
        profile = sci_flash.profile_by_key("raw")
        profile.block_words = 2
        source = image("@0000\n01 02 03 04\nq\n")
        result, device = sci_flash.flash_simulated(source, profile)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(len(device.memory), 2)
        self.assertEqual(device.memory[0], 0x0201)

    def test_result_text_is_readable(self) -> None:
        result, _device = sci_flash.flash_simulated(image())
        text = result.as_text()
        self.assertIn("结果：成功", text)
        self.assertIn("过程：", text)

    def test_preview_bytes(self) -> None:
        self.assertEqual(sci_flash.preview_bytes(b""), "（无）")
        self.assertIn("41", sci_flash.preview_bytes(b"\x41"))
        self.assertIn("字节", sci_flash.preview_bytes(bytes(range(20))))


class ExternalFlasherTest(unittest.TestCase):
    def test_build_command_replaces_placeholders(self) -> None:
        argv = flash_external.build_command(
            "{exe} --port {port} --baud {baud} --file {file}",
            exe=r"C:\tools\flash.exe",
            file=r"D:\proj\app.hex",
            port="COM3",
            baud=115200,
        )
        self.assertEqual(argv[0], r"C:\tools\flash.exe")
        self.assertIn("COM3", argv)
        self.assertIn("115200", argv)
        self.assertTrue(any(arg.endswith("app.hex") for arg in argv))

    def test_empty_template_runs_exe_only(self) -> None:
        self.assertEqual(flash_external.build_command("", exe="flash.exe", file="", port="", baud=1),
                         ["flash.exe"])

    def test_guide_mentions_compliance_and_placeholders(self) -> None:
        text = flash_external.install_guide()
        self.assertIn("不附带", text)
        self.assertIn("{file}", text)
        self.assertIn("UniFlash", text)

    def test_runner_rejects_missing_exe(self) -> None:
        runner = flash_external.ExternalRunner([r"C:\nope\nope.exe"])
        result = runner.run()
        self.assertFalse(result.ok)
        self.assertIn("找不到外部程序", result.message)

    def test_runner_streams_output_and_exit_code(self) -> None:
        import sys

        lines: list[str] = []
        runner = flash_external.ExternalRunner(
            [sys.executable, "-c", "print('hello-flash')"], on_line=lines.append
        )
        result = runner.run()
        self.assertTrue(result.ok, result.message)
        self.assertIn("hello-flash", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
