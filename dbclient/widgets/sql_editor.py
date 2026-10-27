"""带语法高亮、行号栏、自动补全（支持表名上下文）、当前语句高亮、
语句块执行、查找/替换、行操作、括号自动闭合、智能缩进的 SQL 编辑器。

界面 v2（2024 美化重构）：
- 编辑区 padding 加大，呼吸感更强
- 行号栏右侧细分隔线，当前行号加粗强调色
- 当前行 / 当前语句高亮层次更清晰
- 查找栏样式统一，间距舒展
"""
from __future__ import annotations

import re

from PySide6.QtCore import QRegularExpression, QSize, QStringListModel, Qt
from PySide6.QtGui import (
    QColor, QFont, QKeySequence, QPainter, QShortcut, QSyntaxHighlighter,
    QTextCharFormat, QTextCursor, QTextDocument, QTextFormat,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QCompleter, QDialog, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from ..config import settings as _settings
from ..engine import split_statements, statement_bounds
from ..theme import Palette, theme_manager

KEYWORDS = """
SELECT FROM WHERE INSERT INTO VALUES UPDATE SET DELETE CREATE TABLE ALTER DROP
INDEX VIEW TRIGGER PROCEDURE FUNCTION DATABASE SCHEMA JOIN INNER LEFT RIGHT FULL
OUTER CROSS ON AS AND OR NOT NULL IS IN BETWEEN LIKE EXISTS GROUP BY ORDER HAVING
LIMIT OFFSET UNION ALL DISTINCT CASE WHEN THEN ELSE END PRIMARY KEY FOREIGN
REFERENCES UNIQUE DEFAULT AUTO_INCREMENT COMMENT INT INTEGER BIGINT SMALLINT
TINYINT DECIMAL NUMERIC FLOAT DOUBLE REAL CHAR VARCHAR TEXT BLOB DATE TIME
DATETIME TIMESTAMP BOOLEAN BIT JSON UUID SERIAL DESC ASC WITH RECURSIVE EXPLAIN
ANALYZE BEGIN COMMIT ROLLBACK TRANSACTION GRANT REVOKE IF CASCADE CONSTRAINT ADD
COLUMN RENAME TO TRUNCATE USE SHOW DESCRIBE TRUE FALSE
""".split()

_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def format_sql(sql: str, *, keyword_upper: bool = True,
               reindent: bool = True, strip_comments: bool = False) -> str:
    if not sql or not sql.strip():
        return sql
    try:
        import sqlparse
    except ImportError:
        return sql
    try:
        return sqlparse.format(
            sql,
            keyword_case="upper" if keyword_upper else None,
            identifier_case=None,
            reindent=reindent,
            strip_comments=strip_comments,
            use_space_around_operators=True,
            comma_first=False,
        )
    except Exception:  # noqa: BLE001
        return sql


def _has_meaningful_content(stmt: str) -> bool:
    """语句去掉注释后是否还有实质内容。"""
    s = _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(" ", stmt))
    return bool(s.strip())


def _find_fullwidth_semicolon(text: str) -> int | None:
    """返回第一个处于「非字符串、非注释」区域的全角分号位置；找不到返回 None。"""
    n = len(text)
    i = 0
    quote: str | None = None
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            elif ch == "\\" and i + 1 < n:
                i += 2
                continue
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue
        if text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch == "；":
            return i
        i += 1
    return None


def validate_sql(sql: str) -> str | None:
    """按 SQL 语法规则校验待执行命令。返回 None 表示通过。"""
    text = (sql or "").strip()
    if not text:
        return "请先输入要执行的 SQL。"

    pos = _find_fullwidth_semicolon(text)
    if pos is not None:
        line_no = text[:pos].count("\n") + 1
        return (
            "SQL 语句请使用英文半角分号「;」。\n\n"
            "检测到中文全角分号「；」——它不是 SQL 的语句分隔符，\n"
            "会被解析为普通字符，导致语法错误。\n"
            "请改用英文分号「;」。\n\n"
            f"（位置：第 {line_no} 行）"
        )

    statements = split_statements(text)
    if not any(_has_meaningful_content(s) for s in statements):
        return "没有可执行的 SQL 语句（输入可能只有注释或空白）。"

    return None


