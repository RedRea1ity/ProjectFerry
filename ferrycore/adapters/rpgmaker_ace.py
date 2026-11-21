"""RPG Maker XP / VX / VX Ace 适配器。

文本在加密包 `Game.rgssad` / `.rgss2a` / `.rgss3a` 里的 `Data/*.rxdata|rvdata|rvdata2`，
Ruby Marshal 序列化。MV/MZ 那条明文 JSON 路走不通。

可写策略（不重打包）：
    解压整包到副本目录 → 再合并游戏目录里的散装资源 → 不放加密包。
注意：很多 Steam 作把 Shadow.png 等 RTP 文件放在包外；只解压加密包会缺文件。

字体：Ace 常绑日文/像素字体（如 Coder's Crux），中文会直接「消失」。
回写时会：(1) 把 Main / 硬编码 FONT 改成系统中文族名回退列表
（中文 Windows 上黑体真实族名是「黑体」不是 SimHei）；(2) 尽量用本机
TTF 顶替 Fonts/ 里原字体的 name 表；(3) 把字号提到可读大小。

本适配器只依赖标准库 + 同目录的 `rgssad` / `ruby_marshal`。
"""

from __future__ import annotations

import re
import shutil
import zlib
from pathlib import Path

from ..fontcheck import check_font
from ..models import Entry
from .base import Adapter, ApplyReport, ProbeReport
from .container import unique_output
from .rgssad import RgssadArchive, RgssadError, find_archive
from . import ruby_marshal as rm
from .ttf_rename import TtfRenameError, write_renamed
from .undo import UNDO_NAME, UndoLog

# 事件指令：跟 MV 同一套码
_CODE_TEXT = 401
_CODE_SCROLL = 405
_CODE_CHOICES = 102
_CODE_NAME = 320
_CODE_NICKNAME = 324
_CODE_PROFILE = 325

_DB_FILES = {
    "Actors", "Classes", "Enemies", "Items", "Weapons", "Armors",
    "Skills", "States",
}
_DB_FIELDS = ("name", "nickname", "description", "message1", "message2",
              "message3", "message4", "profile")

_MAP_FILE = re.compile(r"^Map\d+\.(rvdata2|rvdata|rxdata)$", re.IGNORECASE)
_CJK = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]")

_SUFFIX_BY_VER = {3: ".rvdata2", 1: None}  # v1 可能是 .rvdata 或 .rxdata


def _worth(text) -> bool:
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if _CJK.search(stripped):
        return True
    if re.fullmatch(r"(\\[A-Za-z]+\[\d+\]|[\s\d\-+*/%=<>!&|.,:;()\[\]{}'\"#@$^~`]*)+", stripped):
        return False
    # 资源/文件名不像台词
    if "/" in stripped or "\\" in stripped:
        return False
    if stripped.startswith("<") and stripped.endswith(">") and len(stripped) < 20:
        # <error> 这种系统标签仍可能要翻；短标签放行
        pass
    return len(stripped) > 1


def _engine_label(version: int, suffix: str) -> str:
    if version == 3 or suffix == ".rvdata2":
        return "RPG Maker VX Ace"
    if suffix == ".rvdata":
        return "RPG Maker VX"
    if suffix == ".rxdata":
        return "RPG Maker XP"
    return "RPG Maker (RGSS)"


