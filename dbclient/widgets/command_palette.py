"""命令面板：Ctrl+P 快速跳转对象 / Ctrl+Shift+P 执行命令。

界面 v2（2024 美化重构）：
- 弹窗圆角 + 细边框
- 输入框大号无边框，聚焦时强调色描边
- 列表项两行布局（主 / 次），层级清晰
- 底部提示 chip 化
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor, QFont, QKeyEvent
from PySide6.QtWidgets import (
    QDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout,
)

from ..theme import theme_manager

log = logging.getLogger(__name__)

COMMAND_PREFIX = ">"
_MAX_RESULTS = 200


@dataclass
class PaletteItem:
    """命令面板的一条候选。"""
    label: str
    detail: str = ""
    action: Callable[[], None] | None = None
    kind: str = "command"       # command / table / view / tab / conn / db
    payload: tuple = field(default_factory=tuple)
    shortcut: str = ""


def _fuzzy_score(query: str, text: str) -> int:
    """子串 / 子序列模糊评分。不匹配返回 -1。"""
    if not query:
        return 0
    q = query.lower()
    t = text.lower()
    idx = t.find(q)
    if idx >= 0:
        base = 2000 - min(idx, 200)
        if q == t:
            base += 500
        return base
    ti = 0
    score = 0
    last = -2
    for qc in q:
        found = t.find(qc, ti)
        if found < 0:
            return -1
        score += 3 if found == last + 1 else 1
        last = found
        ti = found + 1
    return score


class CommandPalette(QDialog):
    """非模态命令面板。打开前由调用方填充候选。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("命令面板")
        self.setModal(False)
        self.setWindowFlag(Qt.Tool, True)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setMinimumWidth(680)

        self._items: list[PaletteItem] = []

        # ---------- 输入框 ----------
        self.input = QLineEdit(self)
        self.input.setPlaceholderText("输入以搜索…")
        self.input.setClearButtonEnabled(True)
        self.input.setMinimumHeight(44)
        self.input.textChanged.connect(self._refresh)
        self.input.installEventFilter(self)

        # ---------- 提示 ----------
        self.hint_label = QLabel("", self)
        self.hint_label.setObjectName("paletteHint")

        # ---------- 列表 ----------
        self.list = QListWidget(self)
        self.list.setObjectName("paletteList")
        self.list.setUniformItemSizes(False)
        self.list.itemActivated.connect(self._activate)
        self.list.itemClicked.connect(self._activate)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(8)
        layout.addWidget(self.input)
        layout.addWidget(self.hint_label)
        layout.addWidget(self.list, 1)

        self._apply_style()

    # ---------- 样式 ----------
    def _apply_style(self) -> None:
        p = theme_manager.palette
        border_color = p.border_strong if p.is_dark else p.border
        self.setStyleSheet(f"""
            QDialog {{
                background: {p.surface};
                border: 1px solid {border_color};
                border-radius: 14px;
            }}
            QLineEdit#/* placeholder */ {{
                background: transparent;
            }}
            QLineEdit {{
                background: {p.surface_alt};
                border: 1.5px solid transparent;
                border-radius: 10px;
                padding: 6px 16px;
                color: {p.text};
                font-size: 11pt;
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
            QLabel#paletteHint {{
                color: {p.text_muted};
                font-size: 9pt;
                padding: 0 6px 2px 6px;
            }}
            QListWidget#paletteList {{
                background: transparent;
                border: none;
                outline: 0;
                padding: 2px;
                color: {p.text};
            }}
            QListWidget#paletteList::item {{
                padding: 9px 12px;
                border-radius: 8px;
                margin: 1px 2px;
                min-height: 22px;
            }}
            QListWidget#paletteList::item:hover {{
                background: {p.surface_hover};
            }}
            QListWidget#paletteList::item:selected {{
                background: {p.accent_soft};
                color: {p.accent_text};
            }}
        """)

    # ---------- 对外 ----------
    def open_with(
        self,
        items: list[PaletteItem],
        *,
        placeholder: str = "输入以搜索…",
        initial: str = "",
        hint: str = "",
    ) -> None:
        self._items = list(items)
        self.input.setPlaceholderText(placeholder)
        self.input.blockSignals(True)
        self.input.setText(initial)
        self.input.blockSignals(False)
        self._refresh(initial)
        self.hint_label.setText(hint or f"共 {len(self._items)} 项")
        self._reposition()
        self.show()
        self.raise_()
        self.activateWindow()
        self.input.setFocus()
        if initial:
            self.input.selectAll()

    # ---------- 内部 ----------
    def _reposition(self) -> None:
        parent = self.parent()
        if parent is None:
            return
        try:
            pg = parent.frameGeometry()
        except Exception:  # noqa: BLE001
            pg = parent.geometry()
        w = max(680, min(pg.width() - 120, 920))
        h = min(480, max(320, pg.height() // 2))
        x = pg.x() + (pg.width() - w) // 2
        y = pg.y() + max(48, pg.height() // 6)
        self.resize(w, h)
        self.move(x, y)

    @staticmethod
    def _parse_query(text: str) -> tuple[bool, str]:
        t = (text or "").lstrip()
        if t.startswith(COMMAND_PREFIX):
            return True, t[len(COMMAND_PREFIX):].strip()
        return False, t.strip()

    def _refresh(self, text: str) -> None:
        command_mode, query = self._parse_query(text)
        pool = [
            it for it in self._items
            if (it.kind == "command") == command_mode
        ]
        if not pool:
            pool = list(self._items)

        if query:
            scored: list[tuple[int, PaletteItem]] = []
            for it in pool:
                s1 = _fuzzy_score(query, it.label)
                s2 = _fuzzy_score(query, it.detail) if it.detail else -1
                s = max(s1, s2)
                if s >= 0:
                    scored.append((s, it))
            scored.sort(key=lambda x: -x[0])
            pool = [it for _, it in scored]

        self.list.clear()
        for it in pool[:_MAX_RESULTS]:
            # 两行：主行 = label (+ shortcut)，次行 = detail
            main = it.label
            if it.detail:
                main = f"{main}\n{it.detail}"
            if it.shortcut:
                main = f"{main}\n{it.shortcut}"
            item = QListWidgetItem(main)
            item.setData(Qt.UserRole, it)
            self.list.addItem(item)

        if self.list.count():
            self.list.setCurrentRow(0)

        mode = "命令" if command_mode else "对象"
        tail = "Enter 执行" if command_mode else "Enter 跳转"
        self.hint_label.setText(
            f"{mode}  ·  {self.list.count()} 项  ·  {tail}  ·  Esc 关闭"
        )

    def _activate(self, item: QListWidgetItem) -> None:
        if item is None:
            return
        data: PaletteItem | None = item.data(Qt.UserRole)
        if data is None or data.action is None:
            return
        self.hide()
        try:
            data.action()
        except Exception:  # noqa: BLE001
            log.exception("命令面板动作执行失败")

    # ---------- 键盘 ----------
    def eventFilter(self, obj, event):
        if obj is self.input and event.type() == QEvent.KeyPress \
                and isinstance(event, QKeyEvent):
            key = event.key()
            if key == Qt.Key_Escape:
                self.hide()
                return True
            if key == Qt.Key_Down:
                row = self.list.currentRow()
                if self.list.count():
                    self.list.setCurrentRow(min(row + 1, self.list.count() - 1))
                return True
            if key == Qt.Key_Up:
                row = self.list.currentRow()
                if row > 0:
                    self.list.setCurrentRow(row - 1)
                elif self.list.count():
                    self.list.setCurrentRow(0)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                cur = self.list.currentItem()
                if cur is not None:
                    self._activate(cur)
                return True
        return super().eventFilter(obj, event)