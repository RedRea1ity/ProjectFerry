"""大型引擎进阶能力回归测试：外置网游语言接口增量补丁、UnityPy 资产回写、Unreal .pak -> *_P.pak 补丁包、富文本保护。"""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import mc_ai_translator as core
from ferrycore import deps
from ferrycore.adapters import undo as ferryundo
from ferrycore.adapters import unity as ferryunity
from ferrycore.adapters import unreal as ferryunreal


def _make_sample_locres_v2(strings: list[str]) -> bytes:
    """构造标准 UE4/UE5 Optimized v2 .locres 二进制用于离线自测。"""
    # 头部：16B magic + 1B version(2) + 8B string_array_offset(25)
    header = bytearray(ferryunreal.LOCRES_MAGIC)
    header.append(2)
    header.extend(struct.pack("<q", 25))
    arr = bytearray(struct.pack("<I", len(strings)))
    for s in strings:
        arr.extend(ferryunreal.write_fstring(s))
        arr.extend(struct.pack("<i", 1))
    return bytes(header) + bytes(arr)


class RichTextAndLargeEngineTests(unittest.TestCase):
    def test_unity_limbus_rich_text_tags_are_protected_in_placeholders(self):
        src = '<color=#ff0000>Burn</color>: Take <sprite="Status" name="Burn"> {0} <b>damage</b>'
        masked, rep = core.mask_all(src, {}, set())
        # 所有富文本标签与 {0} 都必须被掩码保护，且顺序还原一致
        self.assertNotIn("<color", masked)
        self.assertNotIn("<sprite", masked)
        self.assertNotIn("<b>", masked)
        self.assertEqual(len(rep), 6)
        restored = core.restore_and_validate(src, masked.replace("Burn", "烧伤").replace("Take", "受到").replace("damage", "伤害"), {}, set())
        self.assertEqual(restored, '<color=#ff0000>烧伤</color>: 受到 <sprite="Status" name="Burn"> {0} <b>伤害</b>')

    def test_external_lang_pack_incremental_delta_preserves_human_translations(self):
        """模拟《边狱巴士》*_Data/Lang/ 外置接口：已有零协中文基线时，只增量补翻新增热更新词条。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "LimbusDemo"
            en_dir = root / "LimbusCompany_Data" / "Lang" / "EN"
            zh_dir = root / "LimbusCompany_Data" / "Lang" / "LLC_zh-CN"
            en_dir.mkdir(parents=True)
            zh_dir.mkdir(parents=True)

            en_file = en_dir / "Skills.json"
            en_data = {
                "dataList": [
                    {"id": 1, "name": "Rip Space", "desc": "Deal heavy slash damage"},
                    {"id": 2, "name": "New Hotfix Skill", "desc": "Inflict <color=#f00>Burn</color> on hit"},
                ]
            }
            en_file.write_text(json.dumps(en_data, ensure_ascii=False, indent=2), encoding="utf-8")

            # 已有人工汉化只翻了 id=1，id=2 是热更新新增的英文
            zh_file = zh_dir / "Skills.json"
            zh_data = {
                "dataList": [
                    {"id": 1, "name": "空间撕裂", "desc": "造成大量斩击伤害"},
                ]
            }
            zh_file.write_text(json.dumps(zh_data, ensure_ascii=False, indent=2), encoding="utf-8")

            adapter = ferryunity.UnityAdapter()
            report = adapter.probe(root)
            self.assertTrue(report.ready)
            self.assertEqual(report.container, "external-lang")
            self.assertEqual(report.unit_count, 2, "已有人工汉化的 2 条必须跳过，只收集新增的 2 条")

            entries = adapter.collect(root)
            texts = {e.english for e in entries}
            self.assertEqual(texts, {"New Hotfix Skill", "Inflict <color=#f00>Burn</color> on hit"})

            tr = {
                "New Hotfix Skill": "热更斩击",
                "Inflict <color=#f00>Burn</color> on hit": "命中时施加 <color=#f00>烧伤</color>",
            }
            applied = adapter.apply(root, tr)
            self.assertEqual(applied.applied, 2)

            # 1) 原版 EN JSON 一字节未动
            self.assertEqual(json.loads(en_file.read_text(encoding="utf-8")), en_data)
            # 2) 中文 JSON 既保留了原有人工汉化，又写入了新增热更译文
            merged = json.loads(zh_file.read_text(encoding="utf-8"))
            self.assertEqual(merged["dataList"][0]["name"], "空间撕裂")
            self.assertEqual(merged["dataList"][0]["desc"], "造成大量斩击伤害")
            self.assertEqual(merged["dataList"][1]["name"], "热更斩击")
            self.assertEqual(merged["dataList"][1]["desc"], "命中时施加 <color=#f00>烧伤</color>")

            # 3) 撤销必须无损还原回原人工汉化状态
            rev = ferryundo.revert(root)
            self.assertTrue(rev.ok)
            self.assertEqual(json.loads(zh_file.read_text(encoding="utf-8")), zh_data)

    def test_unitypy_is_available_and_repacks_textasset_and_string_table(self):
        # 构造模拟 UnityPy 环境，验证 TextAsset JSON 与 Localization StringTable 的提取与重打包路径
        # （CI 跑纯标准库环境，不强依赖系统已安装第三方 UnityPy 包）
        fake_text_data = MagicMock()
        fake_text_data.m_Script = json.dumps({"title": "Start New Adventure"}, ensure_ascii=False)
        fake_text_obj = MagicMock()
        fake_text_obj.type.name = "TextAsset"
        fake_text_obj.path_id = 101
        fake_text_obj.read.return_value = fake_text_data

        table_tree = {"m_TableData": [{"m_Id": 1, "m_Localized": "Continue Saved Game"}]}
        fake_table_obj = MagicMock()
        fake_table_obj.type.name = "MonoBehaviour"
        fake_table_obj.path_id = 202
        fake_table_obj.read_typetree.return_value = table_tree

        fake_env = MagicMock()
        fake_env.objects = [fake_text_obj, fake_table_obj]
        fake_env.file.save.return_value = b"REPACKED_UNITY_ASSET_BYTES"

        fake_unitypy = MagicMock()
        fake_unitypy.load.return_value = fake_env

        with tempfile.TemporaryDirectory() as tmp:
            asset = Path(tmp) / "resources.assets"
            asset.write_bytes(b"\x00" * 128)
            with patch.object(ferryunity, "HAS_UNITYPY", True), patch.object(ferryunity, "UnityPy", fake_unitypy):
                entries = ferryunity._extract_unitypy_entries(asset, "Game_Data/resources.assets", "Demo")
                self.assertEqual({e.english for e in entries}, {"Start New Adventure", "Continue Saved Game"})

                repacked, ok_n, miss_n, errs = ferryunity._rebuild_unitypy_asset(
                    asset,
                    entries,
                    {"Start New Adventure": "开始新冒险", "Continue Saved Game": "继续存档"},
                )
        self.assertFalse(errs)
        self.assertEqual((ok_n, miss_n), (2, 0))
        self.assertEqual(repacked, b"REPACKED_UNITY_ASSET_BYTES")
        self.assertEqual(json.loads(fake_text_data.m_Script)["title"], "开始新冒险")
        self.assertEqual(table_tree["m_TableData"][0]["m_Localized"], "继续存档")

    def test_unreal_pak_auto_extract_and_high_priority_patch_pak_roundtrip(self):
        """验证虚幻 .pak 免工具直接拆出 .locres，并打成高优先级 ~mods/*_P.pak 补丁包（不改原 pak）。"""
        self.assertIn("repak", deps.REGISTRY)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "UnrealDemo"
            paks_dir = root / "DemoGame" / "Content" / "Paks"
            paks_dir.mkdir(parents=True)
            (root / "Engine").mkdir()

            locres_raw = _make_sample_locres_v2(["Press Start Button", "Settings Menu"])
            base_pak_bytes = ferryunreal.build_ue_patch_pak(
                {"DemoGame/Content/Localization/Game/en/Game.locres": locres_raw},
                mount_point="../../../",
            )
            base_pak = paks_dir / "DemoGame-WindowsNoEditor.pak"
            base_pak.write_bytes(base_pak_bytes)

            adapter = ferryunreal.UnrealAdapter()
            probe = adapter.probe(root)
            self.assertTrue(probe.ready)
            self.assertEqual(probe.container, "pak -> *_P.pak")
            self.assertEqual(probe.unit_count, 2)

            entries = adapter.collect(root)
            self.assertEqual({e.english for e in entries}, {"Press Start Button", "Settings Menu"})

            applied = adapter.apply(root, {"Press Start Button": "按下开始按钮", "Settings Menu": "设置菜单"})
            self.assertEqual(applied.applied, 2)

            # 1) 原版基础 .pak 一字节未改
            self.assertEqual(base_pak.read_bytes(), base_pak_bytes)
            # 2) 生成了高优先级 ~mods/ProjectFerry_zh_CN_P.pak，且能反解出精确中文
            patch_pak = paks_dir / "~mods" / ferryunreal.UE_PATCH_PAK_NAME
            self.assertTrue(patch_pak.is_file())
            mount, extracted = ferryunreal.extract_pak_locres(patch_pak)
            self.assertEqual(mount, "../../../")
            self.assertIn("DemoGame/Content/Localization/Game/en/Game.locres", extracted)
            parsed = ferryunreal.parse_locres(extracted["DemoGame/Content/Localization/Game/en/Game.locres"])
            self.assertEqual([item.text for item in parsed.strings], ["按下开始按钮", "设置菜单"])

            # 3) 撤销直接删掉补丁包
            rev = ferryundo.revert(root)
            self.assertTrue(rev.ok)
            self.assertFalse(patch_pak.exists())


if __name__ == "__main__":
    unittest.main()
