"""PlayStation 1 (PSX) 与 PlayStation 2 (PS2) 光盘镜像适配器 (`Ps1Adapter`, `Ps2Adapter`)。

本模块完全基于 Python 标准库实现索尼初代与二代家用主机的光盘解析、流式读写与校验重算：

1. **流式 ISO-9660 解析器 (`parse_iso9660_stream`)**：
   - 通过 `seek()` 仅读取第 16 扇区主卷描述符与目录树扇区，**绝不把 4.7 GB 的 PS2 DVD 整盘读进内存**；
   - 统一兼容 2048 字节 DVD/ISO 与 2352 字节原始光盘 `.bin`（MODE1/2352、MODE2/2352 Form 1）。

2. **PlayStation 1 (`Ps1Adapter`)**：
   - 支持 **Redump 标准 MODE2/2352 Form 1 `.bin` + `.cue`**、**MAME `.chd`**（经 `chdman` 无损解包/重封）、
     **2048 字节 `.iso`** 与 **解包目录**（含 `SYSTEM.CNF` `BOOT = cdrom:...` 或 `PS-X EXE`）。
   - 修改 2352 字节扇区后自动按黄皮书 ECMA-130 重算 **MODE2 Form 1 EDC（含 8 字节子头）与 Reed-Solomon ECC (P/Q)**，
     并同步产出配套 `.cn.cue` 或经 `chdman createcd` 无损封回 `.cn.chd`。
   - 完整支持外挂 Thingy `.tbl` 自定义码表（`ferry.tbl` / `<镜像名>.tbl` + `*_cn.tbl`）。

3. **PlayStation 2 (`Ps2Adapter`)**：
   - 支持 **4.7 GB DVD `.iso`**（流式分块拷贝 + `seek` 原位扇区覆写，内存占用极低）、**蓝盘 CD `.bin`/`.cue`**、
     **`.chd`**（自动区分 CD/DVD CHD）与 **解包目录**（含 `SYSTEM.CNF` `BOOT2 = cdrom0:...`）。
   - 解析 `SYSTEM.CNF` 提取游戏编号（如 `SLPS-25001` / `SLUS-20312` / `SCES-50294`）、版本与制式（NTSC/PAL），
     按区域码自动切换日文 Shift-JIS / 欧美英文扫描策略，并支持外挂 `.tbl` 码表。
"""

from __future__ import annotations

import hashlib
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from ferrygame.rawbin import patch_blob
from ferrygame.tbl import ThingyTable, find_tbl_pair, is_thingy_tbl_file
from ferrygame.units import Unit

from .. import deps
from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .console import ISO_SECTOR_SIZE, _STRUCTURED_SUFFIXES
from .container import unique_output
from .mobile import collect_entries_from_virtual_files, patch_virtual_files
from .retro_console import (
    CDROM_RAW_SECTOR_SIZE,
    CHD_MAGIC,
    SaturnIsoEntry,
    _scan_retro_console_binary,
    detect_cdrom_layout,
    is_chd_file,
    parse_cue_first_bin,
    recompute_cdrom_edc_ecc,
    rewrite_cue_for_patched_bin,
)
from .undo import UndoLog

PSX_EXE_MAGIC = b"PS-X EXE"

_PS_SKIP_EXTS = {
    ".str", ".xa", ".vb", ".vh", ".seq", ".sep", ".vab", ".tim", ".tmd", ".pmd",
    ".pss", ".sfd", ".m2v", ".ipu", ".irx", ".iop", ".adx", ".afs", ".cpk",
    ".vag", ".wav", ".ac3", ".tm2", ".tim2", ".pak", ".pck", ".bgm", ".snd",
    ".mov", ".avi", ".mpg", ".mp3", ".ogg", ".png", ".bmp", ".jpg", ".tga",
}
_PS_BIN_EXTS = {
    ".bin", ".dat", ".mes", ".scr", ".scn", ".msg", ".txt", ".tbl", ".ovl",
    ".exe", ".elf", ".slp", ".rel", ".ev", ".evt", ".dlg", ".str2",
}
_PS_BOOT_ID_RE = re.compile(r"([A-Z]{4})[_\-](\d{3})\.?(\d{2})", re.IGNORECASE)
_PS_JP_PREFIXES = ("SLPS", "SLPM", "SCPS", "SCPM", "PAPX", "PCPX")


@dataclass
class PsSystemCnf:
    platform: str = ""  # "ps1" | "ps2"
    boot_path: str = ""  # 如 "SLPS_012.34"
    game_id: str = ""  # 规范化编号，如 "SLPS-01234"
    version: str = ""
    vmode: str = ""


