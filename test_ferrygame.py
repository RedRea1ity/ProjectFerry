"""通用小众游戏线（ferrygame）回归测试。

全部在临时目录里造字节，不联网、不碰本机任何真实游戏或配置文件。
"""

from __future__ import annotations

import json
import struct
import tempfile
import unittest
from pathlib import Path

import ferrygame
from ferrycore.adapters.undo import revert as revert_game
from ferrygame import (
    PC98Adapter,
    PC98FontMapper,
    Project,
    RawBinaryAdapter,
    Unit,
    apply_project,
    classify,
    controls,
    detect_source_lang,
    encode_padded,
    inspect_game,
    load,
    merge,
    patch_blob,
    run_game,
    scan_game,
    scan_sjis_strings,
    translate_project,
    translate_units,
)


def _make_fat16_hdi(files: dict[str, bytes]) -> bytes:
    """在内存里造一个带 4096 字节 Anex86 头的最小 FAT16 .hdi 磁盘镜像。"""
    header_size = 4096
    bps = 512
    spc = 1
    reserved = 1
    fats = 1
    spf = 16
    root_entries = 64
    total_sectors = 5000  # >4085 簇 → 标准 FAT16

    img = bytearray(header_size + total_sectors * bps)
    bpb = header_size
    img[bpb : bpb + 3] = b"\xEB\x3C\x90"
    struct.pack_into("<H", img, bpb + 11, bps)
    img[bpb + 13] = spc
    struct.pack_into("<H", img, bpb + 14, reserved)
    img[bpb + 16] = fats
    struct.pack_into("<H", img, bpb + 17, root_entries)
    struct.pack_into("<H", img, bpb + 19, total_sectors)
    struct.pack_into("<H", img, bpb + 22, spf)

    fat_off = bpb + reserved * bps
    root_off = fat_off + fats * spf * bps
    root_sectors = (root_entries * 32 + bps - 1) // bps
    data_off = root_off + root_sectors * bps

    next_cluster = 2
    dir_idx = 0
    for name, content in files.items():
        stem, _, ext = name.upper().partition(".")
        raw_name = stem.encode("ascii")[:8].ljust(8, b" ")
        raw_ext = ext.encode("ascii")[:3].ljust(3, b" ")
        first_cluster = next_cluster
        needed = max(1, (len(content) + bps - 1) // bps)
        for i in range(needed):
            c = first_cluster + i
            nxt = (c + 1) if (i + 1 < needed) else 0xFFFF
            struct.pack_into("<H", img, fat_off + c * 2, nxt)
            c_off = data_off + (c - 2) * bps
            chunk = content[i * bps : (i + 1) * bps]
            img[c_off : c_off + len(chunk)] = chunk
        next_cluster += needed

        ent = root_off + dir_idx * 32
        img[ent : ent + 8] = raw_name
        img[ent + 8 : ent + 11] = raw_ext
        img[ent + 11] = 0x20
        struct.pack_into("<H", img, ent + 26, first_cluster)
        struct.pack_into("<I", img, ent + 28, len(content))
        dir_idx += 1

    return bytes(img)


class ControlsAndUnitsTests(unittest.TestCase):
    def test_masks_rpgmaker_renpy_tyrano_and_printf_tags(self):
        src = r"\C[2]\N[1]\C[0]：「こんにちは[r]{w=0.5}、%1$sさん！」"
        masked, codes = controls.mask(src)
        self.assertEqual(len(codes), 6)
        self.assertNotIn(r"\C[2]", masked)
        self.assertNotIn("[r]", masked)
        self.assertNotIn("{w=0.5}", masked)
        self.assertNotIn("%1$s", masked)

        translated = "__T0____T1____T2__：「你好__T3____T4__，__T5__先生！」"
        restored = controls.unmask(src, translated, codes)
        self.assertEqual(restored, r"\C[2]\N[1]\C[0]：「你好[r]{w=0.5}，%1$s先生！」")

        # 漏掉一个控制符必须整句作废
        self.assertIsNone(controls.unmask(src, "__T0____T1__：「你好」", codes))

    def test_classify_story_vs_menu_and_detect_lang(self):
        self.assertEqual(classify("「陛下、我が軍の兵糧がもう持ちません！」"), "story")
        self.assertEqual(classify("コンフィグ"), "menu")
        self.assertEqual(classify("Save Game"), "menu")
        self.assertEqual(classify("Long ago, in a kingdom far beyond the northern mountains..."), "story")
        self.assertEqual(detect_source_lang(["はじめから", "つづきから"]), "ja")
        self.assertEqual(detect_source_lang(["Start Game", "Options"]), "en")


class TranslateContextAndBudgetTests(unittest.TestCase):
    def test_rolling_context_and_byte_budget_retry(self):
        units = [
            Unit(key="s#1", src="「アリス、行くぞ！」", kind="story", file="a.mes", order=1, max_bytes=20, encoding="gbk"),
            Unit(key="s#2", src="「うん、わかった！」", kind="story", file="a.mes", order=2, max_bytes=20, encoding="gbk"),
            Unit(key="m#1", src="はじめから", kind="menu", file="a.mes", order=3, max_bytes=8, encoding="gbk"),
        ]
        seen_inputs: list[dict] = []

        def fake_call(engine, masked, settings):
            payload = json.loads(settings["model_input_builder"](masked, settings))
            seen_inputs.append(payload)
            # 确认没有 MC 的苦力怕提示词混进来
            self.assertNotIn("Minecraft", settings["system_prompt_override"])
            out = {}
            for sid, info in payload["strings"].items():
                if info["kind"] == "menu":
                    # 第一次故意返回超长的（12 字节 > 8 字节），触发自动重试缩短
                    if "too_long" not in info:
                        out[sid] = "从头开始新的游戏"
                    else:
                        out[sid] = "开始游戏"
                elif "アリス" in info["text"]:
                    out[sid] = "「爱丽丝，走吧！」"
                else:
                    out[sid] = "「嗯，明白了！」"
            return out

        with tempfile.TemporaryDirectory() as tmp:
            report = translate_units(
                units,
                settings={"engine": "openai", "batch_size": 1},
                source_lang="ja",
                glossary={"アリス": "爱丽丝"},
                call=fake_call,
                cache_dir=Path(tmp) / "cache",
            )
            self.assertEqual(report.translated, 3)
            self.assertEqual(report.too_long, 0)
            self.assertEqual(units[0].dst, "「爱丽丝，走吧！」")
            self.assertEqual(units[1].dst, "「嗯，明白了！」")
            self.assertEqual(units[2].dst, "开始游戏")
            # 第 2 批必须收到第 1 批的上下文
            self.assertEqual(seen_inputs[1]["context"], [{"src": "「アリス、行くぞ！」", "zh": "「爱丽丝，走吧！」"}])
            # 第 3 批触发了超长重试，所以一共调了 4 次
            self.assertEqual(len(seen_inputs), 4)


class RawBinaryAdapterTests(unittest.TestCase):
    def _build_sample_binary(self) -> bytes:
        # 模拟一个奇葩单 EXE / .dat：头尾都有固定指针和魔数，中间夹着 Shift-JIS 台词和菜单
        parts = [
            b"WEIRD_ENGINE_V1\x00\x12\x34\x56\x78",
            "chara/alice_normal.png".encode("ascii") + b"\x00",  # 路径噪音，不该被抽
            "「おはようございます、先輩！今日もいい天気ですね。」".encode("cp932") + b"\x00",
            b"\xFF\x00\xAA\x55",
            "「ああ、そうだな。そろそろ出発しようか。」".encode("cp932") + b"\x00",
            b"\x01\x02\x03\x04",
            "はじめから".encode("cp932") + b"\x00",
            "つづきから".encode("cp932") + b"\x00",
            "コンフィグ".encode("cp932") + b"\x00",
            b"TAIL_MARKER_OK\x00",
        ]
        return b"".join(parts)

    def test_rawbin_extracts_story_and_menu_and_patches_fixed_length(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dat = root / "scenario.dat"
            orig_blob = self._build_sample_binary()
            dat.write_bytes(orig_blob)

            project, probe = scan_game(root)
            self.assertTrue(probe.readable)
            self.assertTrue(probe.writable)
            self.assertEqual(project.adapter, "rawbin")
            srcs = [u.src for u in project.units]
            self.assertNotIn("chara/alice_normal.png", srcs)
            self.assertIn("はじめから", srcs)
            self.assertIn("「おはようございます、先輩！今日もいい天気ですね。」", srcs)

            # 模拟玩家直接编辑 ferry_tl.json 并写回
            tl_file = root / "ferry_tl.json"
            self.assertTrue(tl_file.is_file())
            data = json.loads(tl_file.read_text(encoding="utf-8"))
            translations = {
                "「おはようございます、先輩！今日もいい天気ですね。」": "「早上好，前辈！今天天气真不错呢。」",
                "「ああ、そうだな。そろそろ出発しようか。」": "「是啊。那我们差不多该出发了吧。」",
                "はじめから": "开始游戏",
                "つづきから": "继续游戏",
                "コンフィグ": "系统设置",
            }
            for item in data["units"]:
                item["dst"] = translations[item["src"]]
            tl_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

            loaded = load(root)
            self.assertIsNotNone(loaded)
            ap = apply_project(loaded)
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 5)

            new_blob = dat.read_bytes()
            # 铁律 1：总字节长度一字节不差，头尾魔数和中间控制字节纹丝不动
            self.assertEqual(len(new_blob), len(orig_blob))
            self.assertTrue(new_blob.startswith(b"WEIRD_ENGINE_V1\x00\x12\x34\x56\x78"))
            self.assertTrue(new_blob.endswith(b"TAIL_MARKER_OK\x00"))
            self.assertIn("「早上好，前辈！今天天气真不错呢。」".encode("gbk"), new_blob)
            self.assertIn("开始游戏".encode("gbk"), new_blob)

            # 铁律 2：一键 revert 后字节 100% 还原
            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertEqual(dat.read_bytes(), orig_blob)

    def test_length_prefixed_lcf_records_do_not_swallow_field_ids(self):
        # 真实 RPG_RT.ldb 字节布局：[field_id][len][SJIS]，其中 field_id 0x34='4'、len 0x0a='\n'
        s1 = "は倒れた！".encode("cp932")
        s2 = "を倒した！".encode("cp932")
        s3 = "は立ち上がった！".encode("cp932")
        blob = (
            b"\x01\x14\x33" + bytes([len(s1)]) + s1
            + b"\x34" + bytes([len(s2)]) + s2
            + b"\x37" + bytes([len(s3)]) + s3
            + b"\x00"
        )
        units = scan_sjis_strings(blob, rel_path="RPG_RT.ldb")
        self.assertEqual([u.src for u in units], ["は倒れた！", "を倒した！", "は立ち上がった！"])
        self.assertEqual([u.max_bytes for u in units], [10, 10, 16])
        for u, zh in zip(units, ["倒下了！", "被击败了！", "站起来了！"]):
            u.dst = zh
        patched, count, conflicts = patch_blob(blob, units)
        self.assertEqual(count, 3)
        self.assertEqual(conflicts, [])
        self.assertEqual(len(patched), len(blob))
        # 字段头 0x33 0x0a / 0x34 0x0a / 0x37 0x10 一字节都没被碰
        self.assertEqual(patched[2:4], b"\x33\x0a")
        self.assertEqual(patched[14:16], b"\x34\x0a")
        self.assertEqual(patched[26:28], b"\x37\x10")


class PC98AdapterTests(unittest.TestCase):
    def test_pc98_hdi_fat16_scan_and_font_remap_apply(self):
        script = b"".join([
            b"\x00\x10",
            "「ここは一体どこなんだ……？誰もいないのか？」".encode("cp932") + b"\x00",
            "「気をつけて！この遺跡には罠が仕掛けられているわ。」".encode("cp932") + b"\x00",
            "はじめから".encode("cp932") + b"\x00",
            "ロード".encode("cp932") + b"\x00",
        ])
        hdi_bytes = _make_fat16_hdi({"MAIN.MES": script})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hdi_path = root / "game98.hdi"
            hdi_path.write_bytes(hdi_bytes)

            # 侦查台能直接认出 PC-98 镜像里的 FAT 文件
            recon = inspect_game(root)
            self.assertIn("game98.hdi::MAIN.MES", recon.pc98_files)

            def fake_call(engine, masked, settings):
                return {
                    "1": "「这里到底是哪里……？没有人吗？」",
                    "2": "「小心！这座遗迹里设有陷阱。」",
                    "3": "开始",
                    "4": "读档",
                }

            res = run_game(
                root,
                settings={"engine": "openai", "batch_size": 10},
                call=fake_call,
                cache_dir=root / "cache",
            )
            self.assertTrue(res.ok)
            self.assertEqual(res.applied.applied, 4)
            self.assertIsNotNone(res.applied.out_path)
            self.assertTrue(res.applied.out_path.is_file())
            # 镜像副本长度与原镜像一模一样
            self.assertEqual(res.applied.out_path.stat().st_size, len(hdi_bytes))
            # 产出了 pc98_charmap.json，且记录了 cp932 缺失的简体字（如「这」「里」「吗」）
            charmap_file = root / "pc98_charmap.json"
            self.assertTrue(charmap_file.is_file())
            charmap = json.loads(charmap_file.read_text(encoding="utf-8"))
            self.assertIn("这", charmap)

    def test_anex86_font_bmp_patching_and_jis_coordinates(self):
        from ferrygame.pc98 import patch_pc98_font_bmp, sjis_to_jis

        # 实测核对过的三个坐标：亜=(0x30,0x21)、前=(0x41,0x30)、弌=(0x50,0x21)
        self.assertEqual(sjis_to_jis(*"亜".encode("cp932")), (0x30, 0x21))
        self.assertEqual(sjis_to_jis(*"前".encode("cp932")), (0x41, 0x30))
        self.assertEqual(sjis_to_jis(*"弌".encode("cp932")), (0x50, 0x21))

        # 构造一张标准 2048x2048 1-bit BMP（524,350 字节，全 0xFF 空白背景）
        hdr = bytearray(62)
        hdr[0:2] = b"BM"
        struct.pack_into("<I", hdr, 2, 524350)
        struct.pack_into("<I", hdr, 10, 62)
        struct.pack_into("<I", hdr, 14, 40)
        struct.pack_into("<iiHH", hdr, 18, 2048, 2048, 1, 1)
        struct.pack_into("<I", hdr, 34, 524288)
        hdr[58:62] = b"\xff\xff\xff\x00"
        blank_bmp = bytes(hdr) + (b"\xff" * 524288)

        patched = patch_pc98_font_bmp(blank_bmp, {"这": "姨"})
        self.assertEqual(len(patched), 524350)
        self.assertNotEqual(patched, blank_bmp)
        # 确认只有「姨」对应的 16x16 格子被写入了非 0xFFFF 的前景像素
        s1, s2 = "姨".encode("cp932")
        j1, j2 = sjis_to_jis(s1, s2)
        cx, cy = j1 - 0x20, j2
        cell_words = []
        for dy in range(16):
            pos = 62 + (2047 - (cy * 16 + dy)) * 256 + cx * 2
            cell_words.append(struct.unpack_from(">H", patched, pos)[0])
        self.assertTrue(any(w != 0xFFFF for w in cell_words))

    def test_pc98_lodsw_word_alignment_prevents_odd_byte_shift(self):
        from ferrygame.pc98 import PC98FontMapper, align_pc98_words

        # 大模型常把双空格缩成单空格、把全角字母数字翻成半角奇数串，在 PC-98 LODSW 下会导致奇偶错位乱码
        raw = "年B$c1月C$c1日 体力D$c1I$c1 信徒数E$c1^m 店F$c1 设备G$c1^m 金H$c1万日元^m"
        mapper = PC98FontMapper()
        encoded = mapper.encode_padded(raw, max_bytes=76, pad="nul")
        self.assertIsNotNone(encoded)
        self.assertEqual(len(encoded), 76)
        # B$ / C$ / D$ / I$ / E$ / F$ / G$ / H$ 必须全部落在偶数偏移上
        for var in (b"B$", b"C$", b"D$", b"I$", b"E$", b"F$", b"G$", b"H$"):
            pos = encoded.index(var)
            self.assertEqual(pos % 2, 0, f"{var!r} 落在了奇数偏移 {pos}")
        self.assertEqual(align_pc98_words("Q热"), "Ｑ热")


if __name__ == "__main__":
    unittest.main()
