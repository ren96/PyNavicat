"""会话 / 变量面板：查看当前连接的服务端变量、状态与进程列表。

界面 v2（2024 美化重构）：
- 三行顶部布局：连接+刷新 / 分类 / 搜索，互不挤压
- 表格无边框圆角，表头加粗
- 底部状态行 chip 化
"""
from __future__ import annotations

import logging

from PySide6.QtCore import (
    QObject, QRunnable, QSortFilterProxyModel, Qt, QThreadPool, Signal,
)
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMenu, QPushButton, QSizePolicy, QTableView, QVBoxLayout, QWidget,
)

from ..engine import DatabaseClient
from ..theme import theme_manager

log = logging.getLogger(__name__)


def _queries_for(db_type: str) -> list[tuple[str, str]]:
    """返回 [(显示名, SQL), ...]。不支持的数据库返回空列表。"""
    if db_type == "mysql":
        return [
            ("会话变量", "SHOW SESSION VARIABLES"),
            ("全局变量", "SHOW GLOBAL VARIABLES"),
            ("全局状态", "SHOW GLOBAL STATUS"),
            ("进程列表", "SHOW FULL PROCESSLIST"),
        ]
    if db_type == "postgresql":
        return [
            ("当前设置",
             "SELECT name, setting, unit, category FROM pg_settings ORDER BY name"),
            ("活动会话",
             "SELECT pid, usename, application_name, client_addr, "
             "state, query_start, LEFT(query, 200) AS query "
             "FROM pg_stat_activity WHERE pid <> pg_backend_pid() "
             "ORDER BY query_start DESC NULLS LAST"),
        ]
    if db_type == "sqlite":
        return [
            ("数据库列表", "PRAGMA database_list"),
        ]
    return []


class _SessionWorker(QRunnable):
    class _Signals(QObject):
        done = Signal(list, list)   # columns, rows
        failed = Signal(str)

    def __init__(self, client: DatabaseClient, sql: str):
        super().__init__()
        self.client = client
        self.sql = sql
        self.signals = _SessionWorker._Signals()

    def run(self) -> None:
        try:
            results = self.client.execute_many(self.sql, max_rows=5000)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(str(exc))
            return
        if not results:
            self.signals.done.emit([], [])
            return
        r = results[0]
        if not r.success:
            self.signals.failed.emit(r.message)
            return
        self.signals.done.emit(list(r.columns), [tuple(x) for x in r.rows])


