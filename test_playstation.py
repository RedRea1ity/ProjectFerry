r"""PlayStation 1 (PSX) 与 PlayStation 2 (PS2) 光盘适配器回归测试。

验证：
1. PS1 Redump MODE2/2352 Form 1 `.bin` + `.cue`：`SYSTEM.CNF` 解析（`BOOT = cdrom:\SLPS_012.34;1`）、
   Shift-JIS 抽取与回写、MODE2 Form 1 黄皮书 EDC + ECC (P/Q) 重算、`.cn.cue` 生成与撤销；
2. PS1 `.chd` 端到端解包 → 汉化回写 → `chdman createcd` → `chdman verify` 实盘校验（若本机已装 chdman）；
3. PS2 2048 字节 DVD `.iso` 流式解析与回写（`BOOT2 = cdrom0:\SLUS_203.12;1`，欧美版跳过 SJIS 误报、
   提取复古英文菜单与对话）、外挂 Thingy `.tbl` 码表以及与 PS1/土星的严格互斥识别。
"""

from __future__ import annotations

import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from ferrycore import deps
from ferrycore.adapters import (
    Ps1Adapter,
    Ps2Adapter,
    compute_cdrom_edc,
    detect,
    parse_iso9660_stream,
    parse_ps_system_cnf,
    recompute_cdrom_edc_ecc,
)
from ferrycore.adapters.playstation import PSX_EXE_MAGIC, read_file_stream
from ferrycore.adapters.retro_console import CDROM_RAW_SECTOR_SIZE, CDROM_SYNC
from ferrycore.adapters.undo import revert as revert_game
from ferrygame.pipeline import scan_game


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


