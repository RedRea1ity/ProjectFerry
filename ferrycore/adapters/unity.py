"""Unity 引擎适配器（StreamingAssets 明文词表 + .NET #US 元数据抽取 + XUnity 预翻译包写出）。

主流 Unity 游戏的文本分布在三处：
1. `*_Data/StreamingAssets/` 下的明文 `.json` / `.txt` / `.csv` 本地化文件；
2. Mono 后端 `*_Data/Managed/Assembly-CSharp.dll` 的 CLI 元数据 `#US`（User Strings）流；
3. AssetBundle / `resources.assets` / IL2CPP `global-metadata.dat`。

本适配器采用零第三方依赖的纯标准库实现：
- 直接解析 PE/CLI 元数据根 `BSJB` 与 `#US` UTF-16LE 用户字符串流；
- 原地回写 `StreamingAssets` 明文词表；
- 同时写出开源生态标准 **XUnity.AutoTranslator** 静态预翻译字典
  `BepInEx/Translation/zh/Text/_PreTranslated.txt` 与默认 `AutoTranslatorConfig.ini`，
  全程接入 `UndoLog`（`revert` 一键无损还原）。
"""

from __future__ import annotations

import json
import re
import struct
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .rpgmaker import parse_pointer, set_pointer
from .undo import UNDO_NAME, UndoLog

try:
    import UnityPy  # type: ignore

    HAS_UNITYPY = True
except Exception:
    UnityPy = None
    HAS_UNITYPY = False

_ZH_LANG_TOKENS = ("zh", "cn", "chs", "chinese", "hans")
_SRC_LANG_PRIORITY = ("en", "en_us", "en-us", "english", "jp", "ja", "jpn", "japanese", "kr", "ko", "kor", "korean")
_UNITY_ASSET_MAX_BYTES = 256 * 1024 * 1024

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_CODE_OR_PATH_RE = re.compile(
    r"(?:^[A-Za-z0-9_./\\:-]+$)"
    r"|(?:^(?:Assets|UnityEngine|System|TMPro|Mono|il2cpp|Shader|Hidden|Custom|GUI|UI)[./_])"
    r"|(?:\.(?:png|jpg|jpeg|ogg|wav|mp3|prefab|mat|asset|unity|shader|json|xml|txt|csv|dll|bytes)$)"
    r"|(?:[{};=<>]{2,})"
    r"|(?:^\s*<[/?a-zA-Z])",
    re.IGNORECASE,
)


def is_unity_translatable(text: str) -> bool:
    """判定一条 Unity 字符串是否属于玩家可见文本，过滤内部类名/路径/Shader/代码。"""
    if not isinstance(text, str):
        return False
    s = text.strip()
    if len(s) < 2 or len(s) > 2000:
        return False
    if any(ord(ch) < 0x20 and ch not in ("\n", "\r", "\t") for ch in s):
        return False
    if _CJK_RE.search(s):
        return True
    if _CODE_OR_PATH_RE.search(s):
        return False
    if "_" in s and " " not in s:
        return False
    # 驼峰标识符（如 PlayerController / OnClickButton）
    if " " not in s and re.search(r"[a-z][A-Z]", s):
        return False
    words = _LATIN_WORD_RE.findall(s)
    return len(words) >= 2 and " " in s


def _read_compressed_uint(blob: bytes, pos: int) -> tuple[int, int]:
    """读取 ECMA-335 II.23.2 压缩无符号整数，返回 `(value, next_pos)`。"""
    b0 = blob[pos]
    if (b0 & 0x80) == 0:
        return b0, pos + 1
    if (b0 & 0xC0) == 0x80:
        if pos + 2 > len(blob):
            raise ValueError("compressed uint16 越界")
        return ((b0 & 0x3F) << 8) | blob[pos + 1], pos + 2
    if (b0 & 0xE0) == 0xC0:
        if pos + 4 > len(blob):
            raise ValueError("compressed uint32 越界")
        val = ((b0 & 0x1F) << 24) | (blob[pos + 1] << 16) | (blob[pos + 2] << 8) | blob[pos + 3]
        return val, pos + 4
    raise ValueError(f"非法压缩整数头字节: 0x{b0:02X}")


