"""Ren'Py 引擎适配器 —— 明文 `.rpy` 脚本，UTF-8，无编码问题。

跟 HSP 那种 ANSI 引擎是两个极端：Ren'Py 内核是 Python，字符串天生 Unicode，
`.rpy` 就是 UTF-8 纯文本。**改文本不需要碰显示层**，这是它最大的优势，
也是它为什么不该被当成"难啃的引擎"。

改的是 `game/` 下的 `.rpy` 原文，不是造 `tl/` 翻译包。理由：

- 摆渡的整个模型是「原文整条当 key」（gettext 的 msgid 语义），改原文最贴
- 造 `tl/` 要生成 `translate <lang> <hash>:` 块，hash 由 Ren'Py 内部算，
  算错就静默失效 —— 那是给自己找麻烦
- 用户改回原文只要 revert，不用去 `tl/` 里删文件

**RPA 归档：** Steam 作常把剧本打进 `archive.rpa`。Ren'Py 加载时散装
优先于包内，所以策略是抽出 `*.rpy` 到 `game/` 再改，**不重打包**。

**必须删 `.rpyc`。** Ren'Py 优先加载编译缓存，光改 `.rpy` 不删缓存等于没改，
而且不报错。这是本引擎最容易踩的坑，写进 `apply` 里。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .rpa import RpaError, find_archives, open_archive
from .undo import UNDO_NAME, UndoLog

# 这些语句开头的行，字符串不是给玩家看的（是文件名、变量名、跳转目标）。
# 宁可漏判几行，也不要把 `jump "chapter2"` 翻成中文 —— 那游戏当场崩。
_NOT_TEXT = re.compile(
    r"^\s*(?:"
    # 语句关键字
    r"\$|python|init|define|default|image|transform|style|screen|"
    r"label|jump|call|return|if|elif|else|while|for|pass|"
    r"play|queue|stop|scene|show|hide|with|window|voice|pause|"
    r"layeredimage|translate|old|new|"
    # 属性/参数关键词（在 say 语句里也常见，但只取第一个词判断）
    r"at|behind|onlayer|as|expression"
    r")\b"
)
_INDENT = re.compile(r"^(\s*)")
_SAY = re.compile(
    # 可选 speaker（含属性点号链）+ 一个引号字符串
    r'^\s*(?P<who>[A-Za-z_][\w.]*(?:\s+[A-Za-z_]\w*)*\s+)?'
    r'(?P<q>["\'])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*$'
)
_MENU_OPTION = re.compile(r'^\s*(?P<q>["\'])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*:\s*$')
_TL_STRING = re.compile(r'_\(\s*(?P<q>["\'])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)\s*\)')
# `$ player_name = renpy.input("What is your name?", length=12)` —— 取名提示是玩家看得到的
_INPUT_PROMPT = re.compile(
    r"renpy\.input\s*\(\s*(?P<q>[\"'])(?P<text>(?:\\.|(?!(?P=q)).)*)(?P=q)"
)
_COMMENT = re.compile(r"^\s*#")
# Ren'Py 里 `\[` / `\]` 是字面量方括号（旁白常用 `\[ ... \]`），不是 [变量]
_LITERAL_BRACKET = re.compile(r"\\[\[\]]")
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")


class RenpyAdapter(Adapter):
    name = "renpy"
    priority = 20  # 判据很明确（game/ + .rpy），比通用兜底靠前

    def __init__(self, include_samples: bool = False) -> None:
        # Ren'Py 没有官方示例剧本混在游戏里的情况，参数只为接口一致
        self.include_samples = include_samples

    # ---- 探测 ----

    def _game_dir(self, root: Path) -> Path | None:
        """`game/` 目录，或直接把 `game/` 本身当 root 传进来。

        先补全绝对路径：传 `.` 进来时 `Path('.').name` 是空串，
        `root.name == "game"` 永远不成立，会白白认不出。

        有散装 `.rpy` 或 `.rpa` 都算认领成功（剧本可能全在归档里）。
        """
        root = Path(root).absolute()
        candidate = root / "game"
        if candidate.is_dir() and self._has_scripts(candidate):
            return candidate
        if root.name == "game" and self._has_scripts(root):
            return root
        return None

    @staticmethod
    def _has_scripts(game: Path) -> bool:
        if any(game.glob("*.rpa")):
            return True
        if any(game.rglob("*.rpy")):
            return True
        return False

    def _archives(self, game: Path) -> list[Path]:
        return find_archives(game)

    def _scripts(self, root: Path) -> list[Path]:
        game = self._game_dir(root)
        if game is None:
            return []
        # 跳过 Ren'Py 自己的 tl/ 和 cache/ —— 前者是已有翻译，后者是引擎缓存
        out = []
        for path in sorted(game.rglob("*.rpy")):
            rel = path.relative_to(game)
            if rel.parts and rel.parts[0] in ("tl", "cache", "saves"):
                continue
            out.append(path)
        return out

    def _iter_script_sources(self, root: Path) -> list[tuple[str, str]]:
        """返回 [(相对 root 的路径, 文本内容), ...]。

        优先散装 `.rpy`；若没有，则直接从 `.rpa` 读（probe/collect 不落盘）。
        """
        root = Path(root).absolute()
        game = self._game_dir(root)
        if game is None:
            return []
        loose = self._scripts(root)
        if loose:
            out: list[tuple[str, str]] = []
            for path in loose:
                rel = path.relative_to(root).as_posix()
                try:
                    text = path.read_bytes().decode("utf-8-sig")
                except (OSError, UnicodeDecodeError):
                    continue
                out.append((rel, text))
            return out

        # 全在归档里：内存读取
        sources: list[tuple[str, str]] = []
        for archive_path in self._archives(game):
            try:
                archive = open_archive(archive_path)
            except (RpaError, OSError):
                continue
            for name in archive.scripts():
                try:
                    raw = archive.read(name)
                    text = raw.decode("utf-8-sig")
                except (RpaError, OSError, UnicodeDecodeError, KeyError):
                    continue
                rel = (game.relative_to(root) / Path(*name.split("/"))).as_posix()
                sources.append((rel, text))
        return sources

    def ensure_extracted(self, root: Path, log: UndoLog | None = None) -> tuple[list[str], set[str]]:
        """若剧本只在 `.rpa` 里，抽到 `game/` 散装（Ren'Py 会优先用散装）。

        返回 (说明, 新抽出文件的相对路径集合)。已有散装则不动。
        新抽出的记 undo=added；回写译文时不要再 will_modify（revert 删文件即可）。
        """
        root = Path(root).absolute()
        game = self._game_dir(root)
        if game is None:
            return [], set()
        if self._scripts(root):
            return [], set()
        notes: list[str] = []
        added: set[str] = set()
        for archive_path in self._archives(game):
            try:
                archive = open_archive(archive_path)
            except (RpaError, OSError) as exc:
                notes.append(f"{archive_path.name}：打不开（{exc}）")
                continue
            try:
                written = archive.extract_scripts(game)
            except (RpaError, OSError) as exc:
                notes.append(f"{archive_path.name}：抽出失败（{exc}）")
                continue
            for path in written:
                rel = path.relative_to(root).as_posix()
                added.add(rel)
                if log is not None:
                    try:
                        log.will_add(rel)
                        log.did_write(rel)
                    except OSError as exc:
                        notes.append(f"{rel}：撤销记录失败（{exc}）")
            if written:
                notes.append(
                    f"已从 {archive_path.name} 抽出 {len(written)} 个 .rpy"
                    "（散装优先，无需重打包）。"
                )
        return notes, added

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root)
        game = self._game_dir(root)
        if game is None:
            return ProbeReport(engine=self.name, root=root,
                               notes=["没找到 game/*.rpy 或 game/*.rpa，不是 Ren'Py 工程"])
        sources = self._iter_script_sources(root)
        units = self.collect(root)
        archives = self._archives(game)
        report = ProbeReport(
            engine="Ren'Py",
            root=root,
            readable=True,
            # 明文 UTF-8 + 磁盘可写 = 能写回。不需要任何外部工具。
            writable=True,
            # Ren'Py 有 `--compile` 但没有读画面的通道，翻完要人进游戏看
            verifiable=False,
            unit_count=len(units),
            container="rpa" if archives and not self._scripts(root) else "folder",
            text_format=".rpy",
            unicode_engine=True,
            in_place=True,
        )
        if archives:
            report.notes.append(
                f"发现 {len(archives)} 个 .rpa；"
                + (
                    "剧本已在散装 .rpy 中。"
                    if self._scripts(root)
                    else "剧本在归档内，回写前会抽出 .rpy（不重打包）。"
                )
            )
        rpyc = sum(1 for _ in game.rglob("*.rpyc"))
        if rpyc:
            report.notes.append(
                f"有 {rpyc} 个 .rpyc 编译缓存；回写时会一并删掉对应缓存，"
                "否则 Ren'Py 会继续用旧文本且不报错。"
            )
        if (game / "tl").is_dir():
            report.notes.append("已存在 game/tl/ 翻译目录；本适配器改的是原文，不动 tl/。")
        report.notes.append(f"扫到 {len(sources)} 个 .rpy 脚本。")
        report.notes.append("字符串里的 [变量] 和 {格式标签} 交给翻译层保留，写回时会校验标签数。")
        return report

    # ---- 抽取 ----

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).absolute()
        entries: list[Entry] = []
        for rel, text in self._iter_script_sources(root):
            for index, line in enumerate(text.split("\n"), start=1):
                kind, payload = classify_line(line)
                if kind is None:
                    continue
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}#{index}",
                        english=payload,
                        source="game",
                        kind=kind,
                        rel_path=rel,
                        json_pointer=str(index),
                    )
                )
        return entries

    # ---- 回写 ----

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        """原地改 .rpy。

        这里不产出副本：Ren'Py 工程动辄几个 GB（assets 全在里面），
        复制一份毫无意义。原地改 + `revert` 是这个引擎的正解，
        前提是 undo 记录必须先落盘。

        若剧本在 `.rpa` 里：先抽到散装（记入 undo 的 added），再改文件。
        """
        root = Path(root).absolute()
        report = ApplyReport()
        if out is not None:
            report.notes.append(
                "Ren'Py 支持原地汉化，没收 --out；要保留原样请先自己备份整个目录。"
            )

        log = UndoLog.open(root, engine=self.name)
        extract_notes, extracted_rels = self.ensure_extracted(root, log=log)
        report.notes.extend(extract_notes)

        entries = self.collect(root)
        if not entries:
            report.notes.append("没有文本单元")
            if extract_notes:
                log.finish()
            return report

        by_file: dict[str, list[Entry]] = {}
        for entry in entries:
            by_file.setdefault(entry.rel_path, []).append(entry)

        pending: dict[Path, list[str]] = {}

        for rel_path, items in by_file.items():
            path = root / rel_path
            try:
                text = path.read_bytes().decode("utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                report.notes.append(f"{rel_path}：读不了（{exc}）")
                continue
            lines = text.split("\n")
            changed = False
            for entry in items:
                translated = translations.get(entry.key)
                if translated is None:
                    translated = translations.get(entry.english)
                if translated is None or not translated.strip():
                    report.missed += 1
                    continue
                index = int(entry.json_pointer) - 1
                if not (0 <= index < len(lines)):
                    report.conflicts.append(f"{entry.key}：行号越界")
                    continue
                problem = check_tags(entry.english, translated)
                if problem:
                    report.conflicts.append(f"{entry.key}：{problem}")
                    continue
                try:
                    lines[index] = replace_line(lines[index], entry.kind, translated)
                except ValueError as exc:
                    report.conflicts.append(f"{entry.key}：{exc}")
                    continue
                changed = True
                report.applied += 1
            if changed:
                pending[path] = lines

        if not report.applied:
            if extract_notes:
                log.finish()
                report.notes.append("已抽出 .rpy，但一条译文都没写回去。")
            else:
                report.notes.append("一条都没写回去，没有动任何文件")
            return report

        # 先备份再写。刚从 RPA 抽出的走 added，不要再 will_modify。
        try:
            for path in pending:
                rel = path.relative_to(root).as_posix()
                if rel not in extracted_rels:
                    log.will_modify(rel)
        except OSError as exc:
            report.conflicts.append(f"撤销记录写不出来，放弃回写：{exc}")
            return report

        for path, lines in pending.items():
            rel = path.relative_to(root).as_posix()
            try:
                path.write_bytes("\n".join(lines).encode("utf-8"))
            except OSError as exc:
                report.notes.append(f"{rel}：写失败（{exc}）")
                continue
            log.did_write(rel)
            # 删编译缓存：Ren'Py 优先用 .rpyc，不删等于没改，而且不报错
            cache = path.with_suffix(".rpyc")
            if cache.exists():
                try:
                    cache.unlink()
                except OSError as exc:
                    report.notes.append(f"{cache.name} 删不掉（{exc}），游戏可能仍在用旧文本")
        log.finish()

        report.out_path = root
        report.in_place = True
        report.undo_path = root / UNDO_NAME
        report.notes.append("已原地改 .rpy；`revert` 可还原，删 DLL/缓存之类的手工残留不在本适配器范围内。")
        return report


# ---- 行分类与替换（纯函数，单独测） ----


def classify_line(line: str) -> tuple[str | None, str]:
    """判断这一行里有没有该翻的文本。返回 (kind, 原文)。

    kind: "say"（对话/旁白）/ "menu"（选项）/ "input"（renpy.input 提示）/ None
    """
    if not line.strip() or _COMMENT.match(line):
        return None, ""
    # `$ ...` 必须单独处理：`_NOT_TEXT` 里的 `\$\b` 匹配不到 `$ `（$ 与空格之间没有单词边界）。
    if line.lstrip().startswith("$"):
        match = _TL_STRING.search(line)
        if match:
            text = _unescape(match.group("text"))
            if _worth_translating(text):
                return "say", text
        match = _INPUT_PROMPT.search(line)
        if match:
            text = _unescape(match.group("text"))
            if _worth_translating(text):
                return "input", text
        return None, ""
    if _NOT_TEXT.match(line):
        return None, ""

    option = _MENU_OPTION.match(line)
    if option:
        return "menu", _unescape(option.group("text"))

    say = _SAY.match(line)
    if say:
        text = _unescape(say.group("text"))
        # 只有数字/纯 ASCII 标识符的"字符串"多半是资源名，不翻
        if not _worth_translating(text):
            return None, ""
        return "say", text
    return None, ""


def _worth_translating(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    # 已是中文写回（无假名）—— 半成品续跑时跳过，避免把中文再送去翻。
    # 日文原文通常带平假名/片假名，不会被这条误伤。
    kana = len(_KANA_RE.findall(stripped))
    cjk = len(_CJK_RE.findall(stripped))
    latin_words = len(_LATIN_WORD_RE.findall(stripped))
    if kana == 0 and cjk >= 2 and cjk >= max(latin_words, 1):
        return False
    # 有空格或非 ASCII = 人流文本；单个英文单词多半是 tag/名字，先放过
    return " " in stripped or any(ord(ch) > 127 for ch in stripped)


def replace_line(raw: str, kind: str, translated: str) -> str:
    """把译文塞回这一行，保留缩进、speaker、行尾冒号。"""
    if '"' in translated or "'" in translated:
        raise ValueError("译文含直引号，会破坏语法（请用中文引号）")
    if "\n" in translated:
        raise ValueError("译文含换行")
    indent = _INDENT.match(raw).group(1)

    if kind == "menu":
        return f'{indent}"{translated}":'
    if kind == "input":
        match = _INPUT_PROMPT.search(raw)
        if not match:
            raise ValueError("这一行没有 renpy.input 提示语")
        return raw[: match.start("text")] + translated + raw[match.end("text") :]
    # `$ _("原文")` —— 只替换括号里的字符串
    tl = _TL_STRING.search(raw)
    if tl and _NOT_TEXT.match(raw):
        return raw[: tl.start("text")] + translated + raw[tl.end("text") :]
    match = _SAY.match(raw)
    if not match:
        raise ValueError("这一行不是可识别的 say 语句")
    who = (match.group("who") or "").rstrip()
    prefix = f"{who} " if who else ""
    return f'{indent}{prefix}"{translated}"'


def _unescape(text: str) -> str:
    """Ren'Py 里 `\\"` 是转义引号，翻的时候要还原成真引号给模型看。"""
    return text.replace('\\"', '"').replace("\\'", "'")