class SessionPanel(QWidget):
    """会话 / 变量面板。由主窗口通过 set_providers 注入数据源。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._connections_provider = None
        self._db_type_provider = None
        self._client_getter = None
        self._busy = False
        self._pool = QThreadPool.globalInstance()

        self.setMinimumWidth(200)

        # ---------- 第一行：连接 + 刷新 ----------
        self.conn_combo = QComboBox()
        self.conn_combo.setMinimumWidth(120)
        self.conn_combo.setMinimumHeight(30)
        self.conn_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.conn_combo.setToolTip("选择要查看的连接")
        self.conn_combo.currentTextChanged.connect(self._on_conn_changed)

        self.refresh_btn = QPushButton("↻  重新加载")
        self.refresh_btn.setCursor(Qt.PointingHandCursor)
        self.refresh_btn.setToolTip("重新加载当前分类的数据")
        self.refresh_btn.setMinimumHeight(30)
        self.refresh_btn.clicked.connect(self._load)

        row1 = QHBoxLayout()
        row1.setContentsMargins(8, 8, 8, 2)
        row1.setSpacing(6)
        row1.addWidget(self.conn_combo, 1)
        row1.addWidget(self.refresh_btn)

        # ---------- 第二行：分类 ----------
        self.kind_combo = QComboBox()
        self.kind_combo.setMinimumWidth(80)
        self.kind_combo.setMinimumHeight(30)
        self.kind_combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.kind_combo.setToolTip("选择要查看的分类")
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)

        row2 = QHBoxLayout()
        row2.setContentsMargins(8, 0, 8, 2)
        row2.setSpacing(6)
        row2.addWidget(self.kind_combo, 1)

        # ---------- 第三行：搜索 ----------
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 过滤变量 / 名称…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumHeight(30)
        self.search_edit.textChanged.connect(self._apply_filter)

        row3 = QHBoxLayout()
        row3.setContentsMargins(8, 0, 8, 6)
        row3.setSpacing(6)
        row3.addWidget(self.search_edit, 1)

        # ---------- 表格 ----------
        self.model = QStandardItemModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)

        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionBehavior(QTableView.SelectRows)
        self.view.setSelectionMode(QTableView.ExtendedSelection)
        self.view.setWordWrap(False)
        self.view.setShowGrid(False)
        self.view.setFrameShape(QTableView.NoFrame)
        self.view.verticalHeader().setVisible(False)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)

        # ---------- 底部状态行 ----------
        self.status = QLabel("就绪")
        self.status.setObjectName("sessionStatus")
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)

        status_row = QHBoxLayout()
        status_row.setContentsMargins(10, 4, 10, 8)
        status_row.addWidget(self.status, 1)

        # ---------- 整体布局 ----------
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(row1)
        layout.addLayout(row2)
        layout.addLayout(row3)
        layout.addWidget(self.view, 1)
        layout.addLayout(status_row)

        self._apply_style()

    # ---------- 样式 ----------
    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QComboBox, QLineEdit {{
                background: {p.surface_sunken};
                border: 1px solid transparent;
                border-radius: 8px;
                padding: 4px 10px;
                color: {p.text};
            }}
            QComboBox:hover, QLineEdit:hover {{
                background: {p.surface_alt};
                border-color: {p.border};
            }}
            QComboBox:focus, QLineEdit:focus {{
                background: {p.surface};
                border-color: {p.accent};
            }}
            QTableView {{
                background: {p.surface};
                border: none;
                outline: 0;
                alternate-background-color: {p.surface_alt};
                selection-background-color: {p.accent_soft};
                selection-color: {p.accent_text};
                padding: 2px;
            }}
            QTableView::item {{
                padding: 4px 10px;
                border: none;
            }}
            QTableView::item:hover {{
                background: {p.surface_hover};
            }}
            QTableView::item:selected {{
                background: {p.accent_soft};
                color: {p.accent_text};
            }}
            QHeaderView::section {{
                background: {p.surface_alt};
                padding: 8px 12px;
                border: none;
                border-right: 1px solid {p.border};
                border-bottom: 1px solid {p.border};
                font-weight: 600;
                color: {p.text_muted};
                font-size: 9pt;
            }}
            QHeaderView::section:last {{
                border-right: none;
            }}
            QLabel#sessionStatus {{
                color: {p.text_muted};
                font-size: 9pt;
            }}
        """)

    # ---------- 注入 ----------
    def set_providers(self, connections, db_type, client_getter) -> None:
        self._connections_provider = connections
        self._db_type_provider = db_type
        self._client_getter = client_getter
        self.refresh_connections()

    def refresh_connections(self) -> None:
        if self._connections_provider is None:
            return
        try:
            names = list(self._connections_provider() or [])
        except Exception:  # noqa: BLE001
            names = []
        current = self.conn_combo.currentText()
        self.conn_combo.blockSignals(True)
        self.conn_combo.clear()
        self.conn_combo.addItems(names)
        if current and current in names:
            self.conn_combo.setCurrentText(current)
        self.conn_combo.blockSignals(False)
        self._reload_kinds()

    # ---------- 内部 ----------
    def _current_db_type(self) -> str:
        name = self.conn_combo.currentText()
        if not name or self._db_type_provider is None:
            return "mysql"
        try:
            return self._db_type_provider(name) or "mysql"
        except Exception:  # noqa: BLE001
            return "mysql"

    def _reload_kinds(self) -> None:
        db_type = self._current_db_type()
        entries = _queries_for(db_type)
        self.kind_combo.blockSignals(True)
        self.kind_combo.clear()
        if not entries:
            self.kind_combo.addItem("（暂不支持）", None)
        else:
            for label, sql in entries:
                self.kind_combo.addItem(label, (label, sql))
        self.kind_combo.blockSignals(False)
        self._on_kind_changed(0)

    def _on_conn_changed(self, _text: str) -> None:
        self._reload_kinds()

    def _on_kind_changed(self, _index: int) -> None:
        self._load()

    def _apply_filter(self, text: str) -> None:
        self.proxy.setFilterFixedString(text or "")

    def _clear_model(self) -> None:
        self.model.clear()
        self.model.setHorizontalHeaderLabels([])

    def _set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setToolTip(text)

    def _load(self) -> None:
        if self._busy:
            return
        name = self.conn_combo.currentText()
        if not name:
            self._set_status("无可用连接")
            self._clear_model()
            return
        data = self.kind_combo.currentData()
        if not data:
            self._set_status("当前数据库暂不支持")
            self._clear_model()
            return
        _label, sql = data
        if self._client_getter is None:
            return
        client = self._client_getter(name)
        if client is None:
            self._set_status("连接尚未打开，请先在对象树中展开该连接")
            self._clear_model()
            return
        self._busy = True
        self.refresh_btn.setEnabled(False)
        self._set_status("正在加载…")
        worker = _SessionWorker(client, sql)
        worker.signals.done.connect(self._on_done)
        worker.signals.failed.connect(self._on_failed)
        self._pool.start(worker)

    def _on_done(self, columns: list, rows: list) -> None:
        self._busy = False
        self.refresh_btn.setEnabled(True)
        self.model.clear()
        if not columns:
            self.model.setHorizontalHeaderLabels(["结果"])
            self._set_status("无数据")
            return
        self.model.setHorizontalHeaderLabels([str(c) for c in columns])
        for r in rows:
            items = [
                QStandardItem("" if v is None else str(v)) for v in r
            ]
            for it in items:
                it.setEditable(False)
            self.model.appendRow(items)

        col_count = self.model.columnCount()
        if col_count <= 20:
            self.view.resizeColumnsToContents()
            for c in range(col_count):
                if self.view.columnWidth(c) > 360:
                    self.view.setColumnWidth(c, 360)
        else:
            for c in range(col_count):
                self.view.setColumnWidth(c, 140)
        self._set_status(f"共 {len(rows)} 行")

    def _on_failed(self, message: str) -> None:
        self._busy = False
        self.refresh_btn.setEnabled(True)
        self._set_status(f"加载失败：{message}")

    def _on_context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("复制选中", self._copy_selection)
        menu.addAction("复制当前单元格", self._copy_current)
        menu.addAction("刷新", self._load)
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _copy_current(self) -> None:
        idx = self.view.currentIndex()
        if idx.isValid():
            QApplication.clipboard().setText(str(idx.data() or ""))

    def _copy_selection(self) -> None:
        idxs = self.view.selectionModel().selectedIndexes()
        if not idxs:
            return
        rows: dict[int, dict[int, str]] = {}
        for idx in idxs:
            rows.setdefault(idx.row(), {})[idx.column()] = str(idx.data() or "")
        text = "\n".join(
            "\t".join(cells.get(c, "") for c in sorted(cells))
            for _, cells in sorted(rows.items())
        )
        QApplication.clipboard().setText(text)