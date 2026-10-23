"""烧写文件解析与在工程目录里找文件（纯逻辑，不依赖串口/硬件）。"""

from __future__ import annotations

import os
import tempfile
import unittest

from icomm.core import flash_file


def ihex_record(address: int, kind: int, data: bytes) -> str:
    payload = bytes([len(data), (address >> 8) & 0xFF, address & 0xFF, kind]) + data
    checksum = (0x100 - (sum(payload) & 0xFF)) & 0xFF
    return ":" + (payload + bytes([checksum])).hex().upper()


def srec_record(kind: str, address: int, data: bytes, address_size: int) -> str:
    body = address.to_bytes(address_size, "big") + data
    count = len(body) + 1
    checksum = (~(count + sum(body))) & 0xFF
    return f"S{kind}{count:02X}" + body.hex().upper() + f"{checksum:02X}"


class TiTxtTest(unittest.TestCase):
    def test_single_block(self) -> None:
        image = flash_file.parse_text("@1000\n00 11 22 33\nq\n")
        self.assertEqual(image.fmt, "ti-txt")
        self.assertEqual(len(image.blocks), 1)
        self.assertEqual(image.blocks[0].address, 0x1000)
        self.assertEqual(image.blocks[0].data, bytes.fromhex("00112233"))
        self.assertEqual(image.byte_count, 4)

    def test_multiple_blocks_and_contiguous_merge(self) -> None:
        text = "@1000\n01 02\n@1002\n03 04\n@2000\nAA\nq\n"
        image = flash_file.parse_text(text)
        self.assertEqual(len(image.blocks), 2, "相邻段应合并")
        self.assertEqual(image.blocks[0].data, bytes.fromhex("01020304"))
        self.assertEqual(image.blocks[1].address, 0x2000)

    def test_q_is_optional_but_noted(self) -> None:
        image = flash_file.parse_text("@1000\n01\n")
        self.assertTrue(any("q" in note for note in image.notes))

    def test_data_before_address_is_rejected(self) -> None:
        with self.assertRaisesRegex(flash_file.FlashError, "地址行之前"):
            flash_file.parse_text("01 02 03\n")

    def test_bad_byte_is_rejected_with_line_number(self) -> None:
        with self.assertRaisesRegex(flash_file.FlashError, "第 2 行"):
            flash_file.parse_text("@1000\n01 ZZ\nq\n")


class IntelHexTest(unittest.TestCase):
    def test_data_and_eof(self) -> None:
        text = "\n".join(
            [
                ihex_record(0x0100, 0, bytes.fromhex("214601360121470136007EFE09D21901")),
                ihex_record(0x0000, 0x01, b""),
            ]
        )
        image = flash_file.parse_text(text)
        self.assertEqual(image.fmt, "ihex")
        self.assertEqual(image.start_address, 0x0100)
        self.assertEqual(len(image.blocks[0].data), 16)

    def test_extended_linear_address(self) -> None:
        text = "\n".join(
            [
                ihex_record(0x0000, 0x04, bytes.fromhex("0001")),  # 基址 0x10000
                ihex_record(0x0010, 0, bytes.fromhex("AA BB")),
                ihex_record(0x0000, 0x01, b""),
            ]
        )
        image = flash_file.parse_text(text)
        self.assertEqual(image.start_address, 0x10010)

    def test_bad_checksum_is_rejected(self) -> None:
        record = ihex_record(0x0000, 0, b"\x01\x02")
        broken = record[:-2] + "00"
        with self.assertRaisesRegex(flash_file.FlashError, "校验和"):
            flash_file.parse_text(broken)

    def test_start_address_record(self) -> None:
        text = "\n".join(
            [
                ihex_record(0x0000, 0, b"\x01"),
                ihex_record(0x0000, 0x05, bytes.fromhex("00001234")),
                ihex_record(0x0000, 0x01, b""),
            ]
        )
        image = flash_file.parse_text(text)
        self.assertEqual(image.entry, 0x1234)


class SrecTest(unittest.TestCase):
    def test_s1_data(self) -> None:
        text = "\n".join(
            [
                srec_record("1", 0x1000, bytes.fromhex("01020304"), 2),
                srec_record("9", 0x1000, b"", 2),
            ]
        )
        image = flash_file.parse_text(text)
        self.assertEqual(image.fmt, "srec")
        self.assertEqual(image.start_address, 0x1000)
        self.assertEqual(image.entry, 0x1000)

    def test_s3_32bit_address(self) -> None:
        text = srec_record("3", 0x0001_0000, bytes.fromhex("AA"), 4)
        image = flash_file.parse_text(text)
        self.assertEqual(image.start_address, 0x10000)

    def test_bad_checksum_is_rejected(self) -> None:
        record = srec_record("1", 0x1000, b"\x01", 2)
        broken = record[:-2] + "00"
        with self.assertRaisesRegex(flash_file.FlashError, "校验和"):
            flash_file.parse_text(broken)


class FormatDetectTest(unittest.TestCase):
    def test_detects_by_content_not_extension(self) -> None:
        self.assertEqual(flash_file.detect_format("@1000\n01\nq\n"), "ti-txt")
        self.assertEqual(flash_file.detect_format(":00000001FF\n"), "ihex")
        self.assertEqual(flash_file.detect_format("S00600004844521B\n"), "srec")

    def test_unknown_content_is_rejected(self) -> None:
        with self.assertRaisesRegex(flash_file.FlashError, "认不出"):
            flash_file.parse_text("hello world\n")


class FileTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def path(self, name: str) -> str:
        return os.path.join(self.tmp.name, name)

    def test_binary_with_base_address(self) -> None:
        path = self.path("app.bin")
        with open(path, "wb") as handle:
            handle.write(b"\x01\x02\x03")
        image = flash_file.parse_file(path, base_address=0x8000)
        self.assertEqual(image.fmt, "bin")
        self.assertEqual(image.start_address, 0x8000)
        self.assertTrue(any("0x00008000" in note for note in image.notes))

    def test_out_file_is_explained(self) -> None:
        path = self.path("app.out")
        with open(path, "wb") as handle:
            handle.write(b"\x7fELF")
        with self.assertRaisesRegex(flash_file.FlashError, "hex2000|hex 或 bin"):
            flash_file.parse_file(path)

    def test_missing_file(self) -> None:
        with self.assertRaisesRegex(flash_file.FlashError, "不存在"):
            flash_file.parse_file(self.path("nope.hex"))

    def test_ti_txt_round_trip(self) -> None:
        path = self.path("app.hex")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("@1000\n01 02 03\n@1010\nAA\nq\n")
        image = flash_file.parse_file(path)
        again = flash_file.parse_text(image.to_ti_txt())
        self.assertEqual(again.blocks, image.blocks)


class ImageMathTest(unittest.TestCase):
    def test_checksums_are_stable(self) -> None:
        image = flash_file.parse_text("@0000\n01 02 03 04\nq\n")
        self.assertEqual(image.checksum16(), (0x0102 + 0x0304) & 0xFFFF)
        self.assertEqual(image.crc32(), flash_file.FlashImage(image.blocks).crc32())

    def test_words_are_little_endian_by_default(self) -> None:
        image = flash_file.parse_text("@0000\n34 12\nq\n")
        self.assertEqual(image.words(), [(0, 0x1234)])

    def test_words_big_endian_option(self) -> None:
        image = flash_file.parse_text("@0000\n12 34\nq\n")
        self.assertEqual(image.words("big"), [(0, 0x1234)])

    def test_odd_address_and_length_are_padded(self) -> None:
        image = flash_file.parse_text("@0001\nAA BB CC\nq\n")
        words = image.words()
        # 小端：低字节在前。地址 0 补 0xFF（低字节），所以 0xFF+0xAA → 0xAAFF
        self.assertEqual(words[0], (0, 0xAAFF), "奇数地址前面补 0xFF")
        self.assertEqual(words[-1], (1, 0xCCBB), "奇数长度后面补 0xFF")

    def test_overlap_with_different_data_is_rejected(self) -> None:
        with self.assertRaisesRegex(flash_file.FlashError, "重叠"):
            flash_file.FlashImage(
                [
                    flash_file.FlashBlock(0x1000, b"\x01\x02"),
                    flash_file.FlashBlock(0x1001, b"\x09"),
                ]
            )

    def test_overlap_with_same_data_is_merged(self) -> None:
        image = flash_file.FlashImage(
            [
                flash_file.FlashBlock(0x1000, b"\x01\x02"),
                flash_file.FlashBlock(0x1001, b"\x02\x03"),
            ]
        )
        self.assertEqual(len(image.blocks), 1)
        self.assertEqual(image.blocks[0].data, b"\x01\x02\x03")


class DiscoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)

    def make(self, relative: str, text: str = "@1000\n01\nq\n") -> str:
        path = os.path.join(self.tmp.name, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_finds_files_recursively_and_sorts_by_time(self) -> None:
        older = self.make("proj/fw/old.hex")
        newer = self.make("proj/app.hex")
        self.make("proj/notes.md", "x")
        os.utime(older, (1_000_000, 1_000_000))
        os.utime(newer, (2_000_000, 2_000_000))
        found = flash_file.discover_files([os.path.join(self.tmp.name, "proj")])
        self.assertEqual([entry.name for entry in found], ["app.hex", "old.hex"])

    def test_skips_build_and_vcs_dirs(self) -> None:
        self.make("proj/build/generated.hex")
        self.make("proj/.git/hook.bin")
        self.make("proj/real.hex")
        found = flash_file.discover_files([os.path.join(self.tmp.name, "proj")])
        self.assertEqual([entry.name for entry in found], ["real.hex"])

    def test_limit_and_missing_roots_are_safe(self) -> None:
        for index in range(5):
            self.make(f"proj/f{index}.hex")
        found = flash_file.discover_files(
            [os.path.join(self.tmp.name, "proj"), os.path.join(self.tmp.name, "nope")],
            limit=3,
        )
        self.assertEqual(len(found), 3)

    def test_flash_file_text_helpers(self) -> None:
        path = self.make("proj/a.hex")
        stat = os.stat(path)
        entry = flash_file.FlashFile(path=path, size=stat.st_size, mtime=stat.st_mtime)
        self.assertEqual(entry.name, "a.hex")
        self.assertEqual(entry.extension, ".hex")
        self.assertTrue(entry.size_text().endswith(("B", "KB", "MB")))
        self.assertRegex(entry.time_text(), r"\d{4}-\d{2}-\d{2}")


if __name__ == "__main__":
    unittest.main()
