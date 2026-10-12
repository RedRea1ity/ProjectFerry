"""一键诊断包：把「排查摆渡这次到底卡在哪」需要的信息收成一个纯文本文件。

为什么单独做这个功能：出问题时，用户手上其实什么证据都没有 —— 日志在 `ferry.log`、
桥的实时状态在游戏目录的 `ferrybridge-status.txt`、装了哪些模组在 `mods/`。
让一个只想把模组汉化的玩家自己去凑齐这些、再找到作者发过去，中间每一步都会掉人，
最后多半只剩一句「有问题」，没有下文。

于是这里的唯一目标就是：**点一下，拿到一个可以整份发出去的文件。**

安全边界（这个文件是要发到公开 issue / 聊天框里的，必须干净）：

  · **永不写入密钥的值** —— 配置部分只写「字段设没设、是明文还是密文」；
  · 从磁盘读来的文本（日志、桥的状态文件）一律先过 :func:`redact_secrets`；
  · 密钥在摆渡里本来就是 DPAPI 密文落盘的，这里再脱一层是为了防「用户自己
    把明文贴进了别的地方」这种情况。
"""

from __future__ import annotations

import json
import platform
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ferrycore import secrets as secretbox
from ferrycore.base import CONFIG_FILE, LOG_FILE, app_dir, redact_secrets

# 诊断包固定放在数据目录下的子目录里，免得把仓库根 / exe 目录堆乱。
DIAGNOSE_DIRNAME = "diagnostics"

DIAGNOSE_PREFIX = "ferry-diagnose-"

# 日志尾部最多收这么多行；日志可能几十兆，不能整个塞进诊断包。
MAX_LOG_LINES = 400

# 单行日志超过这个长度就截断（有些 stacktrace 单行极长）。
MAX_LINE_CHARS = 500

# 实例里那个桥状态文件不大，整份收进来。
GAME_STATUS_NAME = "ferrybridge-status.txt"

# 固定名诊断包（桥在游戏里写的那份），只报存在与大小，不整份塞进来。
GAME_REPORT_NAME = "ferrybridge-diagnose.txt"

# 这些字段的值可以直接展示（都是设置项，不含密钥、不含个人信息）。
_SAFE_VALUE_KEYS = {
    "engine", "batch_size", "timeout", "delay", "concurrency", "pack_format",
    "pack_name", "fill_community", "refine_community", "community_enabled",
    "community_notice_accepted", "disclaimer_accepted", "notice_dismissed",
    "settings_expanded", "online_pack_format", "translate_hardcoded",
    "auto_export_bridge", "auto_update_literalbridge", "update_auto_check",
    "deepl_free", "openai_model", "anthropic_model", "gemini_model", "local_model",
    "openai_base_url", "anthropic_base_url", "gemini_base_url", "local_base_url",
    "community_dict_url",
}


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _clip(text: str) -> str:
    text = text.replace("\r", "")
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + "…"


def _tail_text(path: Path, max_lines: int) -> str:
    """取文件结尾若干行；读不动就返回一句说明（诊断不能因为读文件失败而中断）。"""
    try:
        if not path.is_file():
            return ""
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"（读取失败：{type(exc).__name__}）"
    lines = raw.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(_clip(line) for line in lines)


def _config_shape(path: Path | None = None) -> list[str]:
    """配置概览：只写字段与「设没设 / 明文还是密文」，**不写密钥的值**。

    路径留成参数（默认取 `CONFIG_FILE`）而不是写死默认值，这样测试能指向临时文件，
    不会去读真实配置。
    """
    path = path or CONFIG_FILE
    if not path.is_file():
        return ["  （还没有配置文件，说明从没保存过设置）"]
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        return [f"  （配置读取失败：{type(exc).__name__}）"]
    if not isinstance(raw, dict):
        return ["  （配置文件的根不是对象，可能被改坏了）"]

    lines: list[str] = []
    for key in sorted(raw):
        value = raw[key]
        if secretbox.is_secret_field(key):
            if isinstance(value, str) and value:
                state = "已设置（密文 dpapi）" if secretbox.is_encrypted(value) else "已设置（!! 明文 !!)"
            else:
                state = "未设置"
            lines.append(f"  {key} = {state}")
        elif key in _SAFE_VALUE_KEYS and isinstance(value, (str, int, float, bool)):
            lines.append(f"  {key} = {_clip(str(value))}")
        elif isinstance(value, (str, int, float, bool)) or value is None:
            # 其余标量可能是路径之类，只报形状不给值。
            lines.append(f"  {key} = <{type(value).__name__}>")
        elif isinstance(value, (list, dict)):
            lines.append(f"  {key} = <{type(value).__name__}，{len(value)} 项>")
        else:
            lines.append(f"  {key} = <{type(value).__name__}>")

    # 嵌套的密钥（多模型池 / 自定义预设）单独点出来 —— 它们是「配置里还有明文吗」
    # 这个问题最容易漏看的地方。
    for container in ("model_pool", "custom_profiles"):
        node = raw.get(container)
        if node and secretbox.has_plaintext_secret(node):
            lines.append(f"  !! {container} 里仍有明文密钥（DPAPI 没覆盖到）")
    return lines