class RpgMakerAceAdapter(Adapter):
    """名字沿用 ace，实际 XP/VX/Ace 都能认；priority 压过 MV，因为
    `Game.rgss3a` 比 `data/System.json` 更特异。"""

    name = "rpgmaker_ace"
    priority = 22

    def __init__(self, include_samples: bool = False) -> None:
        self.include_samples = include_samples

    # ---- 定位 ----

    def _archive(self, root: Path) -> Path | None:
        return find_archive(Path(root))

    def _loose_data(self, root: Path) -> Path | None:
        data = Path(root) / "Data"
        if not data.is_dir():
            return None
        if any(data.glob("*.rvdata2")) or any(data.glob("*.rvdata")) or any(data.glob("*.rxdata")):
            return data
        return None

    def _detect_suffix(self, root: Path) -> str | None:
        arc = self._archive(root)
        if arc is not None:
            try:
                archive = RgssadArchive(arc)
            except RgssadError:
                return None
            if archive.version == 3:
                return ".rvdata2"
            # v1：看档案里 Data 下的后缀
            for entry in archive.data_files():
                lower = entry.name.lower()
                for suf in (".rvdata2", ".rvdata", ".rxdata"):
                    if lower.endswith(suf):
                        return suf
            return ".rxdata"
        data = self._loose_data(root)
        if data is None:
            return None
        for suf in (".rvdata2", ".rvdata", ".rxdata"):
            if any(data.glob(f"*{suf}")):
                return suf
        return None

    def _list_data_paths(self, root: Path) -> list[tuple[str, callable]]:
        """返回 (相对路径 Data/xxx, 读字节的无参函数)。"""
        root = Path(root)
        suf = self._detect_suffix(root)
        if suf is None:
            return []
        out: list[tuple[str, callable]] = []
        loose = self._loose_data(root)
        if loose is not None:
            for path in sorted(loose.glob(f"*{suf}")):
                rel = f"Data/{path.name}"
                out.append((rel, path.read_bytes))
            return out
        arc_path = self._archive(root)
        if arc_path is None:
            return []
        archive = RgssadArchive(arc_path)

        def reader(name: str):
            return lambda n=name: archive.read(n)

        for entry in archive.data_files(suf):
            out.append((entry.name, reader(entry.name)))
        return out

    # ---- 探测 ----

    def probe(self, root: Path) -> ProbeReport:
        root = Path(root)
        suf = self._detect_suffix(root)
        if suf is None:
            return ProbeReport(
                engine=self.name,
                root=root,
                notes=["没找到 Game.rgss3a/rgss2a/rgssad，也没有 Data/*.rvdata2"],
            )
        arc = self._archive(root)
        version = 3
        if arc is not None:
            try:
                version = RgssadArchive(arc).version
            except RgssadError as exc:
                return ProbeReport(engine=self.name, root=root, notes=[str(exc)])
        entries = self.collect(root)
        report = ProbeReport(
            engine=_engine_label(version, suf),
            root=root,
            readable=True,
            writable=True,
            verifiable=False,
            unit_count=len(entries),
            container="rgssad" if arc else "folder",
            text_format=suf,
            unicode_engine=True,
        )
        if arc:
            report.notes.append(
                f"加密包 {arc.name}（RGSSAD v{version}）。"
                "回写会解压到副本目录并去掉加密包，不改原目录。"
            )
        else:
            report.notes.append("已是散装 Data/，回写改副本里的 .rvdata*。")
        report.notes.append(
            "只翻事件 401/405/102/320/324/325 与数据库 name/description 等字段；"
            "Scripts 里的 Ruby 代码不碰。"
        )
        # 标题
        for rel, reader in self._list_data_paths(root):
            if rel.lower().endswith(f"/system{suf}"):
                try:
                    sysobj = rm.load(reader())
                except (rm.MarshalError, OSError, KeyError):
                    break
                title = sysobj.get("game_title") if isinstance(sysobj, dict) else None
                if title:
                    report.notes.append(f"游戏标题：{title}")
                break
        return report

    # ---- 抽取 ----

    def collect(self, root: Path) -> list[Entry]:
        root = Path(root).absolute()
        entries: list[Entry] = []
        for rel, reader in self._list_data_paths(root):
            try:
                payload = rm.load(reader())
            except (rm.MarshalError, OSError, KeyError, RgssadError):
                continue
            stem = Path(rel).stem
            for pointer, text, kind in iter_texts(stem, Path(rel).name, payload):
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
        root = Path(root).absolute()
        report = ApplyReport()
        entries = self.collect(root)
        if not entries:
            report.notes.append("没有文本单元")
            return report

        dest = Path(out) if out else unique_output(root.parent / f"{root.name}.cn")
        if dest.exists():
            report.conflicts.append(f"副本已存在：{dest}")
            return report

        try:
            self._build_playable_copy(root, dest)
        except (OSError, RgssadError) as exc:
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            report.conflicts.append(f"解压/复制失败：{exc}")
            return report

        # 按文件改 Marshal
        by_file: dict[str, list[Entry]] = {}
        for entry in entries:
            by_file.setdefault(entry.rel_path, []).append(entry)

        for rel_path, items in by_file.items():
            path = dest / Path(*rel_path.split("/"))
            if not path.is_file():
                report.notes.append(f"副本里没有 {rel_path}")
                continue
            try:
                payload = rm.load(path.read_bytes())
            except (rm.MarshalError, OSError) as exc:
                report.notes.append(f"{rel_path}：读失败（{exc}）")
                continue
            changed = False
            for entry in items:
                translated = translations.get(entry.key)
                if translated is None:
                    translated = translations.get(entry.english)
                if translated is None or not translated.strip():
                    report.missed += 1
                    continue
                try:
                    set_pointer(payload, entry.json_pointer, translated.strip())
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    report.conflicts.append(f"{entry.key}：{exc}")
                    continue
                changed = True
                report.applied += 1
            if changed:
                try:
                    path.write_bytes(rm.dump(payload))
                except (rm.MarshalError, OSError) as exc:
                    report.conflicts.append(f"{rel_path}：写失败（{exc}）")

        # 撤销记录挂在原目录：删掉副本即可还原「我做过的事」
        log = UndoLog.open(root, engine=self.name)
        log.did_copy(dest)
        log.finish()

        font_note = _ensure_cjk_font(dest)
        if font_note:
            report.notes.append(font_note)
        skin_note = _ensure_readable_text_color(dest)
        if skin_note:
            report.notes.append(skin_note)

        report.out_path = dest
        report.in_place = False
        report.undo_path = root / UNDO_NAME
        report.notes.append(
            f"已解压并写出副本：{dest}。原目录未改。删副本或 `revert` 即可。"
        )
        return report

    def _build_playable_copy(self, root: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=False)
        # 先解压加密包
        arc = self._archive(root)
        archive_obj = None
        if arc is not None:
            archive_obj = RgssadArchive(arc)
            archive_obj.extract_all(dest)
        # 顶层可执行文件 / ini
        for name in ("Game.exe", "Game.ini", "Licenses.txt", "RGSS301.dll", "RGSS202E.dll", "RGSS100J.dll"):
            src = root / name
            if src.is_file():
                shutil.copy2(src, dest / name)
        # 散装目录必须合并进解压结果：Steam 作常把几百个 RTP 图放在包外，
        # 旧逻辑「Graphics 已存在就跳过」会丢掉 Shadow.png 之类，进游戏直接报 unable to find file。
        # Data 也要合并：少数作会把已改 rvdata 放包外。翻译写入发生在此之后。
        for dirname in ("System", "Fonts", "Audio", "Graphics", "Data"):
            src = root / dirname
            if src.is_dir():
                _merge_tree(src, dest / dirname)
        # 窗口皮肤：包外常有「白字」Window.png，盖掉包内黑字皮肤后，
        # 白底对话框会整段看不见字。包内有就强制用包内的。
        if archive_obj is not None:
            _prefer_archive_windowskin(archive_obj, dest)
        # 副本里绝不能留加密包，否则引擎优先读包、改了也白改
        for pat in ("*.rgss3a", "*.rgss2a", "*.rgssad"):
            for leftover in dest.glob(pat):
                leftover.unlink()