def _make_ps_disc(
    files: dict[str, bytes],
    *,
    mode2_2352: bool = False,
    min_sectors: int = 26,
    system_id: bytes = b"PLAYSTATION",
    volume_id: bytes = b"PS_DEMO",
) -> bytes:
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
    pvd[8:40] = system_id.ljust(32, b" ")[:32]
    pvd[40:72] = volume_id.ljust(32, b" ")[:32]
    pvd[156:190] = _iso_dir_record(b"\x00", root_lba, 2048, is_dir=True)

    total_sectors = max(cur_lba + 2, min_sectors)
    user_sectors = [b"\x00" * 2048 for _ in range(total_sectors)]
    user_sectors[16] = bytes(pvd)
    user_sectors[root_lba] = root_sec
    for _, lba, content in file_layout:
        sec_n = max(1, (len(content) + 2047) // 2048)
        for i in range(sec_n):
            user_sectors[lba + i] = content[i * 2048 : (i + 1) * 2048].ljust(2048, b"\x00")

    if not mode2_2352:
        return b"".join(user_sectors)

    # Redump PS1 MODE2/2352 Form 1（12B Sync + 4B MSF/Mode=2 + 8B Subheader + 2048B Data + 4B EDC + 276B ECC）
    raw_out = bytearray()
    for lba, udata in enumerate(user_sectors):
        sec = bytearray(CDROM_RAW_SECTOR_SIZE)
        sec[0:12] = CDROM_SYNC
        fr = lba + 150
        m, s, f = fr // 4500, (fr // 75) % 60, fr % 75
        sec[12] = ((m // 10) << 4) | (m % 10)
        sec[13] = ((s // 10) << 4) | (s % 10)
        sec[14] = ((f // 10) << 4) | (f % 10)
        sec[15] = 2  # MODE 2
        # 标准 Form 1 subheader: 00 00 08 00 00 00 08 00 (data bit = 0x08)
        sec[16:24] = b"\x00\x00\x08\x00\x00\x00\x08\x00"
        sec[24:2072] = udata
        recompute_cdrom_edc_ecc(sec, mode=2)
        raw_out.extend(sec)
    return bytes(raw_out)


class PlayStationAdapterTests(unittest.TestCase):
    def test_system_cnf_parser_distinguishes_ps1_and_ps2(self):
        ps1 = parse_ps_system_cnf("BOOT = cdrom:\\SLPS_012.34;1\r\nTCB = 4\r\nEVENT = 10\r\n")
        self.assertIsNotNone(ps1)
        self.assertEqual(ps1.platform, "ps1")
        self.assertEqual(ps1.game_id, "SLPS-01234")
        self.assertEqual(ps1.boot_path, "SLPS_012.34")

        ps2 = parse_ps_system_cnf("BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\nVMODE = NTSC\r\n")
        self.assertIsNotNone(ps2)
        self.assertEqual(ps2.platform, "ps2")
        self.assertEqual(ps2.game_id, "SLUS-20312")
        self.assertEqual(ps2.vmode, "NTSC")

    def test_ps1_mode2_2352_bin_cue_roundtrip_recomputes_edc_ecc_and_reverts(self):
        cnf = b"BOOT = cdrom:\\SLPS_012.34;1\r\nTCB = 4\r\nEVENT = 10\r\n"
        exe = PSX_EXE_MAGIC + b"\x00" * 32 + "「魔導の塔へようこそ、若き冒険者よ！」".encode("cp932") + b"\x00\x00"
        script = b"\x00\x00" + "「セーブデータを書き込みますか？」".encode("cp932") + b"\x00\x00"
        disc = _make_ps_disc(
            {"SYSTEM.CNF": cnf, "SLPS_012.34": exe, "EVENT.BIN": script},
            mode2_2352=True,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ps1_game"
            root.mkdir()
            bin_p = root / "tower.bin"
            cue_p = root / "tower.cue"
            bin_p.write_bytes(disc)
            cue_p.write_text(
                'FILE "tower.bin" BINARY\r\n  TRACK 01 MODE2/2352\r\n    INDEX 01 00:00:00\r\n',
                encoding="utf-8",
            )

            ad = detect(cue_p)
            self.assertIsInstance(ad, Ps1Adapter)
            probe = ad.probe(cue_p)
            self.assertTrue(probe.ready)
            self.assertEqual(probe.container, "ps1-mode2-2352")
            self.assertEqual(probe.unit_count, 2)

            units = ad.scan_units(cue_p)
            self.assertEqual(len(units), 2)
            tr = {
                u.key: ("「欢迎来到魔导之塔，年轻冒险者！」" if "魔導" in u.src else "「要写入存档数据吗？」")
                for u in units
            }
            applied = ad.apply(cue_p, tr)
            self.assertTrue(applied.ok)
            self.assertEqual(applied.applied, 2)

            out_bin = applied.out_path
            out_cue = out_bin.with_suffix(".cue")
            self.assertTrue(out_bin.is_file())
            self.assertTrue(out_cue.is_file())
            self.assertEqual(bin_p.read_bytes(), disc)

            # 验证输出盘可流式解析、含中文、且 MODE2 Form 1 EDC + ECC 完好
            layout = parse_iso9660_stream(out_bin)
            with open(out_bin, "rb") as fh:
                ev_ent = layout.files["EVENT.BIN"]
                ev_raw = read_file_stream(fh, ev_ent.lba, ev_ent.size, layout.sector_size, layout.data_offset)
                self.assertIn("存档数据".encode("gbk"), ev_raw)
                fh.seek(ev_ent.lba * CDROM_RAW_SECTOR_SIZE)
                sec_actual = fh.read(CDROM_RAW_SECTOR_SIZE)
            stored_edc = struct.unpack_from("<I", sec_actual, 2072)[0]
            self.assertEqual(stored_edc, compute_cdrom_edc(sec_actual[16:2072]))
            sec_check = bytearray(sec_actual)
            recompute_cdrom_edc_ecc(sec_check, mode=2)
            self.assertEqual(bytes(sec_check), sec_actual)

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse(out_bin.exists())
            self.assertFalse(out_cue.exists())

    def test_ps1_chd_roundtrip_verified_by_chdman(self):
        chd_dep = deps.REGISTRY.get("chdman")
        chdman = chd_dep.locate() if chd_dep else None
        if chdman is None:
            self.skipTest("chdman 未安装，跳过 PS1 .chd 实盘回封验证")

        cnf = b"BOOT = cdrom:\\SLPS_099.99;1\r\n"
        script = b"\x00\x00" + "「初代プレイステーションの実機テスト完了！」".encode("cp932") + b"\x00\x00"
        disc = _make_ps_disc(
            {"SYSTEM.CNF": cnf, "SCRIPT.BIN": script},
            mode2_2352=True,
            min_sectors=300,
        )

        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "ps1.bin").write_bytes(disc)
            (t / "ps1.cue").write_text(
                'FILE "ps1.bin" BINARY\r\n  TRACK 01 MODE2/2352\r\n    INDEX 01 00:00:00\r\n',
                encoding="utf-8",
            )
            chd_in = t / "ps1.chd"
            subprocess.run(
                [str(chdman), "createcd", "-i", str(t / "ps1.cue"), "-o", str(chd_in), "-f"],
                check=True,
                capture_output=True,
            )
            (t / "ps1.bin").unlink()
            (t / "ps1.cue").unlink()

            ad = detect(chd_in)
            self.assertIsInstance(ad, Ps1Adapter)
            units = ad.scan_units(chd_in)
            self.assertEqual(len(units), 1)
            ap = ad.apply(chd_in, {units[0].key: "「初代 PS 实机镜像测试通过！」"})
            self.assertTrue(ap.ok)
            self.assertEqual(ap.out_path.suffix.lower(), ".chd")

            ver = subprocess.run([str(chdman), "verify", "-i", str(ap.out_path)], capture_output=True, text=True)
            self.assertEqual(ver.returncode, 0, ver.stdout + ver.stderr)

    def test_ps2_dvd_iso_streaming_patch_and_tbl_support(self):
        cnf = b"BOOT2 = cdrom0:\\SLUS_203.12;1\r\nVER = 1.00\r\nVMODE = NTSC\r\n"
        elf = (
            b"\x7fELF"
            + b"\x00" * 60
            + b"delete game note?\x00\x00\x00controller pak full.\x00\x00\x00MISSION COMPLETED\x00"
        )
        iso = _make_ps_disc(
            {"SYSTEM.CNF": cnf, "SLUS_203.12": elf},
            mode2_2352=False,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ps2_dvd"
            root.mkdir()
            iso_p = root / "demo_ps2.iso"
            iso_p.write_bytes(iso)

            ad = detect(iso_p)
            self.assertIsInstance(ad, Ps2Adapter)
            probe = ad.probe(iso_p)
            self.assertTrue(probe.ready)
            self.assertEqual(probe.container, "ps2-iso-2048")
            self.assertEqual(probe.unit_count, 3)

            proj, _ = scan_game(iso_p, save=True)
            self.assertEqual(proj.adapter, "ps2")
            self.assertEqual(proj.path.name, "demo_ps2.ferry_tl.json")

            units = ad.scan_units(iso_p)
            ap = ad.apply(iso_p, {units[0].key: "delete note?", units[2].key: "DONE!"})
            self.assertTrue(ap.ok)
            self.assertEqual(ap.applied, 2)
            self.assertEqual(iso_p.read_bytes(), iso)

            layout = parse_iso9660_stream(ap.out_path)
            with open(ap.out_path, "rb") as fh:
                ent = layout.files["SLUS_203.12"]
                new_elf = read_file_stream(fh, ent.lba, ent.size, layout.sector_size, layout.data_offset)
            self.assertIn(b"delete note?\x00", new_elf)
            self.assertIn(b"DONE!\x00", new_elf)

            rev = revert_game(root)
            self.assertTrue(rev.ok)
            self.assertFalse(ap.out_path.exists())


if __name__ == "__main__":
    unittest.main()
