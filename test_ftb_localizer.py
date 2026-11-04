"""FTB Quest Localizer JSON component integration tests."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ferrycore.json_components import component_entries, extract_text_nodes
from ferrycore.models import LangFile, ScanResult
from ferrycore.packio import load_pack_ftb_components, load_pack_ftb_sources, write_pack
from ferrycore.scan import (
    _modid_keys,
    language_files_in_directory,
    missing_ftb_entries,
    scan_inputs,
    translated_ftb_components,
)


class FtbLocalizerComponentTests(unittest.TestCase):
    def test_extracts_strings_text_and_show_text_but_not_metadata(self):
        value = [
            "visible legacy component",
            {
                "text": "Base",
                "color": "gold",
                "clickEvent": {"action": "run_command", "value": "/say opaque"},
                "extra": [{"text": "Extra"}],
                "with": [{"text": "Argument"}],
                "hoverEvent": {
                    "action": "show_text",
                    "contents": [{"text": "Hover"}],
                },
            },
            {"text": "Not user-facing", "hoverEvent": {"action": "show_item", "contents": {"id": "minecraft:stone"}}},
        ]
        self.assertEqual(
            extract_text_nodes(value),
            [
                ("/0", "visible legacy component"),
                ("/1/text", "Base"),
                ("/1/extra/0/text", "Extra"),
                ("/1/with/0/text", "Argument"),
                ("/1/hoverEvent/contents/0/text", "Hover"),
                ("/2/text", "Not user-facing"),
            ],
        )

    def test_fixed_localizer_export_scanned_but_jar_arrays_are_not_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            mods = instance / "mods"
            mods.mkdir()
            kube_lang = instance / "kubejs/assets/ftbquestlocalizer/lang"
            kube_lang.mkdir(parents=True)
            localizer = {
                "quest.alpha": ["A visible sentence", {"text": "Nested sentence", "clickEvent": {"action": "run_command", "value": "/x"}}],
                "scalar.key": "Normal scalar",
            }
            source_path = kube_lang / "en_us.json"
            source_path.write_text(json.dumps(localizer), encoding="utf-8")
            jar_path = mods / "ftbquestlocalizer.jar"
            with zipfile.ZipFile(jar_path, "w") as archive:
                archive.writestr("assets/ftbquestlocalizer/lang/en_us.json", json.dumps({"jar.array": [{"text": "Do not use"}]}))
            scan = scan_inputs(mods, None, instance)
            self.assertEqual(scan.ftb_localizer, {"quest.alpha": localizer["quest.alpha"]})
            self.assertEqual(scan.ftb_localizer_source, str(source_path))
            self.assertNotIn("jar.array", scan.ftb_localizer)
            self.assertEqual([e.key for e in component_entries("ftbquestlocalizer", scan.ftb_localizer, str(source_path))], [
                "ftblocalizer:quest.alpha#/0", "ftblocalizer:quest.alpha#/1/text",
            ])
            # 导出文件里的普通字符串 key 仍走常规翻译流程。
            self.assertIn("scalar.key", {key for file in scan.english for key in file.data})

    def test_components_are_attached_only_to_localizer_namespace_and_count_as_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "pack"
            lang = root / "assets/ftbquestlocalizer/lang"
            lang.mkdir(parents=True)
            (lang / "zh_cn.json").write_text(json.dumps({"quest.alpha": [{"text": "人工译文"}], "scalar": "人工"}), encoding="utf-8")
            mod_lang = root / "assets/othermod/lang"
            mod_lang.mkdir(parents=True)
            (mod_lang / "zh_cn.json").write_text(json.dumps({"other": [{"text": "opaque"}]}), encoding="utf-8")
            files = language_files_in_directory(root)
            localizer_file = next(f for f in files if f.modid == "ftbquestlocalizer")
            other_file = next(f for f in files if f.modid == "othermod")
            self.assertEqual(localizer_file.components, {"quest.alpha": [{"text": "人工译文"}]})
            self.assertEqual(other_file.components, {})
            self.assertIn("ftblocalizer:quest.alpha#/0/text", _modid_keys([localizer_file])["ftbquestlocalizer"])

    def test_missing_filtering_and_partial_merge_preserve_prior(self):
        source = {
            "quest.a": [
                {"text": "A", "color": "gold", "clickEvent": {"action": "run_command", "value": "/keep"}},
                {"text": "B"},
            ],
            "quest.b": {"text": "C"},
        }
        scan = ScanResult(ftb_localizer=source, ftb_localizer_source="kubejs export")
        scan.reverted = {"ftbquestlocalizer": {"ftblocalizer:quest.b#/text"}}
        prior = {"quest.a": [{"text": "甲", "color": "gold", "clickEvent": {"action": "run_command", "value": "/keep"}}, {"text": "B"}]}
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "translation.zip"
            write_pack(pack, {}, 15, ftb_components=prior, ftb_sources={"quest.a": {"/0/text": "A"}})
            self.assertEqual(load_pack_ftb_sources(pack), {"quest.a": {"/0/text": "A"}})
            missing = missing_ftb_entries(scan, pack)
            self.assertEqual([entry.key for entry in missing], ["ftblocalizer:quest.a#/1/text"])
            late = component_entries("ftbquestlocalizer", source, "kubejs export")
            translations = {"ftbquestlocalizer": {"ftblocalizer:quest.a#/1/text": "乙"}}
            merged = translated_ftb_components(scan, late, translations, pack)
            self.assertEqual(merged["quest.a"][0]["text"], "甲")
            self.assertEqual(merged["quest.a"][1]["text"], "乙")
            self.assertEqual(merged["quest.a"][0]["clickEvent"], source["quest.a"][0]["clickEvent"])
            self.assertEqual(merged["quest.a"][0]["color"], "gold")
            self.assertEqual(merged["quest.b"]["text"], "C", "reverted entry restored to source English")
            write_pack(pack, {}, 15, ftb_components=merged, ftb_sources={"quest.a": {"/0/text": "A", "/1/text": "B"}})
            self.assertEqual(load_pack_ftb_components(pack), merged)
            self.assertEqual(load_pack_ftb_sources(pack), {"quest.a": {"/0/text": "A", "/1/text": "B"}})
            self.assertEqual(source["quest.a"][0]["text"], "A", "source structure is read-only")

    def test_human_translation_yields_whole_language_key(self):
        source = {"quest.a": [{"text": "A"}, {"text": "B"}], "quest.c": {"text": "D"}}
        scan = ScanResult(ftb_localizer=source, ftb_localizer_source="kubejs export")
        scan.community.append(LangFile("ftbquestlocalizer", "zh_cn", "human", {}, components={"quest.a": [{"text": "人工甲"}, {"text": "人工乙"}]}))
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "translation.zip"
            write_pack(pack, {}, 15, ftb_components={"quest.a": [{"text": "AI 甲"}, {"text": "AI 乙"}]})
            missing = missing_ftb_entries(scan, pack)
            self.assertEqual([entry.key for entry in missing], ["ftblocalizer:quest.c#/text"])
            merged = translated_ftb_components(scan, missing, {"ftbquestlocalizer": {"ftblocalizer:quest.c#/text": "丁"}}, pack)
            self.assertNotIn("quest.a", merged)
            self.assertEqual(merged["quest.c"]["text"], "丁")

    def test_source_text_change_resets_translation_and_retranslates(self):
        source = {"quest.x": ["New text"]}
        scan = ScanResult(ftb_localizer=source, ftb_localizer_source="kubejs export")
        prior = {"quest.x": ["旧译文"]}
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "translation.zip"
            write_pack(pack, {}, 15, ftb_components=prior, ftb_sources={"quest.x": {"/0": "Old text"}})
            missing = missing_ftb_entries(scan, pack)
            self.assertEqual([entry.key for entry in missing], ["ftblocalizer:quest.x#/0"])
            translated = translated_ftb_components(scan, missing, {}, pack)
            self.assertEqual(translated["quest.x"], ["New text"], "stale translation falls back to current English")
            retried = translated_ftb_components(scan, missing, {"ftbquestlocalizer": {"ftblocalizer:quest.x#/0": "新译文"}}, pack)
            self.assertEqual(retried["quest.x"], ["新译文"])

    def test_growing_export_keeps_new_nodes_and_carries_unchanged_translations(self):
        source = {"quest.g": ["A", "B", "C"]}
        scan = ScanResult(ftb_localizer=source, ftb_localizer_source="kubejs export")
        prior = {"quest.g": ["甲", "乙"]}
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "translation.zip"
            write_pack(pack, {}, 15, ftb_components=prior, ftb_sources={"quest.g": {"/0": "A", "/1": "B"}})
            missing = missing_ftb_entries(scan, pack)
            self.assertEqual([entry.key for entry in missing], ["ftblocalizer:quest.g#/2"])
            merged = translated_ftb_components(scan, missing, {"ftbquestlocalizer": {"ftblocalizer:quest.g#/2": "丙"}}, pack)
            self.assertEqual(merged["quest.g"], ["甲", "乙", "丙"], "new node kept, unchanged translations carried")

    def test_uninstalled_ftb_namespace_suppresses_missing_entries(self):
        scan = ScanResult(ftb_localizer={"quest.a": [{"text": "Text"}]}, ftb_localizer_source="kubejs")
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "translation.zip"
            with patch("ferrycore.packio.load_uninstalled_ai", return_value={"ftbquestlocalizer": {"quest.a": []}}):
                self.assertEqual(missing_ftb_entries(scan, pack), [])


if __name__ == "__main__":
    unittest.main()
