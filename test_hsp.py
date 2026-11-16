"""HSP 适配器的离线测试。

不依赖任何本机游戏：PE 导入表用假的 `ImportTable` 顶替，
剧本用临时目录里现造的 SJIS 字节。

钉死的是三条最容易默默坏掉的性质：

1. **未翻字段的字节一个都不能动。** 整行重新编码会把没翻的 SJIS 日文也按 GBK
   编一遍，原本能显示的日文全变乱码 —— 这是这条路唯一真正的危险。
2. **注释行和备份目录不许进。** 注释里的日文是给作者看的；备份目录翻一遍
   等于白烧额度，还说不清到底改了哪一份。
3. **代理挂不上就不许写回。** 中文一定乱码的汉化不是汉化。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ferrycore.adapters import hsp as ferryhsp
from ferrycore.adapters import pe as ferrype

# 带制表符缩进 + CRLF，跟真实的 HSP 剧本一致
SCRIPT = (
    "\t// 作者メモ：この場面は悲しく\r\n"
    "\tmes,0,こんにちは、世界。\r\n"
    "\tmes,1,さようなら。\r\n"
    "\tname,アレン\r\n"
    "\tbg,\"bg\\\\room.png\"\r\n"
    "\tmes,2,また会おう。\r\n"
).encode("cp932")

# 资源表：字段全是 ASCII 标识符，日文只在注释里 —— 不该被当成剧本
RESOURCE = (
    "//s0 BGV_MAX(20)\r\n"
    "1,ah_s0r0x1,ah_s0r0x1_g4s5,\r\n"
    "1,gs_s0r0x1,gs_s0r0x1_g4wh_6,\r\n"
    "0,se_click,\r\n"
).encode("cp932")


def fake_table(bits: int = 32) -> ferrype.ImportTable:
    return ferrype.ImportTable(
        machine=ferrype.IMAGE_FILE_MACHINE_I386 if bits == 32 else ferrype.IMAGE_FILE_MACHINE_AMD64,
        pe32_plus=bits == 64,
        imports={"msimg32.dll": ["GradientFill"]},
    )


def make_game(base: Path, bits: int = 32) -> Path:
    root = base / "hsp_game"
    (root / "eve").mkdir(parents=True)
    (root / "eve" / "sn1.txt").write_bytes(SCRIPT)
    (root / "gsxy").mkdir()
    (root / "gsxy" / "bgv_s0r0.csv").write_bytes(RESOURCE)
    # 备份目录：上一轮摆渡自己就建过 eve_sjis_bak
    (root / "eve_sjis_bak").mkdir()
    (root / "eve_sjis_bak" / "sn1.txt").write_bytes(SCRIPT)
    (root / "hsp3imp.dll").write_bytes(b"fake runtime")
    (root / "rai8.exe").write_bytes(b"MZ" + b"\0" * 150_000)
    return root


class FieldJudgementTests(unittest.TestCase):
    def test_japanese_text_is_translatable(self):
        self.assertTrue(ferryhsp._is_translatable("こんにちは、世界。"))
        self.assertTrue(ferryhsp._is_translatable("八星帝"))

    def test_ascii_identifiers_are_not(self):
        self.assertFalse(ferryhsp._is_translatable("ah_s0r0x1"))
        self.assertFalse(ferryhsp._is_translatable("bg\\room.png"))
        self.assertFalse(ferryhsp._is_translatable("1"))
        self.assertFalse(ferryhsp._is_translatable(""))

    def test_asset_paths_are_not_text(self):
        self.assertFalse(ferryhsp._is_translatable("音声/001.ogg"))
        self.assertFalse(ferryhsp._is_translatable("画像\\背景.bmp"))


class ScriptDetectionTests(unittest.TestCase):
    def test_script_is_recognized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "eve.txt"
            path.write_bytes(SCRIPT)
            self.assertTrue(ferryhsp._looks_like_script(path))

    def test_resource_table_is_not_a_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bgv.csv"
            path.write_bytes(RESOURCE)
            self.assertFalse(ferryhsp._looks_like_script(path))

    def test_backup_dirs_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_game(Path(tmp))
            files = ferryhsp.HspAdapter()._script_files(root)
            names = {p.relative_to(root).as_posix() for p in files}
            self.assertIn("eve/sn1.txt", names)
            self.assertNotIn("eve_sjis_bak/sn1.txt", names)

    def test_comment_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_game(Path(tmp))
            entries = ferryhsp.HspAdapter().collect(root)
            texts = [e.english for e in entries]
            self.assertIn("こんにちは、世界。", texts)
            # 注释里的日文是给作者看的
            self.assertFalse(any("作者メモ" in t for t in texts))
            # 资源路径不该被抽
            self.assertFalse(any("room.png" in t for t in texts))


class RewriteTests(unittest.TestCase):
    def test_only_target_field_changes(self):
        """核心性质：没翻的字段必须逐字节原样。"""
        wanted = {(2, 2): ("こんにちは、世界。", "你好，世界。")}
        out, applied, conflicts = ferryhsp._rewrite(SCRIPT, wanted)
        self.assertEqual(applied, 1)
        self.assertEqual(conflicts, [])

        lines = out.split(b"\n")
        # 第 2 行（下标 1）被替换成 GBK
        self.assertEqual(lines[1], b"\tmes,0," + "你好，世界。".encode("gbk") + b"\r")
        # 其余行必须与原字节完全一致 —— 包括 SJIS 的日文
        original = SCRIPT.split(b"\n")
        for index in (0, 2, 3, 4, 5):
            self.assertEqual(lines[index], original[index], f"第 {index + 1} 行被改动")

    def test_crlf_is_preserved(self):
        wanted = {(3, 2): ("さようなら。", "再见。")}
        out, applied, _ = ferryhsp._rewrite(SCRIPT, wanted)
        self.assertEqual(applied, 1)
        self.assertEqual(out.count(b"\r\n"), SCRIPT.count(b"\r\n"))
        self.assertNotIn(b"\n\n", out)

    def test_leading_indent_is_kept(self):
        raw = b"\tmes,0,   \x82\xb1\x82\xf1\r\n"
        wanted = {(1, 2): ("こん", "你好")}
        out, applied, _ = ferryhsp._rewrite(raw, wanted)
        self.assertEqual(applied, 1)
        self.assertTrue(out.startswith(b"\tmes,0,   "), out[:20])

    def test_translation_containing_comma_is_refused(self):
        wanted = {(2, 2): ("こんにちは、世界。", "你好,世界")}
        out, applied, conflicts = ferryhsp._rewrite(SCRIPT, wanted)
        self.assertEqual(applied, 0)
        self.assertTrue(any("逗号" in c for c in conflicts))
        self.assertEqual(out, SCRIPT)

    def test_mismatched_original_is_refused(self):
        """磁盘上的内容跟记录不符（文件被外部改过）就不动它。"""
        wanted = {(2, 2): ("完全に別の文", "别的句子")}
        out, applied, conflicts = ferryhsp._rewrite(SCRIPT, wanted)
        self.assertEqual(applied, 0)
        self.assertTrue(any("不符" in c for c in conflicts))
        self.assertEqual(out, SCRIPT)


class ProbeTests(unittest.TestCase):
    def test_non_hsp_folder_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "readme.txt").write_text("hi", encoding="utf-8")
            report = ferryhsp.HspAdapter().probe(root)
            self.assertFalse(report.readable)

    def test_probe_reports_units_and_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_game(Path(tmp))
            with patch.object(ferrype, "read_imports", return_value=fake_table(32)):
                report = ferryhsp.HspAdapter().probe(root)
            self.assertTrue(report.readable)
            self.assertTrue(report.writable)
            self.assertGreater(report.unit_count, 0)
            self.assertTrue(any("msimg32" in n for n in report.notes))

    def test_64bit_exe_is_not_writable(self):
        """内嵌代理是 32 位，64 位游戏挂不上 —— 挂不上就不许写回。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_game(Path(tmp))
            with patch.object(ferrype, "read_imports", return_value=fake_table(64)):
                report = ferryhsp.HspAdapter().probe(root)
            self.assertTrue(report.readable)
            self.assertFalse(report.writable)
            self.assertFalse(report.ready)
            self.assertTrue(any("64 位" in n for n in report.notes))

    def test_no_proxy_slot_blocks_writeback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_game(Path(tmp))
            table = ferrype.ImportTable(machine=ferrype.IMAGE_FILE_MACHINE_I386)
            with patch.object(ferrype, "read_imports", return_value=table):
                report = ferryhsp.HspAdapter().probe(root)
            self.assertTrue(report.readable)
            self.assertFalse(report.writable)


