"""应用级配置：路径、常量、QSettings 封装。"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QIcon

log = logging.getLogger(__name__)


def _is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的可执行文件中。"""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


# ==================== 路径 ====================
if _is_frozen():
    _MEIPASS = Path(sys._MEIPASS)            # type: ignore[attr-defined]
    PACKAGE_DIR = _MEIPASS / "dbclient"
    PROJECT_DIR = Path(sys.executable).resolve().parent
else:
    PACKAGE_DIR = Path(__file__).resolve().parent
    PROJECT_DIR = PACKAGE_DIR.parent

DEFAULT_DATA_DIR = PROJECT_DIR / "shujv"


def _prepare_data_dir() -> Path:
    """选取一个可写的数据目录。"""
    candidates: list[Path] = []
    env = os.environ.get("DBCLIENT_HOME")
    if env:
        candidates.append(Path(env))
    candidates.append(DEFAULT_DATA_DIR)
    candidates.append(Path(tempfile.gettempdir()) / "pynavicat")

    last_error: OSError | None = None
    for path in candidates:
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".write_probe"
            try:
                probe.write_text("x", encoding="utf-8")
            finally:
                try:
                    probe.unlink()
                except OSError:
                    pass
            return path
        except OSError as exc:
            last_error = exc
            continue

    log.warning("所有候选数据目录均不可写（%s），沿用默认路径", last_error)
    return DEFAULT_DATA_DIR


DATA_DIR = _prepare_data_dir()

CONFIG_FILE = DATA_DIR / "connections.json"
LOG_FILE = DATA_DIR / "pycat.log"


# ==================== 图标 ====================
def _find_icon_dir() -> Path:
    """定位图标目录：环境变量 > 包内 resources/ > 项目根 tubiao/ > 包内 tubiao/。"""
    env = os.environ.get("DBCLIENT_ICON_DIR")
    if env:
        return Path(env)

    candidates: list[Path] = []
    if _is_frozen():
        candidates.append(_MEIPASS / "tubiao")
        candidates.append(_MEIPASS / "dbclient" / "tubiao")
    candidates += [
        PACKAGE_DIR / "resources",
        PROJECT_DIR / "tubiao",
        PACKAGE_DIR / "tubiao",
    ]

    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    # 修复：找不到任何图标目录时留下日志，便于排查图标缺失问题
    log.warning("未找到图标目录，候选路径：%s", [str(c) for c in candidates])
    return PROJECT_DIR / "tubiao"


ICON_DIR = _find_icon_dir()
ICON_ICO = ICON_DIR / "MySQL客户端图标设计.ico"
ICON_PNG = ICON_DIR / "MySQL客户端图标设计.png"


def app_icon() -> QIcon:
    """统一的图标加载入口。优先 .ico，回退 .png；均不存在时返回空 QIcon。"""
    icon = QIcon()
    for path in (ICON_ICO, ICON_PNG):
        try:
            if path.is_file():
                icon.addFile(str(path))
        except Exception:  # noqa: BLE001
            continue
    return icon


# ==================== 应用元信息 ====================
APP_NAME = "PyNavicat"
APP_ORG = "PyNavicat"
APP_VERSION = "1.1.0"
APP_TITLE = f"{APP_NAME} · 数据库客户端"

# ==================== 默认参数 ====================
DEFAULT_PAGE_SIZE = 200
DEFAULT_MAX_ROWS = 5000
DEFAULT_WINDOW_SIZE = (1360, 860)
DEFAULT_SPLITTER_SIZES = (300, 1060)


# ==================== QSettings ====================
def settings() -> QSettings:
    """返回全局 QSettings 实例（Windows 下写入注册表）。"""
    return QSettings(APP_ORG, APP_NAME)


def page_size() -> int:
    """从设置读取默认分页大小。"""
    return int(settings().value("query/page_size", DEFAULT_PAGE_SIZE))


def max_rows() -> int:
    """从设置读取单次返回上限。"""
    return int(settings().value("query/max_rows", DEFAULT_MAX_ROWS))


# ==================== 打开页签的持久化 ====================
_OPEN_TABS_KEY = "session/open_tabs"
_MAX_OPEN_TABS = 30


def save_open_tabs(tabs: list[dict]) -> None:
    """把当前打开的查询页签写回 QSettings（JSON 字符串）。"""
    data = list(tabs or [])
    if len(data) > _MAX_OPEN_TABS:
        data = data[-_MAX_OPEN_TABS:]
    try:
        settings().setValue(
            _OPEN_TABS_KEY, json.dumps(data, ensure_ascii=False)
        )
        settings().sync()
    except Exception as exc:  # noqa: BLE001
        log.warning("保存打开页签失败：%s", exc)


def load_open_tabs() -> list[dict]:
    """读取上次退出时的页签列表；无有效数据时返回空列表。"""
    try:
        raw = settings().value(_OPEN_TABS_KEY, "")
    except Exception as exc:  # noqa: BLE001
        log.debug("读取打开页签失败：%s", exc)
        return []
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        conn = item.get("conn")
        if not conn:
            continue
        out.append({
            "conn": str(conn),
            "db": item.get("db") or None,
            "sql": item.get("sql") or "",
            "title": item.get("title") or str(conn),
            "paged": bool(item.get("paged")),
            "table_key": item.get("table_key"),
        })
    return out[-_MAX_OPEN_TABS:]


def clear_open_tabs() -> None:
    """清空页签持久化数据。"""
    try:
        settings().remove(_OPEN_TABS_KEY)
        settings().sync()
    except Exception as exc:  # noqa: BLE001
        log.debug("清空打开页签失败：%s", exc)