def extract_dotnet_us_strings(pe_bytes: bytes) -> list[tuple[int, str]]:
    """从 .NET 程序集（如 `Assembly-CSharp.dll`）的 `#US` 元数据流抽取 `(us_offset, string)`。

    遵循 ECMA-335 CLI Metadata Root (`BSJB`) 规范：
    - `BSJB` (4B) + Major(2B) + Minor(2B) + Reserved(4B) + VersionLen(4B) + Version(VersionLen)
    - Flags(2B) + Streams(2B)
    - 每个 Stream Header: Offset(4B) + Size(4B) + Null-terminated ASCII Name (4B 对齐)
    - `#US` 流内按压缩长度逐条存放 UTF-16LE 字符串 + 1 字节尾部标记。
    """
    results: list[tuple[int, str]] = []
    search_pos = 0
    while True:
        idx = pe_bytes.find(b"BSJB", search_pos)
        if idx < 0 or idx + 16 > len(pe_bytes):
            break
        search_pos = idx + 4
        try:
            major, minor, _reserved, ver_len = struct.unpack_from("<HHII", pe_bytes, idx + 4)
            if major > 10 or minor > 10 or ver_len == 0 or ver_len > 256:
                continue
            ver_aligned = (ver_len + 3) & ~3
            pos = idx + 16 + ver_aligned
            if pos + 4 > len(pe_bytes):
                continue
            _flags, stream_count = struct.unpack_from("<HH", pe_bytes, pos)
            pos += 4
            if stream_count == 0 or stream_count > 32:
                continue

            us_offset = None
            us_size = None
            for _ in range(stream_count):
                if pos + 8 > len(pe_bytes):
                    break
                s_off, s_size = struct.unpack_from("<II", pe_bytes, pos)
                pos += 8
                name_end = pe_bytes.find(b"\x00", pos, min(len(pe_bytes), pos + 32))
                if name_end < 0:
                    break
                s_name = pe_bytes[pos:name_end].decode("ascii", errors="replace")
                # Stream name 含结尾 \0 后向 4 字节对齐
                name_len = (name_end - pos) + 1
                pos += (name_len + 3) & ~3
                if s_name == "#US":
                    us_offset = idx + s_off
                    us_size = s_size

            if us_offset is None or us_size is None:
                continue
            if us_offset + us_size > len(pe_bytes):
                continue

            # 解析 #US 流（第 0 字节固定是 0x00 空串）
            cur = us_offset + 1
            end = us_offset + us_size
            while cur < end:
                entry_rel = cur - us_offset
                try:
                    byte_len, cur = _read_compressed_uint(pe_bytes, cur)
                except ValueError:
                    break
                if byte_len == 0:
                    continue
                if cur + byte_len > end:
                    break
                str_bytes = pe_bytes[cur : cur + byte_len - 1]
                cur += byte_len
                if len(str_bytes) < 2 or (len(str_bytes) % 2) != 0:
                    continue
                try:
                    text = str_bytes.decode("utf-16le")
                except UnicodeDecodeError:
                    continue
                if is_unity_translatable(text):
                    results.append((entry_rel, text))
            if results:
                return results
        except struct.error:
            continue
    return results


def extract_il2cpp_metadata_strings(blob: bytes) -> list[tuple[int, str]]:
    """解析 Unity IL2CPP `global-metadata.dat`（魔数 `0xFAB11BAF`）的 `stringLiteral` 字符串常量池。"""
    if len(blob) < 24:
        return []
    sanity, version, lit_off, lit_size, data_off, data_size = struct.unpack_from("<IiIIII", blob, 0)
    if sanity != 0xFAB11BAF or version < 16 or version > 40:
        return []
    if lit_size == 0 or (lit_size % 8) != 0:
        return []
    if lit_off + lit_size > len(blob) or data_off + data_size > len(blob):
        return []

    count = lit_size // 8
    results: list[tuple[int, str]] = []
    for idx in range(min(count, 500000)):
        length, rel_data_off = struct.unpack_from("<II", blob, lit_off + idx * 8)
        if length < 2 or length > 4096 or rel_data_off + length > data_size:
            continue
        raw = blob[data_off + rel_data_off : data_off + rel_data_off + length]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if is_unity_translatable(text):
            results.append((idx, text))
    return results


def rebuild_il2cpp_metadata(blob: bytes, replacements: dict[str, str]) -> tuple[bytes, int]:
    """重建 Unity IL2CPP `global-metadata.dat` 的 `stringLiteral` 与 `stringLiteralData` 表。

    由于 `stringLiteral` 索引表固定为每项 8 字节 `(length, data_index)`，将新的 `stringLiteralData`
    UTF-8 数据块放置于原区间或追加至文件末尾并更新 Header Offset 16/20，即可支持任意长度简体中文而不破坏任何其它元数据偏移！
    """
    if len(blob) < 24:
        return blob, 0
    sanity, version, lit_off, lit_size, data_off, data_size = struct.unpack_from("<IiIIII", blob, 0)
    if sanity != 0xFAB11BAF or (lit_size % 8) != 0:
        return blob, 0
    if lit_off + lit_size > len(blob) or data_off + data_size > len(blob):
        return blob, 0

    count = lit_size // 8
    new_lits = bytearray(lit_size)
    new_data = bytearray()
    applied = 0

    for idx in range(count):
        length, rel_data_off = struct.unpack_from("<II", blob, lit_off + idx * 8)
        raw = blob[data_off + rel_data_off : data_off + rel_data_off + length]
        tr = replacements.get(f"IL2CPP:{idx}")
        if tr is None and 2 <= length <= 4096:
            try:
                src_text = raw.decode("utf-8")
                tr = replacements.get(src_text)
                if tr == src_text:
                    tr = None
            except UnicodeDecodeError:
                tr = None
        if tr and tr.strip():
            raw = tr.encode("utf-8")
            applied += 1
        new_rel_off = len(new_data)
        new_data.extend(raw)
        struct.pack_into("<II", new_lits, idx * 8, len(raw), new_rel_off)

    if applied == 0:
        return blob, 0

    buf = bytearray(blob)
    buf[lit_off : lit_off + lit_size] = new_lits
    if data_off + data_size == len(blob) or len(new_data) <= data_size:
        # 原 stringLiteralData 在文件末尾或新数据能直接放进原区间
        out = buf[:data_off] + new_data + buf[data_off + data_size :]
        if data_off + data_size == len(blob):
            struct.pack_into("<I", out, 20, len(new_data))
        elif len(new_data) <= data_size:
            out = buf
            out[data_off : data_off + len(new_data)] = new_data
            struct.pack_into("<I", out, 20, len(new_data))
        return bytes(out), applied

    # 追加到文件末尾（4 字节对齐）并更新 Header offset 16 (stringLiteralDataOffset) 与 20 (stringLiteralDataSize)
    while len(buf) % 4 != 0:
        buf.append(0)
    new_data_off = len(buf)
    buf.extend(new_data)
    struct.pack_into("<II", buf, 16, new_data_off, len(new_data))
    return bytes(buf), applied


