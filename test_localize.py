"""引擎适配器与本地汉化编排的离线测试。

全部用临时目录造数据，**不依赖任何本机游戏文件** ——
本机有游戏、CI 没有，这种测试只在本地绿，等于没测。
"""

from __future__ import annotations

import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from ferrycore import adapters, localize as ferrylocalize
from ferrycore.adapters import container as ferrycontainer
from ferrycore.adapters import tyrano as ferrytyrano

FIRST_KS = """*start
[image layer="base" page="fore" storage="haikei_.jpg"]
[playbgm storage="piano06.ogg"]
こんにちは[p]
[glink text="はじめから" target="*start"]
[jump storage="extra.ks"]
[position x="100" y="200"]
"""

EXTRA_KS = """*scene
それは、だれにも聞こえない声だった。[p]
[glink text="つづきから" target="*scene"]
"""

ORPHAN_KS = """*dead
この行は誰にも実行されない。[p]
"""


def make_folder(base: Path, name: str = "game") -> Path:
    root = base / name
    (root / "data/scenario").mkdir(parents=True)
    (root / "data/image").mkdir(parents=True)
    (root / "data/scenario/first.ks").write_text(FIRST_KS, encoding="utf-8-sig")
    (root / "data/scenario/extra.ks").write_text(EXTRA_KS, encoding="utf-8-sig")
    (root / "data/scenario/orphan.ks").write_text(ORPHAN_KS, encoding="utf-8-sig")
    (root / "data/image/hajime.png").write_bytes(b"\x89PNG\r\n\x1a\n fake")
    (root / "data/image/bg.jpg").write_bytes(b"\xff\xd8\xff fake")
    return root


def zip_tree(folder: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(folder).as_posix())
    return buffer.getvalue()


def make_exe(base: Path, name: str = "game.exe", decoy_zip: bool = False) -> Path:
    folder = make_folder(base, "payload_" + name.replace(".", "_"))
    payload = zip_tree(folder)
    prefix = b"MZ" + b"\x00" * 4094
    if decoy_zip:
        # 真实案例：一个 exe 里前后两个 zip，前面那个是打包时误带的示例包
        decoy = make_folder(base, "decoy_" + name.replace(".", "_"))
        prefix += zip_tree(decoy)
        shutil.rmtree(decoy)
    exe = base / name
    exe.write_bytes(prefix + payload)
    # 造完就删掉中间目录：否则同级目录里的 .ks 会被当成「散装游戏」先认领走
    shutil.rmtree(folder)
    return exe


