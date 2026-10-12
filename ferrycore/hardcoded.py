"""硬编码文本检测：扒 jar 里的 class 常量池，挑出玩家可见但语言文件覆盖不到的文本。
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zipfile

from pathlib import Path

from ferrycore.base import atomic_write_text
from ferrycore.models import Entry, ScanResult
from ferrycore.scan import is_excluded_mod, language_files_in_zip


# 判断 .class 里的字符串常量是否像“用户可见文本”，用于检测硬编码文本。
_HARDCODED_TECH_START = (
    "could not", "cannot", "can not", "can't", "failed", "invalid", "unknown",
    "missing", "no ", "not ", "unsupported", "error", "illegal", "unexpected",
    "trying to", "unable", "already", "warning", "incorrect", "duplicate",
    "uninitialized", "use of", "versions from", "application aborted",
)


_HARDCODED_TECH_ANY = (
    "config file", "packet", "message id", "message type", "stacktrace", "null",
    "class bytes", "deserializ", "thread", "mixin", "reflect", "logger", "handler",
    "parameter", "argument", "not exist", "does not exist", "not found", "not support",
    "not allowed", "not present", "must be", "must have", "is null", "registry",
    "bytecode", "modifier", "wrapper", "predicate", "instance", "lambda", "buffer",
    "can only", "have to", "need to be", "supposed", "read it backwards",
    "shader", "uniform", "lexer", "parser", "retry", "logging", "backing up",
    "unloaded", "accessor", "vertex", "zlib", "json", "nbt",
    "serializ", "initializ", "registered", "dependency", "namespace", "resource location",
)


def _class_utf8_constants(data: bytes) -> list[str]:
    out: list[str] = []
    if len(data) < 10 or data[:4] != b"\xca\xfe\xba\xbe":
        return out
    count = struct.unpack(">H", data[8:10])[0]
    i, n, index = 10, len(data), 1
    while index < count and i < n:
        tag = data[i]
        i += 1
        if tag == 1:
            length = struct.unpack(">H", data[i:i + 2])[0]
            i += 2
            raw = data[i:i + length]
            i += length
            try:
                out.append(raw.decode("utf-8"))
            except UnicodeDecodeError:
                pass
        elif tag in (7, 8, 16, 19, 20):
            i += 2
        elif tag == 15:
            i += 3
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            i += 4
        elif tag in (5, 6):
            i += 8
            index += 1
        else:
            break
        index += 1
    return out


def _looks_like_ui_text(text: str, min_len: int = 14, min_words: int = 3, max_len: int = 160) -> bool:
    if len(text) < min_len or len(text) > max_len or " " not in text:
        return False
    if not (text[0].isupper() or text[0].islower() or text[0] in "\"'“”‘’«»"):
        return False
    low = text.lower()
    if any(low.startswith(word) for word in _HARDCODED_TECH_START):
        return False
    if any(word in low for word in _HARDCODED_TECH_ANY):
        return False
    if any(ch in text for ch in "/\\;()[]{}=<>@#%&*"):
        return False
    if re.search(r"[a-z][A-Z]", text):
        return False
    printable = sum(ch.isalpha() or ch.isspace() or ch in ".,!?'-\"“”‘’…—«»" for ch in text)
    if printable < len(text) * 0.92:
        return False
    if len([w for w in text.split() if w]) < min_words:
        return False
    return True


def detect_hardcoded_texts(mods_dir: Path, threshold: int = 5, candidate_min_len: int = 4, candidate_min_words: int = 2, candidate_max_len: int = 400) -> list[tuple[str, int, list[str]]]:
    """扫描 mods 目录，找出疑似含硬编码文本（无法通过资源包汉化）的模组。

    返回 [(jar 名, 疑似条数, 样例)]，按条数降序。
    """
    results: list[tuple[str, int, list[str]]] = []
    if not mods_dir.is_dir():
        return results
    for jar in sorted(mods_dir.rglob("*.jar")):
        if is_excluded_mod(jar.stem.lower()):
            continue
        try:
            files = language_files_in_zip(jar)
        except (zipfile.BadZipFile, OSError):
            continue
        modids = {file.modid for file in files}
        if modids and all(is_excluded_mod(modid) for modid in modids):
            continue
        lang_values = [str(value) for file in files if file.locale == "en_us" for value in file.data.values()]
        known = " ".join(lang_values).lower()
        constants: list[str] = []
        try:
            with zipfile.ZipFile(jar) as zf:
                for name in zf.namelist():
                    if not name.endswith(".class"):
                        continue
                    try:
                        constants.extend(_class_utf8_constants(zf.read(name)))
                    except (OSError, struct.error, zipfile.BadZipFile):
                        continue
        except (zipfile.BadZipFile, OSError):
            continue
        # 剥掉 § 格式码再比对：桥接匹配时游戏侧也会剥码，两侧口径一致
        code_stripper = re.compile(chr(167) + ".")
        all_text = sorted({code_stripper.sub("", text).strip() for text in constants if text.strip()}, key=len, reverse=True)
        strict = [text for text in all_text if _looks_like_ui_text(text) and text.lower() not in known]
        relaxed = [text for text in all_text if _looks_like_ui_text(text, min_len=4, min_words=2, max_len=candidate_max_len) and text.lower() not in known]
        if len(strict) >= threshold or len(relaxed) >= 3:
            # 第三个元素是全量候选（宽松档，含短聊天句，按长度降序）；
            # 数量字段用严格档计数，展示方自己取前几条当样例。
            results.append((jar.name, len(strict), relaxed))
    results.sort(key=lambda item: -item[1])
    return results


def hardcoded_modids(scan: ScanResult, findings: list[tuple[str, int, list[str]]]) -> set[str]:
    """Associate heuristic class-string findings with language namespaces in the same jar."""
    flagged_jars = {name for name, _, _ in findings}
    return {
        file.modid
        for file in scan.english
        if Path(file.path.split("!", 1)[0]).name in flagged_jars
    }


def hardcoded_strings_by_modid(scan: ScanResult, findings: list[tuple[str, int, list[str]]], modids: set[str] | None = None) -> dict[str, list[str]]:
    """把 jar 级别的疑似硬编码候选按语言命名空间归组（同 hardcoded_modids 的对应逻辑）。"""
    wanted = {m.lower().replace("-", "_") for m in modids} if modids is not None else None
    result: dict[str, list[str]] = {}
    for jar_name, _count, candidates in findings:
        for file in scan.english:
            if Path(file.path.split("!", 1)[0]).name != jar_name:
                continue
            if wanted is not None and file.modid.lower().replace("-", "_") not in wanted:
                continue
            result.setdefault(file.modid, []).extend(candidates)
    return {modid: list(dict.fromkeys(strings)) for modid, strings in result.items()}


def missing_hardcoded_entries(strings_by_modid: dict[str, list[str]], existing: dict[str, dict[str, str]] | None = None, selected: set[str] | None = None) -> list[Entry]:
    """把疑似硬编码文本包装成可翻译条目（kind="nbt"，key 前缀 nbt:），跳过已有译文的。"""
    result: list[Entry] = []
    for modid, strings in strings_by_modid.items():
        if selected is not None and modid not in selected:
            continue
        have = {en for en, zh in (existing or {}).get(modid, {}).items() if str(zh).strip()}
        for text in dict.fromkeys(strings):
            if not text or not text.strip() or text.strip() in have:
                continue
            key = "nbt:" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            result.append(Entry(modid, key, text, "hardcoded", "nbt"))
    return result


def has_pending_hardcoded(findings: list[tuple[str, int, list[str]]], scan: ScanResult, modids: set[str] | None = None, existing: dict[str, dict[str, str]] | None = None) -> bool:
    return bool(missing_hardcoded_entries(hardcoded_strings_by_modid(scan, findings, modids), existing, modids))


def pending_hardcoded_modids(findings: list[tuple[str, int, list[str]]], scan: ScanResult, existing: dict[str, dict[str, str]] | None = None, modids: set[str] | None = None) -> dict[str, int]:
    """按 modid 数出「还差几条硬编码没翻」。

    只包含「检出过硬编码、且还没翻完」的 modid；全部翻完的不会出现。
    用途：表格状态列 —— 语言文件全通、硬编码也翻完了，才配显示「已 AI 汉化完成」。
    """
    counts: dict[str, int] = {}
    for entry in missing_hardcoded_entries(hardcoded_strings_by_modid(scan, findings, modids), existing, modids):
        counts[entry.modid] = counts.get(entry.modid, 0) + 1
    return counts


def hardcoded_pairs_from_entries(entries: list[Entry], translated: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """翻译完成后，把 nbt: 条目的译文按 英文原文 -> 中文 收拢。"""
    pairs: dict[str, dict[str, str]] = {}
    for entry in entries:
        if entry.kind != "nbt":
            continue
        zh = translated.get(entry.modid, {}).get(entry.key)
        if zh and str(zh).strip():
            pairs.setdefault(entry.modid, {})[entry.english] = str(zh)
    return pairs


def hardcoded_translations_path(pack: Path) -> Path:
    return pack.with_name(pack.name + ".hardcoded.json")


def load_hardcoded_translations(pack: Path) -> dict[str, dict[str, str]]:
    path = hardcoded_translations_path(pack)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for modid, entries in raw.items():
        if isinstance(modid, str) and isinstance(entries, dict):
            clean = {k: str(v) for k, v in entries.items() if isinstance(k, str) and isinstance(v, str) and v.strip()}
            if clean:
                result[modid] = clean
    return result


def save_hardcoded_translations(pack: Path, data: dict[str, dict[str, str]]) -> None:
    path = hardcoded_translations_path(pack)
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


_HARDCODED_LOG_KEYWORDS = {
    "config", "exception", "error", "failed", "loading", "debug", "warning", "logger",
    "registry", "handler", "packet", "mixin", "class", "method", "field", "null",
    "string", "bytes", "layer", "offset", "header", "protocol", "gateway", "http",
    "stack", "trace", "invalid", "unknown", "unsupported", "cannot", "could not",
    "utility", "final", "enabled", "disabled", "shutting", "dump", "task", "report",
    "install", "version", "build", "source", "target", "missing", "request", "response",
    "connection", "server", "already", "provide", "provided", "must", "required",
}


_HARDCODED_CMD_KEYWORDS = {
    "execute", "teleport", "spreadplayers", "stopsound", "scoreboard", "summon",
    "give", "effect", "kill", "title", "say", "tp", "fill", "setblock", "clone",
    "data", "tag", "team", "clear", "particle", "playsound", "@a", "@e", "@p", "@s",
}


# 关键词过滤只在「短串」上生效。这两张表是用来剔除命令 / 日志这类短标识符的；
# 而长句（人类可读的整句）即便偶然包含其中的词，也必然是给玩家看的界面文本。
# 实测：The God 右键生命卷轴的那句长聊天文本，因为含日志词 "already" 被整句误杀，
# 导致纯静态扫描漏掉它 —— 故按长度设闸，长句一律不参与关键词过滤。
_HARDCODED_KEYWORD_MAX_LEN = 40


# Minecraft 命令串的特征（与长度无关，任何长度都要剔除）：
#   1) 以 / 开头；
#   2) 含目标选择器 @a/@e/@p/@r/@s（可带 [ 参数），如 @e[type=boh:rat]。
# 命令串永远不是给玩家看的文本，翻译它毫无意义（桥也不会拿它去匹配任何显示文本）。
# 实测：只按长度设闸后，boh 的 300 条候选里混进了 160 条 /execute、/summon 命令。
_SELECTOR_RE = re.compile(r"@[aeprs](?:\[|\b)")


# 命令动词只在「短串」时才算判据 —— "Used to summon the Specimen 9" 这种说明文本
# 里也会出现 summon，长句不能因为一个动词就被判成命令。
_COMMAND_VERB_RE = re.compile(
    r"^\s*/?\s*(?:execute|summon|playsound|stopsound|particle|spreadplayers|effect|tellraw|"
    r"title|item|kick|teleport|tp|fill|setblock|clone|data|scoreboard|tag|team|clear|say|"
    r"give|kill|attribute|weather|time|gamemode|difficulty|xp|advancement|function|forceload|"
    r"locate|me|msg|tell|seed|spawnpoint|spectate|trigger)\b",
    re.IGNORECASE,
)


def _looks_like_command(text: str) -> bool:
    """判断一段字符串是不是 Minecraft 命令，而不是给玩家看的显示文本。"""
    body = text.lstrip()
    if body.startswith("/"):
        return True
    if _SELECTOR_RE.search(text):
        return True
    if len(text) <= _HARDCODED_KEYWORD_MAX_LEN and _COMMAND_VERB_RE.match(body):
        return True
    return False


def extract_class_strings(data: bytes) -> list[str]:
    if len(data) < 10 or data[:4] != b"\xca\xfe\xba\xbe":
        return []
    idx = 8
    try:
        cp = int.from_bytes(data[idx:idx + 2], "big")
    except IndexError:
        return []
    idx += 2
    strings: list[str] = []
    i = 1
    try:
        while i < cp and idx < len(data):
            tag = data[idx]
            idx += 1
            if tag == 1:
                length = int.from_bytes(data[idx:idx + 2], "big")
                idx += 2
                strings.append(data[idx:idx + length].decode("utf-8", "replace"))
                idx += length
            elif tag in (7, 8, 16, 19, 20):
                idx += 2
            elif tag == 15:
                idx += 3
            elif tag in (3, 4):
                idx += 4
            elif tag in (5, 6):
                idx += 8
                i += 1
            elif tag in (9, 10, 11, 12, 17, 18):
                idx += 4
            else:
                break
            i += 1
    except (IndexError, ValueError):
        pass
    return strings


def looks_like_hardcoded_text(text: str) -> bool:
    if len(text) < 4 or " " not in text:
        return False
    if not re.search(r"[a-zA-Z]", text):
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return False
    if any(x in text for x in ("net.", "com.", "org.", "java/", "()", "(L", "Ljava", ".java", ".class", "mcreator")):
        return False
    if "{}" in text or "%s" in text or "\x01" in text or "\\n" in text:
        return False
    if _looks_like_command(text):
        return False
    if len(text) <= _HARDCODED_KEYWORD_MAX_LEN:
        low = text.lower()
        if any(k in low for k in _HARDCODED_LOG_KEYWORDS):
            return False
        if any(k in low for k in _HARDCODED_CMD_KEYWORDS):
            return False
    return True


def detect_hardcoded_mods(mods_dir: Path) -> dict[str, int]:
    """扫描 mods 目录，返回 {modid: 疑似硬编码可见文本数量}。

    硬编码文本（如直接写在代码里的物品 tooltip、聊天消息）不经过语言文件，
    资源包无法覆盖，因此这类模组无法做到完全汉化。
    """
    result: dict[str, int] = {}
    if not mods_dir.is_dir():
        return result
    for jar in sorted(mods_dir.rglob("*.jar")):
        try:
            lang_files = language_files_in_zip(jar)
        except (zipfile.BadZipFile, OSError):
            continue
        modids = {f.modid for f in lang_files if f.locale == "en_us"}
        if not modids:
            continue
        seen: set[str] = set()
        try:
            with zipfile.ZipFile(jar) as zf:
                for name in zf.namelist():
                    if not name.endswith(".class"):
                        continue
                    data = zf.read(name)
                    for text in extract_class_strings(data):
                        if text not in seen and looks_like_hardcoded_text(text):
                            seen.add(text)
        except (zipfile.BadZipFile, OSError):
            continue
        if seen:
            for modid in modids:
                result[modid] = result.get(modid, 0) + len(seen)
    return result
