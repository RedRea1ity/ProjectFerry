"""撤销记录 —— 「汉化完能跑路」的实现凭据。

适配器有两种产出方式，撤销的形态完全不同：

    副本型  apply 把整个游戏复制一份再改 → revert 就是删掉那份副本
    原地型  apply 直接往游戏目录里写（HSP 放 DLL、明文 CSV 改字节）
            → revert 必须知道「加了哪些文件」「改过哪些文件、原内容是什么」

硬编码两套逻辑迟早会漏。所以统一成一份 `UNDO_NAME` 记录 + 一份备份目录，
`revert()` 只看记录做事，不认识任何引擎。

**顺序是有讲究的**：`apply` 必须在写第一个字节之**前**把记录落盘
（`will_add` / `will_modify` 立刻写备份并 save）。反过来，写到一半崩溃
就会留下一个「回不去」的状态 —— 那比不汉化更糟。

三条铁律，跟 `probe` 的 `writable` 同一性质：

1. **没有记录就拒绝**。不猜、不「清空目录试试」。
2. **还原后校验 md5**。对不上就报告失败，不假装成功。
3. **绝不删记录以外的任何东西**。副本目录、备份目录都要过路径校验。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

UNDO_NAME = ".ferry-undo.json"
BACKUP_DIR = ".ferry-backup"
# 记录格式变了就升这个数，旧记录不认 —— 宁可拒绝，不猜兼容。
UNDO_VERSION = 1


def file_digest(path: Path) -> str:
    """大文件要分块，3 GB 的游戏一次读进内存会炸。"""
    digest = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class Action:
    """一条可撤销的动作。"""

    kind: str  # added（我新建的）/ modified（我改过的）/ copy（我产出的副本）
    path: str  # 相对 root 的路径；copy 动作存绝对路径
    backup: str = ""  # 原内容备份，相对 root
    digest: str = ""  # 原文件 md5（added 记录的是我写进去之后的 md5）
    # 写完之后的 md5。added 用它判断「用户后来自己动过没」，动过就不删。
    written: str = ""

    def to_json(self) -> dict:
        data = {"kind": self.kind, "path": self.path, "digest": self.digest}
        if self.backup:
            data["backup"] = self.backup
        if self.written:
            data["written"] = self.written
        return data

    @classmethod
    def from_json(cls, data: dict) -> "Action":
        return cls(
            kind=data.get("kind", ""),
            path=data.get("path", ""),
            backup=data.get("backup", ""),
            digest=data.get("digest", ""),
            written=data.get("written", ""),
        )


@dataclass
class RevertReport:
    restored: int = 0
    removed: int = 0
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and (self.restored > 0 or self.removed > 0)

    def summary(self) -> str:
        if not (self.restored or self.removed or self.failed):
            return "什么都没做：" + (self.notes[0] if self.notes else "记录是空的")
        parts = []
        if self.restored:
            parts.append(f"还原 {self.restored} 个文件")
        if self.removed:
            parts.append(f"移除 {self.removed} 个文件/目录")
        if self.failed:
            parts.append(f"{len(self.failed)} 项失败")
        return "，".join(parts)


class UndoLog:
    """一次汉化操作的撤销记录。

    用法（顺序不能颠倒）：

        log = UndoLog(root, engine="tyrano")
        log.will_modify("eve/sn1.txt")   # 立刻备份，立刻改变可撤
        ...写文件...
        log.did_write("eve/sn1.txt")     # 记最终 md5
        log.finish()                     # 落盘

    只在内存里攒到最后才写盘的实现是不合格的：中途崩溃就全丢了。
    """

    def __init__(self, root: Path, engine: str = ""):
        self.root = Path(root).resolve()
        self.engine = engine
        self.actions: list[Action] = []
        self.created = datetime.now().isoformat(timespec="seconds")
        self._saved = False

    @classmethod
    def open(cls, root: Path, engine: str = "") -> "UndoLog":
        """有旧记录就接着写，没有就新建。

        必须能续写：同一个游戏汉化两次会产出 `cn2`、`cn3` 两份副本，
        后一次的记录不能把前一次的盖掉 —— 那样第一份副本就撤不回来了。
        """
        log = cls(root, engine=engine)
        data = load(root)
        if data:
            log.actions = [Action.from_json(a) for a in data["actions"]]
            log.created = data.get("created", log.created)
            log.engine = data.get("engine") or engine
        return log

    # ---- 登记 ----

    def will_add(self, rel_path: str) -> None:
        """我要新建这个文件。撤销时删掉它。"""
        self._append(Action(kind="added", path=_norm(rel_path)))

    def will_modify(self, rel_path: str) -> None:
        """我要改这个文件。**立刻**备份原内容并落盘，改到一半崩了也能还原。"""
        rel = _norm(rel_path)
        source = self.root / rel
        if not source.exists():
            raise FileNotFoundError(f"要改的文件不存在：{rel}")
        backup_rel = f"{BACKUP_DIR}/{rel}"
        backup_abs = self.root / backup_rel
        backup_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, backup_abs)
        self._append(Action(kind="modified", path=rel, backup=backup_rel,
                            digest=file_digest(source)))
        # 备份完就地落盘 —— 这是「写第一个字节前」的最后一道保险
        self.save()

    def did_copy(self, out_path: Path) -> None:
        """我产出了一份副本。撤销时删掉这个目录/文件。"""
        target = Path(out_path).resolve()
        if target == self.root:
            raise ValueError("副本路径不能等于游戏目录本身")
        self._append(Action(kind="copy", path=str(target)))

    # ---- 落盘 ----

    def did_write(self, rel_path: str) -> None:
        """记下我写完之后的内容 md5，供 revert 判断文件是否被用户动过。"""
        target = self.root / _norm(rel_path)
        if not target.exists():
            return
        digest = file_digest(target)
        for action in self.actions:
            if action.path == _norm(rel_path) and action.kind in ("added", "modified"):
                action.written = digest
                action.digest = digest if action.kind == "added" else action.digest
                return

    def save(self) -> None:
        payload = {
            "version": UNDO_VERSION,
            "engine": self.engine,
            "created": self.created,
            "root": str(self.root),
            "actions": [a.to_json() for a in self.actions],
        }
        path = self.root / UNDO_NAME
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(path)  # 原子替换，避免写一半留下坏 JSON
        self._saved = True

    def finish(self) -> None:
        self.save()

    def _append(self, action: Action) -> None:
        self.actions.append(action)


def _norm(rel_path: str) -> str:
    """相对路径归一化：反斜杠转正斜杠，去掉开头的 `./`。

    这里**不能**写 `lstrip("./")` —— 它剥的是字符集合，不是前缀。
    `.ferry-backup/x.csv` 会被剥成 `ferry-backup/x.csv`，于是备份路径
    和真实路径混为一谈，报错时还会显示一个根本不存在的文件。
    """
    s = str(rel_path).replace("\\", "/")
    while s.startswith("./"):
        s = s[2:]
    return s.lstrip("/")


def load(root: Path) -> dict | None:
    """读记录。没有、读不动、版本不对 —— 一律返回 None，调用方据此拒绝。"""
    path = Path(root) / UNDO_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("version") != UNDO_VERSION:
        return None
    if not isinstance(data.get("actions"), list):
        return None
    return data


def revert(root: Path, dry_run: bool = False) -> RevertReport:
    """照记录还原。不认识的引擎也能用 —— 它只看记录，不看游戏。"""
    root = Path(root).resolve()
    report = RevertReport()
    data = load(root)
    if data is None:
        record = root / UNDO_NAME
        if record.exists():
            report.failed.append(str(record))
            report.notes.append(
                f"撤销记录存在但读不懂（版本不符或文件损坏）：{record}。不猜，不动任何文件。"
            )
        else:
            report.notes.append(
                "这个目录没有汉化记录，没法撤销。摆渡只撤自己做过的事。"
            )
        return report

    for raw in data["actions"]:
        action = Action.from_json(raw)
        if action.kind == "copy":
            _revert_copy(Path(action.path), root, report, dry_run)
        elif action.kind == "added":
            _revert_added(root / action.path, action, report, dry_run)
        elif action.kind == "modified":
            _revert_modified(root, action, report, dry_run)
        else:
            report.failed.append(f"{action.path}：认不出的动作 {action.kind!r}")

    if dry_run:
        report.notes.append("（预演：以上都没真的执行）")
        return report

    # 全部成功才清场。有失败就留着记录，让用户能再试一次 ——
    # 先删记录再发现还原失败，等于把唯一的凭据也弄丢了。
    if report.failed:
        report.notes.append("有项目没还原成功，记录已保留，修好问题可以再撤一次。")
        return report

    backup_root = root / BACKUP_DIR
    if backup_root.exists():
        _rmtree(backup_root)
    try:
        (root / UNDO_NAME).unlink()
    except OSError as exc:
        report.notes.append(f"记录没删掉（不影响游戏）：{exc}")
    return report


def _revert_copy(target: Path, root: Path, report: RevertReport, dry_run: bool) -> None:
    if not target.exists():
        report.notes.append(f"副本已不在了，跳过：{target}")
        return
    # 路径校验：不许删游戏目录本身，也不许删到游戏目录外面去。
    # 副本是「游戏的兄弟目录」或「游戏目录里的文件」，两种都合法。
    if target == root:
        report.failed.append(f"{target}：指向游戏目录本身，拒绝删除")
        return
    try:
        target.relative_to(root)
        inside = True
    except ValueError:
        inside = target.parent == root.parent or root in target.parents
    if not inside and target.parent != root.parent:
        report.failed.append(f"{target}：不在游戏目录或其同级，拒绝删除")
        return
    if dry_run:
        report.removed += 1
        return
    try:
        if target.is_dir():
            _rmtree(target)
        else:
            target.unlink()
        report.removed += 1
    except OSError as exc:
        report.failed.append(f"{target}：{exc}")


def _revert_added(target: Path, action: Action, report: RevertReport, dry_run: bool) -> None:
    if not target.exists():
        report.notes.append(f"已不在，跳过：{action.path}")
        return
    # 用户后来自己改过这个文件？那就不删 —— 那已经是他的东西了。
    if action.written:
        try:
            current = file_digest(target)
        except OSError as exc:
            report.failed.append(f"{action.path}：读不到，{exc}")
            return
        if current != action.written:
            report.failed.append(
                f"{action.path}：我写进去之后又被人改过，不敢删。要删请自己动手。"
            )
            return
    if dry_run:
        report.removed += 1
        return
    try:
        target.unlink()
        report.removed += 1
    except OSError as exc:
        report.failed.append(f"{action.path}：{exc}")


def _revert_modified(root: Path, action: Action, report: RevertReport, dry_run: bool) -> None:
    target = root / action.path
    backup = root / action.backup if action.backup else None
    if backup is None or not backup.exists():
        report.failed.append(f"{action.path}：备份不见了，没法还原")
        return
    if dry_run:
        report.restored += 1
        return
    try:
        backup_digest = file_digest(backup)
        if action.digest and backup_digest != action.digest:
            report.failed.append(
                f"{action.path}：备份本身和记录对不上（备份被改过？），不敢覆盖"
            )
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, target)
        if action.digest and file_digest(target) != action.digest:
            report.failed.append(f"{action.path}：还原后 md5 对不上")
            return
        report.restored += 1
    except OSError as exc:
        report.failed.append(f"{action.path}：{exc}")


def _rmtree(path: Path) -> None:
    """只读文件（打包出来的 exe 常见）会让 rmtree 半途失败，先清标记。"""

    def _on_error(func, target, _exc_info):
        try:
            Path(target).chmod(0o700)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onerror=_on_error)
