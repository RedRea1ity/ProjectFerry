"""早期 Java ME (J2ME / MIDP "jvav") 手机游戏适配器（`.jar` + `.jad` + `.class` 常量池任意长度重建）。

功能机时代（诺基亚 S40/S60、索爱、摩托罗拉，以及现代模拟器 Kemulator / FreeJ2ME / EKA2L1）的
J2ME 游戏以 `.jar`（配合 `.jad` 描述文件）分发，其内部标志是 `META-INF/MANIFEST.MF` 含有
`MIDlet-1:` / `MIDlet-Name:` / `MicroEdition-Profile: MIDP-`。

J2ME 游戏的文本藏在两处：
1. **Java `.class` 字节码常量池（魔数 `0xCAFEBABE`）**：
   - 根据 JVM 规范（JVMS §4.4），`.class` 文件头部紧跟 `constant_pool`，其中所有字符串字面量
     均为 `CONSTANT_Utf8_info`（Tag `1`：`[0x01][uint16_be byte_len][utf8_bytes]`）。
   - **核心优势**：`.class` 后续的字段、方法与字节码指令（`ldc` / `ldc_w`）全部通过 **1-based 常量池下标**
     引用字符串，**没有任何地方使用字节偏移**！
   - 因此本模块纯标准库解析 `.class` 的整个 `constant_pool`，将 `CONSTANT_Utf8` 直接替换为**任意长度的
     UTF-8 简体中文**并无损拼接后续字节码，彻底告别定长截断！
2. **包内外部资源表（`.txt` / `.lng` / `.properties` / `.bin` / `.dat`）**：
   - 支持明文属性表与二进制字符串表抽取回写。
3. **`.jad` 描述文件联动更新**：
   - 重打包产出 `<name>.cn.jar` 后，若同目录存在 `<name>.jad`，自动生成 `<name>.cn.jad` 并同步修正
     `MIDlet-Jar-Size:` 为新 `.jar` 的精确字节数，防止实机与模拟器报「JAR 大小不匹配」拒绝安装。
"""

from __future__ import annotations

import re
import struct
import zipfile
from pathlib import Path

from ferrygame.rawbin import EN_MENU_TERMS, JP_MENU_TERMS, patch_blob, scan_binary_strings
from ferrygame.units import Unit

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .container import unique_output
from .undo import UNDO_NAME, UndoLog
from .unity import is_unity_translatable

JAVA_CLASS_MAGIC = 0xCAFEBABE

# JVM Constant Pool Tags -> 固定字节长度（不含 tag 本身；Tag 1 CONSTANT_Utf8 为变长）
_CP_FIXED_SIZES = {
    3: 4,   # CONSTANT_Integer
    4: 4,   # CONSTANT_Float
    5: 8,   # CONSTANT_Long (占 2 个槽位)
    6: 8,   # CONSTANT_Double (占 2 个槽位)
    7: 2,   # CONSTANT_Class
    8: 2,   # CONSTANT_String (指向 CONSTANT_Utf8 的 1-based 下标!)
    9: 4,   # CONSTANT_Fieldref
    10: 4,  # CONSTANT_Methodref
    11: 4,  # CONSTANT_InterfaceMethodref
    12: 4,  # CONSTANT_NameAndType
    15: 3,  # CONSTANT_MethodHandle
    16: 2,  # CONSTANT_MethodType
    17: 4,  # CONSTANT_Dynamic
    18: 4,  # CONSTANT_InvokeDynamic
    19: 2,  # CONSTANT_Module
    20: 2,  # CONSTANT_Package
}


def _is_j2me_translatable(text: str) -> bool:
    s = text.strip()
    if not s or len(s) < 2 or len(s) > 2000:
        return False
    # 过滤 Java 内部类描述符与方法签名，如 (Ljavax/microedition/lcdui/Graphics;)V
    if s.startswith("(") and ")" in s:
        return False
    if "/" in s and " " not in s:
        return False
    if s.startswith(("[L", "java/", "javax/", "com/", "org/", "MIDlet-")):
        return False
    if s in EN_MENU_TERMS or s in JP_MENU_TERMS:
        return True
    return is_unity_translatable(s)


