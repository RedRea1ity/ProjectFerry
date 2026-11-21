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


def _build_minimal_asar(files: dict[str, bytes]) -> bytes:
    """构造一个符合 Chromium Pickle 规范的最小 Electron .asar 归档。"""
    tree: dict = {"files": {}}
    data_chunks: list[bytes] = []
    offset = 0
    for rel_path, raw in files.items():
        parts = [p for p in rel_path.split("/") if p]
        node = tree
        for d in parts[:-1]:
            node = node["files"].setdefault(d, {"files": {}})
        node["files"][parts[-1]] = {"offset": str(offset), "size": len(raw)}
        data_chunks.append(raw)
        offset += len(raw)

    json_bytes = json.dumps(tree, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    str_len = len(json_bytes)
    padded_str = json_bytes + (b"\x00" * ((4 - (str_len % 4)) % 4))
    inner_payload_size = 4 + len(padded_str)
    header_total_size = 8 + len(padded_str)
    header = struct.pack("<IIII", 4, header_total_size, inner_payload_size, str_len) + padded_str
    return header + b"".join(data_chunks)


def _encode_ber(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    out = bytearray()
    out.append(value & 0x7F)
    value >>= 7
    while value > 0:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.reverse()
    return bytes(out)


def _build_minimal_gdpc_v2(files: dict[str, bytes]) -> bytes:
    """构造一个最小 Godot 4.x GDPC v2 .pck 归档。"""
    import hashlib

    header_pad = b"\x00" * (16 * 4)
    entries_meta: list[tuple[bytes, int, int, bytes]] = []
    data_blobs: list[bytes] = []
    cur_rel_off = 0
    for path_str, raw in files.items():
        p_bytes = path_str.encode("utf-8")
        pad = (4 - (len(p_bytes) % 4)) % 4
        p_bytes += b"\x00" * pad
        md5 = hashlib.md5(raw).digest()
        entries_meta.append((p_bytes, cur_rel_off, len(raw), md5))
        data_blobs.append(raw)
        cur_rel_off += len(raw)

    index_buf = bytearray()
    index_buf.extend(struct.pack("<I", len(entries_meta)))
    for p_bytes, rel_off, size, md5 in entries_meta:
        index_buf.extend(struct.pack("<I", len(p_bytes)))
        index_buf.extend(p_bytes)
        index_buf.extend(struct.pack("<QQ", rel_off, size))
        index_buf.extend(md5)
        index_buf.extend(struct.pack("<I", 0))

    header_len = 4 + 4 + 12 + 4 + 8 + 64
    file_base = (header_len + len(index_buf) + 7) & ~7
    pad_before_data = file_base - (header_len + len(index_buf))
    # pack_flags = 2 (PACK_REL_FILEBASE)
    head = (
        b"GDPC"
        + struct.pack("<IIIIIQ", 2, 4, 2, 0, 2, file_base)
        + header_pad
    )
    return head + bytes(index_buf) + (b"\x00" * pad_before_data) + b"".join(data_blobs)


def _build_minimal_dotnet_dll(user_strings: list[str]) -> bytes:
    """构造一个含 ECMA-335 BSJB 元数据头与 #US UTF-16LE 字符串流的最小伪 DLL。"""
    us_stream = bytearray(b"\x00")
    for s in user_strings:
        raw = s.encode("utf-16le") + b"\x01"
        length = len(raw)
        if length < 0x80:
            us_stream.append(length)
        else:
            us_stream.extend([0x80 | (length >> 8), length & 0xFF])
        us_stream.extend(raw)
    while len(us_stream) % 4 != 0:
        us_stream.append(0)

    version = b"v4.0.30319\x00\x00"  # 12 bytes (4B aligned)
    stream_name = b"#US\x00"  # 4 bytes
    header_size = 16 + len(version) + 4 + (8 + len(stream_name))
    bsjb = (
        b"BSJB"
        + struct.pack("<HHII", 1, 1, 0, len(version))
        + version
        + struct.pack("<HH", 0, 1)
        + struct.pack("<II", header_size, len(us_stream))
        + stream_name
        + bytes(us_stream)
    )
    return b"MZ" + (b"\x00" * 126) + bsjb


class MainstreamEngineAdapterTests(unittest.TestCase):
    def test_asar_container_roundtrip_with_tyrano(self):
        from ferrycore.adapters import detect, parse_asar, rebuild_asar

        ks_content = "[tb_start_text]\n「先輩、今日も一緒に帰りましょう！」[p]\n[_tb_end_text]\n".encode("utf-8")
        asar_blob = _build_minimal_asar({"data/scenario/first.ks": ks_content})
        _tree, _base, entries = parse_asar(asar_blob)
        self.assertIn("data/scenario/first.ks", entries)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            res_dir = root / "resources"
            res_dir.mkdir()
            asar_path = res_dir / "app.asar"
            asar_path.write_bytes(asar_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "tyrano")
            collected = adapter.collect(root)
            self.assertEqual(len(collected), 1)
            self.assertIn("先輩、今日も一緒に帰りましょう！", collected[0].english)

            ap = adapter.apply(root, {collected[0].key: "「前辈，今天也一起回家吧！」[p]"})
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 1)
            self.assertIsNotNone(ap.out_path)

            out_asar = ap.out_path / "resources" / "app.asar"
            _tree2, _base2, entries_after = parse_asar(out_asar.read_bytes())
            off, sz = entries_after["data/scenario/first.ks"]
            new_ks = out_asar.read_bytes()[off : off + sz].decode("utf-8")
            self.assertIn("「前辈，今天也一起回家吧！」[p]", new_ks)

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse(ap.out_path.exists())

    def test_kirikiri_xp3_extract_and_patch_xp3_writeback(self):
        from ferrycore.adapters import detect
        from ferrycore.adapters.kirikiri import build_xp3, parse_xp3, read_xp3_file

        ks_raw = (
            "*start|スタート画面\n"
            "[cm]\n"
            "「おはようございます、マスター！」[p]\n"
            '@link target="*next" text="次へ進む"\n'
        ).encode("cp932")
        xp3_blob = build_xp3({"scenario/first.ks": ks_raw})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data.xp3").write_bytes(xp3_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "kirikiri")
            units = adapter.collect(root)
            texts = [u.english for u in units]
            self.assertIn("スタート画面", texts)
            self.assertIn("「おはようございます、マスター！」[p]", texts)
            self.assertIn("次へ進む", texts)

            ap = adapter.apply(
                root,
                {
                    "スタート画面": "开始画面",
                    "「おはようございます、マスター！」[p]": "「早上好，御主！」[p]",
                    "次へ進む": "继续前进",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 3)
            patch_xp3 = root / "patch.xp3"
            self.assertTrue(patch_xp3.is_file())

            # 验证 patch.xp3 内写入了 UTF-16LE 带 BOM 的 .ks 剧本
            p_blob = patch_xp3.read_bytes()
            p_entries = {e.name: e for e in parse_xp3(p_blob)}
            self.assertIn("scenario/first.ks", p_entries)
            patched_ks = read_xp3_file(p_blob, p_entries["scenario/first.ks"])
            self.assertTrue(patched_ks.startswith(b"\xff\xfe"))
            decoded_ks = patched_ks.decode("utf-16")
            self.assertIn("*start|开始画面", decoded_ks)
            self.assertIn("「早上好，御主！」[p]", decoded_ks)
            self.assertIn('text="继续前进"', decoded_ks)

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse(patch_xp3.exists())

    def test_rpgmaker_2k_lcf_chunk_isolation_and_patch(self):
        from ferrycore.adapters import detect

        # 构造 RPG_RT.ldb：Chunk 11 (Actors, 白名单内) + Chunk 19 (Chipsets, 必须跳过不翻!)
        actor_str = "アレックス".encode("cp932")
        c11_body = b"\x01\x01\x01" + bytes([len(actor_str)]) + actor_str + b"\x00"
        chipset_str = "基本チップセット".encode("cp932")
        c19_body = b"\x01\x01\x01" + bytes([len(chipset_str)]) + chipset_str + b"\x00"
        ldb_blob = (
            b"\x0bLcfDataBase"
            + _encode_ber(11) + _encode_ber(len(c11_body)) + c11_body
            + _encode_ber(19) + _encode_ber(len(c19_body)) + c19_body
            + b"\x00"
        )

        # 构造 Map0001.lmu：Chunk 71 (底层图块数据) + Chunk 81 (地图事件台词)
        c71_body = b"\x00\x01\x02\x03\x04\x05\x06\x07"
        ev_str = "「ようこそ、辺境の村へ！」".encode("cp932")
        c81_body = b"\x01\x05" + bytes([len(ev_str)]) + ev_str + b"\x00"
        lmu_blob = (
            b"\x0aLcfMapUnit"
            + _encode_ber(71) + _encode_ber(len(c71_body)) + c71_body
            + _encode_ber(81) + _encode_ber(len(c81_body)) + c81_body
            + b"\x00"
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "RPG_RT.ldb").write_bytes(ldb_blob)
            (root / "Map0001.lmu").write_bytes(lmu_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "rpgmaker_2k")
            units = adapter.collect(root)
            texts = [u.english for u in units]
            self.assertIn("アレックス", texts)
            self.assertIn("「ようこそ、辺境の村へ！」", texts)
            # Chunk 19 (Chipsets) 绝不能混入翻译列表
            self.assertNotIn("基本チップセット", texts)

            ap = adapter.apply(
                root,
                {
                    "アレックス": "亚历克斯",
                    "「ようこそ、辺境の村へ！」": "「欢迎来到边境之村！」",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)
            # 字节总长与 BER 块结构保持 100% 不变
            self.assertEqual(len((root / "RPG_RT.ldb").read_bytes()), len(ldb_blob))
            self.assertEqual(len((root / "Map0001.lmu").read_bytes()), len(lmu_blob))
            self.assertIn("亚历克斯".encode("gbk"), (root / "RPG_RT.ldb").read_bytes())
            self.assertIn("「欢迎来到边境之村！」".encode("gbk"), (root / "Map0001.lmu").read_bytes())

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertEqual((root / "RPG_RT.ldb").read_bytes(), ldb_blob)
            self.assertEqual((root / "Map0001.lmu").read_bytes(), lmu_blob)

    def test_godot_pck_and_tscn_roundtrip(self):
        from ferrycore.adapters import detect
        from ferrycore.adapters.godot import parse_pck

        tscn_raw = (
            '[gd_scene format=3]\n'
            '[node name="Title" type="Label"]\n'
            'text = "魔王城への道"\n'
            'placeholder_text = "名前を入力してください"\n'
            'texture_path = "res://assets/title.png"\n'
        ).encode("utf-8")
        pck_blob = _build_minimal_gdpc_v2({"res://scenes/main.tscn": tscn_raw})

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pck_path = root / "game.pck"
            pck_path.write_bytes(pck_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "godot")
            units = adapter.collect(root)
            texts = [u.english for u in units]
            self.assertEqual(texts, ["魔王城への道", "名前を入力してください"])

            ap = adapter.apply(
                root,
                {
                    "魔王城への道": "通往魔王城之路",
                    "名前を入力してください": "请输入名字",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)

            arc = parse_pck(pck_path.read_bytes())
            self.assertEqual(arc.version, 2)
            ent = {e.path: e for e in arc.entries}["res://scenes/main.tscn"]
            patched_tscn = pck_path.read_bytes()[ent.offset : ent.offset + ent.size].decode("utf-8")
            self.assertIn('text = "通往魔王城之路"', patched_tscn)
            self.assertIn('placeholder_text = "请输入名字"', patched_tscn)

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertEqual(pck_path.read_bytes(), pck_blob)

    def test_unity_streaming_assets_and_dotnet_us_to_xunity(self):
        from ferrycore.adapters import detect

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "SampleGame_Data"
            sa_dir = data_dir / "StreamingAssets"
            managed_dir = data_dir / "Managed"
            sa_dir.mkdir(parents=True)
            managed_dir.mkdir(parents=True)

            lang_json = sa_dir / "ui.json"
            lang_json.write_text(
                json.dumps(
                    {
                        "title": "星屑のクロニクル",
                        "sprite": "Assets/Sprites/logo.png",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            dll_path = managed_dir / "Assembly-CSharp.dll"
            dll_path.write_bytes(
                _build_minimal_dotnet_dll(
                    [
                        "PlayerController",
                        "Assets/Prefabs/Hero.prefab",
                        "「本当にこの扉を開けますか？」",
                    ]
                )
            )

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "unity")
            units = adapter.collect(root)
            texts = [u.english for u in units]
            self.assertIn("星屑のクロニクル", texts)
            self.assertIn("「本当にこの扉を開けますか？」", texts)
            self.assertNotIn("PlayerController", texts)
            self.assertNotIn("Assets/Sprites/logo.png", texts)

            ap = adapter.apply(
                root,
                {
                    "星屑のクロニクル": "星尘编年史",
                    "「本当にこの扉を開けますか？」": "「真的要打开这扇门吗？」",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)

            # StreamingAssets JSON 已原地更新
            self.assertEqual(json.loads(lang_json.read_text(encoding="utf-8"))["title"], "星尘编年史")
            # XUnity 预翻译表与配置已生成
            pre_txt = root / "BepInEx" / "Translation" / "zh" / "Text" / "_PreTranslated.txt"
            self.assertTrue(pre_txt.is_file())
            pre_content = pre_txt.read_text(encoding="utf-8")
            self.assertIn("「本当にこの扉を開けますか？」=「真的要打开这扇门吗？」", pre_content)

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertEqual(json.loads(lang_json.read_text(encoding="utf-8"))["title"], "星屑のクロニクル")
            self.assertFalse(pre_txt.exists())

    def test_i18n_table_po_extract_and_apply(self):
        from ferrycore.adapters import detect

        po_content = (
            'msgid ""\n'
            'msgstr ""\n'
            '"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
            '#: src/ui.cpp:12\n'
            'msgid "Start New Game"\n'
            'msgstr ""\n\n'
            '#: src/story.cpp:44\n'
            'msgid "Welcome to the Underground Labyrinth!"\n'
            'msgstr ""\n'
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loc_dir = root / "locales"
            loc_dir.mkdir()
            po_path = loc_dir / "messages.po"
            po_path.write_text(po_content, encoding="utf-8")

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "i18n_table")
            units = adapter.collect(root)
            self.assertEqual([u.english for u in units], ["Start New Game", "Welcome to the Underground Labyrinth!"])

            ap = adapter.apply(
                root,
                {
                    "Start New Game": "开始新游戏",
                    "Welcome to the Underground Labyrinth!": "欢迎来到地下迷宫！",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)
            updated = po_path.read_text(encoding="utf-8")
            self.assertIn('msgstr "开始新游戏"', updated)
            self.assertIn('msgstr "欢迎来到地下迷宫！"', updated)

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertEqual(po_path.read_text(encoding="utf-8"), po_content)


def _build_minimal_dex(strings: list[str]) -> bytes:
    """构造一个含 string_ids 与 string_data_item 的最小合法 Android classes.dex。"""
    import hashlib
    import zlib

    # Header 0x70 bytes, string_ids at 0x70, string_data after string_ids
    n = len(strings)
    string_ids_off = 0x70
    data_start = string_ids_off + n * 4
    string_ids = bytearray()
    data_buf = bytearray()
    for s in strings:
        cur_off = data_start + len(data_buf)
        string_ids.extend(struct.pack("<I", cur_off))
        raw = s.encode("utf-8")
        # 1-byte ULEB128 utf16 length (for strings < 128 chars) + raw + NUL
        data_buf.append(len(s))
        data_buf.extend(raw)
        data_buf.append(0)

    total_size = data_start + len(data_buf)
    header = bytearray(0x70)
    header[0:8] = b"dex\n035\x00"
    struct.pack_into("<I", header, 0x20, total_size)
    struct.pack_into("<I", header, 0x24, 0x70)
    struct.pack_into("<I", header, 0x28, 0x12345678)
    struct.pack_into("<II", header, 0x38, n, string_ids_off)

    dex = header + string_ids + data_buf
    dex[12:32] = hashlib.sha1(dex[32:]).digest()
    struct.pack_into("<I", dex, 8, zlib.adler32(dex[12:]) & 0xFFFFFFFF)
    return bytes(dex)


def _build_param_sfo(fields: dict[str, str]) -> bytes:
    """构造一个符合 Sony \x00PSF 规范的 PARAM.SFO 二进制块。"""
    keys_buf = bytearray()
    data_buf = bytearray()
    index_buf = bytearray()
    for k, v in fields.items():
        k_off = len(keys_buf)
        keys_buf.extend(k.encode("utf-8") + b"\x00")
        raw_v = v.encode("utf-8") + b"\x00"
        max_len = (len(raw_v) + 3) & ~3
        padded_v = raw_v.ljust(max_len, b"\x00")
        d_off = len(data_buf)
        data_buf.extend(padded_v)
        # fmt = 0x0204 (UTF-8 C-string)
        index_buf.extend(struct.pack("<HHIII", k_off, 0x0204, len(raw_v), max_len, d_off))

    while len(keys_buf) % 4 != 0:
        keys_buf.append(0)
    key_start = 20 + len(index_buf)
    data_start = key_start + len(keys_buf)
    head = b"\x00PSF" + struct.pack("<IIII", 0x0101, key_start, data_start, len(fields))
    return head + bytes(index_buf) + bytes(keys_buf) + bytes(data_buf)


def _iso_dir_record(name: bytes, lba: int, size: int, is_dir: bool) -> bytes:
    rec_len = 33 + len(name)
    if rec_len % 2 == 1:
        rec_len += 1
    buf = bytearray(rec_len)
    buf[0] = rec_len
    struct.pack_into("<I", buf, 2, lba)
    struct.pack_into(">I", buf, 6, lba)
    struct.pack_into("<I", buf, 10, size)
    struct.pack_into(">I", buf, 14, size)
    buf[25] = 0x02 if is_dir else 0x00
    struct.pack_into("<H", buf, 28, 1)
    struct.pack_into(">H", buf, 30, 1)
    buf[32] = len(name)
    buf[33 : 33 + len(name)] = name
    return bytes(buf)


def _build_minimal_psp_iso(param_sfo: bytes, script_bin: bytes) -> bytes:
    """构造一个含 /PSP_GAME/PARAM.SFO 与 /PSP_GAME/USRDIR/SCRIPT.BIN 的最小 ISO-9660 光盘镜像。"""
    sector = 2048
    img = bytearray(24 * sector)

    # Sector 16: Primary Volume Descriptor
    pvd = 16 * sector
    img[pvd : pvd + 7] = b"\x01CD001\x01"
    root_rec = _iso_dir_record(b"\x00", lba=17, size=sector, is_dir=True)
    img[pvd + 156 : pvd + 156 + len(root_rec)] = root_rec

    # Sector 17: Root Directory -> PSP_GAME (LBA 18)
    s17 = 17 * sector
    r_dot = _iso_dir_record(b"\x00", 17, sector, True)
    r_dotdot = _iso_dir_record(b"\x01", 17, sector, True)
    r_pspgame = _iso_dir_record(b"PSP_GAME", 18, sector, True)
    cur = s17
    for rec in (r_dot, r_dotdot, r_pspgame):
        img[cur : cur + len(rec)] = rec
        cur += len(rec)

    # Sector 18: PSP_GAME Directory -> PARAM.SFO (LBA 20), USRDIR (LBA 19)
    s18 = 18 * sector
    r_sfo = _iso_dir_record(b"PARAM.SFO;1", 20, len(param_sfo), False)
    r_usrdir = _iso_dir_record(b"USRDIR", 19, sector, True)
    cur = s18
    for rec in (
        _iso_dir_record(b"\x00", 18, sector, True),
        _iso_dir_record(b"\x01", 17, sector, True),
        r_sfo,
        r_usrdir,
    ):
        img[cur : cur + len(rec)] = rec
        cur += len(rec)

    # Sector 19: USRDIR -> SCRIPT.BIN (LBA 21)
    s19 = 19 * sector
    r_script = _iso_dir_record(b"SCRIPT.BIN;1", 21, len(script_bin), False)
    cur = s19
    for rec in (
        _iso_dir_record(b"\x00", 19, sector, True),
        _iso_dir_record(b"\x01", 18, sector, True),
        r_script,
    ):
        img[cur : cur + len(rec)] = rec
        cur += len(rec)

    # Sector 20: PARAM.SFO data
    img[20 * sector : 20 * sector + len(param_sfo)] = param_sfo
    # Sector 21: SCRIPT.BIN data
    img[21 * sector : 21 * sector + len(script_bin)] = script_bin
    return bytes(img)


class MobileAndConsoleAdapterTests(unittest.TestCase):
    def test_android_apk_strings_xml_assets_and_dex_roundtrip(self):
        import hashlib
        import zipfile
        import zlib
        from ferrycore.adapters import detect

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            apk_path = root / "sample.apk"
            dex_blob = _build_minimal_dex(["Ljava/lang/String;", "「勇者よ、立ち上がれ！」"])
            with zipfile.ZipFile(apk_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("AndroidManifest.xml", "<manifest/>")
                zf.writestr("resources.arsc", b"ARSC_DUMMY_TABLE")
                zf.writestr("META-INF/CERT.SF", "Signature-Version: 1.0\n")
                zf.writestr(
                    "res/values/strings.xml",
                    '<resources><string name="app_name">幻想の迷宮</string></resources>',
                )
                zf.writestr(
                    "assets/www/data/System.json",
                    json.dumps({"gameTitle": "魔王討伐記"}, ensure_ascii=False),
                )
                zf.writestr("classes.dex", dex_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "apk")
            entries = adapter.collect(root)
            texts = [e.english for e in entries]
            self.assertIn("幻想の迷宮", texts)
            self.assertIn("魔王討伐記", texts)
            self.assertIn("「勇者よ、立ち上がれ！」", texts)

            ap = adapter.apply(
                root,
                {
                    "幻想の迷宮": "幻想迷宫",
                    "魔王討伐記": "魔王讨伐记",
                    "「勇者よ、立ち上がれ！」": "「勇士啊，快站起来！」",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 3)
            self.assertIsNotNone(ap.out_path)
            self.assertTrue(ap.out_path.name.endswith(".cn.apk"))

            with zipfile.ZipFile(ap.out_path, "r") as zout:
                # 1. 旧 META-INF/*.SF 签名块必须已被剥除
                self.assertNotIn("META-INF/CERT.SF", zout.namelist())
                # 2. resources.arsc 必须保持 ZIP_STORED 无压缩
                self.assertEqual(zout.getinfo("resources.arsc").compress_type, zipfile.ZIP_STORED)
                # 3. strings.xml 与 assets/www/data/System.json 已更新
                self.assertIn("幻想迷宫", zout.read("res/values/strings.xml").decode("utf-8"))
                self.assertIn("魔王讨伐记", zout.read("assets/www/data/System.json").decode("utf-8"))
                # 4. classes.dex 译文已原位写入且 SHA-1 与 Adler32 校验头合法
                patched_dex = zout.read("classes.dex")
                self.assertIn("「勇士啊，快站起来！」".encode("utf-8"), patched_dex)
                self.assertEqual(patched_dex[12:32], hashlib.sha1(patched_dex[32:]).digest())
                self.assertEqual(
                    struct.unpack_from("<I", patched_dex, 8)[0],
                    zlib.adler32(patched_dex[12:]) & 0xFFFFFFFF,
                )

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse(ap.out_path.exists())

    def test_ios_ipa_bplist_and_text_strings_preserve_exec_permissions(self):
        import plistlib
        import zipfile
        from ferrycore.adapters import detect

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ipa_path = root / "game.ipa"
            bplist_strings = plistlib.dumps(
                {"START_BTN": "ゲームを始める", "INTERNAL_ID": "btn_01"},
                fmt=plistlib.FMT_BINARY,
            )
            text_strings = '"GREETING" = "「ようこそ、星見の塔へ！」";\n'.encode("utf-16")

            with zipfile.ZipFile(ipa_path, "w") as zf:
                exec_info = zipfile.ZipInfo("Payload/StarTower.app/StarTower")
                exec_info.external_attr = (0o100755 & 0xFFFF) << 16
                zf.writestr(exec_info, b"\xcf\xfa\xed\xfeMACH_O_BIN")
                zf.writestr("Payload/StarTower.app/ja.lproj/Localizable.strings", bplist_strings)
                zf.writestr("Payload/StarTower.app/ja.lproj/Story.strings", text_strings)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "ipa")
            entries = adapter.collect(root)
            texts = [e.english for e in entries]
            self.assertIn("ゲームを始める", texts)
            self.assertIn("「ようこそ、星見の塔へ！」", texts)
            self.assertNotIn("btn_01", texts)

            ap = adapter.apply(
                root,
                {
                    "ゲームを始める": "开始游戏",
                    "「ようこそ、星見の塔へ！」": "「欢迎来到观星之塔！」",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)
            self.assertTrue(ap.out_path.name.endswith(".cn.ipa"))

            with zipfile.ZipFile(ap.out_path, "r") as zout:
                # UNIX 0755 可执行权限位必须一字不差保留
                self.assertEqual(
                    zout.getinfo("Payload/StarTower.app/StarTower").external_attr,
                    (0o100755 & 0xFFFF) << 16,
                )
                patched_bplist = plistlib.loads(
                    zout.read("Payload/StarTower.app/ja.lproj/Localizable.strings")
                )
                self.assertEqual(patched_bplist["START_BTN"], "开始游戏")
                patched_u16 = zout.read("Payload/StarTower.app/ja.lproj/Story.strings").decode("utf-16")
                self.assertIn('"GREETING" = "「欢迎来到观星之塔！」";', patched_u16)

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse(ap.out_path.exists())

    def test_psv_unpacked_and_repatch_generation(self):
        from ferrycore.adapters import detect

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sce_sys = root / "sce_sys"
            data_dir = root / "data"
            sce_sys.mkdir()
            data_dir.mkdir()

            (sce_sys / "param.sfo").write_bytes(
                _build_param_sfo({"TITLE": "セルセタの樹海", "TITLE_ID": "PCSG00123"})
            )
            script_blob = (
                b"PSV_SCR\x00"
                + "「アドル、気をつけろ！魔物の気配がするぞ。」".encode("cp932")
                + b"\x00TAIL\x00"
            )
            (data_dir / "scene01.bin").write_bytes(script_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "psv")
            probe = adapter.probe(root)
            self.assertTrue(probe.readable)
            self.assertIn("PCSG00123", probe.engine)

            entries = adapter.collect(root)
            self.assertEqual([e.english for e in entries], ["「アドル、気をつけろ！魔物の気配がするぞ。」"])

            ap = adapter.apply(
                root,
                {"「アドル、気をつけろ！魔物の気配がするぞ。」": "「亚特鲁，小心！前面有魔物的气息。」"},
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 1)
            repatch_file = root / "rePatch" / "PCSG00123" / "data" / "scene01.bin"
            self.assertTrue(repatch_file.is_file())
            self.assertEqual(len(repatch_file.read_bytes()), len(script_blob))
            self.assertIn("「亚特鲁，小心！前面有魔物的气息。」".encode("gbk"), repatch_file.read_bytes())

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertEqual((data_dir / "scene01.bin").read_bytes(), script_blob)
            self.assertFalse(repatch_file.exists())

    def test_psp_iso9660_parse_and_lba_extent_patch(self):
        from ferrycore.adapters import detect, parse_iso9660

        sfo = _build_param_sfo({"TITLE": "英雄伝説 空の軌跡", "DISC_ID": "ULJM05170"})
        script = (
            b"PSP_MES\x00"
            + "「ヨシュア、準備はいい？そろそろ出発するわよ！」".encode("cp932")
            + b"\x00END\x00"
        )
        iso_blob = _build_minimal_psp_iso(sfo, script)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso_path = root / "sora.iso"
            iso_path.write_bytes(iso_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "psp")
            probe = adapter.probe(root)
            self.assertTrue(probe.readable)
            self.assertIn("ULJM05170", probe.engine)

            entries = adapter.collect(root)
            self.assertEqual(
                [e.english for e in entries],
                ["「ヨシュア、準備はいい？そろそろ出発するわよ！」"],
            )

            ap = adapter.apply(
                root,
                {"「ヨシュア、準備はいい？そろそろ出発するわよ！」": "「约修亚，准备好了吗？我们差不多该出发啦！」"},
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 1)
            self.assertIsNotNone(ap.out_path)
            self.assertTrue(ap.out_path.name.endswith(".cn.iso"))

            # 重新按 ISO-9660 解析产出的 .cn.iso，确认扇区结构与译文完全正确
            patched_iso = ap.out_path.read_bytes()
            self.assertEqual(len(patched_iso), len(iso_blob))
            iso_entries = parse_iso9660(patched_iso)
            scr_ent = iso_entries["PSP_GAME/USRDIR/SCRIPT.BIN"]
            scr_data = patched_iso[scr_ent.offset : scr_ent.offset + scr_ent.size]
            self.assertIn("「约修亚，准备好了吗？我们差不多该出发啦！」".encode("gbk"), scr_data)

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse(ap.out_path.exists())


def _build_minimal_locres_v2(strings: list[str]) -> bytes:
    """构造一个符合 UE4/UE5 Optimized v2 规范的 .locres 二进制块。"""
    from ferrycore.adapters.unreal import LOCRES_MAGIC, write_fstring

    dummy_index_body = struct.pack("<II", 0, 0)
    str_array_off = 16 + 1 + 8 + len(dummy_index_body)
    head = LOCRES_MAGIC + struct.pack("<Bq", 2, str_array_off) + dummy_index_body
    arr = bytearray(struct.pack("<I", len(strings)))
    for s in strings:
        arr.extend(write_fstring(s))
        arr.extend(struct.pack("<i", 1))  # ref_count = 1
    return head + bytes(arr)


def _build_minimal_il2cpp_metadata(strings: list[str]) -> bytes:
    """构造一个符合 Unity IL2CPP global-metadata.dat (0xFAB11BAF) 规范的最小二进制块。"""
    lit_off = 24
    lit_size = len(strings) * 8
    data_off = lit_off + lit_size
    lits = bytearray()
    data = bytearray()
    for s in strings:
        raw = s.encode("utf-8")
        lits.extend(struct.pack("<II", len(raw), len(data)))
        data.extend(raw)
    head = struct.pack("<IiIIII", 0xFAB11BAF, 29, lit_off, lit_size, data_off, len(data))
    return head + bytes(lits) + bytes(data)


def _build_minimal_arsc(strings: list[str]) -> bytes:
    """构造一个含 ResTable_header (0x0002) 与 UTF-8 ResStringPool (0x0001) 的最小 resources.arsc。"""
    offsets = bytearray()
    str_data = bytearray()
    for s in strings:
        offsets.extend(struct.pack("<I", len(str_data)))
        raw = s.encode("utf-8")
        str_data.append(len(s))
        str_data.append(len(raw))
        str_data.extend(raw)
        str_data.append(0)
    while len(str_data) % 4 != 0:
        str_data.append(0)

    strings_start = 28 + len(offsets)
    pool_size = strings_start + len(str_data)
    pool_hdr = struct.pack(
        "<HHIIIIII",
        0x0001,
        28,
        pool_size,
        len(strings),
        0,
        0x100,  # UTF8_FLAG
        strings_start,
        0,
    )
    table_size = 12 + pool_size
    table_hdr = struct.pack("<HHII", 0x0002, 12, table_size, 0)
    return table_hdr + pool_hdr + bytes(offsets) + bytes(str_data)


def _build_minimal_nds_rom(title: str, code: str, files: dict[str, bytes]) -> bytes:
    """构造一个含 NitroROM FNT (0x40) 与 FAT (0x48) 的最小合法 Nintendo DS .nds 镜像。"""
    rom = bytearray(0x400)
    rom[0x00:0x0C] = title.encode("ascii")[:12].ljust(12, b"\x00")
    rom[0x0C:0x10] = code.encode("ascii")[:4].ljust(4, b"\x00")

    fnt_main = struct.pack("<IHH", 8, 0, 1)
    fnt_sub = bytearray()
    for name in files:
        nb = name.encode("ascii")
        fnt_sub.append(len(nb))
        fnt_sub.extend(nb)
    fnt_sub.append(0)
    fnt = fnt_main + bytes(fnt_sub)
    while len(fnt) % 4 != 0:
        fnt += b"\x00"

    fnt_off = len(rom)
    rom.extend(fnt)
    fat_off = len(rom)
    fat_size = len(files) * 8
    rom.extend(b"\x00" * fat_size)

    for idx, (_name, raw) in enumerate(files.items()):
        while len(rom) % 4 != 0:
            rom.append(0)
        start = len(rom)
        rom.extend(raw)
        end = len(rom)
        struct.pack_into("<II", rom, fat_off + idx * 8, start, end)

    struct.pack_into("<IIII", rom, 0x40, fnt_off, len(fnt), fat_off, fat_size)
    struct.pack_into("<I", rom, 0x80, len(rom))
    return bytes(rom)


class GlobalAndExtendedEngineTests(unittest.TestCase):
    def test_rawbin_multi_encoding_western_utf8_and_utf16le(self):
        from ferrygame import patch_blob, scan_binary_strings

        blob = b"".join([
            b"WESTERN_RPG_V1\x00",
            b"The ancient oak door is locked from the inside.\x00",
            b"New Game\x00",
            b"Quit Game\x00",
            b"\x00\x00",
            "You found a rusted iron key beneath the altar.".encode("utf-16-le") + b"\x00\x00",
            b"END_MARK\x00",
        ])
        units = scan_binary_strings(blob, rel_path="game.exe")
        srcs = [u.src for u in units]
        self.assertIn("The ancient oak door is locked from the inside.", srcs)
        self.assertIn("New Game", srcs)
        self.assertIn("Quit Game", srcs)
        self.assertIn("You found a rusted iron key beneath the altar.", srcs)

        tr_map = {
            "The ancient oak door is locked from the inside.": "那扇古老的橡木门从里面锁上了。",
            "New Game": "开始",
            "Quit Game": "退出",
            "You found a rusted iron key beneath the altar.": "你在祭坛下方找到了一把生锈的铁钥匙。",
        }
        for u in units:
            u.dst = tr_map.get(u.src, "")

        patched, count, conflicts = patch_blob(blob, units)
        self.assertEqual(count, 4)
        self.assertEqual(conflicts, [])
        self.assertEqual(len(patched), len(blob))
        self.assertIn("那扇古老的橡木门从里面锁上了。".encode("utf-8"), patched)
        self.assertIn("你在祭坛下方找到了一把生锈的铁钥匙。".encode("utf-16-le"), patched)

    def test_unreal_locres_v2_extract_and_utf16_rebuild(self):
        from ferrycore.adapters import detect, parse_locres

        locres_blob = _build_minimal_locres_v2([
            "The kingdom has fallen into eternal darkness.",
            "Press Start to Continue",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            loc_dir = root / "Content" / "Localization" / "Game" / "en"
            loc_dir.mkdir(parents=True)
            loc_path = loc_dir / "Game.locres"
            loc_path.write_bytes(locres_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "unreal")
            entries = adapter.collect(root)
            self.assertEqual(
                [e.english for e in entries],
                ["The kingdom has fallen into eternal darkness.", "Press Start to Continue"],
            )

            ap = adapter.apply(
                root,
                {
                    "The kingdom has fallen into eternal darkness.": "王国已坠入永恒的黑暗之中。",
                    "Press Start to Continue": "按开始键继续",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)

            re_parsed = parse_locres(loc_path.read_bytes())
            self.assertEqual(
                [s.text for s in re_parsed.strings],
                ["王国已坠入永恒的黑暗之中。", "按开始键继续"],
            )

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertEqual(loc_path.read_bytes(), locres_blob)

    def test_narrative_ink_yarn_and_twine_roundtrip(self):
        from ferrycore.adapters import detect

        ink_json = json.dumps({
            "inkVersion": 21,
            "root": [["^The lighthouse keeper nods slowly.", "\n", ["ev", "^Ask about the storm", "/ev"]]],
        })
        yarn_text = (
            "title: Harbor\n"
            "---\n"
            "Captain: The fog is too thick to set sail tonight. #line:101\n"
            "-> We cannot wait until morning! #line:102\n"
            "===\n"
        )
        twine_html = (
            '<tw-storydata name="Moor" startnode="1">'
            '<tw-passagedata pid="1" name="Start">'
            "A cold wind blows across the abandoned moor.\n"
            "[[enter_cabin]]"
            "</tw-passagedata></tw-storydata>"
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "story.ink.json").write_text(ink_json, encoding="utf-8")
            (root / "harbor.yarn").write_text(yarn_text, encoding="utf-8")
            (root / "index.html").write_text(twine_html, encoding="utf-8")

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "narrative")
            entries = adapter.collect(root)
            texts = [e.english for e in entries]
            self.assertIn("The lighthouse keeper nods slowly.", texts)
            self.assertIn("Ask about the storm", texts)
            self.assertIn("The fog is too thick to set sail tonight.", texts)
            self.assertIn("We cannot wait until morning!", texts)
            self.assertIn("A cold wind blows across the abandoned moor.", texts)

            ap = adapter.apply(
                root,
                {
                    "The lighthouse keeper nods slowly.": "灯塔看守人缓缓点了点头。",
                    "Ask about the storm": "询问关于暴风雨的事",
                    "The fog is too thick to set sail tonight.": "今晚雾太浓了，没法起航。",
                    "We cannot wait until morning!": "我们等不到明天早上了！",
                    "A cold wind blows across the abandoned moor.": "寒风吹过荒废的旷野。",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 5)

            patched_ink = json.loads((root / "story.ink.json").read_text(encoding="utf-8"))
            self.assertEqual(patched_ink["root"][0][0], "^灯塔看守人缓缓点了点头。")
            patched_yarn = (root / "harbor.yarn").read_text(encoding="utf-8")
            self.assertIn("Captain: 今晚雾太浓了，没法起航。 #line:101", patched_yarn)
            self.assertIn("-> 我们等不到明天早上了！ #line:102", patched_yarn)
            patched_twine = (root / "index.html").read_text(encoding="utf-8")
            self.assertIn("寒风吹过荒废的旷野。", patched_twine)
            self.assertIn("[[enter_cabin]]", patched_twine)

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertEqual((root / "harbor.yarn").read_text(encoding="utf-8"), yarn_text)

    def test_love2d_archive_and_onscripter_xor_roundtrip(self):
        import zipfile
        from ferrycore.adapters import detect
        from ferrycore.adapters.vn_engines import xor_nscript_dat

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            love_path = root / "balatro_like.love"
            lua_src = (
                'local bg = love.graphics.newImage("assets/bg.png")\n'
                'local title = "Chronicles of the Void"\n'
                'local menu = { ["btn_new"] = "New Game", ["btn_quit"] = "Quit Game" }\n'
            )
            with zipfile.ZipFile(love_path, "w") as zf:
                zf.writestr("main.lua", lua_src)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "love2d")
            texts = [e.english for e in adapter.collect(root)]
            self.assertIn("Chronicles of the Void", texts)
            self.assertIn("New Game", texts)
            self.assertNotIn("assets/bg.png", texts)
            self.assertNotIn("btn_new", texts)

            ap = adapter.apply(root, {"Chronicles of the Void": "虚空编年史", "New Game": "新游戏"})
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)
            self.assertTrue(ap.out_path.name.endswith(".cn.love"))
            with zipfile.ZipFile(ap.out_path, "r") as zout:
                patched_lua = zout.read("main.lua").decode("utf-8")
                self.assertIn('"虚空编年史"', patched_lua)
                self.assertIn('["btn_new"] = "新游戏"', patched_lua)

            self.assertTrue(revert_game(root).ok)
            self.assertFalse(ap.out_path.exists())

        with tempfile.TemporaryDirectory() as tmp2:
            root2 = Path(tmp2)
            ons_plain = (
                '*define\n'
                'caption "遠野物語"\n'
                'game\n'
                '*start\n'
                '「先輩、今日はとても月が綺麗ですね。」\\\n'
            ).encode("cp932")
            nscript_path = root2 / "nscript.dat"
            nscript_path.write_bytes(xor_nscript_dat(ons_plain))

            adapter2 = detect(root2)
            self.assertIsNotNone(adapter2)
            self.assertEqual(adapter2.name, "onscripter")
            texts2 = [e.english for e in adapter2.collect(root2)]
            self.assertIn("遠野物語", texts2)
            self.assertIn("「先輩、今日はとても月が綺麗ですね。」\\", texts2)

            ap2 = adapter2.apply(
                root2,
                {
                    "遠野物語": "远野物语",
                    "「先輩、今日はとても月が綺麗ですね。」\\": "「前辈，今晚的月色真美呢。」\\",
                },
            )
            self.assertTrue(ap2.ok)
            self.assertEqual(ap2.applied, 2)
            decrypted = xor_nscript_dat(nscript_path.read_bytes()).decode("gbk")
            self.assertIn('caption "远野物语"', decrypted)
            self.assertIn("「前辈，今晚的月色真美呢。」\\", decrypted)

            self.assertTrue(revert_game(root2).ok)
            self.assertEqual(nscript_path.read_bytes(), xor_nscript_dat(ons_plain))

    def test_unity_il2cpp_arsc_psp_cso_and_nds_rom(self):
        from ferrycore.adapters import (
            compress_cso,
            detect,
            extract_arsc_strings,
            extract_il2cpp_metadata_strings,
            patch_arsc_strings,
            rebuild_il2cpp_metadata,
        )

        meta_blob = _build_minimal_il2cpp_metadata(["PlayerController", "Welcome to the Citadel of Stars!"])
        extracted_meta = extract_il2cpp_metadata_strings(meta_blob)
        self.assertEqual(extracted_meta, [(1, "Welcome to the Citadel of Stars!")])
        rebuilt_meta, count = rebuild_il2cpp_metadata(
            meta_blob, {"Welcome to the Citadel of Stars!": "欢迎来到群星堡垒！这是一段更长的中文译文测试。"}
        )
        self.assertEqual(count, 1)
        self.assertEqual(
            extract_il2cpp_metadata_strings(rebuilt_meta),
            [(1, "欢迎来到群星堡垒！这是一段更长的中文译文测试。")],
        )

        arsc_blob = _build_minimal_arsc(["res/layout/main.xml", "「冒険を始めますか？」"])
        self.assertEqual(extract_arsc_strings(arsc_blob), [(1, "「冒険を始めますか？」")])
        patched_arsc, a_count = patch_arsc_strings(arsc_blob, {"「冒険を始めますか？」": "「要开始冒险吗？」"})
        self.assertEqual(a_count, 1)
        self.assertEqual(extract_arsc_strings(patched_arsc)[0][1].strip(), "「要开始冒险吗？」")

        sfo = _build_param_sfo({"TITLE": "Monster Hunter", "DISC_ID": "ULJM05800"})
        script = b"MH_SCR\x00" + "「集会所へようこそ、ハンターさん！」".encode("cp932") + b"\x00"
        cso_blob = compress_cso(_build_minimal_psp_iso(sfo, script))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "mh.cso").write_bytes(cso_blob)
            adapter = detect(root)
            self.assertEqual(adapter.name, "psp")
            ap = adapter.apply(root, {"「集会所へようこそ、ハンターさん！」": "「欢迎来到集会所，猎人先生！」"})
            self.assertTrue(ap.ok)
            self.assertTrue(ap.out_path.name.endswith(".cn.cso"))
            self.assertTrue(revert_game(root).ok)

        with tempfile.TemporaryDirectory() as tmp_nds:
            root_nds = Path(tmp_nds)
            nds_script = b"NDS_MSG\x00" + "「異議あり！その証言には矛盾があります！」".encode("cp932") + b"\x00"
            nds_blob = _build_minimal_nds_rom("GYAKUTEN", "AGYJ", {"data/case01.bin": nds_script})
            (root_nds / "gyakuten.nds").write_bytes(nds_blob)

            nds_adapter = detect(root_nds)
            self.assertIsNotNone(nds_adapter)
            self.assertEqual(nds_adapter.name, "nds")
            entries = nds_adapter.collect(root_nds)
            self.assertEqual([e.english for e in entries], ["「異議あり！その証言には矛盾があります！」"])

            ap_nds = nds_adapter.apply(
                root_nds,
                {"「異議あり！その証言には矛盾があります！」": "「反对！那份证词里存在明显的矛盾！」"},
            )
            self.assertTrue(ap_nds.ok)
            self.assertEqual(ap_nds.applied, 1)
            self.assertTrue(ap_nds.out_path.name.endswith(".cn.nds"))
            self.assertIn("「反对！那份证词里存在明显的矛盾！」".encode("gbk"), ap_nds.out_path.read_bytes())
            self.assertTrue(revert_game(root_nds).ok)
            self.assertFalse(ap_nds.out_path.exists())


def _build_minimal_java_class(strings: list[str]) -> bytes:
    """构造一个含 CONSTANT_Utf8 (Tag 1) 与 CONSTANT_String (Tag 8) 的最小合法 Java .class (0xCAFEBABE)。"""
    # 每个字符串占 2 个槽位：#i = CONSTANT_Utf8, #(i+1) = CONSTANT_String(#i)
    cp_count = 1 + len(strings) * 2
    cp = bytearray()
    cur_idx = 1
    for s in strings:
        raw = s.encode("utf-8")
        cp.append(1)  # CONSTANT_Utf8
        cp.extend(struct.pack(">H", len(raw)))
        cp.extend(raw)
        cp.append(8)  # CONSTANT_String -> points to cur_idx
        cp.extend(struct.pack(">H", cur_idx))
        cur_idx += 2
    # 末尾附上 16 字节伪 access_flags / this_class / super_class / counts 尾部字节码
    tail = b"\x00\x21\x00\x01\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00"
    return struct.pack(">IHH", 0xCAFEBABE, 0, 46) + struct.pack(">H", cp_count) + bytes(cp) + tail


def _build_minimal_cws_swf_as2(strings: list[str]) -> bytes:
    """构造一个带 AS2 DoAction (Tag 12) + ActionConstantPool (0x88) 的 CWS (zlib) Flash .swf。"""
    import zlib
    from ferrycore.adapters.flash import _encode_swf_tag

    pool_body = bytearray(struct.pack("<H", len(strings)))
    for s in strings:
        pool_body.extend(s.encode("utf-8") + b"\x00")
    action_record = b"\x88" + struct.pack("<H", len(pool_body)) + bytes(pool_body) + b"\x00"
    do_action_tag = _encode_swf_tag(12, action_record)
    end_tag = struct.pack("<H", 0)
    # 最小 RECT (nbits=0 -> 1 byte 0x00) + frame_rate(2B) + frame_count(2B)
    body = b"\x00" + struct.pack("<HH", 24 << 8, 1) + do_action_tag + end_tag
    total_len = 8 + len(body)
    return b"CWS\x08" + struct.pack("<I", total_len) + zlib.compress(body)


def _build_minimal_director_dir(stxt_text: str) -> bytes:
    """构造一个模拟 Win3.1《东脑》《中天》Macromedia Director 4 的 RIFX/MV93 .dir 容器（含 STXT 演员文本块）。"""
    raw_str = stxt_text.encode("cp932")
    stxt_body = struct.pack(">III", 12, len(raw_str), 0) + raw_str
    stxt_chunk = b"STXT" + struct.pack(">I", len(stxt_body)) + stxt_body
    rifx_body = b"MV93" + stxt_chunk
    return b"RIFX" + struct.pack(">I", len(rifx_body)) + rifx_body


class RetroMobileFlashAndSelfTranslateTests(unittest.TestCase):
    def test_j2me_jar_class_constant_pool_rebuild_and_jad_size_sync(self):
        import zipfile
        from ferrycore.adapters import detect
        from ferrycore.adapters.j2me import extract_class_utf8_strings

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jar_path = root / "BounceTales.jar"
            jad_path = root / "BounceTales.jad"
            cls_blob = _build_minimal_java_class([
                "(Ljavax/microedition/lcdui/Graphics;)V",
                "Welcome to the Ancient Temple!",
                "New Game",
            ])
            with zipfile.ZipFile(jar_path, "w") as zf:
                zf.writestr(
                    "META-INF/MANIFEST.MF",
                    "Manifest-Version: 1.0\nMIDlet-Name: AncientTemple\nMIDlet-1: Temple,/icon.png,GameMIDlet\nMicroEdition-Profile: MIDP-2.0\n",
                )
                zf.writestr("GameMIDlet.class", cls_blob)
            jad_path.write_text(
                f"MIDlet-Name: AncientTemple\nMIDlet-Jar-URL: BounceTales.jar\nMIDlet-Jar-Size: {jar_path.stat().st_size}\n",
                encoding="utf-8",
            )

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "j2me")
            entries = adapter.collect(root)
            texts = [e.english for e in entries]
            self.assertIn("Welcome to the Ancient Temple!", texts)
            self.assertIn("New Game", texts)
            self.assertNotIn("(Ljavax/microedition/lcdui/Graphics;)V", texts)

            # 测试任意长度的 UTF-8 中文译文重建 .class 常量池
            long_zh = "欢迎来到远古神庙！这是一段比原英文长很多的中文测试，验证常量池无损变长重建。"
            ap = adapter.apply(
                root,
                {
                    "Welcome to the Ancient Temple!": long_zh,
                    "New Game": "开始新游戏",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)
            self.assertTrue(ap.out_path.name.endswith(".cn.jar"))

            with zipfile.ZipFile(ap.out_path, "r") as zout:
                patched_cls = zout.read("GameMIDlet.class")
                extracted_after = dict(extract_class_utf8_strings(patched_cls))
                self.assertIn(long_zh, extracted_after.values())
                self.assertIn("开始新游戏", extracted_after.values())

            # .cn.jad 必须同步生成且 MIDlet-Jar-Size 与新 .cn.jar 完全一致
            out_jad = ap.out_path.with_suffix(".jad")
            self.assertTrue(out_jad.is_file())
            self.assertIn(f"MIDlet-Jar-Size: {ap.out_path.stat().st_size}", out_jad.read_text(encoding="utf-8"))

            self.assertTrue(revert_game(root).ok)
            self.assertFalse(ap.out_path.exists())
            self.assertFalse(out_jad.exists())

    def test_symbian_s60_unpacked_rsc_and_e32image_app(self):
        from ferrycore.adapters import detect

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "system" / "apps" / "SkyForce"
            app_dir.mkdir(parents=True)
            # 模拟塞班 E32Image .app（偏移 16 处带 EPOC 签名 + UTF-16LE _LIT16 台词）与 .r01 资源
            e32_blob = (
                b"\x00" * 16
                + b"EPOC"
                + b"\x00" * 12
                + "Destroy all enemy radar stations before dawn!".encode("utf-16-le")
                + b"\x00\x00TAIL"
            )
            rsc_blob = (
                b"RSC_HDR\x00"
                + b"New Game\x00"
                + b"Exit Game\x00"
            )
            (app_dir / "SkyForce.app").write_bytes(e32_blob)
            (app_dir / "SkyForce.r01").write_bytes(rsc_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "symbian")
            texts = [e.english for e in adapter.collect(root)]
            self.assertIn("Destroy all enemy radar stations before dawn!", texts)
            self.assertIn("New Game", texts)

            ap = adapter.apply(
                root,
                {
                    "Destroy all enemy radar stations before dawn!": "在黎明前摧毁所有敌方雷达站！",
                    "New Game": "开始",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)
            self.assertIn(
                "在黎明前摧毁所有敌方雷达站！".encode("utf-16-le"),
                (app_dir / "SkyForce.app").read_bytes(),
            )
            self.assertTrue(revert_game(root).ok)
            self.assertEqual((app_dir / "SkyForce.app").read_bytes(), e32_blob)

    def test_flash_cws_swf_and_projector_exe_roundtrip(self):
        from ferrycore.adapters import detect
        from ferrycore.adapters.flash import FLASH_PROJECTOR_FOOTER_MAGIC, extract_swf_strings

        swf_blob = _build_minimal_cws_swf_as2([
            "_root",
            "「剣を抜け、最後の戦いが始まる！」",
            "Start Game",
        ])
        projector_exe = (
            b"MZ" + (b"\x00" * 126) + swf_blob + struct.pack("<II", FLASH_PROJECTOR_FOOTER_MAGIC, len(swf_blob))
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            exe_path = root / "flash_rpg.exe"
            exe_path.write_bytes(projector_exe)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "flash")
            texts = [e.english for e in adapter.collect(root)]
            self.assertIn("「剣を抜け、最後の戦いが始まる！」", texts)
            self.assertIn("Start Game", texts)
            self.assertNotIn("_root", texts)

            ap = adapter.apply(
                root,
                {
                    "「剣を抜け、最後の戦いが始まる！」": "「拔剑吧，最后的决战已经打响了！（支持任意长度 UTF-8 扩展）」",
                    "Start Game": "开始游戏",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)
            self.assertTrue(ap.out_path.name.endswith(".cn.exe"))

            out_bytes = ap.out_path.read_bytes()
            magic, new_swf_size = struct.unpack_from("<II", out_bytes, len(out_bytes) - 8)
            self.assertEqual(magic, FLASH_PROJECTOR_FOOTER_MAGIC)
            embedded_swf = out_bytes[len(out_bytes) - 8 - new_swf_size : len(out_bytes) - 8]
            after_strs = [t for _, t in extract_swf_strings(embedded_swf)]
            self.assertIn("「拔剑吧，最后的决战已经打响了！（支持任意长度 UTF-8 扩展）」", after_strs)
            self.assertIn("开始游戏", after_strs)

            self.assertTrue(revert_game(root).ok)
            self.assertFalse(ap.out_path.exists())

    def test_retro_win31_director_tongnou_and_win16_ne(self):
        from ferrycore.adapters import detect

        # 模拟 Win3.1《东脑》《中天》的 Macromedia Director RIFX/MV93 .dir 文件
        dir_blob = _build_minimal_director_dir("「東脳の迷宮へようこそ。あなたの魂を探しなさい。」")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dir_path = root / "TONGNOU.DIR"
            dir_path.write_bytes(dir_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "retro_win")
            probe = adapter.probe(root)
            self.assertTrue(probe.readable)
            self.assertIn("Macromedia Director", probe.engine)

            entries = adapter.collect(root)
            self.assertEqual([e.english for e in entries], ["「東脳の迷宮へようこそ。あなたの魂を探しなさい。」"])

            ap = adapter.apply(
                root,
                {"「東脳の迷宮へようこそ。あなたの魂を探しなさい。」": "「欢迎来到东脑的迷宫。去寻找你失落的灵魂吧。」"},
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 1)
            self.assertEqual(len(dir_path.read_bytes()), len(dir_blob))
            self.assertIn("「欢迎来到东脑的迷宫。去寻找你失落的灵魂吧。」".encode("gbk"), dir_path.read_bytes())

            self.assertTrue(revert_game(root).ok)
            self.assertEqual(dir_path.read_bytes(), dir_blob)

    def test_self_translation_tsv_extract_and_repack_workflow(self):
        from ferrygame.cli import main as cli_main
        from ferrygame.project import TSV_NAME

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dir_path = root / "CHUTENG.DIR"
            orig_blob = _build_minimal_director_dir("「中天の星々があなたを呼んでいる……」")
            dir_path.write_bytes(orig_blob)

            # 第一步：运行 `ferrygame extract` 导出自翻译表（0 Token）
            rc1 = cli_main(["extract", str(root)])
            self.assertEqual(rc1, 0)
            tsv_path = root / TSV_NAME
            self.assertTrue(tsv_path.is_file())
            # 确认带 UTF-8 BOM 供 Excel 直接打开
            self.assertTrue(tsv_path.read_bytes().startswith(b"\xef\xbb\xbf"))

            # 第二步：玩家在 Excel / 记事本中编辑 ferry_tl.tsv 的 dst 列
            lines = tsv_path.read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(len(lines), 2)
            cols = lines[1].split("\t")
            cols[4] = "「中天的群星正在呼唤着你……」"
            lines[1] = "\t".join(cols)
            tsv_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")

            # 第三步：运行 `ferrygame repack` 自动回录自翻译表并重打包写回游戏（0 Token）
            rc2 = cli_main(["repack", str(root)])
            self.assertEqual(rc2, 0)
            self.assertIn("「中天的群星正在呼唤着你……」".encode("gbk"), dir_path.read_bytes())

            # 第四步：一键撤销
            rc3 = cli_main(["revert", str(root)])
            self.assertEqual(rc3, 0)
            self.assertEqual(dir_path.read_bytes(), orig_blob)

    def test_symbian_sisx_tlv_zlib_and_anna_belle_qt_qm(self):
        import zlib
        from ferrycore.adapters import detect
        from ferrycore.adapters.symbian import (
            QT_QM_MAGIC,
            SYMBIAN_9_SIS_UID1,
            extract_sisx_compressed_blocks,
            parse_qt_qm,
        )

        # 构造一个 Qt .qm (Messages section 0x69: SourceText 0x06 + Translation 0x03 + End 0x01)
        src_str = b"Defend the northern fortress at all costs!"
        tr_str = "Defend the northern fortress at all costs!".encode("utf-16-be")
        msg_rec = (
            b"\x06" + struct.pack(">I", len(src_str)) + src_str
            + b"\x03" + struct.pack(">I", len(tr_str)) + tr_str
            + b"\x01"
        )
        qm_blob = QT_QM_MAGIC + b"\x69" + struct.pack(">I", len(msg_rec)) + msg_rec

        # 塞进 Symbian 9+ (S60v3/v5/Anna/Belle) .sisx 的 SISCompressed (0x0018) zlib TLV 块中
        comp_payload = zlib.compress(qm_blob, 6)
        sis_comp_chunk = (
            struct.pack("<HI", 0x0018, 12 + len(comp_payload))
            + struct.pack("<IQ", 1, len(qm_blob))
            + comp_payload
        )
        sisx_blob = struct.pack("<IIII", SYMBIAN_9_SIS_UID1, 0, 0, 0) + sis_comp_chunk

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sisx_path = root / "AnnaQuest.sisx"
            sisx_path.write_bytes(sisx_blob)

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "symbian")
            entries = adapter.collect(root)
            self.assertEqual([e.english for e in entries], ["Defend the northern fortress at all costs!"])

            ap = adapter.apply(
                root,
                {"Defend the northern fortress at all costs!": "不惜一切代价守住北方要塞！（安娜/贝拉 Qt .qm 变长测试）"},
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 1)
            self.assertTrue(ap.out_path.name.endswith(".cn.sisx"))

            # 解开产出的 .cn.sisx 内部 TLV zlib 块，验证 Qt .qm 已被无损重建为 UTF-16BE 中文
            out_blocks = extract_sisx_compressed_blocks(ap.out_path.read_bytes())
            self.assertEqual(len(out_blocks), 1)
            parsed_qm = parse_qt_qm(out_blocks[0].decompressed)
            self.assertEqual(parsed_qm[0].translation, "不惜一切代价守住北方要塞！（安娜/贝拉 Qt .qm 变长测试）")

            self.assertTrue(revert_game(root).ok)
            self.assertFalse(ap.out_path.exists())

    def test_blackberry_cod_jad_sha1_and_bb10_bar_roundtrip(self):
        import hashlib
        import zipfile
        from ferrycore.adapters import detect
        from ferrycore.adapters.blackberry import RIM_COD_MAGIC

        # 1. BlackBerry OS 4~7 .cod (0xDEC0FFFF) + .jad (RIM-COD-SHA1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cod_path = root / "BrickBreaker.cod"
            jad_path = root / "BrickBreaker.jad"
            cod_blob = (
                RIM_COD_MAGIC
                + b"\x00" * 16
                + b"The encrypted message has been intercepted by headquarters.\x00"
                + b"New Game\x00"
            )
            cod_path.write_bytes(cod_blob)
            jad_path.write_text(
                "MIDlet-Name: BrickBreaker\n"
                f"RIM-COD-URL: {cod_path.name}\n"
                f"RIM-COD-Size: {len(cod_blob)}\n"
                "RIM-COD-SHA1: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00\n",
                encoding="utf-8",
            )

            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "blackberry")
            texts = [e.english for e in adapter.collect(root)]
            self.assertIn("The encrypted message has been intercepted by headquarters.", texts)
            self.assertIn("New Game", texts)

            ap = adapter.apply(
                root,
                {
                    "The encrypted message has been intercepted by headquarters.": "那份加密电报已被总部截获。",
                    "New Game": "开始",
                },
            )
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)
            self.assertTrue(ap.out_path.name.endswith(".cn.cod"))
            self.assertIn("那份加密电报已被总部截获。".encode("utf-8"), ap.out_path.read_bytes())

            # 验证 .cn.jad 的 RIM-COD-SHA1 已按新 .cn.cod 字节精确重算
            out_jad = ap.out_path.with_suffix(".jad")
            self.assertTrue(out_jad.is_file())
            expected_sha1 = " ".join(f"{b:02x}" for b in hashlib.sha1(ap.out_path.read_bytes()).digest())
            self.assertIn(f"RIM-COD-SHA1: {expected_sha1}", out_jad.read_text(encoding="utf-8"))

            self.assertTrue(revert_game(root).ok)
            self.assertFalse(ap.out_path.exists())
            self.assertFalse(out_jad.exists())

        # 2. BlackBerry 10 / PlayBook .bar (QNX Cascades QML)
        with tempfile.TemporaryDirectory() as tmp2:
            root2 = Path(tmp2)
            bar_path = root2 / "CascadesRpg.bar"
            with zipfile.ZipFile(bar_path, "w") as zf:
                zf.writestr("META-INF/MANIFEST.MF", "Package-Name: com.example.rpg\nEntry-Point-Type: Qnx/Elf\n")
                zf.writestr(
                    "app/native/assets/main.qml",
                    'import bb.cascades 1.2\nPage { Label { text: qsTr("Enter the Shadow Realm") } }\n',
                )

            adapter2 = detect(root2)
            self.assertIsNotNone(adapter2)
            self.assertEqual(adapter2.name, "blackberry")
            entries2 = adapter2.collect(root2)
            self.assertEqual([e.english for e in entries2], ["Enter the Shadow Realm"])

            ap2 = adapter2.apply(root2, {"Enter the Shadow Realm": "踏入暗影领域"})
            self.assertTrue(ap2.ok)
            self.assertTrue(ap2.out_path.name.endswith(".cn.bar"))
            with zipfile.ZipFile(ap2.out_path, "r") as zout:
                self.assertIn('qsTr("踏入暗影领域")', zout.read("app/native/assets/main.qml").decode("utf-8"))
            self.assertTrue(revert_game(root2).ok)


if __name__ == "__main__":
    unittest.main()