def _merge_tree(src: Path, dest: Path) -> None:
    """把 src 合并进 dest：散装文件补洞并覆盖同名（包外资源优先）。"""
    dest.mkdir(parents=True, exist_ok=True)
    for item in src.rglob("*"):
        if not item.is_file():
            continue
        target = dest / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def _prefer_archive_windowskin(archive: RgssadArchive, dest: Path) -> None:
    """加密包里的 Window.png 优先于散装（避免白字皮肤盖掉可读皮肤）。"""
    key = "Graphics/System/Window.png"
    raw = None
    try:
        raw = archive.read(key)
    except KeyError:
        for entry in archive.entries:
            if entry.name.lower().replace("\\", "/") == key.lower():
                raw = archive.read(entry.name)
                break
    if raw is None:
        return
    target = dest.joinpath(*key.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw)


# 本机常见中文字体（只要 .ttf/.otf，不要 .ttc —— RGSS3 对 TTC 经常画不出字）。
_CJK_FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path(r"C:\Windows\Fonts\Deng.ttf"),
    Path(r"C:\Windows\Fonts\msyh.ttf"),
    Path(r"C:\Windows\Fonts\NotoSansSC-Regular.otf"),
    Path(r"C:\Windows\Fonts\NotoSansSC-VF.ttf"),
)

# Scripts 里只写 ASCII 族名。中文 Windows 上「黑体」等中文名进 Scripts
# 偶发编码/匹配问题；统一打一份 GameCJK.ttf（name 表=GameCJK）最稳。
_GAME_CJK_FACE = "GameCJK"

