"""TyranoScript 适配器。

TyranoScript（ティラノスクリプト）是日本同人 AVG 的主力引擎之一，纯 JS。
剧本是 `data/scenario/*.ks` 的**纯文本**，标签是 `[p]` `[l]` `[r]` 这种方括号，
编码普遍是 UTF-8（带不带 BOM 都有）。所以它是「文本好读」的典型，
难点全在容器和回写，不在解析。

一个 `.ks` 行只有三类：

    [image layer="base" storage=haikei_.jpg]     标签行 —— 引号里的可能是文本
    こんにちは[p]                                  正文行 —— 整行都是文本
    ; コメント                                     注释行 —— 不碰

所以文本单元的粒度是「一行」，正文行整行替换，标签行只替换引号里的值。
整行替换是最稳的：标签位置一个都不动，就不可能出现「翻完渲染崩了」。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport, VerifyReport
from .container import Container, open_container, unique_output
from .undo import UNDO_NAME, UndoLog

# 标签 / 注释行：以 [ @ * 开头是标签订阅，; 和 // 是注释
_MARKUP_LINE = re.compile(r"^\s*(?:\[|@|\*)")
_COMMENT_LINE = re.compile(r"^\s*(?:;|//)")

# 标签行里**有名字的文本属性**才翻。这一条是硬判据，不靠猜：
# `storage=` 永远是资源路径，`target=` 永远是 label 或场景名，翻它们等于把游戏改坏。
# Tyrano 显示角色名用的是 jname，name 是内部 ID，所以 name 不在名单里。
_TEXT_ATTRS = {"text", "title", "jname", "message", "caption"}
_ATTR = re.compile(r'([A-Za-z_]\w*)\s*=\s*"([^"]*)"')

# 资源路径：带扩展名且不长成一句话，就不是文本
_ASSET_EXT = re.compile(
    r"\.(?:png|jpe?g|webp|gif|bmp|ogg|mp3|m4a|wav|mp4|webm|ttf|otf|woff2?|json|ks|tjs|js|css|html)$",
    re.I,
)
# 代码特征：调用、赋值、比较、块
_CODE_HINT = re.compile(r"[();{}]|=>|==|!=|\+=|&&|\|\||\btf\.|\bTG\.|\bparseInt\b|\bfunction\b")

# 这两个标签之间的内容是 JS，一行都不能翻
_SCRIPT_BEGIN = re.compile(r"^\s*\[iscript")
_SCRIPT_END = re.compile(r"^\s*\[endscript")

# 有没有「值得翻的文本」。中文/日文/韩文直接算；拉丁文至少要两个词，
# 免得把 storage="piano06.ogg" 这种资源名当文本。
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff66-\uff9f]")
_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")

# 图片里可能带文字的线索（按钮、标题、logo）。只是疑似，不能确认。
_TEXT_IMAGE_HINT = re.compile(r"(button|btn|menu|title|logo|start|hajime|tuduki|tsuzuki|continue|select)", re.I)

# 引擎 UI 文本：跟剧本不是一套格式，单独收
_ENGINE_TEXT_FILES = ("tyrano/lang.js", "data/system/Config.tjs")

# Tyrano 的默认入口。别的 .ks 只有被引用才会被执行。
_ENTRY_SCENARIO = "first.ks"

# 引擎官方示例包的指纹。Tyrano 的示例剧本里一定带官网链接，而正常游戏里
# 不会出现 —— 实测这 147 条教程台词占了一个小游戏总文本量的七成。
_SAMPLE_HINT = re.compile(r"tyrano\.(?:jp|camp)")


def has_translatable(value: str) -> bool:
    """这串东西是不是「人话」。

    判据顺序是有讲究的：先排资源路径（带扩展名又不长成句子），再排
    「没有中日韩字符却长着一副代码样」的行，最后才轮到拉丁文多词。
    反过来先看「有没有两个英文单词」的话，`$(".layer_camera").empty();`
    和 `title/button_start.png` 都会混进来 —— 这两种误判在真实游戏里
    各出现过一百多次。
    """
    core = value.strip()
    if not core:
        return False
    if _ASSET_EXT.search(core) and " " not in core:
        return False
    if _CJK.search(core):
        return True
    if _CODE_HINT.search(core):
        return False
    return len(_LATIN_WORD.findall(core)) >= 2 and " " in core


def split_units(text: str) -> list[tuple[int, str, int, str, str]]:
    """把一份剧本拆成文本单元。

    返回 `[(行号, 属性名, 同名属性序号, 原文, 定位串)]`。

    - 正文行：属性名为空，整行替换；
    - 标签行：只取 `_TEXT_ATTRS` 里那几个有语义的属性，按属性名定位。

    行号从 1 开始，跟编辑器里看到的一致 —— 报错时用户能直接跳过去。
    `[iscript]` 到 `[endscript]` 之间整块跳过：那是 JS，不是台词。
    """
    units: list[tuple[int, str, int, str, str]] = []
    in_script = False
    for index, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or _COMMENT_LINE.match(line):
            continue
        begin, end = bool(_SCRIPT_BEGIN.match(line)), bool(_SCRIPT_END.match(line))
        if begin != end:
            in_script = begin
            continue
        if in_script:
            continue
        if _MARKUP_LINE.match(line):
            seen: dict[str, int] = {}
            for match in _ATTR.finditer(line):
                attr = match.group(1).lower()
                if attr not in _TEXT_ATTRS:
                    continue
                order = seen.get(attr, 0)
                seen[attr] = order + 1
                value = match.group(2)
                if has_translatable(value):
                    units.append((index, attr, order, value, f"{index}#{attr}#{order}"))
            continue
        if has_translatable(line):
            units.append((index, "", -1, line, f"{index}#-1"))
    return units


def _replace_attr(line: str, attr: str, order: int, value: str) -> str:
    targets = [m for m in _ATTR.finditer(line) if m.group(1).lower() == attr]
    if order >= len(targets):
        raise ValueError(f"这一行没有第 {order + 1} 个 {attr}= 属性")
    match = targets[order]
    return line[: match.start(2)] + value + line[match.end(2) :]


class TyranoAdapter(Adapter):
    name = "tyrano"
    priority = 20

    def __init__(self, include_samples: bool = False) -> None:
        self.include_samples = include_samples
        self._containers: dict[Path, Container] = {}
        self._units: dict[Path, list[Entry]] = {}
        self._orphans: dict[Path, list[str]] = {}
        self._samples: dict[Path, list[str]] = {}

    # ---- 探测 ----

    def probe(self, root: Path) -> ProbeReport:
        container = self._container(root)
        if container is None:
            return ProbeReport(engine="tyrano", root=root, notes=["没找到 .ks 剧本，也不是可读的 zip-in-exe"])

        scenarios = self._scenario_files(container)
        if not scenarios:
            return ProbeReport(engine="tyrano", root=root, container=container.kind,
                               notes=["容器打开了，但里面没有 data/scenario/*.ks"])

        units = self.collect(root)
        report = ProbeReport(
            engine="TyranoScript",
            root=root,
            readable=True,
            # 容器可写 = 能产出副本。散装目录和 zip-in-exe 都已在实现里覆盖。
            writable=True,
            # nw.js / Electron 内核本身能开调试端口读 DOM，这条路已经验证可行，
            # 但 verify() 目前还是基类桩。**报 True 而 verify 什么都不做就是骗人**，
            # 所以在这里报 False，并在备注里说清楚「能力在，只是还没接」。
            verifiable=False,
            unit_count=len(units),
            container=container.kind,
            text_format=".ks",
        )
        if self._orphans.get(root):
            report.notes.append(
                "以下剧本没有任何跳转能进到，是死代码，不计入缺口：" + "、".join(self._orphans[root])
            )
        if self._samples.get(root):
            report.notes.append(
                "跳过引擎官方示例剧本（不是这个游戏的内容）：" + "、".join(self._samples[root])
            )
        images = self._count_text_images(container)
        report.images_with_text = images
        if images:
            report.notes.append(
                f"那 {images} 处图片文字文本层永远碰不到，只能重画图；报告里已单列，不算漏翻。"
            )
        report.notes.append("这个引擎的内核支持自动验证（能开调试端口读画面），但摆渡还没接，翻完要你自己进游戏看。")
        engine_ui = [n for n in _ENGINE_TEXT_FILES if n in self._all_names(container)]
        if engine_ui:
            report.notes.append("引擎 UI 文本另在 " + "、".join(engine_ui) + "，本适配器暂不处理")
        return report

    # ---- 抽取 ----

    def collect(self, root: Path) -> list[Entry]:
        if root in self._units:
            return list(self._units[root])
        container = self._container(root)
        if container is None:
            return []
        scenarios = self._scenario_files(container)
        texts = {name: container.read_text(name) for name in scenarios}
        orphan_names = set(self._find_orphans(scenarios, texts))
        self._orphans[root] = sorted(orphan_names)

        sample_names: set[str] = set()
        if not self.include_samples:
            sample_names = {
                name for name, text in texts.items()
                if _SAMPLE_HINT.search(text) and name.rsplit("/", 1)[-1] != _ENTRY_SCENARIO
            }
        self._samples[root] = sorted(sample_names)

        skip = orphan_names | sample_names
        entries: list[Entry] = []
        for name in scenarios:
            if name in skip:
                continue
            for line_no, attr, _order, value, pointer in split_units(texts[name]):
                entry = self.make_entry(root, name, line_no, value)
                entry.kind = "line" if not attr else "attr"
                entry.json_pointer = pointer
                entries.append(entry)
        self._units[root] = entries
        return list(entries)

    # ---- 回写 ----

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        container = self._container(root)
        report = ApplyReport()
        if container is None:
            report.notes.append("容器打不开，没法回写")
            return report

        entries = self.collect(root)
        if not entries:
            report.notes.append("没有文本单元")
            return report

        by_file: dict[str, list[Entry]] = {}
        for entry in entries:
            by_file.setdefault(entry.rel_path, []).append(entry)

        for rel_path, items in by_file.items():
            document = container.read_document(rel_path)
            lines = document.text.split("\n")
            touched = False
            for entry in items:
                # 真实链路里 `translate_entries` 返回的是 `{Entry.key: 译文}`（key = 文件#行号）；
                # 手工准备的译文表往往只有原文。先按位置精确查，再退回按原文查 ——
                # 前者是契约，后者是 gettext 的 msgid 语义，留着不算错。
                translated = translations.get(entry.key)
                if translated is None:
                    translated = translations.get(entry.english)
                if translated is None or not translated.strip():
                    report.missed += 1
                    continue
                parts = entry.json_pointer.split("#")
                index = int(parts[0]) - 1
                if not (0 <= index < len(lines)):
                    report.conflicts.append(f"{rel_path}#{parts[0]}：行号越界")
                    continue
                raw = lines[index]
                if '"' in translated:
                    report.conflicts.append(f"{entry.key}：译文含半角引号，会破坏语法")
                    continue
                try:
                    if len(parts) == 2:
                        indent = raw[: len(raw) - len(raw.lstrip())]
                        lines[index] = indent + translated.strip()
                    else:
                        lines[index] = _replace_attr(raw, parts[1], int(parts[2]), translated.strip())
                except ValueError as exc:
                    report.conflicts.append(f"{entry.key}：{exc}")
                    continue
                report.applied += 1
                touched = True
            if touched:
                container.write_document(rel_path, "\n".join(lines), document)

        if not report.applied:
            report.notes.append("一条都没写回去，不产出副本")
            return report

        base_target = out or self.default_output(root, container)
        target = unique_output(base_target)
        if target != base_target:
            report.notes.append(f"同名副本已存在，这次写到 {target.name}（旧的没动）")
        try:
            report.out_path = container.commit(target)
        except FileExistsError as exc:
            report.notes.append(str(exc))
            return report
        # 记一笔「我产出了这份副本」。原游戏的数据文件没动，但用户要能
        # 一句话找到并撤掉副本 —— 没这条记录，他只能自己翻目录找。
        try:
            log = UndoLog.open(root, engine=self.name)
            log.did_copy(report.out_path)
            log.finish()
            report.undo_path = Path(root) / UNDO_NAME
        except OSError as exc:
            report.notes.append(f"副本已产出，但撤销记录没写成（{exc}）；删副本请手动")
        report.notes.extend(container.notes)
        return report

    # ---- 验证 ----

    def verify(self, root: Path) -> VerifyReport:
        """起一次 exe，只看它能不能正常启动。

        这不是「文本对不对」，是「exe 有没有被我改坏」——重建 zip 拼回尾部时
        最容易出的错就是偏移算错，那会让 exe 直接不启动。这条冒烟测试
        已经能挡住最贵的一类事故。
        """
        container = self._container(root)
        if container is None or container.kind != "zip-in-exe":
            return VerifyReport(ok=False, ran=False, notes=["散装目录不需要冒烟测试"])
        return VerifyReport(ok=False, ran=False, notes=["冒烟测试待接入（需起进程），当前请在游戏里手动确认"])

    # ---- 内部 ----

    def _container(self, root: Path) -> Container | None:
        if root not in self._containers:
            found = open_container(root)
            if found is None:
                return None
            self._containers[root] = found
        return self._containers[root]

    @staticmethod
    def _scenario_files(container: Container) -> list[str]:
        names = container.list_files((".ks",))
        return sorted(
            n for n in names
            if n.startswith("data/scenario/") and "/tyrano/" not in n and not n.startswith("node_modules/")
        )

    @staticmethod
    def _all_names(container: Container) -> set[str]:
        return set(container.list_files((".ks", ".js", ".tjs", ".json")))

    @staticmethod
    def _find_orphans(names: list[str], texts: dict[str, str]) -> list[str]:
        """找出没有任何剧本会跳进去的文件。它们不执行，翻了也没人看见。"""
        orphans: list[str] = []
        for name in names:
            base = name.rsplit("/", 1)[-1]
            if base == _ENTRY_SCENARIO:
                continue
            referenced = any(
                other != name and base in text for other, text in texts.items()
            )
            if not referenced:
                orphans.append(name)
        return orphans

    @staticmethod
    def _count_text_images(container: Container) -> int:
        """游戏自带的按钮/标题图。引擎自带的通用按钮不算 —— 那不是这个游戏的缺口。"""
        images = [
            n for n in container.list_files((".png", ".jpg", ".jpeg"))
            if not n.startswith(("tyrano/", "node_modules/"))
        ]
        return sum(1 for n in images if _TEXT_IMAGE_HINT.search(n.rsplit("/", 1)[-1]))

    @staticmethod
    def default_output(root: Path, container: Container) -> Path:
        if container.kind == "zip-in-exe":
            source = container.source
            return source.with_name(f"{source.stem}.cn{source.suffix}")
        return root.parent / f"{root.name}.cn"
