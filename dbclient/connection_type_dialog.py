"""新建连接第一步：卡片式选择数据库类型。

设计参考 Navicat / DBeaver 的新建连接向导：
- 顶部搜索框按名称过滤
- 左侧分类侧边栏（关系型数据库 / 轻量数据库 / 数仓分析）
- 中间卡片网格，图标使用品牌色 + 短字母生成（不依赖外部资源）
- 底部「已选择 + 取消 / 下一步」

界面 v2（2024 美化重构）：
- 搜索框独立顶部，聚焦强调色
- 左侧分类圆角选中块
- 卡片更大更舒展，hover 有轻微上浮感
- 选中态强调色边框 + 淡背景
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QIcon, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QScrollArea, QToolButton,
    QVBoxLayout, QWidget,
)

from .theme import theme_manager


# ============================================================
# 目录：只列出本程序真正支持的类型
# ============================================================
@dataclass(frozen=True)
class DbTypeEntry:
    key: str          # 与 connection.DRIVERS 的 key 一致
    name: str         # 显示名
    category: str     # 所属分类
    color: str        # 品牌色
    short: str        # 图标上的短字母
    hint: str = ""    # 悬停提示


# ---- 分类 ----
_CAT_REL = "关系型数据库"
_CAT_LIGHT = "轻量数据库"
_CAT_OLAP = "数仓分析"

_CATALOG: tuple[DbTypeEntry, ...] = (
    # ========== MySQL 协议族 ==========
    DbTypeEntry(
        "mysql", "MySQL", _CAT_REL, "#4479A1", "My",
        "MySQL 5.5+ / 8.0，使用 mysql+pymysql 驱动",
    ),
    DbTypeEntry(
        "mariadb", "MariaDB", _CAT_REL, "#C0765A", "Ma",
        "兼容 MySQL 协议，直接复用 mysql+pymysql 驱动",
    ),
    DbTypeEntry(
        "tidb", "TiDB", _CAT_REL, "#E30C34", "Ti",
        "兼容 MySQL 协议（默认端口 4000）",
    ),
    DbTypeEntry(
        "oceanbase", "OceanBase", _CAT_REL, "#1E75BC", "OB",
        "OceanBase MySQL 模式（默认端口 2881）",
    ),

    # ========== PostgreSQL 协议族 ==========
    DbTypeEntry(
        "postgresql", "PostgreSQL", _CAT_REL, "#336791", "Pg",
        "PostgreSQL 9.6+，使用 postgresql+psycopg2 驱动",
    ),
    DbTypeEntry(
        "cockroachdb", "CockroachDB", _CAT_REL, "#6933FF", "CR",
        "兼容 PostgreSQL 协议（默认端口 26257）",
    ),
    DbTypeEntry(
        "kingbase", "人大金仓 KingBase", _CAT_REL, "#C7302B", "KB",
        "兼容 PostgreSQL 协议（默认端口 54321）",
    ),

    # ========== 企业级 ==========
    DbTypeEntry(
        "mssql", "SQL Server", _CAT_REL, "#CC2927", "MS",
        "需要 pyodbc + Microsoft ODBC Driver for SQL Server",
    ),
    DbTypeEntry(
        "oracle", "Oracle", _CAT_REL, "#C74634", "Or",
        "使用 oracledb（thin 模式纯 Python，无需 Instant Client）",
    ),
    DbTypeEntry(
        "db2", "IBM DB2", _CAT_REL, "#052FAD", "DB",
        "需要 pip install ibm-db-sa（附带系统级 DB2 客户端）",
    ),
    DbTypeEntry(
        "firebird", "Firebird", _CAT_REL, "#E21E26", "FB",
        "需要 pip install sqlalchemy-firebird fdb",
    ),

    # ========== 轻量 / 嵌入式 ==========
    DbTypeEntry(
        "sqlite", "SQLite", _CAT_LIGHT, "#0F80CC", "SL",
        "零配置嵌入式数据库，直连 .db / .sqlite 文件",
    ),
    DbTypeEntry(
        "duckdb", "DuckDB", _CAT_LIGHT, "#FFB200", "DD",
        "嵌入式分析型数据库，需要 pip install duckdb-engine",
    ),

    # ========== 数仓 / 分析 ==========
    DbTypeEntry(
        "clickhouse", "ClickHouse", _CAT_OLAP, "#F7B218", "CH",
        "列存 OLAP 数据库（HTTP 端口 8123）；"
        "需要 pip install clickhouse-sqlalchemy",
    ),
    DbTypeEntry(
        "trino", "Trino", _CAT_OLAP, "#DD00A1", "Tr",
        "分布式 SQL 查询引擎（默认端口 8080）；"
        "需要 pip install 'trino[sqlalchemy]'",
    ),
)

_ALL = "全部"
_CATEGORIES: tuple[str, ...] = (_ALL,) + tuple(
    dict.fromkeys(e.category for e in _CATALOG)
)

_CARD_W = 156
_CARD_H = 140
_GRID_COLS = 4


def _make_db_icon(entry: DbTypeEntry, size: int = 56) -> QIcon:
    """生成品牌色圆角方块 + 短字母的图标。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(QColor(entry.color)))
    radius = size * 0.24
    painter.drawRoundedRect(1, 1, size - 2, size - 2, radius, radius)

    text_color = "#1a1a1a" if entry.color.upper() in (
        "#FFB200", "#FFD500", "#FFCC00",
    ) else "#ffffff"
    painter.setPen(QPen(QColor(text_color)))
    f = QFont()
    f.setPixelSize(max(10, int(size * 0.34)))
    f.setBold(True)
    painter.setFont(f)
    painter.drawText(pm.rect(), Qt.AlignCenter, entry.short)
    painter.end()
    return QIcon(pm)