# 原作像素字体常用 14；换成真正的中文字形后提到 20，否则对话框像没字。
_CJK_FONT_SIZE = 20


def _pick_system_cjk_font() -> Path | None:
    for path in _CJK_FONT_CANDIDATES:
        if not path.is_file():
            continue
        try:
            cov = check_font(path)
        except OSError:
            continue
        if cov.level == "ok":
            return path
    return None


def _ensure_cjk_font(dest: Path) -> str:
    """汉化副本的显示层字库处理。

    1. 在 Fonts/ 放入 GameCJK.ttf（本机中文字形 + 精简 ASCII 族名）。
    2. Main / FONT → Font.default_name = ["GameCJK"]（Scripts 不含中文）。
    3. Window_Base#reset_font_settings 强制 contents.font.name = Font.default_name。
    4. 顺带 spoof 原字体文件名（双保险）；字号提到 20。
    """
    notes: list[str] = []
    fonts_dir = dest / "Fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    sys_font = _pick_system_cjk_font()
    if sys_font is None:
        return "本机没找到可用中文 TTF（需 simhei.ttf 等），对话框可能空白。"

    bundled = fonts_dir / f"{_GAME_CJK_FACE}.ttf"
    try:
        write_renamed(sys_font, bundled, _GAME_CJK_FACE)
        notes.append(f"已写入 Fonts/{bundled.name}（源 {sys_font.name}）。")
    except (TtfRenameError, OSError) as exc:
        return f"写入 {_GAME_CJK_FACE}.ttf 失败：{exc}"

    original_families = _detect_font_families(dest)
    spoofed = 0
    for family in original_families:
        if family == _GAME_CJK_FACE:
            continue
        if _spoof_font_file(fonts_dir, sys_font, family):
            spoofed += 1
    if spoofed:
        notes.append(f"另顶替了 {spoofed} 个原字体文件（保留原族名）。")

    faces = [_GAME_CJK_FACE, *original_families]
    # 去重且保序
    dedup: list[str] = []
    for f in faces:
        if f and f not in dedup:
            dedup.append(f)

    if _patch_font_names(dest, dedup):
        notes.append(f"已改 Scripts：Font.default_name = {dedup!r}")
    else:
        notes.append("未能改 Scripts 字体名。")

    if _patch_reset_font_name(dest):
        notes.append("已补 Window_Base：contents.font.name = Font.default_name。")

    if _bump_font_size(dest, _CJK_FONT_SIZE):
        notes.append(f"字号已提到 {_CJK_FONT_SIZE}。")
    return " ".join(notes)


def _windowskin_color0_luma(path: Path) -> float | None:
    """读 Window.png 正文字色（色板 0）亮度；失败返回 None。"""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        im = Image.open(path).convert("RGBA")
    except OSError:
        return None
    if im.size[0] < 128 or im.size[1] < 128:
        return None
    # Ace 色板：从 (64,96) 起每格 8×8，色号 0 在正中取样
    r, g, b, _a = im.getpixel((68, 100))
    return (r * 299 + g * 587 + b * 114) / 1000.0


def _ensure_readable_text_color(dest: Path) -> str:
    """窗口皮肤正文字若接近白色，强制 Window_Base#normal_color 为黑。

    Hello Charlotte 一类白底对话框 + 散装白字皮肤 = 有框有箭头但看不见字。
    """
    skin = dest / "Graphics" / "System" / "Window.png"
    if not skin.is_file():
        return ""
    luma = _windowskin_color0_luma(skin)
    if luma is None or luma < 200:
        return ""
    if _force_black_normal_color(dest):
        return f"窗口皮肤正文字过亮（亮度 {luma:.0f}），已强制黑字。"
    return f"窗口皮肤正文字过亮（亮度 {luma:.0f}），但未能改 Scripts。"