_TAG = re.compile(r"\{/?[a-zA-Z]+[^{}]*\}|(?<!\\)\[[^\[\]]+\]")
_ESCAPE = re.compile(r"\\.")


def check_tags(source: str, translated: str) -> str:
    """译文里的 {格式标签} 和 [变量] 必须原样保留，否则游戏里会缺字或报错。

    Ren'Py 的 \\[ \\] 是字面量括号（旁白装饰），先抹掉再比 —— 否则整段
    旁白会被当成一个「变量」，译文一改内容就拒写。

    数量对不上就拒绝写回 —— 宁可这条没翻，也不要让玩家看到格式符裸露。
    """
    src_n = _LITERAL_BRACKET.sub("", source)
    dst_n = _LITERAL_BRACKET.sub("", translated)
    src = sorted(_TAG.findall(src_n))
    dst = sorted(_TAG.findall(dst_n))
    if src != dst:
        return f"格式标签/变量被改动（原文 {src}，译文 {dst}）"
    # 字面量 `\\[` `\\]` 也算转义，必须成对保留；其它转义同理
    src_esc = len(_ESCAPE.findall(source))
    dst_esc = len(_ESCAPE.findall(translated))
    if src_esc != dst_esc:
        return f"转义字符数量不一致（原文 {src_esc}，译文 {dst_esc}）"
    return ""