class SqlHighlighter(QSyntaxHighlighter):
    def __init__(self, document, palette: Palette):
        super().__init__(document)
        self._palette = palette
        self._rules: list[tuple[QRegularExpression, QTextCharFormat]] = []
        self._build_rules()

    def set_palette(self, palette: Palette) -> None:
        self._palette = palette
        self._build_rules()
        self.rehighlight()

    def _build_rules(self) -> None:
        p = self._palette
        rules: list[tuple[QRegularExpression, QTextCharFormat]] = []

        kw_fmt = QTextCharFormat()
        kw_fmt.setForeground(QColor(p.syn_kw))
        kw_fmt.setFontWeight(QFont.Bold)
        rules.append((
            QRegularExpression(
                r"\b(" + "|".join(KEYWORDS) + r")\b",
                QRegularExpression.CaseInsensitiveOption,
            ),
            kw_fmt,
        ))
        num_fmt = QTextCharFormat()
        num_fmt.setForeground(QColor(p.syn_num))
        rules.append((QRegularExpression(r"\b\d+(\.\d+)?\b"), num_fmt))
        str_fmt = QTextCharFormat()
        str_fmt.setForeground(QColor(p.syn_str))
        rules.append((QRegularExpression(r"'([^'\\]|\\.|'')*'"), str_fmt))
        id_fmt = QTextCharFormat()
        id_fmt.setForeground(QColor(p.syn_ident))
        rules.append((QRegularExpression(r"`[^`]*`"), id_fmt))
        rules.append((QRegularExpression(r'"[^"\n]*"'), id_fmt))
        cmt_fmt = QTextCharFormat()
        cmt_fmt.setForeground(QColor(p.syn_cmt))
        cmt_fmt.setFontItalic(True)
        rules.append((QRegularExpression(r"--[^\n]*"), cmt_fmt))
        rules.append((QRegularExpression(r"/\*.*?\*/"), cmt_fmt))
        self._rules = rules

    def highlightBlock(self, text: str) -> None:
        for pattern, fmt in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)


