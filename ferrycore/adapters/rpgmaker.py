"""RPG Maker MV / MZ 适配器 —— 明文 JSON，UTF-8，无编码问题。

跟 Ren'Py 同属「最容易」那一档：文本就是 `data/*.json` 里的字符串，
改完引擎直接读，不涉及任何显示层。

难点不在编码，在**找对字段**。RM 的数据文件是深度嵌套的：

    Map001.json
      events[i].pages[p].list[l]        ← 事件指令表
        {code: 401, parameters: ["セリフ"]}   ← 401 = 显示文本
        {code: 102, parameters: [["はい","いいえ"]]}  ← 102 = 显示选项

所以判据必须按「文件 + 结构位置 + 指令码」三重定位，不能按值猜。
猜错的后果是翻译引擎把 `@` 开头的控制符、队伍初始成员 ID 之类一起改掉。

**哪些不翻，是硬编码的白名单外**：`Animations/ Tilesets/` 里是 base64 音效数据，
`MapInfos` 只有名字，`plugins.js` 是 JS 代码。宁可少翻，不可翻坏。

MV 与 MZ 的目录不同（`www/data/` vs `data/`），但 JSON 结构几乎一样，
所以一个适配器覆盖两代。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .undo import UNDO_NAME, UndoLog

# 事件指令：真正给玩家看的文本只有这几种码
_CODE_TEXT = 401  # 显示文本（正文）
_CODE_SCROLL = 405  # 滚动文本
_CODE_CHOICES = 102  # 显示选项，parameters[0] 是字符串数组
_CODE_NAME = 320  # 改变角色名
_CODE_NICKNAME = 324  # 改变昵称
_CODE_PROFILE = 325  # 改变简介

# 数据库数组：每项在同一个位置放人类可读字段
_DB_FILES = {
    "Actors", "Classes", "Enemies", "Items", "Weapons", "Armors",
    "Skills", "States", "Troops",
}
_DB_FIELDS = ("name", "nickname", "profile", "description",
              "message1", "message2", "message3", "message4")
# 有事件指令表的文件
_EVENT_FILES = {"CommonEvents", "Troops"}
_MAP_FILE = re.compile(r"^Map\d{3}\.json$", re.IGNORECASE)

_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")


class RpgMakerAdapter(Adapter):
    name = "rpgmaker"
    priority = 25

    def __init__(self, include_samples: bool = False) -> None:
        self.include_samples = include_samples

    # ---- 定位数据目录 ----

    def _data_dir(self, root: Path) -> Path | None:
        """MV 在 `www/data/`，MZ 在 `data/`。也接受直接传数据目录。"""
        root = Path(root).absolute()
        for candidate in (root / "data", root / "www" / "data", root):
            if not candidate.is_dir():
                continue
            if (candidate / "System.json").is_file():
                return candidate
            if any(_MAP_FILE.match(p.name) for p in candidate.glob("*.json")):
                return candidate
        return None

    def _project_root(self, root: Path) -> Path:
        """相对路径的锚点。数据目录是 `www/data` 时，Entry 的 rel_path 从项目根算。"""
        root = Path(root).absolute()
        if (root / "www" / "data").is_dir():
            return root
        if root.name == "data" and (root / "System.json").is_file():
            return root.parent
        return root

    def _files(self, root: Path) -> list[Path]:
        data = self._data_dir(root)
        if data is None:
            return []
        return sorted(p for p in data.glob("*.json") if p.is_file())

    # ---- 探测 ----

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root)
        data = self._data_dir(root)
        if data is None:
            return ProbeReport(engine=self.name, root=root,
                               notes=["没找到 data/System.json 或 MapNNN.json，不是 RM MV/MZ 工程"])
        entries = self.collect(root)
        report = ProbeReport(
            engine="RPG Maker MV/MZ",
            root=root,
            readable=True,
            writable=True,
            verifiable=False,
            unit_count=len(entries),
            container="folder",
            text_format=".json",
            unicode_engine=True,
            in_place=True,
        )
        files = self._files(root)
        maps = sum(1 for p in files if _MAP_FILE.match(p.name))
        report.notes.append(f"数据目录 {data.name}/：{len(files)} 个 json，其中 {maps} 个地图。")
        system = data / "System.json"
        if system.is_file():
            try:
                sysdata = json.loads(system.read_text(encoding="utf-8-sig"))
                title = sysdata.get("gameTitle") or ""
                if title:
                    report.notes.append(f"游戏标题：{title}")
            except (OSError, ValueError):
                pass
        report.notes.append(
            "只翻事件指令里的 401/405/102/320/324/325 和数据库的 name/description，"
            "脚本行（$gameVariables 之类）一律不碰。"
        )
        plugins = list((data.parent).glob("plugins.js")) or list((data.parent.parent).glob("plugins.js"))
        if plugins:
            report.notes.append("插件配置 plugins.js 里的文本没做（那是 JS，改坏风险高）。")
        return report

    # ---- 抽取 ----

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).absolute()
        data = self._data_dir(root)
        if data is None:
            return []
        anchor = self._project_root(root)
        entries: list[Entry] = []
        for path in self._files(root):
            try:
                raw = path.read_text(encoding="utf-8-sig")
                payload = json.loads(raw)
            except (OSError, ValueError):
                continue
            try:
                rel = path.relative_to(anchor).as_posix()
            except ValueError:
                rel = path.name
            for pointer, text, kind in iter_texts(path.name, payload):
                entries.append(
                    Entry(
                        modid=root.name,
                        key=f"{rel}#{pointer}",
                        english=text,
                        source="game",
                        kind=kind,
                        rel_path=rel,
                        json_pointer=pointer,
                    )
                )
        return entries

    # ---- 回写 ----

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        """原地改 json。RM 工程带几百 MB 的 img/audio，复制一份没意义。"""
        root = Path(root).absolute()
        report = ApplyReport()
        if out is not None:
            report.notes.append("RM 支持原地汉化，没收 --out；要保留原样请先自己备份。")

        entries = self.collect(root)
        if not entries:
            report.notes.append("没有文本单元")
            return report

        anchor = self._project_root(root)
        by_file: dict[str, list[Entry]] = {}
        for entry in entries:
            by_file.setdefault(entry.rel_path, []).append(entry)

        log = UndoLog.open(root, engine=self.name)
        pending: dict[Path, dict] = {}

        for rel_path, items in by_file.items():
            path = anchor / rel_path
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                report.notes.append(f"{rel_path}：读不了（{exc}）")
                continue
            changed = False
            for entry in items:
                translated = translations.get(entry.key)
                if translated is None:
                    translated = translations.get(entry.english)
                if translated is None or not translated.strip():
                    report.missed += 1
                    continue
                if '"' in translated or "\\" in translated:
                    # RM 的文本字段会被塞进 JSON 字符串，引号/反斜杠要在翻译层就清掉；
                    # 到了这里直接拒绝，比事后在游戏里看到 `\"` 好。
                    report.conflicts.append(f"{entry.key}：译文含直引号或反斜杠")
                    continue
                try:
                    set_pointer(payload, entry.json_pointer, translated)
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    report.conflicts.append(f"{entry.key}：{exc}")
                    continue
                changed = True
                report.applied += 1
            if changed:
                pending[path] = payload

        if not report.applied:
            report.notes.append("一条都没写回去，没有动任何文件")
            return report

        try:
            for path in pending:
                log.will_modify(path.relative_to(anchor).as_posix())
        except OSError as exc:
            report.conflicts.append(f"撤销记录写不出来，放弃回写：{exc}")
            return report

        for path, payload in pending.items():
            rel = path.relative_to(anchor).as_posix()
            try:
                # RM 读的是紧凑 JSON；换成缩进版会让文件暴涨几十倍，
                # 而且 MV/MZ 的加载器虽然能读，diff 会完全不可用。
                text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
                path.write_text(text, encoding="utf-8")
            except OSError as exc:
                report.notes.append(f"{rel}：写失败（{exc}）")
                continue
            log.did_write(rel)
        log.finish()

        report.out_path = root
        report.in_place = True
        report.undo_path = root / UNDO_NAME
        report.notes.append("已原地改 data/*.json；`revert` 可还原。")
        return report


# ---- JSON Pointer 与文本判定（纯函数，单独测） ----


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _unescape_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def parse_pointer(pointer: str) -> list:
    """`/events/1/pages/0/list/3/parameters/0` -> 令牌列表。数字留给调用方判断。"""
    if not pointer.startswith("/"):
        raise ValueError(f"不是合法的 JSON Pointer：{pointer!r}")
    return [_unescape_token(p) for p in pointer.split("/")[1:]]


def set_pointer(root, pointer: str, value) -> None:
    tokens = parse_pointer(pointer)
    node = root
    for token in tokens[:-1]:
        node = _step(node, token)
    last = tokens[-1]
    if isinstance(node, list):
        node[int(last)] = value
    elif isinstance(node, dict):
        if last not in node:
            raise KeyError(f"{last} 不存在")
        node[last] = value
    else:
        raise TypeError(f"不能往 {type(node).__name__} 上赋值")


def _step(node, token: str):
    if isinstance(node, list):
        return node[int(token)]
    if isinstance(node, dict):
        if token not in node:
            raise KeyError(f"{token} 不存在")
        return node[token]
    raise TypeError(f"{type(node).__name__} 不能再往下走")


def _worth(text) -> bool:
    """空串、纯数字、纯 ASCII 标点不值得翻。

    特别注意：`\\N[1]` 这种是 RM 的转义（引用角色名），单独成条时不该翻 ——
    换掉就等于把变量名当天书翻了。
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if _CJK.search(stripped):
        return True
    # 全是转义/控制符/数字的，跳过
    if re.fullmatch(r"(\\[A-Za-z]+\[\d+\]|[\s\d\-+*/%=<>!&|.,:;()\[\]{}'\"#@$^~`]*)+", stripped):
        return False
    return len(stripped) > 1


