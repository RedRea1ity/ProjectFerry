"""Offline checks for human-first, opt-in corpus translation."""

import json
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import mc_ai_translator as core


class CorpusTranslationTests(unittest.TestCase):
    def setUp(self):
        self.english = [core.LangFile("example", "en_us", "mod.jar", {
            "item.a": "Apple", "item.b": "Red Apple", "item.c": "Pear",
        })]
        self.community = [core.LangFile("example", "zh_cn", "mod.jar", {
            "item.a": "苹果", "item.b": "红苹果",
        })]

    def test_only_missing_keys_are_eligible(self):
        self.assertEqual(core.missing_entries(self.english, self.community, []), [])
        self.assertEqual([e.key for e in core.missing_entries(
            self.english, self.community, [], fill_community=True,
        )], ["item.c"])
        self.assertEqual([e.key for e in core.missing_entries(
            self.english, self.community, [], refine_community=True,
        )], ["item.c"])
        ai = [core.LangFile("example", "zh_cn", "ai.zip", {"item.c": "梨"}, True)]
        self.assertEqual(core.missing_entries(
            self.english, self.community, ai, refine_community=True,
        ), [])
        self.assertEqual(core.missing_entries(
            self.english, self.community, [], refine_community=True,
            reverted={"example": {"item.c"}},
        ), [])

    def test_blank_english_values_are_not_counted_as_missing(self):
        english = [core.LangFile("example", "en_us", "mod.jar", {
            "translated": "Hello", "blank": "", "whitespace": "   ",
        })]
        ai = [core.LangFile("example", "zh_cn", "ai.zip", {"translated": "你好"}, True)]
        status = core.build_mod_status(english, [], ai)[0]
        self.assertEqual((status.total_keys, status.ai_keys, status.missing), (1, 1, 0))
        self.assertTrue(status.fully_translated)
        self.assertEqual(core.missing_entries(english, [], ai), [])

    def test_complete_coverage_uses_disjoint_human_and_ai_counts(self):
        ai = [core.LangFile("example", "zh_cn", "ai.zip", {
            "item.a": "旧AI苹果", "item.c": "梨",
        }, True)]
        status = core.build_mod_status(self.english, self.community, ai)[0]
        self.assertTrue(status.fully_translated)
        self.assertEqual((status.total_keys, status.community_keys, status.ai_keys, status.missing), (3, 2, 1, 0))
        self.assertIn("人工汉化：2 个（66.7%）", core.translation_coverage_label(status))
        self.assertIn("AI 补全：1 个（33.3%）", core.translation_coverage_label(status))

        human_only = core.build_mod_status(self.english, [core.LangFile(
            "example", "zh_cn", "human.zip", {"item.a": "苹果", "item.b": "红苹果", "item.c": "梨"},
        )], [])[0]
        self.assertTrue(human_only.fully_translated)
        self.assertIn("AI 补全：0 个（0.0%）", core.translation_coverage_label(human_only))

        incomplete = core.build_mod_status(self.english, self.community, [])[0]
        self.assertFalse(incomplete.fully_translated)
        self.assertEqual(core.translation_coverage_label(incomplete), "")
        reverted = core.build_mod_status(self.english, self.community, ai, reverted={"example": {"item.c"}})[0]
        self.assertFalse(reverted.fully_translated)
        self.assertEqual(core.translation_coverage_label(reverted), "")

    def test_model_uses_references_without_returning_human_keys(self):
        corpus = core.community_corpus(self.english, self.community)
        entries = core.missing_entries(self.english, self.community, [], refine_community=True)
        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                "cache_dir": str(Path(tmp) / "cache"), "batch_size": 3, "concurrency": 1,
                "glossary": {}, "keep": set(), "delay": 0, "timeout": 5,
                "refine_community": True, "engine": "openai", "model": "test",
                "base_url": "https://api.example.test/v1", "api_key": "test",
            }
            calls = []

            def fake_post(_url, payload, _headers, _timeout):
                request = json.loads(payload["messages"][1]["content"])
                calls.append(request)
                self.assertEqual(set(request["strings"]), {"item.c"})
                self.assertEqual({item["zh"] for item in request["references"]}, {"苹果", "红苹果"})
                return {"choices": [{"message": {"content": json.dumps({
                    "item.c": "梨", "item.a": "禁止覆盖苹果",
                }, ensure_ascii=False)}}]}

            with patch.object(core, "_http_post_json", side_effect=fake_post):
                translations, errors, failed = core.translate_entries(entries, settings, corpus=corpus)
            self.assertFalse(errors or failed)
            self.assertEqual(len(calls), 1)
            self.assertEqual(translations, {"example": {"item.c": "梨"}})
            pack = Path(tmp) / "pack.zip"
            core.write_pack(pack, translations, 15)
            self.assertEqual(core.load_pack_translations(pack), translations)
            self.assertEqual(core.missing_entries(
                self.english, self.community, core.language_files_in_zip(pack), refine_community=True,
            ), [])
            self.assertNotEqual(
                core.cache_key(entries, "openai", "test"),
                core.cache_key(entries, "openai", "test", references=core.reference_examples(entries, corpus["example"])),
            )

    def test_ai_pack_yields_to_human_translations_and_leaves_other_mods(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "ai.zip"
            core.write_pack(pack, {"example": {"item.a": "旧AI苹果", "item.c": "AI梨"}, "other": {"key": "保留"}}, 15)
            scan = core.ScanResult(community=self.community, ai=core.language_files_in_zip(pack))
            self.assertTrue(core.yield_to_community(scan, pack))
            self.assertEqual(core.load_pack_translations(pack), {"example": {"item.c": "AI梨"}, "other": {"key": "保留"}})
            self.assertEqual(core.build_mod_status(self.english, self.community, scan.ai)[0].ai_keys, 1)
            self.assertFalse(core.yield_to_community(scan, pack))
            self.assertEqual(core.merge_pack_translations(pack, {"example": {"item.a": "意外覆盖"}}, self.community)["example"], {"item.c": "AI梨"})

            full = [core.LangFile("example", "zh_cn", "human.zip", {
                "item.a": "苹果", "item.b": "红苹果", "item.c": "梨",
            })]
            full_scan = core.ScanResult(english=self.english, community=full, ai=core.language_files_in_zip(pack))
            self.assertTrue(core.yield_to_community(full_scan, pack))
            self.assertEqual(core.load_pack_translations(pack), {"other": {"key": "保留"}})
            self.assertEqual(core.missing_entries(self.english, full, full_scan.ai, refine_community=True), [])

    def test_empty_ai_pack_is_removed_but_explicit_english_override_survives(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "ai.zip"
            core.write_pack(pack, {"example": {"item.a": "AI苹果"}}, 15)
            self.assertTrue(core.yield_to_community(core.ScanResult(community=self.community, ai=core.language_files_in_zip(pack)), pack))
            self.assertFalse(pack.exists())

            core.write_pack(pack, {"example": {"item.a": "Apple"}}, 15, {"example": {"item.a"}})
            self.assertFalse(core.yield_to_community(core.ScanResult(community=self.community, ai=core.language_files_in_zip(pack)), pack))
            with zipfile.ZipFile(pack) as zf:
                self.assertIn(core.REVERT_MARKER, zf.namelist())

    def test_uninstall_and_reload_preserve_human_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "ai.zip"
            core.write_pack(pack, {"example": {"item.a": "旧 AI 苹果", "item.c": "AI 梨"}, "other": {"x": "其他"}}, 15)
            self.assertEqual(core.remove_ai_translations(pack, {"example"}), ["example"])
            self.assertEqual(core.load_pack_translations(pack), {"other": {"x": "其他"}})
            self.assertEqual(core.load_uninstalled_ai(pack)["example"], {"item.a": "旧 AI 苹果", "item.c": "AI 梨"})
            scan = core.ScanResult(english=self.english, community=self.community)
            self.assertEqual(core.reload_ai_translations(pack, {"example"}, scan, 15), ["example"])
            self.assertEqual(core.load_pack_translations(pack)["example"], {"item.c": "AI 梨"})
            self.assertFalse(core.uninstalled_ai_path(pack).exists())

    def test_uninstall_last_mod_deletes_pack_and_reload_recreates_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "ai.zip"
            core.write_pack(pack, {"example": {"item.c": "AI 梨"}}, 15)
            self.assertEqual(core.remove_ai_translations(pack, {"example"}), ["example"])
            self.assertFalse(pack.exists())
            self.assertIn("example", core.load_uninstalled_ai(pack))
            scan = core.ScanResult(english=self.english, community=self.community)
            self.assertEqual(core.reload_ai_translations(pack, {"example"}, scan, 15), ["example"])
            self.assertEqual(core.load_pack_translations(pack)["example"], {"item.c": "AI 梨"})

    def test_explicit_english_override_is_not_uninstalled(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "ai.zip"
            core.write_pack(pack, {"example": {"item.a": "Apple", "item.c": "AI 梨"}}, 15, {"example": {"item.a"}})
            self.assertEqual(core.remove_ai_translations(pack, {"example"}), ["example"])
            self.assertEqual(core.load_pack_translations(pack)["example"], {"item.a": "Apple"})
            self.assertEqual(core.load_uninstalled_ai(pack)["example"], {"item.c": "AI 梨"})

    def test_free_engine_rejected_before_request(self):
        entries = core.missing_entries(self.english, self.community, [], refine_community=True)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "精加工需要"):
                core.translate_entries(entries, {
                    "cache_dir": tmp, "batch_size": 3, "concurrency": 1,
                    "refine_community": True, "engine": "mymemory", "model": "mymemory",
                }, corpus=core.community_corpus(self.english, self.community))


class HumanPackDownloadTests(unittest.TestCase):
    @staticmethod
    def _zip_pack(namespace="example", marker=False):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as zf:
            zf.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 15, "description": "人工汉化"}}))
            zf.writestr(f"assets/{namespace}/lang/zh_cn.json", json.dumps({"item.c": "人工梨"}, ensure_ascii=False))
            if marker:
                zf.writestr(core.AI_MARKER, "AI")
        return buffer.getvalue()

    def test_downloaded_pack_is_recognized_as_human_and_ai_yields(self):
        class Download(io.BytesIO):
            def geturl(self):
                return "https://example.invalid/human.zip"

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "mods").mkdir()
            with patch.object(core.urllib.request, "urlopen", return_value=Download(self._zip_pack())):
                dest = core.download_human_pack("https://example.invalid/human.zip", instance, "example")
            self.assertEqual(dest, core.human_pack_destination(instance, "example"))
            scan = core.scan_inputs(instance / "mods", instance / "resourcepacks")
            self.assertEqual(len(scan.community), 1)
            self.assertEqual(scan.community[0].data["item.c"], "人工梨")
            self.assertFalse(scan.ai)

            ai_pack = instance / "resourcepacks" / "ai.zip"
            core.write_pack(ai_pack, {"example": {"item.c": "旧 AI 梨"}}, 15)
            scan = core.scan_inputs(instance / "mods", instance / "resourcepacks")
            self.assertTrue(core.yield_to_community(scan, ai_pack))
            self.assertFalse(ai_pack.exists())

    def test_invalid_or_ai_pack_cannot_replace_existing_human_pack(self):
        class Download(io.BytesIO):
            def geturl(self):
                return "https://example.invalid/human.zip"

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "mods").mkdir()
            dest = core.human_pack_destination(instance, "example")
            dest.parent.mkdir()
            dest.write_bytes(self._zip_pack())
            original = dest.read_bytes()
            for invalid in (self._zip_pack("wrong"), self._zip_pack(marker=True), b"not a zip"):
                with patch.object(core.urllib.request, "urlopen", return_value=Download(invalid)):
                    with self.assertRaises(ValueError):
                        core.download_human_pack("https://example.invalid/human.zip", instance, "example")
                self.assertEqual(dest.read_bytes(), original)
                self.assertEqual(list(dest.parent.glob("*.part")), [])
            with self.assertRaisesRegex(ValueError, "HTTPS"):
                core.download_human_pack("http://example.invalid/human.zip", instance, "example")


if __name__ == "__main__":
    unittest.main()
