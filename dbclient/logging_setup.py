"""统一日志初始化。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import LOG_FILE

_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_initialized = False


def init_logging(level: int = logging.INFO) -> None:
    """初始化根 logger：文件（轮转）+ 控制台。重复调用无副作用。"""
    global _initialized
    if _initialized:
        return
    _initialized = True

    root = logging.getLogger()
    root.setLevel(level)

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)

    try:
        # 防御：log 文件所在目录可能因配置变更而不存在
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        root.addHandler(fh)
    except Exception:  # noqa: BLE001
        pass  # 日志文件不可写时静默降级

    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(formatter)
    ch.setLevel(logging.WARNING)  # 控制台只输出 WARNING 及以上
    root.addHandler(ch)