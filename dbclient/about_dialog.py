"""关于对话框：版本、技术栈、支持的数据库、主要功能、运行环境与数据位置。

界面 v2（2024 美化重构）：
- 顶部 Header 卡片化（淡色底 + 大图标）
- 各区块分段卡片式，标题带下边距
- 环境信息标签 + 值分列对齐
"""
from __future__ import annotations

import platform
import sys
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .config import (
    APP_NAME, APP_ORG, APP_VERSION,
    DATA_DIR, LOG_FILE, ICON_ICO, ICON_PNG,
)
from .theme import theme_manager

_TECH_STACK = [
    ("界面框架", "PySide6 (Qt for Python)"),
    ("数据库抽象", "SQLAlchemy 2.x"),
    ("驱动", "PyMySQL / psycopg2 / pyodbc / oracledb"),
    ("密码存储", "keyring 优先，本地 Fernet 加密兜底"),
    ("加密实现", "cryptography · PBKDF2-HMAC-SHA256"),
    ("导入导出", "openpyxl / csv / json"),
    ("SQL 美化", "sqlparse"),
]

_DATABASES = [
    ("MySQL", "mysql+pymysql", "3306"),
    ("PostgreSQL", "postgresql+psycopg2", "5432"),
    ("SQLite", "sqlite（文件型）", "—"),
    ("SQL Server", "mssql+pyodbc", "1433"),
    ("Oracle", "oracle+oracledb", "1521"),
]

_FEATURES = [
    "对象浏览器：连接 / 库 / 表 / 视图 / 列，多级懒加载",
    "SQL 编辑器：语法高亮、行号栏、自动补全、一键格式化",
    "结果表格：分页、过滤、排序、脱敏、增删改、撤销/重做",
    "导入导出：CSV / JSON / Excel / INSERT 语句",
    "结构管理：新建库表、ALTER TABLE、索引管理、DDL 生成",
    "事务支持：手动事务、提交 / 回滚、查询取消",
    "主题：浅色 / 深色，编辑器同步换肤",
    "安全：密码走系统钥匙串，不明文落盘",
    "高危操作：DROP / TRUNCATE / 无 WHERE 写操作先预览确认",
]

_MONO_STYLE = "font-family: Consolas, 'JetBrains Mono', monospace;"


@lru_cache(maxsize=1)
def _qt_version() -> str:
    """缓存 PySide6 版本探测，避免每次构建对话框都重新导入。"""
    try:
        from PySide6 import __version__ as v  # type: ignore
        return str(v)
    except Exception:  # noqa: BLE001
        return "未知"


def _sep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setProperty("role", "sep")
    return line


class AboutDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"关于 {APP_NAME}")
        self.setMinimumSize(760, 660)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(24, 22, 24, 18)
        body.setSpacing(16)

        body.addWidget(self._build_header())
        body.addWidget(self._section("技术栈", _TECH_STACK))
        body.addWidget(self._section("支持的数据库", _DATABASES))
        body.addWidget(self._feature_list())
        body.addWidget(self._build_env())
        body.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

        # ---------- 底部按钮 ----------
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn = buttons.button(QDialogButtonBox.Close)
        close_btn.setText("关闭")
        close_btn.setMinimumHeight(32)
        buttons.rejected.connect(self.reject)

        copy_btn = QPushButton("复制版本信息")
        copy_btn.setCursor(Qt.PointingHandCursor)
        copy_btn.setMinimumHeight(32)
        copy_btn.clicked.connect(self._copy_info)

        data_btn = QPushButton("打开数据目录")
        data_btn.setCursor(Qt.PointingHandCursor)
        data_btn.setMinimumHeight(32)
        data_btn.clicked.connect(lambda: self._open_dir(DATA_DIR))

        log_btn = QPushButton("打开日志目录")
        log_btn.setCursor(Qt.PointingHandCursor)
        log_btn.setMinimumHeight(32)
        log_btn.clicked.connect(lambda: self._open_dir(LOG_FILE.parent))

        buttons.addButton(copy_btn, QDialogButtonBox.ActionRole)
        buttons.addButton(data_btn, QDialogButtonBox.ActionRole)
        buttons.addButton(log_btn, QDialogButtonBox.ActionRole)

        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(18, 12, 18, 14)
        bottom_layout.setSpacing(8)
        bottom_layout.addWidget(buttons)
        layout.addWidget(bottom)

        self._apply_style()

    # ---------- 样式 ----------
    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QDialog {{
                background: {p.bg};
            }}
            QScrollArea {{
                background: {p.bg};
                border: none;
            }}
            QFrame[role="sep"] {{
                border: none;
                border-top: 1px solid {p.border};
                max-height: 1px;
            }}
            QLabel[role="hint"] {{
                color: {p.text_muted};
            }}
        """)

    # ---------- 各区块 ----------
    def _build_header(self) -> QWidget:
        p = theme_manager.palette
        box = QWidget()
        box.setStyleSheet(f"""
            QWidget {{
                background: {p.surface_alt};
                border: 1px solid {p.border};
                border-radius: 14px;
            }}
        """)
        row = QHBoxLayout(box)
        row.setContentsMargins(20, 18, 20, 18)
        row.setSpacing(18)

        icon_label = QLabel()
        pix = QPixmap()
        for path in (ICON_PNG, ICON_ICO):
            if isinstance(path, Path) and path.is_file() and pix.load(str(path)):
                break
        if not pix.isNull():
            icon_label.setPixmap(
                pix.scaled(72, 72, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            )
        icon_label.setFixedSize(76, 76)
        icon_label.setAlignment(Qt.AlignCenter)
        icon_label.setStyleSheet("border: none; background: transparent;")
        row.addWidget(icon_label, 0, Qt.AlignTop)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(4)

        title = QLabel(
            f"<span style='font-size:22pt; font-weight:600;'>{APP_NAME}</span>"
        )
        title.setTextFormat(Qt.RichText)
        title.setStyleSheet("border: none; background: transparent;")

        version = QLabel(f"版本 {APP_VERSION}　·　{APP_ORG}")
        version.setProperty("role", "hint")
        version.setStyleSheet(
            f"border: none; background: transparent; color: {p.text_muted};"
        )

        desc = QLabel(
            "一个用 Python + PySide6 + SQLAlchemy 编写的轻量级数据库客户端，"
            "面向日常库表浏览、SQL 调试与数据导入导出。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"border: none; background: transparent; color: {p.text_muted};"
        )

        text_col.addWidget(title)
        text_col.addWidget(version)
        text_col.addSpacing(6)
        text_col.addWidget(desc)
        text_col.addStretch(1)
        row.addLayout(text_col, 1)
        return box

    @staticmethod
    def _section(title: str, rows: Sequence[Sequence[str]]) -> QWidget:
        p = theme_manager.palette
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        head = QLabel(f"<b style='font-size:10.5pt;'>{title}</b>")
        head.setTextFormat(Qt.RichText)
        outer.addWidget(head)

        grid = QGridLayout()
        grid.setContentsMargins(4, 0, 0, 0)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(6)
        for r, row in enumerate(rows):
            if len(row) == 3:
                name, driver, port = row
                name_lbl = QLabel(f"•  {name}")
                name_lbl.setStyleSheet("font-weight: 500;")
                grid.addWidget(name_lbl, r, 0)
                mono = QLabel(driver)
                mono.setStyleSheet(_MONO_STYLE)
                mono.setProperty("role", "hint")
                grid.addWidget(mono, r, 1)
                port_lbl = QLabel(f"默认端口 {port}")
                port_lbl.setProperty("role", "hint")
                grid.addWidget(port_lbl, r, 2)
            else:
                name, value = row
                name_lbl = QLabel(f"•  {name}")
                name_lbl.setStyleSheet("font-weight: 500;")
                grid.addWidget(name_lbl, r, 0)
                value_lbl = QLabel(value)
                value_lbl.setProperty("role", "hint")
                value_lbl.setWordWrap(True)
                grid.addWidget(value_lbl, r, 1, 1, 2)
        grid.setColumnStretch(1, 1)
        outer.addLayout(grid)
        return box

    def _feature_list(self) -> QWidget:
        box = QWidget()
        outer = QVBoxLayout(box)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        head = QLabel("<b style='font-size:10.5pt;'>主要功能</b>")
        head.setTextFormat(Qt.RichText)
        outer.addWidget(head)

        lines = QVBoxLayout()
        lines.setContentsMargins(4, 0, 0, 0)
        lines.setSpacing(5)
        for f in _FEATURES:
            outer_lbl = QLabel(f"•  {f}")
            outer_lbl.setWordWrap(True)
            lines.addWidget(outer_lbl)
        outer.addLayout(lines)
        return box

    def _build_env(self) -> QWidget:
        p = theme_manager.palette
        box = QWidget()
        box.setStyleSheet(f"""
            QWidget {{
                background: {p.surface_alt};
                border: 1px solid {p.border};
                border-radius: 12px;
            }}
        """)
        grid = QGridLayout(box)
        grid.setContentsMargins(18, 14, 18, 14)
        grid.setHorizontalSpacing(20)
        grid.setVerticalSpacing(8)

        def add(row: int, name: str, value: str, mono: bool = False) -> None:
            n = QLabel(name)
            n.setStyleSheet(
                f"border: none; background: transparent; color: {p.text_muted};"
            )
            v = QLabel(value)
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextSelectableByMouse)
            base_style = "border: none; background: transparent;"
            if mono:
                base_style += _MONO_STYLE
            v.setStyleSheet(base_style)
            grid.addWidget(n, row, 0, Qt.AlignTop)
            grid.addWidget(v, row, 1)

        add(0, "操作系统", f"{platform.system()} {platform.release()} ({platform.machine()})")
        add(1, "Python", sys.version.split()[0])
        add(2, "Qt / PySide6", _qt_version())
        add(3, "数据目录", str(DATA_DIR), mono=True)
        add(4, "日志文件", str(LOG_FILE), mono=True)
        grid.setColumnStretch(1, 1)
        return box

    # ---------- 动作 ----------
    def _open_dir(self, path: Path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _copy_info(self) -> None:
        text = "\n".join([
            f"{APP_NAME} {APP_VERSION}",
            f"组织：{APP_ORG}",
            f"操作系统：{platform.system()} {platform.release()} ({platform.machine()})",
            f"Python：{sys.version.split()[0]}",
            f"Qt / PySide6：{_qt_version()}",
            f"数据目录：{DATA_DIR}",
            f"日志文件：{LOG_FILE}",
        ])
        QApplication.clipboard().setText(text)