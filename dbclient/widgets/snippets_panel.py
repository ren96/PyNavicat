"""查询片段面板：搜索、双击插入编辑器、右键增删改。

界面 v2（2024 美化重构）：
- 编辑对话框更宽松，SQL 输入区带边框、聚焦描边
- 列表项视觉层次更清晰，标签用 # 前缀
- 空状态提示
"""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu,
    QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from ..sql_snippets import Snippet, snippet_store
from ..theme import theme_manager

log = logging.getLogger(__name__)


class _SnippetEditorDialog(QDialog):
    """保存 / 编辑片段的对话框。"""

    def __init__(self, parent=None, snippet: Snippet | None = None,
                 connection: str = "", database: str = ""):
        super().__init__(parent)
        self.setWindowTitle("保存查询片段" if snippet is None else "编辑片段")
        self.setMinimumSize(620, 480)
        self._original = snippet

        self.name_edit = QLineEdit(snippet.name if snippet else "")
        self.name_edit.setPlaceholderText("给片段起个名字")
        self.name_edit.setMinimumHeight(30)
        self.tags_edit = QLineEdit(", ".join(snippet.tags) if snippet else "")
        self.tags_edit.setPlaceholderText("用逗号分隔，例如：join, 报表, 统计")
        self.tags_edit.setMinimumHeight(30)
        self.conn_edit = QLineEdit(
            (snippet.connection if snippet else connection) or ""
        )
        self.conn_edit.setPlaceholderText("可选")
        self.conn_edit.setMinimumHeight(30)
        self.db_edit = QLineEdit(
            (snippet.database if snippet else database) or ""
        )
        self.db_edit.setPlaceholderText("可选")
        self.db_edit.setMinimumHeight(30)
        self.sql_edit = QPlainTextEdit(snippet.sql if snippet else "")
        self.sql_edit.setPlaceholderText("在此输入 SQL…")

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.addRow("名称 *", self.name_edit)
        form.addRow("标签", self.tags_edit)
        form.addRow("连接", self.conn_edit)
        form.addRow("数据库", self.db_edit)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        sql_label = QLabel("SQL *")
        sql_label.setProperty("role", "hint")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addLayout(form)
        layout.addSpacing(2)
        layout.addWidget(sql_label)
        layout.addWidget(self.sql_edit, 1)
        layout.addWidget(buttons)

        self._apply_style()

    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QDialog {{
                background: {p.bg};
            }}
            QPlainTextEdit {{
                background: {p.editor_bg};
                color: {p.text};
                border: 1px solid {p.border_strong};
                border-radius: 8px;
                padding: 10px 12px;
                font-family: Consolas, 'JetBrains Mono', monospace;
                font-size: 12px;
                selection-background-color: {p.editor_selection};
                selection-color: {p.text};
            }}
            QPlainTextEdit:hover {{
                border-color: {p.accent};
            }}
            QPlainTextEdit:focus {{
                border-color: {p.accent};
            }}
            QLabel {{
                color: {p.text_muted};
                font-weight: 500;
            }}
        """)

    def _on_accept(self) -> None:
        if not self.name_edit.text().strip():
            QMessageBox.warning(self, "参数不合法", "名称不能为空。")
            return
        if not self.sql_edit.toPlainText().strip():
            QMessageBox.warning(self, "参数不合法", "SQL 不能为空。")
            return
        self.accept()

    def result_snippet(self) -> Snippet:
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        return Snippet(
            name=self.name_edit.text().strip(),
            sql=self.sql_edit.toPlainText(),
            connection=self.conn_edit.text().strip(),
            database=self.db_edit.text().strip(),
            tags=tags,
        )


class SnippetsPanel(QWidget):
    """双击条目 → 请求插入编辑器。"""

    insertRequested = Signal(str)  # sql

    def __init__(self, parent=None):
        super().__init__(parent)

        # ---------- 搜索栏 ----------
        self.search = QLineEdit()
        self.search.setPlaceholderText("🔍 搜索片段 / 标签…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumHeight(30)
        self.search.textChanged.connect(self._reload)

        # ---------- 列表 ----------
        self.list = QListWidget()
        self.list.setAlternatingRowColors(False)
        self.list.setWordWrap(True)
        self.list.itemDoubleClicked.connect(lambda _i: self._insert_selected())
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._on_menu)
        self.list.setSpacing(0)

        # ---------- 按钮 ----------
        self.add_btn = QPushButton("＋ 保存当前 SQL")
        self.add_btn.setToolTip(
            "把当前查询页签的 SQL 保存为片段；无查询页签时新建片段"
        )
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setMinimumHeight(30)
        self.add_btn.clicked.connect(self._add_from_current)

        bar = QHBoxLayout()
        bar.setContentsMargins(8, 8, 8, 4)
        bar.setSpacing(6)
        bar.addWidget(self.search, 1)
        bar.addWidget(self.add_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(bar)
        layout.addWidget(self.list, 1)

        self._current_sql_provider = None
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
                padding: 10px 12px;
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

    # ---------- 对外 ----------
    def set_sql_provider(self, provider) -> None:
        """provider 返回 (sql, conn, db)。"""
        self._current_sql_provider = provider

    def refresh(self) -> None:
        self._reload()

    # ---------- 内部 ----------
    def _reload(self) -> None:
        p = theme_manager.palette
        try:
            items = snippet_store.search(self.search.text())
        except Exception as exc:  # noqa: BLE001
            log.warning("加载片段失败：%s", exc)
            items = []

        self.list.clear()

        if not items:
            empty = QListWidgetItem(
                "暂无片段\n点右上角「保存当前 SQL」或在编辑器里创建"
            )
            empty.setFlags(Qt.ItemIsEnabled)
            empty.setForeground(QBrush(QColor(p.text_disabled)))
            empty.setTextAlignment(Qt.AlignCenter)
            self.list.addItem(empty)
            return

        for s in items:
            title = s.name
            if s.tags:
                title += f"    #{'  #'.join(s.tags)}"
            text = f"{title}\n    {s.summary()}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, s)
            item.setToolTip(s.sql)
            self.list.addItem(item)

    def _current(self) -> Snippet | None:
        it = self.list.currentItem()
        if it is None:
            return None
        data = it.data(Qt.UserRole)
        return data if isinstance(data, Snippet) else None

    def _insert_selected(self) -> None:
        s = self._current()
        if s is None:
            return
        self.insertRequested.emit(s.sql)

    # ---------- 增删改 ----------
    def _add_from_current(self) -> None:
        sql, conn, db = "", "", ""
        if self._current_sql_provider is not None:
            try:
                sql, conn, db = self._current_sql_provider()
            except Exception:  # noqa: BLE001
                sql, conn, db = "", "", ""
        dlg = _SnippetEditorDialog(
            self, snippet=None, connection=conn, database=db,
        )
        if sql:
            dlg.sql_edit.setPlainText(sql)
        if dlg.exec() != QDialog.Accepted:
            return
        snip = dlg.result_snippet()
        if snippet_store.get(snip.name):
            if QMessageBox.question(
                self, "重名", f"片段「{snip.name}」已存在，是否覆盖？"
            ) != QMessageBox.Yes:
                return
        try:
            snippet_store.add(snip)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self._reload()

    def _edit_selected(self) -> None:
        s = self._current()
        if s is None:
            return
        dlg = _SnippetEditorDialog(self, snippet=s)
        if dlg.exec() != QDialog.Accepted:
            return
        new = dlg.result_snippet()
        try:
            snippet_store.update(s.name, new)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self._reload()

    def _rename_selected(self) -> None:
        s = self._current()
        if s is None:
            return
        name, ok = QInputDialog.getText(self, "重命名", "新名称：", text=s.name)
        if not ok or not name.strip():
            return
        new_name = name.strip()
        if new_name != s.name and snippet_store.get(new_name):
            QMessageBox.warning(self, "重名", f"「{new_name}」已存在。")
            return
        new = Snippet(
            name=new_name, sql=s.sql, connection=s.connection,
            database=s.database, tags=list(s.tags),
            created_at=s.created_at,
        )
        try:
            snippet_store.update(s.name, new)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "重命名失败", str(exc))
            return
        self._reload()

    def _remove_selected(self) -> None:
        s = self._current()
        if s is None:
            return
        if QMessageBox.question(
            self, "确认", f"删除片段「{s.name}」？"
        ) != QMessageBox.Yes:
            return
        try:
            snippet_store.remove(s.name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "删除失败", str(exc))
            return
        self._reload()

    def _on_menu(self, pos) -> None:
        s = self._current()
        if s is None:
            return
        menu = QMenu(self)
        menu.addAction("插入到当前编辑器", self._insert_selected)
        menu.addAction("复制 SQL", lambda: QApplication.clipboard().setText(s.sql))
        menu.addSeparator()
        menu.addAction("编辑…", self._edit_selected)
        menu.addAction("重命名…", self._rename_selected)
        menu.addAction("删除", self._remove_selected)
        menu.exec(self.list.viewport().mapToGlobal(pos))