def _force_black_normal_color(dest: Path) -> bool:
    scripts_path = _scripts_path(dest)
    if scripts_path is None:
        return False
    try:
        scripts = rm.load(scripts_path.read_bytes())
    except (rm.MarshalError, OSError):
        return False
    changed = False
    for item in scripts if isinstance(scripts, list) else []:
        if not isinstance(item, list) or len(item) < 3 or item[1] != "Window_Base":
            continue
        body = item[2]
        if not isinstance(body, (bytes, bytearray)):
            continue
        try:
            text = zlib.decompress(body).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            continue
        if "Color.new(0, 0, 0, 255)" in text and "def normal_color" in text:
            return False
        pat = re.compile(
            r"def normal_color\s*;\s*text_color\(0\)\s*;\s*end;"
        )
        if not pat.search(text):
            continue
        text2 = pat.sub(
            "def normal_color;      Color.new(0, 0, 0, 255);   end;",
            text,
            count=1,
        )
        if text2 == text:
            continue
        item[2] = zlib.compress(text2.encode("utf-8"))
        changed = True
        break
    if not changed:
        return False
    try:
        scripts_path.write_bytes(rm.dump(scripts))
    except (rm.MarshalError, OSError):
        return False
    return True


def _detect_font_families(dest: Path) -> list[str]:
    """从 Main / 常见脚本里抠 Font.default_name 与硬编码 FONT = \"...\"。"""
    scripts_path = _scripts_path(dest)
    if scripts_path is None:
        return []
    try:
        scripts = rm.load(scripts_path.read_bytes())
    except (rm.MarshalError, OSError):
        return []
    found: list[str] = []
    pat_arr = re.compile(r"Font\.default_name\s*=\s*\[([^\]]*)\]")
    pat_str = re.compile(r'Font\.default_name\s*=\s*"([^"]+)"')
    pat_const = re.compile(r'FONT\s*=\s*"([^"]+)"')
    for item in scripts if isinstance(scripts, list) else []:
        if not isinstance(item, list) or len(item) < 3:
            continue
        body = item[2]
        if not isinstance(body, (bytes, bytearray)):
            continue
        try:
            text = zlib.decompress(body).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            continue
        for m in pat_arr.finditer(text):
            for piece in re.findall(r'"([^"]+)"', m.group(1)):
                if piece and piece not in found:
                    found.append(piece)
        for m in pat_str.finditer(text):
            if m.group(1) not in found:
                found.append(m.group(1))
        for m in pat_const.finditer(text):
            if m.group(1) not in found:
                found.append(m.group(1))
    return found


def _spoof_font_file(fonts_dir: Path, cjk_src: Path, family: str) -> bool:
    """把 Fonts/ 里家族名或文件名对得上的 ttf 换成中文字形，name 表仍用 family。"""
    # 先按已有文件的家族名匹配
    candidates: list[Path] = []
    for path in list(fonts_dir.glob("*.ttf")) + list(fonts_dir.glob("*.otf")):
        try:
            cov = check_font(path)
        except OSError:
            continue
        if cov.family == family or family.lower() in path.stem.lower().replace("_", " "):
            candidates.append(path)
    # 常见文件名猜测
    slug = re.sub(r"[^A-Za-z0-9]+", "", family).lower()
    for path in fonts_dir.glob("*.ttf"):
        if slug and slug in path.stem.lower().replace("_", ""):
            if path not in candidates:
                candidates.append(path)
    if not candidates:
        # 没有现成文件就按家族名新建一个
        safe = re.sub(r"[^\w]+", "_", family).strip("_") or "GameFont"
        candidates = [fonts_dir / f"{safe}.ttf"]
    ok = False
    for path in candidates:
        try:
            # 覆盖率已经够的中文字体不要再砸
            if path.exists():
                try:
                    if check_font(path).level == "ok" and check_font(path).family == family:
                        ok = True
                        continue
                except OSError:
                    pass
            write_renamed(cjk_src, path, family)
            ok = True
        except (TtfRenameError, OSError):
            continue
    return ok


def _scripts_path(dest: Path) -> Path | None:
    for name in ("Scripts.rvdata2", "Scripts.rvdata", "Scripts.rxdata"):
        cand = dest / "Data" / name
        if cand.is_file():
            return cand
    return None


def _bump_font_size(dest: Path, size: int) -> bool:
    scripts_path = _scripts_path(dest)
    if scripts_path is None:
        return False
    try:
        scripts = rm.load(scripts_path.read_bytes())
    except (rm.MarshalError, OSError):
        return False
    size_pat = re.compile(r"Font\.default_size\s*=\s*\d+")
    changed = False
    for item in scripts if isinstance(scripts, list) else []:
        if not isinstance(item, list) or len(item) < 3 or item[1] != "Main":
            continue
        body = item[2]
        if not isinstance(body, (bytes, bytearray)):
            continue
        try:
            text = zlib.decompress(body).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            continue
        if not size_pat.search(text):
            continue
        if re.search(rf"Font\.default_size\s*=\s*{size}\b", text):
            return False
        text2 = size_pat.sub(f"Font.default_size = {size}", text, count=1)
        item[2] = zlib.compress(text2.encode("utf-8"))
        changed = True
        break
    if not changed:
        return False
    try:
        scripts_path.write_bytes(rm.dump(scripts))
    except (rm.MarshalError, OSError):
        return False
    return True