def _p(base: str, *tokens) -> str:
    return base + "".join("/" + _escape(str(t)) for t in tokens)


def iter_texts(filename: str, payload):
    """产出 (pointer, text, kind)。filename 决定走哪套规则。"""
    stem = filename[:-5] if filename.lower().endswith(".json") else filename

    if stem == "System":
        yield from _system_texts(payload)
        return
    if stem == "MapInfos":
        yield from _mapinfo_texts(payload)
        return
    if _MAP_FILE.match(filename):
        yield from _event_texts(payload)
        return
    if stem in _EVENT_FILES:
        # CommonEvents 是数组；Troops 的指令在 pages[].list[]
        yield from _event_texts(payload)
        return
    if stem in _DB_FILES:
        yield from _db_texts(payload)
        return


def _system_texts(payload) -> list:
    out = []
    if not isinstance(payload, dict):
        return out
    title = payload.get("gameTitle")
    if _worth(title):
        out.append(("/gameTitle", title, "title"))
    terms = payload.get("terms")
    if isinstance(terms, dict):
        for group, values in terms.items():
            if isinstance(values, list):
                for index, text in enumerate(values):
                    if _worth(text):
                        out.append((_p("", "terms", group, index), text, "term"))
            elif isinstance(values, dict):
                for key, text in values.items():
                    if _worth(text):
                        out.append((_p("", "terms", group, key), text, "term"))
    return out


