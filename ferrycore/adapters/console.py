"""掌机与主机平台适配器 —— PlayStation Vita (`.vpk` / `sce_sys`) 与 PSP (`.iso` / `PSP_GAME`)。

本模块完全使用 Python 标准库实现 Sony 掌机平台的核心容器与元数据解析：

1. **Sony `PARAM.SFO` (`\\x00PSF`) 解析器 (`parse_param_sfo`)**：
   - 自动读取游戏标题 (`TITLE`)、游戏编号 (`TITLE_ID` 如 `PCSG00123` / `DISC_ID` 如 `ULJM05123`) 与系统版本。

2. **PlayStation Vita (`PsvAdapter`)**：
   - 支持 `.vpk` 安装包（ZIP 容器）与 MaiDump / Nonpdrm / FAGDec 解包目录（含 `sce_sys/param.sfo`）。
   - 自动鉴别 `eboot.bin` 是 Sony `SCE\\x00` 加密 SELF 还是已解密的 `\\x7fELF`。
   - 同时支持包内明文脚本（`.json` / `.csv` / `.ks` / `.po` / `.tscn`）与裸二进制剧本/ELF（`.bin` / `.dat` / `.mes` / `.scr`）
     的 Shift-JIS 抽取及定长原位回写。
   - `.vpk` 模式输出 `<name>.cn.vpk` 副本，解包目录模式原地回写并同步生成 Vita 实机/Vita3K 标准
     `rePatch/<TITLE_ID>/` 挂载补丁目录。

3. **PlayStation Portable (`PspAdapter`)**：
   - 纯标准库实现 **ISO-9660 光盘镜像解析器 (`parse_iso9660`)**：从第 16 扇区 (`0x8000`) 读取主卷描述符 `\\x01CD001\\x01`，
     递归遍历目录记录定位 `/PSP_GAME/PARAM.SFO`、`/PSP_GAME/SYSDIR/BOOT.BIN` 与 `/PSP_GAME/USRDIR/*` 的精确 LBA 扇区偏移。
   - 自动鉴别 `EBOOT.BIN` / `BOOT.BIN` 是否为 `~PSP` Kirk 加密 PRX。
   - 回写时直接在 `.cn.iso` 镜像副本的对应 LBA 扇区段内完成定长原位覆盖（不破坏 UMD 扇区布局与 LBA 表），
     PPSSPP 与实机可直接加载运行。
"""

from __future__ import annotations

import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from ferrygame.rawbin import patch_blob, scan_binary_strings, scan_sjis_strings
from ferrygame.tbl import ThingyTable, find_tbl_pair, scan_tbl_strings
from ferrygame.units import Unit

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .container import unique_output
from .mobile import collect_entries_from_virtual_files, patch_virtual_files
from .undo import UNDO_NAME, UndoLog

PSF_MAGIC = b"\x00PSF"
ISO_SECTOR_SIZE = 2048
ISO_PVD_OFFSET = 16 * ISO_SECTOR_SIZE
_BINARY_SCRIPT_SUFFIXES = (".bin", ".dat", ".mes", ".scr", ".scn", ".msg", ".hed", ".pak")
_STRUCTURED_SUFFIXES = (".json", ".csv", ".po", ".ks", ".tscn", ".strings", ".xml")


# ============================================================================
# Sony PARAM.SFO (\x00PSF) 解析器
# ============================================================================


def parse_param_sfo(blob: bytes) -> dict[str, str | int]:
    """解析 Sony `PARAM.SFO` / `param.sfo` (`\\x00PSF`) 元数据表。"""
    if len(blob) < 20 or blob[:4] != PSF_MAGIC:
        return {}
    _version, key_start, data_start, count = struct.unpack_from("<IIII", blob, 4)
    if count > 1024 or key_start >= len(blob) or data_start >= len(blob):
        return {}

    result: dict[str, str | int] = {}
    for i in range(count):
        ent_off = 20 + i * 16
        if ent_off + 16 > len(blob):
            break
        k_off, fmt, d_len, _d_max, d_off = struct.unpack_from("<HHIII", blob, ent_off)
        abs_k = key_start + k_off
        if abs_k >= len(blob):
            continue
        k_end = blob.find(b"\x00", abs_k, min(len(blob), abs_k + 128))
        if k_end < 0:
            continue
        key = blob[abs_k:k_end].decode("utf-8", errors="replace")

        abs_d = data_start + d_off
        if abs_d + d_len > len(blob):
            continue
        raw_val = blob[abs_d : abs_d + d_len]
        if fmt == 0x0404 and d_len == 4:
            result[key] = struct.unpack_from("<I", raw_val, 0)[0]
        elif fmt in (0x0204, 0x0004):
            result[key] = raw_val.rstrip(b"\x00").decode("utf-8", errors="replace")
    return result