def parse_java_class_constants(blob: bytes) -> tuple[int, list[tuple[int, int, bytes]], set[int]]:
    """解析 `.class` 常量池。

    返回 `(cp_end_offset, [(cp_index, tag, raw_body), ...], string_literal_utf8_indices)`。
    其中 `string_literal_utf8_indices` 是被 `CONSTANT_String` (Tag 8) 显式引用的 `CONSTANT_Utf8` 下标集合——
    只有被 `CONSTANT_String` 引用（或含明显中日韩字符）的 `CONSTANT_Utf8` 才是游戏里的字符串字面量，
    类名、方法名（如 `paint`、`keyPressed`、`startApp`）绝不能改！
    """
    if len(blob) < 10 or struct.unpack_from(">I", blob, 0)[0] != JAVA_CLASS_MAGIC:
        raise ValueError("不是合法的 Java .class 文件")
    cp_count = struct.unpack_from(">H", blob, 8)[0]
    pos = 10
    idx = 1
    items: list[tuple[int, int, bytes]] = []
    string_refs: set[int] = set()

    while idx < cp_count:
        if pos >= len(blob):
            raise ValueError(".class 常量池意外截断")
        tag = blob[pos]
        pos += 1
        if tag == 1:  # CONSTANT_Utf8
            if pos + 2 > len(blob):
                raise ValueError("CONSTANT_Utf8 长度越界")
            u_len = struct.unpack_from(">H", blob, pos)[0]
            pos += 2
            if pos + u_len > len(blob):
                raise ValueError("CONSTANT_Utf8 内容越界")
            raw_utf8 = blob[pos : pos + u_len]
            pos += u_len
            items.append((idx, tag, raw_utf8))
            idx += 1
        elif tag in _CP_FIXED_SIZES:
            sz = _CP_FIXED_SIZES[tag]
            if pos + sz > len(blob):
                raise ValueError(f"CONSTANT tag {tag} 越界")
            body = blob[pos : pos + sz]
            pos += sz
            if tag == 8:  # CONSTANT_String -> uint16_be utf8_index
                utf8_idx = struct.unpack_from(">H", body, 0)[0]
                string_refs.add(utf8_idx)
            items.append((idx, tag, body))
            idx += 2 if tag in (5, 6) else 1
        else:
            raise ValueError(f"未知的 JVM 常量池 Tag: {tag}")

    return pos, items, string_refs


def extract_class_utf8_strings(blob: bytes) -> list[tuple[int, str]]:
    """从 `.class` 文件抽取所有被 `CONSTANT_String` 引用的可翻译玩家文本，返回 `[(cp_idx, text)]`。"""
    try:
        _end, items, string_refs = parse_java_class_constants(blob)
    except ValueError:
        return []

    out: list[tuple[int, str]] = []
    for cp_idx, tag, body in items:
        if tag != 1 or cp_idx not in string_refs:
            continue
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if _is_j2me_translatable(text):
            out.append((cp_idx, text))
    return out


def rebuild_class_utf8_strings(blob: bytes, replacements: dict[str, str]) -> tuple[bytes, int]:
    """重建 `.class` 文件的 `constant_pool`，支持任意长度的 UTF-8 中文字符串替换。"""
    cp_end, items, string_refs = parse_java_class_constants(blob)
    out_cp = bytearray()
    applied = 0

    for cp_idx, tag, body in items:
        out_cp.append(tag)
        if tag == 1:
            if cp_idx in string_refs:
                try:
                    src_text = body.decode("utf-8")
                except UnicodeDecodeError:
                    src_text = ""
                tr = replacements.get(f"CP:{cp_idx}") or (replacements.get(src_text) if src_text else None)
                if tr and tr.strip() and tr != src_text:
                    new_bytes = tr.encode("utf-8")
                    if len(new_bytes) <= 65535:
                        body = new_bytes
                        applied += 1
            out_cp.extend(struct.pack(">H", len(body)))
            out_cp.extend(body)
        else:
            out_cp.extend(body)

    if applied == 0:
        return blob, 0
    return blob[:10] + bytes(out_cp) + blob[cp_end:], applied


def _is_j2me_manifest(manifest_text: str) -> bool:
    low = manifest_text.lower()
    return "midlet-" in low or "microedition-profile" in low


