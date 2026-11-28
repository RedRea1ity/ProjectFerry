"""Adventure Game Studio (AGS) 编译版适配器回归测试。

全部在临时目录构造合成字节，验证：
1. `.tra` 解析与回写（含 AGS 2.x Block 2 长度少算 NUL 的历史怪癖）；
2. `&1013 ` 语音配音编号剥离与自动补回（包括官方英文包漏写编号的条目）；
3. 原版德语/小语种查找键 `src_raw` 逐字节保留 + 独立 `Chinese.tra` + `acsetup.cfg` + `UndoLog` 撤销；
4. CLIB v15 + `ac2game.dta` 引擎版本与字体识别；
5. 无 `.tra` 的 AGS 目录由 `AgsAdapter` 给出精准诊断而不被 `rawbin` 误吞。
"""

from __future__ import annotations

import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ferrycore import deps
from ferrycore.adapters import AgsAdapter, detect, inspect_ags_archive, parse_tra, rebuild_tra
from ferrycore.adapters.ags import (
    AVIS_KEY,
    CJK_FONT_BASE_NAME,
    CLIB_MAGIC,
    CLIB_V15_KEY,
    DTA_MAGIC,
    LAUNCHER_BAT_NAME,
    TRA_MAGIC,
    avis_encrypt,
)
from ferrycore.adapters.undo import revert as revert_game


def _make_sample_tra(
    game_uid: int,
    game_name: bytes,
    pairs: list[tuple[bytes, bytes]],
    *,
    legacy_header_size_bug: bool = True,
) -> bytes:
    out = bytearray(TRA_MAGIC)
    name_nul = game_name + b"\x00"
    enc_name = avis_encrypt(name_nul)
    header_body = struct.pack("<ii", game_uid, len(enc_name)) + enc_name
    reported_size = len(header_body) - 1 if legacy_header_size_bug else len(header_body)
    out.extend(struct.pack("<ii", 2, reported_size))
    out.extend(header_body)

    dict_body = bytearray()
    for src, dst in pairs:
        es = avis_encrypt(src + b"\x00")
        ed = avis_encrypt(dst + b"\x00")
        dict_body.extend(struct.pack("<i", len(es)))
        dict_body.extend(es)
        dict_body.extend(struct.pack("<i", len(ed)))
        dict_body.extend(ed)
    dict_body.extend(struct.pack("<ii", 0, 0))
    out.extend(struct.pack("<ii", 1, len(dict_body)))
    out.extend(dict_body)

    out.extend(struct.pack("<iiiii", 3, 12, -1, -1, -1))
    out.extend(struct.pack("<ii", -1, 0))
    return bytes(out)


def _make_sample_clib_v15_exe(engine_ver: bytes = b"2.62", dta_ver: int = 27) -> bytes:
    """构造最小可执行文件 + 尾部 CLIB v15 归档（含 ac2game.dta 与 agsfnt0.ttf）。"""
    stub = b"MZ" + b"\x00" * 126
    clib_off = len(stub)

    dta = bytearray(DTA_MAGIC)
    dta.extend(struct.pack("<i", dta_ver))
    dta.extend(struct.pack("<i", len(engine_ver)))
    dta.extend(engine_ver)
    dta.extend(b"Sowjet-Unterz\xf6gersdorf".ljust(50, b"\x00"))
    dta.extend(b"\x00" * 64)

    ttf = b"\x00\x01\x00\x00" + b"\x00" * 60

    def _enc_v15(name: str) -> bytes:
        raw = name.encode("ascii").ljust(25, b"\x00")
        return bytes((b + CLIB_V15_KEY[i % len(CLIB_V15_KEY)]) & 0xFF for i, b in enumerate(raw))

    files = [("ac2game.dta", bytes(dta)), ("agsfnt0.ttf", ttf)]
    header = bytearray(CLIB_MAGIC)
    header.append(15)  # ver
    header.append(0)  # lib_idx
    header.extend(struct.pack("<i", 1))  # num_df
    header.extend(b"ac2game.ags".ljust(20, b"\x00"))
    header.extend(struct.pack("<i", len(files)))
    for fname, _ in files:
        header.extend(_enc_v15(fname))

    data_start = len(header) + len(files) * 4 * 2 + len(files)
    offsets = []
    lengths = []
    cur = data_start
    for _, content in files:
        offsets.append(cur)
        lengths.append(len(content))
        cur += len(content)

    for o in offsets:
        header.extend(struct.pack("<I", o))
    for l in lengths:
        header.extend(struct.pack("<I", l))
    header.extend(bytes([0] * len(files)))

    body = b"".join(content for _, content in files)
    tail = struct.pack("<I", clib_off) + b"CLIB\x01\x02\x03\x04SIGE"
    return stub + bytes(header) + body + tail


