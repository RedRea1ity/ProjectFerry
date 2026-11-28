"""引擎适配器 —— 摆渡的「外挂模组」。

本体只认识 Minecraft。每多一个引擎，就往这个目录里加一个文件，
**不改 `scan` / `packio` / `engine` / `cli` 的任何逻辑**。

    detect(root)  -> Adapter | None     自动认领
    get(name)     -> Adapter | None     按名字取

新增一个引擎的顺序：
1. 在 `container` 里确认它的数据装在哪儿（散装 / zip-in-exe / 需要打包器）
2. 新建 `<engine>.py`，实现 probe / collect / apply
3. 加进下面的 `REGISTRY`
4. 写测试：至少证明「抽出来的条数」和「写回去以后文件仍能被解析」

`probe` 答不出 `writable` 的引擎，不要加进来 —— 翻了也白翻。
"""

from __future__ import annotations

from pathlib import Path

from .ags import AgsAdapter, inspect_ags_archive, parse_tra, rebuild_tra
from .base import Adapter, ApplyReport, ProbeReport, VerifyReport
from .blackberry import BlackBerryAdapter, is_blackberry_bar, is_blackberry_cod
from .console import (
    NdsAdapter,
    PspAdapter,
    PsvAdapter,
    compress_cso,
    decompress_cso,
    parse_iso9660,
    parse_nds_rom,
    parse_param_sfo,
)
from .container import (
    AsarContainer,
    Container,
    Document,
    FolderGame,
    ZipInExe,
    find_asar,
    locate_appended_zip,
    open_container,
    parse_asar,
    rebuild_asar,
    unique_output,
)
from .gamemaker import GameMakerAdapter
from .godot import GodotAdapter
from .hints import scan_hints
from .hsp import HspAdapter
from .i18n_table import I18nTableAdapter
from .j2me import J2meAdapter, extract_class_utf8_strings, rebuild_class_utf8_strings
from .kirikiri import KirikiriAdapter
from .love2d import Love2dAdapter
from .mobile import (
    ApkAdapter,
    IpaAdapter,
    extract_arsc_strings,
    extract_dex_strings,
    patch_arsc_strings,
    patch_dex_strings,
)
from .narrative import NarrativeAdapter
from .playstation import Ps1Adapter, Ps2Adapter, parse_iso9660_stream, parse_ps_system_cnf
from .renpy import RenpyAdapter
from .retro_console import (
    N64Adapter,
    SaturnAdapter,
    compute_cdrom_edc,
    compute_n64_crc,
    detect_n64_cic,
    detect_n64_format,
    fix_n64_crc,
    normalize_n64_rom,
    parse_n64_header,
    parse_saturn_disc,
    parse_saturn_header,
    recompute_cdrom_edc_ecc,
    restore_n64_format,
)
from .retro_win import RetroWinAdapter, is_alicesoft_ald, is_director_container, is_win16_ne_executable
from .rpgmaker import RpgMakerAdapter
from .rpgmaker_2k import RpgMaker2kAdapter
from .rpgmaker_ace import RpgMakerAceAdapter
from .symbian import (
    SymbianAdapter,
    extract_sisx_compressed_blocks,
    is_symbian_e32image,
    is_symbian_sis,
    parse_qt_qm,
    rebuild_qt_qm,
    rebuild_sisx_with_blocks,
)
from .tyrano import TyranoAdapter
from .undo import UNDO_NAME, RevertReport, UndoLog
from .undo import revert as revert_game
from .unity import (
    UnityAdapter,
    extract_il2cpp_metadata_strings,
    rebuild_il2cpp_metadata,
)
from .unreal import (
    UnrealAdapter,
    inspect_pak,
    normalize_aes_key,
    parse_locres,
    parse_uasset_stringtable,
    rebuild_locres,
    rebuild_uasset_stringtable,
)
from .vn_engines import ArtemisAdapter, OnscripterAdapter, WolfRpgAdapter
from .flash import FlashAdapter, extract_swf_strings, rebuild_swf_strings

# 数字小的先探测。认领越具体的排越前，通用兜底排最后。
# 注意：PC98Adapter / RawBinaryAdapter 位于 ferrygame 包，延迟加载以避免
# 先 import ferrygame 时与 ferrycore.adapters.base 形成循环导入。
_CORE_REGISTRY: list[type[Adapter]] = [
    ApkAdapter,
    IpaAdapter,
    J2meAdapter,
    BlackBerryAdapter,
    SymbianAdapter,
    PsvAdapter,
    PspAdapter,
    NdsAdapter,
    SaturnAdapter,
    Ps1Adapter,
    Ps2Adapter,
    N64Adapter,
    FlashAdapter,
    RetroWinAdapter,
    OnscripterAdapter,
    KirikiriAdapter,
    WolfRpgAdapter,
    ArtemisAdapter,
    TyranoAdapter,
    RenpyAdapter,
    RpgMaker2kAdapter,
    RpgMakerAceAdapter,  # rgss3a / rvdata2，必须在 MV/MZ 明文适配器之前
    Love2dAdapter,
    RpgMakerAdapter,
    UnrealAdapter,
    GodotAdapter,
    UnityAdapter,
    AgsAdapter,
    NarrativeAdapter,
    GameMakerAdapter,
    I18nTableAdapter,
    HspAdapter,
]

REGISTRY: list[type[Adapter]] = list(_CORE_REGISTRY)
_EXTRAS_LOADED = False


def _ensure_extra_adapters() -> list[type[Adapter]]:
    global _EXTRAS_LOADED
    if not _EXTRAS_LOADED:
        from ferrygame.pc98 import PC98Adapter
        from ferrygame.rawbin import RawBinaryAdapter

        if PC98Adapter not in REGISTRY:
            try:
                idx = REGISTRY.index(FlashAdapter)
            except ValueError:
                idx = len(REGISTRY)
            REGISTRY.insert(idx, PC98Adapter)
        if RawBinaryAdapter not in REGISTRY:
            REGISTRY.append(RawBinaryAdapter)
        _EXTRAS_LOADED = True
    return REGISTRY


def __getattr__(name: str):
    if name == "PC98Adapter":
        from ferrygame.pc98 import PC98Adapter
        return PC98Adapter
    if name == "RawBinaryAdapter":
        from ferrygame.rawbin import RawBinaryAdapter
        return RawBinaryAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def registered() -> list[str]:
    return [cls.name for cls in _ensure_extra_adapters()]


def _build(cls: type[Adapter], options: dict) -> Adapter:
    """按需把选项喂给构造器；适配器不收这个选项就退回无参构造。"""
    try:
        return cls(**options)
    except TypeError:
        return cls()


def get(name: str, **options) -> Adapter | None:
    for cls in _ensure_extra_adapters():
        if cls.name == name:
            return _build(cls, options)
    return None


_HSP_RUNTIME_MARKERS = ("hsp3imp.dll", "hspda.dll", "hspext.dll", "hspogg.dll", "hsp3utf.dll")


def _match_dir_launcher_target(exe_path: Path, options: dict) -> tuple[Path, Adapter] | None:
    """仅当拖入的是多文件引擎启动器 `.exe` 且同目录存在该引擎专属目录结构时，回退到对应目录/同名包。

    绝不扫描同目录里的其他独立游戏文件（如 .apk/.nds/.jar/.swf/其他 .exe），
    防止在下载目录或合集目录里拖入一个文件却误识别成别的游戏。
    """
    parent = exe_path.parent
    if not parent.is_dir():
        return None

    # 1) Godot 同名 .pck（精准锁定与 exe 同名的包，不碰同目录其他 .pck）
    sibling_pck = parent / f"{exe_path.stem}.pck"
    if sibling_pck.is_file():
        adapter = _build(GodotAdapter, options)
        try:
            if adapter.probe(sibling_pck).readable:
                return sibling_pck, adapter
        except Exception:
            pass

    # 2) 仅检查明确的多文件目录型引擎特征
    candidates: list[type[Adapter]] = []
    if (parent / "game").is_dir() and (
        (parent / "renpy").is_dir()
        or any((parent / "game").glob("*.rpy"))
        or any((parent / "game").glob("*.rpa"))
    ):
        candidates.append(RenpyAdapter)
    if (parent / "RPG_RT.ldb").is_file():
        candidates.append(RpgMaker2kAdapter)
    if any(
        (parent / name).is_file()
        for name in ("Game.rgss3a", "Game.rgss2a", "Game.rgssad")
    ) or any(
        (parent / "Data" / name).is_file()
        for name in ("MapInfos.rvdata2", "MapInfos.rvdata", "MapInfos.rxdata")
    ):
        candidates.append(RpgMakerAceAdapter)
    if (parent / "www" / "data" / "System.json").is_file() or (parent / "data" / "System.json").is_file():
        candidates.append(RpgMakerAdapter)
    if (parent / f"{exe_path.stem}_Data").is_dir():
        candidates.append(UnityAdapter)
    if (parent / "data.win").is_file() or (parent / "game.win").is_file():
        candidates.append(GameMakerAdapter)
    if any((parent / dll).is_file() for dll in _HSP_RUNTIME_MARKERS):
        candidates.append(HspAdapter)
    if (parent / "Data.wolf").is_file() or (parent / "Data" / "BasicData").is_dir():
        candidates.append(WolfRpgAdapter)
    if (parent / "data" / "scenario").is_dir() or (parent / "resources" / "app.asar").is_file():
        candidates.append(TyranoAdapter)
    if (parent / "data" / "system" / "Initialize.tjs").is_file() or (
        parent / "data" / "system" / "Config.tjs"
    ).is_file():
        candidates.append(KirikiriAdapter)
    if (parent / "nscript.dat").is_file() or (parent / "0.txt").is_file():
        candidates.append(OnscripterAdapter)

    for cls in candidates:
        adapter = _build(cls, options)
        try:
            if adapter.probe(parent).readable:
                return parent, adapter
        except Exception:
            continue
    return None


def resolve_game_target(root: Path, **options) -> tuple[Path, Adapter | None]:
    """精准识别目标路径（单文件或目录），返回 `(resolved_root, adapter)`。

    - 若传入单个文件：优先只针对该文件本身做全适配器探测，绝不误扫同目录其他文件；
    - 仅当传入 `.exe` 且其本身不是自包含单文件游戏（也不是诊断命中的加密包/GM8/AGS）时，
      才检查其父目录是否为对应的多文件目录型引擎（RPG Maker / Ren'Py / Unity 等）；
    - 最后再对精确文件尝试 `rawbin` 兜底。
    """
    target = Path(root)
    if not target.is_file():
        return target, detect(target, **options)

    ordered = sorted(_ensure_extra_adapters(), key=lambda c: c.priority)
    rawbin_cls: type[Adapter] | None = None
    diagnosed: Adapter | None = None

    for cls in ordered:
        if cls.name == "rawbin":
            rawbin_cls = cls
            continue
        adapter = _build(cls, options)
        try:
            report = adapter.probe(target)
            if report.readable:
                return target, adapter
            if diagnosed is None and (report.needs or report.container):
                diagnosed = adapter
        except Exception:
            continue

    if diagnosed is not None:
        return target, diagnosed

    if target.suffix.lower() == ".exe":
        launcher_match = _match_dir_launcher_target(target, options)
        if launcher_match is not None:
            return launcher_match

    if rawbin_cls is not None:
        adapter = _build(rawbin_cls, options)
        try:
            if adapter.probe(target).readable:
                return target, adapter
        except Exception:
            pass

    return target, None


def detect(root: Path, **options) -> Adapter | None:
    """交给第一个认领成功的适配器。认不出就返回 None —— 别猜。

    若某个具体引擎适配器明确识别出容器/依赖（如加密 UE `.pak` 缺少 `repak` 或 AES 密钥），
    优先返回该适配器以展示精准诊断，避免被最后的裸二进制兜底误吞。
    """
    diagnosed: Adapter | None = None
    for cls in sorted(_ensure_extra_adapters(), key=lambda c: c.priority):
        if cls.name == "rawbin" and diagnosed is not None:
            return diagnosed
        adapter = _build(cls, options)
        try:
            report = adapter.probe(Path(root))
            if report.readable:
                return adapter
            if diagnosed is None and (report.needs or report.container):
                diagnosed = adapter
        except Exception:
            # 单个适配器炸了不该拖垮整轮探测
            continue
    return diagnosed


__all__ = [
    "Adapter",
    "AgsAdapter",
    "ApkAdapter",
    "ApplyReport",
    "ArtemisAdapter",
    "AsarContainer",
    "BlackBerryAdapter",
    "Container",
    "Document",
    "FlashAdapter",
    "FolderGame",
    "GameMakerAdapter",
    "GodotAdapter",
    "HspAdapter",
    "I18nTableAdapter",
    "IpaAdapter",
    "J2meAdapter",
    "KirikiriAdapter",
    "Love2dAdapter",
    "N64Adapter",
    "NarrativeAdapter",
    "NdsAdapter",
    "OnscripterAdapter",
    "PC98Adapter",
    "ProbeReport",
    "Ps1Adapter",
    "Ps2Adapter",
    "PspAdapter",
    "PsvAdapter",
    "RawBinaryAdapter",
    "RenpyAdapter",
    "RetroWinAdapter",
    "RevertReport",
    "RpgMaker2kAdapter",
    "RpgMakerAceAdapter",
    "RpgMakerAdapter",
    "SaturnAdapter",
    "SymbianAdapter",
    "TyranoAdapter",
    "UNDO_NAME",
    "UndoLog",
    "UnityAdapter",
    "UnrealAdapter",
    "VerifyReport",
    "WolfRpgAdapter",
    "ZipInExe",
    "compress_cso",
    "compute_cdrom_edc",
    "compute_n64_crc",
    "decompress_cso",
    "detect",
    "detect_n64_cic",
    "detect_n64_format",
    "extract_arsc_strings",
    "extract_dex_strings",
    "extract_il2cpp_metadata_strings",
    "find_asar",
    "fix_n64_crc",
    "get",
    "inspect_ags_archive",
    "inspect_pak",
    "locate_appended_zip",
    "normalize_aes_key",
    "normalize_n64_rom",
    "open_container",
    "parse_asar",
    "parse_iso9660",
    "parse_iso9660_stream",
    "parse_locres",
    "parse_n64_header",
    "parse_nds_rom",
    "parse_param_sfo",
    "parse_ps_system_cnf",
    "parse_saturn_disc",
    "parse_saturn_header",
    "parse_tra",
    "parse_uasset_stringtable",
    "patch_arsc_strings",
    "patch_dex_strings",
    "rebuild_asar",
    "rebuild_il2cpp_metadata",
    "rebuild_locres",
    "rebuild_tra",
    "rebuild_uasset_stringtable",
    "recompute_cdrom_edc_ecc",
    "registered",
    "resolve_game_target",
    "restore_n64_format",
    "revert_game",
    "scan_hints",
    "unique_output",
]
