"""Regression tests for version detection and scanner extensions."""

import json
import io
import hashlib
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import urllib.error
from types import SimpleNamespace
from pathlib import Path

import mc_ai_translator as core


class VersionDetectionTests(unittest.TestCase):
    def test_early_resource_pack_versions_use_format_one(self):
        self.assertEqual(core.pack_format_for_version("1.6.1"), 1)
        self.assertEqual(core.pack_format_for_version("1.7.10"), 1)
        with self.assertRaisesRegex(ValueError, "手动指定"):
            core.pack_format_for_version("1.5.2")

    def test_year_based_versions_map_to_latest_resource_formats(self):
        self.assertEqual(core.pack_format_for_version("1.21.7"), 64)
        self.assertEqual(core.pack_format_for_version("1.21.8"), 64)
        self.assertEqual(core.pack_format_for_version("1.21.9"), 69.0)
        self.assertEqual(core.pack_format_for_version("1.21.11"), 75.0)
        self.assertEqual(core.pack_format_for_version("26.1"), 84.0)
        self.assertEqual(core.pack_format_for_version("26.1.2"), 84.0)
        self.assertEqual(core.pack_format_for_version("26.2"), 88.0)
        self.assertEqual(core.pack_format_for_version("26.3"), 97.1)
        self.assertEqual(core.pack_format_for_version("1.26.3"), 97.1)
        self.assertEqual(core.pack_format_for_version("26.4"), 98.0)
        self.assertEqual(core.pack_format_for_version("27.1"), 98.0)

    def test_new_versions_use_min_max_format_without_pack_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old.zip"
            core.write_pack(old, {}, 64)
            old_meta = json.loads(zipfile.ZipFile(old).read("pack.mcmeta"))["pack"]
            self.assertEqual(old_meta["pack_format"], 64)
            self.assertNotIn("min_format", old_meta)
            new = Path(tmp) / "new.zip"
            core.write_pack(new, {}, 97.1)
            new_meta = json.loads(zipfile.ZipFile(new).read("pack.mcmeta"))["pack"]
            self.assertEqual((new_meta["min_format"], new_meta["max_format"]), ([97, 1], [97, 1]))
            self.assertNotIn("pack_format", new_meta)
            self.assertEqual(core.read_pack_format(new), 97.1)
            whole = Path(tmp) / "69.zip"
            core.write_pack(whole, {}, 69)
            whole_meta = json.loads(zipfile.ZipFile(whole).read("pack.mcmeta"))["pack"]
            self.assertEqual((whole_meta["min_format"], whole_meta["max_format"]), (69, 69))
            self.assertEqual(core.read_pack_format(whole), 69)

    def test_client_version_json_gives_exact_format_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "26.3-Forge"
            (instance / "mods").mkdir(parents=True)
            version_json = {"id": "26.3", "pack_version": {"resource_major": 97, "resource_minor": 1, "data_major": 120, "data_minor": 0}}
            with zipfile.ZipFile(instance / "26.3-Forge.jar", "w") as archive:
                archive.writestr("version.json", json.dumps(version_json))
            self.assertEqual(core.detect_minecraft_version(instance), "26.3")
            self.assertEqual(core.detect_pack_format(instance), 97.1)
            # Table fallback must not be needed, but should agree.
            self.assertEqual(core.pack_format_for_version("26.3"), 97.1)

    def test_existing_pack_upgrades_to_newer_detected_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "26.3"
            (instance / "resourcepacks").mkdir(parents=True)
            pack = instance / "resourcepacks" / core.DEFAULT_CONFIG["pack_name"]
            core.write_pack(pack, {}, 63)
            self.assertEqual(core.resolve_pack_format({}, instance), 97.1)
            core.write_pack(pack, {}, 99)
            self.assertEqual(core.resolve_pack_format({}, instance), 99)

    def test_forward_detection_for_year_named_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "26.3-Forge"
            (instance / "mods").mkdir(parents=True)
            self.assertEqual(core.detect_minecraft_version(instance), "26.3")
            self.assertEqual(core.detect_pack_format(instance), 97.1)

    def test_versions_directory_before_instance_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / ".minecraft"
            version = instance / "versions" / "1.12.2"
            version.mkdir(parents=True)
            (version / "1.12.2.jar").touch()
            self.assertEqual(core.detect_pack_format(instance), 3)

    def test_mmc_before_versions_and_existing_pack_before_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            instance.mkdir()
            (instance / "mmc-pack.json").write_text(json.dumps({"components": [
                {"uid": "net.minecraft", "version": "1.7.10"},
            ]}), encoding="utf-8")
            self.assertEqual(core.detect_pack_format(instance), 1)
            pack = instance / "resourcepacks" / core.DEFAULT_CONFIG["pack_name"]
            core.write_pack(pack, {}, 42)
            self.assertEqual(core.resolve_pack_format({}, instance), 42)

    def test_unknown_version_requires_explicit_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "instance"
            instance.mkdir()
            with self.assertRaisesRegex(ValueError, "未检测到游戏版本"):
                core.detect_pack_format(instance)

    def test_cli_force_fallback_is_explicitly_marked_in_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp) / "unknown"
            (instance / "mods").mkdir(parents=True)
            instance.joinpath("mods", "demo.jar").write_bytes(ScannerTests._jar({"assets/demo/lang/en_us.json": '{"key":"Hello"}'}))
            args = SimpleNamespace(instance=str(instance), mods=None, resourcepacks=None, interactive=False,
                engine="mymemory", base_url=None, api_key=None, model=None, batch_size=None, timeout=None,
                delay=None, concurrency=None, pack_format=None, pack_name=None, output_dir=None,
                fill_community=None, refine_community=None, include_modid=None, only_modid=None, yes=True)
            config = {**core.DEFAULT_CONFIG, "community_enabled": False, "cache_dir": str(instance / "cache")}
            with patch.object(core, "load_config", return_value=config):
                args.yes = False
                with self.assertRaisesRegex(ValueError, "未检测到游戏版本"):
                    core.command_translate(args)
                self.assertFalse((instance / "resourcepacks" / core.DEFAULT_CONFIG["pack_name"]).exists())
                args.yes = True
            with patch.object(core, "load_config", return_value=config), patch.object(core, "translate_entries", return_value=({"demo": {"key": "你好"}}, [], {})), patch.object(core, "update_progress"):
                self.assertEqual(core.command_translate(args), 0)
            pack = instance / "resourcepacks" / core.DEFAULT_CONFIG["pack_name"]
            with zipfile.ZipFile(pack) as zf:
                self.assertIn("README_VERSION_UNCONFIRMED.txt", zf.namelist())
                self.assertEqual(json.loads(zf.read("pack.mcmeta"))["pack"]["pack_format"], 15)

    def test_cloud_engines_require_https_but_local_is_allowed(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            core.validate_engine_network("openai", "http://127.0.0.1:11434/v1")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            core.validate_engine_network("anthropic", "localhost:1234")
        core.validate_engine_network("local", "http://127.0.0.1:11434/v1")
        core.validate_engine_network("openai", "https://api.example.com/v1")


class ScannerTests(unittest.TestCase):
    @staticmethod
    def _jar(files):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        return buffer.getvalue()

    def test_nested_mod_language_and_human_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            mods = instance / "mods"
            mods.mkdir()
            nested = self._jar({
                "assets/depmod/lang/en_us.json": '{"example": "Hello"}',
                "assets/depmod/lang/zh_cn.json": '{"example": "你好"}',
            })
            (mods / "outer.jar").write_bytes(self._jar({
                "META-INF/jars/dep.jar": nested,
                "META-INF/jars/broken.jar": b"not a zip",
                "assets/outer/lang/en_us.json": '{"outer": "Hello"}',
            }))
            with self.assertLogs(core.LOG, level="WARNING") as logs:
                scan = core.scan_inputs(mods, None)
            self.assertIn("broken.jar", " ".join(logs.output))
            self.assertIn("depmod", {item.modid for item in scan.english})
            self.assertIn("depmod", {item.modid for item in scan.community})
            self.assertIn("outer.jar!META-INF/jars/dep.jar!assets/depmod/lang/en_us.json", scan.english[0].path)
            self.assertFalse(any(item.modid == "depmod" for item in core.missing_entries(scan.english, scan.community, [])))

    def test_kubejs_files_are_read_only_and_count_as_human(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "mods").mkdir()
            lang = instance / "kubejs" / "assets" / "kubejs" / "lang"
            lang.mkdir(parents=True)
            english = lang / "en_us.json"
            human = lang / "zh_cn.json"
            english.write_text('{"a": "Apple", "b": "Pear"}', encoding="utf-8")
            human.write_text('{"a": "苹果"}', encoding="utf-8")
            before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (english, human)}
            scan = core.scan_inputs(instance / "mods", None)
            self.assertEqual(core.missing_entries(scan.english, scan.community, [], fill_community=True)[0].key, "b")
            self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (english, human)})

    def test_hardcoded_jar_findings_map_to_its_language_namespaces(self):
        scan = core.ScanResult(english=[
            core.LangFile("legends", "en_us", "D:/mods/legends.jar!assets/legends/lang/en_US.lang", {"a": "Hello"}),
            core.LangFile("horror", "en_us", "D:/mods/legends.jar!assets/horror/lang/en_US.lang", {"a": "Hello"}),
            core.LangFile("other", "en_us", "D:/mods/other.jar!assets/other/lang/en_US.lang", {"a": "Hello"}),
        ])
        self.assertEqual(core.hardcoded_modids(scan, [("legends.jar", 8, ["Hardcoded line"])]), {"legends", "horror"})

    def test_legacy_pack_writes_zh_cn_lang_and_remains_scannable(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "mods").mkdir()
            pack = instance / "resourcepacks" / "ai.zip"
            data = {"item.example.name": "你好", "note": "第一行\n第二行"}
            core.write_pack(pack, {"demo": data}, 1)
            with zipfile.ZipFile(pack) as zf:
                self.assertEqual(json.loads(zf.read("pack.mcmeta"))["pack"]["pack_format"], 1)
                self.assertIn("assets/demo/lang/zh_CN.lang", zf.namelist())
                self.assertNotIn("assets/demo/lang/zh_cn.json", zf.namelist())
                self.assertIn("item.example.name=你好", zf.read("assets/demo/lang/zh_CN.lang").decode("utf-8"))
            self.assertEqual(core.load_pack_translations(pack)["demo"]["item.example.name"], "你好")
            self.assertEqual(core.scan_inputs(instance / "mods", pack.parent).ai[0].modid, "demo")

    def test_migrate_old_json_pack_once_with_backup_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "mods").mkdir()
            pack = instance / "resourcepacks" / "ai.zip"
            data = {"item.example.name": "你好"}
            core.write_pack(pack, {"demo": data}, 15, sources={"demo": {"item.example.name": "community"}})
            before = hashlib.sha256(pack.read_bytes()).hexdigest()
            self.assertTrue(core.migrate_legacy_pack(pack, 1))
            backup = pack.with_name(pack.name + ".pre-lang-backup")
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), before)
            with zipfile.ZipFile(pack) as zf:
                self.assertIn("assets/demo/lang/zh_CN.lang", zf.namelist())
                self.assertNotIn("assets/demo/lang/zh_cn.json", zf.namelist())
            self.assertEqual(core.load_pack_translations(pack)["demo"], data)
            self.assertEqual(core.load_pack_sources(pack)["demo"]["item.example.name"], "community")
            self.assertFalse(core.migrate_legacy_pack(pack, 1))
            self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), before)

    def test_patchouli_only_text_fields_and_macro_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            mods = instance / "mods"
            mods.mkdir()
            original = {
                "name": "Guide", "icon": "demo:book", "category": "demo:entry", "pages": [
                    {"type": "patchouli:text", "text": "Hello $(br) world $(l:next)link$(/l)", "title": "Welcome", "recipe": "demo:recipe"},
            ],
            }
            source = self._jar({"assets/demo/patchouli_books/guide/en_us/entries/start.json": json.dumps(original)})
            (mods / "demo.jar").write_bytes(source)
            scan = core.scan_inputs(mods, None)
            entries = core.missing_patchouli_entries(scan)
            self.assertEqual(len(entries), 3)
            self.assertTrue(all(entry.kind == "patchouli" for entry in entries))
            self.assertIn("$(br)", core.placeholder_tokens(entries[1].english))
            self.assertIsNone(core.restore_and_validate(entries[1].english, "丢了标记", {}, set()))
            masked, _ = core.mask_all(entries[1].english, {}, set())
            self.assertIn("$(br)", core.restore_and_validate(entries[1].english, masked, {}, set()))
            translations = {"demo": {entry.key: entry.english for entry in entries}}
            translations["demo"][entries[1].key] = "你好 $(br) 世界 $(l:next)链接$(/l)"
            pages = core.translated_patchouli_files(scan, entries, translations)
            path = "assets/demo/patchouli_books/guide/zh_cn/entries/start.json"
            self.assertEqual(pages[path]["icon"], original["icon"])
            self.assertEqual(pages[path]["category"], original["category"])
            self.assertEqual(pages[path]["pages"][0]["recipe"], original["pages"][0]["recipe"])
            self.assertEqual(pages[path]["pages"][0]["text"], translations["demo"][entries[1].key])
            pack = instance / "resourcepacks" / "ai.zip"
            merged = core.merge_pack_translations(pack, {"demo": {}}, [])
            self.assertEqual(merged, {})
            core.write_pack(pack, merged, 15, patchouli=pages)
            with zipfile.ZipFile(pack) as zf:
                self.assertNotIn("assets/demo/lang/zh_cn.json", zf.namelist())
            scan = core.scan_inputs(mods, pack.parent)
            self.assertFalse(core.missing_patchouli_entries(scan))

            human = instance / "resourcepacks" / "human.zip"
            core.write_pack(human, {}, 15, patchouli={path: original})
            # A human pack has no AI marker. Check the scanner using a marker-free ZIP.
            with zipfile.ZipFile(human, "w") as zf:
                zf.writestr("pack.mcmeta", json.dumps({"pack": {"pack_format": 15}}))
                zf.writestr(path, json.dumps(original))
            scan = core.scan_inputs(mods, pack.parent)
            self.assertTrue(core.yield_to_community(scan, pack))
            self.assertFalse(pack.exists())


