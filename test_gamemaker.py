"""GameMaker (GMS) 适配器测试。

自造的 data.win 一律**手写字节**，不调用被测代码里的 build_strg ——
拿被测函数验证被测函数等于什么都没验。
"""

from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from ferrycore.adapters import GameMakerAdapter


# ---- 手写构造 data.win ----


def chunk(cid: bytes, data: bytes) -> bytes:
    return cid + struct.pack("<I", len(data)) + data


def strg_with_ptr(strings: list[str]) -> bytes:
    """count | ptr[count] | 串……  指针是相对本 chunk 数据区起点的偏移。"""
    bodies = [s.encode("utf-8") + b"\x00" for s in strings]
    n = len(strings)
    head = 4 + 4 * n
    ptrs, cur = [], head
    for b in bodies:
        ptrs.append(cur)
        cur += len(b)
    return struct.pack("<I", n) + struct.pack("<%dI" % n, *ptrs) + b"".join(bodies)


def strg_no_ptr(strings: list[str]) -> bytes:
    return struct.pack("<I", len(strings)) + b"".join(s.encode("utf-8") + b"\x00" for s in strings)


def data_win(chunks: list[bytes]) -> bytes:
    body = b"GEN8" + b"".join(chunks)
    return b"FORM" + struct.pack(">I", len(body)) + body


TEXTR = b"\x11" * 32          # 冒充 STRG 后面的 chunk，用来验证平移
GAME_STRINGS = [
    "spr_player",              # 资源名，不该被抽
    "Hello, traveler.",        # 该抽
    "Yes",                     # 短选项，该抽
    "obj_door",                # 资源名，不该抽
    "第一行#第二行",            # 带软换行
    "",                        # 空串，不该抽
]


