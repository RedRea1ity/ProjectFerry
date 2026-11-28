"""离线验证 GUI P0 错误隔离；不访问真实实例或模型。"""

import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import mc_ai_translator as core


def load_gui():
    path = Path(__file__).with_name("ProjectFerry.pyw")
    spec = importlib.util.spec_from_file_location("ProjectFerry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuiP0Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.gui = load_gui()
        except ImportError as exc:  # 没有 tkinter 是环境问题，跳过合理
            raise unittest.SkipTest(f"没有 tkinter：{exc}") from exc
        try:
            tmp = tempfile.TemporaryDirectory()
            cls.addClassCleanup(tmp.cleanup)
            for target, attr, value in (
                (cls.gui, "UI_STATE_FILE", Path(tmp.name) / "ui.json"),
                (core, "load_config", lambda: dict(core.DEFAULT_CONFIG)),
                (core, "load_localize_history", lambda: []),
                (cls.gui.FerryApp, "refresh_instances", lambda self: None),
                (cls.gui.FerryApp, "_security_check", lambda self: None),
                (cls.gui.FerryApp, "_maybe_auto_update_check", lambda self: None),
            ):
                cls.enterClassContext(patch.object(target, attr, value))
            cls.window = cls.gui.FerryApp()
            cls.window.update_idletasks()
        except Exception as exc:
            text = str(exc).lower()
            # 只有「真的没有显示设备」才允许跳过。原来是 except Exception 一律 skip，
            # 结果把布局冲突（grid/pack 混用）也伪装成「无桌面环境」——
            # 2026-11-04 才发现这层假绿：GUI 起不来，测试却报 OK(skipped)。
            if "display" in text or "no such file or directory: '/tmp" in text:
                raise unittest.SkipTest(f"Tk 无可用桌面环境：{exc}") from exc
            raise

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "window"):
            cls.window._on_close()

    def test_bridge_export_writes_tooltip_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "mods").mkdir()
            scan = core.ScanResult(
                english=[core.LangFile("demo", "en_us", "jar", {"item.demo": "Blue Sword"})],
                community=[core.LangFile("demo", "zh_cn", "jar", {"item.demo": "人工蓝剑"})],
            )
            self.window.path_var.set(str(instance))
            self.window.scan_cache[str(instance)] = scan
            with patch.object(core, "resolve_translate_settings", return_value={"output_dir": str(instance / "resourcepacks"), "pack_name": "ai.zip"}), patch.object(core, "load_pack_translations", return_value={}), patch.object(core, "load_hardcoded_translations", return_value={}), patch.object(self.gui.messagebox, "showinfo"), patch.object(self.window, "_start_literalbridge_fetch", return_value=""):
                self.window.export_bridge_mapping()
            # 摆渡桥读的是**游戏目录**下的外部映射表；config/ferrybridge/translations.json
            # 是早期版本的位置，现在的桥根本不读那里（这条断言 2026-11-04 才第一次真正跑起来 ——
            # 之前本机 Python 没有 tkinter，6 个 GUI 测试全部静默跳过，所以它一直断言着旧路径）。
            mapping_file = instance / "ferrybridge-mappings.json"
            self.assertTrue(mapping_file.exists(), "没写出摆渡桥外部映射表")
            data = json.loads(mapping_file.read_text(encoding="utf-8"))
            self.assertEqual(data["map"]["Blue Sword"], "人工蓝剑")
            # LiteralBridge 规则另写一份，根节点只能是组
            rules_file = instance / "config" / "literalbridge" / "ferry.json"
            self.assertTrue(rules_file.exists(), "没写出 LiteralBridge 规则文件")

    def test_close_cancels_running_translation(self):
        self.window.translating = True
        self.window.cancel_event = threading.Event()
        with patch.object(self.window, "destroy"), patch.object(self.window, "_save_ui_state"):
            self.window._on_close()
        self.assertTrue(self.window.cancel_event.is_set())
        self.window._closing = False
        self.window.translating = False

    def test_glossary_error_does_not_unlock_translation(self):
        self.window.translating = True
        self.window.updating_glossary = True
        self.window._set_translating_ui(True)
        self.window.result_queue.put(("error", ("glossary", "模拟术语表失败")))
        with patch.object(self.gui.messagebox, "showerror"):
            self.window._drain_queue_once()
        self.assertTrue(self.window.translating)
        self.assertFalse(self.window.updating_glossary)
        self.assertEqual(self.window.translate_all_button.cget("state"), "disabled")
        self.window.translating = False
        self.window._set_translating_ui(False)

    def test_discovery_message_cannot_unlock_inflight_scan(self):
        self.window.scanning = True
        self.window.result_queue.put(("instances", [Path("C:/demo/instance")]))
        with patch.object(self.window, "start_scan") as start:
            self.window._drain_queue_once()
        self.assertTrue(self.window.scanning)
        start.assert_not_called()
        self.window.scanning = False

    def test_empty_second_round_clears_old_nbt_and_keeps_queue_running(self):
        self.window._last_nbt_count = 2
        self.window.result_queue.put(("translated", ([], [], "")))
        self.window._drain_queue()
        self.assertEqual(self.window._last_nbt_count, 0)
        self.assertIsNotNone(self.window._drain_after)

    def test_progress_write_failure_does_not_block_pack_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            instance = Path(tmp)
            (instance / "mods").mkdir()
            (instance / "resourcepacks").mkdir()
            scan = core.ScanResult(english=[core.LangFile("demo", "en_us", "jar", {"item.demo": "Apple"})])
            self.window.scan_cache[str(instance)] = scan
            config = {**core.DEFAULT_CONFIG, "community_enabled": False, "translate_hardcoded": False}
            settings = {
                "output_dir": str(instance / "resourcepacks"), "pack_name": "ai.zip",
                "engine": "mymemory", "api_key": "", "fill_community": False,
                "refine_community": False, "pack_format": 15,
            }
            calls = []

            def write_pack(*args, **kwargs):
                calls.append("pack")

            def fail_progress(*args, **kwargs):
                calls.append("progress")
                raise OSError("simulated read-only progress file")

            with patch.object(core, "resolve_translate_settings", return_value=settings), patch.object(core, "yield_to_community"), patch.object(core, "missing_entries", return_value=[core.Entry("demo", "item.demo", "Apple", "jar")]), patch.object(core, "missing_patchouli_entries", return_value=[]), patch.object(core, "translate_entries", return_value=({"demo": {"item.demo": "苹果"}}, [], {})), patch.object(core, "write_pack", side_effect=write_pack), patch.object(core, "update_progress", side_effect=fail_progress):
                self.window._translate_worker({str(instance): {"demo"}}, config, threading.Event())
            self.assertEqual(calls, ["pack", "progress"])
            kinds = [item[0] for item in list(self.window.result_queue.queue)]
            self.assertIn("translated", kinds)

    def test_dual_mode_workbench_switches_between_mc_and_game(self):
        self.window._switch_mode("game", persist=False)
        self.window.update_idletasks()
        self.assertEqual(self.window._active_mode, "game")
        self.assertIn("pc98", self.window._mode_hint_var.get())
        self.assertIn("rawbin", self.window._mode_hint_var.get())
        self.window._switch_mode("mc", persist=False)
        self.window.update_idletasks()
        self.assertEqual(self.window._active_mode, "mc")

    def test_disable_thinking_checkbox_syncs_config_and_summary(self):
        original = bool(self.window.disable_thinking_var.get())
        try:
            self.window.disable_thinking_var.set(False)
            self.window._update_settings_summary()
            self.assertFalse(self.window._snapshot_config()["disable_thinking"])
            self.assertNotIn("已关思考", self.window.settings_summary_var.get())

            self.window.disable_thinking_var.set(True)
            self.window._update_settings_summary()
            self.assertTrue(self.window._snapshot_config()["disable_thinking"])
            self.assertIn("已关思考", self.window.settings_summary_var.get())
        finally:
            self.window.disable_thinking_var.set(original)
            self.window._update_settings_summary()

    def test_batch_api_dialog_switches_domestic_presets(self):
        dlg = self.window._show_batch_api_dialog()
        self.addCleanup(dlg.destroy)
        dlg.update_idletasks()
        ctrl = dlg._batch_controls
        ctrl["preset_var"].set(core.BATCH_PRESETS["bigmodel"]["label"])
        ctrl["on_preset_changed"]()
        self.assertEqual(ctrl["url_var"].get(), "https://open.bigmodel.cn/api/paas/v4")
        self.assertEqual(ctrl["endpoint_var"].get(), "/v4/chat/completions")

        ctrl["preset_var"].set(core.BATCH_PRESETS["dashscope"]["label"])
        ctrl["on_preset_changed"]()
        self.assertEqual(ctrl["url_var"].get(), "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertEqual(ctrl["endpoint_var"].get(), "/v1/chat/completions")

    def test_drag_and_drop_routes_game_and_mc_instance(self):
        from ferrycore.win_dnd import classify_dropped_path

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            game_dir = base / "MyGame"
            game_dir.mkdir()
            game_exe = game_dir / "Game.exe"
            game_exe.write_bytes(b"MZ")

            mc_dir = base / "MyMC"
            (mc_dir / "mods").mkdir(parents=True)
            (mc_dir / "resourcepacks").mkdir()
            mod_jar = mc_dir / "mods" / "sample.jar"
            mod_jar.write_bytes(b"PK")

            self.assertEqual(classify_dropped_path(game_dir), ("game", game_dir))
            self.assertEqual(classify_dropped_path(game_exe), ("game", game_exe))
            self.assertEqual(classify_dropped_path(mc_dir), ("mc", mc_dir))
            self.assertEqual(classify_dropped_path(mod_jar), ("mc", mc_dir))
            self.assertEqual(classify_dropped_path(base / "missing")[0], "invalid")

            # 测试实际拖入游戏文件：自动切到「游戏汉化」页并保留精确文件路径（不截断成父目录）
            with patch.object(self.window, "_loc_start_probe") as probe_spy:
                self.window._on_files_dropped([game_exe])
            self.assertEqual(self.window._active_mode, "game")
            self.assertEqual(Path(self.window._loc_path_var.get()), game_exe)
            probe_spy.assert_called_once()

            # 测试实际拖入 MC 实例目录：自动切到「模组汉化」页并触发扫描
            with patch.object(self.window, "start_scan") as scan_spy, patch.object(core, "save_config"):
                self.window._on_files_dropped([mc_dir])
            self.assertEqual(self.window._active_mode, "mc")
            self.assertEqual(Path(self.window.path_var.get()), mc_dir)
            scan_spy.assert_called_once()

            # 验证 Windows 原生拖放钩子可正常安装并卸载（无崩溃/泄漏）
            self.window._setup_file_drop()
            if self.window._dnd_hook is not None:
                self.window._dnd_hook.uninstall()
                self.window._dnd_hook = None

    def test_drag_single_file_in_mixed_folder_targets_only_dropped_file(self):
        from ferrycore.adapters import resolve_game_target
        from ferrygame import project as fg_project

        with tempfile.TemporaryDirectory() as tmp:
            mixed = Path(tmp) / "Downloads"
            mixed.mkdir()

            # 同目录混放多个可识别文件 + AGS 标记，验证不会随机识别成别的文件
            (mixed / "acsetup.cfg").write_text("[misc]\n", encoding="utf-8")
            other_po = mixed / "aaa_other.po"
            other_po.write_text('msgid "Other Game Title"\nmsgstr ""\n', encoding="utf-8")
            target_po = mixed / "zzz_target.po"
            target_po.write_text('msgid "Target Game Dialogue"\nmsgstr ""\n', encoding="utf-8")
            unrelated_txt = mixed / "readme.txt"
            unrelated_txt.write_text("plain note\n", encoding="utf-8")

            # 1) 精准锁定 zzz_target.po，不串读 aaa_other.po，也不被 acsetup.cfg 劫持
            resolved, adapter = resolve_game_target(target_po)
            self.assertEqual(resolved, target_po)
            self.assertIsNotNone(adapter)
            self.assertEqual(adapter.name, "i18n_table")
            entries = adapter.collect(resolved)
            self.assertEqual([e.english for e in entries], ["Target Game Dialogue"])

            # 2) 单文件双语表按文件名隔离，不与同目录其他游戏互相覆盖
            self.assertEqual(fg_project.default_path(target_po).name, "zzz_target.ferry_tl.json")
            self.assertEqual(fg_project.default_path(other_po).name, "aaa_other.ferry_tl.json")

            # 3) 拖入不可识别的单文件，不会错误回退去识别同目录的 aaa_other.po 或 acsetup.cfg
            res_txt, adp_txt = resolve_game_target(unrelated_txt)
            self.assertEqual(res_txt, unrelated_txt)
            self.assertIsNone(adp_txt)

            # 4) 同目录放两个独立二进制 .exe，拖入第二个时只抽取第二个文件本身
            sjis_a = "これは別のゲームのメッセージです。".encode("cp932")
            sjis_b = "これは翻訳対象のメッセージです。".encode("cp932")
            exe_a = mixed / "aaa_game.exe"
            exe_b = mixed / "zzz_game.exe"
            exe_a.write_bytes(b"MZ" + b"\x00" * 32 + b"\x00".join([sjis_a] * 4) + b"\x00")
            exe_b.write_bytes(b"MZ" + b"\x00" * 32 + b"\x00".join([sjis_b] * 4) + b"\x00")

            res_b, adp_b = resolve_game_target(exe_b)
            self.assertEqual(res_b, exe_b)
            self.assertIsNotNone(adp_b)
            self.assertEqual(adp_b.name, "rawbin")
            texts_b = [e.english for e in adp_b.collect(res_b)]
            self.assertTrue(any("翻訳対象" in t for t in texts_b))
            self.assertFalse(any("別のゲーム" in t for t in texts_b))

            # 5) 多文件目录型引擎的启动器 .exe（如 RPG Maker MV 的 Game.exe）仍能自动归位到游戏目录
            rpg_dir = Path(tmp) / "MyRpgMv"
            (rpg_dir / "www" / "data").mkdir(parents=True)
            (rpg_dir / "www" / "data" / "System.json").write_text(
                '{"gameTitle": "Fantasy Quest", "terms": {"basic": ["Level"]}}',
                encoding="utf-8",
            )
            rpg_exe = rpg_dir / "Game.exe"
            rpg_exe.write_bytes(b"MZ" + b"\x00" * 64)

            res_rpg, adp_rpg = resolve_game_target(rpg_exe)
            self.assertEqual(res_rpg, rpg_dir)
            self.assertIsNotNone(adp_rpg)
            self.assertEqual(adp_rpg.name, "rpgmaker")

    def test_history_remove_and_clear_buttons_never_delete_disk_files(self):
        from ferrycore import localized as ferrylocalized

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            keep_dir = base / "GameA.cn"
            keep_dir.mkdir()
            marker = keep_dir / "keep.txt"
            marker.write_text("alive", encoding="utf-8")
            rec1 = ferrylocalized.LocalizedRecord(root=str(base / "GameA"), play_path=str(keep_dir), mode="copy", engine="tyrano", label="GameA.cn")
            rec2 = ferrylocalized.LocalizedRecord(root=str(base / "GameB"), play_path=str(base / "GameB.cn"), mode="copy", engine="renpy", label="GameB.cn")

            with patch.object(ferrylocalized, "app_dir", return_value=base):
                ferrylocalized.save_history([rec1, rec2])
                self.window._home_page.refresh(ferrylocalized.load_history())
                self.assertEqual(len(self.window._home_page.table.get_children()), 2)

                # 移除单条
                self.assertTrue(ferrylocalized.remove_history(keep_dir))
                self.assertEqual(len(ferrylocalized.load_history()), 1)
                self.assertTrue(marker.is_file(), "清理历史绝不能删除磁盘上的游戏文件")

                # 清理失效条目 + 全部清空
                self.assertEqual(ferrylocalized.prune_history(), 1)
                ferrylocalized.save_history([rec1])
                with patch.object(core, "load_localize_history", side_effect=ferrylocalized.load_history), \
                     patch.object(core, "clear_localize_history", side_effect=ferrylocalized.clear_history), \
                     patch.object(self.gui.messagebox, "askyesno", return_value=True):
                    self.window._clear_records()
                self.assertEqual(ferrylocalized.load_history(), [])
                self.assertEqual(len(self.window._home_page.table.get_children()), 0)
                self.assertTrue(marker.is_file())

    def test_settings_page_and_three_mode_theme_follow_system(self):
        import ferrycore.config as ferryconfig
        original_mode = self.window._theme_mode
        try:
            # 侧边栏精简：翻译引擎/运行日志/导出诊断/主题统一收纳进「设置」页
            self.assertNotIn("夜间模式", self.window.shell.util_buttons)
            self.assertNotIn("运行日志", self.window.shell.util_buttons)
            self.assertNotIn("导出诊断", self.window.shell.util_buttons)
            self.assertIn("使用说明", self.window.shell.util_buttons)
            self.assertIn("关于摆渡", self.window.shell.util_buttons)
            self.assertIn("settings", self.window.shell.nav)

            with patch.object(core, "save_config"):
                self.window.theme_mode_var.set("light")
                self.window._on_theme_mode_selected()
                self.assertEqual(self.window._theme_mode, "light")
                self.assertEqual(self.window._theme, "light")
                self.assertEqual(self.window.cget("bg"), self.gui.THEMES["light"]["bg"])
                self.assertEqual(self.window.config.get("theme"), "light")
                self.assertIn("日间模式", self.window.theme_status_var.get())

                self.window.theme_mode_var.set("dark")
                self.window._on_theme_mode_selected()
                self.assertEqual(self.window._theme_mode, "dark")
                self.assertEqual(self.window._theme, "dark")
                self.assertEqual(self.window.cget("bg"), self.gui.THEMES["dark"]["bg"])
                self.assertEqual(self.window._loc_report.cget("bg"), self.gui.THEMES["dark"]["card"])
                self.assertEqual(self.window.config.get("theme"), "dark")
                self.assertIn("夜间模式", self.window.theme_status_var.get())

                # 跟随系统：系统深色 → 自动切换夜间；系统切浅色 → 刷新跟随日间
                with patch.object(ferryconfig, "detect_system_theme", return_value="dark"):
                    self.window.theme_mode_var.set("system")
                    self.window._on_theme_mode_selected()
                    self.assertEqual(self.window._theme_mode, "system")
                    self.assertEqual(self.window._theme, "dark")
                    self.assertEqual(self.window.config.get("theme"), "system")
                    self.assertIn("跟随系统", self.window.theme_status_var.get())

                with patch.object(ferryconfig, "detect_system_theme", return_value="light"):
                    self.window._refresh_system_theme()
                    self.assertEqual(self.window._theme_mode, "system")
                    self.assertEqual(self.window._theme, "light")
                    self.assertEqual(self.window.cget("bg"), self.gui.THEMES["light"]["bg"])
        finally:
            self.window._apply_theme(original_mode, save=False)

    def test_about_reflows_without_clipping_text(self):
        self.window._show_about()
        dialog = next(w for w in self.window.winfo_children()
                      if isinstance(w, self.gui.tk.Toplevel) and w.title() == f"关于 {self.gui.APP_NAME}")
        self.addCleanup(dialog.destroy)

        def descendants(parent):
            for child in parent.winfo_children():
                yield child
                yield from descendants(child)

        labels = [w for w in descendants(dialog) if isinstance(w, self.gui.ttk.Label)]
        sections = [w.master for w in labels if w.cget("text") in ("游戏汉化", "Minecraft 模组")]
        self.assertEqual(len(sections), 2)
        controls = [w for w in descendants(dialog) if isinstance(w, (self.gui.ttk.Button, self.gui.ttk.Scrollbar))]
        for size, columns in (("820x620", [0, 1]), ("460x420", [0, 0]), ("360x300", [0, 0]),
                              ("740x420", [0, 1]), ("820x620", [0, 1])):
            with self.subTest(size=size):
                dialog.geometry(size)
                dialog.update()
                self.assertEqual([int(s.grid_info()["column"]) for s in sections], columns)
                for label in labels:
                    self.assertGreaterEqual(label.winfo_width(), label.winfo_reqwidth(), label.cget("text"))
                for control in controls:
                    self.assertTrue(control.winfo_ismapped(), control)
                    self.assertGreaterEqual(control.winfo_width(), control.winfo_reqwidth(), control)

    def test_workbench_navigation_preserves_state_and_scopes_shortcuts(self):
        app = self.window
        app._mc_started = False
        with patch.object(app, "refresh_instances") as discover, patch.object(app, "start_scan") as scan:
            for key in ("home", "settings", "game", "home"):
                app._switch_mode(key, persist=False)
                app.update_idletasks()
                self.assertEqual([k for k, page in app._pages.items() if page.winfo_ismapped()], [key])
            app._on_rescan_key()
            discover.assert_not_called()
            scan.assert_not_called()
            app._switch_mode("mc", persist=False)
            app._switch_mode("game", persist=False)
            with patch.object(app, "_loc_start_probe") as probe:
                app._on_rescan_key()
                probe.assert_called_once()
            app._switch_mode("mc", persist=False)
            discover.assert_called_once()
            app._on_rescan_key()
            scan.assert_called_once()
        # 兼容旧版配置里存的 "engine" 页名，平滑映射到 "settings"
        app._switch_mode("engine")
        self.assertEqual(app._load_ui_state()["workbench_page"], "settings")
        self.assertEqual(app.api_key_entry.cget("show"), "•")

    def test_home_opens_selected_record_not_first_record(self):
        from ferrycore.localized import LocalizedRecord
        app = self.window
        records = [LocalizedRecord("a", "a.cn", "copy"), LocalizedRecord("b", "b.cn", "copy")]
        with patch.object(core, "load_localize_history", return_value=records):
            app._switch_mode("home", persist=False)
        index = app._home_page
        index.table.selection_set(index.table.get_children()[1])
        with patch.object(index, "open_output") as opened:
            index._open()
            opened.assert_called_once_with(Path("b.cn"))


if __name__ == "__main__":
    unittest.main()
