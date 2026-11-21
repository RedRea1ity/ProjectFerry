"""RPG Maker 2000 / 2003 适配器（LcfDataBase .ldb / LcfMapUnit .lmu）。

RM2000/2003 的数据文件采用 LCF 二进制格式（头部魔数 `\\x0bLcfDataBase` / `\\x0aLcfMapUnit`），
顶层由 `[BER 块ID][BER 块长度][块数据]` 组成：

- 在 `MapNNNN.lmu` 里，块 `71` (0x47) 和 `72` (0x48) 是底层/上层地图图块 ID 矩阵，
  **真正的剧情对话与事件选项全部集中在块 `81` (0x51, Events)**；
- 在 `RPG_RT.ldb` 里，块 `19` (动画)、`20` (图块集)、`23` (开关名)、`24` (变量名) 是内部数据，
  **真正给玩家看的角色/物品/技能/状态/系统菜单词表/公共事件集中在块 `11..18, 21, 25`**。

通过先用 LCF BER 顶层块解析器精准切出「纯事件与数据库文本块」，再在块内按长度前缀提取
与定长原位覆盖（不改动任何 BER 块长度），既不会误伤半个地图图块，也绝不会破坏 LCF 块结构。
"""

from __future__ import annotations

from pathlib import Path

from ferrygame.rawbin import patch_blob, scan_sjis_strings
from ferrygame.units import Unit

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .undo import UNDO_NAME, UndoLog

_LDB_MAGIC = b"\x0bLcfDataBase"
_LMU_MAGIC = b"\x0aLcfMapUnit"

# LDB 中面向玩家的顶层块：11=角色 12=技能 13=物品 14=敌人 15=敌群 17=属性 18=状态 21=系统词汇表 25=公共事件
_LDB_TEXT_CHUNKS = {11, 12, 13, 14, 15, 17, 18, 21, 25}
# LMU 中面向玩家的顶层块：81 (0x51) = 地图事件（跳过 71/72 图块矩阵）
_LMU_TEXT_CHUNKS = {81}


def read_ber(blob: bytes, pos: int) -> tuple[int, int]:
    """读取 LCF BER 变长整数，返回 (值, 新位置)。"""
    val = 0
    n = len(blob)
    while pos < n:
        b = blob[pos]
        pos += 1
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return val, pos


def iter_lcf_chunks(blob: bytes) -> tuple[bytes, list[tuple[int, int, int]]]:
    """解析 LCF 顶层块，返回 (魔数, [(chunk_id, 数据起点, 数据长度), ...])。"""
    if len(blob) < 12:
        return b"", []
    name_len, pos = read_ber(blob, 0)
    if name_len not in (10, 11) or pos + name_len > len(blob):
        return b"", []
    magic = blob[: pos + name_len]
    if magic not in (_LDB_MAGIC, _LMU_MAGIC):
        return b"", []
    pos += name_len
    chunks: list[tuple[int, int, int]] = []
    n = len(blob)
    while pos < n:
        cid, pos = read_ber(blob, pos)
        if cid == 0 or pos >= n:
            break
        clen, pos = read_ber(blob, pos)
        if clen < 0 or pos + clen > n:
            break
        chunks.append((cid, pos, clen))
        pos += clen
    return magic, chunks


