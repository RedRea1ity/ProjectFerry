"""最底层：可写目录、日志、脱敏、原子写文件。

这一层**不依赖包内任何其它模块**，谁都可以放心 import 它。
"""

from __future__ import annotations

import logging
import os
import re
import struct
import sys
import tempfile
import traceback
import zipfile
import zlib
import json
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

# 全包共用一个 logger 名：各处 LOG.xxx 都写进同一份 ferry.log，
# 测试里 assertLogs(core.LOG) 也能一次抓到所有模块的记录。
LOGGER_NAME = "ferry"

# AI 生成的资源包内会写入这个标记文件，用于下次扫描时把它和社区/官方汉化区分开。
AI_MARKER = "AI_TRANSLATION_NOTICE.txt"

# 资源包内的这个文件记录「已强制还原英文」的键：这些键在 zh_cn.json 里是英文，
# 扫描时不算作已翻译，也不会再被“翻译全部”重新翻译。
REVERT_MARKER = "FERRY_REVERTED.json"

SOURCES_MARKER = "FERRY_SOURCES.json"

# 结构化 FTB 译文（富文本数组）的英文原稿记录：只用来判断任务书英文是否已改动。
FTB_SOURCES_MARKER = "FERRY_FTB_SOURCES.json"

COMMUNITY_NOTICE = "COMMUNITY_ATTRIBUTION.txt"


