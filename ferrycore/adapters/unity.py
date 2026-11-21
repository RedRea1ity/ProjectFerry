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

    def _find_streaming_files(self, root: Path) -> list[Path]:
        files: list[Path] = []
        for data_dir in self._find_data_dirs(root):
            sa = data_dir / "StreamingAssets"
            if sa.is_dir():
                for ext in ("*.json", "*.txt"):
                    files.extend(sorted(p for p in sa.rglob(ext) if p.is_file()))
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
        entries = self.collect(root)
        sa_files = self._find_streaming_files(root)
        dlls = self._find_managed_dlls(root)

        if not entries:
            note = (
                "检测到 Unity (IL2CPP 后端)，但未找到明文 StreamingAssets，需先提取 global-metadata 或 AssetBundle"
                if il2cpp
                else "检测到 Unity 工程目录，但未在 StreamingAssets 或 Assembly-CSharp.dll 中检出可翻译文本"
            )
            return ProbeReport(
                engine="Unity Engine",
                root=root,
                readable=False,
                writable=False,
                notes=[note],
            )

        notes = [
            f"检测到 Unity 游戏（StreamingAssets 文件 {len(sa_files)} 个，Managed 程序集 {len(dlls)} 个），"
            f"共抽取 {len(entries)} 条文本。",
            "回写策略：StreamingAssets 明文词表原地更新，并同步输出 XUnity.AutoTranslator 预翻译包 "
            "(BepInEx/Translation/zh/Text/_PreTranslated.txt)。",
        ]
        return ProbeReport(
            engine="Unity Engine",
            root=root,
            readable=True,
            writable=True,
            verifiable=False,
            unit_count=len(entries),
            container="folder",
            text_format="StreamingAssets + .NET #US -> XUnity",
            unicode_engine=True,
            in_place=True,
            notes=notes,
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).absolute()
        if not self._find_data_dirs(root):
            return []

        entries: list[Entry] = []
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

        by_file: dict[str, list[Entry]] = {}
        for entry in entries:
            by_file.setdefault(entry.rel_path, []).append(entry)

        xunity_pairs: dict[str, str] = {}
        pending_writes: dict[str, bytes] = {}

        for rel_path, file_entries in by_file.items():
            full_path = root / rel_path
            kind = file_entries[0].kind

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