class AgsAdapterTests(unittest.TestCase):
    def test_tra_roundtrip_voice_preservation_and_undo(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sovund_demo"
            root.mkdir()
            (root / "suzoeg.exe").write_bytes(_make_sample_clib_v15_exe(b"2.62", 27))
            orig_cfg = b"[misc]\r\nwindowed=0\r\n[language]\r\n"
            (root / "acsetup.cfg").write_bytes(orig_cfg)

            raw_pairs = [
                ("Zündhölzer aus heimischer Edelbirke!".encode("cp1252"), b"Matches made from precious local birch."),
                ("&1013 Das kann doch wohl nicht wahr sein!".encode("cp1252"), b"&1013 I can't believe my eyes!"),
                # 模拟官方英文包漏掉 &1061 语音标签的情况：回写时仍应自动补回
                ("&1061 Der ganze Stolz!".encode("cp1252"), b"The pride of our nation!"),
                (b"", b""),
            ]
            tra_blob = _make_sample_tra(
                1076528450,
                "Sowjet-Unterzögersdorf".encode("cp1252"),
                raw_pairs,
                legacy_header_size_bug=True,
            )
            (root / "ENGLISH.tra").write_bytes(tra_blob)

            detected = detect(root)
            self.assertIsInstance(detected, AgsAdapter)

            probe = detected.probe(root)
            self.assertTrue(probe.ready)
            self.assertEqual(probe.unit_count, 3)
            self.assertFalse(probe.unicode_engine)  # AGS 2.62 为旧版 ANSI 引擎
            self.assertEqual(probe.needs, ["ags36"])

            entries = detected.collect(root)
            self.assertEqual([e.english for e in entries], [
                "Matches made from precious local birch.",
                "I can't believe my eyes!",
                "The pride of our nation!",
            ])

            translations = {
                entries[0].key: "本地珍贵白桦木制成的火柴。",
                entries[1].key: "我简直不敢相信自己的眼睛！",
                entries[2].key: "我们国家的骄傲！",
            }
            fake_ttf = Path(tmp) / "fake_cjk.ttf"
            fake_ttf.write_bytes(b"\x00\x01\x00\x00" + b"FAKECJK" * 16)
            with patch("ferrycore.fontpatch.pick_cjk_font", return_value=(fake_ttf, "FakeCJK")):
                applied = detected.apply(root, translations)
            self.assertTrue(applied.ok)
            self.assertEqual(applied.applied, 3)

            # 1) ENGLISH.tra 原封不动
            self.assertEqual((root / "ENGLISH.tra").read_bytes(), tra_blob)

            # 2) Chinese.tra 可无损解析，保留原版德语字节键，含 ext_sopts encoding=utf-8，并补回 &1013 / &1061 语音前缀
            cn_path = root / "Chinese.tra"
            self.assertTrue(cn_path.is_file())
            parsed_cn = parse_tra(cn_path.read_bytes())
            self.assertEqual(parsed_cn.game_uid, 1076528450)
            self.assertEqual(parsed_cn.ext_options.get("encoding"), "utf-8")
            self.assertEqual(parsed_cn.entries[0].src_raw, "Zündhölzer aus heimischer Edelbirke!".encode("cp1252"))
            self.assertEqual(parsed_cn.entries[0].dst_text, "本地珍贵白桦木制成的火柴。")
            self.assertEqual(parsed_cn.entries[1].dst_text, "&1013 我简直不敢相信自己的眼睛！")
            self.assertEqual(parsed_cn.entries[2].dst_text, "&1061 我们国家的骄傲！")

            # 3) acsetup.cfg 已指向 translation=Chinese，外置中文字体与启动脚本已落盘
            cfg_now = (root / "acsetup.cfg").read_text(encoding="utf-8")
            self.assertIn("translation=Chinese", cfg_now)
            self.assertTrue((root / CJK_FONT_BASE_NAME).is_file())
            self.assertTrue((root / "agsfnt0.ttf").is_file())
            bat_path = root / LAUNCHER_BAT_NAME
            self.assertTrue(bat_path.is_file())
            bat_txt = bat_path.read_text(encoding="utf-8")
            self.assertIn("--translation Chinese", bat_txt)
            self.assertIn("suzoeg.exe", bat_txt)

            # 4) 一键撤销必须删掉 Chinese.tra / 字体 / 启动脚本，并把 acsetup.cfg 还原到一字节不差
            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse(cn_path.exists())
            self.assertFalse((root / CJK_FONT_BASE_NAME).exists())
            self.assertFalse((root / "agsfnt0.ttf").exists())
            self.assertFalse(bat_path.exists())
            self.assertEqual((root / "acsetup.cfg").read_bytes(), orig_cfg)

    def test_ags36_dependency_selective_zip_extraction(self):
        """验证 ags36 依赖从大型发布包中只抽取 acwin.exe 与 SDL2.dll（忽略编辑器等大文件）。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / "AGS-3.6.3.17.zip"
            with zipfile.ZipFile(bundle, "w") as zf:
                zf.writestr("acwin.exe", b"MZ_ACWIN")
                zf.writestr("SDL2.dll", b"MZ_SDL2")
                zf.writestr("AGSEditor.exe", b"MZ_BIG_EDITOR")
                zf.writestr("Linux/lib64/libSDL2.so", b"ELF")

            base_dep = deps.REGISTRY["ags36"]
            test_dep = deps.Dependency(
                id=base_dep.id,
                name=base_dep.name,
                why=base_dep.why,
                size_mb=base_dep.size_mb,
                binaries=base_dep.binaries,
                target=root / "ags36_out",
                zip_url=bundle.as_uri(),
                extract_files=base_dep.extract_files,
            )
            ok, msg = deps.install(test_dep)
            self.assertTrue(ok, msg)
            self.assertTrue((test_dep.target / "acwin.exe").is_file())
            self.assertTrue((test_dep.target / "SDL2.dll").is_file())
            self.assertFalse((test_dep.target / "AGSEditor.exe").exists())
            self.assertFalse((test_dep.target / "Linux").exists())

    def test_ags_without_tra_is_diagnosed_not_swallowed_by_rawbin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ags_no_tra"
            root.mkdir()
            (root / "game.exe").write_bytes(_make_sample_clib_v15_exe(b"3.6.0", 50))
            (root / "acsetup.cfg").write_text("[misc]\nwindowed=1\n", encoding="utf-8")

            info = inspect_ags_archive(root)
            self.assertIsNotNone(info)
            self.assertTrue(info.unicode_native)
            self.assertEqual(info.ttf_fonts, ["agsfnt0.ttf"])

            detected = detect(root)
            self.assertIsInstance(detected, AgsAdapter)
            probe = detected.probe(root)
            self.assertFalse(probe.readable)
            self.assertEqual(probe.container, "ags-clib")


if __name__ == "__main__":
    unittest.main()
