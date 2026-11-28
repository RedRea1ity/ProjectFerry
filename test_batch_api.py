"""官方 Batch API 开放接口的完整离线端到端测试（OpenAI / Anthropic / Gemini）。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mc_ai_translator as core
from ferrycore import batch_api


class BatchApiOpenInterfaceTests(unittest.TestCase):
    def _sample_entries(self) -> list[core.Entry]:
        return [
            core.Entry("demo", "k_ph", "%1$s / %2$s", "jar"),
            core.Entry("demo", "item.mod.long_sword_key", "Deals %s damage", "jar"),
            core.Entry("demo", "item.mod.duplicate_sword_key", "Deals %s damage", "jar"),
            core.Entry("demo", "item.mod.shield_key", "Blocks incoming hits", "jar"),
        ]

    def test_openai_batch_full_lifecycle_and_tm_writeback(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            settings = {
                "engine": "openai",
                "model": "gpt-4o-mini",
                "base_url": "https://api.example.test/v1",
                "api_key": "sk-test-key",
                "cache_dir": str(cache_dir),
                "batch_size": 10,
                "max_batch_chars": 3600,
                "glossary": {},
                "keep": set(),
                "timeout": 30,
            }
            entries = self._sample_entries()
            job = batch_api.prepare_batch_job(entries, settings)
            self.assertEqual(job.status, "prepared")
            self.assertEqual(job.prefiltered, 1)
            self.assertEqual(job.deduped, 1)
            self.assertEqual(job.request_count, 1)
            jsonl_lines = Path(job.jsonl_path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(jsonl_lines), 1)
            rec = json.loads(jsonl_lines[0])
            self.assertEqual(rec["url"], "/v1/chat/completions")
            user_obj = json.loads(rec["body"]["messages"][1]["content"])
            self.assertEqual(user_obj, {"1": "Deals <p0> damage", "2": "Blocks incoming hits"})

            # Mock submit -> poll -> collect
            def fake_raw(url, *, method="GET", data=None, headers=None, timeout=180):
                if method == "POST" and url.endswith("/files"):
                    return json.dumps({"id": "file-abc123"}).encode("utf-8")
                if method == "GET" and url.endswith("/batches/batch-xyz"):
                    return json.dumps({
                        "id": "batch-xyz",
                        "status": "completed",
                        "request_counts": {"total": 1, "completed": 1, "failed": 0},
                        "output_file_id": "file-out-999",
                    }).encode("utf-8")
                if method == "GET" and url.endswith("/files/file-out-999/content"):
                    line = json.dumps({
                        "custom_id": rec["custom_id"],
                        "response": {
                            "body": {
                                "choices": [{"message": {"content": json.dumps({"1": "造成 <p0> 点伤害", "2": "格挡来袭攻击"}, ensure_ascii=False)}}],
                                "usage": {"prompt_tokens": 110, "completion_tokens": 28, "prompt_tokens_details": {"cached_tokens": 60}},
                            }
                        },
                    }, ensure_ascii=False)
                    return (line + "\n").encode("utf-8")
                raise AssertionError(f"Unexpected raw call: {method} {url}")

            def fake_post(url, payload, headers, timeout):
                if url.endswith("/batches"):
                    self.assertEqual(payload["input_file_id"], "file-abc123")
                    return {"id": "batch-xyz", "status": "validating"}
                raise AssertionError(f"Unexpected post call: {url}")

            with patch.object(batch_api, "_http_request_raw", side_effect=fake_raw), patch.object(batch_api, "_http_post_json", side_effect=fake_post):
                submitted = batch_api.submit_batch_job(job, settings)
                self.assertEqual(submitted.remote_id, "batch-xyz")
                self.assertEqual(submitted.status, "submitted")

                polled = batch_api.poll_batch_job(submitted.job_id, settings)
                self.assertEqual(polled.status, "completed")
                self.assertEqual(polled.output_file_id, "file-out-999")

                translations, errors, failed, done_job = batch_api.collect_batch_results(polled.job_id, settings)

            self.assertFalse(errors or failed)
            self.assertEqual(translations["demo"], {
                "k_ph": "%1$s / %2$s",
                "item.mod.long_sword_key": "造成 %s 点伤害",
                "item.mod.duplicate_sword_key": "造成 %s 点伤害",
                "item.mod.shield_key": "格挡来袭攻击",
            })
            self.assertEqual((done_job.input_tokens, done_job.output_tokens, done_job.cached_tokens), (110, 28, 60))
            self.assertEqual(len(batch_api.list_batch_jobs(cache_dir)), 1)

            # 取回结果已写入条目级 TM：再跑实时翻译应 0 次 API 调用全部命中缓存！
            with patch.object(core, "_http_post_json", side_effect=AssertionError("应全部命中 TM/预过滤")):
                usage: dict = {}
                sync_tr, sync_err, sync_fail = core.translate_entries(entries, settings, usage=usage)
            self.assertFalse(sync_err or sync_fail)
            self.assertEqual(sync_tr["demo"], translations["demo"])
            self.assertEqual(usage["_saved"]["tm_hits"], 3)

    def test_anthropic_and_gemini_batch_lifecycles(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            entries = [core.Entry("demo", "k1", "Golden Pickaxe", "jar")]

            # --- Anthropic ---
            ant_settings = {
                "engine": "anthropic", "model": "claude-3-5-haiku-latest",
                "base_url": "https://api.anthropic.com", "api_key": "sk-ant-test",
                "cache_dir": str(cache_dir), "batch_size": 10, "glossary": {}, "keep": set(), "timeout": 30,
            }
            ant_job = batch_api.prepare_batch_job(entries, ant_settings)

            def ant_post(url, payload, headers, timeout):
                if url.endswith("/v1/messages/batches"):
                    self.assertEqual(len(payload["requests"]), 1)
                    return {"id": "msgbatch_123"}
                if url.endswith("/v1/messages/batches/msgbatch_123/cancel"):
                    return {"id": "msgbatch_123", "processing_status": "canceling"}
                raise AssertionError(url)

            def ant_raw(url, *, method="GET", data=None, headers=None, timeout=180):
                if url.endswith("/v1/messages/batches/msgbatch_123"):
                    return json.dumps({
                        "id": "msgbatch_123",
                        "processing_status": "ended",
                        "request_counts": {"succeeded": 1, "errored": 0, "expired": 0, "canceled": 0},
                        "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_123/results",
                    }).encode("utf-8")
                if url.endswith("/results"):
                    return (json.dumps({
                        "custom_id": "b00001",
                        "result": {
                            "type": "succeeded",
                            "message": {
                                "content": [{"type": "text", "text": json.dumps({"1": "金镐"}, ensure_ascii=False)}],
                                "usage": {"input_tokens": 20, "cache_read_input_tokens": 50, "output_tokens": 8},
                            },
                        },
                    }, ensure_ascii=False) + "\n").encode("utf-8")
                raise AssertionError(url)

            with patch.object(batch_api, "_http_post_json", side_effect=ant_post), patch.object(batch_api, "_http_request_raw", side_effect=ant_raw):
                ant_job = batch_api.submit_batch_job(ant_job, ant_settings)
                tr, err, fail, done = batch_api.collect_batch_results(ant_job.job_id, ant_settings, write_tm=False)
                self.assertFalse(err or fail)
                self.assertEqual(tr, {"demo": {"k1": "金镐"}})
                self.assertEqual((done.input_tokens, done.cached_tokens, done.output_tokens), (70, 50, 8))
                canceled = batch_api.cancel_batch_job(ant_job.job_id, ant_settings)
                self.assertEqual(canceled.status, "cancelled")

            # --- Gemini ---
            gem_settings = {
                "engine": "gemini", "model": "gemini-2.5-flash",
                "base_url": "https://generativelanguage.googleapis.com/v1beta", "api_key": "AIza-test",
                "cache_dir": str(cache_dir), "batch_size": 10, "glossary": {}, "keep": set(), "timeout": 30,
                "disable_thinking": True,
            }
            gem_job = batch_api.prepare_batch_job(entries, gem_settings, bypass_cache=True)

            def gem_post(url, payload, headers, timeout):
                self.assertIn(":batchGenerateContent", url)
                req = payload["batch"]["input_config"]["requests"]["requests"][0]
                self.assertEqual(req["request"]["generationConfig"]["thinkingConfig"], {"thinkingBudget": 0})
                return {"name": "batches/gem-777"}

            def gem_raw(url, *, method="GET", data=None, headers=None, timeout=180):
                self.assertTrue(url.endswith("/batches/gem-777"))
                return json.dumps({
                    "name": "batches/gem-777",
                    "done": True,
                    "metadata": {"state": "BATCH_STATE_SUCCEEDED", "batchStats": {"successfulRequestCount": 1}},
                    "response": {
                        "inlinedResponses": {
                            "inlinedResponses": [{
                                "metadata": {"key": "b00001"},
                                "response": {
                                    "candidates": [{"content": {"parts": [{"text": json.dumps({"1": "黄金镐"}, ensure_ascii=False)}]}}],
                                    "usageMetadata": {"promptTokenCount": 60, "candidatesTokenCount": 9, "cachedContentTokenCount": 30},
                                },
                            }]
                        }
                    },
                }, ensure_ascii=False).encode("utf-8")

            with patch.object(batch_api, "_http_post_json", side_effect=gem_post), patch.object(batch_api, "_http_request_raw", side_effect=gem_raw):
                gem_job = batch_api.submit_batch_job(gem_job, gem_settings)
                tr, err, fail, done = batch_api.collect_batch_results(gem_job.job_id, gem_settings, write_tm=False)
                self.assertFalse(err or fail)
                self.assertEqual(tr, {"demo": {"k1": "黄金镐"}})
                self.assertEqual((done.input_tokens, done.output_tokens, done.cached_tokens), (60, 9, 30))

    def test_domestic_batch_presets_bigmodel_dashscope_siliconflow_qianfan(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache_dir = Path(tmp) / "cache"
            entries = [core.Entry("demo", "k1", "Iron Helmet", "jar")]

            # 1) 智谱 BigModel：自动识别 /v4/chat/completions（JSONL 与 /batches 均必须为 /v4）
            zhipu_settings = {
                "engine": "bigmodel",
                "api_key": "zhipu-key",
                "cache_dir": str(cache_dir),
                "batch_size": 10,
                "glossary": {},
                "keep": set(),
                "timeout": 30,
            }
            zhipu_job = batch_api.prepare_batch_job(entries, zhipu_settings)
            self.assertEqual(zhipu_job.provider, "openai")
            self.assertEqual(zhipu_job.base_url, "https://open.bigmodel.cn/api/paas/v4")
            self.assertEqual(zhipu_job.endpoint, "/v4/chat/completions")
            line = json.loads(Path(zhipu_job.jsonl_path).read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(line["url"], "/v4/chat/completions")

            def zhipu_raw(url, *, method="GET", data=None, headers=None, timeout=180):
                if method == "POST" and url.endswith("/paas/v4/files"):
                    return json.dumps({"id": "file-zp-1"}).encode("utf-8")
                if method == "GET" and url.endswith("/paas/v4/batches/batch-zp-1"):
                    return json.dumps({"id": "batch-zp-1", "status": "succeeded", "output_file_id": "out-zp-1"}).encode("utf-8")
                raise AssertionError(f"Unexpected: {method} {url}")

            def zhipu_post(url, payload, headers, timeout):
                self.assertTrue(url.endswith("/paas/v4/batches"))
                self.assertEqual(payload["endpoint"], "/v4/chat/completions")
                return {"id": "batch-zp-1"}

            with patch.object(batch_api, "_http_request_raw", side_effect=zhipu_raw), patch.object(batch_api, "_http_post_json", side_effect=zhipu_post):
                zhipu_job = batch_api.submit_batch_job(zhipu_job, zhipu_settings)
                polled = batch_api.poll_batch_job(zhipu_job, zhipu_settings)
            self.assertEqual(polled.status, "completed")

            # 2) 阿里云百炼 DashScope + 开启关思考开关
            dash_settings = {
                "engine": "dashscope",
                "model": "qwen3-32b",
                "api_key": "sk-dash",
                "cache_dir": str(cache_dir),
                "batch_size": 10,
                "glossary": {},
                "keep": set(),
                "disable_thinking": True,
                "timeout": 30,
            }
            dash_job = batch_api.prepare_batch_job(entries, dash_settings, bypass_cache=True)
            self.assertEqual(dash_job.base_url, "https://dashscope.aliyuncs.com/compatible-mode/v1")
            self.assertEqual(dash_job.endpoint, "/v1/chat/completions")
            dash_line = json.loads(Path(dash_job.jsonl_path).read_text(encoding="utf-8").splitlines()[0])
            self.assertIs(dash_line["body"].get("enable_thinking"), False)

            # 3) 硅基流动 & 百度千帆预设完整性
            for name in ("siliconflow", "qianfan"):
                self.assertIn(name, batch_api.BATCH_PRESETS)
                self.assertTrue(batch_api.BATCH_PRESETS[name]["base_url"].startswith("https://"))


if __name__ == "__main__":
    unittest.main()
