# -*- coding: utf-8 -*-
"""RPA-3.0 + Ren'Py 归档剧本探测/抽出/回写。"""
from __future__ import annotations

import pickle
import tempfile
import unittest
import zlib
from pathlib import Path

from ferrycore import adapters
from ferrycore.adapters.rpa import open_archive


def build_rpa3(path: Path, files: dict[str, bytes], key: int = 0x42424242) -> None:
    """写一个最小可用的 RPA-3.0，供单测用。"""
    # 先占位头，再写内容，再写索引
    header = f"RPA-3.0 {0:016x} {key:08x}\n".encode("ascii")
    blob = bytearray(header)
    index: dict[str, list[tuple[int, int, bytes]]] = {}
    for name, data in files.items():
        while len(blob) % 16:
            blob.append(0)
        offset = len(blob)
        blob.extend(data)
        # 存盘时 offset/length 要 XOR key（引擎读取时再解）
        index[name] = [(offset ^ key, len(data) ^ key, b"")]
    while len(blob) % 16:
        blob.append(0)
    index_off = len(blob)
    blob.extend(zlib.compress(pickle.dumps(index, protocol=2)))
    # 回填头里的 index offset
    new_header = f"RPA-3.0 {index_off:016x} {key:08x}\n".encode("ascii")
    if len(new_header) != len(header):
        raise RuntimeError("header length shifted")
    blob[: len(header)] = new_header
    path.write_bytes(bytes(blob))


class RpaArchiveTests(unittest.TestCase):
    def test_roundtrip_read_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            rpa = Path(tmp) / "archive.rpa"
            script = b'label start:\n    "Hello world from RPA."\n'
            build_rpa3(rpa, {"script.rpy": script, "gui.png": b"\x89PNG"})
            arch = open_archive(rpa)
            self.assertEqual(arch.version, 3)
            self.assertEqual(arch.scripts(), ["script.rpy"])
            self.assertEqual(arch.read("script.rpy"), script)

    def test_extract_skips_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            game = Path(tmp) / "game"
            game.mkdir()
            rpa = game / "archive.rpa"
            build_rpa3(rpa, {"a.rpy": b'"one"\n', "b.rpy": b'"two"\n'})
            (game / "a.rpy").write_text('"kept"\n', encoding="utf-8")
            written = open_archive(rpa).extract_scripts(game)
            self.assertEqual([p.name for p in written], ["b.rpy"])
            self.assertEqual((game / "a.rpy").read_text(encoding="utf-8"), '"kept"\n')
            self.assertEqual((game / "b.rpy").read_bytes(), b'"two"\n')


class RenpyRpaAdapterTests(unittest.TestCase):
    SCRIPT = 'label start:\n    "Rain is falling."\n    e "Hello, [player]."\n'

    def make_rpa_game(self, base: Path) -> Path:
        root = base / "StayStay"
        game = root / "game"
        game.mkdir(parents=True)
        build_rpa3(game / "archive.rpa", {"script.rpy": self.SCRIPT.encode("utf-8")})
        # Ren'Py 壳目录信号
        (root / "renpy").mkdir()
        return root

    def test_probe_rpa_only_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_rpa_game(Path(tmp))
            report = adapters.RenpyAdapter().probe(root)
            self.assertTrue(report.readable)
            self.assertTrue(report.writable)
            self.assertEqual(report.container, "rpa")
            self.assertGreaterEqual(report.unit_count, 2)
            self.assertTrue(any(".rpa" in n for n in report.notes))

    def test_collect_from_rpa_without_extract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_rpa_game(Path(tmp))
            entries = adapters.RenpyAdapter().collect(root)
            texts = [e.english for e in entries]
            self.assertIn("Rain is falling.", texts)
            self.assertIn("Hello, [player].", texts)
            # 不应落盘
            self.assertFalse((root / "game" / "script.rpy").exists())

    def test_apply_extracts_then_translates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_rpa_game(Path(tmp))
            adapter = adapters.RenpyAdapter()
            entries = adapter.collect(root)
            translations = {
                e.key: (
                    "下着雨。"
                    if e.english == "Rain is falling."
                    else "你好，[player]。"
                    if e.english == "Hello, [player]."
                    else e.english
                )
                for e in entries
            }
            applied = adapter.apply(root, translations)
            self.assertGreaterEqual(applied.applied, 2)
            self.assertTrue((root / "game" / "script.rpy").exists())
            text = (root / "game" / "script.rpy").read_text(encoding="utf-8")
            self.assertIn("下着雨。", text)
            self.assertIn("你好，[player]。", text)
            # revert 应删掉抽出的散装（added）
            report = adapters.revert_game(root)
            self.assertTrue(report.ok, report.failed)
            self.assertFalse((root / "game" / "script.rpy").exists())

    def test_detect_rpa_renpy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make_rpa_game(Path(tmp))
            found = adapters.detect(root)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "renpy")


if __name__ == "__main__":
    unittest.main()