def is_encrypted_sony_executable(blob: bytes) -> tuple[bool, str]:
    """检测 `eboot.bin` / `EBOOT.BIN` 是否处于 Sony 专有加密外壳下。"""
    if blob.startswith(b"SCE\x00"):
        return True, "Sony PS Vita SELF (SCE\\x00) 加密可执行文件"
    if blob.startswith(b"~PSP"):
        return True, "Sony PSP Kirk (~PSP) 加密模块"
    if blob.startswith(b"\x00PSPB"):
        return True, "Sony PBP 封装包"
    if blob.startswith(b"\x7fELF"):
        return False, "明文 ELF 可执行文件"
    return False, "未知二进制格式"


# ============================================================================
# ISO-9660 光盘镜像纯标准库解析器（供 PSP .iso 使用）
# ============================================================================


@dataclass
class IsoFileEntry:
    path: str
    lba: int
    offset: int
    size: int
    dir_record_size_offset: int  # 指向目录记录中 data_length (both-endian uint32) 的绝对字节位置


def parse_iso9660(blob: bytes) -> dict[str, IsoFileEntry]:
    """解析标准 ISO-9660 光盘镜像，返回 `{路径: IsoFileEntry}`。"""
    if len(blob) < ISO_PVD_OFFSET + ISO_SECTOR_SIZE:
        raise ValueError("文件太小，不是 ISO-9660 镜像")
    if blob[ISO_PVD_OFFSET : ISO_PVD_OFFSET + 6] != b"\x01CD001":
        raise ValueError("未在扇区 16 找到 ISO-9660 主卷描述符 (CD001)")

    # 根目录记录位于主卷描述符偏移 156 处（34 字节）
    root_rec = blob[ISO_PVD_OFFSET + 156 : ISO_PVD_OFFSET + 190]
    root_lba = struct.unpack_from("<I", root_rec, 2)[0]
    root_size = struct.unpack_from("<I", root_rec, 10)[0]

    files: dict[str, IsoFileEntry] = {}
    visited_lbas: set[int] = set()

    def _walk_dir(dir_lba: int, dir_size: int, prefix: str) -> None:
        if dir_lba in visited_lbas or dir_lba <= 0:
            return
        visited_lbas.add(dir_lba)
        base_off = dir_lba * ISO_SECTOR_SIZE
        end_off = min(len(blob), base_off + dir_size)
        pos = base_off

        while pos < end_off:
            rec_len = blob[pos]
            if rec_len == 0:
                # 跳到下一个 2048 字节扇区起点
                next_sector = ((pos // ISO_SECTOR_SIZE) + 1) * ISO_SECTOR_SIZE
                if next_sector <= pos:
                    break
                pos = next_sector
                continue
            if pos + rec_len > end_off or rec_len < 34:
                break

            ext_lba = struct.unpack_from("<I", blob, pos + 2)[0]
            data_len = struct.unpack_from("<I", blob, pos + 10)[0]
            flags = blob[pos + 25]
            name_len = blob[pos + 32]
            raw_name = blob[pos + 33 : pos + 33 + name_len]

            if raw_name not in (b"\x00", b"\x01"):
                name = raw_name.decode("ascii", errors="replace")
                if ";" in name:
                    name = name.split(";", 1)[0]
                if name.endswith("."):
                    name = name[:-1]
                rel = f"{prefix}/{name}" if prefix else name
                is_dir = bool(flags & 0x02)
                if is_dir:
                    _walk_dir(ext_lba, data_len, rel)
                else:
                    file_off = ext_lba * ISO_SECTOR_SIZE
                    if 0 <= file_off and file_off + data_len <= len(blob):
                        files[rel] = IsoFileEntry(
                            path=rel,
                            lba=ext_lba,
                            offset=file_off,
                            size=data_len,
                            dir_record_size_offset=pos + 10,
                        )
            pos += rec_len

    _walk_dir(root_lba, root_size, "")
    return files


def decompress_cso(cso_blob: bytes) -> bytes:
    """将 PSP `CISO` (`.cso`) 压缩光盘镜像解压为标准 ISO-9660 字节流。"""
    if len(cso_blob) < 24 or cso_blob[:4] != b"CISO":
        raise ValueError("不是合法的 PSP CISO (.cso) 镜像")
    _hdr_size, total_bytes, block_size, _ver, align = struct.unpack_from("<IQIBB", cso_blob, 4)
    if block_size == 0 or total_bytes == 0 or total_bytes > 4 * 1024 * 1024 * 1024:
        raise ValueError("CISO 头参数异常")
    total_blocks = int(total_bytes // block_size)
    idx_table_end = 24 + (total_blocks + 1) * 4
    if idx_table_end > len(cso_blob):
        raise ValueError("CISO 索引表越界")

    indices = struct.unpack_from(f"<{total_blocks + 1}I", cso_blob, 24)
    out = bytearray()
    for b_idx in range(total_blocks):
        cur_val = indices[b_idx]
        nxt_val = indices[b_idx + 1]
        is_plain = bool(cur_val & 0x80000000)
        read_pos = (cur_val & 0x7FFFFFFF) << align
        read_end = (nxt_val & 0x7FFFFFFF) << align
        chunk = cso_blob[read_pos:read_end]
        if is_plain:
            out.extend(chunk[:block_size].ljust(block_size, b"\x00"))
        else:
            decomp = zlib.decompress(chunk, -15)
            out.extend(decomp[:block_size].ljust(block_size, b"\x00"))
    return bytes(out)


def compress_cso(iso_blob: bytes, block_size: int = ISO_SECTOR_SIZE) -> bytes:
    """将标准 ISO-9660 字节流压缩打包为 PSP `CISO` (`.cso`) 镜像。"""
    total_bytes = len(iso_blob)
    total_blocks = (total_bytes + block_size - 1) // block_size
    data_start = 24 + (total_blocks + 1) * 4
    indices: list[int] = []
    chunks: list[bytes] = []
    cursor = data_start

    for b_idx in range(total_blocks):
        sector = iso_blob[b_idx * block_size : (b_idx + 1) * block_size].ljust(block_size, b"\x00")
        cobj = zlib.compressobj(6, zlib.DEFLATED, -15)
        comp = cobj.compress(sector) + cobj.flush()
        if len(comp) >= block_size:
            indices.append(0x80000000 | cursor)
            chunks.append(sector)
            cursor += len(sector)
        else:
            indices.append(cursor)
            chunks.append(comp)
            cursor += len(comp)
    indices.append(cursor)

    header = b"CISO" + struct.pack("<IQIBB2s", 24, total_bytes, block_size, 1, 0, b"\x00\x00")
    idx_bytes = struct.pack(f"<{total_blocks + 1}I", *indices)
    return header + idx_bytes + b"".join(chunks)


# ============================================================================
# 掌机二进制 + 明文混合抽取与回写辅助
# ============================================================================


def _is_console_binary_candidate(rel: str, raw: bytes) -> bool:
    low = rel.lower()
    fname = Path(low).name
    if fname in ("param.sfo", "icon0.png", "pic0.png", "pic1.png", "snd0.at3"):
        return False
    if fname in ("eboot.bin", "boot.bin"):
        enc, _ = is_encrypted_sony_executable(raw)
        return not enc
    return low.endswith(_BINARY_SCRIPT_SUFFIXES)


def _collect_console_all(
    modid: str,
    virtual_files: dict[str, bytes],
    *,
    src_tbl: ThingyTable | None = None,
) -> tuple[list[Entry], dict[str, list[Unit]]]:
    """同时抽取结构化文本文件与掌机二进制剧本（支持 Shift-JIS 定长串与外挂 Thingy `.tbl` 码表）。"""
    structured_files = {
        k: v for k, v in virtual_files.items() if k.lower().endswith(_STRUCTURED_SUFFIXES)
    }
    entries = collect_entries_from_virtual_files(modid, structured_files)
    bin_units_by_file: dict[str, list[Unit]] = {}

    for rel, raw in sorted(virtual_files.items()):
        if rel in structured_files:
            continue
        if not _is_console_binary_candidate(rel, raw):
            continue
        units = scan_tbl_strings(raw, src_tbl, rel_path=rel) if src_tbl is not None else scan_binary_strings(raw, rel_path=rel)
        if not units:
            continue
        bin_units_by_file[rel] = units
        for u in units:
            entries.append(
                Entry(
                    modid=modid,
                    key=u.key,
                    english=u.src,
                    source="game",
                    kind=f"bin_{u.kind}",
                    rel_path=rel,
                    json_pointer=f"BIN:{u.meta.get('offset', 0)}:{u.max_bytes}",
                )
            )
    return entries, bin_units_by_file


def _patch_console_all(
    virtual_files: dict[str, bytes],
    entries: list[Entry],
    bin_units_by_file: dict[str, list[Unit]],
    translations: dict[str, str],
    *,
    dst_tbl: ThingyTable | None = None,
) -> tuple[dict[str, bytes], int, int, list[str]]:
    structured_entries = [e for e in entries if not e.kind.startswith("bin_")]
    modified, applied, missed, conflicts = patch_virtual_files(
        virtual_files, structured_entries, translations
    )

    for rel, units in bin_units_by_file.items():
        raw = virtual_files.get(rel)
        if raw is None:
            continue
        file_hit = False
        for u in units:
            tr = translations.get(u.key) or translations.get(u.src)
            if tr and tr.strip():
                u.dst = tr
                file_hit = True
            else:
                missed += 1
        if not file_hit:
            continue
        patched, count, bin_conflicts = patch_blob(raw, units, tbl_encode=dst_tbl)
        conflicts.extend(bin_conflicts)
        if count > 0:
            modified[rel] = patched
            applied += count

    return modified, applied, missed, conflicts


# ============================================================================
# PlayStation Vita (.vpk / sce_sys) 适配器
# ============================================================================


class PsvAdapter(Adapter):
    """PlayStation Vita `.vpk` 安装包与解包目录适配器。"""

    name = "psv"
    priority = 13

    def _find_vpk(self, root: Path) -> Path | None:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".vpk" and ".cn" not in root.stem.lower():
            return root
        if root.is_dir():
            for p in sorted(root.glob("*.vpk")):
                if p.is_file() and ".cn" not in p.stem.lower():
                    return p
        return None

    def _is_unpacked_psv(self, root: Path) -> bool:
        root = Path(root)
        return root.is_dir() and (root / "sce_sys" / "param.sfo").is_file()

    def _load_files(self, root: Path) -> tuple[Path | None, dict[str, bytes], dict[str, str | int]]:
        vpk = self._find_vpk(root)
        files: dict[str, bytes] = {}
        sfo_meta: dict[str, str | int] = {}

        if vpk is not None:
            try:
                with zipfile.ZipFile(vpk, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        low = info.filename.lower()
                        if low == "sce_sys/param.sfo":
                            sfo_meta = parse_param_sfo(zf.read(info.filename))
                        elif low.endswith(_STRUCTURED_SUFFIXES + _BINARY_SCRIPT_SUFFIXES):
                            files[info.filename] = zf.read(info.filename)
            except (OSError, zipfile.BadZipFile):
                return vpk, {}, {}
            return vpk, files, sfo_meta

        if self._is_unpacked_psv(root):
            try:
                sfo_meta = parse_param_sfo((root / "sce_sys" / "param.sfo").read_bytes())
            except OSError:
                pass
            for p in sorted(root.rglob("*")):
                if not p.is_file() or ".ferry-backup" in p.parts or "rePatch" in p.parts:
                    continue
                rel = p.relative_to(root).as_posix()
                low = rel.lower()
                if low.endswith(_STRUCTURED_SUFFIXES + _BINARY_SCRIPT_SUFFIXES):
                    try:
                        files[rel] = p.read_bytes()
                    except OSError:
                        continue
        return None, files, sfo_meta

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        vpk = self._find_vpk(root)
        unpacked = self._is_unpacked_psv(root)
        if vpk is None and not unpacked:
            return ProbeReport(engine=self.name, root=root, notes=["未找到 .vpk 归档或 sce_sys/param.sfo"])

        _, files, sfo = self._load_files(root)
        title = str(sfo.get("TITLE") or root.name)
        title_id = str(sfo.get("TITLE_ID") or "UNKNOWN")
        src_tbl, _dst_tbl = find_tbl_pair(root)
        entries, _ = _collect_console_all(root.name, files, src_tbl=src_tbl)

        eboot_raw = files.get("eboot.bin") or files.get("EBOOT.BIN")
        eboot_note = ""
        if eboot_raw:
            enc, desc = is_encrypted_sony_executable(eboot_raw)
            eboot_note = f"eboot.bin 状态：{desc}"

        if not entries:
            return ProbeReport(
                engine=f"PlayStation Vita ({title_id})",
                root=root,
                readable=False,
                writable=False,
                container="vpk" if vpk else "folder",
                notes=[
                    f"游戏标题：{title} [{title_id}]",
                    eboot_note or "未检出明文脚本或未加密的 ELF/数据文件（若为 NoNpDrm 加密包请先用 FAGDec/Vita3K 解密）",
                ],
            )

        notes = [f"PS Vita 游戏：{title} [{title_id}]，共检出 {len(entries)} 条可翻译文本。"]
        if eboot_note:
            notes.append(eboot_note)
        notes.append(
            "回写策略：.vpk 模式产出 .cn.vpk 副本；解包目录模式原地更新并同步生成 rePatch/<TITLE_ID>/ 实机补丁目录。"
        )
        return ProbeReport(
            engine=f"PlayStation Vita ({title_id})",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="vpk" if vpk else "folder",
            text_format="PSV script + binary",
            unicode_engine=False,
            in_place=vpk is None,
            notes=notes,
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        _, files, _ = self._load_files(root)
        src_tbl, _ = find_tbl_pair(root)
        entries, _ = _collect_console_all(root.name, files, src_tbl=src_tbl)
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        vpk, files, sfo = self._load_files(root)
        title_id = str(sfo.get("TITLE_ID") or "PCSG00000")
        src_tbl, dst_tbl = find_tbl_pair(root)
        entries, bin_units = _collect_console_all(root.name, files, src_tbl=src_tbl)
        report = ApplyReport()
        if not entries:
            report.notes.append("PSV 游戏内无可用文本单元")
            return report

        modified, applied, missed, conflicts = _patch_console_all(
            files, entries, bin_units, translations, dst_tbl=dst_tbl
        )
        report.applied = applied
        report.missed = missed
        report.conflicts.extend(conflicts)
        if not modified:
            report.notes.append("未命中任何译文，未写出文件")
            return report

        undo = UndoLog.open(base, engine=self.name)
        if vpk is not None:
            default_out = vpk.with_name(f"{vpk.stem}.cn.vpk")
            target_out = unique_output(Path(out) if out else default_out)
            if target_out.is_dir():
                target_out = unique_output(target_out / default_out.name)
            with zipfile.ZipFile(vpk, "r") as zin, zipfile.ZipFile(target_out, "w") as zout:
                for info in zin.infolist():
                    data = modified.get(info.filename, zin.read(info.filename))
                    fresh = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
                    fresh.compress_type = info.compress_type
                    fresh.external_attr = info.external_attr
                    zout.writestr(fresh, data)
            undo.did_copy(target_out)
            report.undo_path = undo.finish()
            report.out_path = target_out
            report.in_place = False
            report.notes.append(f"已生成 PS Vita 汉化安装包：{target_out.name}。")
            return report

        # 解包目录：原地更新 + 输出 rePatch/<TITLE_ID>/ 补丁层
        for rel, new_raw in modified.items():
            undo.will_modify(rel)
            (root / rel).write_bytes(new_raw)
            undo.did_write(rel)
            repatch_rel = f"rePatch/{title_id}/{rel}"
            repatch_target = root / repatch_rel
            if repatch_target.exists():
                undo.will_modify(repatch_rel)
            else:
                undo.will_add(repatch_rel)
            repatch_target.parent.mkdir(parents=True, exist_ok=True)
            repatch_target.write_bytes(new_raw)
            undo.did_write(repatch_rel)

        report.undo_path = undo.finish()
        report.out_path = root
        report.in_place = True
        report.notes.append(f"已更新 PSV 目录并生成实机 rePatch/{title_id}/ 补丁目录。")
        return report


# ============================================================================
# PlayStation Portable (PSP .iso / PSP_GAME) 适配器
# ============================================================================


class PspAdapter(Adapter):
    """PlayStation Portable (PSP) ISO-9660 光盘镜像与 `PSP_GAME` 解包目录适配器。"""

    name = "psp"
    priority = 14

    def _find_psp_iso(self, root: Path) -> Path | None:
        root = Path(root)
        candidates: list[Path] = []
        if root.is_file() and root.suffix.lower() in (".iso", ".cso") and ".cn" not in root.stem.lower():
            candidates.append(root)
        elif root.is_dir():
            for ext in ("*.iso", "*.cso"):
                candidates.extend(
                    p for p in sorted(root.glob(ext)) if p.is_file() and ".cn" not in p.stem.lower()
                )
        for iso_path in candidates:
            try:
                raw = iso_path.read_bytes()
                blob = decompress_cso(raw) if raw[:4] == b"CISO" else raw
                iso_files = parse_iso9660(blob)
                if any(k.upper() == "PSP_GAME/PARAM.SFO" for k in iso_files):
                    return iso_path
            except Exception:
                continue
        return None

    def _find_unpacked_psp_dir(self, root: Path) -> Path | None:
        root = Path(root)
        if not root.is_dir():
            return None
        if (root / "PSP_GAME" / "PARAM.SFO").is_file():
            return root / "PSP_GAME"
        if (root / "PARAM.SFO").is_file() and ((root / "SYSDIR").is_dir() or (root / "USRDIR").is_dir()):
            return root
        return None

    def _load_files(
        self, root: Path
    ) -> tuple[Path | None, bytes | None, dict[str, IsoFileEntry], dict[str, bytes], dict[str, str | int]]:
        iso_path = self._find_psp_iso(root)
        if iso_path is not None:
            raw_img = iso_path.read_bytes()
            blob = decompress_cso(raw_img) if raw_img[:4] == b"CISO" else raw_img
            iso_index = parse_iso9660(blob)
            virtual_files: dict[str, bytes] = {}
            sfo_meta: dict[str, str | int] = {}
            for rel, entry in iso_index.items():
                up = rel.upper()
                raw = blob[entry.offset : entry.offset + entry.size]
                if up == "PSP_GAME/PARAM.SFO":
                    sfo_meta = parse_param_sfo(raw)
                elif rel.lower().endswith(_STRUCTURED_SUFFIXES + _BINARY_SCRIPT_SUFFIXES):
                    virtual_files[rel] = raw
            return iso_path, blob, iso_index, virtual_files, sfo_meta

        psp_dir = self._find_unpacked_psp_dir(root)
        if psp_dir is not None:
            sfo_meta = {}
            try:
                sfo_meta = parse_param_sfo((psp_dir / "PARAM.SFO").read_bytes())
            except OSError:
                pass
            virtual_files = {}
            for p in sorted(root.rglob("*")):
                if not p.is_file() or ".ferry-backup" in p.parts:
                    continue
                rel = p.relative_to(root).as_posix()
                if rel.lower().endswith(_STRUCTURED_SUFFIXES + _BINARY_SCRIPT_SUFFIXES):
                    try:
                        virtual_files[rel] = p.read_bytes()
                    except OSError:
                        continue
            return None, None, {}, virtual_files, sfo_meta

        return None, None, {}, {}, {}

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        iso_path, _blob, _idx, files, sfo = self._load_files(root)
        if iso_path is None and not self._find_unpacked_psp_dir(root):
            return ProbeReport(engine=self.name, root=root, notes=["未找到 PSP .iso 光盘镜像或 PSP_GAME/PARAM.SFO"])

        title = str(sfo.get("TITLE") or root.name)
        disc_id = str(sfo.get("DISC_ID") or "ULJM00000")
        src_tbl, _ = find_tbl_pair(root)
        entries, _ = _collect_console_all(root.name, files, src_tbl=src_tbl)

        if not entries:
            return ProbeReport(
                engine=f"PlayStation Portable ({disc_id})",
                root=root,
                readable=False,
                writable=False,
                container="iso" if iso_path else "folder",
                notes=[
                    f"PSP 游戏：{title} [{disc_id}]",
                    "未检出明文文本或未加密的 BOOT.BIN / USRDIR 脚本（若 EBOOT.BIN 为 ~PSP 加密且无外部剧本，需先用 PPSSPP 解密）",
                ],
            )

        return ProbeReport(
            engine=f"PlayStation Portable ({disc_id})",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="iso" if iso_path else "folder",
            text_format="ISO-9660 LBA extent + SJIS/GBK",
            unicode_engine=False,
            in_place=iso_path is None,
            notes=[
                f"PSP 游戏：{title} [{disc_id}]，共检出 {len(entries)} 条文本单元。",
                "回写采用 ISO-9660 扇区 LBA 原位定长覆写产出 .cn.iso 副本，不破坏光盘文件表，PPSSPP 与实机直读。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        _iso, _blob, _idx, files, _sfo = self._load_files(root)
        src_tbl, _ = find_tbl_pair(root)
        entries, _ = _collect_console_all(root.name, files, src_tbl=src_tbl)
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        iso_path, iso_blob, iso_index, files, sfo = self._load_files(root)
        disc_id = str(sfo.get("DISC_ID") or "ULJM00000")
        src_tbl, dst_tbl = find_tbl_pair(root)
        entries, bin_units = _collect_console_all(root.name, files, src_tbl=src_tbl)
        report = ApplyReport()
        if not entries:
            report.notes.append("PSP 游戏内无可用文本单元")
            return report

        modified, applied, missed, conflicts = _patch_console_all(
            files, entries, bin_units, translations, dst_tbl=dst_tbl
        )
        report.applied = applied
        report.missed = missed
        report.conflicts.extend(conflicts)
        if not modified:
            report.notes.append("未命中任何译文，未写出文件")
            return report

        undo = UndoLog.open(base, engine=self.name)
        if iso_path is not None and iso_blob is not None:
            new_iso = bytearray(iso_blob)
            for rel, new_raw in modified.items():
                ent = iso_index.get(rel)
                if ent is None:
                    continue
                # 允许在原分配的 2048 字节扇区对齐容量内写回
                allocated_capacity = ((ent.size + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE) * ISO_SECTOR_SIZE
                if len(new_raw) > max(ent.size, allocated_capacity):
                    report.conflicts.append(
                        f"{rel}：修改后大小 ({len(new_raw)}) 超出 ISO 扇区预留容量 ({allocated_capacity})"
                    )
                    continue
                padded_raw = new_raw.ljust(allocated_capacity, b"\x00")
                new_iso[ent.offset : ent.offset + len(padded_raw)] = padded_raw
                # 若是结构化文本文件且长度变化，同步更新 ISO 目录记录中的 both-endian uint32 文件长度
                if len(new_raw) != ent.size:
                    pos = ent.dir_record_size_offset
                    struct.pack_into("<I", new_iso, pos, len(new_raw))
                    struct.pack_into(">I", new_iso, pos + 4, len(new_raw))

            is_cso = iso_path.suffix.lower() == ".cso" or iso_path.read_bytes()[:4] == b"CISO"
            out_ext = ".cso" if is_cso else ".iso"
            default_out = iso_path.with_name(f"{iso_path.stem}.cn{out_ext}")
            target_out = unique_output(Path(out) if out else default_out)
            if target_out.is_dir():
                target_out = unique_output(target_out / default_out.name)
            final_bytes = compress_cso(bytes(new_iso)) if is_cso else bytes(new_iso)
            target_out.write_bytes(final_bytes)
            undo.did_copy(target_out)
            report.undo_path = undo.finish()
            report.out_path = target_out
            report.in_place = False
            report.notes.append(f"已生成 PSP 汉化光盘镜像 [{disc_id}]：{target_out.name}。")
            return report

        # 解包 PSP_GAME 目录原地回写
        for rel, new_raw in modified.items():
            undo.will_modify(rel)
            (root / rel).write_bytes(new_raw)
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.out_path = root
        report.in_place = True
        report.notes.append(f"已原地更新 PSP_GAME 目录 [{disc_id}] 共 {len(modified)} 个文件。")
        return report


# ============================================================================
# Nintendo DS (.nds NitroROM FNT + FAT) 适配器
# ============================================================================


@dataclass
class NdsFileEntry:
    path: str
    file_id: int
    offset: int
    size: int


def parse_nds_rom(blob: bytes) -> tuple[str, str, dict[str, NdsFileEntry]]:
    """解析 Nintendo DS `.nds` NitroROM 头部、文件名表 (FNT, 0x40) 与文件分配表 (FAT, 0x48)。"""
    if len(blob) < 0x200:
        raise ValueError("NDS ROM 过小")
    title = blob[0x00:0x0C].rstrip(b"\x00").decode("ascii", errors="replace").strip()
    game_code = blob[0x0C:0x10].rstrip(b"\x00").decode("ascii", errors="replace").strip()
    fnt_off, fnt_size, fat_off, fat_size = struct.unpack_from("<IIII", blob, 0x40)
    if (
        fnt_off < 0x200
        or fat_off < 0x200
        or fnt_size < 8
        or fat_size < 8
        or (fat_size % 8) != 0
        or fnt_off + fnt_size > len(blob)
        or fat_off + fat_size > len(blob)
    ):
        raise ValueError("不是合法的 Nintendo DS NitroROM 镜像")

    file_count = fat_size // 8
    entries: dict[str, NdsFileEntry] = {}

    def _walk_fnt_dir(dir_id: int, prefix: str, visited: set[int]) -> None:
        if dir_id in visited:
            return
        visited.add(dir_id)
        idx = dir_id & 0x0FFF
        main_pos = fnt_off + idx * 8
        if main_pos + 8 > fnt_off + fnt_size:
            return
        sub_rel, first_file_id, _parent_or_count = struct.unpack_from("<IHH", blob, main_pos)
        pos = fnt_off + sub_rel
        cur_file_id = first_file_id

        while pos < fnt_off + fnt_size:
            type_len = blob[pos]
            pos += 1
            if type_len == 0:
                break
            name_len = type_len & 0x7F
            is_dir = bool(type_len & 0x80)
            if pos + name_len > fnt_off + fnt_size:
                break
            name = blob[pos : pos + name_len].decode("ascii", errors="replace")
            pos += name_len
            rel = f"{prefix}/{name}" if prefix else name
            if is_dir:
                if pos + 2 > fnt_off + fnt_size:
                    break
                sub_dir_id = struct.unpack_from("<H", blob, pos)[0]
                pos += 2
                _walk_fnt_dir(sub_dir_id, rel, visited)
            else:
                if cur_file_id < file_count:
                    f_start, f_end = struct.unpack_from("<II", blob, fat_off + cur_file_id * 8)
                    if 0 <= f_start <= f_end <= len(blob):
                        entries[rel] = NdsFileEntry(
                            path=rel,
                            file_id=cur_file_id,
                            offset=f_start,
                            size=f_end - f_start,
                        )
                cur_file_id += 1

    _walk_fnt_dir(0xF000, "", set())
    return title, game_code, entries


class NdsAdapter(Adapter):
    """Nintendo DS (`.nds` NitroROM) 掌机镜像适配器。"""

    name = "nds"
    priority = 14

    def _find_nds(self, root: Path) -> Path | None:
        root = Path(root)
        candidates: list[Path] = []
        if root.is_file() and root.suffix.lower() == ".nds" and ".cn" not in root.stem.lower():
            candidates.append(root)
        elif root.is_dir():
            candidates.extend(
                p for p in sorted(root.glob("*.nds")) if p.is_file() and ".cn" not in p.stem.lower()
            )
        for p in candidates:
            try:
                parse_nds_rom(p.read_bytes())
                return p
            except Exception:
                continue
        return None

    def _load_rom(
        self, root: Path
    ) -> tuple[Path | None, bytes | None, str, str, dict[str, NdsFileEntry], dict[str, bytes]]:
        nds_path = self._find_nds(root)
        if nds_path is None:
            return None, None, "", "", {}, {}
        blob = nds_path.read_bytes()
        title, code, nds_index = parse_nds_rom(blob)
        virtual_files: dict[str, bytes] = {}
        for rel, ent in nds_index.items():
            if rel.lower().endswith(_STRUCTURED_SUFFIXES + _BINARY_SCRIPT_SUFFIXES + (".txt",)):
                virtual_files[rel] = blob[ent.offset : ent.offset + ent.size]
        return nds_path, blob, title, code, nds_index, virtual_files

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        nds_path, _blob, title, code, nds_index, files = self._load_rom(root)
        if nds_path is None:
            return ProbeReport(engine=self.name, root=root, notes=["未找到合法的 Nintendo DS (.nds) NitroROM 镜像"])

        src_tbl, _ = find_tbl_pair(nds_path)
        entries, _ = _collect_console_all(root.name, files, src_tbl=src_tbl)
        label = f"Nintendo DS ({title or nds_path.stem} [{code}])"
        if not entries:
            return ProbeReport(
                engine=label,
                root=root,
                readable=False,
                writable=False,
                container="nds (NitroROM)",
                notes=[f"已解析 NitroROM 文件系统（共 {len(nds_index)} 个内部文件），但未在标准脚本中检出明文台词"],
            )

        return ProbeReport(
            engine=label,
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container="nds (NitroROM)",
            text_format="NitroROM FAT extent",
            unicode_engine=False,
            in_place=False,
            notes=[
                f"NDS 卡带镜像：{title} [{code}]，NitroROM 内含 {len(nds_index)} 个文件，共检出 {len(entries)} 条文本。",
                "回写采用 NitroROM FAT 区间原位定长覆盖产出 .cn.nds 副本，DeSmuME / melonDS / 烧录卡直读。",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        nds_path, _blob, _title, _code, _idx, files = self._load_rom(root)
        src_tbl, _ = find_tbl_pair(nds_path or root)
        entries, _ = _collect_console_all(root.name, files, src_tbl=src_tbl)
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        nds_path, blob, _title, code, nds_index, files = self._load_rom(root)
        report = ApplyReport()
        if nds_path is None or blob is None:
            report.notes.append("未找到 NDS ROM")
            return report

        src_tbl, dst_tbl = find_tbl_pair(nds_path)
        entries, bin_units = _collect_console_all(root.name, files, src_tbl=src_tbl)
        modified, applied, missed, conflicts = _patch_console_all(
            files, entries, bin_units, translations, dst_tbl=dst_tbl
        )
        report.applied = applied
        report.missed = missed
        report.conflicts.extend(conflicts)
        if not modified:
            report.notes.append("未命中任何译文，未生成 NDS 镜像")
            return report

        new_rom = bytearray(blob)
        for rel, new_raw in modified.items():
            ent = nds_index.get(rel)
            if ent is None:
                continue
            if len(new_raw) > ent.size:
                report.conflicts.append(f"{rel}：修改后字节 ({len(new_raw)}) 超出 FAT 分配空间 ({ent.size})")
                continue
            padded = new_raw.ljust(ent.size, b"\x00")
            new_rom[ent.offset : ent.offset + ent.size] = padded

        default_out = nds_path.with_name(f"{nds_path.stem}.cn.nds")
        target_out = unique_output(Path(out) if out else default_out)
        if target_out.is_dir():
            target_out = unique_output(target_out / default_out.name)
        target_out.write_bytes(bytes(new_rom))

        undo = UndoLog.open(base, engine=self.name)
        undo.did_copy(target_out)
        report.undo_path = undo.finish()
        report.out_path = target_out
        report.in_place = False
        report.notes.append(f"已生成 Nintendo DS 汉化镜像 [{code}]：{target_out.name}。")
        return report
