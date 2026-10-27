"""查询结果表格：可编辑模型 + 视图 + 搜索/排序 + 多格式导出 + 敏感列脱敏
+ 增删行 + 撤销/重做（保存时统一生成 SQL）+ 粘贴 + 大字段查看器 + EXPLAIN 高亮
+ 值 → SQL 快捷通道 + 类型化编辑器 + 单元格变更高亮。

界面 v2（2024 美化重构）：
- 工具栏按功能分组（搜索 / 编辑 / 保存 / 输出）
- 按钮紧凑化，图标优先
- 敏感列提示 chip 化
- 空状态更友好
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QKeySequence, QShortcut, QUndoCommand, QUndoStack,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton, QTableView, QToolButton, QVBoxLayout, QWidget,
)

from .. import config as _config
from .. import io_tools
from ..sql_util import qualify_table, quote_ident, sql_literal
from ..theme import theme_manager
from .delegates import TypedEditDelegate
from .export_dialog import ExportDialog

log = logging.getLogger(__name__)

_SENSITIVE_RE = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|private[_-]?key"
    r"|credit[_-]?card|card[_-]?no|ssn|id[_-]?card)",
    re.IGNORECASE,
)
_MASK = "••••••"
_MAX_CELL_CHARS = 500

_EXPLAIN_BAD_TYPE = {"all", "index"}
_EXPLAIN_BAD_EXTRA_KEYWORDS = ("filesort", "temporary")
_EXPLAIN_BAD_PLAN_KEYWORDS = ("seq scan", "full scan", "table scan")

_DEFAULT_ROW_HEIGHT = 32


@dataclass
class TableContext:
    table: str
    schema: str | None = None
    db_type: str = "mysql"
    primary_keys: tuple[str, ...] = ()
    editable: bool = True
    column_types: dict[str, str] = field(default_factory=dict)
    auto_increment_columns: tuple[str, ...] = ()


class ResultModel(QAbstractTableModel):
    cellEdited = Signal(int, int, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._columns: list[str] = []
        self._rows: list[list] = []
        self._original: list[list] = []
        self._baseline_rows: list[list] = []
        self._context: TableContext | None = None
        self._pk_index: list[int] = []
        self._dirty_rows: set[int] = set()
        self._dirty_cells: set[tuple[int, int]] = set()
        self._inserted_rows: set[int] = set()
        self._deleted_rows: list[list] = []
        self._mask_enabled = True
        self._sensitive_cols: set[int] = set()
        self._explain_cols: dict[int, str] = {}

    # ---------- 装载 ----------
    def set_result(self, columns, rows, context: TableContext | None = None):
        self.beginResetModel()
        self._columns = list(columns)
        self._rows = [list(r) for r in rows]
        self._original = [list(r) for r in rows]
        self._baseline_rows = [list(r) for r in rows]
        self._context = context
        if context:
            self._pk_index = [
                self._columns.index(k) for k in context.primary_keys
                if k in self._columns
            ]
        else:
            self._pk_index = []
        self._dirty_rows = set()
        self._dirty_cells = set()
        self._inserted_rows = set()
        self._deleted_rows = []
        self._sensitive_cols = {
            i for i, c in enumerate(self._columns) if _SENSITIVE_RE.search(c or "")
        }
        self._explain_cols = self._detect_explain_columns()
        self.endResetModel()

    def append_rows(self, rows: list[list]) -> None:
        if not rows:
            return
        start = len(self._rows)
        end = start + len(rows) - 1
        self.beginInsertRows(QModelIndex(), start, end)
        for r in rows:
            self._rows.append(list(r))
            self._original.append(list(r))
            self._baseline_rows.append(list(r))
        self.endInsertRows()

    def _detect_explain_columns(self) -> dict[int, str]:
        out: dict[int, str] = {}
        for i, c in enumerate(self._columns):
            key = (c or "").strip().lower()
            if key in ("type", "extra", "plan", "key", "possible_keys"):
                out[i] = key
        return out

    def clear(self) -> None:
        self.set_result([], [])

    def columns(self) -> list[str]:
        return list(self._columns)

    def rows(self) -> list[list]:
        return [list(r) for r in self._rows]

    def has_dirty(self) -> bool:
        return bool(self._dirty_rows) or bool(self._deleted_rows)

    def dirty_rows(self) -> set[int]:
        return set(self._dirty_rows)

    def set_mask_enabled(self, enabled: bool) -> None:
        self._mask_enabled = bool(enabled)
        if self._columns and self._rows:
            top = self.index(0, 0)
            bottom = self.index(len(self._rows) - 1, len(self._columns) - 1)
            self.dataChanged.emit(top, bottom, [Qt.DisplayRole])

    def mask_enabled(self) -> bool:
        return self._mask_enabled

    # ---------- Qt 模型接口 ----------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def flags(self, index: QModelIndex) -> Qt.ItemFlags:
        base = super().flags(index)
        if not index.isValid() or self._context is None or not self._context.editable:
            return base
        if not self._pk_index or index.column() in self._pk_index:
            return base
        return base | Qt.ItemIsEditable

    def _explain_color(self, col: int, value) -> str | None:
        if not self._explain_cols:
            return None
        name = self._explain_cols.get(col)
        if name is None:
            return None
        palette = theme_manager.palette
        text = "" if value is None else str(value).lower()
        if name == "type":
            if text in _EXPLAIN_BAD_TYPE:
                return palette.danger
        elif name == "key":
            if value in (None, ""):
                return palette.warning
        elif name == "extra":
            for kw in _EXPLAIN_BAD_EXTRA_KEYWORDS:
                if kw in text:
                    return palette.danger
        elif name == "plan":
            for kw in _EXPLAIN_BAD_PLAN_KEYWORDS:
                if kw in text:
                    return palette.danger
        return None

    # ---------- 单元格展示 / 编辑 ----------
    @staticmethod
    def _to_text(value) -> str:
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return f"<{len(raw)} bytes>"
        if isinstance(value, datetime):
            return value.isoformat(sep=" ")
        if isinstance(value, (date, time)):
            return value.isoformat()
        return str(value)

    @classmethod
    def _format_full(cls, value) -> str:
        return "" if value is None else cls._to_text(value)

    @classmethod
    def _format(cls, value) -> str:
        if value is None:
            return "NULL"
        text = cls._to_text(value)
        if len(text) > _MAX_CELL_CHARS:
            return text[:_MAX_CELL_CHARS] + f"…（共 {len(text)} 字符，双击查看）"
        return text

    def _raw_equal(self, a, b) -> bool:
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        if isinstance(a, (bytes, bytearray, memoryview)) or isinstance(
            b, (bytes, bytearray, memoryview)
        ):
            try:
                return bytes(a) == bytes(b)  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001
                pass
        if isinstance(a, (int, float, Decimal)) and isinstance(
            b, (int, float, Decimal)
        ):
            try:
                return float(a) == float(b)
            except (TypeError, ValueError):
                return a == b
        if isinstance(a, str) and isinstance(b, str):
            return a == b
        if isinstance(a, datetime) and isinstance(b, datetime):
            return a == b
        if isinstance(a, date) and isinstance(b, date):
            return a == b
        return self._to_text(a) == self._to_text(b)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        try:
            value = self._rows[index.row()][index.column()]
        except IndexError:
            return None

        if role == Qt.DisplayRole:
            if (self._mask_enabled
                    and index.column() in self._sensitive_cols
                    and value is not None and value != ""):
                return _MASK
            return self._format(value)

        if role == Qt.EditRole:
            return self._format_full(value)

        if role == Qt.UserRole:
            if value is None:
                return None
            if isinstance(value, Decimal):
                return float(value)
            return value

        if role == Qt.BackgroundRole:
            palette = theme_manager.palette
            row, col = index.row(), index.column()
            if row in self._inserted_rows:
                return QBrush(QColor(palette.accent_soft))
            if (row, col) in self._dirty_cells:
                return QBrush(QColor(palette.accent_soft))
            return None

        if role == Qt.ToolTipRole:
            if index.column() in self._sensitive_cols and self._mask_enabled:
                return "敏感列已被脱敏（可通过工具栏取消脱敏）"
            return self._format(value)

        if role == Qt.ForegroundRole:
            if value is None:
                return QBrush(QColor(theme_manager.palette.text_disabled))
            color = self._explain_color(index.column(), value)
            if color:
                return QBrush(QColor(color))
            return None

        if role == Qt.TextAlignmentRole:
            if isinstance(value, (int, float, Decimal)):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)
        return None

    def _recompute_dirty_row(self, row: int) -> None:
        self._dirty_cells = {(r, c) for (r, c) in self._dirty_cells if r != row}

        if row < 0 or row >= len(self._rows) or row >= len(self._original):
            self._dirty_rows.discard(row)
            return

        if row in self._inserted_rows:
            self._dirty_rows.add(row)
            return

        cur = self._rows[row]
        orig = self._original[row]
        width = min(len(cur), len(orig))
        has_change = False
        for i in range(width):
            if not self._raw_equal(cur[i], orig[i]):
                self._dirty_cells.add((row, i))
                has_change = True
        if has_change:
            self._dirty_rows.add(row)
        else:
            self._dirty_rows.discard(row)

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if role != Qt.EditRole or not index.isValid():
            return False
        if self._context is None or not self._context.editable or not self._pk_index:
            return False
        row, col = index.row(), index.column()
        if not (0 <= row < len(self._rows) and 0 <= col < len(self._columns)):
            return False
        if col in self._pk_index:
            return False

        old = self._rows[row][col]
        new_value: Any = value

        if isinstance(value, str):
            if value.strip().upper() == "NULL":
                new_value = None
            elif value == "" and old is None:
                return False

        if old is None and new_value is None:
            return False
        if self._raw_equal(old, new_value):
            return False

        self._rows[row][col] = new_value
        self._recompute_dirty_row(row)
        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
        self.cellEdited.emit(row, col, old, new_value)
        return True

    def paste_block(self, start_row: int, start_col: int,
                    block: list[list[str]]) -> int:
        if not block or self._context is None or not self._context.editable:
            return 0
        written = 0
        for r_off, row_vals in enumerate(block):
            r = start_row + r_off
            if r >= len(self._rows):
                break
            for c_off, raw in enumerate(row_vals):
                c = start_col + c_off
                if c >= len(self._columns) or c in self._pk_index:
                    continue
                new_value: Any = raw
                if raw == "" or raw.upper() == "NULL":
                    new_value = None
                old = self._rows[r][c]
                if self._raw_equal(old, new_value):
                    continue
                self._rows[r][c] = new_value
                self._recompute_dirty_row(r)
                idx = self.index(r, c)
                self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.EditRole])
                self.cellEdited.emit(r, c, old, new_value)
                written += 1
        return written

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self._columns[section] if section < len(self._columns) else None
        return section + 1

    def apply_value(self, row: int, col: int, value) -> None:
        if not (0 <= row < len(self._rows) and 0 <= col < len(self._columns)):
            return
        self._rows[row][col] = value
        self._recompute_dirty_row(row)
        idx = self.index(row, col)
        self.dataChanged.emit(idx, idx, [Qt.DisplayRole, Qt.EditRole])

    def insert_blank_row(self, at: int) -> None:
        at = max(0, min(at, len(self._rows)))
        self.beginInsertRows(QModelIndex(), at, at)
        self._rows.insert(at, [None] * len(self._columns))
        self._original.insert(at, [None] * len(self._columns))
        self.endInsertRows()
        self._inserted_rows = {r + 1 if r >= at else r for r in self._inserted_rows}
        self._inserted_rows.add(at)
        self._dirty_rows = {r + 1 if r >= at else r for r in self._dirty_rows}
        self._dirty_cells = {
            (r + 1 if r >= at else r, c) for (r, c) in self._dirty_cells
        }
        self._recompute_dirty_row(at)

    def remove_row_at(self, at: int) -> None:
        if not (0 <= at < len(self._rows)):
            return
        was_inserted = at in self._inserted_rows
        snapshot_row = list(self._rows[at])
        self.beginRemoveRows(QModelIndex(), at, at)
        self._rows.pop(at)
        self._original.pop(at)
        self.endRemoveRows()
        if not was_inserted:
            self._deleted_rows.append(snapshot_row)
        self._inserted_rows = {
            r - 1 if r > at else r for r in self._inserted_rows if r != at
        }
        self._dirty_rows = {
            r - 1 if r > at else r for r in self._dirty_rows if r != at
        }
        self._dirty_cells = {
            (r - 1 if r > at else r, c)
            for (r, c) in self._dirty_cells if r != at
        }

    def snapshot(self) -> tuple:
        return (
            [list(r) for r in self._rows],
            [list(r) for r in self._original],
            set(self._inserted_rows),
            set(self._dirty_rows),
            [list(r) for r in self._deleted_rows],
            set(self._dirty_cells),
        )

    def restore(self, snap) -> None:
        rows, original, inserted, dirty, deleted, cells = snap
        self.beginResetModel()
        self._rows = [list(r) for r in rows]
        self._original = [list(r) for r in original]
        self._inserted_rows = set(inserted)
        self._dirty_rows = set(dirty)
        self._deleted_rows = [list(r) for r in deleted]
        self._dirty_cells = set(cells)
        self.endResetModel()

    def is_inserted(self, row: int) -> bool:
        return row in self._inserted_rows

    def build_pending_sql(self) -> list[str]:
        ctx = self._context
        if ctx is None:
            return []
        db = ctx.db_type
        # 修复：使用 qualify_table 正确处理多级 schema（如 DuckDB 的 catalog.schema）
        table = qualify_table(ctx.table, ctx.schema, db)
        auto_cols = set(ctx.auto_increment_columns or ())
        stmts: list[str] = []

        # ---------- DELETE ----------
        if self._pk_index:
            for deleted in self._deleted_rows:
                where_parts: list[str] = []
                has_null_pk = False
                for i in self._pk_index:
                    if not (0 <= i < len(deleted) and i < len(self._columns)):
                        continue
                    if deleted[i] is None:
                        has_null_pk = True
                        break
                    where_parts.append(
                        f"{quote_ident(self._columns[i], db)} = "
                        f"{sql_literal(deleted[i], db)}"
                    )
                if has_null_pk or not where_parts:
                    log.warning("跳过 DELETE：主键含 NULL，无法定位行")
                    continue
                stmts.append(
                    f"DELETE FROM {table} WHERE " + " AND ".join(where_parts) + ";"
                )

        # ---------- INSERT / UPDATE ----------
        for r in sorted(self._dirty_rows):
            if not (0 <= r < len(self._rows)):
                continue

            if r in self._inserted_rows:
                cols_out: list[str] = []
                vals_out: list[str] = []
                for i, c in enumerate(self._columns):
                    v = self._rows[r][i]
                    if c in auto_cols and v is None:
                        continue
                    cols_out.append(quote_ident(c, db))
                    vals_out.append(sql_literal(v, db))
                if not cols_out:
                    continue
                stmts.append(
                    f"INSERT INTO {table} ({', '.join(cols_out)}) "
                    f"VALUES ({', '.join(vals_out)});"
                )
                continue

            if r >= len(self._original):
                continue
            old = self._original[r]
            new = self._rows[r]
            changed = [
                (i, new[i]) for i in range(len(new))
                if i < len(old) and not self._raw_equal(old[i], new[i])
            ]
            if not changed:
                continue

            where_parts: list[str] = []
            has_null_pk = False
            for i in self._pk_index:
                old_val = old[i] if i < len(old) else None
                if old_val is None:
                    has_null_pk = True
                    break
                where_parts.append(
                    f"{quote_ident(self._columns[i], db)} = "
                    f"{sql_literal(old_val, db)}"
                )
            if has_null_pk or not where_parts:
                log.warning("第 %d 行主键含 NULL，跳过 UPDATE", r + 1)
                continue

            set_part = ", ".join(
                f"{quote_ident(self._columns[i], db)} = {sql_literal(v, db)}"
                for i, v in changed
            )
            stmts.append(
                f"UPDATE {table} SET {set_part} "
                f"WHERE " + " AND ".join(where_parts) + ";"
            )
        return stmts

    def mark_clean(self) -> None:
        self.beginResetModel()
        self._original = [list(r) for r in self._rows]
        self._baseline_rows = [list(r) for r in self._rows]
        self._dirty_rows = set()
        self._dirty_cells = set()
        self._inserted_rows = set()
        self._deleted_rows = []
        self.endResetModel()

    def revert_all(self) -> None:
        self.beginResetModel()
        self._rows = [list(r) for r in self._baseline_rows]
        self._original = [list(r) for r in self._baseline_rows]
        self._dirty_rows = set()
        self._dirty_cells = set()
        self._inserted_rows = set()
        self._deleted_rows = []
        self.endResetModel()


class _EditCellCommand(QUndoCommand):
    def __init__(self, table: "ResultTable", row: int, col: int, old, new):
        super().__init__(f"编辑第 {row + 1} 行")
        self.table = table
        self.row = row
        self.col = col
        self.old = old
        self.new = new
        self._first = True

    def redo(self) -> None:
        if self._first:
            self._first = False
            return
        self.table.model.apply_value(self.row, self.col, self.new)

    def undo(self) -> None:
        self.table.model.apply_value(self.row, self.col, self.old)


class _InsertRowCommand(QUndoCommand):
    def __init__(self, table: "ResultTable", row: int):
        super().__init__("插入行")
        self.table = table
        self.row = row

    def redo(self) -> None:
        self.table.model.insert_blank_row(self.row)

    def undo(self) -> None:
        self.table.model.remove_row_at(self.row)


class _DeleteRowsCommand(QUndoCommand):
    def __init__(self, table: "ResultTable", rows: list[int], snapshot):
        super().__init__(f"删除 {len(rows)} 行")
        self.table = table
        self.rows = sorted(rows, reverse=True)
        self.snapshot = snapshot

    def redo(self) -> None:
        for r in self.rows:
            self.table.model.remove_row_at(r)

    def undo(self) -> None:
        self.table.model.restore(self.snapshot)


class _PasteCommand(QUndoCommand):
    def __init__(self, table: "ResultTable", before, after, count: int):
        super().__init__(f"粘贴 {count} 个单元格")
        self.table = table
        self.before = before
        self.after = after
        self._first = True

    def redo(self) -> None:
        if self._first:
            self._first = False
            return
        self.table.model.restore(self.after)
        self.table._update_dirty_ui()

    def undo(self) -> None:
        self.table.model.restore(self.before)
        self.table._update_dirty_ui()


class _CellViewerDialog(QDialog):
    def __init__(self, parent, column: str, text: str):
        super().__init__(parent)
        self.setWindowTitle(f"查看单元格 - {column}")
        self.setMinimumSize(720, 520)
        content = text
        stripped = text.strip()
        if stripped[:1] in ("{", "[") and stripped[-1:] in ("}", "]"):
            try:
                import json as _json
                content = _json.dumps(
                    _json.loads(stripped), ensure_ascii=False, indent=2
                )
            except Exception:  # noqa: BLE001
                pass
        self.editor = QPlainTextEdit(content)
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.editor.setStyleSheet(
            "font-family: Consolas, 'JetBrains Mono', monospace; font-size: 12px;"
        )
        tip = QLabel(f"共 {len(text)} 字符")
        tip.setProperty("role", "hint")
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        copy_btn = QPushButton("复制全部")
        copy_btn.clicked.connect(lambda: QApplication.clipboard().setText(text))
        buttons.addButton(copy_btn, QDialogButtonBox.ActionRole)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(tip)
        layout.addWidget(self.editor, 1)
        layout.addWidget(buttons)


def _vsep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.VLine)
    line.setProperty("role", "sep")
    line.setFixedWidth(1)
    return line


class ResultTable(QWidget):
    def __init__(self, parent=None, client=None, table_ctx: dict | None = None):
        super().__init__(parent)
        self.client = client
        self.table_ctx = table_ctx
        self.model = ResultModel(self)

        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setSortCaseSensitivity(Qt.CaseInsensitive)

        self.view = QTableView(self)
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.setAlternatingRowColors(True)
        self.view.setSelectionBehavior(QTableView.SelectItems)
        self.view.setSelectionMode(QTableView.ExtendedSelection)
        self.view.setWordWrap(False)
        self.view.setShowGrid(False)
        self.view.setCornerButtonEnabled(False)
        self.view.setFrameShape(QTableView.NoFrame)
        self.view.verticalHeader().setDefaultSectionSize(_DEFAULT_ROW_HEIGHT)
        self.view.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.view.horizontalHeader().setStretchLastSection(True)
        self.view.horizontalHeader().setHighlightSections(False)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._on_context_menu)
        self.view.doubleClicked.connect(self._on_double_clicked)

        self._delegate = TypedEditDelegate(self.model, self.view)
        self.view.setItemDelegate(self._delegate)

        # ---------- 搜索 ----------
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 过滤…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMaximumWidth(220)
        self.search_edit.setMinimumHeight(26)
        self.search_edit.textChanged.connect(self.proxy.setFilterFixedString)

        # ---------- 脱敏 ----------
        self.mask_box = QCheckBox("脱敏")
        self.mask_box.setToolTip("隐藏密码/token 等敏感列的显示")
        initial_mask = _config.settings().value(
            "query/mask_sensitive", True, type=bool
        )
        self.mask_box.setChecked(initial_mask)
        self.model.set_mask_enabled(initial_mask)
        self.mask_box.toggled.connect(self.model.set_mask_enabled)

        # ---------- info ----------
        self.info = QLabel("就绪")
        self.info.setObjectName("resultInfo")
        self.info.setProperty("role", "hint")

        # ---------- 撤销栈 ----------
        self.undo_stack = QUndoStack(self)
        self.undo_stack.setUndoLimit(200)
        self.undo_stack.indexChanged.connect(self._update_dirty_ui)

        # ---------- 编辑按钮 ----------
        self.undo_btn = self._make_tool_btn("↶", "撤销（Ctrl+Z）")
        self.undo_btn.setEnabled(False)
        self.undo_btn.clicked.connect(self.undo_stack.undo)

        self.redo_btn = self._make_tool_btn("↷", "重做（Ctrl+Y）")
        self.redo_btn.setEnabled(False)
        self.redo_btn.clicked.connect(self.undo_stack.redo)

        self.insert_btn = QPushButton("＋ 插入行")
        self.insert_btn.setToolTip("在结果末尾插入空行（保存时生成 INSERT）")
        self.insert_btn.setEnabled(False)
        self.insert_btn.setCursor(Qt.PointingHandCursor)
        self.insert_btn.clicked.connect(self._insert_row)

        self.delete_btn = QPushButton("－ 删除行")
        self.delete_btn.setToolTip("删除选中行（保存时生成 DELETE）")
        self.delete_btn.setEnabled(False)
        self.delete_btn.setCursor(Qt.PointingHandCursor)
        self.delete_btn.clicked.connect(self._delete_rows)

        self.save_btn = QPushButton("💾  保存修改")
        self.save_btn.setToolTip("保存所有未提交的修改（Ctrl+S）")
        self.save_btn.setObjectName("savePrimary")
        self.save_btn.setEnabled(False)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self._save_changes)

        self.revert_btn = QPushButton("↺  放弃")
        self.revert_btn.setToolTip("放弃所有未保存的修改")
        self.revert_btn.setEnabled(False)
        self.revert_btn.setCursor(Qt.PointingHandCursor)
        self.revert_btn.clicked.connect(self._revert_changes)

        # ---------- 输出按钮 ----------
        self.copy_btn = QPushButton("复制")
        self.copy_btn.setToolTip("复制选中单元格")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.clicked.connect(self.copy_selection)

        self.export_btn = QPushButton("导出 ▾")
        self.export_btn.setToolTip("导出为 CSV / JSON / Excel / INSERT")
        self.export_btn.setCursor(Qt.PointingHandCursor)
        self.export_btn.clicked.connect(self._show_export_menu)

        # ---------- 工具栏布局（分组） ----------
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 8, 10, 6)
        bar.setSpacing(6)

        # 组1：搜索 + 脱敏
        bar.addWidget(self.search_edit)
        bar.addWidget(self.mask_box)

        # 组2：info（弹性）
        bar.addWidget(_vsep())
        bar.addWidget(self.info, 1)

        # 组3：编辑
        bar.addWidget(_vsep())
        bar.addWidget(self.undo_btn)
        bar.addWidget(self.redo_btn)
        bar.addWidget(self.insert_btn)
        bar.addWidget(self.delete_btn)

        # 组4：保存 / 放弃
        bar.addSpacing(2)
        bar.addWidget(self.save_btn)
        bar.addWidget(self.revert_btn)

        # 组5：输出
        bar.addSpacing(2)
        bar.addWidget(self.copy_btn)
        bar.addWidget(self.export_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)

        self.model.cellEdited.connect(self._on_cell_edited)

        QShortcut(QKeySequence.StandardKey.Undo, self, activated=self.undo_stack.undo)
        QShortcut(QKeySequence.StandardKey.Redo, self, activated=self.undo_stack.redo)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save_changes)
        QShortcut(QKeySequence.StandardKey.Paste, self,
                  activated=self.paste_from_clipboard)

        self._apply_local_style()

    # ---------- 局部样式 ----------
    def _make_tool_btn(self, text: str, tooltip: str) -> QToolButton:
        btn = QToolButton()
        btn.setText(text)
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(28, 26)
        return btn

    def _apply_local_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QLabel#resultInfo {{
                color: {p.text_muted};
                font-size: 9pt;
                padding: 0 4px;
            }}
            QToolButton {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
                color: {p.text_muted};
                font-size: 12pt;
                font-weight: 600;
            }}
            QToolButton:hover {{
                background: {p.surface_hover};
                color: {p.accent};
                border-color: {p.border};
            }}
            QToolButton:pressed {{
                background: {p.surface_active};
            }}
            QToolButton:disabled {{
                color: {p.text_disabled};
            }}
            QPushButton#savePrimary {{
                background: {p.accent};
                color: #ffffff;
                border: 1px solid {p.accent};
                font-weight: 600;
            }}
            QPushButton#savePrimary:hover {{
                background: {p.accent_hover};
                border-color: {p.accent_hover};
                color: #ffffff;
            }}
            QPushButton#savePrimary:pressed {{
                background: {p.accent_pressed};
                border-color: {p.accent_pressed};
            }}
            QPushButton#savePrimary:disabled {{
                background: {p.surface_alt};
                color: {p.text_disabled};
                border-color: {p.border};
            }}
            QFrame[role="sep"] {{
                background: {p.border};
                border: none;
            }}
        """)

    def set_result(self, columns, rows, ctx: TableContext | None = None) -> None:
        self.undo_stack.clear()
        self.model.set_result(columns, rows, ctx)
        fm = self.view.fontMetrics()
        col_count = self.model.columnCount()
        sample_rows = min(self.model.rowCount(), 50)
        for c in range(col_count):
            name = str(self.model.headerData(c, Qt.Horizontal) or "")
            max_w = fm.horizontalAdvance(name)
            for r in range(sample_rows):
                text = self.model.data(self.model.index(r, c), Qt.DisplayRole)
                if text:
                    max_w = max(max_w, fm.horizontalAdvance(str(text)))
            self.view.setColumnWidth(c, min(max_w + 24, 320))
        editable = ctx is not None and ctx.editable and bool(ctx.primary_keys)
        self.insert_btn.setEnabled(editable)
        self.delete_btn.setEnabled(editable)
        if ctx is not None and not ctx.editable:
            tip = "当前结果集不可编辑（无主键、非表查询，或连接为只读）"
            self.insert_btn.setToolTip(tip)
            self.delete_btn.setToolTip(tip)
        self._update_dirty_ui()
        self._apply_default_sort(ctx)

    def append_rows(self, rows: list[list]) -> None:
        self.model.append_rows(rows)

    def _apply_default_sort(self, ctx: TableContext | None) -> None:
        if self.model.columnCount() == 0:
            return
        sort_col = 0
        if ctx and ctx.primary_keys:
            try:
                sort_col = self.model.columns().index(ctx.primary_keys[0])
            except ValueError:
                sort_col = 0
        self.view.sortByColumn(sort_col, Qt.AscendingOrder)
        self.view.horizontalHeader().setSortIndicator(sort_col, Qt.AscendingOrder)

    def has_pending_edits(self) -> bool:
        return self.model.has_dirty()

    def _update_dirty_ui(self, *_args) -> None:
        dirty = self.model.has_dirty()
        self.save_btn.setEnabled(dirty)
        self.revert_btn.setEnabled(dirty)
        self.undo_btn.setEnabled(self.undo_stack.canUndo())
        self.redo_btn.setEnabled(self.undo_stack.canRedo())

    def _on_cell_edited(self, row: int, col: int, old, new) -> None:
        self.undo_stack.push(_EditCellCommand(self, row, col, old, new))

    def _insert_row(self) -> None:
        if self.model._context is None or not self.model._context.editable:
            return
        self.undo_stack.push(_InsertRowCommand(self, self.model.rowCount()))

    def _delete_rows(self) -> None:
        idxs = self.view.selectionModel().selectedIndexes()
        if not idxs:
            return
        src_rows = sorted(
            {self.proxy.mapToSource(i).row() for i in idxs}, reverse=True
        )
        if not src_rows:
            return
        snap = self.model.snapshot()
        self.undo_stack.push(_DeleteRowsCommand(self, src_rows, snap))

    def paste_from_clipboard(self) -> None:
        if not self.insert_btn.isEnabled():
            QMessageBox.information(
                self, "提示",
                "当前结果集不可编辑（无主键、非表查询，或连接为只读）",
            )
            return
        text = QApplication.clipboard().text()
        if not text:
            return
        idx = self.view.currentIndex()
        if idx.isValid():
            src = self.proxy.mapToSource(idx)
            row, col = src.row(), src.column()
        else:
            row, col = 0, 0
        lines = text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n").split("\n")
        block = [line.split("\t") for line in lines]
        if len(block) == 1 and len(block[0]) == 1 and "\t" not in text:
            block = [[line] for line in lines]
        snap_before = self.model.snapshot()
        count = self.model.paste_block(row, col, block)
        if count == 0:
            return
        snap_after = self.model.snapshot()
        self.undo_stack.push(_PasteCommand(self, snap_before, snap_after, count))
        self._update_dirty_ui()
        self.info.setText(f"已粘贴 {count} 个单元格")

    def _on_double_clicked(self, index) -> None:
        if not index.isValid():
            return
        src = self.proxy.mapToSource(index)
        r, c = src.row(), src.column()
        if not (0 <= r < len(self.model._rows)
                and 0 <= c < len(self.model._columns)):
            return
        value = self.model._rows[r][c]
        if value is None:
            return

        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            try:
                decoded = raw.decode("utf-8")
                text = (
                    f"// UTF-8 decoded ({len(raw)} bytes)\n{decoded}\n\n"
                    f"// Hex\n{raw.hex(' ')}"
                )
            except UnicodeDecodeError:
                text = f"// Hex ({len(raw)} bytes)\n{raw.hex(' ')}"
            dlg = _CellViewerDialog(self, self.model.columns()[c], text)
            dlg.exec()
            return

        text = self.model._to_text(value)
        if len(text) < _MAX_CELL_CHARS:
            return
        dlg = _CellViewerDialog(self, self.model.columns()[c], text)
        dlg.exec()

    def _save_changes(self) -> None:
        if not self.client or not self.model.has_dirty():
            return
        stmts = self.model.build_pending_sql()
        if not stmts:
            self.model.mark_clean()
            self.undo_stack.clear()
            self._update_dirty_ui()
            return
        ctx = self.model._context
        schema = ctx.schema if ctx is not None else None
        try:
            results = self.client.execute_many(
                "\n".join(stmts), schema=schema,
            )
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        failed = [r for r in results if not r.success]
        if failed:
            QMessageBox.critical(self, "保存失败", failed[0].message)
            self.info.setText(f"保存失败：{failed[0].message}")
            return
        self.model.mark_clean()
        self.undo_stack.clear()
        self._update_dirty_ui()
        self.info.setText(f"已保存 {len(stmts)} 条修改")
        if self.table_ctx and self.table_ctx.get("table") and self.client:
            try:
                self.client.invalidate_metadata()
            except Exception:  # noqa: BLE001
                pass

    def _revert_changes(self) -> None:
        if not self.model.has_dirty():
            return
        if QMessageBox.question(
            self, "确认", "放弃所有未保存的修改？"
        ) != QMessageBox.Yes:
            return
        self.undo_stack.clear()
        self.model.revert_all()
        self._update_dirty_ui()

    def _on_context_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("复制选中", self.copy_selection)
        menu.addAction("复制当前单元格", self._copy_current)
        menu.addAction("复制列名", self._copy_column_name)
        menu.addAction("复制整行", self._copy_current_row)
        menu.addSeparator()
        sql_menu = menu.addMenu("复制为 SQL…")
        sql_menu.addAction("字面量（literal）", self._copy_as_sql_literal)
        sql_menu.addAction("WHERE 条件（主键/整行）", self._copy_as_where)
        sql_menu.addAction("INSERT 语句（当前行）", self._copy_as_insert)
        sql_menu.addAction("SELECT 列名列表", self._copy_as_column_list)
        menu.addSeparator()
        if self.insert_btn.isEnabled():
            menu.addAction("粘贴", self.paste_from_clipboard)
            menu.addAction("插入行", self._insert_row)
            menu.addAction("删除选中行", self._delete_rows)
            menu.addSeparator()
        menu.addAction("全选", self.view.selectAll)
        menu.exec(self.view.viewport().mapToGlobal(pos))

    def _copy_current(self) -> None:
        idx = self.view.currentIndex()
        if not idx.isValid():
            return
        QApplication.clipboard().setText(self.proxy.data(idx) or "")

    def _copy_column_name(self) -> None:
        idx = self.view.currentIndex()
        if not idx.isValid():
            return
        src = self.proxy.mapToSource(idx)
        name = self.model.headerData(src.column(), Qt.Horizontal)
        if name:
            QApplication.clipboard().setText(str(name))

    def _copy_current_row(self) -> None:
        idx = self.view.currentIndex()
        if not idx.isValid():
            return
        src_row = self.proxy.mapToSource(idx).row()
        cols = range(self.model.columnCount())
        text = "\t".join(
            str(self.model.data(self.model.index(src_row, c)) or "") for c in cols
        )
        QApplication.clipboard().setText(text)

    # ---------- 值 → SQL ----------
    def _current_source(self) -> tuple[int, int] | None:
        idx = self.view.currentIndex()
        if not idx.isValid():
            return None
        src = self.proxy.mapToSource(idx)
        return src.row(), src.column()

    def _db_type(self) -> str:
        return (self.table_ctx or {}).get("db_type", "mysql")

    def _copy_as_sql_literal(self) -> None:
        pos = self._current_source()
        if pos is None:
            return
        r, c = pos
        if not (0 <= r < len(self.model._rows) and 0 <= c < len(self.model._columns)):
            return
        value = self.model._rows[r][c]
        QApplication.clipboard().setText(sql_literal(value, self._db_type()))
        self.info.setText("已复制 SQL 字面量")

    def _copy_as_where(self) -> None:
        pos = self._current_source()
        if pos is None:
            return
        r, _ = pos
        if not (0 <= r < len(self.model._rows)):
            return
        cols = self.model.columns()
        values = self.model._rows[r]
        db = self._db_type()
        pk = self.model._pk_index or list(range(len(cols)))
        parts = [
            f"{quote_ident(cols[i], db)} = {sql_literal(values[i], db)}"
            for i in pk if i < len(values)
        ]
        if not parts:
            return
        QApplication.clipboard().setText(" AND ".join(parts))
        self.info.setText("已复制 WHERE 条件")

    def _copy_as_insert(self) -> None:
        pos = self._current_source()
        if pos is None:
            return
        r, _ = pos
        if not (0 <= r < len(self.model._rows)):
            return
        db = self._db_type()
        ctx = self.table_ctx or {}
        qualified = qualify_table(ctx.get("table", "table"), ctx.get("schema"), db)
        cols = self.model.columns()
        values = self.model._rows[r]
        col_list = ", ".join(quote_ident(c, db) for c in cols)
        val_list = ", ".join(sql_literal(v, db) for v in values)
        QApplication.clipboard().setText(
            f"INSERT INTO {qualified} ({col_list}) VALUES ({val_list});"
        )
        self.info.setText("已复制 INSERT 语句")

    def _copy_as_column_list(self) -> None:
        db = self._db_type()
        names = ", ".join(quote_ident(c, db) for c in self.model.columns())
        if names:
            QApplication.clipboard().setText(names)
            self.info.setText("已复制列名列表")

    def copy_selection(self) -> None:
        indexes = self.view.selectionModel().selectedIndexes()
        if not indexes:
            return
        rows: dict[int, dict[int, str]] = {}
        for idx in indexes:
            src = self.proxy.mapToSource(idx)
            rows.setdefault(src.row(), {})[src.column()] = self.model.data(src) or ""
        lines = [
            "\t".join(cells.get(c, "") for c in sorted(cells))
            for _, cells in sorted(rows.items())
        ]
        QApplication.clipboard().setText("\n".join(lines))
        self.info.setText(f"已复制 {len(lines)} 行")

    # ---------- 导出 ----------
    def _visible_source_rows(self) -> list[int]:
        rows: list[int] = []
        for r in range(self.proxy.rowCount()):
            src = self.proxy.mapToSource(self.proxy.index(r, 0))
            if src.isValid():
                rows.append(src.row())
        return rows

    def _filtered(self) -> bool:
        return self.proxy.rowCount() != self.model.rowCount()

    def _show_export_menu(self) -> None:
        if self.model.rowCount() == 0:
            QMessageBox.information(self, "提示", "没有可导出的数据")
            return
        menu = QMenu(self)
        for kind, label in (("csv", "CSV"), ("json", "JSON"),
                            ("xlsx", "Excel"), ("sql", "INSERT")):
            menu.addAction(f"{label}（全部）…", lambda k=kind: self._export(k))
        if self._filtered():
            menu.addSeparator()
            for kind, label in (("csv", "CSV"), ("json", "JSON"),
                                ("xlsx", "Excel"), ("sql", "INSERT")):
                menu.addAction(
                    f"{label}（过滤结果）…",
                    lambda k=kind: self._export(k, filtered_only=True),
                )
        menu.addSeparator()
        for kind, label in (("csv", "CSV"), ("json", "JSON"),
                            ("xlsx", "Excel"), ("sql", "INSERT")):
            menu.addAction(
                f"{label}（选中）…",
                lambda k=kind: self._export(k, selected_only=True),
            )
        menu.exec(self.export_btn.mapToGlobal(self.export_btn.rect().bottomLeft()))

    def _selected_source_rows(self) -> list[int]:
        idxs = self.view.selectionModel().selectedIndexes()
        return sorted({self.proxy.mapToSource(i).row() for i in idxs})

    def _export(self, kind: str, selected_only: bool = False,
                filtered_only: bool = False) -> None:
        filters = {
            "csv": ("CSV 文件 (*.csv)", ".csv"),
            "json": ("JSON 文件 (*.json)", ".json"),
            "xlsx": ("Excel 文件 (*.xlsx)", ".xlsx"),
            "sql": ("SQL 文件 (*.sql)", ".sql"),
        }
        title, ext = filters[kind]
        suffix = "_selected" if selected_only else (
            "_filtered" if filtered_only else ""
        )

        all_columns = self.model.columns()
        ctx = self.table_ctx or {}
        db_type = ctx.get("db_type", "mysql")
        table = ctx.get("table", "table")

        dlg = ExportDialog(
            self, columns=all_columns, table=table,
            db_type=db_type, kind=kind,
        )
        if dlg.exec() != dlg.DialogCode.Accepted:
            return
        keep_indices = dlg.result_column_indices()
        columns = [all_columns[i] for i in keep_indices]
        limit = dlg.result_limit()
        encoding = dlg.result_encoding()
        batch = dlg.result_batch()

        if selected_only:
            idxs = self._selected_source_rows()
            if not idxs:
                QMessageBox.information(self, "提示", "未选中任何行。")
                return
        elif filtered_only:
            idxs = self._visible_source_rows()
            if not idxs:
                QMessageBox.information(self, "提示", "过滤后没有可见行。")
                return
        else:
            idxs = list(range(self.model.rowCount()))

        all_rows = self.model.rows()
        filtered_rows: list[list] = []
        for i in idxs:
            row = all_rows[i]
            filtered_rows.append([row[j] for j in keep_indices])
        if limit > 0:
            filtered_rows = filtered_rows[:limit]

        path, _ = QFileDialog.getSaveFileName(
            self, f"导出 {kind.upper()}{suffix}",
            f"result{suffix}{ext}", title,
        )
        if not path:
            return

        note = ""
        if limit > 0 and (selected_only or filtered_only):
            note = f"\n（已按上限 {limit} 行截断）"

        try:
            if kind == "csv":
                io_tools.export_csv(path, columns, filtered_rows, encoding=encoding)
            elif kind == "json":
                io_tools.export_json(path, columns, filtered_rows)
            elif kind == "xlsx":
                io_tools.export_xlsx(path, columns, filtered_rows)
            elif kind == "sql":
                io_tools.export_insert(
                    path, table, columns, filtered_rows,
                    ctx.get("schema"), db_type, batch=batch,
                )
            self.info.setText(f"已导出 {len(filtered_rows)} 行 → {path}{note}")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导出失败", str(exc))