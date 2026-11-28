"""高频引擎中文字体自动回退注入器（全程纳入 UndoLog，可一键撤销与打包分发）。

`fontcheck.py` 负责「体检」（判断现有字体装不装得下 GB2312 一级汉字），
本模块负责「治病」——针对高频引擎与缺字 `.ttf`，自动注入开源或本机完整中文字体：

- **Ren'Py (`renpy`)**：零侵入！向 `game/fonts/ferry_cjk.ttf` 放入中文字体，并新增高优先级
  `game/zz_ferry_cjk_font.rpy` 覆盖默认字体，不修改游戏原有 `.rpy` 脚本；
- **RPG Maker MV / MZ (`rpgmaker`)**：放入 `fonts/ferry_cjk.ttf`，MV 改写 `fonts/gamefont.css`
  的 `GameFont` 源，MZ 更新 `data/System.json` 的 `advanced.mainFontFilename` 与 `fallbackFonts`；
- **RPG Maker XP / VX / Ace (`rpgmaker_ace`)**：借 `ttf_rename.write_renamed` 保持原字体内部家族名，
  替换 `Fonts/` 下缺字 `.ttf` 并放入 `Fonts/GameCJK.ttf`；
- **Kirikiri (`kirikiri`)**：更新散装 `Config.tjs` 中的 `defaultFace` / `userFace`；
- **Godot (`godot`)**：散装工程向 `project.godot` 写入 `[gui]` 默认中文字体；
- **通用兜底 (`generic`)**：把 `scan_fonts` 检出的缺字 `.ttf` 按原家族名无损替换为中文字体。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .adapters.ttf_rename import TtfRenameError, write_renamed
from .adapters.undo import UndoLog
from .base import app_dir
from .deps import DEFAULT_TOOLS_DIR
from .fontcheck import check_font, scan_fonts

FERRY_CJK_FILENAME = "ferry_cjk.ttf"

# 开源中文字体（用户放在 摆渡目录/fonts 或 D:/tools/fonts 下优先命中）+ Windows 系统单文件 TTF
_OPENSOURCE_FONT_NAMES = (
    "NotoSansSC-Regular.ttf",
    "NotoSansSC-Regular.otf",
    "NotoSansCJKsc-Regular.otf",
    "LXGWWenKai-Regular.ttf",
    "LXGWWenKaiLite-Regular.ttf",
    "SourceHanSansSC-Regular.otf",
    "SarasaGothicSC-Regular.ttf",
)

_SYSTEM_CJK_CANDIDATES = (
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path(r"C:\Windows\Fonts\Deng.ttf"),
    Path(r"C:\Windows\Fonts\msyh.ttf"),
    Path(r"C:\Windows\Fonts\NotoSansSC-Regular.otf"),
    Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
)


@dataclass
class FontPatchReport:
    engine: str = ""
    font_used: Path | None = None
    font_family: str = ""
    patched_files: list[str] = field(default_factory=list)
    undo_path: Path | None = None
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.patched_files)

    def summary(self) -> str:
        if not self.ok:
            return "未注入中文字体：" + (self.notes[0] if self.notes else "未找到可处理的字体位置")
        prefix = "【预演】" if self.dry_run else ""
        src_name = self.font_used.name if self.font_used else "未知字体"
        return (
            f"{prefix}已为 {self.engine} 注入中文字体回退（源字体：{src_name}），"
            f"共写入 {len(self.patched_files)} 个文件：" + "、".join(self.patched_files[:6])
        )


def pick_cjk_font(preferred: Path | None = None, *, require_ttf: bool = False) -> tuple[Path | None, str]:
    """寻找一款通过 GB2312 一级字覆盖检测的中文字体，返回 `(路径, 家族名)`。

    优先级：
    1. 用户显式指定的字体文件 `preferred`
    2. 摆渡目录 `fonts/` 或 `D:/tools/fonts/` 中的开源中文字体（思源黑体 / 霞鹜文楷等）
    3. Windows / 系统自带单文件 CJK 字体
    """
    candidates: list[Path] = []
    if preferred is not None:
        candidates.append(Path(preferred))
    for base_dir in (app_dir() / "fonts", DEFAULT_TOOLS_DIR / "fonts"):
        if base_dir.is_dir():
            for name in _OPENSOURCE_FONT_NAMES:
                candidates.append(base_dir / name)
            for extra in sorted(base_dir.glob("*.ttf")) + sorted(base_dir.glob("*.otf")):
                if extra not in candidates:
                    candidates.append(extra)
    candidates.extend(_SYSTEM_CJK_CANDIDATES)

    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_file():
            continue
        seen.add(resolved)
        if require_ttf and resolved.suffix.lower() not in (".ttf", ".otf"):
            continue
        cov = check_font(resolved)
        if cov.level == "ok":
            return resolved, (cov.family or resolved.stem)
    return None, ""


def _write_tracked(base: Path, rel: str, data: bytes, undo: UndoLog, dry_run: bool) -> None:
    rel_norm = rel.replace("\\", "/").lstrip("/")
    target = base / rel_norm
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        undo.will_modify(rel_norm)
    else:
        undo.will_add(rel_norm)
    target.write_bytes(data)
    undo.did_write(rel_norm)


def _copy_tracked(base: Path, rel: str, src: Path, undo: UndoLog, dry_run: bool) -> None:
    rel_norm = rel.replace("\\", "/").lstrip("/")
    target = base / rel_norm
    if dry_run:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        undo.will_modify(rel_norm)
    else:
        undo.will_add(rel_norm)
    shutil.copy2(src, target)
    undo.did_write(rel_norm)


def _detect_engine_for_font(root: Path) -> str:
    if (root / "game").is_dir() and (any((root / "game").rglob("*.rpy")) or any((root / "game").glob("*.rpa"))):
        return "renpy"
    if (root / "www" / "data" / "System.json").is_file() or (root / "data" / "System.json").is_file():
        return "rpgmaker"
    if (root / "Game.ini").is_file() and (
        (root / "Fonts").is_dir() or any(root.glob("Game.rgss*")) or (root / "Data" / "Scripts.rvdata2").is_file()
    ):
        return "rpgmaker_ace"
    if (root / "project.godot").is_file():
        return "godot"
    for cand in (root / "system" / "Config.tjs", root / "data" / "system" / "Config.tjs", root / "Config.tjs"):
        if cand.is_file():
            return "kirikiri"
    return "generic"


def _patch_renpy(base: Path, font_src: Path, undo: UndoLog, report: FontPatchReport, dry_run: bool) -> None:
    game_dir = base / "game" if (base / "game").is_dir() else base
    ext = font_src.suffix.lower() if font_src.suffix.lower() in (".ttf", ".otf") else ".ttf"
    font_name = f"ferry_cjk{ext}"
    font_rel = (game_dir / "fonts" / font_name).relative_to(base).as_posix()
    script_rel = (game_dir / "zz_ferry_cjk_font.rpy").relative_to(base).as_posix()

    rpy_code = (
        "# 由摆渡计划自动生成的中文字体回退补丁（不修改游戏原有脚本，点「撤销汉化」即可完全还原）\n"
        "init 999 python:\n"
        f'    _ferry_cjk = "fonts/{font_name}"\n'
        "    if renpy.loadable(_ferry_cjk):\n"
        "        for _attr in (\n"
        '            "text_font", "name_text_font", "interface_text_font",\n'
        '            "button_text_font", "choice_button_text_font", "system_font",\n'
        "        ):\n"
        "            if hasattr(gui, _attr):\n"
        "                setattr(gui, _attr, _ferry_cjk)\n"
        "        style.default.font = _ferry_cjk\n"
        '        for _sname in ("say_dialogue", "say_label", "choice_button_text", "button_text", "notify_text"):\n'
        "            if hasattr(style, _sname):\n"
        "                getattr(style, _sname).font = _ferry_cjk\n"
    )
    _copy_tracked(base, font_rel, font_src, undo, dry_run)
    _write_tracked(base, script_rel, rpy_code.encode("utf-8"), undo, dry_run)
    report.patched_files.extend([font_rel, script_rel])
    report.notes.append("Ren'Py 已采用零侵入高优先级脚本（init 999）注入中文字体，原 .rpy 未改动。")


def _patch_rpgmaker_mvmz(base: Path, font_src: Path, undo: UndoLog, report: FontPatchReport, dry_run: bool) -> None:
    web_root = base / "www" if (base / "www" / "data").is_dir() else base
    ext = font_src.suffix.lower() if font_src.suffix.lower() in (".ttf", ".otf") else ".ttf"
    font_name = f"ferry_cjk{ext}"
    font_rel = (web_root / "fonts" / font_name).relative_to(base).as_posix()
    _copy_tracked(base, font_rel, font_src, undo, dry_run)
    report.patched_files.append(font_rel)

    # MV: fonts/gamefont.css
    css_path = web_root / "fonts" / "gamefont.css"
    if css_path.is_file():
        css_text = css_path.read_text(encoding="utf-8-sig", errors="replace")
        new_css, count = re.subn(
            r"""url\(\s*(['"]?)[^)'"]+\.(?:ttf|otf|woff2?)\1\s*\)""",
            f'url("{font_name}")',
            css_text,
            count=1,
            flags=re.IGNORECASE,
        )
        if count == 0:
            new_css = (
                f'@font-face {{\n    font-family: GameFont;\n    src: url("{font_name}");\n}}\n\n'
                + css_text
            )
        css_rel = css_path.relative_to(base).as_posix()
        _write_tracked(base, css_rel, new_css.encode("utf-8"), undo, dry_run)
        report.patched_files.append(css_rel)
        report.notes.append("RPG Maker MV：已将 fonts/gamefont.css 的 GameFont 指向中文字体。")

    # MZ: data/System.json -> advanced.mainFontFilename / fallbackFonts
    sys_path = web_root / "data" / "System.json"
    if sys_path.is_file():
        try:
            data = json.loads(sys_path.read_text(encoding="utf-8-sig"))
        except Exception:
            data = None
        if isinstance(data, dict) and isinstance(data.get("advanced"), dict):
            adv = data["advanced"]
            adv["mainFontFilename"] = font_name
            fb = str(adv.get("fallbackFonts") or "").strip()
            cjk_fb = '"Microsoft YaHei", "SimHei", sans-serif'
            if "YaHei" not in fb and "SimHei" not in fb:
                adv["fallbackFonts"] = f"{fb}, {cjk_fb}" if fb else cjk_fb
            sys_rel = sys_path.relative_to(base).as_posix()
            _write_tracked(
                base,
                sys_rel,
                json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
                undo,
                dry_run,
            )
            report.patched_files.append(sys_rel)
            report.notes.append("RPG Maker MZ：已更新 data/System.json 的 mainFontFilename 与 fallbackFonts。")


def _patch_rpgmaker_ace(base: Path, font_src: Path, undo: UndoLog, report: FontPatchReport, dry_run: bool) -> None:
    fonts_dir = base / "Fonts"
    bundled_rel = "Fonts/GameCJK.ttf"
    if not dry_run:
        fonts_dir.mkdir(parents=True, exist_ok=True)
        bundled_abs = base / bundled_rel
        if bundled_abs.exists():
            undo.will_modify(bundled_rel)
        else:
            undo.will_add(bundled_rel)
        try:
            write_renamed(font_src, bundled_abs, "GameCJK")
        except TtfRenameError:
            shutil.copy2(font_src, bundled_abs)
        undo.did_write(bundled_rel)
    report.patched_files.append(bundled_rel)

    if fonts_dir.is_dir():
        for existing in sorted(fonts_dir.glob("*.ttf")):
            if existing.name.lower() == "gamecjk.ttf":
                continue
            cov = check_font(existing)
            if cov.level == "ok":
                continue
            rel = existing.relative_to(base).as_posix()
            family = cov.family or existing.stem
            if not dry_run:
                undo.will_modify(rel)
                try:
                    write_renamed(font_src, existing, family)
                except TtfRenameError:
                    shutil.copy2(font_src, existing)
                undo.did_write(rel)
            report.patched_files.append(rel)
            report.notes.append(f"已保留原家族名「{family}」替换缺字字体 {rel}。")


def _patch_kirikiri(base: Path, font_src: Path, family: str, undo: UndoLog, report: FontPatchReport, dry_run: bool) -> None:
    face = family or "SimHei"
    for cand in (base / "system" / "Config.tjs", base / "data" / "system" / "Config.tjs", base / "Config.tjs"):
        if not cand.is_file():
            continue
        raw = cand.read_bytes()
        enc = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        try:
            text = raw.decode(enc)
        except UnicodeDecodeError:
            enc = "cp932"
            text = raw.decode(enc, errors="replace")
        patched = re.sub(
            r'((?:defaultFace|userFace)\s*=\s*")[^"]*(")',
            rf"\g<1>{face}\g<2>",
            text,
        )
        if patched != text:
            rel = cand.relative_to(base).as_posix()
            out_bytes = patched.encode("utf-16") if enc == "utf-16" else patched.encode("utf-8-sig")
            _write_tracked(base, rel, out_bytes, undo, dry_run)
            report.patched_files.append(rel)
            report.notes.append(f"Kirikiri：已将 {rel} 默认字体面改为 {face}。")
            return
    report.notes.append("未找到可改写的散装 Config.tjs（若配置封在 .xp3 内，请先确保系统含宋体/黑体并在 patch.xp3 中覆盖）。")


def _patch_godot(base: Path, font_src: Path, undo: UndoLog, report: FontPatchReport, dry_run: bool) -> None:
    proj = base / "project.godot"
    if not proj.is_file():
        report.notes.append("未找到散装 project.godot（纯 .pck 发行版建议通过源工程配置回退字体）。")
        return
    ext = font_src.suffix.lower() if font_src.suffix.lower() in (".ttf", ".otf") else ".ttf"
    font_rel = f"ferry_cjk{ext}"
    _copy_tracked(base, font_rel, font_src, undo, dry_run)
    report.patched_files.append(font_rel)

    text = proj.read_text(encoding="utf-8-sig", errors="replace")
    setting_line = f'theme/custom_font="res://{font_rel}"'
    if "[gui]" in text:
        if re.search(r"^\s*theme/custom_font\s*=", text, flags=re.M):
            new_text = re.sub(r"^\s*theme/custom_font\s*=.*$", setting_line, text, count=1, flags=re.M)
        else:
            new_text = text.replace("[gui]", f"[gui]\n{setting_line}", 1)
    else:
        new_text = text.rstrip() + f"\n\n[gui]\n{setting_line}\n"
    _write_tracked(base, "project.godot", new_text.encode("utf-8"), undo, dry_run)
    report.patched_files.append("project.godot")
    report.notes.append("Godot：已在 project.godot [gui] 段写入默认中文字体。")


def _patch_generic_broken_ttfs(base: Path, font_src: Path, undo: UndoLog, report: FontPatchReport, dry_run: bool) -> None:
    scan = scan_fonts(base)
    broken = [f for f in scan.found if f.level in ("broken", "tight") and f.path.suffix.lower() == ".ttf"]
    if not broken:
        report.notes.append("未在游戏目录中发现缺字的散装 .ttf 字体文件（字体可能已支持中文或内嵌在资源包中）。")
        return
    for cov in broken:
        rel = cov.path.relative_to(base).as_posix()
        family = cov.family or cov.path.stem
        if not dry_run:
            undo.will_modify(rel)
            try:
                write_renamed(font_src, cov.path, family)
            except TtfRenameError:
                shutil.copy2(font_src, cov.path)
            undo.did_write(rel)
        report.patched_files.append(rel)
        report.notes.append(f"已保留原家族名「{family}」替换缺字字体 {rel}（原覆盖率 {cov.ratio * 100:.1f}%）。")


def patch_game_font(
    root: Path,
    *,
    engine: str = "auto",
    font_path: Path | None = None,
    dry_run: bool = False,
) -> FontPatchReport:
    """为指定游戏目录自动注入中文字体回退，并写入 `.ferry-undo.json` 支持一键撤销。"""
    root = Path(root).resolve()
    base = root.parent if root.is_file() else root
    detected = _detect_engine_for_font(base) if (not engine or engine == "auto") else engine.lower()
    report = FontPatchReport(engine=detected, dry_run=dry_run)

    require_ttf = detected in ("rpgmaker_ace", "generic")
    chosen_font, chosen_family = pick_cjk_font(font_path, require_ttf=require_ttf)
    if chosen_font is None:
        report.notes.append(
            "未找到可用的完整中文字体（GB2312 一级字覆盖需 ≥99%）。"
            "请放一款开源中文字体（如思源黑体 NotoSansSC / 霞鹜文楷）到摆渡目录 fonts/ 下，或用 --font 指定。"
        )
        return report

    report.font_used = chosen_font
    report.font_family = chosen_family
    undo = UndoLog.open(base, engine=detected)

    if detected == "renpy":
        _patch_renpy(base, chosen_font, undo, report, dry_run)
    elif detected == "rpgmaker":
        _patch_rpgmaker_mvmz(base, chosen_font, undo, report, dry_run)
    elif detected == "rpgmaker_ace":
        _patch_rpgmaker_ace(base, chosen_font, undo, report, dry_run)
    elif detected == "kirikiri":
        _patch_kirikiri(base, chosen_font, chosen_family, undo, report, dry_run)
    elif detected == "godot":
        _patch_godot(base, chosen_font, undo, report, dry_run)
    else:
        _patch_generic_broken_ttfs(base, chosen_font, undo, report, dry_run)

    if report.patched_files and not dry_run:
        report.undo_path = undo.finish()
    return report