def _ruby_string_array(names: list[str]) -> str:
    return "[" + ", ".join(f'"{n}"' for n in names) + "]"


def _patch_font_names(dest: Path, faces: list[str]) -> bool:
    """改 Main 的 Font.default_name，以及脚本里硬编码的 FONT = \"...\"。"""
    if not faces:
        return False
    scripts_path = _scripts_path(dest)
    if scripts_path is None:
        return False
    try:
        scripts = rm.load(scripts_path.read_bytes())
    except (rm.MarshalError, OSError):
        return False
    name_pat = re.compile(r"Font\.default_name\s*=\s*(?:\[[^\]]*\]|\"[^\"]+\")")
    const_pat = re.compile(r'(FONT\s*=\s*)\"[^\"]+\"')
    replacement = f"Font.default_name = {_ruby_string_array(faces)}"
    primary = faces[0]
    changed = False
    for item in scripts if isinstance(scripts, list) else []:
        if not isinstance(item, list) or len(item) < 3:
            continue
        body = item[2]
        if not isinstance(body, (bytes, bytearray)):
            continue
        try:
            text = zlib.decompress(body).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            continue
        if not text.strip():
            continue
        text2 = text
        if item[1] == "Main" or name_pat.search(text2):
            if name_pat.search(text2):
                text2 = name_pat.sub(replacement, text2, count=1)
            elif item[1] == "Main":
                text2 = replacement + "\n" + text2
        if const_pat.search(text2):
            text2 = const_pat.sub(rf'\1"{primary}"', text2)
        if text2 == text:
            continue
        item[2] = zlib.compress(text2.encode("utf-8"))
        changed = True
    if not changed:
        return False
    try:
        scripts_path.write_bytes(rm.dump(scripts))
    except (rm.MarshalError, OSError):
        return False
    return True


def _patch_default_font_ascii(dest: Path, family: str) -> bool:
    """兼容旧调用：只写一个族名。"""
    return _patch_font_names(dest, [family])


def _patch_reset_font_name(dest: Path) -> bool:
    """标准 Window_Base#reset_font_settings 不重设 font.name。

    若某处 Bitmap 在 Font.default_name 改完之前就 create_contents，
    会一直用旧族名。补一行 contents.font.name = Font.default_name。
    """
    scripts_path = _scripts_path(dest)
    if scripts_path is None:
        return False
    try:
        scripts = rm.load(scripts_path.read_bytes())
    except (rm.MarshalError, OSError):
        return False
    needle = "contents.font.name = Font.default_name"
    changed = False
    for item in scripts if isinstance(scripts, list) else []:
        if not isinstance(item, list) or len(item) < 3 or item[1] != "Window_Base":
            continue
        body = item[2]
        if not isinstance(body, (bytes, bytearray)):
            continue
        try:
            text = zlib.decompress(body).decode("utf-8")
        except (zlib.error, UnicodeDecodeError):
            continue
        if needle in text:
            return False
        # 插在 reset_font_settings 方法体内、第一条 contents.font.* 之前
        # （有的作会先 change_color，不能假定 size 是第一行）
        pat = re.compile(
            r"([ \t]*def reset_font_settings\r?\n)(.*?)([ \t]*contents\.font\.)",
            re.DOTALL,
        )
        m = pat.search(text)
        if not m:
            continue
        # 别跨到下一个方法
        if "def " in m.group(2):
            continue
        nl = "\r\n" if "\r\n" in text else "\n"
        indent_m = re.search(r"[ \t]*contents\.font\.", m.group(0))
        indent = indent_m.group(0).split("contents")[0] if indent_m else "    "
        line = f"{indent}contents.font.name = Font.default_name{nl}"
        text2 = text[: m.start(3)] + line + text[m.start(3) :]
        item[2] = zlib.compress(text2.encode("utf-8"))
        changed = True
        break
    if not changed:
        return False
    try:
        scripts_path.write_bytes(rm.dump(scripts))
    except (rm.MarshalError, OSError):
        return False
    return True


