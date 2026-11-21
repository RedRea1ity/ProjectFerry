"""RPG Maker VX Ace / RGSSAD / Marshal 单测。"""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from ferrycore.adapters import detect
from ferrycore.adapters.rpgmaker_ace import RpgMakerAceAdapter, set_pointer
from ferrycore.adapters.rgssad import RgssadArchive
from ferrycore.adapters import ruby_marshal as rm


def _pack_v3(files: dict[str, bytes], seed: int = 0x12345678) -> bytes:
    """造一个最小 RGSSAD v3，供单测用。"""
    meta_key = (seed * 9 + 3) & 0xFFFFFFFF
    # 元数据紧跟头；文件数据接在元数据后
    header = bytearray(b"RGSSAD\x00\x03")
    header += struct.pack("<I", seed)
    meta = bytearray()
    blobs: list[tuple[int, int, bytes]] = []  # offset占位, key, data
    # 先估元数据长度：每条 16+namelen，再 +4 的终止 0
    names = list(files.keys())
    meta_len = 12  # header+seed already in file; meta starts at 12
    # 实际写两遍：先算 meta 大小
    cursor = 0
    for name in names:
        name_b = name.replace("/", "\\").encode("utf-8")
        cursor += 16 + len(name_b)
    cursor += 4  # terminator
    data_offset = 12 + cursor
    pos = data_offset
    file_entries = []
    for name, raw in files.items():
        fkey = 0xABCDEF01
        # 加密内容
        enc = bytearray(len(raw))
        k = fkey
        kb = k.to_bytes(4, "little")
        j = 0
        for i in range(len(raw)):
            if j == 4:
                j = 0
                k = (k * 7 + 3) & 0xFFFFFFFF
                kb = k.to_bytes(4, "little")
            enc[i] = raw[i] ^ kb[j]
            j += 1
        name_b = name.replace("/", "\\").encode("utf-8")
        file_entries.append((pos, len(raw), fkey, name_b, bytes(enc)))
        pos += len(raw)
    for offset, size, fkey, name_b, _enc in file_entries:
        meta += struct.pack("<I", offset ^ meta_key)
        meta += struct.pack("<I", size ^ meta_key)
        meta += struct.pack("<I", fkey ^ meta_key)
        meta += struct.pack("<I", len(name_b) ^ meta_key)
        nb = bytearray(name_b)
        kb = meta_key.to_bytes(4, "little")
        for i in range(len(nb)):
            nb[i] ^= kb[i % 4]
        meta += nb
    meta += struct.pack("<I", 0 ^ meta_key)
    body = b"".join(enc for *_rest, enc in file_entries)
    return bytes(header) + bytes(meta) + body


class MarshalRoundtripTests(unittest.TestCase):
    def test_event_command_roundtrip(self):
        sample = {
            "__class__": "RPG::EventCommand",
            "code": 401,
            "indent": 0,
            "parameters": ["Hello, Charlotte."],
        }
        back = rm.load(rm.dump(sample))
        self.assertEqual(back["parameters"][0], "Hello, Charlotte.")
        self.assertEqual(back["code"], 401)

    def test_table_userdef_roundtrip(self):
        raw = b"\x00\x01\x02\x03" * 50
        obj = {"__class__": "RPG::Table", "__userdef__": raw}
        back = rm.load(rm.dump(obj))
        self.assertEqual(back["__userdef__"], raw)

    def test_negative_int(self):
        for n in (-1, -5, -123, 0, 1, 122, 200, 401):
            self.assertEqual(rm.load(rm.dump(n)), n)


class RgssadV3Tests(unittest.TestCase):
    def test_extract_roundtrip(self):
        actors = rm.dump([
            None,
            {"__class__": "RPG::Actor", "name": "Charlotte", "description": "A girl."},
        ])
        blob = _pack_v3({"Data/Actors.rvdata2": actors})
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "Game.rgss3a"
            path.write_bytes(blob)
            arc = RgssadArchive(path)
            self.assertEqual(arc.version, 3)
            got = arc.read("Data/Actors.rvdata2")
            data = rm.load(got)
            self.assertEqual(data[1]["name"], "Charlotte")