def parse_ps_system_cnf(raw: bytes | str) -> PsSystemCnf | None:
    """解析 PlayStation 1 (`BOOT = cdrom:...`) 或 PlayStation 2 (`BOOT2 = cdrom0:...`) 的 `SYSTEM.CNF`。"""
    text = raw.decode("ascii", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    cnf = PsSystemCnf()
    for line in text.splitlines():
        s = line.strip()
        if not s or "=" not in s:
            continue
        key, _, val = s.partition("=")
        k = key.strip().upper()
        v = val.strip()
        if k == "BOOT2":
            cnf.platform = "ps2"
            cnf.boot_path = _clean_cdrom_boot_path(v)
        elif k == "BOOT" and not cnf.platform:
            cnf.platform = "ps1"
            cnf.boot_path = _clean_cdrom_boot_path(v)
        elif k == "VER":
            cnf.version = v
        elif k == "VMODE":
            cnf.vmode = v

    if not cnf.platform or not cnf.boot_path:
        return None
    m = _PS_BOOT_ID_RE.search(cnf.boot_path)
    if m:
        cnf.game_id = f"{m.group(1).upper()}-{m.group(2)}{m.group(3)}"
    return cnf


def _clean_cdrom_boot_path(val: str) -> str:
    s = val.strip()
    s = re.sub(r"^cdrom0?:\s*\\*", "", s, flags=re.IGNORECASE)
    s = s.split(";", 1)[0].replace("\\", "/").lstrip("/")
    return s


def _is_jp_game_id(game_id: str) -> bool:
    if not game_id:
        return True
    prefix = game_id.split("-", 1)[0].upper()
    return prefix in _PS_JP_PREFIXES


# ============================================================================
# 流式 ISO-9660 解析与扇区读写（支持 4.7 GB PS2 DVD 零内存爆炸）
# ============================================================================


@dataclass
class StreamIsoLayout:
    sector_size: int = ISO_SECTOR_SIZE
    data_offset: int = 0
    mode: int = 0
    system_id: str = ""
    volume_id: str = ""
    total_sectors: int = 0
    files: dict[str, SaturnIsoEntry] = field(default_factory=dict)


def _read_sector_stream(fh: BinaryIO, lba: int, sector_size: int, data_offset: int) -> bytes:
    fh.seek(lba * sector_size + data_offset)
    data = fh.read(ISO_SECTOR_SIZE)
    if len(data) < ISO_SECTOR_SIZE:
        raise ValueError(f"扇区 LBA {lba} 读取不完整")
    return data


def read_file_stream(fh: BinaryIO, lba: int, size: int, sector_size: int, data_offset: int) -> bytes:
    """从打开的光盘镜像句柄按 LBA 读取指定文件字节（兼容 2048 ISO 与 2352 RAW BIN）。"""
    if size <= 0:
        return b""
    if sector_size == ISO_SECTOR_SIZE and data_offset == 0:
        fh.seek(lba * ISO_SECTOR_SIZE)
        return fh.read(size)
    sectors = (size + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE
    out = bytearray()
    for i in range(sectors):
        out.extend(_read_sector_stream(fh, lba + i, sector_size, data_offset))
    return bytes(out[:size])


def write_file_stream(
    fh: BinaryIO,
    lba: int,
    new_data: bytes,
    sector_size: int,
    data_offset: int,
    mode: int,
) -> None:
    """按 LBA 扇区流式回写到 `r+b` 镜像句柄；若为 2352 RAW（MODE1/MODE2）则自动重算 EDC/ECC。"""
    sectors = (len(new_data) + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE
    for i in range(sectors):
        chunk = new_data[i * ISO_SECTOR_SIZE : (i + 1) * ISO_SECTOR_SIZE].ljust(ISO_SECTOR_SIZE, b"\x00")
        sec_start = (lba + i) * sector_size
        if sector_size == CDROM_RAW_SECTOR_SIZE and mode in (1, 2):
            fh.seek(sec_start)
            sec_buf = bytearray(fh.read(CDROM_RAW_SECTOR_SIZE))
            if len(sec_buf) < CDROM_RAW_SECTOR_SIZE:
                raise ValueError(f"扇区 LBA {lba + i} 不完整")
            sec_buf[data_offset : data_offset + ISO_SECTOR_SIZE] = chunk
            recompute_cdrom_edc_ecc(sec_buf, mode=mode)
            fh.seek(sec_start)
            fh.write(sec_buf)
        else:
            fh.seek(sec_start + data_offset)
            fh.write(chunk)


def parse_iso9660_stream(path: Path) -> StreamIsoLayout:
    """流式解析 ISO-9660 光盘镜像（支持 4.7 GB DVD `.iso` 与 2352 字节 `.bin`）。"""
    file_size = path.stat().st_size
    if file_size < 17 * ISO_SECTOR_SIZE:
        raise ValueError("镜像文件过小，不是 ISO-9660 光盘镜像")

    with open(path, "rb") as fh:
        head = fh.read(CDROM_RAW_SECTOR_SIZE)
        sector_size, data_offset, mode = detect_cdrom_layout(head)
        total_sectors = file_size // sector_size
        if total_sectors < 17:
            raise ValueError("光盘扇区数不足")

        pvd = _read_sector_stream(fh, 16, sector_size, data_offset)
        if pvd[:6] != b"\x01CD001":
            raise ValueError("第 16 扇区未见 ISO-9660 主卷描述符 (CD001)")

        system_id = pvd[8:40].decode("ascii", errors="replace").strip()
        volume_id = pvd[40:72].decode("ascii", errors="replace").strip()
        root_rec = pvd[156:190]
        root_lba = struct.unpack_from("<I", root_rec, 2)[0]
        root_size = struct.unpack_from("<I", root_rec, 10)[0]

        files: dict[str, SaturnIsoEntry] = {}
        visited: set[int] = set()

        def _walk_dir(dir_lba: int, dir_size: int, prefix: str) -> None:
            if dir_lba in visited or dir_lba <= 0 or dir_lba >= total_sectors:
                return
            visited.add(dir_lba)
            sec_count = max(1, (dir_size + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE)
            for s_idx in range(sec_count):
                cur_lba = dir_lba + s_idx
                if cur_lba >= total_sectors:
                    break
                sec = _read_sector_stream(fh, cur_lba, sector_size, data_offset)
                pos = 0
                while pos < ISO_SECTOR_SIZE:
                    rec_len = sec[pos]
                    if rec_len == 0:
                        break
                    if pos + rec_len > ISO_SECTOR_SIZE or rec_len < 34:
                        break
                    ext_lba = struct.unpack_from("<I", sec, pos + 2)[0]
                    data_len = struct.unpack_from("<I", sec, pos + 10)[0]
                    flags = sec[pos + 25]
                    name_len = sec[pos + 32]
                    raw_name = sec[pos + 33 : pos + 33 + name_len]
                    if raw_name not in (b"\x00", b"\x01"):
                        name = raw_name.decode("ascii", errors="replace")
                        if ";" in name:
                            name = name.split(";", 1)[0]
                        if name.endswith("."):
                            name = name[:-1]
                        rel = f"{prefix}/{name}" if prefix else name
                        if flags & 0x02:
                            _walk_dir(ext_lba, data_len, rel)
                        else:
                            end_lba = ext_lba + ((data_len + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE)
                            if 0 <= ext_lba and end_lba <= total_sectors:
                                files[rel] = SaturnIsoEntry(
                                    path=rel,
                                    lba=ext_lba,
                                    size=data_len,
                                    dir_lba=cur_lba,
                                    dir_offset_in_sector=pos + 10,
                                )
                    pos += rec_len

        _walk_dir(root_lba, root_size, "")

    return StreamIsoLayout(
        sector_size=sector_size,
        data_offset=data_offset,
        mode=mode,
        system_id=system_id,
        volume_id=volume_id,
        total_sectors=total_sectors,
        files=files,
    )


def _chd_unit_bytes(chd_path: Path) -> int:
    """读取 CHD v5 头的 `unit_bytes`（CD 光盘为 2448，DVD 光盘为 2048）。"""
    try:
        with open(chd_path, "rb") as fh:
            hdr = fh.read(64)
        if len(hdr) >= 64 and hdr[:8] == CHD_MAGIC:
            ver = struct.unpack_from(">I", hdr, 12)[0]
            if ver == 5:
                return struct.unpack_from(">I", hdr, 60)[0]
    except OSError:
        pass
    return 2448


def _chd_extract_ps_cached(chd_path: Path, chdman_exe: Path) -> tuple[Path, Path | None, bool]:
    """用 `chdman` 解包 PS1/PS2 `.chd` 到临时缓存，返回 `(img_path, cue_path_or_none, is_dvd)`。"""
    st = chd_path.stat()
    sig = hashlib.sha1(f"{chd_path.resolve()}|{st.st_size}|{st.st_mtime_ns}".encode("utf-8")).hexdigest()[:12]
    cache_dir = Path(tempfile.gettempdir()) / "ferry_chd_cache" / f"{chd_path.stem}_{sig}"
    is_dvd = _chd_unit_bytes(chd_path) == ISO_SECTOR_SIZE

    if is_dvd:
        iso_out = cache_dir / f"{chd_path.stem}.iso"
        if iso_out.is_file():
            return iso_out, None, True
        cache_dir.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [str(chdman_exe), "extractdvd", "-i", str(chd_path), "-o", str(iso_out), "-f"],
            capture_output=True,
            text=True,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not iso_out.is_file():
            raise ValueError(f"chdman extractdvd 失败：{proc.stderr.strip() or proc.stdout.strip()}")
        return iso_out, None, True

    cue_out = cache_dir / f"{chd_path.stem}.cue"
    bin_out = cache_dir / f"{chd_path.stem}.bin"
    if cue_out.is_file() and bin_out.is_file():
        return bin_out, cue_out, False
    cache_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [str(chdman_exe), "extractcd", "-i", str(chd_path), "-o", str(cue_out), "-ob", str(bin_out), "-f"],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 or not bin_out.is_file():
        raise ValueError(f"chdman extractcd 失败：{proc.stderr.strip() or proc.stdout.strip()}")
    return bin_out, cue_out, False


def _is_ps_candidate_file(rel: str, size: int, boot_path: str = "") -> bool:
    if size <= 0 or size > 12 * 1024 * 1024:
        return False
    low = rel.lower()
    fname = Path(low).name
    if fname == "system.cnf":
        return False
    if boot_path and low == boot_path.lower():
        return size <= 8 * 1024 * 1024
    suf = Path(low).suffix
    if suf in _PS_SKIP_EXTS:
        return False
    if suf in _STRUCTURED_SUFFIXES or suf in _PS_BIN_EXTS:
        return True
    # 兼容 PS1/PS2 主程序编号命名（如 SLPS_012.34、SLUS_203.12）
    if _PS_BOOT_ID_RE.search(fname):
        return size <= 8 * 1024 * 1024
    return suf == "" and size <= 4 * 1024 * 1024


def _collect_ps_units(
    modid: str,
    virtual_files: dict[str, bytes],
    *,
    src_tbl: ThingyTable | None = None,
    allow_sjis: bool = True,
) -> tuple[list[Entry], list[Unit], dict[str, list[Unit]]]:
    structured_files = {
        k: v for k, v in virtual_files.items() if k.lower().endswith(_STRUCTURED_SUFFIXES)
    }
    entries = collect_entries_from_virtual_files(modid, structured_files)
    all_units: list[Unit] = []
    order = 0
    for e in entries:
        order += 1
        all_units.append(
            Unit(
                key=e.key,
                src=e.english,
                kind="story",
                file=e.rel_path,
                order=order,
                encoding="utf-8",
            )
        )

    bin_units_by_file: dict[str, list[Unit]] = {}
    for rel, raw in sorted(virtual_files.items()):
        if rel in structured_files:
            continue
        units = _scan_retro_console_binary(raw, rel_path=rel, src_tbl=src_tbl, allow_sjis=allow_sjis)
        if not units:
            continue
        bin_units_by_file[rel] = units
        for u in units:
            order += 1
            u.order = order
            all_units.append(u)
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
    return entries, all_units, bin_units_by_file


# ============================================================================
# PlayStation 1 / PlayStation 2 共享光盘适配器基类
# ============================================================================


class _PsDiscAdapter(Adapter):
    """PS1 与 PS2 共享的光盘镜像 / 解包目录适配器核心逻辑。"""

    target_platform: str = ""  # "ps1" | "ps2"
    display_platform: str = ""

    def _find_chd(self, root: Path) -> Path | None:
        root = Path(root)
        if root.is_file() and root.suffix.lower() == ".chd" and ".cn" not in root.stem.lower():
            return root if is_chd_file(root) else None
        if root.is_dir():
            for p in sorted(root.glob("*.chd")):
                if p.is_file() and ".cn" not in p.stem.lower() and is_chd_file(p):
                    return p
        return None

    def _identify_iso_platform(self, img_path: Path, layout: StreamIsoLayout) -> PsSystemCnf | None:
        """校验 ISO-9660 镜像是否属于当前适配器目标平台（PS1 或 PS2）。"""
        cnf_entry = next((v for k, v in layout.files.items() if k.upper() == "SYSTEM.CNF"), None)
        if cnf_entry is not None and cnf_entry.size <= 4096:
            with open(img_path, "rb") as fh:
                raw_cnf = read_file_stream(fh, cnf_entry.lba, cnf_entry.size, layout.sector_size, layout.data_offset)
            cnf = parse_ps_system_cnf(raw_cnf)
            if cnf is not None:
                return cnf if cnf.platform == self.target_platform else None

        # 1994-1995 极早期 PS1 光盘可能无 SYSTEM.CNF，直接带 PSX.EXE (`PS-X EXE`) 且卷系统标识为 PLAYSTATION
        if self.target_platform == "ps1":
            psx_ent = next((v for k, v in layout.files.items() if k.upper() == "PSX.EXE"), None)
            if psx_ent is not None and psx_ent.size >= 16:
                with open(img_path, "rb") as fh:
                    head = read_file_stream(fh, psx_ent.lba, 16, layout.sector_size, layout.data_offset)
                if head.startswith(PSX_EXE_MAGIC):
                    return PsSystemCnf(platform="ps1", boot_path=psx_ent.path, game_id=layout.volume_id or "PSX")
        return None

    def _find_disc(self, root: Path) -> tuple[Path | None, Path | None, Path | None, bool, StreamIsoLayout | None, PsSystemCnf | None]:
        """返回 `(img_path, cue_path, chd_path, is_dvd_chd, layout, cnf)`。"""
        root = Path(root)
        chd_p = self._find_chd(root)
        if chd_p is not None:
            chd_dep = deps.REGISTRY.get("chdman")
            chdman_exe = chd_dep.locate() if chd_dep else None
            if chdman_exe is None:
                # 仅由 PS1 兜底报缺 chdman，避免与 Saturn 重复误报
                return None, None, chd_p, _chd_unit_bytes(chd_p) == ISO_SECTOR_SIZE, None, None
            try:
                img_p, cue_p, is_dvd = _chd_extract_ps_cached(chd_p, chdman_exe)
                layout = parse_iso9660_stream(img_p)
                cnf = self._identify_iso_platform(img_p, layout)
                if cnf is not None:
                    return img_p, cue_p, chd_p, is_dvd, layout, cnf
            except (OSError, ValueError):
                return None, None, None, False, None, None
            return None, None, None, False, None, None

        candidates: list[tuple[Path, Path | None]] = []
        if root.is_file():
            suf = root.suffix.lower()
            if ".cn" in root.stem.lower():
                return None, None, None, False, None, None
            if suf == ".cue":
                bin_p = parse_cue_first_bin(root)
                if bin_p is not None:
                    candidates.append((bin_p, root))
            elif suf in (".iso", ".bin", ".img"):
                sib_cue = root.with_suffix(".cue")
                candidates.append((root, sib_cue if sib_cue.is_file() else None))
        elif root.is_dir():
            seen: set[Path] = set()
            for cue in sorted(root.glob("*.cue")):
                if ".cn" in cue.stem.lower():
                    continue
                bin_p = parse_cue_first_bin(cue)
                if bin_p is not None and bin_p not in seen:
                    seen.add(bin_p)
                    candidates.append((bin_p, cue))
            for ext in ("*.iso", "*.bin", "*.img"):
                for p in sorted(root.glob(ext)):
                    if not p.is_file() or ".cn" in p.stem.lower() or p in seen:
                        continue
                    seen.add(p)
                    sib_cue = p.with_suffix(".cue")
                    candidates.append((p, sib_cue if sib_cue.is_file() else None))

        for img_p, cue_p in candidates:
            try:
                layout = parse_iso9660_stream(img_p)
                cnf = self._identify_iso_platform(img_p, layout)
                if cnf is not None:
                    return img_p, cue_p, None, False, layout, cnf
            except (OSError, ValueError):
                continue
        return None, None, None, False, None, None

    def _find_unpacked_cnf(self, root: Path) -> PsSystemCnf | None:
        root = Path(root)
        if not root.is_dir():
            return None
        for name in ("SYSTEM.CNF", "system.cnf"):
            p = root / name
            if p.is_file():
                try:
                    cnf = parse_ps_system_cnf(p.read_bytes())
                    if cnf is not None and cnf.platform == self.target_platform:
                        return cnf
                except OSError:
                    continue
        return None

    def _load_disc(
        self, root: Path
    ) -> tuple[Path | None, Path | None, Path | None, bool, StreamIsoLayout | None, dict[str, bytes], PsSystemCnf | None]:
        img_p, cue_p, chd_p, is_dvd, layout, cnf = self._find_disc(root)
        if chd_p is not None and img_p is None:
            return None, None, chd_p, is_dvd, None, {}, None
        if img_p is not None and layout is not None and cnf is not None:
            vfiles: dict[str, bytes] = {}
            with open(img_p, "rb") as fh:
                for rel, ent in layout.files.items():
                    if _is_ps_candidate_file(rel, ent.size, boot_path=cnf.boot_path):
                        vfiles[rel] = read_file_stream(fh, ent.lba, ent.size, layout.sector_size, layout.data_offset)
            return img_p, cue_p, chd_p, is_dvd, layout, vfiles, cnf

        cnf = self._find_unpacked_cnf(root)
        if cnf is not None:
            vfiles = {}
            for p in sorted(root.rglob("*")):
                if not p.is_file() or ".ferry-backup" in p.parts:
                    continue
                rel = p.relative_to(root).as_posix()
                try:
                    sz = p.stat().st_size
                except OSError:
                    continue
                if _is_ps_candidate_file(rel, sz, boot_path=cnf.boot_path):
                    if p.suffix.lower() == ".tbl" and is_thingy_tbl_file(p):
                        continue
                    try:
                        vfiles[rel] = p.read_bytes()
                    except OSError:
                        continue
            return None, None, None, False, None, vfiles, cnf

        return None, None, None, False, None, {}, None

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        try:
            img_p, _cue_p, chd_p, is_dvd, layout, files, cnf = self._load_disc(root)
        except ValueError as exc:
            return ProbeReport(engine=self.name, root=root, notes=[f"{self.display_platform} 镜像解析失败：{exc}"])

        if chd_p is not None and img_p is None:
            return ProbeReport(
                engine=f"{self.display_platform} (.chd)",
                root=root,
                readable=False,
                writable=False,
                container="chd",
                needs=["chdman"],
                notes=[
                    f"检测到 MAME 压缩光盘镜像：{chd_p.name}",
                    "需要安装开源工具 chdman（约 2.7 MB）解出轨道并校验 PlayStation 光盘结构。",
                ],
            )
        if cnf is None:
            return ProbeReport(engine=self.name, root=root, notes=[f"未找到 {self.display_platform} 光盘镜像或 SYSTEM.CNF"])

        src_tbl, dst_tbl = find_tbl_pair(root)
        allow_sjis = _is_jp_game_id(cnf.game_id)
        entries, _, _ = _collect_ps_units(root.name, files, src_tbl=src_tbl, allow_sjis=allow_sjis)
        gid = cnf.game_id or (layout.volume_id if layout else root.name)
        label = f"{self.display_platform} ({gid})"
        container = (
            f"{self.target_platform}-chd-{'dvd' if is_dvd else 'cd'}"
            if chd_p is not None
            else (
                f"{self.target_platform}-mode{layout.mode}-{layout.sector_size}"
                if layout is not None and layout.sector_size == CDROM_RAW_SECTOR_SIZE
                else (f"{self.target_platform}-iso-2048" if img_p is not None else f"{self.target_platform}-folder")
            )
        )

        if not entries:
            return ProbeReport(
                engine=label,
                root=root,
                readable=False,
                writable=False,
                container=container,
                notes=[
                    f"{self.display_platform} 游戏 [{gid}]（引导 {cnf.boot_path}，制式 {cnf.vmode or '未知'}）。",
                    f"已流式解析 ISO-9660（共 {len(layout.files) if layout else len(files)} 个文件），"
                    "但未检出未压缩明文台词（若该作使用自定义码表，可在镜像旁放入 ferry.tbl 后再扫）。",
                ],
            )

        notes = [
            f"{self.display_platform} 游戏 [{gid}]（引导 {cnf.boot_path}，制式 {cnf.vmode or '未知'}），共检出 {len(entries)} 条文本。",
        ]
        if src_tbl is not None:
            notes.append(f"已启用外挂 Thingy 码表：{src_tbl.name} → {dst_tbl.name if dst_tbl else src_tbl.name}。")
        if chd_p is not None:
            notes.append("回写将完成扇区覆写（2352 轨自动重算黄皮书 EDC/ECC），并经 chdman 无损封回 .cn.chd。")
        elif layout is not None and layout.sector_size == CDROM_RAW_SECTOR_SIZE:
            notes.append(
                f"回写将对 MODE{layout.mode}/2352 原始光盘扇区执行定长原位覆写，并自动重算黄皮书 EDC + ECC (P/Q) 与配套 .cn.cue。"
            )
        elif img_p is not None:
            notes.append("回写采用流式分块拷贝 + LBA 扇区原位覆写产出 .cn.iso 副本（支持 4.7 GB DVD 低内存直出）。")
        else:
            notes.append("回写将原地更新解包目录并通过 UndoLog 备份可撤销。")

        return ProbeReport(
            engine=label,
            root=root,
            readable=True,
            writable=True,
            unit_count=len(entries),
            container=container,
            text_format=f"ISO-9660 + {'SJIS/ASCII' if allow_sjis else 'ASCII'}" + (f" + {src_tbl.name}" if src_tbl else ""),
            unicode_engine=False,
            in_place=img_p is None,
            notes=notes,
        )

    def scan_units(self, root: Path) -> list[Unit]:
        root = Path(root).resolve()
        try:
            _img, _cue, _chd, _dvd, _layout, files, cnf = self._load_disc(root)
        except ValueError:
            return []
        if cnf is None:
            return []
        src_tbl, _ = find_tbl_pair(root)
        _, units, _ = _collect_ps_units(root.name, files, src_tbl=src_tbl, allow_sjis=_is_jp_game_id(cnf.game_id))
        return units

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        try:
            _img, _cue, _chd, _dvd, _layout, files, cnf = self._load_disc(root)
        except ValueError:
            return []
        if cnf is None:
            return []
        src_tbl, _ = find_tbl_pair(root)
        entries, _, _ = _collect_ps_units(root.name, files, src_tbl=src_tbl, allow_sjis=_is_jp_game_id(cnf.game_id))
        return entries

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        report = ApplyReport()
        try:
            img_p, cue_p, chd_p, is_dvd, layout, files, cnf = self._load_disc(root)
        except ValueError as exc:
            report.conflicts.append(str(exc))
            return report
        if cnf is None:
            report.notes.append(f"未找到 {self.display_platform} 镜像或 SYSTEM.CNF")
            return report

        src_tbl, dst_tbl = find_tbl_pair(root)
        entries, _, bin_units = _collect_ps_units(root.name, files, src_tbl=src_tbl, allow_sjis=_is_jp_game_id(cnf.game_id))
        if not entries:
            report.notes.append(f"{self.display_platform} 镜像内无可用文本单元")
            return report

        structured_entries = [e for e in entries if not e.kind.startswith("bin_")]
        modified, applied, missed, conflicts = patch_virtual_files(files, structured_entries, translations)
        for rel, units in bin_units.items():
            raw = files.get(rel)
            if raw is None:
                continue
            hit = False
            for u in units:
                tr = translations.get(u.key) or translations.get(u.src)
                if tr and tr.strip():
                    u.dst = tr
                    hit = True
                else:
                    missed += 1
            if not hit:
                continue
            patched, count, bin_conflicts = patch_blob(raw, units, tbl_encode=dst_tbl)
            conflicts.extend(bin_conflicts)
            if count > 0:
                modified[rel] = patched
                applied += count

        report.applied = applied
        report.missed = missed
        report.conflicts.extend(conflicts)
        if not modified:
            report.notes.append(f"未命中任何译文，未写出 {self.display_platform} 镜像")
            return report

        gid = cnf.game_id or self.target_platform.upper()
        undo = UndoLog.open(base, engine=self.name)

        if img_p is not None and layout is not None:
            def _patch_img_file_inplace(target_img: Path) -> None:
                with open(target_img, "r+b") as fh:
                    for rel, new_raw in modified.items():
                        ent = layout.files.get(rel)
                        if ent is None:
                            continue
                        allocated = ((ent.size + ISO_SECTOR_SIZE - 1) // ISO_SECTOR_SIZE) * ISO_SECTOR_SIZE
                        if len(new_raw) > max(ent.size, allocated):
                            report.conflicts.append(
                                f"{rel}：修改后大小 ({len(new_raw)}) 超出光盘扇区预留容量 ({allocated})"
                            )
                            continue
                        write_file_stream(
                            fh,
                            ent.lba,
                            new_raw.ljust(allocated, b"\x00"),
                            layout.sector_size,
                            layout.data_offset,
                            layout.mode,
                        )
                        if len(new_raw) != ent.size:
                            dir_sec = bytearray(
                                _read_sector_stream(fh, ent.dir_lba, layout.sector_size, layout.data_offset)
                            )
                            struct.pack_into("<I", dir_sec, ent.dir_offset_in_sector, len(new_raw))
                            struct.pack_into(">I", dir_sec, ent.dir_offset_in_sector + 4, len(new_raw))
                            write_file_stream(
                                fh,
                                ent.dir_lba,
                                bytes(dir_sec),
                                layout.sector_size,
                                layout.data_offset,
                                layout.mode,
                            )

            if chd_p is not None:
                chd_dep = deps.REGISTRY.get("chdman")
                chdman_exe = chd_dep.locate() if chd_dep else None
                if chdman_exe is None:
                    report.conflicts.append("缺少 chdman 工具，无法将修改后的镜像重新封回 .chd")
                    return report
                with tempfile.TemporaryDirectory(prefix="ferry_ps_chd_") as tmp_repack:
                    tmp_dir = Path(tmp_repack)
                    tmp_img = tmp_dir / img_p.name
                    shutil.copyfile(img_p, tmp_img)
                    _patch_img_file_inplace(tmp_img)

                    default_chd = chd_p.with_name(f"{chd_p.stem}.cn.chd")
                    target_chd = unique_output(Path(out) if out else default_chd)
                    if target_chd.is_dir():
                        target_chd = unique_output(target_chd / default_chd.name)

                    if is_dvd:
                        cmd = [str(chdman_exe), "createdvd", "-i", str(tmp_img), "-o", str(target_chd), "-f"]
                    else:
                        assert cue_p is not None
                        tmp_cue = tmp_dir / cue_p.name
                        old_cue = cue_p.read_text(encoding="utf-8-sig", errors="replace")
                        tmp_cue.write_text(rewrite_cue_for_patched_bin(old_cue, img_p.name, tmp_img.name), encoding="utf-8")
                        cmd = [str(chdman_exe), "createcd", "-i", str(tmp_cue), "-o", str(target_chd), "-f"]

                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        errors="replace",
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    if proc.returncode != 0 or not target_chd.is_file():
                        report.conflicts.append(f"chdman 重打包失败：{proc.stderr.strip() or proc.stdout.strip()}")
                        return report

                undo.did_copy(target_chd)
                report.undo_path = undo.finish()
                report.out_path = target_chd
                report.in_place = False
                report.notes.append(f"已生成 {self.display_platform} 汉化 CHD 镜像 [{gid}]：{target_chd.name}。")
                return report

            # 普通 .iso / .bin(+ .cue)：流式拷贝原镜像，再按扇区 seek 原位覆写
            default_out = img_p.with_name(f"{img_p.stem}.cn{img_p.suffix}")
            target_out = unique_output(Path(out) if out else default_out)
            if target_out.is_dir():
                target_out = unique_output(target_out / default_out.name)
            shutil.copyfile(img_p, target_out)
            _patch_img_file_inplace(target_out)
            undo.did_copy(target_out)

            cue_note = ""
            if cue_p is not None and cue_p.is_file():
                try:
                    old_cue = cue_p.read_text(encoding="utf-8-sig", errors="replace")
                    new_cue = rewrite_cue_for_patched_bin(old_cue, img_p.name, target_out.name)
                    new_cue_path = target_out.with_suffix(".cue")
                    new_cue_path.write_text(new_cue, encoding="utf-8")
                    undo.did_copy(new_cue_path)
                    cue_note = f"（已同步生成 {new_cue_path.name}）"
                except OSError:
                    pass

            report.undo_path = undo.finish()
            report.out_path = target_out
            report.in_place = False
            report.notes.append(f"已生成 {self.display_platform} 汉化光盘镜像 [{gid}]：{target_out.name}{cue_note}。")
            return report

        # 解包目录原地写回
        for rel, new_raw in modified.items():
            undo.will_modify(rel)
            (root / rel).write_bytes(new_raw)
            undo.did_write(rel)
        report.undo_path = undo.finish()
        report.out_path = root
        report.in_place = True
        report.notes.append(f"已原地更新 {self.display_platform} 解包目录 [{gid}] 共 {len(modified)} 个文件。")
        return report

    def apply_units(self, root: Path, units: list[Unit], out: Path | None = None) -> ApplyReport:
        mapping = {u.key: u.dst for u in units if u.done and u.translatable}
        return self.apply(root, mapping, out=out)


class Ps1Adapter(_PsDiscAdapter):
    """Sony PlayStation 1 (PSX) 光盘镜像与解包目录适配器。"""

    name = "ps1"
    priority = 14
    target_platform = "ps1"
    display_platform = "PlayStation 1"


class Ps2Adapter(_PsDiscAdapter):
    """Sony PlayStation 2 (PS2) DVD/CD 光盘镜像与解包目录适配器。"""

    name = "ps2"
    priority = 14
    target_platform = "ps2"
    display_platform = "PlayStation 2"