class DatabaseTypeDialog(QDialog):
    """选择数据库类型；accept 后用 selected_type() 取 key。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建连接")
        self.setMinimumSize(980, 660)

        self._selected: DbTypeEntry | None = None
        self._cards: dict[str, QToolButton] = {}
        self._category = _ALL

        self._build_ui()
        self._apply_style()
        self._populate()

        if _CATALOG:
            self._select(_CATALOG[0])

    # ---------- 对外 ----------
    def selected_type(self) -> str | None:
        return self._selected.key if self._selected is not None else None

    # ---------- UI ----------
    def _build_ui(self) -> None:
        # ---- 搜索 ----
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔎  搜索数据库类型…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumHeight(38)
        self.search_edit.textChanged.connect(self._populate)

        # ---- 左侧分类 ----
        self.cat_list = QListWidget()
        self.cat_list.setObjectName("dbCatList")
        self.cat_list.setFixedWidth(170)
        self.cat_list.setFrameShape(QFrame.NoFrame)
        self.cat_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for name in _CATEGORIES:
            item = QListWidgetItem(name)
            item.setData(Qt.UserRole, name)
            self.cat_list.addItem(item)
        self.cat_list.setCurrentRow(0)
        self.cat_list.currentItemChanged.connect(self._on_category_changed)

        # ---- 卡片网格 ----
        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(4, 4, 4, 4)
        self._grid.setHorizontalSpacing(14)
        self._grid.setVerticalSpacing(14)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setWidget(self._grid_host)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(16)
        body.addWidget(self.cat_list)
        body.addWidget(self._scroll, 1)

        # ---- 底部 ----
        self.selected_label = QLabel("已选择：—")
        self.selected_label.setObjectName("selectedLabel")

        self.next_btn = QPushButton("下一步  ›")
        self.next_btn.setObjectName("primaryBtn")
        self.next_btn.setCursor(Qt.PointingHandCursor)
        self.next_btn.setDefault(True)
        self.next_btn.setEnabled(False)
        self.next_btn.setMinimumHeight(36)
        self.next_btn.setMinimumWidth(120)
        self.next_btn.clicked.connect(self.accept)

        cancel_btn = QPushButton("取消")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setMinimumHeight(36)
        cancel_btn.setMinimumWidth(88)
        cancel_btn.clicked.connect(self.reject)

        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(8)
        footer.addWidget(self.selected_label, 1)
        footer.addWidget(cancel_btn)
        footer.addWidget(self.next_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(self.search_edit)
        layout.addLayout(body, 1)
        layout.addLayout(footer)

    # ---------- 样式 ----------
    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QDialog {{
                background: {p.bg};
            }}
            QLineEdit {{
                background: {p.surface};
                border: 1.5px solid {p.border_strong};
                border-radius: 10px;
                padding: 4px 14px;
                color: {p.text};
                font-size: 10.5pt;
            }}
            QLineEdit:hover {{
                border-color: {p.accent};
            }}
            QLineEdit:focus {{
                border-color: {p.accent};
            }}

            QToolButton[dbcard="true"] {{
                background: {p.surface};
                border: 1.5px solid {p.border};
                border-radius: 12px;
                padding: 14px 6px 12px 6px;
                color: {p.text};
                font-weight: 500;
            }}
            QToolButton[dbcard="true"]:hover {{
                background: {p.surface_hover};
                border-color: {p.accent};
                color: {p.accent};
            }}
            QToolButton[dbcard="true"]:checked {{
                background: {p.accent_soft};
                border: 2px solid {p.accent};
                color: {p.accent_text};
                font-weight: 600;
            }}

            QListWidget#dbCatList {{
                background: {p.surface_alt};
                border: 1px solid {p.border};
                border-radius: 10px;
                outline: 0;
                padding: 6px 4px;
            }}
            QListWidget#dbCatList::item {{
                padding: 9px 12px;
                border-radius: 7px;
                margin: 1px 2px;
                color: {p.text_muted};
            }}
            QListWidget#dbCatList::item:hover {{
                background: {p.surface_hover};
                color: {p.text};
            }}
            QListWidget#dbCatList::item:selected {{
                background: {p.accent_soft};
                color: {p.accent_text};
                font-weight: 600;
            }}

            QLabel#selectedLabel {{
                color: {p.text_muted};
                font-size: 9.5pt;
                padding-left: 4px;
            }}
            QPushButton#primaryBtn {{
                background: {p.accent};
                color: #ffffff;
                border: 1px solid {p.accent};
                border-radius: 8px;
                padding: 6px 20px;
                font-weight: 600;
            }}
            QPushButton#primaryBtn:hover {{
                background: {p.accent_hover};
                border-color: {p.accent_hover};
            }}
            QPushButton#primaryBtn:pressed {{
                background: {p.accent_pressed};
            }}
            QPushButton#primaryBtn:disabled {{
                background: {p.surface_alt};
                color: {p.text_disabled};
                border-color: {p.border};
            }}
            QScrollArea {{
                background: transparent;
                border: none;
            }}
        """)

    # ---------- 事件 ----------
    def _on_category_changed(self, current, _previous) -> None:
        if current is None:
            return
        self._category = str(current.data(Qt.UserRole) or _ALL)
        self._populate()

    def _populate(self, *_args) -> None:
        # 清空旧卡片
        while self._grid.count():
            item = self._grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._cards.clear()

        needle = self.search_edit.text().strip().lower()
        if needle:
            entries = [e for e in _CATALOG if needle in e.name.lower()]
        else:
            entries = [
                e for e in _CATALOG
                if self._category == _ALL or e.category == self._category
            ]

        if not entries:
            empty = QLabel("未找到匹配的数据库类型")
            empty.setProperty("role", "hint")
            empty.setAlignment(Qt.AlignCenter)
            empty.setStyleSheet("padding: 60px 0;")
            self._grid.addWidget(empty, 0, 0, 1, _GRID_COLS)
            return

        for i, entry in enumerate(entries):
            card = self._make_card(entry)
            self._grid.addWidget(
                card, i // _GRID_COLS, i % _GRID_COLS,
                alignment=Qt.AlignTop | Qt.AlignLeft,
            )
            self._cards[entry.key] = card
            if self._selected is not None and self._selected.key == entry.key:
                card.setChecked(True)

        for c in range(_GRID_COLS):
            self._grid.setColumnStretch(c, 0)
        self._grid.setColumnStretch(_GRID_COLS, 1)

    def _make_card(self, entry: DbTypeEntry) -> QToolButton:
        btn = QToolButton()
        btn.setProperty("dbcard", "true")
        btn.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        btn.setIcon(_make_db_icon(entry))
        btn.setIconSize(QSize(56, 56))
        btn.setText(entry.name)
        btn.setCheckable(True)
        btn.setFixedSize(_CARD_W, _CARD_H)
        btn.setCursor(Qt.PointingHandCursor)
        if entry.hint:
            btn.setToolTip(entry.hint)
        btn.clicked.connect(lambda _=False, e=entry: self._select(e))
        return btn

    def _select(self, entry: DbTypeEntry) -> None:
        self._selected = entry
        for key, card in self._cards.items():
            card.setChecked(key == entry.key)
        self.selected_label.setText(
            f"已选择：<b style='color:{theme_manager.palette.accent};'>"
            f"{entry.name}</b>"
        )
        self.selected_label.setTextFormat(Qt.RichText)
        if entry.hint:
            self.selected_label.setToolTip(entry.hint)
        self.next_btn.setEnabled(True)