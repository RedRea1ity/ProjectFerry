"""Regression tests for version detection and scanner extensions."""

import json
import io
import hashlib
import tempfile
import unittest
import zipfile
from unittest.mock import patch
import urllib.request
import time
import urllib.error
from types import SimpleNamespace
from pathlib import Path

import mc_ai_translator as core
from ferrycore import cli as ferrycli
from ferrycore import config as ferryconfig
from ferrycore import diagnose as ferrydiagnose
from ferrycore import literalbridge as ferryliteralbridge
from ferrycore import online as ferryonline
from ferrycore import packio as ferrypackio
from ferrycore import secrets as secretbox
from ferrycore import update as ferryupdate
from ferrycore import versions as ferryversions


class VersionDetectionTests(unittest.TestCase):
    def test_early_resource_pack_versions_use_format_one(self):
        self.assertEqual(core.pack_format_for_version("1.6.1"), 1)
        self.assertEqual(core.pack_format_for_version("1.7.10"), 1)
        with self.assertRaisesRegex(ValueError, "没有资源包机制"):
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
        self.assertEqual(core.pack_format_for_version("26.4"), 99.0)
        self.assertEqual(core.pack_format_for_version("27.1"), 99.0)

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

    def test_remote_pack_format_table(self):
        # 缓存命中（TTL 内）不联网；过期后联网取新值并落缓存；网络失败回退过期缓存。
        import time as time_module
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "cache"
            cache.mkdir()
            (cache / "pack_formats.json").write_text(json.dumps({
                "fetched_at": time_module.time(),
                "formats": {"26.5": 100.0, "1.20.1": 15, "junk": "x"},
            }), encoding="utf-8")
            with mock.patch.object(ferryversions, "_fetch_json", side_effect=AssertionError("缓存命中不该联网")):
                self.assertEqual(core.pack_format_for_version_remote("26.5", cache_dir=cache, online=True), 100.0)
                self.assertEqual(core.pack_format_for_version_remote("1.20.1", cache_dir=cache, online=True), 15)

            stale = Path(tmp) / "stale"
            stale.mkdir()
            (stale / "pack_formats.json").write_text(json.dumps({"fetched_at": 0, "formats": {"26.4": 99.0}}), encoding="utf-8")
            with mock.patch.object(ferryversions, "_fetch_json", side_effect=OSError("offline")):
                self.assertEqual(core.pack_format_for_version_remote("26.4", cache_dir=stale), 99.0)
            with mock.patch.object(ferryversions, "_fetch_json", return_value={"formats": {"26.4": 100.5, "bad": "oops"}}):
                self.assertEqual(core.pack_format_for_version_remote("26.4", cache_dir=stale), 100.5)
                saved = json.loads((stale / "pack_formats.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["formats"], {"26.4": 100.5})

    def test_detect_pack_format_prefers_online_for_table_tail(self):
        # 表尾（26.4+）在正式版定版前随时可能变：联网优先，断网回退本地表；老版本从不联网。
        import unittest.mock as mock
        with tempfile.TemporaryDirectory() as tmp:
            def make_instance(name, version):
                instance = Path(tmp) / name
                vdir = instance / "versions" / version
                vdir.mkdir(parents=True)
                with zipfile.ZipFile(vdir / f"{version}.jar", "w") as archive:
                    archive.writestr("empty.txt", "")
                return instance

            old_instance = make_instance("old", "1.20.1")
            with mock.patch.object(ferryversions, "_fetch_json", side_effect=AssertionError("表内版本不该联网")):
                self.assertEqual(core.detect_pack_format(old_instance, cache_dir=Path(tmp), online=True), 15)

            new_instance = make_instance("new", "26.4")
            with mock.patch.object(ferryversions, "_fetch_json", return_value={"formats": {"26.4": 100.5}}):
                self.assertEqual(core.detect_pack_format(new_instance, cache_dir=Path(tmp) / "c1", online=True), 100.5)
            self.assertEqual(core.detect_pack_format(new_instance, cache_dir=Path(tmp) / "c2", online=False), 99.0)

    def test_build_bridge_mapping_prefers_human_over_ai(self):
        english = [core.LangFile("demo", "en_us", "jar", {
            "item.a": "Apple", "item.b": "Pear", "item.same": "Same", "item.blank": "Blank",
        })]
        community = [core.LangFile("demo", "zh_cn", "human", {"item.a": "人工苹果", "item.blank": "  "})]
        ai = {"demo": {"item.a": "AI 苹果", "item.b": "AI 梨", "item.same": "Same"}}
        mapping = core.build_bridge_mapping(english, community, ai)
        self.assertEqual(mapping["Apple"], "人工苹果", "人工译文优先")
        self.assertEqual(mapping["Pear"], "AI 梨")
        self.assertNotIn("Same", mapping, "译文等于原文的键没有价值")
        self.assertNotIn("Blank", mapping, "空译文不导出")

    def test_literalbridge_export_and_lang_merge(self):
        groups = {
            "jei": {"Hello world": "你好世界", "Exit": "退出"},
            "hard_only": {"Only hardcoded text here": "只有硬编码"},
        }
        groups["jei"]["N"] = "北"
        groups["jei"]["Run!"] = "快跑"
        rules_doc = core.build_literalbridge_export(groups)
        self.assertEqual(set(rules_doc), {"jei", "hard_only"})
        jei_rules = rules_doc["jei"]["rules"]
        # 短文本 "N" 被长度过滤（子串匹配下会啃玩家名）
        self.assertNotIn("N", [rule["original"] for rule in jei_rules])
        by_original = {rule["original"]: rule["translation"] for rule in jei_rules}
        self.assertEqual(by_original.get("Hello world"), "你好世界")
        self.assertEqual(by_original.get("Run!"), "快跑")
        self.assertEqual([rule["original"] for rule in jei_rules], ["Hello world", "Exit", "Run!"])

        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "AI_Translation_LowPriority.zip"
            core.write_pack(pack, {"demo": {"item.x": "你好"}}, 15)
            before = set(zipfile.ZipFile(pack).namelist())
            core.merge_bridge_lang(pack, {"text.ferrybridge.demo.r_x": "中文"}, {"text.ferrybridge.demo.r_x": "Hello world"})
            with zipfile.ZipFile(pack) as zf:
                after = set(zipfile.ZipFile(pack).namelist())
                merged = json.loads(zf.read("assets/literalbridge/lang/zh_cn.json"))
            self.assertIn("assets/literalbridge/lang/zh_cn.json", after - before)
            self.assertIn("assets/demo/lang/zh_cn.json", after, "原有内容不丢")
            self.assertEqual(merged["text.ferrybridge.demo.r_x"], "中文")

    def test_hardcoded_nbt_pipeline(self):
        import json as json_module

        def class_with(text: str) -> bytes:
            raw = text.encode("utf-8")
            magic = bytes((0xCA, 0xFE, 0xBA, 0xBE))
            return (magic + (0).to_bytes(2, "big") + (61).to_bytes(2, "big")
                    + (2).to_bytes(2, "big") + bytes((1,)) + len(raw).to_bytes(2, "big") + raw)
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            mods.mkdir()
            strings = [
                "You hear a distant whisper from the ancient ruins at midnight",
                "The forgotten king guards the silent treasury of dust and sorrow",
                "A letter from the front line describes the winter campaign",
                "The gunsmith explains how to maintain the bolt action rifle",
                "Field manual: always check the chamber before cleaning",
                "Radio crackles with coordinates for the artillery strike",
            ]
            with zipfile.ZipFile(mods / "tacz-1.20.1.jar", "w") as zf:
                zf.writestr("assets/tacz/lang/en_us.json", json_module.dumps({"item.x": "Hello world"}))
                for index, text in enumerate(strings):
                    zf.writestr(f"com/example/C{index}.class", class_with(text))

            findings = core.detect_hardcoded_texts(mods, threshold=2)
            self.assertEqual([(name, count) for name, count, _ in findings], [("tacz-1.20.1.jar", 6)])
            scan = core.scan_inputs(mods, None)
            by_modid = core.hardcoded_strings_by_modid(scan, findings)
            self.assertEqual(set(by_modid), {"tacz"})
            self.assertEqual(sorted(by_modid["tacz"]), sorted(strings))

            entries = core.missing_hardcoded_entries(by_modid, {}, {"tacz"})
            self.assertEqual(len(entries), 6)
            self.assertTrue(all(entry.kind == "nbt" and entry.key.startswith("nbt:") for entry in entries))
            # 已有译文的条目不再生成
            existing = {"tacz": {entries[0].english: "已翻"}}
            again = core.missing_hardcoded_entries(by_modid, existing, {"tacz"})
            self.assertEqual(len(again), 5)
            self.assertNotIn(entries[0].english, {entry.english for entry in again})

            translated = {"tacz": {entry.key: "译" for entry in entries}}
            pairs = core.hardcoded_pairs_from_entries(entries, translated)
            self.assertEqual(pairs, {"tacz": {text: "译" for text in strings}})

            pack = Path(tmp) / "AI_Translation_LowPriority.zip"
            pack.write_bytes(b"placeholder")
            core.save_hardcoded_translations(pack, pairs)
            self.assertEqual(core.load_hardcoded_translations(pack), pairs)

            mapping = core.build_bridge_mapping(scan.english, [], {}, hardcoded=pairs)
            self.assertEqual(mapping, {text: "译" for text in strings})

    def test_pending_hardcoded_modids_counts_only_untranslated(self):
        """状态列的「已完成 vs ⚠ 待翻N」靠它：全翻完的 modid 不该再出现。"""
        def class_with(text: str) -> bytes:
            raw = text.encode("utf-8")
            magic = bytes((0xCA, 0xFE, 0xBA, 0xBE))
            return (magic + (0).to_bytes(2, "big") + (61).to_bytes(2, "big")
                    + (2).to_bytes(2, "big") + bytes((1,)) + len(raw).to_bytes(2, "big") + raw)
        strings = [
            "You hear a distant whisper from the ancient ruins at midnight",
            "The forgotten king guards the silent treasury of dust and sorrow",
            "A letter from the front line describes the winter campaign",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            mods.mkdir()
            with zipfile.ZipFile(mods / "tacz-1.20.1.jar", "w") as zf:
                zf.writestr("assets/tacz/lang/en_us.json", json.dumps({"item.x": "Hello world"}))
                for index, text in enumerate(strings):
                    zf.writestr(f"com/example/C{index}.class", class_with(text))
            findings = core.detect_hardcoded_texts(mods, threshold=2)
            scan = core.scan_inputs(mods, None)
            self.assertEqual([(name, count) for name, count, _ in findings], [("tacz-1.20.1.jar", 3)])

            self.assertEqual(core.pending_hardcoded_modids(findings, scan), {"tacz": 3}, "一条没翻 = 全待翻")
            self.assertEqual(core.pending_hardcoded_modids(findings, scan, {"tacz": {text: "译" for text in strings}}), {}, "全翻完 = 不再挂 ⚠")
            self.assertEqual(core.pending_hardcoded_modids(findings, scan, {"tacz": {strings[0]: "译"}}), {"tacz": 2}, "报的是剩余条数")
            self.assertEqual(core.pending_hardcoded_modids([], scan), {}, "没检出硬编码 = 没有待翻")

    def test_collected_file_to_ferrybridge_external(self):
        """游戏采集清单 → Entry → 摆渡桥外部映射表（这是「大批量翻译」的接线）。"""
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            collected = instance / core.COLLECTED_NAME
            collected.write_text(
                "摆渡桥 FerryBridge · 未命中文本采集\n"
                "更新时间: 2026-10-12T09:00:00\n"
                "\n"
                "【这是什么】\n"
                "  游戏里出现过、但当前映射表没有对应译文的英文。\n"
                "\n"
                "===== 聊天栏（2 条）=====\n"
                "A favorite of killers\n"
                "watching you\n"
                "\n"
                "===== 物品 tooltip（2 条）=====\n"
                "Cooldown:\n"
                "A favorite of killers\n",
                encoding="utf-8",
            )
            buckets = core.parse_collected_file(collected)
            self.assertEqual(sorted(buckets), ["物品 tooltip", "聊天栏"])
            self.assertEqual(buckets["聊天栏"], ["A favorite of killers", "watching you"])

            entries = core.collected_entries(buckets)
            # 「A favorite of killers」两个桶都有 → 去重后只应产生一条 Entry
            self.assertEqual(len(entries), 3)
            self.assertTrue(all(entry.modid == core.COLLECTED_MODID for entry in entries))
            self.assertEqual(
                {entry.english for entry in entries},
                {"A favorite of killers", "watching you", "Cooldown:"},
            )

            target = core.write_ferrybridge_external(instance, {"Cooldown:": "冷却时间："})
            self.assertEqual(target, instance / core.FERRYBRIDGE_EXTERNAL_NAME)
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["map"], {"Cooldown:": "冷却时间："})
            self.assertEqual(payload["count"], 1)

    def test_parse_collected_file_missing_or_empty(self):
        """清单不存在 / 没有分组时返回空 dict，绝不抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(core.parse_collected_file(Path(tmp) / "nope.txt"), {})
            empty = Path(tmp) / core.COLLECTED_NAME
            empty.write_text("摆渡桥 FerryBridge · 未命中文本采集\n更新时间: now\n", encoding="utf-8")
            self.assertEqual(core.parse_collected_file(empty), {})

    def test_parse_collected_file_bom_and_crlf(self):
        """Windows 记事本会加 BOM、写 CRLF：两种都要能吃下。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / core.COLLECTED_NAME
            path.write_bytes("===== 聊天栏（1 条）=====\r\nHello there\r\n".encode("utf-8-sig"))
            self.assertEqual(core.parse_collected_file(path), {"聊天栏": ["Hello there"]})

    def test_hardcoded_text_keeps_long_sentence_with_log_keyword(self):
        """长句即便含日志关键词也必须保留。

        实测：The God 右键生命卷轴的那句长聊天文本含 "already"，被日志关键词整句误杀，
        导致纯桌面静态扫描漏掉它，只能靠游戏内采集补回来。
        """
        sentence = (
            "He's watching you, he knows everything you do, behave yourself and he'll be nice to you. "
            "But maybe it's just a mistake? Maybe it's not God at all, no one knows. "
            "You came into this world to stop God, but will you? He's already angry."
        )
        self.assertTrue(core.looks_like_hardcoded_text(sentence))
        # 短串仍受日志关键词过滤保护
        self.assertFalse(core.looks_like_hardcoded_text("config already loaded"))

    def test_default_config_auto_export_bridge_on(self):
        """「译完自动导出桥映射」默认开启 —— 这是「扫描→翻译→一步到位」的最后一环。

        GUI 勾选框读的就是这个默认值，删掉 key 会 KeyError。
        """
        self.assertIs(core.DEFAULT_CONFIG["auto_export_bridge"], True)

    def test_default_config_auto_update_literalbridge_on(self):
        """「自动升级 LiteralBridge」默认开启 —— 已装 LiteralBridge 的实例才会走到这条线。"""
        self.assertIs(core.DEFAULT_CONFIG["auto_update_literalbridge"], True)

    def test_hardcoded_text_rejects_commands_regardless_of_length(self):
        """命令串不是显示文本，无论多长都要剔除；说明文字里的 summon 不能误伤。"""
        for command in (
            "/execute as @e[type=boh:rat,limit=1,distance=0..2] run spreadplayers ~ ~ 3 3 false @e[type=boh:ratazana]",
            "spreadplayers ~ ~ 1 4 under 4 true @e[type=boh:Taperecorderbaldi,limit=1,distance=0..3]",
            "effect give @s kurolib:paranoia 3 0 false",
            "kick @a You don't deserve to live in this world",
            "weather rain",
        ):
            self.assertFalse(core.looks_like_hardcoded_text(command), command)
        # 含命令动词、但其实是给人看的说明文本 → 必须保留
        self.assertTrue(core.looks_like_hardcoded_text("- Used to summon the Specimen 9 bossfight."))

    def test_ferrybridge_external_write_merges_existing(self):
        """「导出桥接映射」与「导入游戏采集」写的是同一个文件：必须合并，不能互相覆盖。

        历史 bug：两者都是整表覆盖，后点的会把先点的那批全部抹掉。
        """
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            core.write_ferrybridge_external(instance, {"A": "甲", "B": "乙"})
            core.write_ferrybridge_external(instance, {"B": "乙改", "C": "丙"})
            self.assertEqual(
                core.load_ferrybridge_external(instance),
                {"A": "甲", "B": "乙改", "C": "丙"},
                "旧条目应保留、同 key 应被新译覆盖、新条目应追加",
            )
            core.write_ferrybridge_external(instance, {"Z": "贼"}, merge=False)
            self.assertEqual(core.load_ferrybridge_external(instance), {"Z": "贼"})

    def test_latest_github_release_compares_versions(self):
        import unittest.mock as mock
        release = {"tag_name": "v1.6.0", "body": "更新说明", "html_url": "https://example.com/r",
                   "assets": [{"name": "ProjectFerry.exe", "browser_download_url": "https://example.com/exe", "size": 1024}]}
        with mock.patch.object(ferryupdate, "_fetch_json", return_value=release):
            info = core.latest_github_release("1.5.0")
            self.assertEqual(info["version"], "1.6.0")
            self.assertEqual(info["asset_url"], "https://example.com/exe")
            self.assertIsNone(core.latest_github_release("1.6.0"), "相同版本不提示")
            self.assertIsNone(core.latest_github_release("1.7.0"), "本地更新不提示")
        with mock.patch.object(ferryupdate, "_fetch_json", side_effect=OSError("offline")):
            self.assertIsNone(core.latest_github_release("1.5.0"))

    def test_check_online_translation_statuses(self):
        import io
        import unittest.mock as mock

        class FakeResponse:
            def __init__(self, body):
                self._body = body.encode("utf-8")
            def read(self, limit=-1):
                return self._body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def fake_urlopen_error(code):
            raise urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(b""))

        site = "https://example.com/{modid}"
        cases = [
            ('{"a": 1}', 200, "found"),
            ("", 200, "miss"),
            ("Couldn't find the requested file", 200, "miss"),
        ]
        for body, code, expected in cases:
            if code == 200:
                with mock.patch.object(urllib.request, "urlopen", return_value=FakeResponse(body)):
                    self.assertEqual(core.check_online_translation(site, "demo")[0], expected)
            else:
                with mock.patch.object(urllib.request, "urlopen", side_effect=lambda *a, **k: fake_urlopen_error(code)):
                    self.assertEqual(core.check_online_translation(site, "demo")[0], expected)
        with mock.patch.object(urllib.request, "urlopen", side_effect=lambda *a, **k: fake_urlopen_error(500)):
            self.assertEqual(core.check_online_translation(site, "demo")[0], "error")

    def test_query_online_translations_fans_out(self):
        import unittest.mock as mock
        def fake_check(site, modid, timeout=6.0):
            return ("found", site.replace("{modid}", modid)) if modid == "jei" else ("miss", site)
        with mock.patch.object(ferryonline, "check_online_translation", side_effect=fake_check):
            results = core.query_online_translations(["a/{modid}", "b/{modid}"], ["jei", "nope"])
        # 无整合包时也包含内置 CFPA 源（2 条 miss）+ 2 源 x 2 模组模板
        self.assertEqual(len(results), 6)
        found = sorted(r["url"] for r in results if r["status"] == "found")
        self.assertEqual(found, ["a/jei", "b/jei"])

    def test_download_release_asset_streams_and_reports_progress(self):
        import unittest.mock as mock
        class FakeResponse:
            headers = {"Content-Length": "10"}
            def __init__(self):
                self._data = [b"12345", b"67890"]
            def read(self, limit=-1):
                return self._data.pop(0) if self._data else b""
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "new.exe"
            marks = []
            with mock.patch.object(urllib.request, "urlopen", return_value=FakeResponse()):
                core.download_release_asset("https://example.com/exe", dest, progress=lambda d, t: marks.append((d, t)))
            self.assertEqual(dest.read_bytes(), b"1234567890")
            self.assertEqual(marks[-1], (10, 10))

    def test_legacy_lang_file_casing_follows_pack_format(self):
        # 1.6.1–1.10.2（format 1–2）用区域代码 zh_CN；1.11（format 3）起资源包内文件名强制全小写。
        with tempfile.TemporaryDirectory() as tmp:
            for fmt, expected in ((1, "assets/demo/lang/zh_CN.lang"), (2, "assets/demo/lang/zh_CN.lang"), (3, "assets/demo/lang/zh_cn.lang")):
                pack = Path(tmp) / f"pack{fmt}.zip"
                core.write_pack(pack, {"demo": {"key": "值"}}, fmt)
                names = [name for name in zipfile.ZipFile(pack).namelist() if name.endswith(".lang")]
                self.assertEqual(names, [expected], f"pack_format {fmt}")

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
            # 注意 patch 目标是 ferrycli（调用者所在的模块），不是薄壳 core：
            # core.command_translate 内部用的是 ferrycore.cli 里 import 进来的名字，
            # patch 薄壳不会影响它 —— 那样会真的去读本机 ferry_config.json、真的打网络。
            with patch.object(ferrycli, "load_config", return_value=config):
                args.yes = False
                with self.assertRaisesRegex(ValueError, "未检测到游戏版本"):
                    core.command_translate(args)
                self.assertFalse((instance / "resourcepacks" / core.DEFAULT_CONFIG["pack_name"]).exists())
                args.yes = True
            with patch.object(ferrycli, "load_config", return_value=config), patch.object(ferrycli, "translate_entries", return_value=({"demo": {"key": "你好"}}, [], {})), patch.object(ferrycli, "update_progress"):
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

    def test_placeholder_masks_percent_and_named_formats_without_swallowing_text(self):
        for source in ("50%% off today", "Deal 100%% sure", "%-10s", "%+d", "%08.2f", "%(name)s grows", "§x§f§f§0§0§0§0Hello"):
            masked, replacements = core.mask_placeholders(source)
            restored = masked
            for token, original in replacements.items():
                restored = restored.replace(token, original)
            self.assertEqual(restored, source)
        masked, replacements = core.mask_placeholders("50%% off today")
        self.assertIn("off today", masked)
        self.assertEqual(list(replacements.values()), ["%%"])
        self.assertIn("%(name)s", core.placeholder_tokens("%(name)s grows"))
        self.assertIn("§x", core.placeholder_tokens("§x§f§f§0§0§0§0Hello"))

    def test_duplicate_placeholder_reordering_is_rejected(self):
        source = "Gains %s then %s"
        masked, replacements = core.mask_placeholders(source)
        self.assertEqual(list(replacements), ["__MC_PH_0__", "__MC_PH_1__"])
        swapped = "获得 __MC_PH_1__ 然后 __MC_PH_0__"
        self.assertIsNone(core.restore_and_validate(source, swapped, {}, set()))
        correct = "获得 __MC_PH_0__ 然后 __MC_PH_1__"
        self.assertEqual(core.restore_and_validate(source, correct, {}, set()), "获得 %s 然后 %s")


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
            # 社区基线来自 CFPA 整合包缓存：手工放一个假整合包进缓存目录。
            pack_cache = Path(tmp) / "CFPA_Modpack.zip"
            with zipfile.ZipFile(pack_cache, "w") as zf:
                zf.writestr("assets/demo/lang/zh_cn.json", json.dumps({"a": "不同苹果", "b": "梨"}, ensure_ascii=False))
            with patch.object(urllib.request, "urlopen", side_effect=AssertionError("基线读取不该联网")):
                baseline = core.fetch_community_baseline(scan, core.DEFAULT_CONFIG, instance)
            self.assertEqual(baseline, {"demo": {"b": "梨"}})
            self.assertEqual(len(core.missing_entries(scan.english, scan.community + [core.LangFile("demo", "zh_cn", "community", baseline["demo"])], [], fill_community=True)), 1)
            self.assertEqual([entry.key for entry in core.entries_with_baseline(scan, baseline)], ["c"])
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

    def test_missing_pack_does_not_interrupt_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            scan = core.ScanResult(english=[core.LangFile("demo", "en_us", "jar", {"a": "Apple"})])
            with patch.object(urllib.request, "urlopen", side_effect=AssertionError("无整合包时不该联网")):
                self.assertEqual(core.fetch_community_baseline(scan, core.DEFAULT_CONFIG, Path(tmp)), {})
            self.assertEqual(len(core.missing_entries(scan.english, scan.community, scan.ai)), 1)

    def test_community_pack_translations_reads_json_and_lang(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = Path(tmp) / "CFPA_Modpack.zip"
            with zipfile.ZipFile(pack, "w") as zf:
                zf.writestr("assets/jei/lang/zh_cn.json", json.dumps({"item.jei": "合成"}, ensure_ascii=False))
                zf.writestr("assets/TwilightForest/lang/zh_cn.lang", "entity.boar=野猪\n")
                zf.writestr("assets/other/lang/en_us.json", "{}")
                zf.writestr("assets/broken/lang/zh_cn.json", "not json")
            hits = core.community_pack_translations(pack, ["jei", "TwilightForest", "other", "broken", "missing"])
            self.assertEqual(hits["jei"], {"item.jei": "合成"})
            self.assertEqual(hits["TwilightForest"], {"entity.boar": "野猪"})
            self.assertNotIn("broken", hits)
            self.assertNotIn("missing", hits)
            # 大小写不敏感匹配 modid，返回键保留调用方写法
            self.assertIn("TwilightForest", hits)
            self.assertEqual(core.community_pack_translations(None, ["jei"]), {})


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
            with patch.object(ferrypackio, "load_locks", return_value=core.load_locks(locks)):
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
            with patch.object(ferrypackio, "translate_entries", return_value=({"demo": {"key": "新译文"}}, [], {})) as translate:
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
            with patch.object(ferryconfig, "is_git_ignored", return_value=False):
                warning = core.config_secret_risk(config)
            self.assertIn("泄漏", warning or "")
            with patch.object(ferryconfig, "is_git_ignored", return_value=True):
                self.assertIsNone(core.config_secret_risk(config))
            config.write_text(json.dumps({"openai_api_key": ""}), encoding="utf-8")
            with patch.object(ferryconfig, "is_git_ignored", return_value=False):
                self.assertIsNone(core.config_secret_risk(config))

    def test_public_config_redacts_nested_api_keys(self):
        secret = "sk-test-placeholder-not-a-real-key"
        shown = core.public_config({"openai_api_key": secret, "model_pool": [{"api_key": secret}]})
        self.assertEqual(shown["openai_api_key"], "sk-***")
        self.assertEqual(shown["model_pool"][0]["api_key"], "sk-***")


class ApiKeyProtectionTests(unittest.TestCase):
    """API Key 落盘保护：Windows 用 DPAPI 加密，其它平台原样存（不假装安全）。

    CI 跑在 ubuntu，DPAPI 不可用 → 涉及密文的用例自动跳过；
    与平台无关的用例（不改入参、往返不丢值）两边都要过。
    """

    CONFIG = {
        "engine": "openai",
        "openai_api_key": "sk-top-secret-value",
        "anthropic_api_key": "",
        "model_pool": [{"api_key": "sk-pool-secret", "model": "x"}],
        "custom_profiles": {"p": {"provider": "openai", "api_key": "sk-profile-secret"}},
        "mymemory_email": "me@example.com",
        "glossary": {"a": "b"},
    }

    def test_secret_field_detection(self):
        self.assertTrue(secretbox.is_secret_field("openai_api_key"))
        self.assertTrue(secretbox.is_secret_field("api_key"))
        self.assertTrue(secretbox.is_secret_field("API_KEY"))
        self.assertFalse(secretbox.is_secret_field("mymemory_email"))
        self.assertFalse(secretbox.is_secret_field("openai_model"))

    def test_protect_config_does_not_mutate_input(self):
        original = json.loads(json.dumps(self.CONFIG))
        secretbox.protect_config(self.CONFIG)
        self.assertEqual(self.CONFIG, original)
        secretbox.unprotect_config(self.CONFIG)
        self.assertEqual(self.CONFIG, original)

    def test_config_roundtrip_keeps_every_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ferry_config.json"
            core.save_config(self.CONFIG, path)
            back = core.load_config(path)
            self.assertEqual(back["openai_api_key"], "sk-top-secret-value")
            self.assertEqual(back["model_pool"][0]["api_key"], "sk-pool-secret")
            self.assertEqual(back["custom_profiles"]["p"]["api_key"], "sk-profile-secret")
            self.assertEqual(back["anthropic_api_key"], "")
            # 非密钥字段不该被碰
            self.assertEqual(back["mymemory_email"], "me@example.com")
            self.assertEqual(back["glossary"], {"a": "b"})
            # 内存里的那份不能被替换成密文
            self.assertEqual(self.CONFIG["openai_api_key"], "sk-top-secret-value")

    @unittest.skipUnless(secretbox.available(), "仅 Windows DPAPI 可用")
    def test_api_keys_are_not_plaintext_on_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ferry_config.json"
            core.save_config(self.CONFIG, path)
            raw = path.read_text(encoding="utf-8")
            for secret in ("sk-top-secret-value", "sk-pool-secret", "sk-profile-secret"):
                self.assertNotIn(secret, raw)
            self.assertIn(secretbox.ENC_PREFIX, raw)

    @unittest.skipUnless(secretbox.available(), "仅 Windows DPAPI 可用")
    def test_encrypted_config_is_not_flagged_as_leak(self):
        """密文即使被提交也解不开，不该再弹「密钥泄漏」把真警报淹了。"""
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".git").mkdir()
            path = project / "ferry_config.json"
            core.save_config(self.CONFIG, path)
            with patch.object(ferryconfig, "is_git_ignored", return_value=False):
                self.assertIsNone(core.config_secret_risk(path))

    @unittest.skipUnless(secretbox.available(), "仅 Windows DPAPI 可用")
    def test_undecryptable_blob_degrades_to_empty(self):
        self.assertEqual(secretbox.unprotect("dpapi:bm90LXJlYWxseS1kcGFwaQ=="), "")
        self.assertEqual(secretbox.unprotect("dpapi:!!!not-base64!!!"), "")

    @unittest.skipUnless(secretbox.available(), "仅 Windows DPAPI 可用")
    def test_protect_is_idempotent(self):
        once = secretbox.protect("sk-once")
        self.assertEqual(secretbox.protect(once), once)
        self.assertEqual(secretbox.unprotect(once), "sk-once")

    @unittest.skipUnless(secretbox.available(), "仅 Windows DPAPI 可用")
    def test_load_config_migrates_plaintext_keys_in_place(self):
        """升级上来的老配置：第一次读取就地把明文密钥换成密文，语义不变。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ferry_config.json"
            path.write_text(json.dumps(self.CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
            self.assertIn("sk-top-secret-value", path.read_text(encoding="utf-8"))
            back = core.load_config(path)
            raw = path.read_text(encoding="utf-8")
            for secret in ("sk-top-secret-value", "sk-pool-secret", "sk-profile-secret"):
                self.assertNotIn(secret, raw)
            self.assertIn(secretbox.ENC_PREFIX, raw)
            # 迁移只改存储形态，不该改语义
            self.assertEqual(back["openai_api_key"], "sk-top-secret-value")
            self.assertEqual(back["model_pool"][0]["api_key"], "sk-pool-secret")
            self.assertEqual(back["custom_profiles"]["p"]["api_key"], "sk-profile-secret")
            self.assertEqual(back["mymemory_email"], "me@example.com")
            self.assertEqual(back["glossary"], {"a": "b"})

    def test_load_config_does_not_rewrite_secret_free_file(self):
        """没有明文密钥时不碰文件 —— 免得每次启动都无谓写盘。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ferry_config.json"
            path.write_text('{\n  "engine": "mymemory",\n  "openai_api_key": ""\n}\n', encoding="utf-8")
            before = path.read_bytes()
            core.load_config(path)
            self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(secretbox.available(), "仅 Windows DPAPI 可用")
    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ferry_config.json"
            path.write_text(json.dumps(self.CONFIG, ensure_ascii=False), encoding="utf-8")
            core.load_config(path)
            once = path.read_bytes()
            core.load_config(path)
            self.assertEqual(path.read_bytes(), once)

    def test_no_dpapi_keeps_plaintext_file_untouched(self):
        """DPAPI 不可用（非 Windows）时绝不写盘：写不出密文就别动用户的文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ferry_config.json"
            path.write_text(json.dumps({"openai_api_key": "sk-plain"}), encoding="utf-8")
            before = path.read_bytes()
            with patch.object(secretbox, "available", return_value=False):
                back = core.load_config(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(back["openai_api_key"], "sk-plain")


class LiteralBridgeFetchTests(unittest.TestCase):
    """LiteralBridge 自动获取：版本区间判定、附件挑选、下载后复核。

    全部离线：不碰网络，用临时 jar 造元数据。真实的 Release 查询只靠手工验证。
    """

    def test_mc_range_allows_handles_common_forge_ranges(self):
        self.assertTrue(core.mc_range_allows("[1.20.1,1.21)", "1.20.1"))
        self.assertTrue(core.mc_range_allows("[1.20.1,1.21)", "1.20.6"))
        self.assertFalse(core.mc_range_allows("[1.20.1,1.21)", "1.21.10"))
        self.assertTrue(core.mc_range_allows("[1.20.1,)", "1.21.10"))
        self.assertFalse(core.mc_range_allows("[1.21,)", "1.20.1"))
        self.assertTrue(core.mc_range_allows("[1.20,1.21]", "1.21.0"))
        self.assertTrue(core.mc_range_allows("1.20.1", "1.20.1"))
        self.assertTrue(core.mc_range_allows("*", "9.9.9"))
        # 解析不出来的写法一律放行，不能误拒
        self.assertTrue(core.mc_range_allows("这不是版本号", "1.20.1"))

    def _make_mod_jar(self, path: Path, *, loader: str, mc_range: str, version: str = "9.9.9") -> Path:
        entry = "META-INF/neoforge.mods.toml" if loader == "neoforge" else "META-INF/mods.toml"
        toml = (
            'modLoader="javafml"\n'
            '[[mods]]\n'
            'modId="literalbridge"\n'
            f'version="{version}"\n'
            '[[dependencies.literalbridge]]\n'
            'modId="minecraft"\n'
            f'versionRange="{mc_range}"\n'
        )
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr(entry, toml)
        return path

    def test_read_mod_jar_info_extracts_loader_and_mc_range(self):
        with tempfile.TemporaryDirectory() as tmp:
            jar = self._make_mod_jar(Path(tmp) / "lb.jar", loader="forge", mc_range="[1.20.1,1.21)")
            info = core.read_mod_jar_info(jar)
            self.assertEqual(info["modid"], "literalbridge")
            self.assertEqual(info["version"], "9.9.9")
            self.assertEqual(info["loaders"], ["forge"])
            self.assertEqual(info["mc_ranges"], ["[1.20.1,1.21)"])

    def test_read_mod_jar_info_returns_empty_for_garbage(self):
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "not-a-jar.jar"
            bogus.write_bytes(b"definitely not a zip")
            self.assertEqual(core.read_mod_jar_info(bogus), {})

    def test_pick_literalbridge_asset_prefers_loader_and_version_in_name(self):
        release = {
            "assets": [
                {"name": "literalbridge-1.3.0.jar", "url": "u1", "size": 10},
                {"name": "literalbridge-forge-1.20.1-1.2.0.jar", "url": "u2", "size": 10},
                {"name": "literalbridge-1.3.0-sources.jar", "url": "u3", "size": 10},
            ]
        }
        picked = core.pick_literalbridge_asset(release, "forge", "1.20.1")
        self.assertEqual(picked["name"], "literalbridge-forge-1.20.1-1.2.0.jar")

    def test_pick_literalbridge_asset_falls_back_to_generic_name(self):
        release = {"assets": [{"name": "literalbridge-1.3.0.jar", "url": "u1", "size": 10}]}
        picked = core.pick_literalbridge_asset(release, "forge", "1.20.1")
        self.assertEqual(picked["name"], "literalbridge-1.3.0.jar")

    def test_pick_literalbridge_asset_rejects_sources_and_javadoc(self):
        release = {
            "assets": [
                {"name": "literalbridge-1.3.0-sources.jar", "url": "u1", "size": 10},
                {"name": "literalbridge-1.3.0-javadoc.jar", "url": "u2", "size": 10},
            ]
        }
        self.assertIsNone(core.pick_literalbridge_asset(release, "forge", "1.20.1"))

    def test_download_literalbridge_reports_no_match_without_writing(self):
        """不做版本复核就会往 NeoForge 实例里塞 Forge 版 jar —— 这里守住这条线。"""
        release = {"version": "1.3.0", "page": "p", "assets": [{"name": "literalbridge-1.3.0.jar", "url": "u", "size": 1}]}
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            with patch.object(ferryliteralbridge, "literalbridge_releases", return_value=[release]), patch.object(
                ferryliteralbridge, "download_release_asset", side_effect=self._write_forge_jar
            ):
                result = core.download_literalbridge(mods, loader="neoforge", mc_version="1.21.10")
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "no_match")
            self.assertFalse(mods.exists())

    def test_download_literalbridge_installs_matching_jar(self):
        release = {"version": "1.3.0", "page": "p", "assets": [{"name": "literalbridge-1.3.0.jar", "url": "u", "size": 1}]}
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            with patch.object(ferryliteralbridge, "literalbridge_releases", return_value=[release]), patch.object(
                ferryliteralbridge, "download_release_asset", side_effect=self._write_forge_jar
            ):
                result = core.download_literalbridge(mods, loader="forge", mc_version="1.20.1")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["version"], "1.3.0")
            self.assertTrue((mods / "literalbridge-1.3.0.jar").is_file())

    def test_download_literalbridge_falls_back_when_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ferryliteralbridge, "literalbridge_releases", side_effect=OSError("no network")):
                result = core.download_literalbridge(Path(tmp) / "mods", loader="forge", mc_version="1.20.1")
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "network")
            self.assertEqual(result["page"], core.LITERALBRIDGE_PAGE)

    def _write_forge_jar(self, url, dest, timeout=60.0, progress=None, attempts=1):
        """冒充下载：落一个支持 forge / [1.20.1,1.21) 的假 jar。"""
        self._make_mod_jar(Path(dest), loader="forge", mc_range="[1.20.1,1.21)")
        return Path(dest)

    # ---- 自动升级（2026-10-12 加） ----

    def test_version_key_orders_numerically_not_lexically(self):
        self.assertEqual(core.version_key("1.3.0"), (1, 3, 0))
        self.assertGreater(core.version_key("1.10.0"), core.version_key("1.9.9"))
        self.assertEqual(core.version_key(""), (0,))

    def test_installed_literalbridge_ignores_backups_and_picks_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            mods.mkdir()
            self._make_mod_jar(mods / "literalbridge-forge-1.20.1-1.2.0.jar", loader="forge", mc_range="[1.20.1,1.21)", version="1.2.0")
            self._make_mod_jar(mods / "literalbridge-1.3.0.jar", loader="forge", mc_range="[1.20.1,1.21)", version="1.3.0")
            # 升级时被改名的旧版不能算「已装」，否则每一轮都会重复升级
            self._make_mod_jar(mods / "literalbridge-9.9.9.jar.bak-20261012", loader="forge", mc_range="[1.20.1,1.21)", version="9.9.9")
            found = core.installed_literalbridge(mods)
            self.assertEqual(found["version"], "1.3.0")
            self.assertTrue(found["path"].endswith("literalbridge-1.3.0.jar"))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(core.installed_literalbridge(Path(tmp) / "mods"))

    def test_download_literalbridge_already_installed_does_not_touch_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            mods.mkdir()
            self._make_mod_jar(mods / "literalbridge-1.3.0.jar", loader="forge", mc_range="[1.20.1,1.21)", version="1.3.0")
            with patch.object(ferryliteralbridge, "literalbridge_releases", side_effect=AssertionError("不该联网")):
                result = core.download_literalbridge(mods, loader="forge", mc_version="1.20.1")
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "already_installed")
            self.assertEqual(result["version"], "1.3.0")

    def test_download_literalbridge_upgrade_reports_up_to_date_without_downloading(self):
        """已是最新时不该再下整份 jar —— 否则下载一失败就被误报成网络错。"""
        release = {"version": "1.3.0", "page": "p", "assets": [{"name": "literalbridge-1.3.0.jar", "url": "u", "size": 1}]}
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            mods.mkdir()
            self._make_mod_jar(mods / "literalbridge-1.3.0.jar", loader="forge", mc_range="[1.20.1,1.21)", version="1.3.0")
            with patch.object(ferryliteralbridge, "literalbridge_releases", return_value=[release]), patch.object(
                ferryliteralbridge, "download_release_asset", side_effect=AssertionError("不该下载")
            ):
                result = core.download_literalbridge(mods, loader="forge", mc_version="1.20.1", upgrade=True)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["action"], "up_to_date")
            self.assertEqual([p.name for p in mods.glob("*")], ["literalbridge-1.3.0.jar"])

    def test_download_literalbridge_upgrade_swaps_jar_and_keeps_one_backup(self):
        release = {"version": "1.4.0", "page": "p", "assets": [{"name": "literalbridge-1.4.0.jar", "url": "u", "size": 1}]}
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            mods.mkdir()
            self._make_mod_jar(mods / "literalbridge-forge-1.20.1-1.2.0.jar", loader="forge", mc_range="[1.20.1,1.21)", version="1.2.0")
            with patch.object(ferryliteralbridge, "literalbridge_releases", return_value=[release]), patch.object(
                ferryliteralbridge, "download_release_asset", side_effect=self._write_forge_jar
            ):
                result = core.download_literalbridge(mods, loader="forge", mc_version="1.20.1", upgrade=True)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["action"], "upgraded")
            self.assertEqual((result["from_version"], result["version"]), ("1.2.0", "1.4.0"))
            # 关键：mods 里只许留一份 .jar —— 两份同 modId 的 jar 会直接崩游戏
            self.assertEqual([p.name for p in mods.glob("*.jar")], ["literalbridge-1.4.0.jar"])
            self.assertIn(".jar.bak-", result["backup"])
            self.assertTrue(Path(result["backup"]).is_file())

    def test_download_literalbridge_upgrade_picks_newest_release_not_first(self):
        """GitHub 的返回顺序别指望 —— 按版本号排序，否则可能换成本地更旧的版本。"""
        older = {"version": "1.1.0", "page": "p", "assets": [{"name": "literalbridge-1.1.0.jar", "url": "u", "size": 1}]}
        newer = {"version": "1.4.0", "page": "p", "assets": [{"name": "literalbridge-1.4.0.jar", "url": "u", "size": 1}]}
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            mods.mkdir()
            self._make_mod_jar(mods / "literalbridge-forge-1.20.1-1.2.0.jar", loader="forge", mc_range="[1.20.1,1.21)", version="1.2.0")
            with patch.object(ferryliteralbridge, "literalbridge_releases", return_value=[older, newer]), patch.object(
                ferryliteralbridge, "download_release_asset", side_effect=self._write_forge_jar
            ):
                result = core.download_literalbridge(mods, loader="forge", mc_version="1.20.1", upgrade=True)
            self.assertEqual(result["version"], "1.4.0", result)

    def test_download_literalbridge_refuses_upgrade_when_old_jar_cannot_be_retired(self):
        """旧版改名失败必须放弃升级 —— 宁可没升，也不能留下两份同 modId 的 jar。"""
        release = {"version": "1.4.0", "page": "p", "assets": [{"name": "literalbridge-1.4.0.jar", "url": "u", "size": 1}]}
        with tempfile.TemporaryDirectory() as tmp:
            mods = Path(tmp) / "mods"
            mods.mkdir()
            old = self._make_mod_jar(mods / "literalbridge-forge-1.20.1-1.2.0.jar", loader="forge", mc_range="[1.20.1,1.21)", version="1.2.0")
            with patch.object(ferryliteralbridge, "literalbridge_releases", return_value=[release]), patch.object(
                ferryliteralbridge, "download_release_asset", side_effect=self._write_forge_jar
            ), patch.object(ferryliteralbridge, "_retire_old_literalbridge", return_value=""):
                result = core.download_literalbridge(mods, loader="forge", mc_version="1.20.1", upgrade=True)
            self.assertFalse(result["ok"])
            self.assertEqual(result["reason"], "backup_failed")
            self.assertEqual([p.name for p in mods.glob("*.jar")], [old.name])

    # ---- 下载重试（2026-10-12 加） ----

    def test_download_release_asset_retries_transient_failures(self):
        """实测 GitHub 附件 3 次里挂 2 次（连接被重置）—— 不重试等于这个功能不能用。"""
        calls = {"n": 0}

        class _Response:
            def __init__(self, payload: bytes) -> None:
                self._buffer = io.BytesIO(payload)
                self.headers = {"Content-Length": str(len(payload))}

            def read(self, size: int = -1) -> bytes:
                return self._buffer.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionResetError("连接被重置")
            return _Response(b"jar-bytes")

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "lb.jar"
            with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), patch.object(time, "sleep"):
                core.download_release_asset("https://example.invalid/lb.jar", dest, attempts=3)
            self.assertEqual(calls["n"], 3)
            self.assertEqual(dest.read_bytes(), b"jar-bytes")

    def test_download_release_asset_default_does_not_retry(self):
        """默认 attempts=1 —— 其它调用点（CFPA 整合包 / 摆渡自更新）的行为不能被悄悄改掉。"""
        calls = {"n": 0}

        def fake_urlopen(request, timeout=None):
            calls["n"] += 1
            raise ConnectionResetError("连接被重置")

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "x.jar"
            with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen), patch.object(time, "sleep"):
                with self.assertRaises(OSError):
                    core.download_release_asset("https://example.invalid/x.jar", dest)
            self.assertEqual(calls["n"], 1)

    def test_detect_mod_loader_votes_from_mods(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            mods = instance / "mods"
            mods.mkdir()
            self._make_mod_jar(mods / "a.jar", loader="forge", mc_range="[1.20.1,1.21)")
            self._make_mod_jar(mods / "b.jar", loader="forge", mc_range="[1.20.1,1.21)")
            self.assertEqual(core.detect_mod_loader(instance), "forge")
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            mods = instance / "mods"
            mods.mkdir()
            self._make_mod_jar(mods / "c.jar", loader="neoforge", mc_range="[1.21,1.22)")
            self.assertEqual(core.detect_mod_loader(instance), "neoforge")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(core.detect_mod_loader(Path(tmp)), "")


class DiagnoseTests(unittest.TestCase):
    """一键诊断包：**能发得出去**（不含密钥、日志脱敏），且读不到文件也不崩。

    这是给普通玩家用的功能 —— 他们对「日志」「配置」没概念，只会按一下按钮然后
    把文件发出去。所以「文件里不能有他的密钥」不是可选项，是前提。
    """

    def test_config_shape_never_shows_secret_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "ferry_config.json"
            core.save_config(
                {"openai_api_key": "sk-diag-secret-1", "model_pool": [{"api_key": "sk-diag-secret-2"}]},
                cfg,
            )
            text = "\n".join(ferrydiagnose._config_shape(cfg))
            self.assertNotIn("sk-diag-secret-1", text)
            self.assertNotIn("sk-diag-secret-2", text)
            self.assertIn("openai_api_key", text)

    def test_plaintext_secret_is_flagged(self):
        """老配置是明文时要一眼看出来 —— 这正是最需要修的状态。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "ferry_config.json"
            cfg.write_text(
                json.dumps({"openai_api_key": "sk-plain-value", "model_pool": [{"api_key": "sk-nested"}]}),
                encoding="utf-8",
            )
            text = "\n".join(ferrydiagnose._config_shape(cfg))
            self.assertIn("明文", text)
            self.assertNotIn("sk-plain-value", text)
            self.assertNotIn("sk-nested", text)

    @unittest.skipUnless(secretbox.available(), "仅 Windows DPAPI 可用")
    def test_encrypted_secret_is_reported_as_ciphertext(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "ferry_config.json"
            core.save_config({"openai_api_key": "sk-encrypted"}, cfg)
            text = "\n".join(ferrydiagnose._config_shape(cfg))
            self.assertIn("密文", text)
            self.assertNotIn("sk-encrypted", text)

    def test_log_tail_is_redacted(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "ferry.log"
            log.write_text("Authorization: Bearer sk-abcdefgh12345678\n普通一行\n", encoding="utf-8")
            with patch.object(ferrydiagnose, "LOG_FILE", log), \
                    patch.object(ferrydiagnose, "CONFIG_FILE", Path(tmp) / "no-config.json"):
                text = ferrydiagnose.build_diagnostic_report()
            self.assertNotIn("sk-abcdefgh12345678", text)
            self.assertIn("普通一行", text)

    def test_missing_everything_still_returns_text(self):
        """读不到日志 / 没有实例 / 没有配置，依然要给出一份可读的报告，不能抛异常。"""
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(ferrydiagnose, "LOG_FILE", Path(tmp) / "nope.log"), \
                    patch.object(ferrydiagnose, "CONFIG_FILE", Path(tmp) / "nope.json"):
                text = ferrydiagnose.build_diagnostic_report(instance=Path(tmp) / "nope-instance")
            self.assertIn("诊断包", text)
            self.assertIn("运行环境", text)
            self.assertIn("游戏侧", text)

    def test_write_diagnostic_report_creates_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "nested" / "ferry-diagnose-x.txt"
            with patch.object(ferrydiagnose, "LOG_FILE", Path(tmp) / "nope.log"), \
                    patch.object(ferrydiagnose, "CONFIG_FILE", Path(tmp) / "nope.json"):
                written = ferrydiagnose.write_diagnostic_report(app_version="9.9.9", dest=dest)
            self.assertEqual(written, dest)
            self.assertTrue(dest.is_file())
            self.assertIn("9.9.9", dest.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
