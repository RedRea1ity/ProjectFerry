"""最底层：可写目录、日志、脱敏、原子写文件。

这一层**不依赖包内任何其它模块**，谁都可以放心 import 它。
"""

from __future__ import annotations

import logging
import os
import re
import sys
import tempfile
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
        record.exc_info = None
        record.exc_text = None
        if record.stack_info:
            record.stack_info = None
        return True


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