def app_dir() -> Path:
    """可写数据目录：源码运行时是脚本目录；打包成 exe 后是 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # 本文件在 <仓库>/ferrycore/ 下；数据目录仍是仓库根（与拆包前一致）。
    return Path(__file__).resolve().parent.parent

CONFIG_FILE = app_dir() / "ferry_config.json"

PROGRESS_FILE = app_dir() / "ferry_progress.json"

USER_GLOSSARY_FILE = app_dir() / "user_glossary.json"

LOCKS_FILE = app_dir() / "ferry_locks.json"

LOG = logging.getLogger(LOGGER_NAME)

LOG_FILE = app_dir() / "ferry.log"


def redact_secrets(text: str) -> str:
    text = re.sub(r"(?i)(?:sk-|AIza|key-)[A-Za-z0-9_-]{8,}", "sk-***", text)
    return re.sub(r"(?i)(api[_-]?key|authorization)\s*[:=]\s*\S+", r"\1=sk-***", text)


class _RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(record.getMessage())
        record.args = ()
        if record.exc_info and record.exc_info[0] is not None:
            formatted = "".join(traceback.format_exception(*record.exc_info)).rstrip()
            record.exc_text = redact_secrets(formatted)
        elif record.exc_text:
            record.exc_text = redact_secrets(record.exc_text)
        record.exc_info = None
        if record.stack_info:
            record.stack_info = redact_secrets(str(record.stack_info))
        return True


# Windows / POSIX 常见错误码人话说明表：(简明结论, 原因与排查建议)
_WINERROR_HINTS: dict[int, tuple[str, str]] = {
    2: ("找不到指定文件", "目标文件不存在，可能已被移动、删除，或被杀毒软件隔离。"),
    3: ("找不到指定路径", "目标目录不存在，或路径过长（超过 Windows 260 字符上限）。"),
    5: ("没有文件读写权限（拒绝访问）", "文件可能是只读属性、位于受保护目录（如 Program Files），或被 Windows Defender / 杀毒软件拦截了对游戏文件的修改。请关闭游戏、检查只读属性或把游戏移到普通文件夹后重试。"),
    14: ("系统内存不足或底层写入被拒（错误码 14）", "常见原因：① 单个游戏文件过大导致内存不足；② 杀毒软件 / 受控文件夹保护拦截了对 .exe 或数据文件的修改；③ 目标二进制被加壳或受特殊保护。建议关闭占用内存的程序、把游戏目录加入杀软白名单，或在「高级设置」里改用旁路输出目录而非原地改写。"),
    17: ("跨盘符移动文件失败", "源目录和输出目录位于不同磁盘分区，系统无法直接原子移动。"),
    32: ("文件正被其他程序占用", "游戏本身仍在运行，或文件被资源管理器 / 杀毒软件锁住。请先完全关闭游戏进程后再试。"),
    33: ("文件部分区域被锁定", "目标文件正被另一个进程锁定，请先关闭游戏或相关编辑器后再试。"),
    112: ("磁盘空间不足", "当前磁盘剩余空间不够保存备份或汉化副本，请清理硬盘空间后重试。"),
    123: ("文件名或路径包含非法字符", "路径中含有 Windows 不允许的特殊字符或无效符号链接，建议把游戏文件夹改名为纯中英文后重试。"),
    183: ("目标文件或目录已存在", "同名输出文件或目录已存在，无法重复创建。"),
    206: ("文件路径过长", "路径超过 Windows 260 字符限制，请把游戏文件夹移到更浅的目录（例如 D:\\Games\\）再试。"),
    225: ("被杀毒软件 / Windows 安全中心拦截", "修改 .exe 或游戏数据文件时触发了杀软误报拦截，请将游戏目录加入白名单后重试。"),
    577: ("系统拒绝加载未签名文件", "Windows 安全策略阻止了该文件操作，请检查安全中心拦截记录。"),
}

_ERRNO_HINTS: dict[int, tuple[str, str]] = {
    2: ("找不到指定文件或目录", "目标文件不存在，可能已被移动、删除，或路径过长。"),
    12: ("系统内存不足", "可用内存不足以一次性读取当前大文件，请关闭其他占用内存的程序后重试。"),
    13: ("没有文件读写权限（拒绝访问）", "文件为只读、游戏仍在运行、目录受系统保护，或被杀毒软件拦截了改写。请关闭游戏并检查目录权限/杀软拦截记录。"),
    14: ("系统底层读写地址异常（错误码 14）", "系统调用读写文件或内存时被拒绝/地址异常，常见于文件被加壳保护、受控文件夹或杀毒软件拦截、或超大文件导致资源不足。建议检查杀软拦截记录或换到普通非桌面目录重试。"),
    17: ("目标文件或目录已存在", "输出位置已有同名文件或目录冲突。"),
    22: ("路径或参数无效", "文件路径含非法字符、或目标编码/系统调用参数不被支持。"),
    28: ("磁盘空间不足", "磁盘剩余空间不够写入备份或译文，请清理空间后重试。"),
    36: ("文件路径过长", "路径超过系统长度限制，请把游戏移到更短的路径（如 D:\\Games\\）再试。"),
}

_CODE_IN_TEXT_RE = re.compile(r"(?i)(?:winerror|errno|error(?:\s+code)?)\s*[:=#]?\s*(-?\d+)")


def explain_error(exc: BaseException, stage: str = "") -> str:
    """把 Python / Windows 底层异常翻译成普通玩家一眼能看懂的中文报错说明。

    始终包含：出错阶段 + 人话原因 + 排查建议 + 涉及文件（如有）+ 原始技术详情。
    """
    stage_name = stage or str(getattr(exc, "_ferry_stage", "") or "").strip()
    prefix = f"【{stage_name}失败】" if stage_name else "【操作失败】"
    raw_msg = redact_secrets(str(exc)).strip()
    exc_type = type(exc).__name__

    headline = ""
    advice = ""
    filename = ""

    if isinstance(exc, OSError):
        filename = str(getattr(exc, "filename", "") or "")
        winerr = getattr(exc, "winerror", None)
        errnum = getattr(exc, "errno", None)
        if isinstance(winerr, int) and winerr in _WINERROR_HINTS:
            headline, advice = _WINERROR_HINTS[winerr]
        elif isinstance(errnum, int) and errnum in _ERRNO_HINTS:
            headline, advice = _ERRNO_HINTS[errnum]
        elif isinstance(exc, PermissionError):
            headline, advice = _ERRNO_HINTS[13]
        elif isinstance(exc, FileNotFoundError):
            headline, advice = _ERRNO_HINTS[2]
        elif isinstance(exc, FileExistsError):
            headline, advice = _ERRNO_HINTS[17]
        else:
            code_tag = f"WinError {winerr}" if winerr else (f"Errno {errnum}" if errnum else "系统 I/O 错误")
            headline = f"文件读写失败（{code_tag}）"
            advice = "请确认游戏已完全关闭、目录没有只读限制、未被杀毒软件拦截，且磁盘空间充足。"
    elif isinstance(exc, MemoryError):
        headline = "内存不足"
        advice = "游戏单个文件体积过大，无法一次性读入内存。请关闭其他占用内存的程序后重试。"
    elif isinstance(exc, UnicodeEncodeError):
        enc = getattr(exc, "encoding", "") or "目标编码"
        headline = f"中文字符无法按 {enc} 编码写回"
        advice = "该游戏引擎使用的旧字库/编码装不下部分简体汉字，需要更换字体/编码方案或缩短/改写对应译文。"
    elif isinstance(exc, UnicodeDecodeError):
        enc = getattr(exc, "encoding", "") or "默认编码"
        headline = f"无法按 {enc} 解码游戏文本"
        advice = "该文件实际编码与预期不符，或文件本身是压缩/加密二进制数据。"
    elif isinstance(exc, zipfile.BadZipFile):
        headline = "压缩包或资源包已损坏"
        advice = "目标文件不是有效的 ZIP 压缩包，可能下载不完整或被加密加壳。"
    elif isinstance(exc, json.JSONDecodeError):
        headline = "JSON 文件格式损坏"
        advice = f"第 {exc.lineno} 行第 {exc.colno} 列附近语法不合法，文件可能被截断或手动改坏。"
    elif isinstance(exc, (zlib.error, struct.error)):
        headline = "游戏数据格式解析失败"
        advice = "该游戏文件可能经过加密、加壳，或引擎数据结构与当前适配器版本不一致。"
    elif isinstance(exc, (KeyError, IndexError)):
        headline = "数据结构与预期不符（缺少字段或索引越界）"
        advice = "这通常是该游戏格式特殊或程序内部兼容边界导致的；你可以点「一键诊断」把诊断包发给作者排查。"
    else:
        # 兜底检查异常文本里是否藏了诸如 "error 14" / "errno 13" 之类晦涩错误码。
        match = _CODE_IN_TEXT_RE.search(raw_msg)
        if match:
            code = int(match.group(1))
            if code in _WINERROR_HINTS:
                headline, advice = _WINERROR_HINTS[code]
            elif code in _ERRNO_HINTS:
                headline, advice = _ERRNO_HINTS[code]
        if not headline:
            # 如果本身已经是可读中文业务报错（如 401 密钥错、缺依赖等），直接主展示。
            has_chinese = bool(re.search(r"[\u4e00-\u9fff]", raw_msg))
            if has_chinese and raw_msg:
                headline = raw_msg
                advice = ""
            else:
                headline = f"遇到未预期的错误（{exc_type}）"
                advice = "该游戏格式可能不受当前流程支持，或触发了内部异常。可查看「运行日志」或点「一键诊断」获取完整堆栈。"

    lines = [f"{prefix}{headline}"]
    if advice:
        lines.append(f"\n原因与建议：{advice}")
    if filename:
        lines.append(f"涉及文件：{filename}")
    detail = f"{exc_type}: {raw_msg}" if raw_msg else exc_type
    if detail and detail != headline and raw_msg != headline:
        lines.append(f"\n技术详情：{detail}")
    lines.append("（完整堆栈已脱敏写入 ferry.log，可在「运行日志」或「一键诊断」中查看）")
    return "\n".join(lines)


def configure_logging() -> None:
    if LOG.handlers:
        return
    handler = TimedRotatingFileHandler(LOG_FILE, when="midnight", backupCount=7, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(_RedactSecrets())
    LOG.addHandler(handler)
    LOG.setLevel(logging.DEBUG)


def atomic_write_text(path: Path, text: str) -> None:
    """先写同目录临时文件，完整写入后再替换，避免留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(text)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

# 导入即生效：把脱敏过滤器挂到文件日志上。
configure_logging()
