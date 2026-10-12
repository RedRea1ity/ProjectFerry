#!/usr/bin/env python3
"""摆渡计划 / ProjectFerry —— 离线 Minecraft 模组汉化资源包生成器。

人无语言则茫然无依，故有摆渡。
Where words fail, we ferry.

只读英文源语言文件和已有中文文件，计算缺失 key。识别 AI 生成的资源包与
社区/官方汉化：检测到某模组已有人工汉化时自动降级（默认不为其生成 AI 翻译）。

支持多翻译引擎：MyMemory（免费，默认）、DeepL Free、OpenAI 兼容、
Anthropic 兼容、Gemini 兼容、本地模型（Ollama / LM Studio）。

────────────────────────────────────────────────────────────────────────
这个文件现在是一个**薄壳**：真正的实现都在 `ferrycore/` 包里，按职责分层。
它自己只做两件事：

  1. 把 `ferrycore` 的名字重新导出 —— GUI（`ProjectFerry.pyw`）和测试都
     `import mc_ai_translator as core`，`core.xxx` 这个入口保持不变；
  2. `python mc_ai_translator.py <命令>` 时把参数转交 `ferrycore.cli`。

想找某个功能，别在这里翻 —— 先看 `ferrycore/` 下的模块名，
每个模块的职责与分层依赖见 `docs/ARCHITECTURE.md`。
────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import sys

# 对外 API 面：全部来自 ferrycore，名字与拆分前完全一致。
# 这一层刻意保留「全量导出」——GUI 与测试的调用面太广，漏一个就是运行期崩。
from ferrycore import secrets as secretbox
from ferrycore.base import AI_MARKER, COMMUNITY_NOTICE, CONFIG_FILE, LOCKS_FILE, LOG, LOG_FILE, PROGRESS_FILE, REVERT_MARKER, SOURCES_MARKER, USER_GLOSSARY_FILE, _RedactSecrets, app_dir, atomic_write_text, configure_logging, redact_secrets
from ferrycore.bridge import COLLECTED_MODID, COLLECTED_NAME, FERRYBRIDGE_EXTERNAL_NAME, LITERALBRIDGE_PAGE, _COLLECTED_BUCKET_HEADER, build_bridge_groups, build_bridge_mapping, build_ferrybridge_external, build_literalbridge_export, collected_entries, load_ferrybridge_external, merge_bridge_lang, parse_collected_file, write_ferrybridge_external
from ferrycore.cli import build_parser, command_bridge_collect, command_config, command_detect, command_glossary, command_scan, command_translate, include_modids_from_args, instance_dirs_from_args, only_modids_from_args
from ferrycore.community import CFPA_PACK_TTL, CFPA_PACK_URL, CFPA_RELEASE_PAGE, community_corpus, community_pack_path, community_pack_translations, download_human_pack, ensure_community_pack, fetch_community_baseline, human_pack_destination
from ferrycore.config import DEFAULT_CONFIG, SECRET_KEY_RE, config_secret_risk, is_git_ignored, load_config, load_progress, public_config, remove_progress_modids, save_config, save_progress, update_progress
from ferrycore.engine import CLOUD_ENGINES, ENGINES_WITH_KEY, _resolve_engine_fields, _translate_anthropic, _translate_batch_with_retry, _translate_deepl, _translate_gemini, _translate_mymemory, _translate_openai, reference_examples, resolve_translate_settings, translate_batch, translate_entries, validate_engine_network
from ferrycore.glossary import BUILTIN_GLOSSARY, BUILTIN_KEEP, DEFAULT_GLOSSARY_EN_URL, DEFAULT_GLOSSARY_ZH_URL, GLOSSARY_TERM_PREFIXES, _term_hint_pattern, fetch_glossary, load_user_glossary, relevant_term_hints
from ferrycore.hardcoded import _COMMAND_VERB_RE, _HARDCODED_CMD_KEYWORDS, _HARDCODED_KEYWORD_MAX_LEN, _HARDCODED_LOG_KEYWORDS, _HARDCODED_TECH_ANY, _HARDCODED_TECH_START, _SELECTOR_RE, _class_utf8_constants, _looks_like_command, _looks_like_ui_text, detect_hardcoded_mods, detect_hardcoded_texts, extract_class_strings, hardcoded_modids, hardcoded_pairs_from_entries, hardcoded_strings_by_modid, hardcoded_translations_path, has_pending_hardcoded, load_hardcoded_translations, looks_like_hardcoded_text, missing_hardcoded_entries, pending_hardcoded_modids, save_hardcoded_translations
from ferrycore.literalbridge import LITERALBRIDGE_RELEASES_API, LITERALBRIDGE_REPO, _LB_RELEASE_CACHE, _LB_RELEASE_TTL, _jar_loaders, _literalbridge_jar_matches, _mc_version_triple, _retire_old_literalbridge, detect_mod_loader, download_literalbridge, installed_literalbridge, literalbridge_releases, mc_range_allows, pick_literalbridge_asset, read_mod_jar_info
from ferrycore.models import Entry, LangFile, ModTranslationStatus, PatchouliFile, ScanResult, mod_status_label, translation_coverage_label
from ferrycore.net import _fetch_json, _http_error_detail, _http_get_json, _http_post_json
from ferrycore.online import check_online_translation, query_online_translations
from ferrycore.packio import YieldReport, _save_uninstalled_ai, _write_pack_file, load_locks, load_pack_reverted, load_pack_sources, load_pack_translations, load_uninstalled_ai, load_yield_history, lock_translation, merge_pack_translations, merged_sources, migrate_legacy_pack, quality_issues, reload_ai_translations, remove_ai_translations, remove_pack_keys, retranslate_pack_keys, revert_mods, uninstalled_ai_path, write_pack, yield_to_community
from ferrycore.scan import DEFAULT_EXCLUDED_MODIDS, DEFAULT_EXCLUDED_NAME_HINTS, LANG_FILE_RE, NESTED_JAR_RE, PATCHOULI_RE, _language_files_in_archive, _modid_keys, _normalize_reverted, _promote_disabled_packs, build_mod_status, candidate_instance_roots, enabled_resource_packs, english_map_for, entries_with_baseline, find_instance_dirs, is_excluded_mod, is_resource_pack_enabled, language_files_in_directory, language_files_in_zip, load_pack_patchouli, load_revert_marker, missing_entries, missing_patchouli_entries, patchouli_fields, patchouli_files_in_zip, patchouli_key, resolve_instance_path, scan_inputs, skip_ignored_entries, translated_patchouli_files
from ferrycore.text import PLACEHOLDER_RE, REFINE_PROMPT, SYSTEM_PROMPT, TRANSLATION_PROMPT_VERSION, _lenient_json_text, _model_input, _parse_ai_json, _parse_lang_raw, _system_prompt, assign_batches, cache_key, format_usage, group_entries, load_json_bytes, load_lang_bytes, mask_all, mask_placeholders, mask_terms, modid_keep_words, placeholder_tokens, restore_and_validate
from ferrycore.update import GITHUB_RELEASE_API, GITHUB_RELEASE_PAGE, download_release_asset, latest_github_release
from ferrycore.versions import MC_VERSION_RE, MIN_MAX_FORMAT_SINCE, PACK_FORMATS, PACK_FORMATS_REMOTE_URLS, REMOTE_FORMAT_TTL, YEAR_VERSION_MIN_SECOND, _parse_remote_formats, _remote_format_cache_path, _version_tuple, clean_pack_format, client_jars, client_pack_format, client_version_json, detect_minecraft_version, detect_pack_format, normalize_version_tuple, pack_format_for_version, pack_format_for_version_remote, read_pack_format, resolve_pack_format, split_pack_format, uses_min_max_format, version_key


if __name__ == "__main__":
    args = build_parser().parse_args()
    try:
        raise SystemExit(args.func(args))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