class LineUnitTests(unittest.TestCase):
    def test_resource_names_are_not_text(self):
        self.assertFalse(ferrytyrano.has_translatable("haikei_.jpg"))
        self.assertFalse(ferrytyrano.has_translatable("piano06.ogg"))
        self.assertFalse(ferrytyrano.has_translatable(""))
        self.assertFalse(ferrytyrano.has_translatable("*start"))
        self.assertFalse(ferrytyrano.has_translatable("title/button_start.png"))

    def test_js_lines_are_not_text(self):
        # 这两个是真实游戏里各被误抽过一百多次的样本
        self.assertFalse(ferrytyrano.has_translatable('$(".layer_camera").empty();'))
        self.assertFalse(ferrytyrano.has_translatable("tf.current_bgm_vol=parseInt(TG.config.defaultBgmVolume);"))
        self.assertFalse(ferrytyrano.has_translatable('if(TG.config.unReadTextSkip != "true"){'))
        self.assertFalse(ferrytyrano.has_translatable("chara_name_area"))

    def test_dialogue_and_labels_are_text(self):
        self.assertTrue(ferrytyrano.has_translatable("こんにちは"))
        self.assertTrue(ferrytyrano.has_translatable("あかね"))
        self.assertTrue(ferrytyrano.has_translatable("はじめから"))
        self.assertTrue(ferrytyrano.has_translatable("Start a new game"))
        self.assertTrue(ferrytyrano.has_translatable("陛下，我軍目前愛心的問題"))

    def test_split_units_kinds(self):
        units = ferrytyrano.split_units(FIRST_KS)
        by_pointer = {pointer: (attr, order, value) for _l, attr, order, value, pointer in units}
        # 正文行整行取
        self.assertIn("4#-1", by_pointer)
        self.assertEqual(by_pointer["4#-1"][2], "こんにちは[p]")
        # 标签行按「属性名」定位，只认有语义的那几个
        self.assertIn("5#text#0", by_pointer)
        self.assertEqual(by_pointer["5#text#0"][2], "はじめから")
        # storage= / target= 这类资源与跳转属性一个都不能进来
        self.assertNotIn("2#storage#0", by_pointer)
        self.assertNotIn("6#storage#0", by_pointer)
        values = [value for _l, _a, _o, value, _p in units]
        self.assertNotIn("*start", values)
        self.assertNotIn("haikei_.jpg", values)

    def test_iscript_block_is_skipped(self):
        text = "[iscript]\nvar a = 1;\nif (a) { TF.msg(\"x\") }\n[endscript]\nこんにちは\n"
        units = ferrytyrano.split_units(text)
        self.assertEqual([v for _l, _a, _o, v, _p in units], ["こんにちは"])

    def test_comments_are_skipped(self):
        units = ferrytyrano.split_units("; これはコメント\n// これも\nこんにちは\n")
        self.assertEqual([v for _l, _a, _o, v, _p in units], ["こんにちは"])

    def test_replace_attr_touches_only_target(self):
        line = '[glink text="はじめから" target="*start"]'
        self.assertEqual(
            ferrytyrano._replace_attr(line, "text", 0, "从头开始"),
            '[glink text="从头开始" target="*start"]',
        )