def escape_xunity_text(text: str) -> str:
    """转义 XUnity.AutoTranslator 键值对中的换行与等号。"""
    out = text.replace("\\", "\\\\").replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\r")
    return out.replace("=", "\\=")


def format_xunity_translations(pairs: dict[str, str]) -> str:
    """生成 `BepInEx/Translation/zh/Text/_PreTranslated.txt` 内容。"""
    lines = [
        "// Generated by ProjectFerry (摆渡计划) - Unity Static Translation Table",
    ]
    for src, dst in pairs.items():
        if not src or not dst or src == dst:
            continue
        lines.append(f"{escape_xunity_text(src)}={escape_xunity_text(dst)}")
    return "\n".join(lines) + "\n"


def _iter_json_strings(node, base: str = ""):
    """递归遍历 StreamingAssets JSON 词表中的可翻译字符串。"""
    if isinstance(node, dict):
        for k, v in node.items():
            token = str(k).replace("~", "~0").replace("/", "~1")
            ptr = f"{base}/{token}"
            if isinstance(v, str):
                if is_unity_translatable(v):
                    yield ptr, v
            elif isinstance(v, (dict, list)):
                yield from _iter_json_strings(v, ptr)
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            ptr = f"{base}/{idx}"
            if isinstance(item, str):
                if is_unity_translatable(item):
                    yield ptr, item
            elif isinstance(item, (dict, list)):
                yield from _iter_json_strings(item, ptr)


def _is_zh_lang_dir(name: str) -> bool:
    low = name.strip().lower().replace("-", "_")
    parts = [p for p in re.split(r"[^a-z0-9]+", low) if p]
    return any(tok in parts for tok in _ZH_LANG_TOKENS) or low in _ZH_LANG_TOKENS


def _is_source_lang_dir(name: str) -> int | None:
    low = name.strip().lower()
    if _is_zh_lang_dir(low):
        return None
    for rank, tok in enumerate(_SRC_LANG_PRIORITY):
        if low == tok or low.startswith(tok + "_") or low.startswith(tok + "-"):
            return rank
    return None


def _discover_external_lang_pack(data_dir: Path) -> tuple[Path, Path | None, Path] | None:
    """识别外置接口型网游语言目录（如《边狱巴士》`LimbusCompany_Data/Lang/`）。

    返回 `(source_dir, existing_zh_dir_or_none, target_zh_dir)`：
    - `source_dir`: 英文/日文/韩文源语言 JSON 文件夹；
    - `existing_zh_dir`: 若目录里已有零协等人工汉化包（含 zh/cn），自动作为增量补翻基线；
    - `target_zh_dir`: 回写目标中文目录（不改动原版 EN/KR 文件，也不碰游戏本体二进制）。
    """
    lang_root = data_dir / "Lang"
    if not lang_root.is_dir():
        return None
    subdirs = [p for p in sorted(lang_root.iterdir()) if p.is_dir() and not p.name.startswith(".")]
    if not subdirs:
        return None
    zh_dirs = [p for p in subdirs if _is_zh_lang_dir(p.name) and any(p.rglob("*.json"))]
    ranked_src: list[tuple[int, Path]] = []
    for p in subdirs:
        rank = _is_source_lang_dir(p.name)
        if rank is not None and any(p.rglob("*.json")):
            ranked_src.append((rank, p))
    if not ranked_src:
        return None
    ranked_src.sort(key=lambda item: (item[0], item[1].name.lower()))
    source_dir = ranked_src[0][1]
    existing_zh = zh_dirs[0] if zh_dirs else None
    target_zh = existing_zh if existing_zh is not None else (lang_root / "Ferry_zh-CN")
    return source_dir, existing_zh, target_zh


