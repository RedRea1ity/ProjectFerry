"""外挂模组（引擎适配器）的公共契约。

摆渡本体只认识 Minecraft：`scan_inputs()` 去找 lang 文件，`write_pack()` 出资源包。
别的引擎不共用这两步，但**共用整条翻译编排**——
`translate_entries(entries, settings)` 吃一个扁平 Entry 列表、吐 `{原文: 译文}`，
它本来就跟引擎无关。MC 只是恰好给它喂了最刁钻的一种数据源。

所以一个适配器只干三件事，把任何游戏压成「扁平 Entry 列表」再摊回去：

    probe(root)                  -> ProbeReport   先回答「值不值得开工」
    collect(root)                -> list[Entry]   抽出可翻译的文本单元
    apply(root, translations)    -> ApplyReport   写回并产出副本

**probe 里「可写」这一项比「可读」重要。** 取到文本是容易的那一半；
放不回去等于白翻，而且通常要到翻完三千行才发现。所以 `probe` 必须在
一个字都没翻之前回答 `writable`，答不出来就不许开工。

适配器**只依赖 `models.Entry`**，不 import `scan` / `packio` / `cli`。
这条是硬约束：加一个引擎只许往 `adapters/` 里加文件，不许改本体。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..models import Entry


@dataclass
class ProbeReport:
    """开工前的体检结果。这五项就是用户第一屏该看到的东西。"""

    engine: str = "unknown"
    root: Path | None = None
    # 能不能取到文本
    readable: bool = False
    # 能不能放回去（最关键的一项）
    writable: bool = False
    # 能不能自动验证（起游戏读 DOM 这类）
    verifiable: bool = False
    # 有多少个可翻译单元
    unit_count: int = 0
    # 文本层永远碰不到的图片文字（按钮、标题 logo）
    images_with_text: int = 0
    # 容器形态 / 文本格式，两个正交维度，分开报
    container: str = ""
    text_format: str = ""
    # 这台机器上必须先装好的外部工具（id 见 `ferrycore/deps.py`）。
    # 适配器自己最清楚要什么：HSP 要编译器编 DLL 代理，别的引擎可能什么都不需要。
    # 声明在这里而不是全局扫，是为了「只在真需要时打扰用户」。
    needs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """三个条件全过才允许开始翻译。"""
        return self.readable and self.writable and self.unit_count > 0

    def summary(self) -> str:
        if not self.readable:
            return f"{self.engine}：认不出文本在哪，不能翻"
        if not self.writable:
            return f"{self.engine}：能读 {self.unit_count} 条，但写不回去，翻了也白翻"
        if self.unit_count == 0:
            return f"{self.engine}：没找到可翻译的文本单元"
        parts = [f"{self.engine} · {self.container} / {self.text_format}",
                 f"{self.unit_count} 条待翻"]
        if self.images_with_text:
            parts.append(f"{self.images_with_text} 处图片文字翻不到")
        return "，".join(parts)


@dataclass
class ApplyReport:
    applied: int = 0
    missed: int = 0
    skipped: int = 0
    # 产出的副本路径（原游戏一个字节都不动）
    out_path: Path | None = None
    conflicts: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.applied > 0 and not self.conflicts


@dataclass
class VerifyReport:
    ok: bool = False
    ran: bool = False
    evidence: str = ""
    notes: list[str] = field(default_factory=list)


class Adapter:
    """适配器基类。子类必须给 `name`，并实现全部四个方法。

    不强制继承也行（鸭子类型即可），但继承能拿到默认的 `verify` 桩。
    """

    name = "unknown"
    # 数字小的先探测。认领越具体的排越前，通用兜底排最后。
    priority = 100

    def probe(self, root: Path) -> ProbeReport:
        raise NotImplementedError

    def collect(self, root: Path) -> list[Entry]:
        raise NotImplementedError

    def apply(self, root: Path, translations: dict[str, str], out: Path | None = None) -> ApplyReport:
        """把译文写回去，产出一个副本。

        `translations` 的键是 `Entry.key`（`文件#行号` 这种位置标识），不是原文 ——
        同一句话出现在多个位置时，位置才是锚点。实现里可以额外接受原文作键，
        那是给手工校对用的，不能反过来假定它。
        """
        raise NotImplementedError

    def verify(self, root: Path) -> VerifyReport:
        """默认不验证。能做自动验证的引擎（nw.js / Electron）覆盖它。"""
        return VerifyReport(ok=False, ran=False, notes=["该引擎没有自动验证通道"])

    # ---- 子类常用的小工具 ----

    @staticmethod
    def make_entry(root: Path, rel_path: str, line_no: int, text: str) -> Entry:
        """把一个文本单元包成 Entry。

        复用本体现成的 identity：`modid` 放游戏名，`key` 放 `文件#行号`，
        `english` 放原文整条。这样翻译层和缓存层一行都不用改。
        """
        return Entry(
            modid=root.name,
            key=f"{rel_path}#{line_no}",
            english=text,
            source="game",
            kind="line",
            rel_path=rel_path,
            json_pointer=str(line_no),
        )
