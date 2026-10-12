"""配置与进度：读写 ferry_config.json（API key 经 secrets 落盘加密）、失败进度记录。
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from ferrycore import secrets as secretbox
from ferrycore.base import CONFIG_FILE, LOG, PROGRESS_FILE, atomic_write_text


DEFAULT_CONFIG = {
    "engine": "mymemory",
    "fill_community": False,
    "refine_community": False,
    "community_enabled": True,
    "community_dict_url": "https://cdn.jsdelivr.net/gh/CFPATools/i18n-dict@main",
    "community_notice_accepted": False,
    "pack_name": "AI_Translation_LowPriority.zip",
    "output_dir": "",
    "cache_dir": "",
    "pack_format": 0,
    "batch_size": 30,
    "timeout": 180,
    "delay": 0.2,
    "concurrency": 1,
    # OpenAI 兼容
    "openai_base_url": "https://api.openai.com/v1",
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    # Anthropic 兼容
    "anthropic_base_url": "https://api.anthropic.com",
    "anthropic_api_key": "",
    "anthropic_model": "claude-3-5-haiku-latest",
    # Gemini 兼容
    "gemini_base_url": "",
    "gemini_api_key": "",
    "gemini_model": "gemini-1.5-flash",
    # MyMemory
    "mymemory_email": "",
    # DeepL Free
    "deepl_api_key": "",
    "deepl_free": True,
    # 本地模型（Ollama / LM Studio 等，OpenAI 兼容）
    "local_base_url": "http://127.0.0.1:11434/v1",
    "local_api_key": "",
    "local_model": "qwen2.5",
    # 多模型池：多个引擎并行翻译不同批次，提速。空列表 = 只用单个引擎。
    "model_pool": [],
    # 自定义接口预设：名称 -> {provider, api_key, base_url, model}
    "custom_profiles": {},
    # 术语表（en -> zh）与专有名词限制表（保持不翻译）
    "glossary": {},
    "keep_untranslated": [],
    "skip_keys": {},
    # 是否已确认"部分模组无法完全汉化"的启动声明
    "disclaimer_accepted": False,
    # GUI 状态：首次引导是否不再显示、翻译设置是否展开
    "notice_dismissed": False,
    "settings_expanded": False,
    # 本地版本表查不到 / 表尾待定时，联网核对远程版本表
    "online_pack_format": True,
    # 「查询在线汉化」的自定义来源：每行一个网址模板，{modid} 会替换成模组 ID；
    # CFPA 社区整合包是内置来源，不需要写在这里
    "lookup_sites": [],
    # 启动时自动检查 GitHub 新版本（每 7 天最多一次）
    "update_auto_check": True,
    "update_last_check": 0,
    # 不翻译名单：这些模组完全跳过 AI 翻译（GUI 右键加入）
    "no_translate_mods": [],
    # 翻译时顺带处理硬编码文本（存 sidecar，供摆渡桥做显示层替换）
    "translate_hardcoded": True,
    # 翻译/重翻结束后自动把映射表写进游戏目录（摆渡桥热重载生效），
    # 免去手动点「导出桥接映射」这一步 —— 这就是「扫描 → 翻译 → 一步到位」的闭环。
    "auto_export_bridge": True,
    # 导出桥映射时顺带检查 LiteralBridge 有没有新版，有就下下来替换旧版。
    # 只影响「已经装了 LiteralBridge」的实例（没装的走首次安装那条线）。
    "auto_update_literalbridge": True,
}


def load_config(path: Path = CONFIG_FILE, *, migrate: bool = True) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    raw: Any = None
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                config.update({k: v for k, v in raw.items() if k in DEFAULT_CONFIG})
        except (OSError, ValueError):
            raw = None
    # 老配置（密钥还是明文）在**第一次读取时**就自动加密写回 —— 只在 Windows 上做，
    # 非 Windows 没有 DPAPI，写不出密文，不动文件更诚实。详见 _migrate_plaintext_secrets。
    if migrate and isinstance(raw, dict):
        _migrate_plaintext_secrets(raw, path)
    # 磁盘上存的是 DPAPI 密文（Windows）；这里解回明文供运行期使用。
    # 纯明文的老配置也会原样通过，方便平滑升级。
    return secretbox.unprotect_config(config)


def _migrate_plaintext_secrets(raw: dict[str, Any], path: Path) -> None:
    """把老配置里的明文密钥加密写回磁盘。

    为什么需要这一步：`load_config` 只解密、不回写。升级上来的用户如果不手动点一次
    「保存设置」，明文密钥会**永远**躺在 `ferry_config.json` 里 —— 加了 DPAPI 也等于没加。
    所以任何一次启动都顺手做一次自愈迁移。

    三条安全前提（宁可不写，也不能写坏用户的配置）：
      1. DPAPI 不可用（非 Windows / 调用失败）→ 直接跳过，避免把文件重写成还是明文的样子；
      2. 先做「加密 → 解密 → 与原值逐字比对」的自检，密文解得回来才允许落盘；
      3. 任何异常只记日志 —— 迁移是锦上添花，绝不能拦住程序启动。
    """
    if not secretbox.available():
        return
    if not secretbox.has_plaintext_secret(raw):
        return
    try:
        stored = secretbox.protect_config(raw)
        # 只要还有明文残留（个别字段加密失败会降级成明文），就整个不写，不半吊子。
        if secretbox.has_plaintext_secret(stored):
            LOG.warning("密钥迁移：仍有字段未能加密，保持原文件不动")
            return
        if secretbox.unprotect_config(stored) != raw:
            LOG.warning("密钥迁移：加解密自检未通过，保持原文件不动")
            return
        atomic_write_text(path, json.dumps(stored, ensure_ascii=False, indent=2) + "\n")
        LOG.info("已把明文 API Key 迁移为 DPAPI 密文：%s", path)
    except Exception as exc:  # noqa: BLE001 —— 迁移失败绝不能影响正常启动
        LOG.warning("密钥迁移失败（不影响使用）：%s", exc)


SECRET_KEY_RE = re.compile(r'(?i)"([^"]*api_key)"\s*:\s*"([^"]*)"')


def is_git_ignored(path: Path) -> bool:
    """True when git would ignore this file (uses `git check-ignore`)."""
    try:
        result = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=path.parent,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (OSError, ValueError):
        return False


def config_secret_risk(config_path: Path = CONFIG_FILE) -> str | None:
    """Warn when the config holds a *plaintext* API key and could be committed to git."""
    if not config_path.is_file():
        return None
    try:
        text = config_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # 只对**明文**密钥报警：DPAPI 密文就算被提交也解不开（换机器 / 换账户即失效），
    # 再弹「密钥泄漏」是狼来了，会让人对真警报麻木。
    if not any(value and not secretbox.is_encrypted(value) for _name, value in SECRET_KEY_RE.findall(text)):
        return None
    project = config_path.resolve().parent
    if not (project / ".git").exists():
        return None
    if is_git_ignored(config_path):
        return None
    return (
        f"检测到 {config_path.name} 内含 API Key，且该文件没有被 .gitignore 忽略。\n"
        "把它提交到 GitHub 会公开泄漏密钥。请保留项目自带的 .gitignore，"
        "或改用不含密钥的 ferry_config.example.json。"
    )


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    def mask(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            return {k: mask(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [mask(item, key) for item in value]
        if key.lower().endswith("api_key") or key.lower() == "api_key":
            return "sk-***" if value else ""
        return value
    return mask(config)


def save_config(config: dict[str, Any], path: Path = CONFIG_FILE) -> None:
    try:
        # 落盘前把密钥字段加密（DPAPI）。protect_config 返回副本，不会把
        # 内存里的明文替换成密文 —— GUI 还要拿它填输入框、拿去调 API。
        stored = secretbox.protect_config(config)
        atomic_write_text(path, json.dumps(stored, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise OSError(f"保存配置失败：{path}（{exc.strerror or type(exc).__name__}）") from exc


def load_progress(path: Path = PROGRESS_FILE) -> dict[str, list[str]]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            failed = data.get("failed", {})
            if isinstance(failed, dict):
                return {str(k): [str(x) for x in v] for k, v in failed.items() if isinstance(v, list)}
    except (OSError, ValueError):
        pass
    return {}


def save_progress(failed: dict[str, list[str]], path: Path = PROGRESS_FILE) -> None:
    try:
        atomic_write_text(path, json.dumps({"failed": failed}, ensure_ascii=False, indent=2) + "\n")
    except OSError as exc:
        raise OSError(f"保存失败记录失败：{path}（{exc.strerror or type(exc).__name__}）") from exc


def update_progress(translations: dict[str, dict[str, str]], failed: dict[str, list[str]], path: Path = PROGRESS_FILE) -> None:
    progress = load_progress(path)
    for modid, data in translations.items():
        if modid in progress:
            remaining = [k for k in progress[modid] if k not in data]
            if remaining:
                progress[modid] = remaining
            else:
                del progress[modid]
    for modid, keys in failed.items():
        progress[modid] = sorted(set(progress.get(modid, [])) | set(keys))
    save_progress(progress, path)


def remove_progress_modids(modids: set[str], path: Path = PROGRESS_FILE) -> None:
    progress = load_progress(path)
    for modid in modids:
        progress.pop(modid, None)
    save_progress(progress, path)
