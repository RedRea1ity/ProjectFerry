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


if __name__ == "__main__":
    unittest.main()
