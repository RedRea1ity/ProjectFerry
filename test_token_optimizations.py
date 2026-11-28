"""Step 1 & Step 2 优化回归测试：Token 节省、真实用量统计、稳定性与暗坑修复。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mc_ai_translator as core
from ferrycore import adapters as core_adapters
from ferrycore import cli as ferrycli
from ferrycore import engine as ferryengine
from ferrycore import localize as ferrylocalize
from ferrycore.net import ApiRequestError
from ferrygame.translate import _build_input as game_build_input, translate_units
from ferrygame.units import Unit


class ShortWireAndPrefilterTests(unittest.TestCase):
    def test_short_ids_and_compact_placeholder_tags_on_wire(self):
        entries = [
            core.Entry("demo", "item.super_long_modid.hyper_enchanted_golden_pickaxe", "Gain %s HP with %s", "jar"),
            core.Entry("demo", "patchouli:guide/basics/intro.json#/pages/0/text", "Press E to open the mysterious ancient chest", "jar"),
        ]
        seen_payloads = []

        def fake_post(_url, payload, _headers, _timeout):
            seen_payloads.append(payload)
            user_obj = json.loads(payload["messages"][1]["content"])
            # 确认长 key 被压成 "1" / "2"，占位符被压成 <p0> / <p1>
            self.assertEqual(set(user_obj), {"1", "2"})
            self.assertEqual(user_obj["1"], "Gain <p0> HP with <p1>")
            self.assertEqual(user_obj["2"], "Press E to open the mysterious ancient chest")
            return {
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": json.dumps({
                        "1": "获得 <p0> 生命并附带 <p1>",
                        "2": "按 E 打开神秘的古老箱子",
                    }, ensure_ascii=False)},
                }],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 35,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
            }

        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                "cache_dir": tmp, "batch_size": 10, "concurrency": 1,
                "glossary": {}, "keep": set(), "delay": 0, "timeout": 5,
                "engine": "openai", "model": "gpt-4o-mini",
                "base_url": "https://api.example.test/v1", "api_key": "test",
            }
            usage: dict = {}
            with patch.object(ferryengine, "_http_post_json", side_effect=fake_post):
                translated, errors, failed = core.translate_entries(entries, settings, usage=usage)
        self.assertFalse(errors or failed)
        self.assertEqual(translated["demo"]["item.super_long_modid.hyper_enchanted_golden_pickaxe"], "获得 %s 生命并附带 %s")
        self.assertEqual(translated["demo"]["patchouli:guide/basics/intro.json#/pages/0/text"], "按 E 打开神秘的古老箱子")
        rec = usage["openai:gpt-4o-mini"]
        self.assertEqual((rec["input_tokens"], rec["output_tokens"], rec["cached_tokens"]), (120, 35, 80))
        formatted = core.format_usage(usage)
        self.assertIn("输入 120", formatted)
        self.assertIn("输出 35", formatted)
        self.assertIn("前缀缓存命中 80", formatted)

    def test_placeholder_tag_collision_falls_back_safely(self):
        # 原文自己包含 <p0> 时，线上传输必须回退到 __MC_PH_0__，防止混淆还原。
        entries = [core.Entry("demo", "k1", "Literal <p0> and %s", "jar")]

        def fake_post(_url, payload, _headers, _timeout):
            user_obj = json.loads(payload["messages"][1]["content"])
            self.assertEqual(user_obj["1"], "Literal <p0> and __MC_PH_0__")
            return {"choices": [{"message": {"content": json.dumps({"1": "字面 <p0> 与 __MC_PH_0__"}, ensure_ascii=False)}}]}

        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                "cache_dir": tmp, "batch_size": 10, "concurrency": 1,
                "glossary": {}, "keep": set(), "delay": 0, "timeout": 5,
                "engine": "openai", "model": "gpt-4o-mini",
                "base_url": "https://api.example.test/v1", "api_key": "test",
            }
            with patch.object(ferryengine, "_http_post_json", side_effect=fake_post):
                translated, errors, failed = core.translate_entries(entries, settings)
        self.assertFalse(errors or failed)
        self.assertEqual(translated["demo"]["k1"], "字面 <p0> 与 %s")

    def test_zero_token_prefilter_and_japanese_kanji_safety(self):
        glossary = {"Redstone": "红石"}
        keep = {"Minecraft"}
        # 纯占位符 / 纯数字 / 纯术语 / 保留词 / 英文源里的纯中文 -> 零 Token 直出
        self.assertEqual(core.prefilter_translation("%1$s / %2$s", glossary, keep, "en"), "%1$s / %2$s")
        self.assertEqual(core.prefilter_translation("§a+100%", glossary, keep, "en"), "§a+100%")
        self.assertEqual(core.prefilter_translation("Redstone", glossary, keep, "en"), "红石")
        self.assertEqual(core.prefilter_translation("Minecraft", glossary, keep, "en"), "Minecraft")
        self.assertEqual(core.prefilter_translation("已启用 %s", glossary, keep, "en"), "已启用 %s")
        # 正常英文 -> 必须送翻
        self.assertIsNone(core.prefilter_translation("Iron Sword", glossary, keep, "en"))
        self.assertIsNone(core.prefilter_translation("Redstone Torch", glossary, keep, "en"))
        # 日文纯汉字词绝不能跳过（需要翻成简体中文「设置」）
        self.assertIsNone(core.prefilter_translation("設定", {}, set(), "ja"))
        self.assertEqual(core.prefilter_translation("100%", {}, set(), "ja"), "100%")

    def test_dedup_and_prefilter_and_corpus_exact_save_api_calls(self):
        entries = [
            core.Entry("demo", "k_ph", "%s / %s", "jar"),
            core.Entry("demo", "k_term", "Redstone", "jar"),
            core.Entry("demo", "k_zh", "已经翻译好了", "jar"),
            core.Entry("demo", "k_dup1", "Open Settings", "jar"),
            core.Entry("demo", "k_dup2", "Open Settings", "jar"),
            core.Entry("demo", "k_dup3", "Open Settings", "jar"),
            core.Entry("demo", "k_corpus", "Apple", "jar"),
        ]
        corpus = {"demo": {"item.existing": ("Apple", "人工苹果")}}
        calls = []

        def fake_post(_url, payload, _headers, _timeout):
            user_obj = json.loads(payload["messages"][1]["content"])
            calls.append(user_obj)
            self.assertEqual(user_obj["strings"], {"1": "Open Settings"})
            return {"choices": [{"message": {"content": json.dumps({"1": "打开设置"}, ensure_ascii=False)}}]}

        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                "cache_dir": tmp, "batch_size": 10, "concurrency": 1,
                "glossary": {"Redstone": "红石"}, "keep": set(), "delay": 0, "timeout": 5,
                "refine_community": True, "engine": "openai", "model": "test",
                "base_url": "https://api.example.test/v1", "api_key": "test",
            }
            usage: dict = {}
            with patch.object(ferryengine, "_http_post_json", side_effect=fake_post):
                translated, errors, failed = core.translate_entries(entries, settings, usage=usage, corpus=corpus)
        self.assertFalse(errors or failed)
        self.assertEqual(len(calls), 1)
        self.assertEqual(translated["demo"], {
            "k_ph": "%s / %s",
            "k_term": "红石",
            "k_zh": "已经翻译好了",
            "k_dup1": "打开设置",
            "k_dup2": "打开设置",
            "k_dup3": "打开设置",
            "k_corpus": "人工苹果",
        })
        self.assertEqual(usage["_saved"]["prefiltered"], 3)
        self.assertEqual(usage["_saved"]["corpus_exact"], 1)
        self.assertEqual(usage["_saved"]["deduped"], 2)


class PromptCacheThinkingAndStabilityTests(unittest.TestCase):
    def test_system_prompt_is_static_across_batches_with_different_hints(self):
        entries = [
            core.Entry("demo", "k1", "Mine Redstone ore", "jar"),
            core.Entry("demo", "k2", "Craft Golden Apple", "jar"),
        ]
        systems = []

        def fake_post(_url, payload, _headers, _timeout):
            systems.append(payload["messages"][0]["content"])
            return {"choices": [{"message": {"content": json.dumps({"1": "译文"}, ensure_ascii=False)}}]}

        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                "cache_dir": tmp, "batch_size": 1, "concurrency": 1,
                "glossary": {}, "term_hints": {"Redstone": "红石", "Golden Apple": "金苹果"},
                "keep": set(), "delay": 0, "timeout": 5,
                "engine": "openai", "model": "gpt-4o-mini",
                "base_url": "https://api.example.test/v1", "api_key": "test",
            }
            with patch.object(ferryengine, "_http_post_json", side_effect=fake_post):
                core.translate_entries(entries, settings)
        self.assertEqual(len(systems), 2)
        self.assertEqual(systems[0], systems[1], "系统提示词必须 100% 静态以命中前缀缓存")

    def test_anthropic_prompt_cache_and_400_fallback(self):
        seen = []

        def fake_post(_url, payload, _headers, _timeout):
            seen.append(payload)
            if isinstance(payload["system"], list):
                raise ApiRequestError("400 unsupported cache_control", status_code=400, retryable=False)
            return {
                "content": [{"type": "text", "text": json.dumps({"1": "钻石剑"}, ensure_ascii=False)}],
                "usage": {"input_tokens": 20, "cache_read_input_tokens": 100, "output_tokens": 10},
            }

        sink: dict[str, int] = {}
        settings = {
            "model": "claude-3-5-haiku-latest", "base_url": "https://api.anthropic.com",
            "api_key": "sk-ant-test", "timeout": 5, "prompt_cache": True, "_token_usage": sink,
        }
        with patch.object(ferryengine, "_http_post_json", side_effect=fake_post):
            out = ferryengine._translate_anthropic({"item.sword": "Diamond Sword"}, settings)
        self.assertEqual(out, {"item.sword": "钻石剑"})
        self.assertEqual(len(seen), 2)
        self.assertIsInstance(seen[0]["system"], list)
        self.assertEqual(seen[0]["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIsInstance(seen[1]["system"], str)
        self.assertEqual((sink["input_tokens"], sink["cached_tokens"], sink["output_tokens"]), (120, 100, 10))

    def test_gemini_json_mode_and_thinking_budget_only_on_25_models(self):
        captured = []

        def fake_post(_url, payload, _headers, _timeout):
            captured.append(payload)
            return {
                "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps({"1": "铁镐"}, ensure_ascii=False)}]}}],
                "usageMetadata": {"promptTokenCount": 90, "candidatesTokenCount": 12, "cachedContentTokenCount": 50, "thoughtsTokenCount": 0},
            }

        with patch.object(ferryengine, "_http_post_json", side_effect=fake_post):
            sink: dict[str, int] = {}
            ferryengine._translate_gemini({"k": "Iron Pickaxe"}, {
                "model": "gemini-2.5-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "test", "timeout": 5, "disable_thinking": True, "_token_usage": sink,
            })
            ferryengine._translate_gemini({"k": "Iron Pickaxe"}, {
                "model": "gemini-1.5-flash", "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key": "test", "timeout": 5, "disable_thinking": True,
            })
        self.assertEqual(captured[0]["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(captured[0]["generationConfig"]["thinkingConfig"], {"thinkingBudget": 0})
        self.assertEqual(captured[1]["generationConfig"]["responseMimeType"], "application/json")
        self.assertNotIn("thinkingConfig", captured[1]["generationConfig"])
        self.assertEqual((sink["input_tokens"], sink["output_tokens"], sink["cached_tokens"]), (90, 12, 50))

    def test_openai_compatible_thinking_toggle_defaults_off_and_supports_deepseek_qwen_fallback(self):
        # 默认不主动限制思考（由用户在界面里决定是否开启省流提速）
        self.assertFalse(core.DEFAULT_CONFIG["disable_thinking"])
        captured = []

        def fake_post(_url, payload, _headers, _timeout):
            captured.append(payload)
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"1": "铁剑"}, ensure_ascii=False)}}]}

        with patch.object(ferryengine, "_http_post_json", side_effect=fake_post):
            # 1) 默认关闭开关 -> deepseek-v4-pro 不附带 thinking 字段
            ferryengine._translate_openai({"k": "Iron Sword"}, {
                "engine": "openai", "model": "deepseek-v4-pro",
                "base_url": "https://api.deepseek.com", "api_key": "test", "timeout": 5,
            })
            # 2) 开启开关 -> deepseek-v4-pro 附带 thinking: {"type": "disabled"}
            ferryengine._translate_openai({"k": "Iron Sword"}, {
                "engine": "openai", "model": "deepseek-v4-pro",
                "base_url": "https://api.deepseek.com", "api_key": "test", "timeout": 5,
                "disable_thinking": True,
            })
            # 3) 开启开关 -> qwen3 / 阿里云 附带 enable_thinking: False
            ferryengine._translate_openai({"k": "Iron Sword"}, {
                "engine": "openai", "model": "qwen3-32b",
                "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "api_key": "test", "timeout": 5,
                "disable_thinking": True,
            })
            # 4) 开启开关 -> Gemini 走 OpenAI 兼容中转 附带 reasoning_effort: "none"
            ferryengine._translate_openai({"k": "Iron Sword"}, {
                "engine": "openai", "model": "gemini-2.5-flash",
                "base_url": "https://relay.example.test/v1", "api_key": "test", "timeout": 5,
                "disable_thinking": True,
            })

        self.assertNotIn("thinking", captured[0])
        self.assertNotIn("enable_thinking", captured[0])
        self.assertEqual(captured[1].get("thinking"), {"type": "disabled"})
        self.assertIs(captured[2].get("enable_thinking"), False)
        self.assertEqual(captured[3].get("reasoning_effort"), "none")

        # 5) 中转站不认 thinking 返回 400 时，先仅剥离 thinking 而保留 response_format
        fallback_seen = []

        def fake_400(_url, payload, _headers, _timeout):
            fallback_seen.append(payload)
            if "thinking" in payload:
                raise ApiRequestError("400 unknown field thinking", status_code=400, retryable=False)
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"1": "铁剑"}, ensure_ascii=False)}}]}

        with patch.object(ferryengine, "_http_post_json", side_effect=fake_400):
            out = ferryengine._translate_openai({"k": "Iron Sword"}, {
                "engine": "openai", "model": "deepseek-v4-pro",
                "base_url": "https://relay.example.test/v1", "api_key": "test", "timeout": 5,
                "disable_thinking": True,
            })
        self.assertEqual(out, {"k": "铁剑"})
        self.assertEqual(len(fallback_seen), 2)
        self.assertIn("thinking", fallback_seen[0])
        self.assertNotIn("thinking", fallback_seen[1])
        self.assertEqual(fallback_seen[1].get("response_format"), {"type": "json_object"})

    def test_non_retryable_401_fails_fast_without_wasting_retries(self):
        attempts = []

        def fake_post(_url, _payload, _headers, _timeout):
            attempts.append(1)
            raise ApiRequestError("API key 无效（401）", status_code=401, retryable=False)

        settings = {
            "model": "gpt-4o-mini", "base_url": "https://api.example.test/v1",
            "api_key": "bad", "timeout": 5,
        }
        with patch.object(ferryengine, "_http_post_json", side_effect=fake_post):
            with self.assertRaises(ApiRequestError):
                ferryengine._translate_batch_with_retry("openai", {"k": "Hello"}, settings, retries=2)
        self.assertEqual(len(attempts), 1, "401 不可重试错误必须立即止损，不能傻重试 3 次")

    def test_truncated_response_auto_splits_batch_instead_of_repeating_failure(self):
        entries = [core.Entry("demo", f"k{i}", f"Long paragraph number {i}", "jar") for i in range(4)]
        call_sizes = []

        def fake_post(_url, payload, _headers, _timeout):
            user_obj = json.loads(payload["messages"][1]["content"])
            call_sizes.append(len(user_obj))
            if len(user_obj) > 2:
                return {"choices": [{"finish_reason": "length", "message": {"content": '{"1": "半截'}}]}
            return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({k: f"译:{v}" for k, v in user_obj.items()}, ensure_ascii=False)}}]}

        with tempfile.TemporaryDirectory() as tmp:
            settings = {
                "cache_dir": tmp, "batch_size": 4, "concurrency": 1,
                "glossary": {}, "keep": set(), "delay": 0, "timeout": 5,
                "engine": "openai", "model": "gpt-4o-mini",
                "base_url": "https://api.example.test/v1", "api_key": "test",
            }
            with patch.object(ferryengine, "_http_post_json", side_effect=fake_post):
                translated, errors, failed = core.translate_entries(entries, settings)
        self.assertFalse(errors or failed)
        self.assertEqual(call_sizes, [4, 2, 2])
        self.assertEqual(len(translated["demo"]), 4)


class DynamicBatchTmAndDifferentialRetryTests(unittest.TestCase):
    def test_dynamic_char_budget_splits_long_entries_and_packs_short_entries(self):
        short = [core.Entry("demo", f"s{i}", "Iron Gear", "jar") for i in range(50)]
        self.assertEqual(len(core.group_entries(short, batch_size=60, max_chars=3600)), 1)

        long_entries = [core.Entry("demo", f"p{i}", "A" * 1000, "jar") for i in range(5)]
        groups = core.group_entries(long_entries, batch_size=60, max_chars=2500)
        self.assertEqual([len(g) for g in groups], [2, 2, 1])

    def test_item_level_tm_survives_batch_size_change_partial_failure_and_unrelated_glossary(self):
        entries = [core.Entry("demo", f"k{i}", f"Unique item {i}", "jar") for i in range(4)]
        with tempfile.TemporaryDirectory() as tmp:
            # 第一次运行：故意让 k3 缺失（且差量重试也缺），验证 k0..k2 立刻写入条目级 TM。
            first_calls = []

            def fake_first(_engine, masked, _settings, cancel_event=None):
                first_calls.append(set(masked))
                return {k: f"译:{v}" for k, v in masked.items() if k != "k3"}

            settings_1 = {
                "cache_dir": tmp, "batch_size": 4, "concurrency": 1,
                "glossary": {}, "keep": set(), "delay": 0, "timeout": 5,
                "engine": "openai", "model": "m1", "base_url": "https://a.example.test/v1", "api_key": "test",
            }
            with patch.object(ferryengine, "_translate_batch_with_retry", side_effect=fake_first):
                tr1, _err1, fail1 = core.translate_entries(entries, settings_1)
            # 触发了局部差量重试：第一次 4 条，第二次只补翻缺失的 k3（1 条）
            self.assertEqual(first_calls, [{"k0", "k1", "k2", "k3"}, {"k3"}])
            self.assertEqual(set(tr1["demo"]), {"k0", "k1", "k2"})
            self.assertEqual(fail1, {"demo": ["k3"]})

            # 第二次运行：改了 batch_size、换了 base_url、加了无关术语，k0..k2 必须 100% 命中 TM，只翻 k3！
            second_calls = []

            def fake_second(_engine, masked, _settings, cancel_event=None):
                second_calls.append(set(masked))
                return {k: f"译:{v}" for k, v in masked.items()}

            settings_2 = {
                "cache_dir": tmp, "batch_size": 2, "concurrency": 1,
                "glossary": {"UnrelatedTerm": "无关词"}, "keep": set(), "delay": 0, "timeout": 5,
                "engine": "openai", "model": "m2", "base_url": "https://b.example.test/v1", "api_key": "test",
            }
            usage: dict = {}
            with patch.object(ferryengine, "_translate_batch_with_retry", side_effect=fake_second):
                tr2, err2, fail2 = core.translate_entries(entries, settings_2, usage=usage)
            self.assertFalse(err2 or fail2)
            self.assertEqual(second_calls, [{"k3"}])
            self.assertEqual(len(tr2["demo"]), 4)
            self.assertEqual(usage["_saved"]["tm_hits"], 3)


class GameLineAndCliConsistencyTests(unittest.TestCase):
    def test_ferrygame_compact_payload_prefilter_and_menu_dedup(self):
        # 默认 story 且无附加元数据时应输出紧凑字符串，空 context / glossary 应省略。
        compact = json.loads(game_build_input({"1": "こんにちは"}, {"_unit_meta": {"1": {"kind": "story"}}}))
        self.assertEqual(compact, {"strings": {"1": "こんにちは"}})

        units = [
            Unit(key="u1", src="---", kind="menu", file="a.txt", order=1),
            Unit(key="u2", src="セーブ", kind="menu", file="a.txt", order=2),
            Unit(key="u3", src="セーブ", kind="menu", file="b.txt", order=1),
            Unit(key="u4", src="おはよう", kind="story", file="a.txt", order=3),
        ]
        seen_calls = []

        def fake_call(_engine, masked, settings):
            payload = json.loads(settings["model_input_builder"](masked, settings))
            seen_calls.append(payload)
            sink = settings.get("_token_usage")
            if isinstance(sink, dict):
                sink["input_tokens"] += 40
                sink["output_tokens"] += 15
            out = {}
            for sid, val in payload["strings"].items():
                text = val["text"] if isinstance(val, dict) else val
                out[sid] = "存档" if text == "セーブ" else "早安"
            return out

        with tempfile.TemporaryDirectory() as tmp:
            report = translate_units(
                units,
                settings={"engine": "openai", "batch_size": 10},
                source_lang="ja",
                call=fake_call,
                cache_dir=Path(tmp) / "cache",
            )
        self.assertEqual(report.translated, 4)
        self.assertEqual(report.prefiltered, 1)
        self.assertEqual(report.deduped, 1)
        self.assertEqual((report.input_tokens, report.output_tokens), (40, 15))
        self.assertEqual([u.dst for u in units], ["---", "存档", "存档", "早安"])
        self.assertIn("40", report.summary())

    def test_adapters_lazy_extras_and_localize_settings_override(self):
        self.assertIn("pc98", core_adapters.registered())
        self.assertIn("rawbin", core_adapters.registered())
        self.assertIsNotNone(core_adapters.get("pc98"))
        self.assertIsNotNone(core_adapters.get("rawbin"))

        with tempfile.TemporaryDirectory() as tmp:
            settings = ferrylocalize.localize_settings(
                {**core.DEFAULT_CONFIG, "glossary": {"Hero": "勇者"}},
                Path(tmp),
                overrides={"engine": "openai", "model": "gpt-4o-mini", "api_key": "test", "batch_size": 25},
            )
        self.assertEqual(settings["batch_size"], 25)
        self.assertEqual(settings["term_hints"], {"Hero": "勇者"})
        self.assertNotIn("Creeper", settings["term_hints"])
        self.assertIn("video game localizer", settings["system_prompt_override"])

    def test_explain_error_and_redacted_traceback_logging(self):
        # 1) 错误码 14（Errno 14 / WinError 14 / 文本里的 error 14）都必须给出明确中文原因与排查建议
        e14 = OSError(14, "Bad address", "C:/game/app.exe")
        text14 = core.explain_error(e14, stage="写回游戏文件")
        self.assertIn("【写回游戏文件失败】", text14)
        self.assertIn("错误码 14", text14)
        self.assertIn("原因与建议：", text14)
        self.assertIn("涉及文件：C:/game/app.exe", text14)

        raw14 = RuntimeError("error 14")
        text_raw14 = core.explain_error(raw14, stage="汉化游戏")
        self.assertIn("错误码 14", text_raw14)
        self.assertIn("原因与建议：", text_raw14)

        # 2) PermissionError 给出只读/杀软/游戏占用的排查建议
        perm = PermissionError(13, "Permission denied", "C:/game/app.exe")
        text_perm = core.explain_error(perm, stage="写回游戏文件")
        self.assertIn("权限", text_perm)
        self.assertIn("杀毒软件", text_perm)

        # 3) _RedactSecrets 保留脱敏后的异常堆栈，同时绝不泄漏 API key
        import logging
        filt = core._RedactSecrets()
        try:
            raise RuntimeError("boom with sk-secret1234567890")
        except RuntimeError:
            rec = logging.LogRecord("ferry", logging.ERROR, __file__, 1, "worker failed", (), sys.exc_info())
        filt.filter(rec)
        self.assertIsNone(rec.exc_info)
        self.assertIn("RuntimeError", rec.exc_text or "")
        self.assertIn("sk-***", rec.exc_text or "")
        self.assertNotIn("sk-secret1234567890", rec.exc_text or "")


if __name__ == "__main__":
    unittest.main()
