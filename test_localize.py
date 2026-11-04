"""引擎适配器与本地汉化编排的离线测试。

全部用临时目录造数据，**不依赖任何本机游戏文件** ——
本机有游戏、CI 没有，这种测试只在本地绿，等于没测。
"""

from __future__ import annotations

import io
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


if __name__ == "__main__":
    unittest.main()
