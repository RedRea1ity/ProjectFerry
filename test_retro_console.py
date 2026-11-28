"""世嘉土星 (Sega Saturn) 与 任天堂 N64 (Nintendo 64) 适配器回归测试。

全部在临时目录构造合成字节，验证：
1. Sega Saturn MODE1/2352 RAW `.bin` + `.cue`：IP.BIN 头解析、跨扇区 ISO-9660 解析、
   Shift-JIS 抽取与定长回写、黄皮书 EDC + Reed-Solomon ECC (P/Q) 重算、`.cn.cue` 生成与撤销；
2. Sega Saturn 2048 字节 `.iso` 与 `IP.BIN` 解包目录；
3. Nintendo 64 全部三种字节序（`.z64` / `.v64` / `.n64`）无损归一化与还原；
4. Nintendo 64 CIC-6102 / 6103 / 6105 / 6106 校验和（CRC1 / CRC2）重算、0x00..0x1000 引导区保护、
   Yaz0/MIO0 压缩块诊断以及 `.cn.v64` 副本回写与撤销。
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from ferrycore.adapters import (
    N64Adapter,
    SaturnAdapter,
    compute_cdrom_edc,
    compute_n64_crc,
    detect,
    fix_n64_crc,
    normalize_n64_rom,
    parse_n64_header,
    parse_saturn_disc,
    recompute_cdrom_edc_ecc,
    restore_n64_format,
)
from ferrycore.adapters.retro_console import (
    CDROM_RAW_SECTOR_SIZE,
    CDROM_SYNC,
    N64_IPL3_END,
    N64_MAGIC_Z64,
    SATURN_MAGIC,
    read_cdrom_file,
)
from ferrycore.adapters.undo import revert as revert_game
from ferrygame.pipeline import scan_game
from ferrygame.rawbin import RawBinaryAdapter
from ferrygame.tbl import encode_tbl_padded, parse_tbl, relative_search, scan_tbl_strings


def _iso_dir_record(name: bytes, lba: int, size: int, *, is_dir: bool = False) -> bytes:
    rec_len = 33 + len(name)
    if rec_len % 2:
        rec_len += 1
    rec = bytearray(rec_len)
    rec[0] = rec_len
    struct.pack_into("<I", rec, 2, lba)
    struct.pack_into(">I", rec, 6, lba)
    struct.pack_into("<I", rec, 10, size)
    struct.pack_into(">I", rec, 14, size)
    rec[25] = 0x02 if is_dir else 0x00
    rec[32] = len(name)
    rec[33 : 33 + len(name)] = name
    return bytes(rec)


def _make_saturn_disc(files: dict[str, bytes], *, raw_2352: bool = True) -> bytes:
    """构造最小合法世嘉土星镜像（Sector 0 = IP.BIN, Sector 16 = PVD, Sector 18 = RootDir, Sector 20+ = 文件）。"""
    # 先准备 2048 字节用户数据扇区列表
    sec0 = bytearray(2048)
    sec0[0x00:0x10] = SATURN_MAGIC
    sec0[0x10:0x20] = b"SEGA ENTERPRISES"
    sec0[0x20:0x2A] = b"T-99999G  "
    sec0[0x2A:0x30] = b"V1.000"
    sec0[0x30:0x38] = b"19970815"
    sec0[0x40:0x50] = b"J               "
    sec0[0x60:0x80] = b"MYSTERY SATURN DEMO".ljust(32, b" ")

    root_lba = 18
    cur_lba = 20
    file_layout: list[tuple[str, int, bytes]] = []
    for name, content in files.items():
        file_layout.append((name, cur_lba, content))
        cur_lba += max(1, (len(content) + 2047) // 2048)

    root_dir = bytearray()
    root_dir.extend(_iso_dir_record(b"\x00", root_lba, 2048, is_dir=True))
    root_dir.extend(_iso_dir_record(b"\x01", root_lba, 2048, is_dir=True))
    for name, lba, content in file_layout:
        iso_name = f"{name.upper()};1".encode("ascii")
        root_dir.extend(_iso_dir_record(iso_name, lba, len(content), is_dir=False))
    root_sec = bytes(root_dir).ljust(2048, b"\x00")

    pvd = bytearray(2048)
    pvd[0:6] = b"\x01CD001"
    pvd[6] = 1
    pvd[156:190] = _iso_dir_record(b"\x00", root_lba, 2048, is_dir=True)

    total_sectors = cur_lba + 2
    user_sectors = [b"\x00" * 2048 for _ in range(total_sectors)]
    user_sectors[0] = bytes(sec0)
    user_sectors[16] = bytes(pvd)
    user_sectors[root_lba] = root_sec
    for _, lba, content in file_layout:
        sec_n = max(1, (len(content) + 2047) // 2048)
        for i in range(sec_n):
            user_sectors[lba + i] = content[i * 2048 : (i + 1) * 2048].ljust(2048, b"\x00")

    if not raw_2352:
        return b"".join(user_sectors)

    # 封装为 Redump 标准 MODE1/2352（含 Sync + MSF + Mode=1 + 2048B 数据 + EDC + ECC P/Q）
    raw_out = bytearray()
    for lba, udata in enumerate(user_sectors):
        sec = bytearray(CDROM_RAW_SECTOR_SIZE)
        sec[0:12] = CDROM_SYNC
        # 简易 BCD MSF 地址（LBA + 150 预隙）
        frames = lba + 150
        m = frames // (60 * 75)
        s = (frames // 75) % 60
        f = frames % 75
        sec[12] = ((m // 10) << 4) | (m % 10)
        sec[13] = ((s // 10) << 4) | (s % 10)
        sec[14] = ((f // 10) << 4) | (f % 10)
        sec[15] = 1  # MODE 1
        sec[16:2064] = udata
        recompute_cdrom_edc_ecc(sec, mode=1)
        raw_out.extend(sec)
    return bytes(raw_out)


def _make_n64_rom(payload_after_ipl: bytes, *, fmt: str = "z64", title: bytes = b"MYSTERY N64 DEMO") -> bytes:
    """构造最小合法 N64 ROM（含正确 CRC1/CRC2），支持输出 .z64 / .v64 / .n64 三种字节序。"""
    rom = bytearray(N64_IPL3_END + max(len(payload_after_ipl), 0x4000))
    rom[0:4] = N64_MAGIC_Z64
    rom[0x20:0x34] = title.ljust(20, b" ")[:20]
    rom[0x3B:0x3F] = b"NMYJ"
    rom[0x3F] = 0x00
    # 写入固定模拟 IPL3 引导段（0x40..0x1000）
    rom[0x40:0x60] = b"MOCK_IPL3_BOOTCODE_DO_NOT_TOUCH!"
    rom[N64_IPL3_END : N64_IPL3_END + len(payload_after_ipl)] = payload_after_ipl
    fix_n64_crc(rom, cic=6102)
    return restore_n64_format(bytes(rom), fmt)


class SaturnAdapterTests(unittest.TestCase):
    def test_mode1_2352_bin_cue_roundtrip_recomputes_edc_ecc_and_reverts(self):
        sjis_line1 = "「この城の奥に、古の秘宝が眠っているという……！」".encode("cp932")
        sjis_line2 = "「油断するな、敵の気配が近づいているぞ！」".encode("cp932")
        script_blob = b"\x00\x00" + sjis_line1 + b"\x00\x00\x00\x00" + sjis_line2 + b"\x00\x00"
        disc_blob = _make_saturn_disc({"SCRIPT.BIN": script_blob}, raw_2352=True)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "saturn_game"
            root.mkdir()
            bin_path = root / "mystery_ss.bin"
            cue_path = root / "mystery_ss.cue"
            bin_path.write_bytes(disc_blob)
            cue_path.write_text(
                'FILE "mystery_ss.bin" BINARY\r\n  TRACK 01 MODE1/2352\r\n    INDEX 01 00:00:00\r\n',
                encoding="utf-8",
            )

            ad = detect(cue_path)
            self.assertIsInstance(ad, SaturnAdapter)

            probe = ad.probe(cue_path)
            self.assertTrue(probe.ready)
            self.assertEqual(probe.container, "saturn-mode1-2352")
            self.assertEqual(probe.unit_count, 2)

            proj, _ = scan_game(cue_path, save=False)
            self.assertEqual(proj.adapter, "saturn")
            self.assertEqual(len(proj.units), 2)
            self.assertGreater(proj.units[0].max_bytes, 0)

            entries = ad.collect(cue_path)
            translations = {
                entries[0].key: "「听说这座城堡深处沉睡着远古秘宝！」",
                entries[1].key: "「别大意，敌人的气息正在逼近！」",
            }
            applied = ad.apply(cue_path, translations)
            self.assertTrue(applied.ok)
            self.assertEqual(applied.applied, 2)

            out_bin = applied.out_path
            self.assertIsNotNone(out_bin)
            self.assertTrue(out_bin.is_file())
            out_cue = out_bin.with_suffix(".cue")
            self.assertTrue(out_cue.is_file())
            self.assertIn(f'FILE "{out_bin.name}" BINARY', out_cue.read_text(encoding="utf-8"))

            # 原盘一字节未动
            self.assertEqual(bin_path.read_bytes(), disc_blob)

            # 新盘可无损解析，内部 SCRIPT.BIN 包含 GBK 中文，且修改扇区 EDC + ECC 与重算完全一致
            patched_blob = out_bin.read_bytes()
            layout = parse_saturn_disc(patched_blob)
            self.assertEqual(layout.header.product_no, "T-99999G")
            ent = layout.files["SCRIPT.BIN"]
            new_script = read_cdrom_file(patched_blob, ent.lba, ent.size, layout.sector_size, layout.data_offset)
            self.assertIn("远古秘宝".encode("gbk"), new_script)
            self.assertIn("敌人的气息".encode("gbk"), new_script)

            # 校验 LBA 20 的 2352 字节扇区 EDC 与 ECC P/Q
            sec_start = ent.lba * CDROM_RAW_SECTOR_SIZE
            sec_actual = patched_blob[sec_start : sec_start + CDROM_RAW_SECTOR_SIZE]
            stored_edc = struct.unpack_from("<I", sec_actual, 2064)[0]
            self.assertEqual(stored_edc, compute_cdrom_edc(sec_actual[:2064]))
            sec_check = bytearray(sec_actual)
            recompute_cdrom_edc_ecc(sec_check, mode=1)
            self.assertEqual(bytes(sec_check), sec_actual)

            # 一键撤销删掉 .cn.bin 与 .cn.cue
            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse(out_bin.exists())
            self.assertFalse(out_cue.exists())

    def test_saturn_iso_2048_and_unpacked_ip_bin_dir(self):
        sjis_line = "「土星の夜空に輝く星を見上げよう。」".encode("cp932")
        iso_blob = _make_saturn_disc({"MSG.DAT": b"\x00\x00" + sjis_line + b"\x00\x00"}, raw_2352=False)

        with tempfile.TemporaryDirectory() as tmp:
            iso_path = Path(tmp) / "game.iso"
            iso_path.write_bytes(iso_blob)
            ad = detect(iso_path)
            self.assertIsInstance(ad, SaturnAdapter)
            probe = ad.probe(iso_path)
            self.assertTrue(probe.ready)
            self.assertEqual(probe.container, "saturn-iso-2048")


class N64AdapterTests(unittest.TestCase):
    def test_n64_byte_order_roundtrip_and_cic_crc_variants(self):
        raw_z64 = _make_n64_rom(b"HELLO WORLD!" + b"\x12\x34\x56\x78" * 32, fmt="z64")
        for fmt in ("z64", "v64", "n64"):
            converted = restore_n64_format(raw_z64, fmt)
            norm, detected = normalize_n64_rom(converted)
            self.assertEqual(detected, fmt)
            self.assertEqual(norm, raw_z64)

        # 验证不同 CIC 型号产生不同校验和（6102 vs 6103 vs 6105 vs 6106）
        crcs = {cic: compute_n64_crc(raw_z64, cic=cic) for cic in (6102, 6103, 6105, 6106)}
        self.assertEqual(len(set(crcs.values())), 4)

    def test_n64_v64_rom_extract_patch_recompute_crc_and_revert(self):
        sjis_1 = "「ハイラルの大地に新たな勇者が目覚めた！」".encode("cp932")
        sjis_2 = "「星のかけらを集めて魔王を倒すのだ！」".encode("cp932")
        payload = b"Yaz0" + b"\x00" * 16 + sjis_1 + b"\x00\x00\x00\x00" + sjis_2 + b"\x00\x00"
        v64_blob = _make_n64_rom(payload, fmt="v64", title=b"N64 QUEST DEMO")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "n64_dir"
            root.mkdir()
            rom_path = root / "quest.v64"
            rom_path.write_bytes(v64_blob)

            ad = detect(root)
            self.assertIsInstance(ad, N64Adapter)

            probe = ad.probe(root)
            self.assertTrue(probe.ready)
            self.assertEqual(probe.container, "n64 (v64)")
            self.assertEqual(probe.unit_count, 2)
            self.assertTrue(any("Yaz0" in n for n in probe.notes))

            units = ad.scan_units(root)
            self.assertEqual(len(units), 2)
            # 所有提取偏移必须严格位于 0x1000 之后，绝不触碰 IPL3 引导区
            for u in units:
                self.assertGreaterEqual(int(u.meta["offset"]), N64_IPL3_END)

            translations = {
                units[0].key: "「海拉鲁大地上新的勇者苏醒了！」",
                units[1].key: "「收集星之碎片打倒魔王吧！」",
            }
            applied = ad.apply(root, translations)
            self.assertTrue(applied.ok)
            self.assertEqual(applied.applied, 2)

            out_rom = applied.out_path
            self.assertIsNotNone(out_rom)
            self.assertTrue(out_rom.is_file())
            self.assertEqual(out_rom.suffix.lower(), ".v64")

            # 原 ROM 一字节未动
            self.assertEqual(rom_path.read_bytes(), v64_blob)

            # 新 ROM 仍为 .v64 字节序，规范化后头部 CRC1/CRC2 与重算值完全一致，且 IPL3 原封不动
            new_hdr, new_z64 = parse_n64_header(out_rom.read_bytes())
            self.assertEqual(new_hdr.byte_order, "v64")
            expected_crc1, expected_crc2 = compute_n64_crc(new_z64, cic=new_hdr.cic)
            self.assertEqual((new_hdr.crc1, new_hdr.crc2), (expected_crc1, expected_crc2))
            self.assertEqual(new_z64[0x40:0x60], b"MOCK_IPL3_BOOTCODE_DO_NOT_TOUCH!")
            self.assertIn("海拉鲁大地".encode("gbk"), new_z64[N64_IPL3_END:])
            self.assertIn("打倒魔王".encode("gbk"), new_z64[N64_IPL3_END:])

            # 撤销删掉 .cn.v64
            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse(out_rom.exists())


class ThingyTblAndRelativeSearchTests(unittest.TestCase):
    def test_custom_tbl_on_n64_rom_with_separate_cn_tbl_and_crc_recalc(self):
        """验证 N64 ROM 旁放入自定义 .tbl + _cn.tbl 后，无需改代码即可按自定义码表抽文本与回写并重算 CRC。"""
        # 构造完全非标准码表：0x10='あ', 0x11='い', 0x12='う', 0x13='え', 0x14='お', 0x15='か', /FF 结束
        src_tbl_txt = (
            "# Custom N64 table\n"
            "10=あ\n11=い\n12=う\n13=え\n14=お\n15=か\n16=き\n17=く\n"
            "*FE=\\n\n"
            "/FF=[END]\n"
        )
        cn_tbl_txt = (
            "# Custom Chinese tile table\n"
            "20=你\n21=好\n22=勇\n23=者\n24=世\n25=界\n26= \n"
            "*FE=\\n\n"
            "/FF=[END]\n"
        )
        # 两条自定义码表台词（Shift-JIS 扫描器绝对认不出来，因为全落在 0x10..0x17）
        line1 = bytes([0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0xFF])  # "あいうえおか" (6B)
        line2 = bytes([0x15, 0x16, 0x17, 0xFE, 0x10, 0x11, 0xFF])  # "かきく\nあい" (6B)
        payload = b"\x00" * 32 + line1 + b"\x00" * 16 + line2 + b"\x00" * 16
        rom_blob = _make_n64_rom(payload, fmt="z64", title=b"CUSTOM TBL N64")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom_n64"
            root.mkdir()
            rom_path = root / "secret.z64"
            rom_path.write_bytes(rom_blob)
            (root / "secret.tbl").write_text(src_tbl_txt, encoding="utf-8")
            (root / "secret_cn.tbl").write_text(cn_tbl_txt, encoding="utf-8")

            ad = detect(root)
            self.assertIsInstance(ad, N64Adapter)
            probe = ad.probe(root)
            self.assertTrue(probe.ready)
            self.assertEqual(probe.unit_count, 2)
            self.assertTrue(any("secret.tbl" in n for n in probe.notes))

            units = ad.scan_units(root)
            self.assertEqual([u.src for u in units], ["あいうえおか", "かきく\nあい"])

            applied = ad.apply(root, {units[0].key: "你好勇者", units[1].key: "世界\n你好"})
            self.assertTrue(applied.ok)
            self.assertEqual(applied.applied, 2)

            out_hdr, out_z64 = parse_n64_header(applied.out_path.read_bytes())
            # 验证已用 secret_cn.tbl 编码（0x20,0x21,0x22,0x23 + 0xFF*2 补齐）且 N64 CRC 校验有效
            self.assertIn(bytes([0x20, 0x21, 0x22, 0x23, 0xFF, 0xFF, 0xFF]), out_z64)
            self.assertIn(bytes([0x24, 0x25, 0xFE, 0x20, 0x21, 0xFF, 0xFF]), out_z64)
            self.assertEqual((out_hdr.crc1, out_hdr.crc2), compute_n64_crc(out_z64, cic=out_hdr.cic))

    def test_custom_tbl_on_rawbin_and_relative_search(self):
        """验证 RawBinaryAdapter 自动识别 ferry.tbl，并验证相对搜索能 1 秒反推出未知码表偏移。"""
        # 自定义偏移码表：'A' = 0x80, 'B' = 0x81, ..., 'Z' = 0x99
        def _enc_custom(s: str) -> bytes:
            return bytes((0x80 + ord(c) - ord("A")) if "A" <= c <= "Z" else 0x7F for c in s)

        blob = b"\x00\x11\x22" + _enc_custom("START") + b"\xFF" + b"\x00\x33" + _enc_custom("OPTION") + b"\xFF"
        hits = relative_search(blob, "START")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].offset, 3)
        self.assertEqual(hits[0].stride, 1)
        self.assertEqual(hits[0].base_code, 0x80 + ord("S") - ord("A"))
        snippet = hits[0].to_tbl_snippet()
        self.assertIn("80=A", snippet)
        self.assertIn("99=Z", snippet)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "custom_raw"
            root.mkdir()
            (root / "script.bin").write_bytes(blob)
            (root / "ferry.tbl").write_text(snippet + "\n7F= \n/FF=[END]\n", encoding="utf-8")

            ad = RawBinaryAdapter()
            probe = ad.probe(root)
            self.assertTrue(probe.ready)
            self.assertEqual(probe.unit_count, 2)
            units = ad.scan_units(root)
            self.assertEqual([u.src for u in units], ["START", "OPTION"])
            applied = ad.apply(root, {units[0].key: "PLAY", units[1].key: "CONFIG"})
            self.assertTrue(applied.ok)
            patched = (root / "script.bin").read_bytes()
            self.assertIn(_enc_custom("PLAY") + b"\xFF", patched)
            self.assertIn(_enc_custom("CONFIG") + b"\xFF", patched)


if __name__ == "__main__":
    unittest.main()