class ContainerTests(unittest.TestCase):
    def test_locate_appended_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            exe = make_exe(base)
            span = ferrycontainer.locate_appended_zip(exe)
            self.assertIsNotNone(span)
            self.assertEqual(span.end, exe.stat().st_size)
            self.assertGreater(span.start, 0)

    def test_picks_the_trailing_zip_when_two_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            exe = make_exe(base, decoy_zip=True)
            span = ferrycontainer.locate_appended_zip(exe)
            self.assertIsNotNone(span)
            with zipfile.ZipFile(exe) as archive:
                self.assertIn("data/scenario/orphan.ks", archive.namelist())
            # 尾部那个才是真档案：起点必须在前一个 zip 之后
            self.assertGreater(span.start, 4096)

    def test_folder_container_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_folder(base)
            box = ferrycontainer.open_container(root)
            self.assertEqual(box.kind, "folder")
            doc = box.read_document("data/scenario/first.ks")
            self.assertTrue(doc.bom)
            box.write_document("data/scenario/first.ks", doc.text.replace("こんにちは", "你好"), doc)
            out = base / "copy"
            box.commit(out)
            self.assertIn("你好", (out / "data/scenario/first.ks").read_text(encoding="utf-8-sig"))
            # 原目录不动
            self.assertIn("こんにちは", (root / "data/scenario/first.ks").read_text(encoding="utf-8-sig"))

    def test_exe_wins_over_stray_unpacked_folder(self):
        """游戏旁边躺着一份解包目录时，必须改 exe —— 引擎只读内嵌那份。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            exe = make_exe(base)
            # 模拟「上次手工解包留下的 package.nw」
            stray = base / "package.nw" / "data" / "scenario"
            stray.mkdir(parents=True)
            (stray / "first.ks").write_text("こんにちは\n", encoding="utf-8-sig")
            box = ferrycontainer.open_container(base)
            self.assertEqual(box.kind, "zip-in-exe")
            self.assertEqual(box.source, exe)

    def test_zip_in_exe_commit_keeps_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            exe = make_exe(base)
            original = exe.read_bytes()
            box = ferrycontainer.open_container(base)
            self.assertEqual(box.kind, "zip-in-exe")
            doc = box.read_document("data/scenario/first.ks")
            box.write_document("data/scenario/first.ks", doc.text.replace("こんにちは", "你好"), doc)
            out = base / "game.cn.exe"
            box.commit(out)
            # 前缀原样保留，原 exe 一个字节没动
            self.assertEqual(exe.read_bytes(), original)
            self.assertTrue(out.read_bytes().startswith(b"MZ"))
            with zipfile.ZipFile(out) as archive:
                text = archive.read("data/scenario/first.ks").decode("utf-8-sig")
            self.assertIn("你好", text)


class TyranoAdapterTests(unittest.TestCase):
    def test_probe_reports_units_and_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_folder(Path(tmp))
            adapter = ferrytyrano.TyranoAdapter()
            report = adapter.probe(root)
            self.assertTrue(report.readable)
            self.assertTrue(report.writable)
            self.assertTrue(report.ready)
            self.assertEqual(report.container, "folder")
            # first.ks 正文1 + 按钮1，extra.ks 正文1 + 按钮1；orphan.ks 是死代码，不算
            self.assertEqual(report.unit_count, 4)
            self.assertEqual(report.images_with_text, 1)  # 只有 hajime.png 命中线索
            self.assertTrue(any("orphan.ks" in note for note in report.notes))

    def test_dead_scenario_is_not_collected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_folder(Path(tmp))
            adapter = ferrytyrano.TyranoAdapter()
            entries = adapter.collect(root)
            paths = {e.rel_path for e in entries}
            self.assertNotIn("data/scenario/orphan.ks", paths)
            self.assertIn("data/scenario/first.ks", paths)

    def test_entries_carry_stable_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_folder(Path(tmp))
            adapter = ferrytyrano.TyranoAdapter()
            entries = adapter.collect(root)
            keys = [e.key for e in entries]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertIn("data/scenario/first.ks#4", keys)
            for entry in entries:
                self.assertEqual(entry.modid, "game")
                self.assertEqual(entry.source, "game")
                self.assertTrue(entry.english)

    def test_apply_writes_only_translated_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_folder(base)
            adapter = ferrytyrano.TyranoAdapter()
            entries = {e.english: e for e in adapter.collect(root)}
            translations = {
                "こんにちは[p]": "你好[p]",
                "はじめから": "从头开始",
                "それは、だれにも聞こえない声だった。[p]": "那是一道谁也没听见的声音。[p]",
                "つづきから": "继续",
            }
            report = adapter.apply(root, translations)
            self.assertTrue(report.ok, msg=str(report.conflicts))
            self.assertEqual(report.applied, 4)
            text = report.out_path.joinpath("data/scenario/first.ks").read_text(encoding="utf-8-sig")
            self.assertIn("你好[p]", text)
            self.assertIn('[glink text="从头开始" target="*start"]', text)
            # 标签一个都不能少
            self.assertIn('[image layer="base" page="fore" storage="haikei_.jpg"]', text)
            self.assertEqual(text.count("\n"), FIRST_KS.count("\n"))

    def test_apply_accepts_entry_key_as_well_as_english(self):
        """真实链路给的是 `{Entry.key: 译文}`，不是原文。

        这一条是补漏来的：本地化跑通之后 `applied` 一直是 0，
        因为 apply 当时只按 `entry.english` 查表，而翻译层返回的键是 `文件#行号`。
        手工构造的测试全都用原文当键，所以没暴露出来。
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_folder(base)
            adapter = ferrytyrano.TyranoAdapter()
            entries = adapter.collect(root)
            translations = {entry.key: f"译:{entry.english}" for entry in entries}
            report = adapter.apply(root, translations)
            self.assertTrue(report.ok, msg=str(report.conflicts))
            self.assertEqual(report.applied, len(entries))

    def test_second_run_does_not_overwrite_first_copy(self):
        """同一游戏跑第二次不能盖掉第一次的副本。

        实测踩到的：`applied=62` 但 `out_path=None` —— 默认名 `game.cn.exe` 已存在，
        commit 拒绝覆盖。拒绝本身是对的（旧副本可能人工校对过），但用户会以为「翻完没产出」。
        现在是自动换名 `game.cn2.exe`。
        """
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_exe(base)
            adapter = ferrytyrano.TyranoAdapter()
            first = adapter.apply(base, {"こんにちは[p]": "你好[p]"})
            self.assertEqual(first.out_path.name, "game.cn.exe")
            second = adapter.apply(base, {"こんにちは[p]": "再译一次[p]"})
            self.assertEqual(second.out_path.name, "game.cn2.exe")
            with zipfile.ZipFile(first.out_path) as archive:
                text = archive.read("data/scenario/first.ks").decode("utf-8-sig")
            self.assertIn("你好[p]", text)

    def test_apply_reports_missing_instead_of_silently_skipping(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_folder(base)
            adapter = ferrytyrano.TyranoAdapter()
            report = adapter.apply(root, {"こんにちは[p]": "你好[p]"})
            self.assertEqual(report.applied, 1)
            self.assertEqual(report.missed, 3)

    def test_apply_refuses_quotes_that_would_break_js(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_folder(base)
            adapter = ferrytyrano.TyranoAdapter()
            report = adapter.apply(root, {"はじめから": '从"头"开始'})
            self.assertTrue(report.conflicts)
            self.assertEqual(report.applied, 0)

    def test_zip_in_exe_apply_produces_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            exe = make_exe(base)
            original = exe.read_bytes()
            adapter = ferrytyrano.TyranoAdapter()
            report = adapter.apply(base, {"こんにちは[p]": "你好[p]"})
            self.assertTrue(report.ok, msg=str(report.conflicts))
            self.assertEqual(report.out_path.name, "game.cn.exe")
            self.assertEqual(exe.read_bytes(), original)
            with zipfile.ZipFile(report.out_path) as archive:
                text = archive.read("data/scenario/first.ks").decode("utf-8-sig")
            self.assertIn("你好[p]", text)


class LocalizeFlowTests(unittest.TestCase):
    def test_unknown_game_stops_before_doing_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            result = ferrylocalize.localize_game(empty, settings={"x": 1})
            self.assertEqual(result.applied, 0)
            self.assertTrue(any("认不出" in note for note in result.notes))

    def test_dry_run_never_calls_translator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_folder(Path(tmp))
            with patch.object(ferrylocalize, "translate_entries") as spy:
                result = ferrylocalize.localize_game(root, settings={"x": 1}, dry_run=True)
            spy.assert_not_called()
            self.assertEqual(result.entries, 4)
            self.assertEqual(result.applied, 0)

    def test_full_flow_uses_body_translator(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = make_folder(base)
            fake = ({"game": {"こんにちは[p]": "你好[p]", "はじめから": "从头开始"}}, [], {})
            with patch.object(ferrylocalize, "translate_entries", return_value=fake) as spy:
                result = ferrylocalize.localize_game(root, settings={"x": 1})
            spy.assert_called_once()
            sent = spy.call_args[0][0]
            self.assertEqual(len(sent), 4)
            self.assertEqual(result.applied, 2)
            self.assertEqual(result.missed, 0)  # missed 只统计引擎明确失败的
            self.assertIsNotNone(result.out_path)
            self.assertTrue(result.out_path.exists())

    def test_unwritable_probe_blocks_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = make_folder(Path(tmp))
            adapter = ferrytyrano.TyranoAdapter()
            broken = adapters.ProbeReport(engine="tyrano", root=root, readable=True, writable=False, unit_count=3)
            with patch.object(adapter, "probe", return_value=broken), patch.object(
                ferrylocalize, "translate_entries"
            ) as spy:
                result = ferrylocalize.localize_game(root, settings={"x": 1}, adapter=adapter)
            spy.assert_not_called()
            self.assertEqual(result.applied, 0)
            self.assertIn("写不回去", result.probe.summary())

    def test_registry_can_find_tyrano(self):
        self.assertIn("tyrano", adapters.registered())
        with tempfile.TemporaryDirectory() as tmp:
            root = make_folder(Path(tmp))
            found = adapters.detect(root)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "tyrano")


class UndoLogTests(unittest.TestCase):
    """撤销记录 —— 「汉化完能跑路」全靠这一层，所以逐条钉死。"""

    def _log(self, root: Path, engine: str = "test") -> adapters.UndoLog:
        return adapters.UndoLog(root, engine=engine)

    def test_no_record_refuses_to_guess(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game.exe").write_bytes(b"MZ")
            report = adapters.revert_game(root)
            self.assertFalse(report.ok)
            self.assertIn("没有汉化记录", report.notes[0])
            # 拒绝的时候一个文件都不能动
            self.assertTrue((root / "game.exe").exists())

    def test_modified_file_is_backed_up_before_write(self):
        """备份必须发生在写之前：写到一半崩了要能还原。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "eve" / "sn1.txt"
            target.parent.mkdir()
            target.write_bytes(b"original")
            log = self._log(root)
            log.will_modify("eve/sn1.txt")
            # 此刻备份已经落盘，记录也已经写出来了
            self.assertTrue((root / ".ferry-undo.json").exists())
            self.assertTrue((root / ".ferry-backup" / "eve" / "sn1.txt").exists())
            target.write_bytes(b"translated")
            log.did_write("eve/sn1.txt")
            log.finish()

            report = adapters.revert_game(root)
            self.assertTrue(report.ok, report.failed)
            self.assertEqual(target.read_bytes(), b"original")
            self.assertEqual(report.restored, 1)
            # 收工后现场清干净
            self.assertFalse((root / ".ferry-undo.json").exists())
            self.assertFalse((root / ".ferry-backup").exists())

    def test_added_file_is_removed_on_revert(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dll = root / "msimg32.dll"
            log = self._log(root)
            log.will_add("msimg32.dll")
            log.save()
            dll.write_bytes(b"proxy")
            log.did_write("msimg32.dll")
            log.finish()

            report = adapters.revert_game(root)
            self.assertTrue(report.ok, report.failed)
            self.assertFalse(dll.exists())
            self.assertEqual(report.removed, 1)

    def test_added_file_touched_by_user_is_not_deleted(self):
        """我加的 DLL 用户后来自己换了版本 —— 那是他的东西了，不能删。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dll = root / "msimg32.dll"
            log = self._log(root)
            log.will_add("msimg32.dll")
            log.save()
            dll.write_bytes(b"proxy")
            log.did_write("msimg32.dll")
            log.finish()
            dll.write_bytes(b"user replaced this")

            report = adapters.revert_game(root)
            self.assertFalse(report.ok)
            self.assertTrue(dll.exists())
            self.assertIn("改过", report.failed[0])
            # 失败时记录要留着，否则凭据也没了
            self.assertTrue((root / ".ferry-undo.json").exists())

    def test_copy_is_removed_and_record_appends(self):
        """两次汉化产出两份副本，一次 revert 全撤掉。"""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = base / "game"
            root.mkdir()
            (root / "game.exe").write_bytes(b"MZ")
            first = base / "game.cn"
            second = base / "game.cn2"
            for target in (first, second):
                target.mkdir()
                (target / "game.exe").write_bytes(b"MZ")

            log = self._log(root)
            log.did_copy(first)
            log.finish()
            log2 = adapters.UndoLog.open(root, engine="test")
            log2.did_copy(second)
            log2.finish()
            self.assertEqual(len(adapters.UndoLog.open(root).actions), 2)

            report = adapters.revert_game(root)
            self.assertTrue(report.ok, report.failed)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())
            self.assertTrue((root / "game.exe").exists())

    def test_refuses_to_delete_the_game_root_itself(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = self._log(root)
            with self.assertRaises(ValueError):
                log.did_copy(root)

    def test_dry_run_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "a.txt"
            target.write_text("orig", encoding="utf-8")
            log = self._log(root)
            log.will_modify("a.txt")
            target.write_text("new", encoding="utf-8")
            log.did_write("a.txt")
            log.finish()

            report = adapters.revert_game(root, dry_run=True)
            self.assertIn("预演", report.notes[-1])
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertTrue((root / ".ferry-undo.json").exists())

    def test_apply_leaves_a_record_that_revert_can_use(self):
        """Tyrano 产出的副本必须能被 revert 删掉 —— 契约闭环。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = make_folder(Path(tmp))
            adapter = adapters.TyranoAdapter()
            entries = adapter.collect(root)
            self.assertTrue(entries)
            translations = {e.key: "中文" for e in entries if e.english}
            applied = adapter.apply(root, translations)
            self.assertIsNotNone(applied.out_path)
            self.assertTrue(applied.undo_path is not None and applied.undo_path.exists())

            report = adapter.revert(root)
            self.assertTrue(report.ok, report.failed)
            self.assertFalse(applied.out_path.exists())
            # 原游戏照旧
            self.assertTrue((root / "data" / "scenario" / "first.ks").exists())


class RenpyAdapterTests(unittest.TestCase):
    """Ren'Py：明文 UTF-8，改原文 + 删 .rpyc。"""

    SCRIPT = """# 注释不该被抽走
define e = Character("Eileen")

label start:
    scene bg room
    play music "piano.ogg"
    "雨が降っている。"
    e "こんにちは、[player_name]さん。"
    e happy "今日はいい天気だね。{b}本当に{/b}"
    $ gold = 100
    jump chapter2

label chapter2:
    menu:
        "はい":
            jump yes
        "いいえ":
            return
    voice "voice/001.ogg"
    n "ナレーション。"
"""

    def make(self, base: Path) -> Path:
        root = base / "renpy_game"
        game = root / "game"
        game.mkdir(parents=True)
        (game / "script.rpy").write_text(self.SCRIPT, encoding="utf-8")
        (game / "script.rpyc").write_bytes(b"compiled cache")
        return root

    @staticmethod
    def translations(entries, overrides: dict[str, str]) -> dict[str, str]:
        """按原文给译文，转成以 Entry.key 为键的表。

        必须走 key：`apply` 先查 key 再退回原文，两种键混在一张表里时
        key 永远赢 —— 直接写 `{"原文": "译文"}` 会被上一轮铺的默认值盖掉。
        """
        return {e.key: overrides.get(e.english, e.english) for e in entries}

    def test_probe_recognizes_renpy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            report = adapters.RenpyAdapter().probe(root)
            self.assertTrue(report.readable)
            self.assertTrue(report.writable)
            self.assertEqual(report.unit_count, 6)
            self.assertEqual(report.container, "folder")

    def test_collect_skips_code_and_resource_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            entries = adapters.RenpyAdapter().collect(root)
            texts = [e.english for e in entries]
            self.assertIn("雨が降っている。", texts)
            self.assertIn("はい", texts)
            # 这些是文件名/变量/注释，翻了就崩
            for banned in ("piano.ogg", "voice/001.ogg", "chapter2", "Eileen", "gold"):
                self.assertNotIn(banned, texts)
            kinds = {e.kind for e in entries}
            self.assertEqual(kinds, {"say", "menu"})

    def test_apply_keeps_indent_and_speaker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            adapter = adapters.RenpyAdapter()
            entries = adapter.collect(root)
            translations = self.translations(entries, {
                "雨が降っている。": "下着雨。",
                "こんにちは、[player_name]さん。": "你好，[player_name]。",
                "はい": "是",
            })
            applied = adapter.apply(root, translations)
            self.assertEqual(applied.applied, 6)
            self.assertTrue(applied.in_place)

            text = (root / "game" / "script.rpy").read_text(encoding="utf-8")
            self.assertIn('    "下着雨。"', text)
            self.assertIn('    e "你好，[player_name]。"', text)
            self.assertIn('        "是":', text)
            # 代码行原样不动
            self.assertIn('    jump chapter2', text)
            self.assertIn("$ gold = 100", text)
            # 缓存必须删，否则 Ren'Py 继续用旧文本且不报错
            self.assertFalse((root / "game" / "script.rpyc").exists())

    def test_revert_restores_original_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            adapter = adapters.RenpyAdapter()
            entries = adapter.collect(root)
            adapter.apply(root, {e.key: e.english for e in entries})
            report = adapters.revert_game(root)
            self.assertTrue(report.ok, report.failed)
            self.assertEqual((root / "game" / "script.rpy").read_text(encoding="utf-8"), self.SCRIPT)

    def test_rejects_translation_that_drops_format_tags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            adapter = adapters.RenpyAdapter()
            entries = adapter.collect(root)
            # 把 {b}...{/b} 丢了 —— 游戏里会变成裸露的格式符
            translations = self.translations(entries, {"今日はいい天気だね。{b}本当に{/b}": "今天天气真好"})
            applied = adapter.apply(root, translations)
            self.assertTrue(any("格式标签" in c for c in applied.conflicts))
            text = (root / "game" / "script.rpy").read_text(encoding="utf-8")
            self.assertIn("{b}本当に{/b}", text)

    def test_rejects_straight_quote_in_translation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            adapter = adapters.RenpyAdapter()
            entries = adapter.collect(root)
            translations = self.translations(entries, {"雨が降っている。": '下着"s"雨'})
            applied = adapter.apply(root, translations)
            self.assertTrue(any("引号" in c for c in applied.conflicts))

    def test_non_renpy_folder_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "readme.txt").write_text("hello", encoding="utf-8")
            self.assertFalse(adapters.RenpyAdapter().probe(root).readable)

    def test_detect_prefers_the_matching_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            found = adapters.detect(root)
            self.assertIsNotNone(found)
            self.assertEqual(found.name, "renpy")


class RpgMakerAdapterTests(unittest.TestCase):
    """RPG Maker MV/MZ：明文 JSON，按「文件 + 结构位置 + 指令码」三重定位。"""

    MAP = {
        "displayName": "城下町",
        "events": [None, {"id": 1, "pages": [{"list": [
            {"code": 101, "parameters": ["", 0, 0, 2]},
            {"code": 401, "parameters": ["おお、勇者よ。"]},
            {"code": 102, "parameters": [["はい", "いいえ"], 0, 0, 2, 0]},
            {"code": 355, "parameters": ["$gameVariables.setValue(1, 10)"]},
            {"code": 405, "parameters": ["長い説明の続き。"]},
        ]}]}],
    }
    SYSTEM = {
        "gameTitle": "冒険の書",
        "terms": {"basic": ["レベル", "HP"], "commands": ["戦う"]},
    }
    ACTORS = [
        None,
        {"id": 1, "name": "アレン", "nickname": "勇者", "profile": "主人公。", "classId": 1},
        {"id": 2, "name": "", "profile": "", "classId": 1},
    ]

    def make(self, base: Path, mv: bool = False) -> Path:
        root = base / "rm_game"
        data = (root / "www" / "data") if mv else (root / "data")
        data.mkdir(parents=True)
        for name, payload in (("Map001.json", self.MAP), ("System.json", self.SYSTEM),
                              ("Actors.json", self.ACTORS)):
            (data / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return root

    def test_probe_recognizes_mz_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            report = adapters.RpgMakerAdapter().probe(root)
            self.assertTrue(report.readable)
            self.assertTrue(report.writable)
            self.assertGreater(report.unit_count, 0)

    def test_probe_recognizes_mv_www_data_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp), mv=True)
            report = adapters.RpgMakerAdapter().probe(root)
            self.assertTrue(report.readable)
            keys = [e.key for e in adapters.RpgMakerAdapter().collect(root)]
            # 相对路径要挂在项目根上，不管数据目录是不是 www/data
            self.assertTrue(all(k.startswith("www/data/") for k in keys), keys[:3])
            self.assertTrue((root / "www" / "data" / "System.json").exists())

    def test_collect_ignores_script_and_empty_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            texts = [e.english for e in adapters.RpgMakerAdapter().collect(root)]
            self.assertIn("おお、勇者よ。", texts)
            self.assertIn("はい", texts)
            self.assertIn("長い説明の続き。", texts)
            # 脚本行是代码，翻了游戏直接崩
            self.assertNotIn("$gameVariables.setValue(1, 10)", texts)
            # 空字段不该占位
            self.assertEqual(sum(1 for t in texts if not t.strip()), 0)

    def test_apply_patches_by_pointer_and_keeps_everything_else(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            adapter = adapters.RpgMakerAdapter()
            entries = adapter.collect(root)
            overrides = {"おお、勇者よ。": "哦，勇者啊。", "はい": "是", "冒険の書": "冒险之书",
                         "アレン": "艾伦"}
            translations = {e.key: overrides.get(e.english, e.english) for e in entries}
            applied = adapter.apply(root, translations)
            self.assertEqual(applied.applied, len(entries))
            self.assertTrue(applied.in_place)

            payload = json.loads((root / "data" / "Map001.json").read_text(encoding="utf-8"))
            instructions = payload["events"][1]["pages"][0]["list"]
            self.assertEqual(instructions[1]["parameters"][0], "哦，勇者啊。")
            self.assertEqual(instructions[2]["parameters"][0], ["是", "いいえ"])
            # 脚本行原样
            self.assertEqual(instructions[3]["parameters"][0], "$gameVariables.setValue(1, 10)")
            self.assertEqual(payload["displayName"], "城下町")

            system = json.loads((root / "data" / "System.json").read_text(encoding="utf-8"))
            self.assertEqual(system["gameTitle"], "冒险之书")
            actors = json.loads((root / "data" / "Actors.json").read_text(encoding="utf-8"))
            self.assertEqual(actors[1]["name"], "艾伦")
            self.assertEqual(actors[2], {"id": 2, "name": "", "profile": "", "classId": 1})

    def test_written_file_stays_compact(self):
        """RM 读紧凑 JSON。缩进版能让文件涨几十倍，还会让 diff 彻底不可用。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            adapter = adapters.RpgMakerAdapter()
            entries = adapter.collect(root)
            adapter.apply(root, {e.key: e.english for e in entries})
            text = (root / "data" / "System.json").read_text(encoding="utf-8")
            self.assertNotIn("\n", text)
            self.assertNotIn(", ", text)

    def test_revert_restores_all_touched_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            adapter = adapters.RpgMakerAdapter()
            entries = adapter.collect(root)
            adapter.apply(root, {e.key: "中文" + e.english for e in entries})
            report = adapters.revert_game(root)
            self.assertTrue(report.ok, report.failed)
            self.assertEqual(report.restored, 3)
            payload = json.loads((root / "data" / "Map001.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["events"][1]["pages"][0]["list"][1]["parameters"][0], "おお、勇者よ。")
            self.assertEqual(payload["displayName"], "城下町")

    def test_rejects_quote_that_would_corrupt_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.make(Path(tmp))
            adapter = adapters.RpgMakerAdapter()
            entries = adapter.collect(root)
            translations = {e.key: e.english for e in entries}
            target = next(e for e in entries if e.english == "おお、勇者よ。")
            translations[target.key] = '哦，"勇者"啊。'
            applied = adapter.apply(root, translations)
            self.assertTrue(any("引号" in c for c in applied.conflicts))
            payload = json.loads((root / "data" / "Map001.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["events"][1]["pages"][0]["list"][1]["parameters"][0], "おお、勇者よ。")

    def test_pointer_roundtrip(self):
        from ferrycore.adapters.rpgmaker import parse_pointer, set_pointer

        payload = {"a": [{"b": ["x"]}]}
        set_pointer(payload, "/a/0/b/0", "y")
        self.assertEqual(payload["a"][0]["b"][0], "y")
        self.assertEqual(parse_pointer("/a/0/b"), ["a", "0", "b"])
        with self.assertRaises(IndexError):
            set_pointer(payload, "/a/9/b/0", "z")

    def test_non_rm_folder_is_not_claimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "readme.txt").write_text("hello", encoding="utf-8")
            self.assertFalse(adapters.RpgMakerAdapter().probe(root).readable)


if __name__ == "__main__":
    unittest.main()