# ---- 文本遍历 / 指针 ----


def iter_texts(stem: str, filename: str, payload):
    if stem == "System":
        yield from _system_texts(payload)
        return
    if stem == "MapInfos":
        yield from _mapinfo_texts(payload)
        return
    if _MAP_FILE.match(filename) or stem == "CommonEvents" or stem == "Troops":
        yield from _event_texts(payload)
        return
    if stem in _DB_FILES:
        yield from _db_texts(payload)
        return


def _system_texts(payload):
    if not isinstance(payload, dict):
        return
    title = payload.get("game_title")
    if _worth(title):
        yield ("game_title", title, "title")
    currency = payload.get("currency_unit")
    if _worth(currency):
        yield ("currency_unit", currency, "term")
    terms = payload.get("terms")
    if isinstance(terms, dict):
        for key, val in terms.items():
            if key.startswith("__"):
                continue
            if isinstance(val, str) and _worth(val):
                yield (f"terms/{key}", val, "term")
            elif isinstance(val, list):
                for i, item in enumerate(val):
                    if _worth(item):
                        yield (f"terms/{key}/{i}", item, "term")


def _mapinfo_texts(payload):
    # Ace 的 MapInfos 是 {id: RPG::MapInfo}；XP 有时是数组
    if isinstance(payload, dict):
        for key, item in payload.items():
            if key == "__class__" or not isinstance(item, dict):
                continue
            name = item.get("name")
            if _worth(name):
                yield (f"{key}/name", name, "mapname")
    elif isinstance(payload, list):
        for i, item in enumerate(payload):
            if isinstance(item, dict) and _worth(item.get("name")):
                yield (f"{i}/name", item["name"], "mapname")


def _db_texts(payload):
    if not isinstance(payload, list):
        return
    for i, item in enumerate(payload):
        if not isinstance(item, dict):
            continue
        for field in _DB_FIELDS:
            text = item.get(field)
            if _worth(text):
                yield (f"{i}/{field}", text, "db")


def _event_texts(payload):
    out = []

    def visit(node, base: str):
        if isinstance(node, dict):
            if node.get("__class__") == "RPG::EventCommand":
                code = node.get("code")
                params = node.get("parameters")
                if isinstance(params, list):
                    where = f"{base}/parameters" if base else "parameters"
                    # base 已含 list/i
                    if code in (_CODE_TEXT, _CODE_SCROLL) and params and _worth(params[0]):
                        out.append((f"{where}/0", params[0], "dialogue"))
                    elif code in (_CODE_NAME, _CODE_NICKNAME, _CODE_PROFILE) and params and _worth(params[0]):
                        out.append((f"{where}/0", params[0], "name"))
                    elif code == _CODE_CHOICES and params and isinstance(params[0], list):
                        for slot, text in enumerate(params[0]):
                            if _worth(text):
                                out.append((f"{where}/0/{slot}", text, "choice"))
                return
            instructions = node.get("list")
            if isinstance(instructions, list):
                for index, instruction in enumerate(instructions):
                    visit(instruction, f"{base}/list/{index}" if base else f"list/{index}")
            for key, value in node.items():
                if key in ("__class__", "list"):
                    continue
                visit(value, f"{base}/{key}" if base else str(key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, f"{base}/{index}" if base else str(index))

    visit(payload, "")
    yield from out


def set_pointer(root, pointer: str, value) -> None:
    tokens = [t for t in pointer.split("/") if t != ""]
    if not tokens:
        raise ValueError("空指针")
    node = root
    for token in tokens[:-1]:
        node = _step(node, token)
    last = tokens[-1]
    if isinstance(node, list):
        node[int(last)] = value
    elif isinstance(node, dict):
        # hash 键可能是 int（MapInfos / events）
        if last not in node:
            try:
                alt = int(last)
            except ValueError:
                alt = None
            if alt is not None and alt in node:
                node[alt] = value
                return
            # 对象字段
            if last not in node:
                raise KeyError(last)
        node[last] = value
    else:
        raise TypeError(type(node).__name__)


def _step(node, token: str):
    if isinstance(node, list):
        return node[int(token)]
    if isinstance(node, dict):
        if token in node:
            return node[token]
        try:
            alt = int(token)
        except ValueError:
            alt = None
        if alt is not None and alt in node:
            return node[alt]
        raise KeyError(token)
    raise TypeError(type(node).__name__)
