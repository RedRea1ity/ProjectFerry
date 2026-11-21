"""HSP3（Hot Soup Processor）ANSI 引擎适配器 —— 唯一需要动显示层的一类。

跟 Ren'Py / RM 完全不同：那些引擎的文本是 UTF-8/UTF-16，改完文件就完事。
HSP 的 ANSI 版把字节交给 GDI，GDI 按系统代码页（日文机上是 SJIS）解释，
所以**光把 GBK 中文写进文件，屏幕上还是乱码**。

两条腿缺一不可：

    数据层  剧本是外部 CSV 指令表（明文），逐字段替换成 GBK
    显示层  放一个替身 `msimg32.dll`，钩住 `TabbedTextOutA` 转宽字符再画

这条路的依据是上一轮四轮否定之后实机验出来的（见 `docs/research/hsp/`）：
`MultiByteToWideChar` 和 `DrawTextA` 全程 0 次调用，改 `CreateFontIndirectA`
的 charset 画面一个字不变 —— 只有钩 `TabbedTextOutA` 有效。

**文本识别不按指令名硬编码。** 上一个游戏的对话指令叫 `z8_msg`，
下一个游戏可能叫 `mes`。所以判据是「这一行像不像 CSV + 这个字段里有没有日文」，
任何用 csv 插件读剧本的 HSP 游戏都适用。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..models import Entry
from . import pe
from .base import Adapter, ApplyReport, ProbeReport
from .undo import UNDO_NAME, UndoLog

# HSP 运行时 DLL：有这些就是 HSP 游戏，不用看 exe
_HSP_RUNTIME = ("hsp3imp.dll", "hspda.dll", "hspext.dll", "hspogg.dll", "hsp3utf.dll")
# 这些目录里是引擎自己的东西，不扫
_SKIP_DIRS = {"data", "savedata", "save", "log", "cache", "temp", "backup", "system"}
# 备份目录一律跳过。
# 必须按名字认：用户自己会把 eve/ 复制成 eve_bak，摆渡上一轮也建过 eve_sjis_bak。
# 把备份当剧本翻一遍，等于白烧一遍额度，还会让「到底改了哪份」变得说不清。
_BAK_DIR = re.compile(r"(^|[_\-. ])(bak|backup|orig|original|old|copy|back)([_\-. ]|$)", re.IGNORECASE)

_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uff66-\uff9f]")
_PATHLIKE = re.compile(r"^[\x20-\x7e]*[\\/][\x20-\x7e]*$")
_ASSET_EXT = re.compile(r"\.(ogg|wav|mp3|png|jpg|jpeg|bmp|gif|webm|mpg|avi|as|dll|exe)$", re.IGNORECASE)
# HSP 剧本的注释：`//` 或 `;` 开头。注释里的日文是给作者看的，不是给玩家的。
_COMMENT = re.compile(r"^\s*(//|;)")

# 一个文件里至少要有这么多行「像 CSV」，才当它是剧本
_SAMPLE_LINES = 120
_MIN_CSV_RATIO = 0.75
_MIN_TEXT_UNITS = 3


@dataclass
class _Unit:
    """一条可翻的字段。行号从 1 开始，字段序号从 0 开始。"""

    line_no: int
    field_no: int
    text: str


class HspAdapter(Adapter):
    name = "hsp"
    priority = 40

    def __init__(self, include_samples: bool = False) -> None:
        self.include_samples = include_samples

    # ---- 定位 ----

    def _exe(self, root: Path) -> Path | None:
        return pe.exe_of(Path(root))

    def _looks_like_hsp(self, root: Path) -> bool:
        root = Path(root)
        try:
            names = {p.name.lower() for p in root.iterdir() if p.is_file()}
        except OSError:
            return False
        if any(name in names for name in _HSP_RUNTIME):
            return True
        # 有的打包工具把 dll 塞进子目录
        try:
            for sub in root.iterdir():
                if sub.is_dir() and any((sub / n).exists() for n in _HSP_RUNTIME):
                    return True
        except OSError:
            pass
        return False

    def _script_files(self, root: Path) -> list[Path]:
        """候选剧本：.txt / .csv，且抽样看起来像 CSV 指令表。"""
        root = Path(root)
        out = []
        try:
            candidates = sorted(
                p for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() in (".txt", ".csv")
                and not _skipped_path(p, root)
                and p.stat().st_size < 32 * 1024 * 1024
            )
        except OSError:
            return out
        for path in candidates[:3000]:
            if _looks_like_script(path):
                out.append(path)
        return out

    # ---- 探测 ----

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root)
        exe = self._exe(root)
        if not self._looks_like_hsp(root):
            return ProbeReport(engine=self.name, root=root,
                               notes=["目录里没有 HSP 运行时（hsp3imp.dll 等），不是 HSP 游戏"])
        if exe is None:
            return ProbeReport(engine=self.name, root=root,
                               notes=["找不到主程序 exe（大于 100 KB 的那个）"])

        table = pe.read_imports(exe)
        if table is None:
            return ProbeReport(engine=self.name, root=root, notes=[f"{exe.name} 读不出 PE 导入表"])

        scripts = self._script_files(root)
        units = self.collect(root)

        report = ProbeReport(
            engine="HSP3 (ANSI)",
            root=root,
            readable=bool(units),
            writable=False,
            verifiable=False,
            unit_count=len(units),
            container="folder",
            text_format=".csv",
            # HSP 的显示层要放 DLL，但 DLL 是内嵌的，不需要用户装任何东西
            needs=[],
            in_place=True,
        )

        # 代理能不能挂上，是「写不写回」的前提 —— 挂不上就等于中文一定乱码
        if table.bits != 32:
            report.notes.append(
                f"{exe.name} 是 {table.bits} 位（{table.arch}）。内嵌的代理是 32 位，挂不上；"
                "64 位 HSP 需要另编一份 64 位代理。"
            )
        else:
            proxy, reason = self._pick_proxy(exe, table)
            if proxy:
                report.writable = bool(units)
                report.notes.append(f"代理空位：{proxy}（{reason}）")
            else:
                report.notes.append(f"找不到可用的代理空位：{reason}")
                report.notes.append(
                    "没有代理就只能改文本，中文在游戏里仍会显示为乱码 —— "
                    "所以按「写不回去」处理，不浪费翻译额度。"
                )

        if not scripts:
            report.notes.append("没找到 CSV 指令表格式的剧本文件")
        else:
            report.notes.append(f"剧本文件 {len(scripts)} 个，共 {len(units)} 条可翻字段。")

        # 混排风险：同一行里已翻和未翻的字段被一起渲染时可能显示混乱
        report.notes.append(
            "提示：同一行里若只有部分字段被翻，那一行可能显示混乱。"
            "同一个文件建议一次翻完（本适配器不要求 100%，但整文件翻完最稳）。"
        )
        report.notes.append("UI 固定文字在加密的脚本包里，本适配器不处理（L1 迭代再说）。")
        return report

    def _pick_proxy(self, exe: Path, table: pe.ImportTable) -> tuple[str, str]:
        """挑一个能安全替身的 DLL。优先白名单，逐个验证。"""
        for name in pe.proxy_candidates(table):
            ok, reason = pe.proxy_verdict(exe, name, table)
            if ok:
                return name, reason
        # 白名单全灭时，把最常见的那个失败理由带出去
        for name in pe.PROXY_CANDIDATES:
            ok, reason = pe.proxy_verdict(exe, name, table)
            if not ok and "没有静态导入" not in reason:
                return "", reason
        return "", "exe 没有静态导入任何可替身的非系统 DLL"

    # ---- 抽取 ----

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root)
        entries: list[Entry] = []
        for path in self._script_files(root):
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                rel = path.name
            for unit in _read_units(path):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}#{unit.line_no}#{unit.field_no}",
                        english=unit.text,
                        source="game",
                        kind="line",
                        rel_path=rel,
                        json_pointer=f"{unit.line_no}#{unit.field_no}",
                    )
                )
        return entries

    # ---- 回写 ----

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        root = Path(root)
        report = ApplyReport()
        if out is not None:
            report.notes.append("HSP 支持原地汉化，没收 --out。")

        exe = self._exe(root)
        if exe is None:
            report.notes.append("找不到主程序，不动任何文件")
            return report
        table = pe.read_imports(exe)
        if table is None:
            report.notes.append("读不出导入表，不敢放代理")
            return report
        proxy_name, _why = self._pick_proxy(exe, table)
        if not proxy_name:
            report.notes.append("没有可用代理空位，中文会乱码，因此不回写")
            return report

        entries = self.collect(root)
        if not entries:
            report.notes.append("没有文本单元")
            return report

        by_file: dict[str, list[Entry]] = {}
        for entry in entries:
            by_file.setdefault(entry.rel_path, []).append(entry)

        log = UndoLog.open(root, engine=self.name)
        pending: dict[Path, bytes] = {}
        partial_files: list[str] = []

        for rel_path, items in by_file.items():
            path = root / rel_path
            try:
                raw = path.read_bytes()
            except OSError as exc:
                report.notes.append(f"{rel_path}：读不了（{exc}）")
                continue
            wanted: dict[tuple[int, int], tuple[str, str]] = {}
            for entry in items:
                translated = translations.get(entry.key)
                if translated is None:
                    translated = translations.get(entry.english)
                if translated is None or not translated.strip():
                    report.missed += 1
                    continue
                line_no, field_no = entry.json_pointer.split("#")
                wanted[(int(line_no), int(field_no))] = (entry.english, translated.strip())
            if not wanted:
                continue

            new_raw, applied, conflicts = _rewrite(raw, wanted)
            report.applied += applied
            report.conflicts.extend(f"{rel_path}：{c}" for c in conflicts)
            if applied and applied < len(items):
                partial_files.append(f"{rel_path}（{applied}/{len(items)}）")
            if applied:
                pending[path] = new_raw

        if not pending:
            report.notes.append("一条都没写回去，没有动任何文件")
            return report

        # 先备份，再写。备份和记录都在第一个字节之前完成。
        try:
            for path in pending:
                log.will_modify(path.relative_to(root).as_posix())
        except OSError as exc:
            report.conflicts.append(f"撤销记录写不出来，放弃回写：{exc}")
            return report

        for path, blob in pending.items():
            rel = path.relative_to(root).as_posix()
            try:
                path.write_bytes(blob)
            except OSError as exc:
                report.notes.append(f"{rel}：写失败（{exc}）")
                continue
            log.did_write(rel)

        # 部署显示层代理。这是「同一个东西」：它不改游戏逻辑，只改字节怎么变宽字符。
        dll_target = root / proxy_name
        deployed = False
        blob = b""
        try:
            from . import hsp_assets

            blob = hsp_assets.load()
            allow_write = True
            if dll_target.exists():
                if dll_target.read_bytes() == blob:
                    # 上次已经部署过，内容一致，不用重复写
                    deployed = True
                    allow_write = False
                else:
                    # 内容不同 —— 可能是游戏自带的，也可能是旧版代理。
                    # 分不清就不覆盖：宁可这次没生效，也别把别人的 DLL 冲掉。
                    report.notes.append(
                        f"{proxy_name} 已存在且内容不同，没有覆盖。"
                        f"如果确认是旧版代理，先 revert 或手动删掉它再跑一次。"
                    )
                    allow_write = False
            if allow_write:
                log.will_add(proxy_name)
                dll_target.write_bytes(blob)
                log.did_write(proxy_name)
                deployed = True
        except OSError as exc:
            report.notes.append(f"代理 DLL 部署失败（{exc}）；文本已改但中文会显示为乱码")
        log.finish()

        report.out_path = root
        report.in_place = True
        report.undo_path = root / UNDO_NAME
        if deployed:
            report.notes.append(f"已部署 {proxy_name}（内嵌，{len(blob):,} 字节），revert 会删掉它。")
        if partial_files:
            shown = "、".join(partial_files[:4])
            more = f" 等 {len(partial_files)} 个文件" if len(partial_files) > 4 else ""
            report.notes.append(f"这些文件只翻了一部分，同屏可能混排：{shown}{more}")
        report.notes.append("改完要重启游戏才生效（DLL 只在进程启动时加载）。")
        return report


# ---- 文件层：识别与改写（纯函数，单独测） ----


def _skipped_path(path: Path, root: Path) -> bool:
    """路径里有没有该跳过的目录段。"""
    try:
        parts = path.relative_to(root).parts[:-1]
    except ValueError:
        return True
    for part in parts:
        lowered = part.lower()
        if lowered in _SKIP_DIRS or _BAK_DIR.search(part):
            return True
        if lowered.startswith("."):
            return True
    return False


def _looks_like_script(path: Path) -> bool:
    """抽样判断：这个文件像不像 CSV 指令表。"""
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    if not raw:
        return False
    lines = raw.split(b"\n")[:_SAMPLE_LINES]
    if not lines:
        return False
    csv_like = 0
    text_like = 0
    for line in lines:
        body = line.rstrip(b"\r")
        if not body.strip():
            continue
        if _COMMENT.match(_decode(body)):
            continue
        fields = body.split(b",")
        if len(fields) >= 2:
            csv_like += 1
        for field in fields:
            text = _decode(field)
            if text and _is_translatable(text):
                text_like += 1
                break
    total = sum(
        1 for line in lines
        if line.strip() and not _COMMENT.match(_decode(line.rstrip(b"\r")))
    )
    if not total:
        return False
    return csv_like / total >= _MIN_CSV_RATIO and text_like >= _MIN_TEXT_UNITS


def _decode(raw: bytes) -> str:
    for codec in ("cp932", "utf-8"):
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            continue
    return ""


def _is_translatable(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 2:
        return False
    if not _CJK.search(stripped):
        return False
    # 资源路径 / 文件名不是给人看的
    if _PATHLIKE.match(stripped) or _ASSET_EXT.search(stripped):
        return False
    return True


def _read_units(path: Path) -> list[_Unit]:
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    out: list[_Unit] = []
    for index, line in enumerate(raw.split(b"\n"), start=1):
        body = line.rstrip(b"\r")
        if not body.strip() or _COMMENT.match(_decode(body)):
            continue
        for field_no, field in enumerate(body.split(b",")):
            text = _decode(field)
            if text and _is_translatable(text):
                out.append(_Unit(line_no=index, field_no=field_no, text=text.strip()))
    return out


def _rewrite(raw: bytes, wanted: dict[tuple[int, int], tuple[str, str]]) -> tuple[bytes, int, list[str]]:
    """逐字段替换，**未翻的字段原字节照抄**。

    不能整行重新编码：那会把没翻的 SJIS 日文也用 GBK 编一遍，原本能显示的
    日文全变乱码。只动该动的字节，是这条路唯一安全的写法。

    `wanted` 的值是 (原文, 译文)。原文用来核对磁盘上的内容还是不是当初那句 ——
    文件被外部改过、或行号错位时，宁可跳过这一条，也不要把别人的字盖掉。
    """
    applied = 0
    conflicts: list[str] = []
    out_lines = []
    for index, line in enumerate(raw.split(b"\n"), start=1):
        body = line.rstrip(b"\r")
        tail = b"\r" if line.endswith(b"\r") else b""
        if not body.strip():
            out_lines.append(line)
            continue
        fields = body.split(b",")
        touched = False
        for field_no, field in enumerate(fields):
            key = (index, field_no)
            if key not in wanted:
                continue
            original_expected, translated = wanted[key]
            current = _decode(field).strip()
            if current != original_expected:
                conflicts.append(f"第 {index} 行第 {field_no} 列：内容和记录不符，跳过")
                continue
            if "," in translated or '"' in translated:
                conflicts.append(f"第 {index} 行第 {field_no} 列：译文含半角逗号或引号，会破坏 CSV")
                continue
            try:
                blob = translated.encode("gbk")
            except UnicodeEncodeError as exc:
                conflicts.append(f"第 {index} 行第 {field_no} 列：译不了 GBK（{exc}）")
                continue
            # 保留字段原有的前导缩进（有的剧本用空格对齐）
            lead = field[: len(field) - len(field.lstrip(b" \t"))]
            fields[field_no] = lead + blob
            touched = True
            applied += 1
        out_lines.append(b",".join(fields) + tail if touched else line)
    return b"\n".join(out_lines), applied, conflicts