def _read_json_pointer_map(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return {ptr: text for ptr, text in _iter_json_strings(payload)}


def _extract_unitypy_entries(asset_path: Path, rel_path: str, modid: str) -> list[Entry]:
    """借助 UnityPy 从 `.assets` / `.bundle` / `data.unity3d` 中提取 TextAsset 与 Localization StringTable。"""
    if not HAS_UNITYPY or UnityPy is None:
        return []
    entries: list[Entry] = []
    try:
        env = UnityPy.load(str(asset_path))
    except Exception:
        return []
    for obj in env.objects:
        tname = getattr(getattr(obj, "type", None), "name", "")
        if tname == "TextAsset":
            try:
                data = obj.read()
                raw_script = getattr(data, "m_Script", "")
                if isinstance(raw_script, bytes):
                    raw_script = raw_script.decode("utf-8-sig", errors="replace")
                if not isinstance(raw_script, str) or not raw_script.strip():
                    continue
                pid = str(obj.path_id)
                stripped = raw_script.strip()
                if stripped.startswith(("{", "[")):
                    try:
                        parsed = json.loads(stripped)
                        for ptr, text in _iter_json_strings(parsed):
                            entries.append(
                                Entry(
                                    modid=modid,
                                    key=f"{rel_path}#ASSET:{pid}{ptr}",
                                    english=text,
                                    source="game",
                                    kind="unity_asset_json",
                                    book=pid,
                                    rel_path=rel_path,
                                    json_pointer=ptr,
                                )
                            )
                        continue
                    except ValueError:
                        pass
                for lineno, line in enumerate(raw_script.splitlines(), start=1):
                    s = line.strip()
                    if not s or s.startswith(("#", "//", ";")):
                        continue
                    if is_unity_translatable(s):
                        entries.append(
                            Entry(
                                modid=modid,
                                key=f"{rel_path}#ASSET:{pid}:L{lineno}",
                                english=line,
                                source="game",
                                kind="unity_asset_txt",
                                book=pid,
                                rel_path=rel_path,
                                json_pointer=str(lineno),
                            )
                        )
            except Exception:
                continue
        elif tname == "MonoBehaviour":
            try:
                tree = obj.read_typetree()
            except Exception:
                continue
            table = tree.get("m_TableData") if isinstance(tree, dict) else None
            if not isinstance(table, list):
                continue
            pid = str(obj.path_id)
            for idx, row in enumerate(table):
                if not isinstance(row, dict):
                    continue
                loc_text = row.get("m_Localized")
                if isinstance(loc_text, str) and is_unity_translatable(loc_text):
                    entries.append(
                        Entry(
                            modid=modid,
                            key=f"{rel_path}#TABLE:{pid}:{idx}",
                            english=loc_text,
                            source="game",
                            kind="unity_asset_table",
                            book=pid,
                            rel_path=rel_path,
                            json_pointer=str(idx),
                        )
                    )
    return entries


def _rebuild_unitypy_asset(
    asset_path: Path,
    file_entries: list[Entry],
    translations: dict[str, str],
) -> tuple[bytes | None, int, int, list[str]]:
    """借助 UnityPy 回写 TextAsset / Localization Table 并重新序列化 `.assets` / `.bundle`。"""
    if not HAS_UNITYPY or UnityPy is None:
        return None, 0, len(file_entries), []
    applied = 0
    missed = 0
    conflicts: list[str] = []
    by_pid: dict[str, list[Entry]] = {}
    for entry in file_entries:
        by_pid.setdefault(entry.book, []).append(entry)

    try:
        env = UnityPy.load(str(asset_path))
    except Exception as exc:
        return None, 0, len(file_entries), [f"{asset_path.name}: UnityPy 加载失败 ({exc})"]

    changed_any = False
    for obj in env.objects:
        pid = str(obj.path_id)
        group = by_pid.get(pid)
        if not group:
            continue
        kind = group[0].kind
        try:
            if kind == "unity_asset_json":
                data = obj.read()
                raw_script = getattr(data, "m_Script", "")
                if isinstance(raw_script, bytes):
                    raw_script = raw_script.decode("utf-8-sig", errors="replace")
                payload = json.loads(raw_script)
                changed = False
                for entry in group:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if not tr or not tr.strip():
                        missed += 1
                        continue
                    set_pointer(payload, entry.json_pointer, tr)
                    changed = True
                    applied += 1
                if changed:
                    data.m_Script = json.dumps(payload, ensure_ascii=False, indent=2)
                    data.save()
                    changed_any = True
            elif kind == "unity_asset_txt":
                data = obj.read()
                raw_script = getattr(data, "m_Script", "")
                if isinstance(raw_script, bytes):
                    raw_script = raw_script.decode("utf-8-sig", errors="replace")
                lines = raw_script.splitlines()
                changed = False
                for entry in group:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if not tr or not tr.strip():
                        missed += 1
                        continue
                    idx = int(entry.json_pointer) - 1
                    if 0 <= idx < len(lines):
                        lines[idx] = tr
                        changed = True
                        applied += 1
                if changed:
                    data.m_Script = "\n".join(lines) + ("\n" if raw_script.endswith("\n") else "")
                    data.save()
                    changed_any = True
            elif kind == "unity_asset_table":
                tree = obj.read_typetree()
                table = tree.get("m_TableData") if isinstance(tree, dict) else None
                if not isinstance(table, list):
                    continue
                changed = False
                for entry in group:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if not tr or not tr.strip():
                        missed += 1
                        continue
                    idx = int(entry.json_pointer)
                    if 0 <= idx < len(table) and isinstance(table[idx], dict):
                        table[idx]["m_Localized"] = tr
                        changed = True
                        applied += 1
                if changed:
                    obj.save_typetree(tree)
                    changed_any = True
        except Exception as exc:
            conflicts.append(f"{asset_path.name}#obj:{pid}: {exc}")

    if not changed_any:
        return None, applied, missed, conflicts
    try:
        repacked = env.file.save()
        if isinstance(repacked, (bytes, bytearray)):
            return bytes(repacked), applied, missed, conflicts
    except Exception as exc:
        conflicts.append(f"{asset_path.name}: UnityPy 重打包失败 ({exc})")
    return None, applied, missed, conflicts


class UnityAdapter(Adapter):
    name = "unity"
    priority = 28

    def _find_data_dirs(self, root: Path) -> list[Path]:
        root = Path(root)
        if not root.is_dir():
            return []
        if root.name.endswith("_Data") and (
            (root / "Managed").is_dir()
            or (root / "StreamingAssets").is_dir()
            or (root / "Lang").is_dir()
            or (root / "il2cpp_data").is_dir()
            or (root / "globalgamemanagers").is_file()
            or (root / "data.unity3d").is_file()
        ):
            return [root]
        found = [p for p in sorted(root.glob("*_Data")) if p.is_dir()]
        if found:
            return found
        if (root / "UnityPlayer.dll").is_file():
            return [root]
        return []

    def _find_external_lang_packs(self, root: Path) -> list[tuple[Path, Path | None, Path]]:
        packs: list[tuple[Path, Path | None, Path]] = []
        for data_dir in self._find_data_dirs(root):
            found = _discover_external_lang_pack(data_dir)
            if found is not None:
                packs.append(found)
        return packs

    def _find_streaming_files(self, root: Path) -> list[Path]:
        files: list[Path] = []
        for data_dir in self._find_data_dirs(root):
            sa = data_dir / "StreamingAssets"
            if sa.is_dir():
                for ext in ("*.json", "*.txt"):
                    files.extend(sorted(p for p in sa.rglob(ext) if p.is_file()))
        return files

    def _find_unity_asset_files(self, root: Path) -> list[Path]:
        if not HAS_UNITYPY:
            return []
        files: list[Path] = []
        seen: set[Path] = set()
        for data_dir in self._find_data_dirs(root):
            candidates: list[Path] = []
            for pattern in ("resources.assets", "sharedassets*.assets", "data.unity3d"):
                candidates.extend(sorted(data_dir.glob(pattern)))
            sa = data_dir / "StreamingAssets"
            if sa.is_dir():
                for ext in ("*.bundle", "*.ab", "*.unity3d"):
                    candidates.extend(sorted(sa.rglob(ext)))
            for path in candidates:
                if not path.is_file() or path in seen or ".ferry-backup" in path.parts:
                    continue
                try:
                    if 64 <= path.stat().st_size <= _UNITY_ASSET_MAX_BYTES:
                        seen.add(path)
                        files.append(path)
                except OSError:
                    continue
        return files

    def _find_managed_dlls(self, root: Path) -> list[Path]:
        dlls: list[Path] = []
        for data_dir in self._find_data_dirs(root):
            managed = data_dir / "Managed"
            if managed.is_dir():
                for name in ("Assembly-CSharp.dll", "Assembly-CSharp-firstpass.dll"):
                    candidate = managed / name
                    if candidate.is_file():
                        dlls.append(candidate)
        return dlls

    def _find_il2cpp_metadata(self, root: Path) -> list[Path]:
        metas: list[Path] = []
        for data_dir in self._find_data_dirs(root):
            candidate = data_dir / "il2cpp_data" / "Metadata" / "global-metadata.dat"
            if candidate.is_file():
                metas.append(candidate)
        direct = Path(root) / "global-metadata.dat"
        if direct.is_file() and direct not in metas:
            metas.append(direct)
        return metas

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root)
        data_dirs = self._find_data_dirs(root)
        if not data_dirs:
            return ProbeReport(
                engine=self.name,
                root=root,
                notes=["未找到 *_Data 目录或 UnityPlayer.dll，不是 Unity 游戏"],
            )

        il2cpp = any((d / "il2cpp_data").is_dir() for d in data_dirs) or (root / "GameAssembly.dll").is_file()
        ext_packs = self._find_external_lang_packs(root)
        entries = self.collect(root)
        sa_files = self._find_streaming_files(root)
        dlls = self._find_managed_dlls(root)
        asset_files = self._find_unity_asset_files(root)

        if not entries:
            if ext_packs and any(zh is not None for _, zh, _ in ext_packs):
                return ProbeReport(
                    engine="Unity Engine（外置语言接口）",
                    root=root,
                    readable=True,
                    writable=False,
                    unicode_engine=True,
                    notes=["检测到外置语言目录，且已有中文汉化包已完整覆盖当前全部词条，无需补翻。"],
                )
            note = (
                "检测到 Unity (IL2CPP 后端)，未在明文/资产包中检出可翻译文本"
                + ("（建议安装 UnityPy 以支持 AssetBundle/.assets 提取）" if not HAS_UNITYPY else "（资产包可能加密）")
                if il2cpp
                else "检测到 Unity 工程目录，但未在 StreamingAssets / 资产包 / Assembly-CSharp.dll 中检出可翻译文本"
            )
            return ProbeReport(
                engine="Unity Engine",
                root=root,
                readable=False,
                writable=False,
                notes=[note],
            )

        if ext_packs:
            src_dir, zh_dir, dst_dir = ext_packs[0]
            mode_note = (
                f"检测到外置语言包接口（源：{src_dir.name}，已存在中文基线：{zh_dir.name}），仅增量补翻缺失的 {len(entries)} 条，保留全部已有译文。"
                if zh_dir is not None
                else f"检测到外置语言包接口（源：{src_dir.name}），共 {len(entries)} 条；将写出到独立中文目录 {dst_dir.name}，不修改游戏本体。"
            )
            return ProbeReport(
                engine="Unity Engine（外置语言接口）",
                root=root,
                readable=True,
                writable=True,
                verifiable=False,
                unit_count=len(entries),
                container="external-lang",
                text_format="Lang JSON（增量补丁，不改原包）",
                unicode_engine=True,
                in_place=True,
                notes=[mode_note],
            )

        asset_entries_count = sum(1 for e in entries if e.kind.startswith("unity_asset_"))
        notes = [
            f"检测到 Unity 游戏（StreamingAssets {len(sa_files)} 个，Managed {len(dlls)} 个，资产包 {len(asset_files)} 个），"
            f"共抽取 {len(entries)} 条文本" + (f"（其中 UnityPy 资产内文本 {asset_entries_count} 条）" if asset_entries_count else "") + "。",
            "回写策略：明文词表与 UnityPy 资产包原地回写重打包（带备份），并同步输出 XUnity.AutoTranslator 预翻译包。",
        ]
        return ProbeReport(
            engine="Unity Engine",
            root=root,
            readable=True,
            writable=True,
            verifiable=False,
            unit_count=len(entries),
            container="folder+assets" if asset_entries_count else "folder",
            text_format="StreamingAssets + UnityPy Assets + .NET #US -> XUnity",
            unicode_engine=True,
            in_place=True,
            notes=notes,
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).absolute()
        if not self._find_data_dirs(root):
            return []

        # 0. 外置语言接口模式（如《边狱巴士》*_Data/Lang/）：安全只走外置 JSON 增量补丁，绝不碰游戏本体。
        ext_packs = self._find_external_lang_packs(root)
        if ext_packs:
            entries: list[Entry] = []
            for source_dir, existing_zh_dir, target_zh_dir in ext_packs:
                src_base_rel = source_dir.relative_to(root).as_posix()
                dst_base_rel = target_zh_dir.relative_to(root).as_posix()
                for src_file in sorted(source_dir.rglob("*.json")):
                    if not src_file.is_file():
                        continue
                    sub_rel = src_file.relative_to(source_dir).as_posix()
                    src_map = _read_json_pointer_map(src_file)
                    if not src_map:
                        continue
                    existing_map: dict[str, str] = {}
                    if existing_zh_dir is not None:
                        zh_file = existing_zh_dir / sub_rel
                        if zh_file.is_file():
                            existing_map = _read_json_pointer_map(zh_file)
                    for pointer, text in src_map.items():
                        # 已有人工汉化（如零协）直接跳过，0 Token 保留，只抽热更新新增/未翻词条
                        old_zh = existing_map.get(pointer)
                        if old_zh and old_zh.strip() and old_zh != text:
                            continue
                        entries.append(
                            Entry(
                                modid=root.name,
                                key=f"lang:{src_base_rel}/{sub_rel}#{pointer}",
                                english=text,
                                source="game",
                                kind="ext_lang_json",
                                book=f"{src_base_rel}|{dst_base_rel}",
                                rel_path=sub_rel,
                                json_pointer=pointer,
                            )
                        )
            return entries

        entries = []
        seen_dll_texts: set[str] = set()

        # 1. StreamingAssets 明文 JSON / TXT
        for path in self._find_streaming_files(root):
            rel = path.relative_to(root).as_posix()
            try:
                raw_text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError):
                continue
            if path.suffix.lower() == ".json":
                try:
                    payload = json.loads(raw_text)
                except ValueError:
                    continue
                for pointer, text in _iter_json_strings(payload):
                    entries.append(
                        Entry(
                            modid=root.name,
                            key=f"{rel}#{pointer}",
                            english=text,
                            source="game",
                            kind="json",
                            rel_path=rel,
                            json_pointer=pointer,
                        )
                    )
            elif path.suffix.lower() == ".txt":
                for lineno, line in enumerate(raw_text.splitlines(), start=1):
                    s = line.strip()
                    if not s or s.startswith(("#", "//", ";")):
                        continue
                    if is_unity_translatable(s):
                        entries.append(
                            Entry(
                                modid=root.name,
                                key=f"{rel}:{lineno}",
                                english=line,
                                source="game",
                                kind="line",
                                rel_path=rel,
                                json_pointer=str(lineno),
                            )
                        )

        # 1.5 UnityPy 资产包（resources.assets / sharedassets*.assets / *.bundle）
        for asset_path in self._find_unity_asset_files(root):
            rel = asset_path.relative_to(root).as_posix()
            entries.extend(_extract_unitypy_entries(asset_path, rel, root.name))

        # 2. Managed Assembly-CSharp.dll (#US 用户字符串表)
        for dll_path in self._find_managed_dlls(root):
            rel = dll_path.relative_to(root).as_posix()
            try:
                pe_bytes = dll_path.read_bytes()
            except OSError:
                continue
            for us_off, text in extract_dotnet_us_strings(pe_bytes):
                if text in seen_dll_texts:
                    continue
                seen_dll_texts.add(text)
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}#US:{us_off}",
                        english=text,
                        source="game",
                        kind="dotnet_us",
                        rel_path=rel,
                        json_pointer=f"US:{us_off}",
                    )
                )

        # 3. IL2CPP global-metadata.dat (stringLiteral 常量池)
        for meta_path in self._find_il2cpp_metadata(root):
            rel = meta_path.relative_to(root).as_posix()
            try:
                meta_bytes = meta_path.read_bytes()
            except OSError:
                continue
            for lit_idx, text in extract_il2cpp_metadata_strings(meta_bytes):
                if text in seen_dll_texts:
                    continue
                seen_dll_texts.add(text)
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}#IL2CPP:{lit_idx}",
                        english=text,
                        source="game",
                        kind="il2cpp_meta",
                        rel_path=rel,
                        json_pointer=f"IL2CPP:{lit_idx}",
                    )
                )

        return entries

    def apply(
        self,
        root: Path,
        translations: dict[str, str],
        out: Path | None = None,
    ) -> ApplyReport:
        root = Path(root).absolute()
        report = ApplyReport()
        entries = self.collect(root)
        if not entries:
            report.notes.append("未找到可回写的 Unity 文本单元")
            return report

        # ── 外置语言接口模式（如《边狱巴士》*_Data/Lang/）：增量合并写出到中文目录，不改原版文件 ──
        if entries[0].kind == "ext_lang_json":
            by_target: dict[tuple[str, str, str], list[Entry]] = {}
            for entry in entries:
                src_base_rel, dst_base_rel = entry.book.split("|", 1)
                by_target.setdefault((src_base_rel, dst_base_rel, entry.rel_path), []).append(entry)

            pending_ext: dict[str, bytes] = {}
            for (src_base_rel, dst_base_rel, sub_rel), group in sorted(by_target.items()):
                src_file = root / src_base_rel / sub_rel
                dst_rel = f"{dst_base_rel}/{sub_rel}"
                dst_file = root / dst_rel
                try:
                    payload = json.loads(src_file.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as exc:
                    report.conflicts.append(f"{sub_rel}: 读取源 JSON 失败 ({exc})")
                    continue
                # 若已有中文汉化包文件，先把它已翻译的条目全部合回，保证 100% 保留人工汉化。
                if dst_file.is_file():
                    existing_map = _read_json_pointer_map(dst_file)
                    for ptr, zh_val in existing_map.items():
                        try:
                            set_pointer(payload, ptr, zh_val)
                        except (KeyError, IndexError, TypeError, ValueError):
                            pass
                changed = False
                for entry in group:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if not tr or not tr.strip():
                        report.missed += 1
                        continue
                    try:
                        set_pointer(payload, entry.json_pointer, tr)
                        changed = True
                        report.applied += 1
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        report.conflicts.append(f"{entry.key}: {exc}")
                if changed:
                    pending_ext[dst_rel] = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

            if not pending_ext:
                report.notes.append("未命中任何译文，未写入外置语言包")
                return report

            log = UndoLog.open(root, engine=self.name)
            for rel_path, raw_bytes in pending_ext.items():
                target = root / rel_path
                if target.exists():
                    log.will_modify(rel_path)
                else:
                    log.will_add(rel_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw_bytes)
                log.did_write(rel_path)
            log.finish()
            report.out_path = root
            report.in_place = True
            report.undo_path = root / UNDO_NAME
            report.notes.append(f"已增量写回外置语言包（更新 {len(pending_ext)} 个 JSON，共 {report.applied} 条），原版语言与游戏本体未改动。")
            return report

        by_file: dict[str, list[Entry]] = {}
        for entry in entries:
            by_file.setdefault(entry.rel_path, []).append(entry)

        xunity_pairs: dict[str, str] = {}
        pending_writes: dict[str, bytes] = {}

        for rel_path, file_entries in by_file.items():
            full_path = root / rel_path
            kind = file_entries[0].kind

            if kind.startswith("unity_asset_"):
                repacked, ok_n, miss_n, errs = _rebuild_unitypy_asset(full_path, file_entries, translations)
                report.missed += miss_n
                report.conflicts.extend(errs)
                if repacked is not None and ok_n > 0:
                    pending_writes[rel_path] = repacked
                    report.applied += ok_n
                    for entry in file_entries:
                        tr = translations.get(entry.key) or translations.get(entry.english)
                        if tr and tr.strip():
                            xunity_pairs[entry.english] = tr
                continue

            if kind == "json":
                try:
                    payload = json.loads(full_path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError) as exc:
                    report.conflicts.append(f"{rel_path}: JSON 解析失败 ({exc})")
                    continue
                changed = False
                for entry in file_entries:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if not tr or not tr.strip():
                        report.missed += 1
                        continue
                    try:
                        set_pointer(payload, entry.json_pointer, tr)
                        xunity_pairs[entry.english] = tr
                        changed = True
                        report.applied += 1
                    except (KeyError, IndexError, TypeError, ValueError) as exc:
                        report.conflicts.append(f"{entry.key}: {exc}")
                if changed:
                    pending_writes[rel_path] = json.dumps(
                        payload, ensure_ascii=False, indent=2
                    ).encode("utf-8")

            elif kind == "line":
                try:
                    lines = full_path.read_text(encoding="utf-8-sig").splitlines()
                except OSError as exc:
                    report.conflicts.append(f"{rel_path}: 读取失败 ({exc})")
                    continue
                changed = False
                for entry in file_entries:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if not tr or not tr.strip():
                        report.missed += 1
                        continue
                    idx = int(entry.json_pointer) - 1
                    if 0 <= idx < len(lines):
                        lines[idx] = tr
                        xunity_pairs[entry.english] = tr
                        changed = True
                        report.applied += 1
                if changed:
                    pending_writes[rel_path] = ("\n".join(lines) + "\n").encode("utf-8")

            elif kind == "dotnet_us":
                for entry in file_entries:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if not tr or not tr.strip():
                        report.missed += 1
                        continue
                    xunity_pairs[entry.english] = tr
                    report.applied += 1

            elif kind == "il2cpp_meta":
                try:
                    meta_blob = full_path.read_bytes()
                except OSError as exc:
                    report.conflicts.append(f"{rel_path}: {exc}")
                    continue
                meta_map: dict[str, str] = {}
                for entry in file_entries:
                    tr = translations.get(entry.key) or translations.get(entry.english)
                    if not tr or not tr.strip():
                        report.missed += 1
                        continue
                    meta_map[entry.json_pointer] = tr
                    meta_map[entry.english] = tr
                    xunity_pairs[entry.english] = tr
                if meta_map:
                    rebuilt_meta, count = rebuild_il2cpp_metadata(meta_blob, meta_map)
                    if count > 0:
                        pending_writes[rel_path] = rebuilt_meta
                        report.applied += count

        if not report.applied:
            report.notes.append("没有任何译文命中，未修改游戏文件")
            return report

        # 生成 XUnity.AutoTranslator 预翻译表与默认配置
        pre_rel = "BepInEx/Translation/zh/Text/_PreTranslated.txt"
        pending_writes[pre_rel] = format_xunity_translations(xunity_pairs).encode("utf-8")

        cfg_rel = "BepInEx/config/AutoTranslatorConfig.ini"
        if not (root / cfg_rel).exists():
            has_kana = any(_KANA_RE.search(k) for k in xunity_pairs)
            from_lang = "ja" if has_kana else "en"
            cfg_text = (
                "[Service]\nEndpoint=\n\n"
                "[General]\nLanguage=zh\nFromLanguage=" + from_lang + "\n\n"
                "[Files]\nDirectory=BepInEx\\Translation\\{Lang}\\Text\n"
                "OutputFile=BepInEx\\Translation\\{Lang}\\Text\\_AutoGeneratedTranslations.txt\n"
            )
            pending_writes[cfg_rel] = cfg_text.encode("utf-8")

        log = UndoLog.open(root, engine=self.name)
        for rel_path, raw_bytes in pending_writes.items():
            target = root / rel_path
            if target.exists():
                log.will_modify(rel_path)
            else:
                log.will_add(rel_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw_bytes)
            log.did_write(rel_path)
        log.finish()

        report.out_path = root
        report.in_place = True
        report.undo_path = root / UNDO_NAME
        report.notes.append(
            f"已回写 {report.applied} 条译文（含 XUnity 静态翻译表 {pre_rel}），可通过 revert 还原。"
        )
        return report