class AceAdapterSyntheticTests(unittest.TestCase):
    def _make_game(self, root: Path, lines: list[str]) -> None:
        commands = []
        for line in lines:
            commands.append({
                "__class__": "RPG::EventCommand",
                "code": 401,
                "indent": 0,
                "parameters": [line],
            })
        commands.append({"__class__": "RPG::EventCommand", "code": 0, "indent": 0, "parameters": []})
        page = {"list": commands, "__class__": "RPG::Event::Page"}
        event = {"__class__": "RPG::Event", "id": 1, "pages": [page]}
        map_obj = {
            "__class__": "RPG::Map",
            "width": 5,
            "height": 5,
            "events": {1: event},
            "data": {"__class__": "RPG::Table", "__userdef__": b"\x00" * 20},
            "display_name": "",
            "tileset_id": 1,
            "autoplay_bgm": False,
            "bgm": {"__class__": "RPG::BGM", "name": "", "volume": 100, "pitch": 100},
            "autoplay_bgs": False,
            "bgs": {"__class__": "RPG::BGS", "name": "", "volume": 100, "pitch": 100},
            "disable_dashing": False,
            "encounter_list": [],
            "encounter_step": 30,
            "parallax_name": "",
            "parallax_loop_x": False,
            "parallax_loop_y": False,
            "parallax_sx": 0,
            "parallax_sy": 0,
            "parallax_show": False,
            "scroll_type": 0,
            "specify_battleback": False,
            "battleback1_name": "",
            "battleback2_name": "",
            "note": "",
        }
        system = {
            "__class__": "RPG::System",
            "game_title": "Synthetic Ace",
            "currency_unit": "G",
            "terms": {"__class__": "RPG::System::Terms"},
        }
        files = {
            "Data/Map001.rvdata2": rm.dump(map_obj),
            "Data/System.rvdata2": rm.dump(system),
            "Data/Actors.rvdata2": rm.dump([None]),
        }
        (root / "Game.ini").write_text("[Game]\nTitle=Synthetic Ace\n", encoding="utf-8")
        (root / "Game.exe").write_bytes(b"MZ")
        (root / "Game.rgss3a").write_bytes(_pack_v3(files))

    def test_detect_and_collect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "game"
            root.mkdir()
            self._make_game(root, ["Hello there.", "How are you?"])
            adapter = detect(root)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "rpgmaker_ace")
            report = adapter.probe(root)
            self.assertTrue(report.readable)
            self.assertTrue(report.writable)
            self.assertGreaterEqual(report.unit_count, 2)
            entries = adapter.collect(root)
            texts = {e.english for e in entries}
            self.assertIn("Hello there.", texts)
            self.assertIn("How are you?", texts)

    def test_apply_writes_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "game"
            root.mkdir()
            self._make_game(root, ["Hello there."])
            adapter = RpgMakerAceAdapter()
            entries = adapter.collect(root)
            target = next(e for e in entries if e.english == "Hello there.")
            out = Path(tmp) / "game.cn"
            report = adapter.apply(root, {target.key: "你好。"}, out=out)
            self.assertGreaterEqual(report.applied, 1)
            self.assertTrue((out / "Data" / "Map001.rvdata2").is_file())
            self.assertFalse(list(out.glob("*.rgss3a")))
            payload = rm.load((out / "Data" / "Map001.rvdata2").read_bytes())
            cmd = payload["events"][1]["pages"][0]["list"][0]
            self.assertEqual(cmd["parameters"][0], "你好。")

    def test_set_pointer_int_keys(self):
        tree = {"events": {1: {"pages": [{"list": [{"parameters": ["A"]}]}]}}}
        set_pointer(tree, "events/1/pages/0/list/0/parameters/0", "B")
        self.assertEqual(tree["events"][1]["pages"][0]["list"][0]["parameters"][0], "B")

    def test_merge_keeps_loose_only_graphics(self):
        """加密包里没有的散装图（如 Shadow.png）必须进副本。"""
        from ferrycore.adapters.rpgmaker_ace import RpgMakerAceAdapter

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "game"
            root.mkdir()
            self._make_game(root, ["Hi."])
            # 包外散装 RTP
            shadow = root / "Graphics" / "System" / "Shadow.png"
            shadow.parent.mkdir(parents=True, exist_ok=True)
            shadow.write_bytes(b"PNG_FAKE")
            dest = Path(tmp) / "out"
            RpgMakerAceAdapter()._build_playable_copy(root, dest)
            self.assertTrue((dest / "Graphics" / "System" / "Shadow.png").is_file())
            self.assertEqual((dest / "Graphics" / "System" / "Shadow.png").read_bytes(), b"PNG_FAKE")


class BinaryScriptRoundtripTests(unittest.TestCase):
    def test_binary_string_survives(self):
        payload = zlib.compress(b"Font.default_name = [\"Coder's Crux\"]\n")
        obj = [123, "Main", payload]
        back = rm.load(rm.dump(obj))
        self.assertIsInstance(back[2], (bytes, bytearray))
        self.assertEqual(zlib.decompress(back[2]), zlib.decompress(payload))

    def test_bump_size_ignores_main_process_section(self):
        """「▼ Main Process」是空分区标题，绝不能被当成 Main 写入。"""
        from ferrycore.adapters.rpgmaker_ace import _bump_font_size

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = root / "Data"
            data.mkdir()
            empty = zlib.compress(b"")
            main = zlib.compress(
                b"# Main\nFont.default_name = [\"Coder's Crux\"]\nFont.default_size = 14\n"
            )
            scripts = [
                [1, "▼ Main Process", empty],
                [2, "Main", main],
            ]
            (data / "Scripts.rvdata2").write_bytes(rm.dump(scripts))
            self.assertTrue(_bump_font_size(root, 20))
            loaded = rm.load((data / "Scripts.rvdata2").read_bytes())
            proc = zlib.decompress(loaded[0][2])
            body = zlib.decompress(loaded[1][2]).decode("utf-8")
            self.assertEqual(proc, b"")
            self.assertIn("Coder's Crux", body)
            self.assertIn("Font.default_size = 20", body)

    def test_ttf_rename_family(self):
        from ferrycore.adapters.ttf_rename import rename_family
        from ferrycore.fontcheck import check_font

        src = Path(r"C:\Windows\Fonts\simhei.ttf")
        if not src.is_file():
            self.skipTest("no simhei.ttf")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "fake.ttf"
            out.write_bytes(rename_family(src, "Coder's Crux"))
            cov = check_font(out)
            self.assertEqual(cov.family, "Coder's Crux")
            self.assertEqual(cov.level, "ok")


if __name__ == "__main__":
    unittest.main()
