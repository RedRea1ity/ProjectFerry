"""纯净汉化补丁包导出器（可分发、零原版大文件、自带安全安装/还原脚本）。

与运行时 Hook 工具「关掉软件恢复外文、无法分享成果」不同，摆渡基于静态补丁与
`.ferry-undo.json` 撤销账本，天然知道本次汉化到底「新增了哪些补丁」「改了哪些小文本」。

本模块把这些变更打成一份轻量 ZIP 补丁包：
- 只收补丁/差异文件（如 `~mods/ProjectFerry_zh_CN_P.pak`、`patch.xp3`、`www/data/*.json`、
  `game/*.rpy`、`BepInEx/Translation/...`、中文字体回退文件等）；
- 默认拦截大型二进制与镜像（`.exe`/`.iso`/`.apk`/`.pak` 原包等），绝不顺手分发受版权保护的原版游戏本体；
- 若某格式只能产出整包副本（如 `.cn.exe` / `.cn.iso`），则退回导出「0 Token 文本协作包」
  （含 `ferry_tl.json` / `ferry_tl.tsv` / `ferry_worldbook.json`），让他人用自己的正版游戏 1 秒重打包；
- 自动附带 `manifest.json`（SHA-256 校验）、`README_安装说明.txt`、`一键安装补丁.bat` 与 `一键卸载还原.bat`。
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .adapters.rgssad import RgssadArchive, RgssadError
from .adapters.undo import BACKUP_DIR, UNDO_NAME, Action, load as load_undo

_CN_COPY_SUFFIX_RE = re.compile(r"\.cn\d*$", re.IGNORECASE)

PATCH_MANIFEST_NAME = "manifest.json"
PATCH_FORMAT = "ferry-patch-pack"
PATCH_VERSION = 1

DEFAULT_MAX_FILE_BYTES = 32 * 1024 * 1024  # 32 MB 单文件保护线

# 默认绝不直接塞进分享补丁包的原版整包/镜像扩展名（除非属于明确新增的小型补丁产物）
_BLOCKED_BINARY_SUFFIXES = {
    ".exe",
    ".iso",
    ".cso",
    ".nds",
    ".apk",
    ".ipa",
    ".vpk",
    ".hdi",
    ".fdi",
    ".nhd",
    ".swf",
    ".sis",
    ".sisx",
    ".cod",
    ".bar",
}

# 明确属于「补丁容器」的新增文件，即使体量略大或后缀特殊也允许收录
_EXPLICIT_PATCH_NAMES = {
    "projectferry_zh_cn_p.pak",
    "patch.xp3",
    "ferry_cjk.ttf",
    "zz_ferry_cjk_font.rpy",
}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_explicit_patch(rel_path: str) -> bool:
    low = rel_path.replace("\\", "/").lower()
    name = low.rsplit("/", 1)[-1]
    if name in _EXPLICIT_PATCH_NAMES:
        return True
    if name.endswith("_p.pak") and ("~mods/" in low or "/paks/" in low):
        return True
    if name.startswith("patch") and name.endswith(".xp3"):
        return True
    return False


def _should_skip_file(rel_path: str, path: Path, kind: str, max_file_bytes: int) -> str | None:
    if not path.is_file():
        return "文件已不存在"
    rel_norm = rel_path.replace("\\", "/").lstrip("/")
    parts = rel_norm.split("/")
    if any(p in (BACKUP_DIR, ".ferry-patch-backup", "__pycache__") for p in parts):
        return "内部备份/缓存文件"
    if parts[-1] in (UNDO_NAME, "ferry_tl.json", "ferry_tl.tsv", "ferry_worldbook.json"):
        return "工程元数据（将单独归入 ferry_source/）"
    suffix = path.suffix.lower()
    if suffix in _BLOCKED_BINARY_SUFFIXES and not _is_explicit_patch(rel_norm):
        return f"已拦截二进制/镜像文件（{suffix}），避免分发原版程序"
    size = path.stat().st_size
    if size > max_file_bytes and not _is_explicit_patch(rel_norm):
        mb = size / (1024 * 1024)
        limit_mb = max_file_bytes / (1024 * 1024)
        return f"文件过大（{mb:.1f} MB > {limit_mb:.0f} MB 保护上限）"
    return None


@dataclass
class PatchEntry:
    rel_path: str
    kind: str  # "added" | "modified"
    size: int
    sha256: str
    source_abs: Path

    def to_json(self) -> dict[str, Any]:
        return {
            "path": self.rel_path,
            "kind": self.kind,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass
class PatchExportReport:
    out_zip: Path | None = None
    engine: str = ""
    mode: str = ""  # "delta_patch" | "source_kit" | "empty"
    patch_files: list[PatchEntry] = field(default_factory=list)
    source_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.out_zip is not None and self.out_zip.is_file() and bool(self.patch_files or self.source_files)

    def summary(self) -> str:
        if not self.ok:
            return "未导出补丁包：" + (self.notes[0] if self.notes else "没有可打包的补丁或译文表")
        parts = [f"已导出纯净汉化补丁包：{self.out_zip}"]
        if self.patch_files:
            total_kb = sum(e.size for e in self.patch_files) / 1024.0
            parts.append(f"含 {len(self.patch_files)} 个补丁文件（约 {total_kb:.1f} KB）")
        if self.source_files:
            parts.append(f"附带 {len(self.source_files)} 个译文协作文件（{' / '.join(self.source_files)}）")
        if self.skipped:
            parts.append(f"已自动拦截/跳过 {len(self.skipped)} 项原版大文件或二进制")
        return "；".join(parts)


def _load_rgss_baselines(orig_root: Path) -> list[RgssadArchive]:
    archives: list[RgssadArchive] = []
    for pat in ("*.rgss3a", "*.rgss2a", "*.rgssad"):
        for cand in sorted(orig_root.glob(pat)):
            if not cand.is_file():
                continue
            try:
                archives.append(RgssadArchive(cand))
            except (RgssadError, OSError):
                continue
    return archives


def _matches_rgss_baseline(rel: str, size: int, item_sha256: str, archives: list[RgssadArchive]) -> bool:
    low = rel.replace("\\", "/").lower()
    for arc in archives:
        ent = arc._by_name.get(low)
        if ent is None or ent.size != size:
            continue
        try:
            raw = arc.read(ent.name)
        except (RgssadError, KeyError, OSError):
            continue
        if hashlib.sha256(raw).hexdigest() == item_sha256:
            return True
    return False


def _collect_from_copy_dir(
    orig_root: Path,
    copy_dir: Path,
    max_file_bytes: int,
    entries: dict[str, PatchEntry],
    skipped: list[str],
    notes: list[str] | None = None,
) -> bool:
    """对比副本目录与原目录（及 RGSS 加密归档基线），只收集真正改动/新增的轻量文件。

    返回是否检测到原目录含 RGSS 加密归档（用于给 README 补充解包提示）。
    """
    rgss_arcs = _load_rgss_baselines(orig_root)
    has_rgss = bool(rgss_arcs)
    rgss_unchanged = 0
    for item in sorted(copy_dir.rglob("*")):
        if not item.is_file():
            continue
        rel = item.relative_to(copy_dir).as_posix()
        item_size = item.stat().st_size
        orig = orig_root / rel
        item_sha = ""
        if orig.is_file():
            if orig.stat().st_size == item_size:
                item_sha = _sha256_file(item)
                if _sha256_file(orig) == item_sha:
                    continue
            kind = "modified"
        else:
            if has_rgss:
                low = rel.lower()
                # 原 RGSS 包内未改动的贴图/音频/数据文件不收入补丁包
                if any(low in arc._by_name for arc in rgss_arcs):
                    item_sha = item_sha or _sha256_file(item)
                    if _matches_rgss_baseline(rel, item_size, item_sha, rgss_arcs):
                        rgss_unchanged += 1
                        continue
                    kind = "modified"
                else:
                    kind = "added"
            else:
                kind = "added"
        reason = _should_skip_file(rel, item, kind, max_file_bytes)
        if reason:
            skipped.append(f"{rel}（{reason}）")
            continue
        entries[rel] = PatchEntry(
            rel_path=rel,
            kind=kind,
            size=item_size,
            sha256=item_sha or _sha256_file(item),
            source_abs=item,
        )
    if has_rgss and notes is not None:
        notes.append(
            f"已自动比对原目录 RGSS 归档，跳过 {rgss_unchanged} 个未改动的原版解包资源（贴图/音频等），仅保留真正变更的译文数据与字体。"
        )
    return has_rgss


def _render_install_bat(entries: list[PatchEntry]) -> str:
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal enabledelayedexpansion",
        'set "SCRIPT_DIR=%~dp0"',
        'set "PATCH_DIR=%SCRIPT_DIR%patch"',
        'if not exist "%PATCH_DIR%" (',
        "  echo [错误] 未找到 patch 目录，请先把整个压缩包解压后再运行本脚本。",
        "  pause",
        "  exit /b 1",
        ")",
        'set "TARGET=%~1"',
        'if "%TARGET%"=="" set "TARGET=%CD%"',
        'if /I "%TARGET%\\"=="%SCRIPT_DIR%" (',
        "  echo 请把本补丁包解压到游戏根目录，或直接把游戏文件夹拖到本脚本图标上运行。",
        '  set /p "TARGET=请输入游戏根目录路径（留空则使用当前目录）: "',
        '  if "!TARGET!"=="" set "TARGET=%CD%"',
        ")",
        'set "TARGET=%TARGET:"=%"',
        'if not exist "%TARGET%" (',
        '  echo [错误] 目标目录不存在: %TARGET%',
        "  pause",
        "  exit /b 1",
        ")",
        'set "BACKUP=%TARGET%\\.ferry-patch-backup"',
        'echo 正在安装摆渡汉化补丁到: %TARGET%',
    ]
    for e in entries:
        win_rel = e.rel_path.replace("/", "\\")
        lines.append(f'if exist "%TARGET%\\{win_rel}" if not exist "%BACKUP%\\{win_rel}" (')
        lines.append(f'  for %%F in ("%BACKUP%\\{win_rel}") do if not exist "%%~dpF" mkdir "%%~dpF"')
        lines.append(f'  copy /Y "%TARGET%\\{win_rel}" "%BACKUP%\\{win_rel}" >nul')
        lines.append(")")
        lines.append(f'for %%F in ("%TARGET%\\{win_rel}") do if not exist "%%~dpF" mkdir "%%~dpF"')
        lines.append(f'copy /Y "%PATCH_DIR%\\{win_rel}" "%TARGET%\\{win_rel}" >nul')
    lines.extend([
        "echo.",
        "echo [完成] 汉化补丁已安装！如需恢复原版，请运行「一键卸载还原.bat」。",
        "pause",
    ])
    return "\r\n".join(lines) + "\r\n"


def _render_uninstall_bat(entries: list[PatchEntry]) -> str:
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "setlocal enabledelayedexpansion",
        'set "TARGET=%~1"',
        'if "%TARGET%"=="" set "TARGET=%CD%"',
        'set "TARGET=%TARGET:"=%"',
        'set "BACKUP=%TARGET%\\.ferry-patch-backup"',
        'echo 正在卸载摆渡汉化补丁并恢复备份: %TARGET%',
    ]
    for e in entries:
        win_rel = e.rel_path.replace("/", "\\")
        if e.kind == "modified":
            lines.append(f'if exist "%BACKUP%\\{win_rel}" (')
            lines.append(f'  copy /Y "%BACKUP%\\{win_rel}" "%TARGET%\\{win_rel}" >nul')
            lines.append(")")
        else:
            lines.append(f'if exist "%BACKUP%\\{win_rel}" (')
            lines.append(f'  copy /Y "%BACKUP%\\{win_rel}" "%TARGET%\\{win_rel}" >nul')
            lines.append(f') else if exist "%TARGET%\\{win_rel}" (')
            lines.append(f'  del /F /Q "%TARGET%\\{win_rel}" >nul')
            lines.append(")")
    lines.extend([
        "echo.",
        "echo [完成] 已还原为补丁安装前的状态。",
        "pause",
    ])
    return "\r\n".join(lines) + "\r\n"


def _render_readme(
    game_name: str,
    engine: str,
    entries: list[PatchEntry],
    source_files: list[str],
    skipped: list[str],
    has_rgss_archive: bool = False,
) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"摆渡计划（ProjectFerry）纯净汉化补丁包 — {game_name}",
        "=" * 56,
        f"导出时间：{now}",
        f"识别引擎：{engine or '未知'}",
        f"补丁文件：{len(entries)} 个",
        "",
    ]
    if entries:
        lines.extend([
            "【安装方法】",
            "1. 建议先备份你的原版游戏目录与存档。",
            "2. 方法 A（自动）：把压缩包解压后，将「游戏根目录」直接拖到 `一键安装补丁.bat` 图标上；",
            "   脚本会自动把被覆盖的原文件备份到 `.ferry-patch-backup/`，随时可运行 `一键卸载还原.bat` 恢复。",
            "3. 方法 B（手动）：直接把 `patch/` 目录下的文件按相同路径复制到游戏根目录。",
            "",
        ])
        if has_rgss_archive:
            lines.extend([
                "【RPG Maker XP/VX/Ace 特别提示】",
                "本游戏原版含加密归档（如 Game.rgss3a / Game.rgss2a / Game.rgssad）。",
                "RGSS 引擎默认优先读取加密包；请先将原游戏解压为散装副本（或用摆渡生成副本后），",
                "再覆盖本补丁包并移除副本中的 .rgss3a 归档，中文才会生效。",
                "",
            ])
    else:
        lines.extend([
            "【零 Token 文本协作包说明】",
            "本游戏的汉化产物为整包副本/镜像，为遵守版权边界，本包不含原版二进制程序。",
            "你可以在 `ferry_source/` 中找到双语译文表（`ferry_tl.json` / `ferry_tl.tsv`），",
            "下载开源工具「摆渡计划」后，选择你自己的原版游戏并点击「导入外部译文 / 自翻回录重打包」，",
            "即可 0 Token 瞬间重建出完整中文版。",
            "",
        ])
    if source_files:
        lines.append("【附带开源译文协作文件（ferry_source/）】")
        for sf in source_files:
            lines.append(f"  - {sf}")
        lines.append("欢迎用 Excel / 记事本 / ParaTranz 继续润色校对后，通过摆渡 0 Token 重刷补丁。")
        lines.append("")
    if skipped:
        lines.append("【以下原版大文件/二进制未收入本补丁包】")
        for sk in skipped[:20]:
            lines.append(f"  - {sk}")
        lines.append("")
    lines.extend([
        "【说明】",
        "本补丁包仅包含汉化文本/补丁覆盖层，不含游戏本体版权资源。",
        "由 MIT 开源项目「摆渡计划 · ProjectFerry」生成。",
    ])
    return "\n".join(lines) + "\n"


def export_patch_pack(
    root: Path,
    out_zip: Path | None = None,
    *,
    include_source: bool = True,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> PatchExportReport:
    """根据 `.ferry-undo.json`（及可选译文表）打包出一份纯净、可分享的汉化补丁 ZIP。

    兼容直接选中原游戏目录或 `.cn` 副本目录（如 `Hello Charlotte EP1.cn`）：
    若选中的是 `.cn` 副本目录，会自动配对同级原目录做精准差异比对。
    """
    root = Path(root).resolve()
    base = root.parent if root.is_file() else root
    report = PatchExportReport()
    has_rgss_archive = False

    # 若用户直接选了「xxx.cn」副本目录，尝试自动定位同级原目录
    paired_copy_dir: Path | None = None
    undo_data = load_undo(base)
    if undo_data is None and _CN_COPY_SUFFIX_RE.search(base.name):
        orig_name = _CN_COPY_SUFFIX_RE.sub("", base.name)
        sibling_orig = base.parent / orig_name
        if sibling_orig.is_dir():
            paired_copy_dir = base
            base = sibling_orig
            undo_data = load_undo(base)
            report.notes.append(f"检测到传入的是 .cn 副本目录，已自动配对原目录进行差异比对：{base.name}")

    entries: dict[str, PatchEntry] = {}
    if paired_copy_dir is not None:
        report.engine = str((undo_data or {}).get("engine") or "")
        has_rgss_archive = _collect_from_copy_dir(
            base, paired_copy_dir, max_file_bytes, entries, report.skipped, notes=report.notes
        )
    elif undo_data is not None:
        report.engine = str(undo_data.get("engine") or "")
        for raw in undo_data.get("actions") or []:
            action = Action.from_json(raw)
            if action.kind in ("added", "modified"):
                rel = action.path.replace("\\", "/").lstrip("/")
                abs_path = base / rel
                reason = _should_skip_file(rel, abs_path, action.kind, max_file_bytes)
                if reason:
                    report.skipped.append(f"{rel}（{reason}）")
                    continue
                entries[rel] = PatchEntry(
                    rel_path=rel,
                    kind=action.kind,
                    size=abs_path.stat().st_size,
                    sha256=_sha256_file(abs_path),
                    source_abs=abs_path,
                )
            elif action.kind == "copy":
                target = Path(action.path)
                if target.is_dir():
                    if _collect_from_copy_dir(
                        base, target, max_file_bytes, entries, report.skipped, notes=report.notes
                    ):
                        has_rgss_archive = True
                elif target.is_file():
                    report.skipped.append(
                        f"{target.name}（整包副本不直接打包，避免分发原版程序；改用 ferry_source/ 零 Token 重打包）"
                    )
    else:
        report.notes.append("未找到 .ferry-undo.json 汉化变更记录；将尝试仅导出译文协作包。")

    # 收集可选的译文协作资产（ferry_tl.json / ferry_tl.tsv / ferry_worldbook.json）
    source_paths: list[tuple[str, Path]] = []
    if include_source:
        try:
            from ferrygame.project import TSV_NAME, find as find_tl
            from ferrygame.worldbook import WORLDBOOK_NAME, find as find_wb

            tl_path = find_tl(base)
            if tl_path is not None and tl_path.is_file():
                source_paths.append((tl_path.name, tl_path))
                tsv_path = tl_path.with_name(TSV_NAME)
                if tsv_path.is_file():
                    source_paths.append((TSV_NAME, tsv_path))
            wb_path = find_wb(base)
            if wb_path is not None and wb_path.is_file():
                source_paths.append((WORLDBOOK_NAME, wb_path))
        except Exception:
            pass

    patch_list = [entries[k] for k in sorted(entries)]
    report.patch_files = patch_list
    report.source_files = [name for name, _ in source_paths]

    if not patch_list and not source_paths:
        report.mode = "empty"
        if not report.notes:
            report.notes.append("没有找到可导出的补丁文件或译文表，请先执行一次汉化或提取文本。")
        return report

    report.mode = "delta_patch" if patch_list else "source_kit"
    if out_zip is None:
        suffix = "汉化补丁" if patch_list else "译文协作包"
        out_zip = base.parent / f"{base.name}_{suffix}.zip"
    out_zip = Path(out_zip).resolve()
    out_zip.parent.mkdir(parents=True, exist_ok=True)

    manifest = {
        "format": PATCH_FORMAT,
        "version": PATCH_VERSION,
        "game": base.name,
        "engine": report.engine,
        "mode": report.mode,
        "created": datetime.now().isoformat(timespec="seconds"),
        "files": [e.to_json() for e in patch_list],
        "source_files": list(report.source_files),
        "skipped": list(report.skipped),
    }

    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr(PATCH_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        zf.writestr(
            "README_安装说明.txt",
            _render_readme(
                base.name,
                report.engine,
                patch_list,
                report.source_files,
                report.skipped,
                has_rgss_archive=has_rgss_archive,
            ).encode("utf-8-sig"),
        )
        for entry in patch_list:
            zf.write(entry.source_abs, arcname=f"patch/{entry.rel_path}")
        if patch_list:
            zf.writestr("一键安装补丁.bat", _render_install_bat(patch_list).encode("utf-8"))
            zf.writestr("一键卸载还原.bat", _render_uninstall_bat(patch_list).encode("utf-8"))
        for name, src_path in source_paths:
            zf.write(src_path, arcname=f"ferry_source/{name}")

    report.out_zip = out_zip
    return report