def _mapinfo_texts(payload) -> list:
    out = []
    if not isinstance(payload, list):
        return out
    for index, item in enumerate(payload):
        if isinstance(item, dict) and _worth(item.get("name")):
            out.append((_p("", index, "name"), item["name"], "mapname"))
    return out


def _db_texts(payload) -> list:
    out = []
    if not isinstance(payload, list):
        return out
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        for fname in _DB_FIELDS:
            text = item.get(fname)
            if _worth(text):
                out.append((_p("", index, fname), text, "db"))
    return out


def _event_texts(payload) -> list:
    """事件指令表。递归找 `list` 数组，只按 code 取值。"""
    out: list = []

    def visit(node, base: str):
        if isinstance(node, dict):
            instructions = node.get("list")
            if isinstance(instructions, list):
                for index, instruction in enumerate(instructions):
                    if not isinstance(instruction, dict):
                        continue
                    code = instruction.get("code")
                    params = instruction.get("parameters")
                    if not isinstance(params, list):
                        continue
                    where = _p(base, "list", index, "parameters")
                    if code in (_CODE_TEXT, _CODE_SCROLL) and len(params) >= 1:
                        if _worth(params[0]):
                            out.append((where + "/0", params[0], "dialogue"))
                    elif code in (_CODE_NAME, _CODE_NICKNAME, _CODE_PROFILE) and len(params) >= 1:
                        if _worth(params[0]):
                            out.append((where + "/0", params[0], "name"))
                    elif code == _CODE_CHOICES and params and isinstance(params[0], list):
                        for slot, text in enumerate(params[0]):
                            if _worth(text):
                                out.append((where + f"/0/{slot}", text, "choice"))
            for key, value in node.items():
                if key == "list":
                    continue
                visit(value, _p(base, key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, _p(base, index))

    visit(payload, "")
    return out