class SqlCompleter(QCompleter):
    """带缓存与增量更新的补全器。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = QStringListModel([], self)
        self.setModel(self._model)
        self.setCaseSensitivity(Qt.CaseInsensitive)
        self.setCompletionMode(QCompleter.PopupCompletion)
        self.setWrapAround(False)
        self._words: set[str] = set()
        self._sorted: list[str] = []

    def set_words(self, words: list[str]) -> None:
        new = {w for w in words if w}
        if new == self._words:
            return
        self._words = new
        self._sorted = sorted(new)
        self._model.setStringList(self._sorted)

    def add_words(self, words: list[str]) -> None:
        added = False
        for w in words:
            if w and w not in self._words:
                self._words.add(w)
                added = True
        if added:
            self._sorted = sorted(self._words)
            self._model.setStringList(self._sorted)


class LineNumberArea(QWidget):
    def __init__(self, editor: "SqlEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.editor.line_number_area_paint_event(event)


class _FindBar(QDialog):
    """轻量查找 / 替换浮层，复用 QPlainTextEdit.find()。"""

    def __init__(self, editor: "SqlEditor", show_replace: bool = False):
        super().__init__(editor)
        self.editor = editor
        self.setWindowTitle("查找 / 替换" if show_replace else "查找")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.Tool, True)
        self.setModal(False)

        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("查找内容")
        self.find_edit.setMinimumWidth(240)
        self.find_edit.setMinimumHeight(28)
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("替换为")
        self.replace_edit.setMinimumHeight(28)
        self.case_box = QCheckBox("区分大小写")
        self.regex_box = QCheckBox("正则")
        self.wrap_box = QCheckBox("循环")
        self.wrap_box.setChecked(True)

        next_btn = QPushButton("下一个")
        prev_btn = QPushButton("上一个")
        close_btn = QPushButton("关闭")
        replace_btn = QPushButton("替换")
        replace_all_btn = QPushButton("全部替换")
        for b in (next_btn, prev_btn, close_btn, replace_btn, replace_all_btn):
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(28)
        for b in (replace_btn, replace_all_btn):
            b.setVisible(show_replace)

        next_btn.clicked.connect(lambda: self._find(True))
        prev_btn.clicked.connect(lambda: self._find(False))
        close_btn.clicked.connect(self.close)
        replace_btn.clicked.connect(self._replace)
        replace_all_btn.clicked.connect(self._replace_all)
        self.find_edit.returnPressed.connect(lambda: self._find(True))
        self.find_edit.textChanged.connect(lambda _t: self._find(True))

        rows = QVBoxLayout(self)
        rows.setContentsMargins(12, 10, 12, 10)
        rows.setSpacing(8)
        r1 = QHBoxLayout()
        r1.setSpacing(6)
        r1.addWidget(QLabel("查找"))
        r1.addWidget(self.find_edit, 1)
        r1.addWidget(next_btn)
        r1.addWidget(prev_btn)
        rows.addLayout(r1)
        if show_replace:
            r2 = QHBoxLayout()
            r2.setSpacing(6)
            r2.addWidget(QLabel("替换"))
            r2.addWidget(self.replace_edit, 1)
            r2.addWidget(replace_btn)
            r2.addWidget(replace_all_btn)
            rows.addLayout(r2)
        r3 = QHBoxLayout()
        r3.setSpacing(8)
        r3.addWidget(self.case_box)
        r3.addWidget(self.regex_box)
        r3.addWidget(self.wrap_box)
        r3.addStretch(1)
        r3.addWidget(close_btn)
        rows.addLayout(r3)

        self._apply_style()

    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QDialog {{
                background: {p.surface};
            }}
            QLabel {{
                color: {p.text_muted};
                font-weight: 500;
            }}
            QLineEdit {{
                background: {p.surface_sunken};
                border: 1px solid {p.border};
                border-radius: 7px;
                padding: 5px 10px;
                color: {p.text};
            }}
            QLineEdit:focus {{
                border-color: {p.accent};
                background: {p.surface};
            }}
            QPushButton {{
                padding: 5px 14px;
                border-radius: 7px;
            }}
            QCheckBox {{
                spacing: 6px;
                color: {p.text_muted};
            }}
        """)

    def _flags(self, backward: bool = False):
        flags = QTextDocument.FindFlags()
        if backward:
            flags |= QTextDocument.FindBackward
        if self.case_box.isChecked():
            flags |= QTextDocument.FindCaseSensitively
        if self.regex_box.isChecked():
            flags |= QTextDocument.FindUseRegExp
        return flags

    def _find(self, forward: bool = True) -> bool:
        text = self.find_edit.text()
        if not text:
            return False
        if self.editor.find(text, self._flags(not forward)):
            return True
        if not self.wrap_box.isChecked():
            return False
        cur = self.editor.textCursor()
        cur.movePosition(QTextCursor.End if not forward else QTextCursor.Start)
        self.editor.setTextCursor(cur)
        return self.editor.find(text, self._flags(not forward))

    def _replace(self) -> None:
        text = self.find_edit.text()
        if not text:
            return
        cur = self.editor.textCursor()
        if cur.hasSelection() and cur.selectedText() == text:
            cur.insertText(self.replace_edit.text())
            self.editor.setTextCursor(cur)
        self._find(True)

    def _replace_all(self) -> None:
        text = self.find_edit.text()
        if not text:
            return
        cur = self.editor.textCursor()
        cur.movePosition(QTextCursor.Start)
        self.editor.setTextCursor(cur)
        n = 0
        while self._find(True):
            c = self.editor.textCursor()
            if not c.hasSelection():
                break
            c.insertText(self.replace_edit.text())
            self.editor.setTextCursor(c)
            n += 1
            if n > 10000:
                break

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)


