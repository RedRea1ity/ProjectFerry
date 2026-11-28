"""P1 与 P2 核心能力离线回归测试：
- P1-A：剧本预分析（ferry_worldbook.json）分层抽样、出处幻觉过滤、合并保留与翻译注入；
- P1-B：纯净汉化补丁包导出（UndoLog 差异收集、二进制/大文件版权拦截、副本目录 diff、安装/卸载脚本与 SHA-256 清单）；
- P2-A：UE4/5 补强（AES 密钥 Hex/Base64 校验、加密 .pak 与 IoStore 诊断、repak --aes-key 参数、StringTable .uasset/.uexp 解析与无损重建）；
- P2-B：高频引擎中文字体回退（Ren'Py / RM MV / RM MZ / Kirikiri / Godot / 缺字 TTF 替换 + UndoLog 无损还原 + 补丁包联动）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import mc_ai_translator as core
from ferrycore import adapters, deps, fontpatch, localize, patchpack
from ferrycore.adapters import unreal
from ferrycore.adapters.undo import UndoLog, revert as revert_game
from ferrycore.fontcheck import check_font
from ferrygame.pipeline import analyze_game, translate_project
from ferrygame.project import Project
from ferrygame.units import Unit
from ferrygame.worldbook import (
    WORLDBOOK_NAME,
    CharacterEntry,
    Worldbook,
    analyze_units,
    load as load_worldbook,
    sample_units_for_analysis,
    verify_and_filter_worldbook,
)


def _make_minimal_sfnt(codepoints: list[int], family: str = "TinyFont") -> bytes:
    """构造最小合法单表 TTF（head + cmap format 4 + name），用于离线测试字体体检与替换。"""
    # 1. cmap format 4（仅保留单段或逐码位段，足够 fontcheck / ttf_rename 解析）
    cps = sorted(set(c for c in codepoints if 1 <= c <= 0xFFFE))
    ranges: list[tuple[int, int]] = []
    for cp in cps:
        if ranges and cp == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], cp)
        else:
            ranges.append((cp, cp))
    ranges.append((0xFFFF, 0xFFFF))

    seg_count = len(ranges)
    seg_x2 = seg_count * 2
    end_codes = b"".join(struct.pack(">H", hi) for _, hi in ranges)
    start_codes = b"".join(struct.pack(">H", lo) for lo, _ in ranges)
    deltas = b"".join(struct.pack(">h", 1 if lo != 0xFFFF else 1) for lo, _ in ranges)
    range_offs = b"\x00\x00" * seg_count
    sub_len = 14 + seg_x2 * 4 + 2
    fmt4 = (
        struct.pack(">HHHHHHH", 4, sub_len, 0, seg_x2, 2, 0, 0)
        + end_codes
        + b"\x00\x00"
        + start_codes
        + deltas
        + range_offs
    )
    cmap_table = struct.pack(">HH", 0, 1) + struct.pack(">HHI", 3, 1, 12) + fmt4

    # 2. name 表（Win Unicode platform=3, encoding=1）
    fam_u16 = family.encode("utf-16-be")
    ps_u16 = family.replace(" ", "").encode("utf-16-be")
    entries = [(3, 1, 1033, 1, fam_u16), (3, 1, 1033, 4, fam_u16), (3, 1, 1033, 6, ps_u16)]
    storage = bytearray()
    recs = bytearray()
    for plat, enc, lang, nid, raw in entries:
        off = len(storage)
        storage.extend(raw)
        recs.extend(struct.pack(">6H", plat, enc, lang, nid, len(raw), off))
    name_table = struct.pack(">HHH", 0, len(entries), 6 + len(recs)) + bytes(recs) + bytes(storage)

    # 3. head 表（54 字节）
    head_table = bytearray(54)
    struct.pack_into(">II", head_table, 0, 0x00010000, 0x00010000)
    struct.pack_into(">I", head_table, 12, 0x5F0F3CF5)

    tables = [(b"cmap", cmap_table), (b"head", bytes(head_table)), (b"name", name_table)]
    offset = 12 + len(tables) * 16
    directory = bytearray()
    body = bytearray()
    for tag, blob in tables:
        padded = blob + b"\x00" * ((4 - len(blob) % 4) % 4)
        directory.extend(struct.pack(">4sIII", tag, 0, offset, len(blob)))
        body.extend(padded)
        offset += len(padded)
    return struct.pack(">IHHHH", 0x00010000, len(tables), 32, 1, 0) + bytes(directory) + bytes(body)


def _make_sample_uasset_stringtable(
    namespace: str,
    pairs: list[tuple[str, str]],
) -> tuple[bytes, bytes]:
    """构造标准 UE4/UE5 分离式 StringTable `.uasset` + `.uexp` 二进制测试夹具。"""
    uexp = bytearray()
    uexp.extend(struct.pack("<q", 0))  # None FName 属性结束标记
    uexp.extend(unreal.write_fstring(namespace))
    uexp.extend(struct.pack("<i", len(pairs)))
    for k, v in pairs:
        uexp.extend(unreal.write_fstring(k))
        uexp.extend(unreal.write_fstring(v))
    uexp.extend(struct.pack("<i", 0))  # NumMetaData = 0
    serial_size = len(uexp)
    uexp.extend(unreal.UASSET_MAGIC_BYTES)  # PACKAGE_FILE_TAG

    uasset = bytearray()
    uasset.extend(unreal.UASSET_MAGIC_BYTES)
    uasset.extend(struct.pack("<ii", -7, 0))  # LegacyFileVersion / LegacyUE3Version
    uasset.extend(b"\x00" * 16)
    uasset.extend(b"StringTable\x00/Script/Engine\x00")
    uasset.extend(struct.pack("<q", serial_size))  # 导出项 SerialSize
    uasset.extend(b"\x00" * 16)
    return bytes(uasset), bytes(uexp)


def _make_encrypted_pak_stub(version: int = 11, encrypted_index: bool = True) -> bytes:
    """构造带加密标记的 UE5 .pak 尾部存根（用于测试 encrypted_index 探测）。"""
    body = b"\x00" * 64
    index_offset = len(body)
    index_blob = b"\xAA" * 32
    index_hash = hashlib.sha1(index_blob).digest()
    footer = struct.pack(
        "<BIIqq20s",
        1 if encrypted_index else 0,
        unreal.UE_PAK_MAGIC,
        version,
        index_offset,
        len(index_blob),
        index_hash,
    )
    return body + index_blob + footer + (b"Oodle" + b"\x00" * 27)


# ==================== P1-A：剧本预分析测试 ====================


class WorldbookTests(unittest.TestCase):
    def test_sampling_and_hallucination_filtering(self):
        units = [
            Unit(key="s1", kind="name", src="Charlotte", file="db.json", order=1),
            Unit(key="s2", kind="menu", src="New Game", file="sys.json", order=2),
            Unit(key="s3", kind="story", src="Charlotte enters the House of Mirrors.", speaker="Narrator", file="ch1.json", order=3),
            Unit(key="s4", kind="story", src="Welcome to White Room, Charlotte!", speaker="Q84", file="ch2.json", order=4),
        ]
        sampled = sample_units_for_analysis(units)
        self.assertIn("Charlotte", sampled["names"])
        self.assertIn("Q84", sampled["speakers"])
        self.assertTrue(any("White Room" in s for s in sampled["story_samples"]))

        raw_model_out = {
            "worldview": "这是一个超现实悬疑冒险故事。",
            "style": "偏冷峻微带黑色幽默的简体中文。",
            "characters": [
                {"src": "Charlotte", "dst": "夏洛特", "gender": "女", "pronoun": "她", "tone": "内向礼貌"},
                {"src": "InventedGhost", "dst": "幻觉幽灵", "gender": "男"},  # 原文不存在 -> 应被过滤
            ],
            "glossary": {
                "White Room": "白房间",
                "House of Mirrors": "镜之屋",
                "FakeTerm999": "模型编造词",  # 原文不存在 -> 应被过滤
            },
        }
        wb, dropped = verify_and_filter_worldbook(raw_model_out, units, root=Path("."), game="Demo", source_lang="en")
        self.assertEqual(dropped, 2)
        self.assertEqual([c.src for c in wb.characters], ["Charlotte"])
        self.assertEqual(wb.glossary, {"White Room": "白房间", "House of Mirrors": "镜之屋"})
        self.assertIn("White Room", wb.evidence["White Room"])

    def test_analyze_saves_merges_and_injects_into_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "MyVN"
            root.mkdir()
            units = [
                Unit(key="1", kind="story", src="Charlotte opened the White Room door.", file="ch1.txt", order=1),
                Unit(key="2", kind="story", src="Hello Charlotte!", file="ch1.txt", order=2),
            ]
            settings = {"engine": "openai", "model": "gpt-test", "api_key": "sk-test", "base_url": "https://example.com/v1"}

            def fake_analyze(_engine, _payload, _req_settings):
                return {
                    "worldview": "围绕白房间展开的心理冒险。",
                    "style": "克制冷静的口语。",
                    "characters": [{"src": "Charlotte", "dst": "夏洛特", "pronoun": "她"}],
                    "glossary": {"White Room": "白房间"},
                }

            wb = analyze_units(units, settings, root=root, game="MyVN", source_lang="en", call=fake_analyze)
            self.assertTrue((root / WORLDBOOK_NAME).is_file())
            loaded = load_worldbook(root)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.glossary["White Room"], "白房间")

            # 手工改一条译名后再次非强制分析 -> 应直接复用已有文件，保留手工编辑
            loaded.glossary["White Room"] = "白色房间(手改)"
            loaded.save()
            wb2 = analyze_units(units, settings, root=root, game="MyVN", source_lang="en", force=False, call=fake_analyze)
            self.assertEqual(wb2.glossary["White Room"], "白色房间(手改)")

            # 翻译时应自动注入 worldbook prompt 与 glossary
            captured_prompts: list[str] = []
            captured_glossaries: list[dict[str, str]] = []

            def fake_translate(_engine, masked, req_settings):
                captured_prompts.append(req_settings["system_prompt_override"])
                captured_glossaries.append(dict(req_settings["_glossary"]))
                return {k: f"译:{v}" for k, v in masked.items()}

            proj = Project(root=root, game="MyVN", adapter="dummy", source_lang="en", units=units)
            rep = translate_project(proj, settings, call=fake_translate, cache_dir=root / "cache", save=False)
            self.assertEqual(rep.translated, 2)
            self.assertTrue(captured_prompts and "白房间" in captured_prompts[0])
            self.assertIn("夏洛特", captured_prompts[0])
            self.assertEqual(captured_glossaries[0].get("White Room"), "白色房间(手改)")
            self.assertEqual(captured_glossaries[0].get("Charlotte"), "夏洛特")

    def test_analyze_rejects_non_llm_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            units = [Unit(key="1", kind="story", src="Hello world", file="a.txt", order=1)]
            with self.assertRaises(ValueError):
                analyze_units(units, {"engine": "mymemory"}, root=root, source_lang="en")

    def test_localize_game_auto_injects_existing_worldbook(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "RenDemo"
            game = root / "game"
            game.mkdir(parents=True)
            (game / "script.rpy").write_text('label start:\n    "Charlotte enters the White Room."\n', encoding="utf-8")
            wb = Worldbook(
                root=root,
                game="RenDemo",
                source_lang="en",
                worldview="测试世界观",
                characters=[CharacterEntry(src="Charlotte", dst="夏洛特", pronoun="她")],
                glossary={"White Room": "白房间"},
            )
            wb.save()

            seen_settings: dict = {}

            def fake_translate_entries(entries, settings, **_kwargs):
                seen_settings.update(settings)
                return {entries[0].modid: {entries[0].english: "夏洛特走进了白房间。"}}, [], {}

            with patch.object(localize, "translate_entries", side_effect=fake_translate_entries):
                res = localize.localize_game(
                    root,
                    settings={"engine": "openai", "model": "test", "api_key": "sk-x", "base_url": "https://x.test/v1"},
                )
            self.assertTrue(res.ok)
            self.assertEqual(seen_settings["term_hints"].get("White Room"), "白房间")
            self.assertIn("测试世界观", seen_settings["system_prompt_override"])
            self.assertTrue(any("已启用剧本设定集" in n for n in res.notes))


# ==================== P1-B：纯净汉化补丁包导出测试 ====================


class PatchPackTests(unittest.TestCase):
    def test_export_delta_patch_with_binary_guard_and_scripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "GameDelta"
            (root / "data").mkdir(parents=True)
            (root / "Content" / "Paks" / "~mods").mkdir(parents=True)
            (root / "data" / "System.json").write_text('{"gameTitle":"Old"}', encoding="utf-8")
            (root / "Game.exe").write_bytes(b"MZ" + b"\x00" * 200)

            undo = UndoLog.open(root, engine="rpgmaker")
            undo.will_modify("data/System.json")
            (root / "data" / "System.json").write_text('{"gameTitle":"中文标题"}', encoding="utf-8")
            undo.did_write("data/System.json")

            # 新增显式 UE 补丁包（应放行）
            pak_rel = "Content/Paks/~mods/ProjectFerry_zh_CN_P.pak"
            undo.will_add(pak_rel)
            (root / pak_rel).write_bytes(b"PAK_PATCH_DATA")
            undo.did_write(pak_rel)

            # 尝试把修改的 .exe 登记在账本里（应被版权守卫自动拦截）
            undo.will_modify("Game.exe")
            (root / "Game.exe").write_bytes(b"MZ" + b"\x01" * 200)
            undo.did_write("Game.exe")
            undo.finish()

            # 准备译文表
            proj = Project(root=root, game="GameDelta", adapter="rpgmaker", units=[Unit(key="k", src="Old", dst="中文标题")])
            proj.save()

            out_zip = Path(tmp) / "delta_out.zip"
            rep = patchpack.export_patch_pack(root, out_zip=out_zip)
            self.assertTrue(rep.ok)
            self.assertEqual(rep.mode, "delta_patch")
            self.assertTrue(any("Game.exe" in s for s in rep.skipped))

            with zipfile.ZipFile(out_zip) as zf:
                names = set(zf.namelist())
                self.assertIn("manifest.json", names)
                self.assertIn("README_安装说明.txt", names)
                self.assertIn("一键安装补丁.bat", names)
                self.assertIn("一键卸载还原.bat", names)
                self.assertIn("patch/data/System.json", names)
                self.assertIn(f"patch/{pak_rel}", names)
                self.assertNotIn("patch/Game.exe", names)
                self.assertIn("ferry_source/ferry_tl.json", names)
                self.assertIn("ferry_source/ferry_tl.tsv", names)
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
                self.assertEqual(manifest["format"], patchpack.PATCH_FORMAT)
                self.assertEqual(len(manifest["files"]), 2)
                for item in manifest["files"]:
                    raw = zf.read(f"patch/{item['path']}")
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])

    def test_export_copy_dir_diff_and_single_exe_source_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "OrigGame"
            copy_dir = Path(tmp) / "OrigGame.cn"
            (root / "data").mkdir(parents=True)
            (copy_dir / "data").mkdir(parents=True)
            (root / "data" / "same.txt").write_text("unchanged", encoding="utf-8")
            (copy_dir / "data" / "same.txt").write_text("unchanged", encoding="utf-8")
            (root / "data" / "story.txt").write_text("hello", encoding="utf-8")
            (copy_dir / "data" / "story.txt").write_text("你好", encoding="utf-8")

            undo = UndoLog.open(root, engine="tyrano")
            undo.did_copy(copy_dir)
            undo.finish()

            rep = patchpack.export_patch_pack(root, out_zip=Path(tmp) / "copy_diff.zip")
            self.assertTrue(rep.ok)
            self.assertEqual([e.rel_path for e in rep.patch_files], ["data/story.txt"])

            # 单文件整包副本 -> 不塞原版 .exe，仅导出零 Token 文本协作包
            root2 = Path(tmp) / "ExeGame"
            root2.mkdir()
            cn_exe = Path(tmp) / "ExeGame.cn.exe"
            cn_exe.write_bytes(b"MZ_COPY")
            u2 = UndoLog.open(root2, engine="tyrano")
            u2.did_copy(cn_exe)
            u2.finish()
            Project(root=root2, game="ExeGame", adapter="tyrano", units=[Unit(key="1", src="Hi", dst="嗨")]).save()
            rep2 = patchpack.export_patch_pack(root2, out_zip=Path(tmp) / "source_kit.zip")
            self.assertTrue(rep2.ok)
            self.assertEqual(rep2.mode, "source_kit")
            self.assertEqual(rep2.patch_files, [])
            self.assertIn("ferry_tl.json", rep2.source_files)

    def test_export_cn_copy_with_rgss3a_excludes_untouched_media(self):
        """模拟《你好夏洛特》场景：原目录有 Game.rgss3a，副本 .cn 解包出贴图/音频并改了 Data + Fonts。
        无论选原目录还是直接选 .cn 副本目录，导出补丁包都必须过滤未改动的贴图与音频！
        """
        from test_rpgmaker_ace import _pack_v3

        with tempfile.TemporaryDirectory() as tmp:
            orig = Path(tmp) / "Hello Charlotte EP1"
            copy_cn = Path(tmp) / "Hello Charlotte EP1.cn"
            orig.mkdir()
            (copy_cn / "Graphics").mkdir(parents=True)
            (copy_cn / "Audio").mkdir(parents=True)
            (copy_cn / "Data").mkdir(parents=True)
            (copy_cn / "Fonts").mkdir(parents=True)

            raw_png = b"\x89PNG\r\n\x1a\nORIGINAL_ART_BYTES"
            raw_ogg = b"OggS_ORIGINAL_BGM_BYTES"
            raw_map = b"ORIGINAL_MAP_DATA"
            (orig / "Game.rgss3a").write_bytes(
                _pack_v3(
                    {
                        "Graphics/Title.png": raw_png,
                        "Audio/BGM.ogg": raw_ogg,
                        "Data/Map001.rvdata2": raw_map,
                    }
                )
            )
            (orig / "Game.exe").write_bytes(b"MZ_ORIG")

            # .cn 副本中：贴图/音频/Game.exe 都是未改动解包文件，只有 Map001 与 GameCJK.ttf 真正变更/新增
            (copy_cn / "Game.exe").write_bytes(b"MZ_ORIG")
            (copy_cn / "Graphics" / "Title.png").write_bytes(raw_png)
            (copy_cn / "Audio" / "BGM.ogg").write_bytes(raw_ogg)
            (copy_cn / "Data" / "Map001.rvdata2").write_bytes(b"TRANSLATED_MAP_DATA_CN")
            (copy_cn / "Fonts" / "GameCJK.ttf").write_bytes(b"CJK_FONT_BYTES")

            undo = UndoLog.open(orig, engine="rpgmaker_ace")
            undo.did_copy(copy_cn)
            undo.finish()

            # 直接对着 .cn 副本目录点导出（用户最常见操作）
            out_zip = Path(tmp) / "hc_patch.zip"
            rep = patchpack.export_patch_pack(copy_cn, out_zip=out_zip)
            self.assertTrue(rep.ok)
            rels = sorted(e.rel_path for e in rep.patch_files)
            self.assertEqual(rels, ["Data/Map001.rvdata2", "Fonts/GameCJK.ttf"])
            with zipfile.ZipFile(out_zip) as zf:
                readme = zf.read("README_安装说明.txt").decode("utf-8-sig")
                self.assertIn("Game.rgss3a", readme)
                self.assertNotIn("patch/Graphics/Title.png", zf.namelist())
                self.assertNotIn("patch/Audio/BGM.ogg", zf.namelist())


# ==================== P2-A：Unreal Engine 4/5 补强测试 ====================


class UnrealP2Tests(unittest.TestCase):
    def test_normalize_aes_key_hex_and_base64(self):
        raw_bytes = bytes(range(32))
        hex_str = raw_bytes.hex()
        b64_str = base64.b64encode(raw_bytes).decode("ascii")
        expected = "0x" + hex_str.upper()

        self.assertEqual(unreal.normalize_aes_key(""), "")
        self.assertEqual(unreal.normalize_aes_key(hex_str), expected)
        self.assertEqual(unreal.normalize_aes_key("0x" + hex_str.lower()), expected)
        self.assertEqual(unreal.normalize_aes_key(b64_str), expected)
        with self.assertRaises(ValueError):
            unreal.normalize_aes_key("not-a-valid-key")
        with self.assertRaises(ValueError):
            unreal.normalize_aes_key(base64.b64encode(b"short").decode("ascii"))

    def test_repak_cmd_places_aes_key_before_unpack(self):
        key = "0x" + "AB" * 32
        cmd = unreal._build_repak_unpack_cmd(
            Path("repak.exe"), Path("game.pak"), Path("out"), ("**/*.locres",), aes_key=key
        )
        self.assertEqual(cmd[:4], ["repak.exe", "--aes-key", key, "unpack"])

    def test_encrypted_pak_and_iostore_diagnosis_not_hijacked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "UE5EncGame"
            paks = root / "Game" / "Content" / "Paks"
            paks.mkdir(parents=True)
            (root / "Engine").mkdir()
            (paks / "Game-Windows.pak").write_bytes(_make_encrypted_pak_stub(version=11, encrypted_index=True))
            (paks / "Game-Windows.utoc").write_bytes(b"UTOC_STUB")

            with patch.object(deps.Dependency, "locate", return_value=None):
                detected = adapters.detect(root)
                self.assertIsNotNone(detected)
                self.assertEqual(detected.name, "unreal")
                report = detected.probe(root)
                self.assertFalse(report.readable)
                self.assertEqual(report.needs, ["repak"])
                joined = "\n".join(report.notes)
                self.assertIn("AES 加密", joined)
                self.assertIn("IoStore", joined)

    def test_uasset_stringtable_loose_and_pak_roundtrip(self):
        uasset, uexp = _make_sample_uasset_stringtable(
            "UI_MainMenu",
            [("BTN_START", "Start New Adventure"), ("BTN_QUIT", "Exit to Desktop")],
        )
        parsed = unreal.parse_uasset_stringtable(uasset, uexp_blob=uexp)
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed.writable)
        self.assertEqual(parsed.namespace, "UI_MainMenu")
        self.assertEqual(len(parsed.entries), 2)

        new_uasset, new_uexp, count = unreal.rebuild_uasset_stringtable(
            uasset,
            {"ST:0:BTN_START": "开始新的冒险", "ST:1:BTN_QUIT": "退出到桌面"},
            uexp_blob=uexp,
        )
        self.assertEqual(count, 2)
        self.assertIsNotNone(new_uexp)
        re_parsed = unreal.parse_uasset_stringtable(new_uasset, uexp_blob=new_uexp)
        self.assertIsNotNone(re_parsed)
        self.assertTrue(re_parsed.writable)
        self.assertEqual([e.value for e in re_parsed.entries], ["开始新的冒险", "退出到桌面"])

        # 装进 .pak 后由 UnrealAdapter 自动拆包、翻译并生成 ~mods/ProjectFerry_zh_CN_P.pak
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "UESTPak"
            paks = root / "Demo" / "Content" / "Paks"
            paks.mkdir(parents=True)
            base_pak = unreal.build_ue_patch_pak(
                {
                    "Demo/Content/Localization/ST_Menu.uasset": uasset,
                    "Demo/Content/Localization/ST_Menu.uexp": uexp,
                }
            )
            (paks / "Demo-Windows.pak").write_bytes(base_pak)

            adapter = unreal.UnrealAdapter()
            probe = adapter.probe(root)
            self.assertTrue(probe.ready)
            entries = adapter.collect(root)
            self.assertEqual(len(entries), 2)
            ap = adapter.apply(root, {"Start New Adventure": "开启新冒险", "Exit to Desktop": "退出游戏"})
            self.assertEqual(ap.applied, 2)
            patch_pak = paks / "~mods" / unreal.UE_PATCH_PAK_NAME
            self.assertTrue(patch_pak.is_file())
            _mount, extracted = unreal.extract_pak_stringtables(patch_pak)
            self.assertEqual(len(extracted), 1)
            self.assertEqual([e.value for e in extracted[0][4].entries], ["开启新冒险", "退出游戏"])


# ==================== P2-B：高频引擎中文字体自动回退测试 ====================


class FontPatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.cjk_font = Path(cls._tmp.name) / "FakeCJK.ttf"
        # 生成覆盖全部 GB2312 一级汉字的轻量测试 TTF（纯标准库，约几十 KB）
        cps = [ord(ch) for ch in core.gb2312_chars(1)] + list(range(0x20, 0x7F))
        cls.cjk_font.write_bytes(_make_minimal_sfnt(cps, family="FerrySansSC"))
        assert check_font(cls.cjk_font).level == "ok"

        cls.broken_font = Path(cls._tmp.name) / "AsciiOnly.ttf"
        cls.broken_font.write_bytes(_make_minimal_sfnt(list(range(0x20, 0x7F)), family="PixelLatin"))
        assert check_font(cls.broken_font).level == "broken"

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_renpy_fontpatch_and_undo_and_patchpack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "StayRenpy"
            game = root / "game"
            game.mkdir(parents=True)
            orig_script = 'label start:\n    "Hello"\n'
            (game / "script.rpy").write_text(orig_script, encoding="utf-8")

            rep = fontpatch.patch_game_font(root, font_path=self.cjk_font)
            self.assertTrue(rep.ok)
            self.assertEqual(rep.engine, "renpy")
            self.assertTrue((game / "fonts" / "ferry_cjk.ttf").is_file())
            init_rpy = game / "zz_ferry_cjk_font.rpy"
            self.assertTrue(init_rpy.is_file())
            self.assertIn("init 999 python:", init_rpy.read_text(encoding="utf-8"))
            # 原脚本一个字节未改
            self.assertEqual((game / "script.rpy").read_text(encoding="utf-8"), orig_script)

            # 导出纯净补丁包应自动带上字体与回退脚本
            pack_rep = patchpack.export_patch_pack(root, out_zip=Path(tmp) / "renpy_font_patch.zip")
            self.assertTrue(pack_rep.ok)
            self.assertIn("game/fonts/ferry_cjk.ttf", [e.rel_path for e in pack_rep.patch_files])
            self.assertIn("game/zz_ferry_cjk_font.rpy", [e.rel_path for e in pack_rep.patch_files])

            # 一键撤销必须删干净新增文件
            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse((game / "fonts" / "ferry_cjk.ttf").exists())
            self.assertFalse(init_rpy.exists())

    def test_rpgmaker_mv_and_mz_fontpatch_and_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            # MV
            mv = Path(tmp) / "RM_MV"
            (mv / "www" / "data").mkdir(parents=True)
            (mv / "www" / "fonts").mkdir(parents=True)
            (mv / "www" / "data" / "System.json").write_text('{"gameTitle":"MV"}', encoding="utf-8")
            orig_css = '@font-face {\n  font-family: GameFont;\n  src: url("mplus-1m-regular.ttf");\n}\n'
            (mv / "www" / "fonts" / "gamefont.css").write_text(orig_css, encoding="utf-8")

            rep_mv = fontpatch.patch_game_font(mv, font_path=self.cjk_font)
            self.assertTrue(rep_mv.ok)
            self.assertIn('url("ferry_cjk.ttf")', (mv / "www" / "fonts" / "gamefont.css").read_text(encoding="utf-8"))
            self.assertTrue(revert_game(mv).ok)
            self.assertEqual((mv / "www" / "fonts" / "gamefont.css").read_text(encoding="utf-8"), orig_css)

            # MZ
            mz = Path(tmp) / "RM_MZ"
            (mz / "data").mkdir(parents=True)
            orig_sys = json.dumps({"gameTitle": "MZ", "advanced": {"mainFontFilename": "mplus.woff", "fallbackFonts": "Verdana"}})
            (mz / "data" / "System.json").write_text(orig_sys, encoding="utf-8")

            rep_mz = fontpatch.patch_game_font(mz, font_path=self.cjk_font)
            self.assertTrue(rep_mz.ok)
            updated = json.loads((mz / "data" / "System.json").read_text(encoding="utf-8"))
            self.assertEqual(updated["advanced"]["mainFontFilename"], "ferry_cjk.ttf")
            self.assertIn("SimHei", updated["advanced"]["fallbackFonts"])
            self.assertTrue(revert_game(mz).ok)
            self.assertEqual((mz / "data" / "System.json").read_text(encoding="utf-8"), orig_sys)

    def test_kirikiri_godot_and_generic_broken_ttf(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Kirikiri
            kr = Path(tmp) / "KrGame"
            (kr / "system").mkdir(parents=True)
            orig_tjs = ';defaultFace = "MS Gothic";\n;userFace = "MS Gothic";\n'
            (kr / "system" / "Config.tjs").write_text(orig_tjs, encoding="utf-8")
            rep_kr = fontpatch.patch_game_font(kr, font_path=self.cjk_font)
            self.assertTrue(rep_kr.ok)
            self.assertIn('defaultFace = "FerrySansSC"', (kr / "system" / "Config.tjs").read_text(encoding="utf-8-sig"))
            self.assertTrue(revert_game(kr).ok)

            # Godot
            gd = Path(tmp) / "GdGame"
            gd.mkdir()
            (gd / "project.godot").write_text('[application]\nconfig/name="Demo"\n', encoding="utf-8")
            rep_gd = fontpatch.patch_game_font(gd, font_path=self.cjk_font)
            self.assertTrue(rep_gd.ok)
            self.assertIn('theme/custom_font="res://ferry_cjk.ttf"', (gd / "project.godot").read_text(encoding="utf-8"))
            self.assertTrue(revert_game(gd).ok)

            # Generic broken TTF in-place replacement preserving internal family name
            gen = Path(tmp) / "GenericGame"
            (gen / "res").mkdir(parents=True)
            broken_target = gen / "res" / "ui.ttf"
            orig_bytes = self.broken_font.read_bytes()
            broken_target.write_bytes(orig_bytes)
            rep_gen = fontpatch.patch_game_font(gen, engine="generic", font_path=self.cjk_font)
            self.assertTrue(rep_gen.ok)
            cov_after = check_font(broken_target)
            self.assertEqual(cov_after.level, "ok")
            self.assertEqual(cov_after.family, "PixelLatin")
            self.assertTrue(revert_game(gen).ok)
            self.assertEqual(broken_target.read_bytes(), orig_bytes)


if __name__ == "__main__":
    unittest.main()
