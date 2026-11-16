"""Ren'Py 引擎适配器 —— 明文 `.rpy` 脚本，UTF-8，无编码问题。

跟 HSP 那种 ANSI 引擎是两个极端：Ren'Py 内核是 Python，字符串天生 Unicode，
`.rpy` 就是 UTF-8 纯文本。**改文本不需要碰显示层**，这是它最大的优势，
也是它为什么不该被当成"难啃的引擎"。

改的是 `game/` 下的 `.rpy` 原文，不是造 `tl/` 翻译包。理由：

- 摆渡的整个模型是「原文整条当 key」（gettext 的 msgid 语义），改原文最贴
- 造 `tl/` 要生成 `translate <lang> <hash>:` 块，hash 由 Ren'Py 内部算，
  算错就静默失效 —— 那是给自己找麻烦
- 用户改回原文只要 revert，不用去 `tl/` 里删文件

**必须删 `.rpyc`。** Ren'Py 优先加载编译缓存，光改 `.rpy` 不删缓存等于没改，
而且不报错。这是本引擎最容易踩的坑，写进 `apply` 里。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
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
_COMMENT = re.compile(r"^\s*#")


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
        """
        root = Path(root).absolute()
        candidate = root / "game"
        if candidate.is_dir() and any(candidate.rglob("*.rpy")):
            return candidate
        if root.name == "game" and any(root.rglob("*.rpy")):
            return root
        return None

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

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root)
        game = self._game_dir(root)
        if game is None:
            return ProbeReport(engine=self.name, root=root,
                               notes=["没找到 game/*.rpy，不是 Ren'Py 工程"])
        scripts = self._scripts(root)
        units = self.collect(root)
        report = ProbeReport(
            engine="Ren'Py",
            root=root,
            readable=True,
            # 明文 UTF-8 + 磁盘可写 = 能写回。不需要任何外部工具。
            writable=True,
            # Ren'Py 有 `--compile` 但没有读画面的通道，翻完要人进游戏看
            verifiable=False,
            unit_count=len(units),
            container="folder",
            text_format=".rpy",
            unicode_engine=True,
        )
        rpyc = sum(1 for _ in game.rglob("*.rpyc"))
        if rpyc:
            report.notes.append(
                f"有 {rpyc} 个 .rpyc 编译缓存；回写时会一并删掉对应缓存，"
                "否则 Ren'Py 会继续用旧文本且不报错。"
            )
        if (game / "tl").is_dir():
            report.notes.append("已存在 game/tl/ 翻译目录；本适配器改的是原文，不动 tl/。")
        report.notes.append(f"扫到 {len(scripts)} 个 .rpy 脚本。")
        report.notes.append("字符串里的 [变量] 和 {格式标签} 交给翻译层保留，写回时会校验标签数。")
        return report

    # ---- 抽取 ----

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).absolute()
        game = self._game_dir(root)
        if game is None:
            return []
        entries: list[Entry] = []
        for path in self._scripts(root):
            rel = path.relative_to(root).as_posix()
            try:
                raw = path.read_bytes()
            except OSError:
                continue
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                # 老工程可能不是 UTF-8。不猜，跳过并留给 probe 报。
                continue
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
        """
        root = Path(root).absolute()
        report = ApplyReport()
        if out is not None:
            report.notes.append(
                "Ren'Py 支持原地汉化，没收 --out；要保留原样请先自己备份整个目录。"
            )

        entries = self.collect(root)
        if not entries:
            report.notes.append("没有文本单元")
            return report

        by_file: dict[str, list[Entry]] = {}
        for entry in entries:
            by_file.setdefault(entry.rel_path, []).append(entry)

        log = UndoLog.open(root, engine=self.name)
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
            report.notes.append("一条都没写回去，没有动任何文件")
            return report

        # 先备份再写。备份和记录都在写第一个字节之前完成 ——
        # 中途崩了也还能 revert 回来。
        try:
            for path in pending:
                log.will_modify(path.relative_to(root).as_posix())
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

    kind: "say"（对话/旁白）/ "menu"（选项）/ None（不翻）
    """
    if not line.strip() or _COMMENT.match(line):
        return None, ""
    if _NOT_TEXT.match(line):
        # `$ _( "x" )` 这种包在可翻译函数里的还是该翻，单独放过
        if "$" in line:
            match = _TL_STRING.search(line)
            if match:
                return "say", _unescape(match.group("text"))
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
    match = _SAY.match(raw)
    if not match:
        raise ValueError("这一行不是可识别的 say 语句")
    who = (match.group("who") or "").rstrip()
    prefix = f"{who} " if who else ""
    return f'{indent}{prefix}"{translated}"'


def _unescape(text: str) -> str:
    """Ren'Py 里 `\\"` 是转义引号，翻的时候要还原成真引号给模型看。"""
    return text.replace('\\"', '"').replace("\\'", "'")


_TAG = re.compile(r"\{/?[a-zA-Z]+[^{}]*\}|\[[^\]]+\]")
_ESCAPE = re.compile(r"\\.")


def check_tags(source: str, translated: str) -> str:
    """译文里的 {格式标签} 和 [变量] 必须原样保留，否则游戏里会缺字或报错。

    数量对不上就拒绝写回 —— 宁可这条没翻，也不要让玩家看到 `{b}` 裸露。
    """
    src = sorted(_TAG.findall(source))
    dst = sorted(_TAG.findall(translated))
    if src != dst:
        return f"格式标签/变量被改动（原文 {src}，译文 {dst}）"
    src_esc = len(_ESCAPE.findall(source))
    dst_esc = len(_ESCAPE.findall(translated))
    if src_esc != dst_esc:
        return f"转义字符数量不一致（原文 {src_esc}，译文 {dst_esc}）"
    return ""