class CommunityBaselineTests(unittest.TestCase):
    def test_community_keys_skip_ai_and_sources_survive_yield(self):
        class Response(io.BytesIO):
            pass

        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            scan = core.ScanResult(
                english=[core.LangFile("demo", "en_us", "jar", {"a": "Apple", "b": "Pear", "c": "Carrot"})],
                community=[core.LangFile("demo", "zh_cn", "jar", {"a": "苹果"})],
            )
            data = json.dumps({"a": "不同苹果", "b": "梨"}, ensure_ascii=False).encode("utf-8")
            with patch.object(core.urllib.request, "urlopen", return_value=Response(data)) as fetch:
                baseline = core.fetch_community_baseline(scan, core.DEFAULT_CONFIG, instance)
            self.assertEqual(baseline, {"demo": {"b": "梨"}})
            self.assertEqual(len(core.missing_entries(scan.english, scan.community + [core.LangFile("demo", "zh_cn", "community", baseline["demo"])], [], fill_community=True)), 1)
            self.assertEqual([entry.key for entry in core.entries_with_baseline(scan, baseline)], ["c"])
            with patch.object(core.urllib.request, "urlopen", side_effect=AssertionError("cached")):
                self.assertEqual(core.fetch_community_baseline(scan, core.DEFAULT_CONFIG, instance), baseline)
            pack = instance / "ai.zip"
            core.write_pack(pack, {"demo": {"b": "梨", "c": "AI 胡萝卜"}}, 15, sources={"demo": {"b": "community", "c": "ai"}})
            self.assertEqual(core.load_pack_sources(pack)["demo"], {"b": "community", "c": "ai"})
            status = core.build_mod_status(scan.english, scan.community, core.language_files_in_zip(pack), community_sources={"demo": {"b"}})[0]
            self.assertEqual((status.community_keys, status.ai_keys), (2, 1))
            scan.ai = core.language_files_in_zip(pack)
            scan.community.append(core.LangFile("demo", "zh_cn", "human", {"b": "新人工梨"}))
            self.assertTrue(core.yield_to_community(scan, pack))
            self.assertEqual(core.load_pack_translations(pack), {"demo": {"c": "AI 胡萝卜"}})
            self.assertEqual(core.load_pack_sources(pack)["demo"], {"c": "ai"})
            core.remove_ai_translations(pack, {"demo"})
            self.assertFalse(pack.exists())

    def test_network_failure_does_not_interrupt_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = core.ScanResult(english=[core.LangFile("demo", "en_us", "jar", {"a": "Apple"})])
            with patch.object(core.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
                with self.assertLogs(core.LOG, level="WARNING"):
                    self.assertEqual(core.fetch_community_baseline(scan, core.DEFAULT_CONFIG, Path(tmp)), {})
            self.assertEqual(len(core.missing_entries(scan.english, scan.community, scan.ai)), 1)


class QualityTests(unittest.TestCase):
    def test_qc_multiset_and_locked_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "ai.zip"
            locks = Path(tmp) / "locks.json"
            core.write_pack(pack, {"demo": {
                "same": "Hello", "blank": " ", "token": "%s", "stale": "旧项",
            }}, 15)
            scan = core.ScanResult(english=[core.LangFile("demo", "en_us", "jar", {
                "same": "Hello", "blank": "Empty", "token": "%s %s",
            })])
            issues = core.quality_issues(scan, pack, locks)
            names = {key: issue for _, key, _, _, issue, _ in issues}
            self.assertEqual(names["token"], "占位符丢失或错乱")
            self.assertEqual(names["same"], "译文等于原文")
            self.assertEqual(names["blank"], "空译文")
            self.assertEqual(names["stale"], "陈旧 key")
            core.lock_translation("demo", "same", "Hello", locks)
            self.assertNotIn("same", {issue[1] for issue in core.quality_issues(scan, pack, locks)})
            self.assertEqual(core.remove_pack_keys(pack, {"demo": {"same", "blank"}}, locks), 1)
            self.assertIn("same", core.load_pack_translations(pack)["demo"])

    def test_community_source_is_not_removed_by_ai_controls(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "ai.zip"
            locks = Path(tmp) / "locks.json"
            core.write_pack(pack, {"demo": {"human": "社区译文", "ai": "AI译文"}}, 15, sources={"demo": {"human": "community", "ai": "ai"}})
            scan = core.ScanResult(english=[core.LangFile("demo", "en_us", "jar", {"human": "Human", "ai": "AI"})])
            self.assertNotIn("human", {issue[1] for issue in core.quality_issues(scan, pack, locks)})
            self.assertEqual(core.remove_pack_keys(pack, {"demo": {"human"}}, locks), 0)
            self.assertEqual(core.remove_ai_translations(pack, {"demo"}), ["demo"])
            self.assertEqual(core.load_pack_translations(pack), {"demo": {"human": "社区译文"}})
            self.assertEqual(core.load_pack_sources(pack)["demo"]["human"], "community")
            core.lock_translation("demo", "human", "锁定译文", locks)
            with patch.object(core, "load_locks", return_value=core.load_locks(locks)):
                merged = core.merge_pack_translations(pack, {"demo": {"human": "新 AI"}}, [core.LangFile("demo", "zh_cn", "external", {"human": "外部人工"})])
            self.assertEqual(merged["demo"]["human"], "锁定译文")

    def test_retranslate_bypasses_cache_with_model_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "ai.zip"
            core.write_pack(pack, {"demo": {"key": "旧译文"}}, 15)
            scan = core.ScanResult(english=[core.LangFile("demo", "en_us", "jar", {"key": "Hello"})])
            settings = {
                "cache_dir": str(Path(tmp) / "cache"), "engine": "openai", "model": "single", "batch_size": 1,
                "concurrency": 2, "glossary": {}, "keep": set(), "pack_format": 15,
                "engines": [{"engine": "openai", "model": "pooled", "weight": 1}],
            }
            with patch.object(core, "translate_entries", return_value=({"demo": {"key": "新译文"}}, [], {})) as translate:
                count, errors = core.retranslate_pack_keys(scan, pack, {"demo": {"key"}}, settings)
            self.assertEqual((count, errors), (1, []))
            self.assertTrue(translate.call_args.kwargs["bypass_cache"])
            self.assertEqual(core.load_pack_translations(pack)["demo"]["key"], "新译文")

    def test_uninstall_last_lang_keeps_patchouli_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "ai.zip"
            page = "assets/demo/patchouli_books/guide/zh_cn/entries/start.json"
            core.write_pack(pack, {"demo": {"key": "AI"}}, 15, patchouli={page: {"name": "向导"}})
            self.assertEqual(core.remove_ai_translations(pack, {"demo"}), ["demo"])
            self.assertTrue(pack.is_file())
            self.assertEqual(core.load_pack_patchouli(pack)[page]["name"], "向导")
            self.assertEqual(core.load_pack_translations(pack), {})

    def test_config_secret_risk_only_warns_when_committable(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            config = project / "ferry_config.json"
            config.write_text(json.dumps({"openai_api_key": "sk-test-placeholder"}), encoding="utf-8")
            self.assertIsNone(core.config_secret_risk(config))
            (project / ".git").mkdir()
            with patch.object(core, "is_git_ignored", return_value=False):
                warning = core.config_secret_risk(config)
            self.assertIn("泄漏", warning or "")
            with patch.object(core, "is_git_ignored", return_value=True):
                self.assertIsNone(core.config_secret_risk(config))
            config.write_text(json.dumps({"openai_api_key": ""}), encoding="utf-8")
            with patch.object(core, "is_git_ignored", return_value=False):
                self.assertIsNone(core.config_secret_risk(config))

    def test_public_config_redacts_nested_api_keys(self):
        secret = "sk-test-placeholder-not-a-real-key"
        shown = core.public_config({"openai_api_key": secret, "model_pool": [{"api_key": secret}]})
        self.assertEqual(shown["openai_api_key"], "sk-***")
        self.assertEqual(shown["model_pool"][0]["api_key"], "sk-***")


if __name__ == "__main__":
    unittest.main()