def _environment(app_version: str) -> list[str]:
    frozen = bool(getattr(sys, "frozen", False))
    return [
        f"  摆渡版本   : {app_version or '（未知）'}",
        f"  运行方式   : {'打包后的 exe' if frozen else '源码（Python 直接运行）'}",
        f"  Python     : {platform.python_version()}",
        f"  操作系统   : {platform.platform()}",
        f"  数据目录   : {app_dir()}",
    ]


def _instance_section(instance: Path | None) -> list[str]:
    lines: list[str] = []
    if instance is None:
        return ["  （这次没有选中任何实例，游戏侧的信息收不到）"]
    lines.append(f"  实例路径   : {instance}")
    mods = instance / "mods"
    try:
        jars = sorted(p.name for p in mods.iterdir() if p.is_file() and p.suffix.lower() == ".jar")
    except OSError:
        jars = []
    lines.append(f"  mods 目录  : {'找不到' if not mods.is_dir() else f'{len(jars)} 个 jar'}")

    status = instance / GAME_STATUS_NAME
    if status.is_file():
        lines.append(f"  {GAME_STATUS_NAME} : 有（见下）")
    else:
        lines.append(f"  {GAME_STATUS_NAME} : 没有（游戏里没启动过，或桥没加载）")
    report = instance / GAME_REPORT_NAME
    if report.is_file():
        lines.append(f"  {GAME_REPORT_NAME} : 有（{report.stat().st_size} 字节，在游戏目录里）")
    else:
        lines.append(f"  {GAME_REPORT_NAME} : 没有（进游戏按 F9 就会生成）")
    return lines


def build_diagnostic_report(
    *,
    app_version: str = "",
    instance: Path | None = None,
    scan_summary: dict[str, Any] | None = None,
    log_lines: int = MAX_LOG_LINES,
) -> str:
    """组装诊断包正文（纯文本）。抽出来是为了可测：不写盘也能检查内容。"""
    parts: list[str] = []
    parts.append("摆渡计划 ProjectFerry 诊断包")
    parts.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    parts.append("（纯文本。要反馈问题时把整个文件发出去就行 —— 粘贴或当附件都可以。）")
    parts.append("")

    parts.append("【运行环境】")
    parts.extend(_environment(app_version))
    parts.append("")

    parts.append("【当前实例 / 游戏侧】")
    parts.extend(_instance_section(instance))
    parts.append("")

    if scan_summary:
        parts.append("【最近一次扫描】")
        for key, value in scan_summary.items():
            parts.append(f"  {key}: {_clip(str(value))}")
        parts.append("")

    parts.append("【配置概览】（只写「设没设」，不写密钥的值）")
    parts.extend(_config_shape())
    parts.append("")

    if instance is not None:
        status_text = _tail_text(instance / GAME_STATUS_NAME, 60)
        if status_text:
            parts.append(f"【游戏里桥的实时状态】（{GAME_STATUS_NAME}）")
            parts.append(redact_secrets(status_text))
            parts.append("")

    log_text = _tail_text(LOG_FILE, log_lines)
    parts.append(f"【摆渡运行日志 · 结尾 {min(log_lines, 9999)} 行】（已脱敏）")
    parts.append(redact_secrets(log_text) if log_text else "  （还没有日志，说明这次是全新的）")
    parts.append("")

    parts.append("【说明】")
    parts.append("  · 这份文件里没有你的 API Key —— 配置一栏只写了「设了没、是明文还是密文」。")
    parts.append("  · 日志里可能带你的文件路径与模组清单；不想发的话删掉对应段落即可，")
    parts.append("    不影响作者判断汉化本身的问题。")
    return "\n".join(parts) + "\n"


def write_diagnostic_report(
    *,
    app_version: str = "",
    instance: Path | None = None,
    scan_summary: dict[str, Any] | None = None,
    dest: Path | None = None,
) -> Path:
    """把诊断包写到磁盘，返回文件路径。

    默认落在 `<数据目录>/diagnostics/ferry-diagnose-<时间戳>.txt`，每份都留档。
    """
    text = build_diagnostic_report(
        app_version=app_version, instance=instance, scan_summary=scan_summary
    )
    if dest is None:
        directory = app_dir() / DIAGNOSE_DIRNAME
        dest = directory / f"{DIAGNOSE_PREFIX}{_stamp()}.txt"
    else:
        directory = dest.parent
    directory.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest
