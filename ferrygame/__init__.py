"""摆渡 · 通用游戏线（ferrygame）。

跟 MC 模组汉化是两条路，从这里起正式分家：

    ferrycore/   MC 主线：实例 → 模组 → 资源包。也提供最底层的「调一次模型」。
    ferrygame/   通用游戏线：游戏目录 → 文本单元 → 译文表（ferry_tl.json）→ 写回游戏。

主攻对象是**小众、猎奇、没人做的游戏**：单 exe、自研引擎、怪后缀数据包、
PC-98 磁盘镜像（.hdi / .fdi / .nhd），聚焦**剧情 + 操作菜单**。
"""

from . import controls, pc98, pipeline, profile, project, rawbin, recon, tbl, translate, units
from .pc98 import PC98Adapter, PC98FontMapper
from .pipeline import (
    GameRunResult,
    apply_project,
    detect_adapter,
    get_adapter,
    registered_adapters,
    run_game,
    scan_game,
    translate_project,
)
from .project import (
    TL_NAME,
    TSV_NAME,
    Project,
    export_tsv,
    import_external_translations,
    load,
    merge,
)
from .rawbin import (
    RawBinaryAdapter,
    encode_padded,
    patch_blob,
    scan_binary_strings,
    scan_sjis_strings,
    scan_utf8_strings,
    scan_utf16le_strings,
)
from .recon import ReconReport, inspect_game, shannon_entropy
from .tbl import (
    RelativeHit,
    ThingyTable,
    encode_tbl_padded,
    find_tbl_pair,
    load_tbl,
    parse_tbl,
    relative_search,
    scan_tbl_strings,
)
from .translate import TranslateReport, translate_units
from .units import Unit, classify, detect_source_lang

__all__ = [
    "GameRunResult",
    "PC98Adapter",
    "PC98FontMapper",
    "Project",
    "RawBinaryAdapter",
    "ReconReport",
    "RelativeHit",
    "TL_NAME",
    "TSV_NAME",
    "ThingyTable",
    "TranslateReport",
    "Unit",
    "apply_project",
    "classify",
    "controls",
    "detect_adapter",
    "detect_source_lang",
    "encode_padded",
    "encode_tbl_padded",
    "export_tsv",
    "find_tbl_pair",
    "get_adapter",
    "import_external_translations",
    "inspect_game",
    "load",
    "load_tbl",
    "merge",
    "parse_tbl",
    "patch_blob",
    "pc98",
    "pipeline",
    "profile",
    "project",
    "rawbin",
    "recon",
    "registered_adapters",
    "relative_search",
    "run_game",
    "scan_game",
    "scan_sjis_strings",
    "scan_tbl_strings",
    "shannon_entropy",
    "tbl",
    "translate",
    "translate_project",
    "translate_units",
    "units",
]