class J2meAdapter(Adapter):
    """早期 Java ME (J2ME / MIDP) `.jar` 手机游戏适配器。"""

    name = "j2me"
    priority = 13

    def _find_j2me_jar(self, root: Path) -> Path | None:
        root = Path(root)
        candidates: list[Path] = []
        if root.is_file() and root.suffix.lower() == ".jar" and ".cn" not in root.stem.lower():
            candidates.append(root)
        elif root.is_dir():
            # 避开 Minecraft 实例目录
            if (root / "mods").is_dir() or (root / "resourcepacks").is_dir():
                return None
            candidates.extend(
                p for p in sorted(root.glob("*.jar")) if p.is_file() and ".cn" not in p.stem.lower()
            )
        for jar in candidates:
            try:
                with zipfile.ZipFile(jar, "r") as zf:
                    if "META-INF/MANIFEST.MF" not in zf.namelist():
                        continue
                    mf = zf.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
                    if _is_j2me_manifest(mf):
                        return jar
            except (OSError, zipfile.BadZipFile):
                continue
        return None

    def _parse_midlet_title(self, jar: Path) -> str:
        try:
            with zipfile.ZipFile(jar, "r") as zf:
                mf = zf.read("META-INF/MANIFEST.MF").decode("utf-8", errors="replace")
                for line in mf.splitlines():
                    if line.lower().startswith("midlet-name:"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
        return jar.stem

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        jar = self._find_j2me_jar(root)
        if jar is None:
            return ProbeReport(engine=self.name, root=root, notes=["未找到含 MIDlet- 清单的 J2ME (.jar) 手机游戏"])

        title = self._parse_midlet_title(jar)
        entries = self.collect(root)
        if not entries:
            return ProbeReport(
                engine=f"Java ME / J2ME ({title})",
                root=root,
                readable=False,
                writable=False,
                container="jar (MIDP)",
                notes=[f"检测到 J2ME 手机游戏《{title}》，但未在 .class 常量池或包内资源中检出可翻译文本"],
            )

        return ProbeReport(
            engine=f"Java ME / J2ME ({title})",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="jar (MIDP)",
            text_format=".class CONSTANT_Utf8 + resources",
            unicode_engine=True,
            in_place=False,
            notes=[
                f"J2ME 经典手机游戏：《{title}》（{jar.name}），共抽出 {len(entries)} 条文本。",
                "回写直接重建 .class 字节码的 CONSTANT_Utf8 常量池（支持任意长度 UTF-8 中文，不破坏字节码偏移），并同步生成更新了 MIDlet-Jar-Size 的 .cn.jad。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        jar = self._find_j2me_jar(root)
        if jar is None:
            return []

        entries: list[Entry] = []
        try:
            with zipfile.ZipFile(jar, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    rel = info.filename
                    low = rel.lower()
                    raw = zf.read(rel)

                    if low.endswith(".class"):
                        for cp_idx, text in extract_class_utf8_strings(raw):
                            entries.append(
                                Entry(
                                    modid=jar.stem,
                                    key=f"{rel}#CP:{cp_idx}",
                                    english=text,
                                    source="game",
                                    kind="java_class",
                                    rel_path=rel,
                                    json_pointer=f"CP:{cp_idx}",
                                )
                            )
                    elif low.endswith((".txt", ".lng", ".lang", ".properties", ".ini")) and low != "meta-inf/manifest.mf":
                        try:
                            text = raw.decode("utf-8-sig")
                        except UnicodeDecodeError:
                            try:
                                text = raw.decode("cp932")
                            except UnicodeDecodeError:
                                continue
                        for lineno, line in enumerate(text.splitlines(), start=1):
                            s = line.strip()
                            if not s or s.startswith(("#", "//", ";")):
                                continue
                            val = s.split("=", 1)[1].strip() if "=" in s else s
                            if _is_j2me_translatable(val):
                                entries.append(
                                    Entry(
                                        modid=jar.stem,
                                        key=f"{rel}:{lineno}",
                                        english=val,
                                        source="game",
                                        kind="j2me_text",
                                        rel_path=rel,
                                        json_pointer=str(lineno),
                                    )
                                )
                    elif low.endswith((".bin", ".dat", ".pak", ".res", ".str")):
                        for u in scan_binary_strings(raw, rel_path=rel):
                            entries.append(
                                Entry(
                                    modid=jar.stem,
                                    key=u.key,
                                    english=u.src,
                                    source="game",
                                    kind="j2me_bin",
                                    rel_path=rel,
                                    json_pointer=str(u.meta.get("offset", 0)),
                                )
                            )
        except (OSError, zipfile.BadZipFile):
            return []
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        jar = self._find_j2me_jar(root)
        report = ApplyReport()
        if jar is None:
            report.notes.append("未找到 J2ME .jar")
            return report

        entries = self.collect(root)
        if not entries:
            report.notes.append("J2ME 包内无可回写条目")
            return report

        by_file: dict[str, list[Entry]] = {}
        for e in entries:
            by_file.setdefault(e.rel_path, []).append(e)

        modified: dict[str, bytes] = {}
        with zipfile.ZipFile(jar, "r") as zf:
            for rel, file_entries in by_file.items():
                raw = zf.read(rel)
                kind = file_entries[0].kind

                if kind == "java_class":
                    cp_map: dict[str, str] = {}
                    for e in file_entries:
                        tr = translations.get(e.key) or translations.get(e.english)
                        if tr and tr.strip():
                            cp_map[e.json_pointer] = tr
                            cp_map[e.english] = tr
                        else:
                            report.missed += 1
                    if cp_map:
                        new_raw, count = rebuild_class_utf8_strings(raw, cp_map)
                        if count > 0:
                            modified[rel] = new_raw
                            report.applied += count

                elif kind == "j2me_text":
                    try:
                        text = raw.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        text = raw.decode("cp932", errors="replace")
                    lines = text.splitlines()
                    changed = False
                    for e in file_entries:
                        tr = translations.get(e.key) or translations.get(e.english)
                        if not tr or not tr.strip():
                            report.missed += 1
                            continue
                        idx = int(e.json_pointer) - 1
                        if 0 <= idx < len(lines):
                            if "=" in lines[idx]:
                                k_part = lines[idx].split("=", 1)[0]
                                lines[idx] = f"{k_part}={tr}"
                            else:
                                lines[idx] = tr
                            changed = True
                            report.applied += 1
                    if changed:
                        modified[rel] = ("\n".join(lines) + "\n").encode("utf-8")

                elif kind == "j2me_bin":
                    units = scan_binary_strings(raw, rel_path=rel)
                    hit = False
                    for u in units:
                        tr = translations.get(u.key) or translations.get(u.src)
                        if tr and tr.strip():
                            u.dst = tr
                            hit = True
                        else:
                            report.missed += 1
                    if hit:
                        patched, count, conflicts = patch_blob(raw, units)
                        report.conflicts.extend(conflicts)
                        if count > 0:
                            modified[rel] = patched
                            report.applied += count

        if not modified:
            report.notes.append("未命中任何译文，未生成 J2ME .jar")
            return report

        default_out = jar.with_name(f"{jar.stem}.cn.jar")
        target_out = unique_output(Path(out) if out else default_out)
        if target_out.is_dir():
            target_out = unique_output(target_out / default_out.name)

        with zipfile.ZipFile(jar, "r") as zin, zipfile.ZipFile(target_out, "w", compression=zipfile.ZIP_DEFLATED) as zout:
            for info in zin.infolist():
                data = modified.get(info.filename, zin.read(info.filename))
                fresh = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
                fresh.compress_type = info.compress_type
                fresh.external_attr = info.external_attr
                zout.writestr(fresh, data)

        undo = UndoLog.open(base, engine=self.name)
        undo.did_copy(target_out)

        # 若同目录存在 .jad 描述文件，同步生成修正了 MIDlet-Jar-Size 与 MIDlet-Jar-URL 的 .cn.jad
        jad_path = jar.with_suffix(".jad")
        if jad_path.is_file():
            try:
                jad_text = jad_path.read_text(encoding="utf-8", errors="replace")
                new_size = target_out.stat().st_size
                jad_text = re.sub(
                    r"(?im)^MIDlet-Jar-Size\s*:.*$",
                    f"MIDlet-Jar-Size: {new_size}",
                    jad_text,
                )
                jad_text = re.sub(
                    r"(?im)^MIDlet-Jar-URL\s*:.*$",
                    f"MIDlet-Jar-URL: {target_out.name}",
                    jad_text,
                )
                out_jad = target_out.with_suffix(".jad")
                out_jad.write_text(jad_text, encoding="utf-8")
                undo.did_copy(out_jad)
                report.notes.append(f"已同步生成更新了字节大小的 {out_jad.name}（MIDlet-Jar-Size: {new_size}）。")
            except OSError:
                pass

        report.undo_path = undo.finish()
        report.out_path = target_out
        report.in_place = False
        report.notes.append(f"已生成 J2ME 汉化包：{target_out.name}。")
        return report
