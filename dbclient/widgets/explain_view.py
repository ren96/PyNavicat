"""执行计划可视化视图：把 EXPLAIN 结果渲染成带色彩标注的树形结构。

界面 v2（2024 美化重构）：
- 顶部摘要 chip 化，严重/警告/良好三色胶囊
- 树无边框圆角，行高更大，层次更清晰
- 状态色用左侧色条 + 文字色双层表达
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QStackedWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..theme import theme_manager

STATUS_GOOD = "good"
STATUS_WARN = "warn"
STATUS_BAD = "bad"
STATUS_NONE = ""

_STATUS_RANK = {STATUS_NONE: 0, STATUS_GOOD: 1, STATUS_WARN: 2, STATUS_BAD: 3}


@dataclass
class ExplainNode:
    label: str = ""
    detail: str = ""
    status: str = STATUS_NONE
    hint: str = ""
    children: list["ExplainNode"] = field(default_factory=list)


def _better(a: str, b: str) -> str:
    return a if _STATUS_RANK.get(a, 0) >= _STATUS_RANK.get(b, 0) else b


def is_explain_result(columns: list[str], db_type: str) -> bool:
    """判断一组结果列是否是 EXPLAIN 输出。"""
    if not columns:
        return False
    lowered = [str(c).strip().lower() for c in columns]
    if db_type == "mysql":
        return "select_type" in lowered and "table" in lowered
    if db_type == "postgresql":
        return len(lowered) == 1 and "query plan" in lowered[0]
    if db_type == "sqlite":
        return "detail" in lowered and "parent" in lowered and "id" in lowered
    return False


# ---------- MySQL ----------
def _parse_mysql(columns: list[str], rows: list[tuple]) -> list[ExplainNode]:
    col_idx = {str(c).strip(): i for i, c in enumerate(columns)}
    nodes: list[ExplainNode] = []
    for r in rows:
        def get(name):
            i = col_idx.get(name)
            if i is None or i >= len(r):
                return None
            return r[i]

        table = get("table") or "?"
        select_type = str(get("select_type") or "")
        type_v = str(get("type") or "").lower()
        key = get("key")
        possible = get("possible_keys")
        rows_est = get("rows")
        extra = str(get("Extra") or "")

        status = STATUS_GOOD
        hints: list[str] = []

        if type_v == "all":
            status = _better(status, STATUS_BAD)
            hints.append("全表扫描")
        elif type_v == "index":
            status = _better(status, STATUS_WARN)
            hints.append("全索引扫描")
        elif type_v in ("range", "index_merge", "index_subquery", "unique_subquery"):
            status = _better(status, STATUS_WARN)
            hints.append(f"type={type_v}")

        if not key and possible:
            status = _better(status, STATUS_WARN)
            hints.append("有候选索引但未使用")
        elif not key and type_v not in ("const", "system"):
            status = _better(status, STATUS_WARN)
            hints.append("未使用索引")

        extra_l = extra.lower()
        if "filesort" in extra_l:
            status = _better(status, STATUS_WARN)
            hints.append("需要额外排序")
        if "temporary" in extra_l:
            status = _better(status, STATUS_BAD)
            hints.append("使用临时表")

        try:
            if rows_est is not None:
                n = int(rows_est)
                if n >= 1_000_000:
                    status = _better(status, STATUS_WARN)
                    hints.append(f"预估扫描 {n} 行")
        except (TypeError, ValueError):
            pass

        detail_bits = []
        if type_v:
            detail_bits.append(f"type={type_v}")
        if key:
            detail_bits.append(f"key={key}")
        if rows_est is not None:
            detail_bits.append(f"rows={rows_est}")
        if extra:
            detail_bits.append(extra)

        label = f"{table}"
        if select_type:
            label = f"[{select_type}] {label}"
        nodes.append(ExplainNode(
            label=label,
            detail="  ".join(detail_bits),
            status=status,
            hint="；".join(hints),
        ))
    return nodes


# ---------- PostgreSQL ----------
_INDENT_RE = re.compile(r"^(\s*)(.*)$")


def _classify_text_node(node: ExplainNode, text: str, db_type: str) -> None:
    lower = text.lower()
    if db_type == "postgresql":
        if "seq scan" in lower:
            node.status = STATUS_BAD
            node.hint = "顺序扫描（全表）"
        elif "index only scan" in lower or "index scan" in lower:
            node.status = STATUS_GOOD
        elif "bitmap index scan" in lower or "bitmap heap scan" in lower:
            node.status = STATUS_WARN
            node.hint = "位图扫描"
        elif "nested loop" in lower:
            node.status = STATUS_WARN
            node.hint = "嵌套循环连接"
        elif "hash join" in lower or "merge join" in lower:
            node.status = STATUS_GOOD
        elif "disk:" in lower and "sort" in lower:
            node.status = STATUS_BAD
            node.hint = "排序溢出到磁盘"
        elif lower.startswith("sort"):
            node.status = STATUS_WARN
        elif "materialize" in lower:
            node.status = STATUS_WARN
    elif db_type == "sqlite":
        if "scan" in lower and "search" not in lower:
            node.status = STATUS_BAD
            node.hint = "全表扫描"
        elif "search" in lower:
            node.status = STATUS_GOOD
        if "temp b-tree" in lower:
            node.status = _better(node.status, STATUS_WARN)
            node.hint = (node.hint + "；" if node.hint else "") + "临时 B-Tree"


def _parse_pg(rows: list[tuple]) -> list[ExplainNode]:
    """解析 PostgreSQL 的缩进文本计划。"""
    nodes: list[ExplainNode] = []
    stack: list[tuple[int, ExplainNode]] = []
    for r in rows:
        line = str(r[0]) if r else ""
        m = _INDENT_RE.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        text = m.group(2).rstrip()
        if not text:
            continue
        node = ExplainNode(label=text)
        _classify_text_node(node, text, "postgresql")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if stack:
            stack[-1][1].children.append(node)
        else:
            nodes.append(node)
        stack.append((indent, node))
    return nodes


# ---------- SQLite ----------
def _parse_sqlite(columns: list[str], rows: list[tuple]) -> list[ExplainNode]:
    col_idx = {str(c).strip().lower(): i for i, c in enumerate(columns)}
    id_i = col_idx.get("id")
    parent_i = col_idx.get("parent")
    detail_i = col_idx.get("detail")
    if id_i is None or parent_i is None or detail_i is None:
        return []
    nodes_by_id: dict[int, ExplainNode] = {}
    parents: dict[int, int] = {}
    for r in rows:
        try:
            nid = int(r[id_i])
            pid = int(r[parent_i])
        except (TypeError, ValueError):
            continue
        detail = str(r[detail_i] or "")
        node = ExplainNode(label=detail)
        _classify_text_node(node, detail, "sqlite")
        nodes_by_id[nid] = node
        parents[nid] = pid
    roots: list[ExplainNode] = []
    for nid, node in nodes_by_id.items():
        pid = parents.get(nid, 0)
        if pid == 0 or pid not in nodes_by_id:
            roots.append(node)
        else:
            nodes_by_id[pid].children.append(node)
    return roots


class ExplainView(QWidget):
    """EXPLAIN 结果的可视化视图。"""

    def __init__(self, parent=None, columns=None, rows=None, db_type="mysql"):
        super().__init__(parent)
        self._db_type = db_type
        self._columns = [str(c) for c in (columns or [])]
        self._rows: list[tuple] = [tuple(r) for r in (rows or [])]

        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setTextFormat(Qt.RichText)

        self.toggle_btn = QPushButton("查看原始数据")
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.setMinimumHeight(28)
        self.toggle_btn.toggled.connect(self._toggle_raw)

        header = QHBoxLayout()
        header.setContentsMargins(14, 10, 14, 4)
        header.setSpacing(8)
        header.addWidget(self.summary_label, 1)
        header.addWidget(self.toggle_btn)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["步骤", "详情", "提示"])
        self.tree.setColumnWidth(0, 340)
        self.tree.setColumnWidth(1, 420)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(False)
        self.tree.setIndentation(20)
        self.tree.setFrameShape(QTreeWidget.NoFrame)
        self.tree.setRootIsDecorated(True)
        self.tree.setExpandsOnDoubleClick(True)

        self.raw = QTreeWidget()
        self.raw.setHeaderLabels(self._columns or ["结果"])
        self.raw.setAlternatingRowColors(True)
        self.raw.setUniformRowHeights(True)
        self.raw.setFrameShape(QTreeWidget.NoFrame)

        self.stack = QStackedWidget()
        self.stack.addWidget(self.tree)
        self.stack.addWidget(self.raw)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(header)
        layout.addWidget(self.stack, 1)

        self._rebuild()
        theme_manager.changed.connect(self._on_theme_changed)
        self.destroyed.connect(self._on_destroyed)

    def _on_destroyed(self, *_args) -> None:
        try:
            theme_manager.changed.disconnect(self._on_theme_changed)
        except (TypeError, RuntimeError):
            pass

    # ---------- 对外 ----------
    def append_rows(self, rows: list[list]) -> None:
        if not rows:
            return
        for r in rows:
            self._rows.append(tuple(r))
        self._rebuild()

    def set_result(self, columns, rows, *_ignored) -> None:
        self._columns = [str(c) for c in (columns or [])]
        self._rows = [tuple(r) for r in (rows or [])]
        self.raw.setHeaderLabels(self._columns or ["结果"])
        self._rebuild()

    def row_count(self) -> int:
        return len(self._rows)

    # ---------- 内部 ----------
    def _rebuild(self) -> None:
        nodes = self._parse()
        self._render(nodes)
        self._render_raw()
        self._update_summary(nodes)
        self._apply_style()

    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QTreeWidget {{
                background: {p.surface};
                border: none;
                outline: 0;
                padding: 6px 8px;
                color: {p.text};
            }}
            QTreeWidget::item {{
                padding: 7px 6px;
                border-radius: 6px;
                margin: 1px 2px;
                min-height: 22px;
            }}
            QTreeWidget::item:hover {{
                background: {p.surface_hover};
            }}
            QTreeWidget::item:selected {{
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
            QLabel {{
                color: {p.text};
            }}
        """)

    def _parse(self) -> list[ExplainNode]:
        if self._db_type == "mysql":
            return _parse_mysql(self._columns, self._rows)
        if self._db_type == "postgresql":
            return _parse_pg(self._rows)
        if self._db_type == "sqlite":
            return _parse_sqlite(self._columns, self._rows)
        return []

    def _render(self, nodes: list[ExplainNode]) -> None:
        self.tree.clear()
        for n in nodes:
            self.tree.addTopLevelItem(self._make_item(n))
        self.tree.expandAll()

    def _make_item(self, node: ExplainNode) -> QTreeWidgetItem:
        item = QTreeWidgetItem([node.label, node.detail, node.hint])
        self._apply_status_color(item, node.status)
        for c in node.children:
            item.addChild(self._make_item(c))
        return item

    @staticmethod
    def _apply_status_color(item: QTreeWidgetItem, status: str) -> None:
        if status == STATUS_NONE:
            return
        p = theme_manager.palette
        color = {
            STATUS_BAD: p.danger,
            STATUS_WARN: p.warning,
            STATUS_GOOD: p.success,
        }.get(status)
        if not color:
            return
        brush = QBrush(QColor(color))
        for c in range(3):
            item.setForeground(c, brush)

    def _render_raw(self) -> None:
        self.raw.clear()
        for r in self._rows:
            self.raw.addTopLevelItem(
                QTreeWidgetItem(["" if v is None else str(v) for v in r])
            )

    def _update_summary(self, nodes: list[ExplainNode]) -> None:
        counter = {STATUS_BAD: 0, STATUS_WARN: 0, STATUS_GOOD: 0}
        hints: list[str] = []

        def walk(items):
            for n in items:
                counter[n.status] = counter.get(n.status, 0) + 1
                if n.hint:
                    hints.append(n.hint)
                walk(n.children)

        walk(nodes)
        p = theme_manager.palette

        chips = []
        if counter.get(STATUS_BAD):
            chips.append(
                f"<span style='background:{p.danger_soft};"
                f"color:{p.danger};padding:2px 10px;border-radius:9px;'>"
                f"严重 {counter[STATUS_BAD]}</span>"
            )
        if counter.get(STATUS_WARN):
            chips.append(
                f"<span style='background:{p.danger_soft};"
                f"color:{p.warning};padding:2px 10px;border-radius:9px;'>"
                f"警告 {counter[STATUS_WARN]}</span>"
            )
        if counter.get(STATUS_GOOD):
            chips.append(
                f"<span style='background:{p.accent_soft};"
                f"color:{p.success};padding:2px 10px;border-radius:9px;'>"
                f"良好 {counter[STATUS_GOOD]}</span>"
            )

        head = " ".join(chips) if chips else "未发现明显问题"

        uniq: list[str] = []
        for h in hints:
            if h not in uniq:
                uniq.append(h)
        hint_text = "  ·  ".join(uniq[:6])

        text = f"<b style='font-size:10.5pt;'>执行计划分析</b>　{head}"
        if hint_text:
            text += (
                f"<br><span style='color:{p.text_muted};"
                f"font-size:9pt;'>{hint_text}</span>"
            )
        self.summary_label.setText(text)

    def _toggle_raw(self, checked: bool) -> None:
        self.stack.setCurrentIndex(1 if checked else 0)
        self.toggle_btn.setText("查看计划" if checked else "查看原始数据")

    def _on_theme_changed(self, *_args) -> None:
        self._rebuild()