class RpgMaker2kAdapter(Adapter):
    """RPG Maker 2000 / 2003 专用适配器。"""

    name = "rpgmaker_2k"
    priority = 21  # 排在 Ace (22) 和 MV/MZ (25) 之前

    def __init__(self, include_samples: bool = False) -> None:
        self.include_samples = include_samples

    def _data_dir(self, root: Path) -> Path | None:
        root = Path(root).resolve()
        if root.is_file():
            root = root.parent
        for candidate in (root, *([p for p in root.iterdir() if p.is_dir()][:10] if root.is_dir() else [])):
            if (candidate / "RPG_RT.ldb").is_file() or any(candidate.glob("Map*.lmu")):
                return candidate
        return None

    def scan_units(self, root: Path) -> list[Unit]:
        data_dir = self._data_dir(root)
        if data_dir is None:
            return []
        base = Path(root).resolve()
        if base.is_file():
            base = base.parent

        units: list[Unit] = []
        files = sorted(list(data_dir.glob("RPG_RT.ldb")) + list(data_dir.glob("Map*.lmu")))
        for path in files:
            try:
                blob = path.read_bytes()
            except OSError:
                continue
            magic, chunks = iter_lcf_chunks(blob)
            if not chunks:
                continue
            allowed = _LDB_TEXT_CHUNKS if magic == _LDB_MAGIC else _LMU_TEXT_CHUNKS
            rel = path.relative_to(base).as_posix()
            for cid, start, length in chunks:
                if cid not in allowed:
                    continue
                sub = blob[start : start + length]
                found = scan_sjis_strings(sub, rel_path=rel, target_encoding="gbk")
                for u in found:
                    abs_off = start + int(u.meta["offset"])
                    u.meta["offset"] = abs_off
                    u.key = f"{rel}@0x{abs_off:x}"
                    if magic == _LDB_MAGIC and cid == 21:
                        u.kind = "menu"
                    units.append(u)
        return units

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root).resolve()
        data_dir = self._data_dir(root)
        if data_dir is None:
            return ProbeReport(engine=self.name, root=root, notes=["未发现 RPG_RT.ldb 或 MapNNNN.lmu"])
        units = self.scan_units(root)
        if not units:
            return ProbeReport(
                engine="RPG Maker 2000 / 2003",
                root=root,
                readable=False,
                writable=False,
                notes=["发现 LCF 文件，但未在事件块或数据库块中检出 Shift-JIS 文本"],
            )
        maps = len(list(data_dir.glob("Map*.lmu")))
        stories = sum(1 for u in units if u.kind == "story")
        menus = sum(1 for u in units if u.kind == "menu")
        return ProbeReport(
            engine="RPG Maker 2000 / 2003",
            root=root,
            readable=True,
            writable=True,
            unit_count=len(units),
            container="folder",
            text_format="LCF (.ldb / .lmu)",
            in_place=True,
            notes=[
                f"LCF 数据目录：{maps} 张地图，共抽出 {len(units)} 条文本（剧情 {stories} / 菜单与数据库 {menus}）",
                "已自动隔离地图图块矩阵 (0x47/0x48) 与开关/变量表，仅修改事件与词汇块",
            ],
        )

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).resolve()
        return [
            Entry(
                modid=root.name,
                key=u.key,
                english=u.src,
                source="game",
                kind=u.kind,
                rel_path=u.file,
                json_pointer=str(u.meta.get("offset", 0)),
            )
            for u in self.scan_units(root)
        ]

    def apply_units(self, root: Path, units: list[Unit], out: Path | None = None) -> ApplyReport:
        root = Path(root).resolve()
        base = root.parent if root.is_file() else root
        by_file: dict[str, list[Unit]] = {}
        for u in units:
            if u.dst and u.translatable:
                by_file.setdefault(u.file, []).append(u)
        if not by_file:
            return ApplyReport(notes=["没有可写回的 LCF 译文"])

        report = ApplyReport(in_place=True, out_path=base)
        undo = UndoLog.open(base, engine=self.name)
        for rel, group in sorted(by_file.items()):
            path = base / rel
            if not path.is_file():
                report.conflicts.append(f"找不到 LCF 文件：{rel}")
                continue
            blob = path.read_bytes()
            patched, count, conflicts = patch_blob(blob, group)
            report.conflicts.extend(conflicts)
            if count == 0:
                continue
            undo.will_modify(rel)
            path.write_bytes(patched)
            undo.did_write(rel)
            report.applied += count

        if report.applied > 0:
            report.undo_path = undo.finish()
        else:
            candidate = base / UNDO_NAME
            if candidate.is_file():
                report.undo_path = candidate
        return report

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        units = self.scan_units(root)
        for u in units:
            if u.key in translations:
                u.dst = translations[u.key]
            elif u.src in translations:
                u.dst = translations[u.src]
        return self.apply_units(root, units, out=out)
