"""Leaf Visual Novel System 3 (LEAFPACK / LVNS3) 适配器与大文件防爆守卫离线单测。"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from ferrycore import adapters
from ferrycore.adapters.leaf_lvns import (
    LVNS3_GLYPH_BYTES,
    LVNS3_GLYPH_COUNT,
    LeafLvnsAdapter,
    default_lvns3_glyphs,
    leaf_lzss_compress,
    leaf_lzss_decompress,
    open_iso_disc,
    parse_leafpack,
    parse_lvns3_scn,
    rebuild_leafpack,
    recover_leafpack_key,
)
from ferrycore.adapters.undo import revert as revert_game
from ferrygame.rawbin import RawBinaryAdapter, candidate_files


def _build_synthetic_scn(messages: list[bytes]) -> bytes:
    """构造标准双块（block0 脚本 + block1 文本）LVNS3 `.SCN` 二进制。"""
    blk0_raw = b"\x03\x00\x0a\x00\x0b\x00\x13\x00\x14\x00\x20\x26\x32\xf0\x28\x00\x00\x00\x00\x00"
    blk0_comp = leaf_lzss_compress(blk0_raw)
    blk0 = bytearray(struct.pack("<I", len(blk0_raw)) + blk0_comp)
    blk0.extend(b"\x00" * ((-len(blk0)) % 16))

    header_len = 2 + len(messages) * 2
    offs = []
    body = bytearray()
    for m in messages:
        offs.append(header_len + len(body))
        body.extend(m)
    blk1_raw = bytearray(struct.pack("<H", len(messages)))
    for o in offs:
        blk1_raw.extend(struct.pack("<H", o))
    blk1_raw.extend(body)
    blk1_comp = leaf_lzss_compress(bytes(blk1_raw))
    blk1 = bytearray(struct.pack("<I", len(blk1_raw)) + blk1_comp)
    blk1.extend(b"\x00" * ((-len(blk1)) % 16))

    b0_para = 1
    b1_para = b0_para + len(blk0) // 16
    hdr = struct.pack("<8H", b0_para, b1_para, 0, 0, 0, 0, 0, 0)
    return hdr + bytes(blk0) + bytes(blk1)


def _encode_lvns_chars(text: str, glyphs: list[str]) -> bytes:
    c2g = {ch: i + 1 for i, ch in enumerate(glyphs) if ch != "\uFFFD"}
    out = bytearray()
    for ch in text:
        gid = c2g[ch]
        out.append((gid >> 8) & 0xFF)
        out.append(gid & 0xFF)
    return bytes(out)


def _build_synthetic_mdf(files: dict[str, bytes], *, sector_size: int = 2448, data_offset: int = 16) -> bytes:
    """构造最小合法 Alcohol 120% 2448B/扇区 ISO-9660 `.mdf` 镜像用于离线测试。"""
    # LBA 16 = PVD, LBA 17 = Root Dir, LBA 18+ = files
    dir_buf = bytearray()
    cur_lba = 18
    file_chunks: list[tuple[int, bytes]] = []
    for name, blob in files.items():
        ident = f"{name.upper()};1".encode("ascii")
        rec_len = 33 + len(ident) + (0 if len(ident) % 2 == 1 else 1)
        rec = bytearray(rec_len)
        rec[0] = rec_len
        struct.pack_into("<I", rec, 2, cur_lba)
        struct.pack_into(">I", rec, 6, cur_lba)
        struct.pack_into("<I", rec, 10, len(blob))
        struct.pack_into(">I", rec, 14, len(blob))
        rec[25] = 0
        rec[32] = len(ident)
        rec[33 : 33 + len(ident)] = ident
        dir_buf.extend(rec)
        file_chunks.append((cur_lba, blob))
        cur_lba += max(1, (len(blob) + 2047) // 2048)

    total_sectors = cur_lba + 2
    img = bytearray(total_sectors * sector_size)

    def _write_sector(lba: int, data2048: bytes) -> None:
        base = lba * sector_size + data_offset
        img[base : base + len(data2048)] = data2048

    pvd = bytearray(2048)
    pvd[0:6] = b"\x01CD001"
    pvd[40:48] = b"TO_HEART"
    struct.pack_into("<I", pvd, 158, 17)
    struct.pack_into("<I", pvd, 166, len(dir_buf))
    _write_sector(16, bytes(pvd))
    _write_sector(17, bytes(dir_buf))

    for start_lba, blob in file_chunks:
        for i in range(0, len(blob), 2048):
            _write_sector(start_lba + i // 2048, blob[i : i + 2048])

    return bytes(img)


class LeafLvnsTests(unittest.TestCase):
    def test_leafpack_key_recovery_and_roundtrip(self):
        key = bytes.fromhex("d1586a569a13a5f77c3e74")
        entries = [(f"{i:04X}.SCN", f"payload-{i}-".encode("ascii") * (i + 3)) for i in range(12)]
        pak = rebuild_leafpack(entries, key)
        self.assertEqual(recover_leafpack_key(pak), key)
        parsed = parse_leafpack(pak)
        self.assertEqual(parsed.key, key)
        self.assertEqual(parsed.entries, entries)

    def test_leaf_lzss_roundtrip_varied_payloads(self):
        samples = [
            b"AAAA" * 100 + b"BCDEFG" * 50 + bytes(range(256)),
            b"\x00\x01\x00\x02" * 200 + b"\xb2\xb0\xaf",
            b"hello world",
        ]
        for raw in samples:
            comp = leaf_lzss_compress(raw)
            dec = leaf_lzss_decompress(comp, len(raw))
            self.assertEqual(dec, raw)

    def test_lvns3_dir_collect_writeback_font_patch_and_undo(self):
        glyphs = default_lvns3_glyphs()
        self.assertEqual(len(glyphs), LVNS3_GLYPH_COUNT)
        key = bytes.fromhex("d1586a569a13a5f77c3e74")

        msg0 = b"\xaf"
        msg1 = (
            b"\x00\x00"
            + _encode_lvns_chars("気がつけば春。", glyphs)
            + b"\xb2\xb0\x00\x00"
            + _encode_lvns_chars("おはよう、", glyphs)
            + bytes(range(0xD8, 0xDE))  # {LV_FN}
            + _encode_lvns_chars("ちゃん！", glyphs)
            + b"\xb3\xaf"
        )
        scn_blob = _build_synthetic_scn([msg0, msg1])
        scn_items = [(f"{i:04X}.SCN", scn_blob if i == 1 else _build_synthetic_scn([b"\xaf"])) for i in range(12)]
        scn_pak = rebuild_leafpack(scn_items, key)
        knj_blank = b"\x00" * (LVNS3_GLYPH_COUNT * LVNS3_GLYPH_BYTES)
        dat_items = [("KNJ_ALL.KNJ", knj_blank)] + [(f"D{i:03d}.DAT", b"DATA" * 8) for i in range(10)]
        dat_pak = rebuild_leafpack(dat_items, key)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ToHeart"
            root.mkdir()
            (root / "LVNS3SCN.PAK").write_bytes(scn_pak)
            (root / "LVNS3DAT.PAK").write_bytes(dat_pak)

            ad = adapters.detect(root)
            self.assertIsInstance(ad, LeafLvnsAdapter)
            pr = ad.probe(root)
            self.assertTrue(pr.ready)
            self.assertEqual(pr.unit_count, 2)

            entries = ad.collect(root)
            self.assertEqual([e.english for e in entries], ["気がつけば春。", "おはよう、{LV_FN}ちゃん！"])

            # 译文刻意包含不在日文字表中的简体字（回、过、这、风、欢、迎）验证动态字模分配 + 点阵写回
            tl = {
                entries[0].key: "回过神来已是春天。",
                entries[1].key: "早安，{LV_FN}！这微风真舒服。",
            }
            rep = ad.apply(root, tl)
            self.assertTrue(rep.ok)
            self.assertEqual(rep.applied, 2)
            self.assertTrue((root / "ferry_lvns3.tbl").is_file())

            # 重读必须 100% 还原简体中文与 {LV_FN} 占位符
            entries2 = ad.collect(root)
            self.assertEqual([e.english for e in entries2], [tl[entries[0].key], tl[entries[1].key]])

            # KNJ_ALL.KNJ 应被写入非空 24x24 点阵，总大小不变
            dat_after = dict(parse_leafpack((root / "LVNS3DAT.PAK").read_bytes()).entries)["KNJ_ALL.KNJ"]
            self.assertEqual(len(dat_after), len(knj_blank))
            self.assertNotEqual(dat_after, knj_blank)

            # 一键撤销必须无损恢复全部文件
            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertEqual((root / "LVNS3SCN.PAK").read_bytes(), scn_pak)
            self.assertEqual((root / "LVNS3DAT.PAK").read_bytes(), dat_pak)
            self.assertFalse((root / "ferry_lvns3.tbl").exists())

    def test_lvns3_mdf_disc_streaming_and_non_destructive_patch_export(self):
        glyphs = default_lvns3_glyphs()
        key = bytes.fromhex("d1586a569a13a5f77c3e74")
        msg1 = b"\x00\x00" + _encode_lvns_chars("季節はいつも通り過ぎていく。", glyphs) + b"\xb3\xaf"
        scn_items = [(f"{i:04X}.SCN", _build_synthetic_scn([msg1]) if i == 1 else _build_synthetic_scn([b"\xaf"])) for i in range(12)]
        scn_pak = rebuild_leafpack(scn_items, key)
        knj_blank = b"\x00" * (LVNS3_GLYPH_COUNT * LVNS3_GLYPH_BYTES)
        dat_pak = rebuild_leafpack([("KNJ_ALL.KNJ", knj_blank)] + [(f"X{i:03d}.DAT", b"1234" * 4) for i in range(10)], key)

        mdf_bytes = _build_synthetic_mdf({"LVNS3SCN.PAK": scn_pak, "LVNS3DAT.PAK": dat_pak})
        with tempfile.TemporaryDirectory() as tmp:
            mdf = Path(tmp) / "TO_HEART.mdf"
            mds = Path(tmp) / "TO_HEART.mds"
            mdf.write_bytes(mdf_bytes)
            mds.write_bytes(b"MEDIA DESCRIPTOR\x01\x03")

            # 传 .mds 或 .mdf 都能流式识别
            disc = open_iso_disc(mds)
            self.assertIsNotNone(disc)
            assert disc is not None
            self.assertEqual(disc.sector_size, 2448)

            ad = adapters.detect(mds)
            self.assertIsInstance(ad, LeafLvnsAdapter)
            entries = ad.collect(mdf)
            self.assertEqual([e.english for e in entries], ["季節はいつも通り過ぎていく。"])

            out_dir = Path(tmp) / "patch_out"
            rep = ad.apply(mdf, {entries[0].key: "季节总是悄然流逝。"}, out=out_dir)
            self.assertTrue(rep.ok)
            # 原 .mdf 一字节未动
            self.assertEqual(mdf.read_bytes(), mdf_bytes)
            # 导出的独立补丁目录可直接重读出简体中文
            self.assertEqual([e.english for e in ad.collect(out_dir)], ["季节总是悄然流逝。"])

    def test_rawbin_blocks_disc_images_and_oversized_single_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            mdf = Path(tmp) / "BIG_DISC.mdf"
            mdf.write_bytes(b"\x00" * 1024)
            self.assertEqual(candidate_files(mdf), [])
            pr = RawBinaryAdapter().probe(mdf)
            self.assertFalse(pr.ready)
            self.assertTrue(any("光盘" in n for n in pr.notes))


if __name__ == "__main__":
    unittest.main()