def make(root: Path, strings=None, with_ptr=True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    strings = GAME_STRINGS if strings is None else strings
    body = strg_with_ptr(strings) if with_ptr else strg_no_ptr(strings)
    blob = data_win([chunk(b"STRG", body), chunk(b"TXTR", TEXTR)])
    (root / "data.win").write_bytes(blob)
    (root / "game.exe").write_bytes(b"MZ" + b"\x00" * 200)
    return root


class GameMakerDetectTests(unittest.TestCase):
    def test_probe_finds_data_win(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make(Path(tmp) / "game")
            probe = GameMakerAdapter().probe(root)
            self.assertEqual(probe.engine, "GameMaker Studio")
            self.assertTrue(probe.readable)
            self.assertTrue(probe.writable)
            self.assertEqual(probe.container, "data.win")
            self.assertTrue(probe.unicode_engine)

    def test_renamed_data_win_still_found(self):
        """有一半的 GM 游戏导出后它叫 game.win。认签名，不认名字。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "game"
            root.mkdir()
            body = strg_with_ptr(GAME_STRINGS)
            (root / "game.win").write_bytes(data_win([chunk(b"STRG", body)]))
            probe = GameMakerAdapter().probe(root)
            self.assertEqual(probe.engine, "GameMaker Studio")

    def test_no_data_win_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "empty"
            root.mkdir()
            (root / "game.exe").write_bytes(b"MZ" + b"\x00" * 200)
            probe = GameMakerAdapter().probe(root)
            self.assertFalse(probe.readable)
            self.assertFalse(probe.writable)


class GameMakerCollectTests(unittest.TestCase):
    def test_extracts_text_and_skips_resource_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make(Path(tmp) / "game")
            texts = [e.english for e in GameMakerAdapter().collect(root)]
            self.assertIn("Hello, traveler.", texts)
            self.assertIn("Yes", texts)
            self.assertIn("第一行#第二行", texts)
            self.assertNotIn("spr_player", texts)
            self.assertNotIn("obj_door", texts)
            self.assertNotIn("", texts)

    def test_keys_are_strg_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make(Path(tmp) / "game")
            entries = GameMakerAdapter().collect(root)
            self.assertEqual(entries[0].key, "strg#1")      # "Hello, traveler."
            self.assertEqual(entries[0].json_pointer, "1")

    def test_strings_without_pointer_table(self):
        """有些 GMS 版本的 STRG 没有指针数组，按顺序读。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make(Path(tmp) / "game", with_ptr=False)
            probe = GameMakerAdapter().probe(root)
            self.assertTrue(probe.readable)
            texts = [e.english for e in GameMakerAdapter().collect(root)]
            self.assertIn("Hello, traveler.", texts)


class GameMakerApplyTests(unittest.TestCase):
    def test_write_back_and_reread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make(Path(tmp) / "game")
            adapter = GameMakerAdapter()
            report = adapter.apply(root, {"strg#1": "你好，旅人。", "strg#2": "是"})
            self.assertEqual(report.applied, 2)
            self.assertTrue(report.in_place)

            strings = adapter._strings_of(root)
            self.assertEqual(strings[1], "你好，旅人。")
            self.assertEqual(strings[2], "是")
            self.assertEqual(strings[0], "spr_player")   # 没动的原样

    def test_form_size_stays_consistent(self):
        """变长以后 FORM 头里的总长必须跟着改，否则 GM 读不到后面的 chunk。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make(Path(tmp) / "game")
            adapter = GameMakerAdapter()
            before = (root / "data.win").stat().st_size
            adapter.apply(root, {"strg#1": "这是一句比原文长很多的中文译文。"})
            blob = (root / "data.win").read_bytes()
            declared = struct.unpack_from(">I", blob, 4)[0]
            self.assertEqual(declared + 8, len(blob))
            self.assertGreater(len(blob), before)

    def test_following_chunk_shifted_intact(self):
        """STRG 后面还有 TXTR，平移过去内容必须一字节不差。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make(Path(tmp) / "game")
            adapter = GameMakerAdapter()
            adapter.apply(root, {"strg#1": "一句话。"})
            blob = (root / "data.win").read_bytes()
            idx = blob.find(b"TXTR")
            self.assertGreater(idx, 0)
            size = struct.unpack_from("<I", blob, idx + 4)[0]
            self.assertEqual(size, len(TEXTR))
            self.assertEqual(blob[idx + 8: idx + 8 + size], TEXTR)

    def test_verify_reports_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make(Path(tmp) / "game")
            adapter = GameMakerAdapter()
            adapter.apply(root, {"strg#1": "改写后的句子。"})
            result = adapter.verify(root)
            self.assertTrue(result.ok, result.notes)

    def test_rejects_when_hash_count_differs(self):
        """GameMaker 用 # 当软换行，数量对不上版面会塌。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make(Path(tmp) / "game")
            adapter = GameMakerAdapter()
            report = adapter.apply(root, {"strg#4": "丢了换行符的译文"})
            self.assertEqual(report.applied, 0)
            self.assertTrue(any("#" in c for c in report.conflicts))

    def test_missing_translation_counted_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make(Path(tmp) / "game")
            adapter = GameMakerAdapter()
            report = adapter.apply(root, {"strg#1": "只翻一条"})
            self.assertEqual(report.applied, 1)
            self.assertGreater(report.missed, 0)
            strings = adapter._strings_of(root)
            self.assertEqual(strings[2], "Yes")   # 没翻的保持原样


class GameMakerRealSampleTests(unittest.TestCase):
    """真样本只在它存在时跑。CI 上没有，所以必须 skip。"""

    SAMPLE = Path(r"C:\Users\zetad\Desktop\Game\魔怔傻逼游戏\神秘宝可梦改版")

    def test_gm8_encrypted_is_refused_not_misread(self):
        if not self.SAMPLE.is_dir():
            self.skipTest("本机没有该样本")
        probe = GameMakerAdapter().probe(self.SAMPLE)
        self.assertEqual(probe.engine, "GameMaker 8")
        self.assertFalse(probe.readable)
        self.assertFalse(probe.writable)
        joined = " ".join(probe.notes)
        self.assertIn("加密", joined)


class GameMakerLanguageTableTests(unittest.TestCase):
    """外挂 CSV 语言表 —— 真游戏里最常见的一种形态。

    实测样本 Project Basement Dating：Keys | English | russian | spanish |
    Simplified Chinese，3027 条原文里 3021 条已经有译文。
    """

    HEADER = "Keys,English,russian,spanish,Simplified Chinese\r\n"
    ROWS = (
        "TITLE,Title,,,\r\n"
        "SECRET,Secret line,,,\r\n"
        "DONE,Already done,,,已完成\r\n"
        "\r\n"
        "EMPTY,,,,,\r\n"
    )

    def make(self, base: Path, bom: bool = False) -> Path:
        root = base / "gmgame"
        root.mkdir(parents=True)
        blob = (self.HEADER + self.ROWS).encode("utf-8")
        (root / "all_text_languages.csv").write_bytes(
            (b"\xef\xbb\xbf" if bom else b"") + blob
        )
        return root

    def test_finds_table_and_counts_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            probe = GameMakerAdapter().probe(root)
            self.assertEqual(probe.container, "CSV 语言表")
            self.assertTrue(probe.writable)
            # TITLE / SECRET 待翻，DONE 已有译文，EMPTY 没原文
            self.assertEqual(probe.unit_count, 2)

    def test_collect_skips_already_translated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            texts = [e.english for e in GameMakerAdapter().collect(root)]
            self.assertEqual(texts, ["Title", "Secret line"])

    def test_apply_does_not_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            adapter = GameMakerAdapter()
            report = adapter.apply(
                root, {"Title": "标题", "Secret line": "秘密", "Already done": "越权覆盖"}
            )
            self.assertEqual(report.applied, 2)
            body = (root / "all_text_languages.csv").read_text("utf-8")
            self.assertIn("已完成", body)
            self.assertNotIn("越权覆盖", body)
            self.assertIn("标题", body)

    def test_no_bom_stays_without_bom(self):
        """`utf-8-sig` 对无 BOM 的文件也能解码，照它回写会凭空加个 BOM，
        表头第一个字段变成 \\ufeffKeys，游戏按列名匹配就全废。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp), bom=False)
            GameMakerAdapter().apply(root, {"Title": "标题"})
            raw = (root / "all_text_languages.csv").read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))

    def test_bom_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp), bom=True)
            GameMakerAdapter().apply(root, {"Title": "标题"})
            raw = (root / "all_text_languages.csv").read_bytes()
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))

    def test_untouched_rows_are_byte_identical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            before = (root / "all_text_languages.csv").read_bytes().split(b"\n")
            GameMakerAdapter().apply(root, {"Title": "标题"})
            after = (root / "all_text_languages.csv").read_bytes().split(b"\n")
            self.assertEqual(len(before), len(after))
            changed = [i for i in range(len(before)) if before[i] != after[i]]
            self.assertEqual(len(changed), 1, f"只有目标行该变，实际变了 {changed}")

    def test_backup_dir_is_not_mistaken_for_game_table(self):
        """`rglob('*.csv')` 会扫到 .ferry-backup 里的备份，而 `.` 排序靠前。
        漏掉这一条的话，译文全写进备份，游戏一个字没变却报告成功。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            adapter = GameMakerAdapter()
            adapter.apply(root, {"Title": "标题"})          # 这次会生成备份
            adapter.apply(root, {"Secret line": "秘密"})     # 这次不能再被备份骗走
            body = (root / "all_text_languages.csv").read_text("utf-8")
            self.assertIn("秘密", body)

    def test_revert_restores_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            original = (root / "all_text_languages.csv").read_bytes()
            adapter = GameMakerAdapter()
            adapter.apply(root, {"Title": "标题"})
            self.assertNotEqual((root / "all_text_languages.csv").read_bytes(), original)
            from ferrycore.adapters import revert_game
            revert_game(root)
            self.assertEqual((root / "all_text_languages.csv").read_bytes(), original)


class UndoPathTests(unittest.TestCase):
    def test_hidden_prefix_is_not_stripped(self):
        """`lstrip('./')` 剥的是字符集合，会把 `.ferry-backup/x` 剥成
        `ferry-backup/x` —— 备份路径和真实路径就此混掉。"""
        from ferrycore.adapters.undo import _norm
        self.assertEqual(_norm(".ferry-backup/a.csv"), ".ferry-backup/a.csv")
        self.assertEqual(_norm("./a.csv"), "a.csv")
        self.assertEqual(_norm("sub\\a.csv"), "sub/a.csv")


if __name__ == "__main__":
    unittest.main()
