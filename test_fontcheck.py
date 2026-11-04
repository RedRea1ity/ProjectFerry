"""字库检查的测试：不依赖任何本机字体文件。

CI 上没有 Windows 字体，所以这里用 struct 手工拼最小 sfnt（只有 cmap 表），
覆盖 format 4 与 format 12 两条解析路径。字体解析最容易出的错是偏移算错一个
字节 —— 那种错在真实字体上表现为「覆盖率莫名其妙偏低」，很难看出来，
所以这里直接断言具体码点而不是只看总数。
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from ferrycore.fontcheck import (
    FontCoverage,
    FontScanResult,
    check_font,
    gb2312_chars,
    read_font_codepoints,
    scan_fonts,
)


def _format4(segments: list[tuple[int, int]]) -> bytes:
    """拼一个 cmap format 4 子表。每段从 1 开始连续编号字形。"""
    segs = list(segments) + [(0xFFFF, 0xFFFF)]  # 规范要求的哨兵段
    seg = len(segs)
    ends = b"".join(struct.pack(">H", e) for _s, e in segs)
    starts = b"".join(struct.pack(">H", s) for s, _e in segs)
    deltas = b"".join(struct.pack(">h", 1 - s if s != 0xFFFF else 1) for s, _e in segs)
    ranges = b"\x00\x00" * seg
    body = ends + b"\x00\x00" + starts + deltas + ranges
    header = struct.pack(">HHHHHHH", 4, 14 + len(body), 0, seg * 2, 0, 0, 0)
    return header + body


def _format12(specs: list[tuple[int, int]]) -> bytes:
    groups = b"".join(struct.pack(">III", s, e, 1) for s, e in specs)
    return struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, len(specs)) + groups


def _font(subtable: bytes, encoding_id: int = 1) -> bytes:
    """把子表包成 cmap 表，再包成只有一个表的 sfnt。"""
    cmap = struct.pack(">HH", 0, 1) + struct.pack(">HHI", 3, encoding_id, 12) + subtable
    assert struct.unpack_from(">H", cmap, 12)[0] in (4, 12)
    head = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    record = b"cmap" + struct.pack(">III", 0, 12 + 16, len(cmap))
    return head + record + cmap


class Gb2312Tests(unittest.TestCase):
    def test_level_counts(self):
        self.assertEqual(len(gb2312_chars(1)), 3755)
        self.assertEqual(len(gb2312_chars(2)), 3008)

    def test_level1_starts_with_a(self):
        # GB2312 一级汉字第一个是「啊」，最后一个在「座」一带
        self.assertEqual(gb2312_chars(1)[0], "啊")

    def test_bad_level(self):
        with self.assertRaises(ValueError):
            gb2312_chars(3)


class FontFormatTests(unittest.TestCase):
    def test_format4_full_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "full.ttf"
            path.write_bytes(_font(_format4([(0x4E00, 0x9FFF)])))
            cov = check_font(path)
            self.assertEqual(cov.level, "ok")
            self.assertEqual(cov.covered, cov.total)
            self.assertEqual(cov.missing, [])

    def test_format4_no_cjk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "latin.ttf"
            # 只覆盖 ASCII，正是 Arial 那类西文字体的情形
            path.write_bytes(_font(_format4([(0x0020, 0x007E)])))
            cov = check_font(path)
            self.assertEqual(cov.covered, 0)
            self.assertEqual(cov.level, "broken")

    def test_format4_partial_is_tight_or_broken(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.ttf"
            # 只覆盖 CJK 区的后半段：能命中一部分一级字，但远不够用
            path.write_bytes(_font(_format4([(0x8000, 0x9FFF)])))
            cov = check_font(path)
            self.assertIn(cov.level, ("tight", "broken"))
            self.assertGreater(cov.covered, 0)
            self.assertLess(cov.covered, cov.total)

    def test_format12_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fmt12.ttf"
            path.write_bytes(_font(_format12([(0x4E00, 0x9FFF)]), encoding_id=10))
            cov = check_font(path)
            self.assertEqual(cov.level, "ok")
            self.assertEqual(cov.covered, cov.total)

    def test_read_codepoints_returns_specific(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "two.ttf"
            path.write_bytes(_font(_format4([(0x4E00, 0x4E10)])))
            parsed = read_font_codepoints(path)
            self.assertIsNotNone(parsed)
            points, _family = parsed
            self.assertIn(0x4E00, points)
            self.assertIn(0x4E10, points)
            self.assertNotIn(0x4E11, points)


class BadInputTests(unittest.TestCase):
    def test_missing_file(self):
        cov = check_font(Path("no/such/font.ttf"))
        self.assertEqual(cov.level, "unknown")

    def test_not_a_font(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fake.ttf"
            path.write_bytes(b"this is definitely not a font file" * 4)
            self.assertEqual(check_font(path).level, "unknown")

    def test_truncated_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.ttf"
            path.write_bytes(b"\x00\x01\x00\x00")
            self.assertIsNone(read_font_codepoints(path))


class ScanTests(unittest.TestCase):
    def test_finds_font_and_bitmap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game").mkdir()
            (root / "game" / "font.ttf").write_bytes(_font(_format4([(0x4E00, 0x9FFF)])))
            (root / "game" / "ui.fnt").write_bytes(b"bitmap font placeholder")
            scan = scan_fonts(root)
            self.assertEqual(len(scan.found), 1)
            self.assertEqual(len(scan.bitmap), 1)
            self.assertEqual(scan.verdict, "ok")

    def test_no_font_gives_no_green_light(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = scan_fonts(Path(tmp))
            self.assertEqual(scan.found, [])
            self.assertEqual(scan.verdict, "unknown")
            self.assertIn("未发现独立字体文件", scan.summary())

    def test_verdict_takes_best_font(self):
        """装饰字体缺字不该把能玩的游戏判死。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "main.ttf").write_bytes(_font(_format4([(0x4E00, 0x9FFF)])))
            (root / "deco.ttf").write_bytes(_font(_format4([(0x0020, 0x007E)])))
            scan = scan_fonts(root)
            self.assertEqual(len(scan.found), 2)
            self.assertEqual(scan.verdict, "ok")

    def test_single_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "one.ttf"
            path.write_bytes(_font(_format4([(0x4E00, 0x9FFF)])))
            scan = scan_fonts(path)
            self.assertEqual(len(scan.found), 1)

    def test_depth_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            deep = Path(tmp)
            for i in range(8):
                deep = deep / f"d{i}"
            deep.mkdir(parents=True)
            (deep / "font.ttf").write_bytes(_font(_format4([(0x4E00, 0x9FFF)])))
            scan = scan_fonts(Path(tmp), depth=3)
            self.assertEqual(scan.found, [])


class SummaryTests(unittest.TestCase):
    def test_summary_wording(self):
        cov = FontCoverage(path=Path("a.ttf"), total=3755, covered=0, level="broken")
        self.assertIn("中文显示不了", cov.summary())
        cov2 = FontCoverage(path=Path("b.ttf"), total=3755, covered=3755, level="ok")
        self.assertIn("可显示中文", cov2.summary())

    def test_empty_scan_summary(self):
        self.assertEqual(FontScanResult(note="未发现").summary(), "未发现")


if __name__ == "__main__":
    unittest.main()
