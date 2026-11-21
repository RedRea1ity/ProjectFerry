# -*- coding: utf-8 -*-
"""已汉化发现 + 汉化任务断点续传。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ferrycore import localize as ferrylocalize
from ferrycore.adapters.undo import UNDO_NAME, UndoLog
from ferrycore.localize_job import load_job, touch_progress
from ferrycore.localized import find_localized, remember, LocalizedRecord


class LocalizedDiscoveryTests(unittest.TestCase):
    def test_find_in_place_from_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Stay Stay DPRK"
            game = root / "game"
            game.mkdir(parents=True)
            script = game / "script.rpy"
            script.write_text('label start:\n    "Hello"\n', encoding="utf-8")
            log = UndoLog(root, engine="renpy")
            log.will_modify("game/script.rpy")
            script.write_text('label start:\n    "你好"\n', encoding="utf-8")
            log.did_write("game/script.rpy")
            log.finish()
            self.assertTrue((root / UNDO_NAME).is_file())

            found = find_localized(root)
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].mode, "in_place")
            self.assertEqual(Path(found[0].play_path), root.resolve())

    def test_find_copy_from_undo_and_sibling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "HelloCharlotte"
            root.mkdir()
            copy = Path(tmp) / "HelloCharlotte.cn"
            copy.mkdir()
            (copy / "Game.exe").write_bytes(b"MZ")
            log = UndoLog(root, engine="rpgmaker_ace")
            log.did_copy(copy)
            log.finish()

            found = find_localized(root)
            paths = {Path(r.play_path).resolve() for r in found}
            self.assertIn(copy.resolve(), paths)

    def test_remember_and_history_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "game"
            root.mkdir()
            with patch("ferrycore.localized.app_dir", return_value=Path(tmp)):
                remember(
                    LocalizedRecord(
                        root=str(root),
                        play_path=str(root),
                        mode="in_place",
                        engine="renpy",
                        label=root.name,
                        created="2026-01-01T00:00:00",
                    )
                )
                lines = ferrylocalize.existing_localize_lines(root)
            self.assertTrue(any("原地改写" in line or "已发现" in line for line in lines))


class LocalizeJobResumeTests(unittest.TestCase):
    def test_job_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "g"
            root.mkdir()
            with patch("ferrycore.localize_job.app_dir", return_value=Path(tmp)):
                touch_progress(root, adapter="renpy", status="cancelled", entries=100, translated=40)
                job = load_job(root)
                self.assertIsNotNone(job)
                assert job is not None
                self.assertTrue(job.resumable)
                self.assertEqual(job.translated, 40)

    def test_localize_resumes_cached_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "Stay"
            game = root / "game"
            game.mkdir(parents=True)
            (root / "renpy").mkdir()
            (game / "script.rpy").write_text(
                'label start:\n    "Alpha line."\n    "Beta line."\n',
                encoding="utf-8",
            )
            cache = Path(tmp) / "ferry_cache"
            cache.mkdir()
            settings = {
                "engine": "mymemory",
                "model": "-",
                "api_key": "",
                "base_url": "",
                "batch_size": 50,
                "timeout": 30,
                "delay": 0,
                "concurrency": 1,
                "cache_dir": str(cache),
                "glossary": {},
                "keep": set(),
                "localize_output_dir": "",
            }

            calls = {"n": 0}

            def fake_translate(entries, settings, progress=None, cancel_event=None, **_kw):
                calls["n"] += 1
                flat: dict[str, dict[str, str]] = {}
                for entry in entries:
                    flat.setdefault(entry.modid, {})[entry.key] = f"译:{entry.english}"
                if progress:
                    progress(len(entries), len(entries), entries[0].modid)
                return flat, [], {}

            with patch("ferrycore.localize_job.app_dir", return_value=Path(tmp)), patch(
                "ferrycore.localized.app_dir", return_value=Path(tmp)
            ), patch("ferrycore.localize.translate_entries", side_effect=fake_translate):
                touch_progress(root, adapter="renpy", status="cancelled", entries=2, translated=1)
                result = ferrylocalize.localize_game(root, settings=settings)
                self.assertTrue(result.resumed)
                self.assertTrue(result.in_place)
                self.assertEqual(result.out_path, root.resolve())
                self.assertGreater(result.applied, 0)
                self.assertEqual(calls["n"], 1)
                text = (game / "script.rpy").read_text(encoding="utf-8")
                self.assertIn("译:", text)

                lines = ferrylocalize.existing_localize_lines(root)
                self.assertTrue(any("原地改写" in line or str(root) in line for line in lines))


if __name__ == "__main__":
    unittest.main()