class SqlEditor(QPlainTextEdit):
    _TABLE_CTX_KEYWORDS = frozenset((
        "from", "join", "into", "update", "table", "view",
        "inner", "left", "right", "outer", "full", "cross",
        "describe", "desc", "explain", "alter", "truncate", "drop",
    ))

    # ---- 自动闭合配对表 ----
    _AUTO_PAIRS = {
        "(": ")",
        "[": "]",
        "{": "}",
        "'": "'",
        '"': '"',
        "`": "`",
    }
    _CLOSERS = frozenset((")", "]", "}", "'", '"', "`"))

    # ---- 智能缩进 ----
    _INDENT_UNIT = "    "
    _BLOCK_KEYWORDS = frozenset((
        "select", "from", "where", "and", "or", "on",
        "join", "inner", "left", "right", "outer", "full", "cross",
        "group", "order", "having", "set", "values",
        "insert", "update", "delete", "into",
        "union", "except", "intersect",
        "begin", "case", "when", "then", "else", "with",
    ))

    def __init__(self, parent=None):
        super().__init__(parent)
        font_size = int(_settings().value("ui/font_size", 11))
        font = QFont("JetBrains Mono")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(font_size)
        if not font.exactMatch():
            font = QFont("Consolas")
            font.setStyleHint(QFont.Monospace)
            font.setPointSize(font_size)
        self.setFont(font)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setFrameShape(QPlainTextEdit.NoFrame)

        self._palette = theme_manager.palette
        self.highlighter = SqlHighlighter(self.document(), self._palette)

        self.completer = SqlCompleter(self)
        self.completer.setWidget(self)
        self.completer.activated.connect(self._insert_completion)
        self._popup_prefix = ""

        self._base_words: list[str] = list(KEYWORDS)
        self._table_columns: dict[str, list[str]] = {}
        self._table_names: list[str] = []

        self._line_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_area_width)
        self.updateRequest.connect(self._update_line_area)
        self.cursorPositionChanged.connect(self._highlight_current_line)
        self._update_line_area_width(0)
        self._highlight_current_line()

        self._apply_style(self._palette)
        theme_manager.changed.connect(self._on_theme_changed)
        self.destroyed.connect(self._on_destroyed)

        self._find_bar: _FindBar | None = None

        # 语句边界缓存
        self._doc_rev = 0
        self._stmt_cache: tuple[int, int, int] | None = None
        self.document().contentsChanged.connect(self._bump_doc_rev)

        QShortcut(QKeySequence("Ctrl+Shift+F"), self, activated=self.format_selection)
        QShortcut(QKeySequence.StandardKey.Find, self,
                  activated=lambda: self.show_find(False))
        QShortcut(QKeySequence("Ctrl+H"), self,
                  activated=lambda: self.show_find(True))

        # 行操作快捷键
        QShortcut(QKeySequence("Ctrl+D"), self,
                  activated=self._duplicate_selection)
        QShortcut(QKeySequence("Ctrl+Shift+D"), self,
                  activated=self._delete_current_line)
        QShortcut(QKeySequence("Ctrl+/"), self,
                  activated=self._toggle_line_comment)
        QShortcut(QKeySequence("Alt+Up"), self,
                  activated=lambda: self._move_current_line(-1))
        QShortcut(QKeySequence("Alt+Down"), self,
                  activated=lambda: self._move_current_line(1))

    def _on_destroyed(self, *_args) -> None:
        try:
            theme_manager.changed.disconnect(self._on_theme_changed)
        except (TypeError, RuntimeError):
            pass

    def set_font_size(self, size: int) -> None:
        size = max(6, min(72, int(size)))
        font = QFont(self.font())
        font.setPointSize(size)
        self.setFont(font)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self._update_line_area_width(0)
        self._highlight_current_line()

    # ---------- 补全数据源 ----------
    def set_completion_words(self, words: list[str]) -> None:
        seen: set[str] = set()
        merged: list[str] = []
        for w in list(KEYWORDS) + list(words or []):
            if w and w not in seen:
                seen.add(w)
                merged.append(w)
        self._base_words = merged

    def add_completion_words(self, words: list[str]) -> None:
        existing = set(self._base_words)
        for w in words or []:
            if w and w not in existing:
                self._base_words.append(w)
                existing.add(w)

    def set_table_columns(self, mapping: dict[str, list[str]]) -> None:
        self._table_columns = {
            k.lower(): list(v) for k, v in (mapping or {}).items()
        }
        self._table_names = sorted((mapping or {}).keys())

    def _refresh_completion_words(
        self, table_hint: str | None, context: str = ""
    ) -> None:
        if table_hint:
            cols = self._table_columns.get(table_hint.lower())
            if cols:
                self.completer.set_words(cols)
                return
        if context == "table":
            names = getattr(self, "_table_names", []) or []
            if names:
                self.completer.set_words(list(KEYWORDS) + names)
                return
        self.completer.set_words(self._base_words)

    def _detect_context(self) -> str:
        cursor = self.textCursor()
        block_text = cursor.block().text()[: cursor.positionInBlock()]
        i = len(block_text) - 1
        while i >= 0 and (block_text[i].isalnum() or block_text[i] in "_.$"):
            i -= 1
        before = block_text[: i + 1]
        words = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", before)
        if not words:
            return ""
        if words[-1].lower() in self._TABLE_CTX_KEYWORDS:
            return "table"
        return ""

    # ---------- 格式化 ----------
    def format_selection(self) -> None:
        cursor = self.textCursor()
        if cursor.hasSelection():
            text = cursor.selectedText().replace("\u2029", "\n")
            cursor.insertText(format_sql(text))
            self.setTextCursor(cursor)
        else:
            pos = cursor.position()
            self.setPlainText(format_sql(self.toPlainText()))
            c = self.textCursor()
            c.setPosition(min(pos, len(self.toPlainText())))
            self.setTextCursor(c)

    # ---------- 主题 ----------
    def _on_theme_changed(self, palette: Palette) -> None:
        self._palette = palette
        self.highlighter.set_palette(palette)
        self._apply_style(palette)
        self._highlight_current_line()
        self._line_area.update()

    def _apply_style(self, p: Palette) -> None:
        self.setStyleSheet(f"""
            QPlainTextEdit {{
                background: {p.editor_bg};
                color: {p.text};
                selection-background-color: {p.editor_selection};
                selection-color: {p.text};
                border: none;
                padding: 10px 14px;
            }}
        """)
        self.setCursorWidth(2)

    # ---------- 行号栏 ----------
    def line_number_area_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return 18 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_line_area_width(self, _count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_area(self, rect, dy: int) -> None:
        if dy:
            self._line_area.scroll(0, dy)
        else:
            self._line_area.update(0, rect.y(), self._line_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_area_width(0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self._line_area.setGeometry(
            cr.left(), cr.top(), self.line_number_area_width(), cr.height()
        )

    def line_number_area_paint_event(self, event) -> None:
        p = self._palette
        painter = QPainter(self._line_area)
        painter.fillRect(event.rect(), QColor(p.editor_gutter))

        # 右侧分隔细线
        painter.setPen(QColor(p.border))
        painter.drawLine(
            self._line_area.width() - 1, event.rect().top(),
            self._line_area.width() - 1, event.rect().bottom(),
        )

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        bottom = top + self.blockBoundingRect(block).height()
        height = self.fontMetrics().height()
        current_block = self.textCursor().blockNumber()

        regular_font = self.font()
        active_font = QFont(regular_font)
        active_font.setBold(True)

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                if block_number == current_block:
                    painter.setFont(active_font)
                    painter.setPen(QColor(p.accent))
                else:
                    painter.setFont(regular_font)
                    painter.setPen(QColor(p.editor_gutter_text))
                painter.drawText(
                    0, int(top),
                    self._line_area.width() - 10, height,
                    Qt.AlignRight, number,
                )
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_number += 1

    def _highlight_current_line(self) -> None:
        extra: list[QTextEdit.ExtraSelection] = []
        if not self.isReadOnly():
            text = self.toPlainText()
            if ";" in text:
                s, e = self._statement_bounds_cached(self.textCursor().position())
                if e > s:
                    stmt = QTextEdit.ExtraSelection()
                    fmt = QTextCharFormat()
                    fmt.setBackground(QColor(self._palette.accent_soft))
                    stmt.format = fmt
                    cur = self.textCursor()
                    cur.setPosition(s)
                    cur.setPosition(e, QTextCursor.KeepAnchor)
                    stmt.cursor = cur
                    extra.append(stmt)

            line = QTextEdit.ExtraSelection()
            line.format.setBackground(QColor(self._palette.editor_line))
            line.format.setProperty(QTextFormat.FullWidthSelection, True)
            line.cursor = self.textCursor()
            line.cursor.clearSelection()
            extra.append(line)

        self.setExtraSelections(extra)

    # ---------- 语句边界缓存 ----------
    def _bump_doc_rev(self) -> None:
        self._doc_rev += 1
        self._stmt_cache = None

    def _statement_bounds_cached(self, pos: int) -> tuple[int, int]:
        c = self._stmt_cache
        if c is not None and c[2] == self._doc_rev and c[0] <= pos <= c[1]:
            return c[0], c[1]
        s, e = statement_bounds(self.toPlainText(), pos)
        self._stmt_cache = (s, e, self._doc_rev)
        return s, e

    # ---------- 补全 ----------
    def _completion_prefix(self) -> tuple[str, str | None]:
        cursor = self.textCursor()
        block_text = cursor.block().text()[: cursor.positionInBlock()]
        i = len(block_text) - 1
        while i >= 0 and (block_text[i].isalnum() or block_text[i] in "_.$"):
            i -= 1
        token = block_text[i + 1:]
        if "." in token:
            head, prefix = token.rsplit(".", 1)
            table_hint = head.split(".")[-1]
            return prefix, table_hint
        return token, None

    def _insert_completion(self, text: str) -> None:
        cursor = self.textCursor()
        prefix_len = len(self._popup_prefix)
        if prefix_len:
            cursor.movePosition(
                QTextCursor.MoveOperation.Left,
                QTextCursor.MoveMode.KeepAnchor,
                prefix_len,
            )
        cursor.insertText(text)
        self.setTextCursor(cursor)

    # ---------- 括号自动闭合 ----------
    def _handle_auto_pair(self, event) -> bool:
        text = event.text()
        if text not in self._AUTO_PAIRS:
            return False
        mods = event.modifiers()
        if mods & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
            return False

        cursor = self.textCursor()
        closer = self._AUTO_PAIRS[text]

        if cursor.hasSelection():
            selected = cursor.selectedText().replace("\u2029", "\n")
            cursor.insertText(text + selected + closer)
            self.setTextCursor(cursor)
            return True

        pos = cursor.position()
        doc_text = self.toPlainText()
        prev_ch = doc_text[pos - 1] if pos > 0 else ""
        next_ch = doc_text[pos] if pos < len(doc_text) else ""

        if text in ("'", '"', "`") and prev_ch == text:
            if next_ch == text:
                cursor.movePosition(QTextCursor.Right)
                self.setTextCursor(cursor)
                return True

        if next_ch == closer:
            cursor.movePosition(QTextCursor.Right)
            self.setTextCursor(cursor)
            return True

        cursor.beginEditBlock()
        cursor.insertText(text + closer)
        cursor.endEditBlock()
        cursor.movePosition(QTextCursor.Left)
        self.setTextCursor(cursor)
        return True

    # ---------- 智能缩进 ----------
    def _handle_smart_newline(self) -> bool:
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        block = cursor.block()
        text = block.text()
        pos_in_block = cursor.positionInBlock()
        before = text[:pos_in_block]

        stripped = before.lstrip(" \t")
        line_indent = before[: len(before) - len(stripped)]

        after = text[pos_in_block:]
        if after.lstrip(" \t").startswith((")", "]")):
            cursor.beginEditBlock()
            cursor.insertText("\n" + line_indent + self._INDENT_UNIT)
            cursor.endEditBlock()
            self.setTextCursor(cursor)
            return True

        extra = ""
        content = before.rstrip()
        if content.endswith(("(", ",", "[")):
            extra = self._INDENT_UNIT
        else:
            m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*$", content)
            if m and m.group(1).lower() in self._BLOCK_KEYWORDS:
                extra = self._INDENT_UNIT

        cursor.beginEditBlock()
        cursor.insertText("\n" + line_indent + extra)
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        return True

    def keyPressEvent(self, event):
        popup = self.completer.popup()
        if popup.isVisible():
            key = event.key()
            if key in (Qt.Key_Enter, Qt.Key_Return, Qt.Key_Tab):
                current = popup.currentIndex()
                if current.isValid():
                    self._insert_completion(current.data())
                popup.hide()
                event.accept()
                return
            if key == Qt.Key_Escape:
                popup.hide()
                event.accept()
                return
            if key in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown):
                QApplication.sendEvent(popup, event)
                event.accept()
                return

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            mods = event.modifiers()
            if not (mods & (Qt.ControlModifier | Qt.AltModifier)):
                if self._handle_smart_newline():
                    return

        if self._handle_auto_pair(event):
            return

        super().keyPressEvent(event)

        prefix, table_hint = self._completion_prefix()
        context = self._detect_context()

        if len(prefix) < 2 and table_hint is None and context != "table":
            popup.hide()
            return
        if not prefix and table_hint is None and context != "table":
            popup.hide()
            return

        self._refresh_completion_words(table_hint, context)
        self._popup_prefix = prefix
        self.completer.setCompletionPrefix(prefix)
        if self.completer.completionCount() == 0:
            popup.hide()
            return

        popup.setCurrentIndex(self.completer.completionModel().index(0, 0))
        rect = self.cursorRect()
        rect.setWidth(
            popup.sizeHintForColumn(0)
            + popup.verticalScrollBar().sizeHint().width()
            + 24
        )
        self.completer.complete(rect)

    # ---------- 查找 / 替换 ----------
    def show_find(self, show_replace: bool = False) -> None:
        if self._find_bar is None or not self._find_bar.isVisible():
            self._find_bar = _FindBar(self, show_replace=show_replace)
        elif show_replace and not self._find_bar.replace_edit.isVisible():
            self._find_bar.close()
            self._find_bar = _FindBar(self, show_replace=True)

        self._find_bar.adjustSize()
        top_right = self.mapToGlobal(self.rect().topRight())
        self._find_bar.move(
            top_right.x() - self._find_bar.width() - 28,
            top_right.y() + 48,
        )
        self._find_bar.show()
        self._find_bar.raise_()
        self._find_bar.activateWindow()
        self._find_bar.find_edit.setFocus()
        sel = self.textCursor().selectedText()
        if sel and "\u2029" not in sel:
            self._find_bar.find_edit.setText(sel)
            self._find_bar.find_edit.selectAll()

    # ---------- 行操作 ----------
    def _duplicate_selection(self) -> None:
        cursor = self.textCursor()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            start = cursor.selectionStart()
            end = cursor.selectionEnd()
            sel = self.toPlainText()[start:end]
            cursor.setPosition(end)
            cursor.insertText(sel)
        else:
            cursor.select(QTextCursor.LineUnderCursor)
            line = cursor.selectedText()
            cursor.setPosition(cursor.selectionEnd())
            cursor.insertText("\n" + line)
        cursor.endEditBlock()

    def _delete_current_line(self) -> None:
        cursor = self.textCursor()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            doc = self.document()
            start_blk = doc.findBlock(cursor.selectionStart())
            end_blk = doc.findBlock(cursor.selectionEnd())
            c = QTextCursor(start_blk)
            c.setPosition(start_blk.position())
            c.setPosition(
                end_blk.position() + end_blk.length() - 1,
                QTextCursor.KeepAnchor,
            )
            c.removeSelectedText()
            if not c.atEnd():
                c.deleteChar()
        else:
            cursor.select(QTextCursor.LineUnderCursor)
            cursor.removeSelectedText()
            if not cursor.atEnd():
                cursor.deleteChar()
        cursor.endEditBlock()

    def _toggle_line_comment(self) -> None:
        cursor = self.textCursor()
        doc = self.document()
        if cursor.hasSelection():
            start = doc.findBlock(cursor.selectionStart()).blockNumber()
            end = doc.findBlock(cursor.selectionEnd()).blockNumber()
        else:
            start = end = cursor.blockNumber()

        blocks = [doc.findBlockByNumber(i) for i in range(start, end + 1)]
        all_commented = all(b.text().lstrip().startswith("--") for b in blocks)

        edit = QTextCursor(doc)
        edit.beginEditBlock()
        for b in blocks:
            text = b.text()
            if all_commented:
                stripped = len(text) - len(text.lstrip())
                after = text[stripped:]
                if after.startswith("-- "):
                    offset = stripped + 3
                elif after.startswith("--"):
                    offset = stripped + 2
                else:
                    continue
                c = QTextCursor(b)
                c.setPosition(b.position() + stripped)
                c.setPosition(b.position() + offset, QTextCursor.KeepAnchor)
                c.removeSelectedText()
            else:
                c = QTextCursor(b)
                c.setPosition(b.position())
                c.insertText("-- ")
        edit.endEditBlock()

    def _move_current_line(self, direction: int) -> None:
        cursor = self.textCursor()
        doc = self.document()
        block = cursor.block()
        block_no = block.blockNumber()
        pos_in_block = cursor.positionInBlock()

        if direction < 0 and block_no == 0:
            return
        if direction > 0 and block_no >= doc.blockCount() - 1:
            return

        target = doc.findBlockByNumber(block_no + direction)
        text_cur = block.text()
        text_tgt = target.text()

        if direction > 0:
            start_pos = block.position()
            end_pos = target.position() + len(text_tgt)
            insert_text = text_tgt + "\n" + text_cur
        else:
            start_pos = target.position()
            end_pos = block.position() + len(text_cur)
            insert_text = text_cur + "\n" + text_tgt

        edit = QTextCursor(doc)
        edit.beginEditBlock()
        edit.setPosition(start_pos)
        edit.setPosition(end_pos, QTextCursor.KeepAnchor)
        edit.insertText(insert_text)
        edit.endEditBlock()

        new_block = doc.findBlockByNumber(block_no + direction)
        if new_block.isValid():
            new_cursor = QTextCursor(doc)
            new_pos = new_block.position() + min(pos_in_block, len(new_block.text()))
            new_cursor.setPosition(new_pos)
            self.setTextCursor(new_cursor)

    # ---------- 快捷 ----------
    def selected_sql(self) -> str:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return ""
        return cursor.selectedText().replace("\u2029", "\n")

    def current_statement(self) -> str:
        text = self.toPlainText()
        pos = self.textCursor().position()
        s, e = self._statement_bounds_cached(pos)
        return text[s:e].strip()

    def sql_to_run(self) -> str:
        """优先级：选中内容 → 光标所在语句 → 全文。原样返回，不做任何改写。"""
        selected = self.selected_sql()
        if selected.strip():
            return selected.strip()
        stmt = self.current_statement()
        if stmt:
            return stmt
        return self.toPlainText().strip()