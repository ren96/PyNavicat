"""SQL 历史面板：搜索、双击开新页签、右键删除/复制。

界面 v2（2024 美化重构）：
- 搜索框更舒展、聚焦时强调色描边
- 列表项 padding 加大，hover 反馈更细腻
- 空状态提示
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from ..sql_history import HistoryEntry, history_store
from ..theme import theme_manager


class HistoryPanel(QWidget):
    """SQL 历史面板。双击条目在新页签中打开 SQL。"""

    openRequested = Signal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)

        # ---------- 搜索栏 ----------
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 搜索 SQL / 连接 / 数据库…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(30)
        self.search.textChanged.connect(self._reload)

        self.clear_btn = QPushButton("清空")
        self.clear_btn.setToolTip("清空所有历史记录")
        self.clear_btn.setCursor(Qt.PointingHandCursor)
        self.clear_btn.setMinimumHeight(30)
        self.clear_btn.clicked.connect(self._clear)

        # ---------- 列表 ----------
        self.list = QListWidget()
        self.list.setAlternatingRowColors(False)
        self.list.setWordWrap(False)
        self.list.itemDoubleClicked.connect(lambda _i: self._open_selected())
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_menu)
        self.list.setSpacing(0)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 8, 8, 4)
        bar.setSpacing(6)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.clear_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(bar)
        layout.addWidget(self.list, 1)

        theme_manager.changed.connect(self._on_theme_changed)
        self.destroyed.connect(self._on_destroyed)

        self._apply_style()
        self._reload()

    # ---------- 样式 ----------
    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QLineEdit {{
                background: {p.surface_sunken};
                border: 1px solid transparent;
                border-radius: 8px;
                padding: 4px 12px;
                color: {p.text};
                selection-background-color: {p.editor_selection};
                selection-color: {p.text};
            }}
            QLineEdit:hover {{
                background: {p.surface_alt};
                border-color: {p.border};
            }}
            QLineEdit:focus {{
                background: {p.surface};
                border-color: {p.accent};
            }}
            QListWidget {{
                background: {p.surface};
                border: none;
                outline: 0;
                padding: 4px 2px;
                color: {p.text};
            }}
            QListWidget::item {{
                padding: 8px 12px;
                border-radius: 6px;
                margin: 1px 6px;
            }}
            QListWidget::item:hover {{
                background: {p.surface_hover};
            }}
            QListWidget::item:selected {{
                background: {p.accent_soft};
                color: {p.accent_text};
            }}
        """)

    def _on_theme_changed(self, _palette) -> None:
        self._apply_style()
        self._reload()

    def _on_destroyed(self, *_args) -> None:
        try:
            theme_manager.changed.disconnect(self._on_theme_changed)
        except (TypeError, RuntimeError):
            pass

    def closeEvent(self, event) -> None:
        try:
            theme_manager.changed.disconnect(self._on_theme_changed)
        except (TypeError, RuntimeError):
            pass
        super().closeEvent(event)

    def refresh(self) -> None:
        self._reload()

    # ---------- 内部 ----------
    def _reload(self) -> None:
        p = theme_manager.palette
        entries = history_store.search(self.search.text())
        self.list.clear()

        if not entries:
            empty = QListWidgetItem("暂无历史记录")
            empty.setFlags(Qt.ItemIsEnabled)
            empty.setForeground(QBrush(QColor(p.text_disabled)))
            empty.setTextAlignment(Qt.AlignCenter)
            self.list.addItem(empty)
            return

        error_brush = QBrush(QColor(p.danger))
        for e in entries:
            mark = "✓" if e.success else "✗"
            text = f"{mark}  {e.when()}  ·  {e.connection or '-'}  ·  {e.summary()}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, e)
            item.setToolTip(e.sql if e.success else f"{e.sql}\n\n错误：{e.error}")
            if not e.success:
                item.setForeground(error_brush)
            self.list.addItem(item)

    def _current(self) -> HistoryEntry | None:
        item = self.list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.UserRole)
        return data if isinstance(data, HistoryEntry) else None

    def _open_selected(self) -> None:
        e = self._current()
        if e is None:
            return
        self.openRequested.emit(e.sql, e.connection, e.database)

    def _on_menu(self, pos) -> None:
        e = self._current()
        if e is None:
            return
        menu = QMenu(self)
        menu.addAction("在新页签中打开", self._open_selected)
        menu.addAction("复制 SQL", lambda: QApplication.clipboard().setText(e.sql))
        menu.addSeparator()
        menu.addAction("删除此条", lambda: self._remove(e))
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _remove(self, entry: HistoryEntry) -> None:
        history_store.remove(entry)
        self._reload()

    def _clear(self) -> None:
        if QMessageBox.question(
            self, "确认", "清空所有 SQL 历史？"
        ) != QMessageBox.Yes:
            return
        history_store.clear()
        self._reload()