class ApplyRevertTests(unittest.TestCase):
    def test_apply_writes_gbk_and_deploys_dll(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_game(Path(tmp))
            adapter = ferryhsp.HspAdapter()
            with patch.object(ferrype, "read_imports", return_value=fake_table(32)):
                entries = adapter.collect(root)
                overrides = {"こんにちは、世界。": "你好，世界。", "さようなら。": "再见。",
                             "また会おう。": "后会有期。", "アレン": "艾伦"}
                translations = {e.key: overrides.get(e.english, e.english) for e in entries}
                report = adapter.apply(root, translations)

            self.assertGreater(report.applied, 0)
            self.assertTrue(report.in_place)
            # 文本写成了 GBK
            raw = (root / "eve" / "sn1.txt").read_bytes()
            self.assertIn("你好，世界。".encode("gbk"), raw)
            # 显示层 DLL 部署了，内容跟内嵌的一致
            dll = root / "msimg32.dll"
            self.assertTrue(dll.exists())
            from ferrycore.adapters import hsp_assets

            self.assertEqual(dll.read_bytes(), hsp_assets.load())
            # 撤销记录也在
            self.assertTrue((root / ".ferry-undo.json").exists())

    def test_revert_restores_text_and_removes_dll(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_game(Path(tmp))
            adapter = ferryhsp.HspAdapter()
            with patch.object(ferrype, "read_imports", return_value=fake_table(32)):
                entries = adapter.collect(root)
                translations = {e.key: overrides_for(e) for e in entries}
                adapter.apply(root, translations)
            self.assertNotEqual((root / "eve" / "sn1.txt").read_bytes(), SCRIPT)

            from ferrycore.adapters import undo as ferryundo

            report = ferryundo.revert(root)
            self.assertTrue(report.ok, report.failed)
            # 剧本逐字节回到原样
            self.assertEqual((root / "eve" / "sn1.txt").read_bytes(), SCRIPT)
            # DLL 被撤掉
            self.assertFalse((root / "msimg32.dll").exists())
            self.assertFalse((root / ".ferry-undo.json").exists())

    def test_existing_different_dll_is_not_overwritten(self):
        """游戏自带同名 DLL 时绝不覆盖 —— 分不清是谁的就别动。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_game(Path(tmp))
            (root / "msimg32.dll").write_bytes(b"the game's own dll")
            adapter = ferryhsp.HspAdapter()
            with patch.object(ferrype, "read_imports", return_value=fake_table(32)):
                entries = adapter.collect(root)
                report = adapter.apply(root, {e.key: overrides_for(e) for e in entries})
            self.assertEqual((root / "msimg32.dll").read_bytes(), b"the game's own dll")
            self.assertTrue(any("没有覆盖" in n for n in report.notes))


def overrides_for(entry) -> str:
    """给每条一个可编码成 GBK 的译文，用来验证写回通路。"""
    table = {
        "こんにちは、世界。": "你好，世界。",
        "さようなら。": "再见。",
        "また会おう。": "后会有期。",
        "アレン": "艾伦",
    }
    return table.get(entry.english, entry.english)


if __name__ == "__main__":
    unittest.main()
