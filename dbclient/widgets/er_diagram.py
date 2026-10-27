"""ER 图：把库内表及外键关系可视化成可拖动的图形。

- 每张表一个节点，显示表名 + 前 N 列
- 外键用曲线连接
- 支持拖动、Ctrl+滚轮缩放、过滤、双击打开表

界面 v2（2024 美化重构）：
- 节点卡片更精致：圆角 10、表头独立层次、列名左右分层
- 边用细腻曲线 + 圆头虚线
- 视图无边框，背景随主题切换
- 工具栏图标化、间距舒展
- 主题切换自动重绘
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication, QComboBox, QGraphicsItem, QGraphicsPathItem,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from ..theme import theme_manager

log = logging.getLogger(__name__)

_NODE_W = 240
_HEADER_H = 32
_ROW_H = 20
_MAX_VISIBLE_COLS = 18


class ErNode(QGraphicsRectItem):
    """一张表的图形节点。"""

    def __init__(self, table: str, columns: list[dict],
                 schema: str | None = None, db_type: str = "mysql"):
        super().__init__(0, 0, _NODE_W, 0)
        self.table = table
        self.schema = schema
        self.db_type = db_type
        self.columns = columns[:_MAX_VISIBLE_COLS]
        self.more_cols = max(0, len(columns) - _MAX_VISIBLE_COLS)
        self._edges: list["ErEdge"] = []

        rows = len(self.columns) + (1 if self.more_cols else 0)
        h = _HEADER_H + _ROW_H * rows + 12
        self.setRect(0, 0, _NODE_W, h)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setZValue(10)

    def add_edge(self, edge: "ErEdge") -> None:
        self._edges.append(edge)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for e in self._edges:
                e.update_path()
        return super().itemChange(change, value)

    def paint(self, painter: QPainter, option, widget=None) -> None:
        p = theme_manager.palette
        rect = self.rect()
        painter.setRenderHint(QPainter.Antialiasing, True)

        # ---------- 卡片主体 ----------
        painter.setBrush(QBrush(QColor(p.surface)))
        painter.setPen(QPen(QColor(p.border_strong), 1.2))
        painter.drawRoundedRect(rect, 10, 10)

        # ---------- 表头 ----------
        head_rect = QRectF(rect.left(), rect.top(), rect.width(), _HEADER_H)
        painter.setBrush(QBrush(QColor(p.surface_alt)))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(head_rect, 10, 10)
        # 补齐底部直角（避免下面部分被画成圆角）
        painter.drawRect(QRectF(
            head_rect.left(), head_rect.bottom() - 10,
            head_rect.width(), 10,
        ))

        # ---------- 表名 ----------
        painter.setPen(QPen(QColor(p.text)))
        f = QFont()
        f.setBold(True)
        painter.setFont(f)
        title = self.table
        if self.schema:
            title = f"{self.schema}.{self.table}"
        painter.drawText(
            head_rect.adjusted(14, 0, -14, 0),
            Qt.AlignVCenter | Qt.AlignLeft, title,
        )

        # ---------- 列 ----------
        painter.setFont(QFont())
        y = head_rect.bottom() + 6
        for col in self.columns:
            name = str(col.get("name", "?"))
            ctype = str(col.get("type", ""))
            line = QRectF(rect.left() + 14, y, rect.width() - 28, _ROW_H)

            painter.setPen(QPen(QColor(p.text)))
            painter.drawText(
                line.adjusted(0, 0, -line.width() // 2, 0),
                Qt.AlignVCenter | Qt.AlignLeft, name,
            )
            painter.setPen(QPen(QColor(p.text_disabled)))
            painter.drawText(
                line.adjusted(line.width() // 2, 0, 0, 0),
                Qt.AlignVCenter | Qt.AlignRight, ctype[:22],
            )
            y += _ROW_H

        if self.more_cols:
            painter.setPen(QPen(QColor(p.text_disabled)))
            painter.drawText(
                QRectF(rect.left() + 14, y, rect.width() - 28, _ROW_H),
                Qt.AlignVCenter | Qt.AlignLeft,
                f"… 另有 {self.more_cols} 列",
            )

        # ---------- 选中态强调边框 ----------
        if self.isSelected():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(p.accent), 2))
            painter.drawRoundedRect(rect.adjusted(-2, -2, 2, 2), 12, 12)


class ErEdge(QGraphicsPathItem):
    """两张表之间的外键连线。"""

    def __init__(self, src: ErNode, dst: ErNode):
        super().__init__()
        self.src = src
        self.dst = dst
        self.setZValue(0)
        c = QColor(theme_manager.palette.border_strong)
        c.setAlpha(200)
        self.setPen(QPen(c, 1.3, Qt.DashLine, Qt.RoundCap, Qt.RoundJoin))
        src.add_edge(self)
        dst.add_edge(self)
        self.update_path()

    def update_path(self) -> None:
        rs = self.src.sceneBoundingRect()
        rd = self.dst.sceneBoundingRect()

        # 判断左右关系，选择最自然的连接点
        if rs.center().x() < rd.center().x():
            p1 = QPointF(rs.right(), rs.center().y())
            p2 = QPointF(rd.left(), rd.center().y())
        else:
            p1 = QPointF(rs.left(), rs.center().y())
            p2 = QPointF(rd.right(), rd.center().y())

        path = QPainterPath(p1)
        mid_x = (p1.x() + p2.x()) / 2
        path.cubicTo(
            QPointF(mid_x, p1.y()),
            QPointF(mid_x, p2.y()),
            p2,
        )
        self.setPath(path)


class ErView(QGraphicsView):
    """支持滚轮缩放、拖动平移的视图。"""

    nodeDoubleClicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorUnderMouse)
        self.setFrameShape(QGraphicsView.NoFrame)

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.modifiers() & Qt.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        item = self.itemAt(event.position().toPoint())
        if isinstance(item, ErNode):
            self.nodeDoubleClicked.emit(item.table)
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class ErDiagramWidget(QWidget):
    """ER 图面板。由主窗口创建，传入 client + 库名。"""

    def __init__(self, client, schema: str | None = None, parent=None):
        super().__init__(parent)
        self.client = client
        self.schema = schema

        # ---------- 场景 / 视图 ----------
        self.scene = QGraphicsScene(self)
        self.scene.setBackgroundBrush(
            QBrush(QColor(theme_manager.palette.surface))
        )

        self.view = ErView(self)
        self.view.setScene(self.scene)
        self.view.nodeDoubleClicked.connect(self._on_node_dblclick)

        # ---------- 顶部工具条 ----------
        filter_label = QLabel("过滤")
        filter_label.setProperty("role", "hint")

        self.filter_combo = QComboBox()
        self.filter_combo.setEditable(True)
        self.filter_combo.addItem("（全部表）")
        self.filter_combo.setMinimumWidth(240)
        self.filter_combo.setMinimumHeight(30)
        self.filter_combo.currentTextChanged.connect(self._apply_filter)

        self.status_label = QLabel("正在加载…")
        self.status_label.setProperty("role", "hint")

        self.refresh_btn = self._make_tool_btn("↻", "重新布局")
        self.refresh_btn.clicked.connect(self._relayout)

        self.reload_btn = self._make_tool_btn("⟳", "重新加载元数据")
        self.reload_btn.clicked.connect(self.load)

        bar = QHBoxLayout()
        bar.setContentsMargins(14, 10, 14, 8)
        bar.setSpacing(8)
        bar.addWidget(filter_label)
        bar.addWidget(self.filter_combo, 1)
        bar.addStretch(1)
        bar.addWidget(self.status_label)
        bar.addWidget(self.refresh_btn)
        bar.addWidget(self.reload_btn)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(bar)
        layout.addWidget(self.view, 1)

        self._nodes: dict[str, ErNode] = {}
        self._edges: list[ErEdge] = []
        self._loaded = False

        theme_manager.changed.connect(self._on_theme_changed)
        self.destroyed.connect(self._on_destroyed)

        self._apply_style()

    # ---------- 小工具 ----------
    def _make_tool_btn(self, text: str, tooltip: str) -> QToolButton:
        btn = QToolButton()
        btn.setText(text)
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(30, 30)
        return btn

    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QWidget {{
                background: {p.bg};
            }}
            QComboBox {{
                background: {p.surface};
                border: 1px solid {p.border_strong};
                border-radius: 8px;
                padding: 4px 10px;
                color: {p.text};
            }}
            QComboBox:hover {{
                border-color: {p.accent};
            }}
            QComboBox:focus {{
                border-color: {p.accent};
            }}
            QToolButton {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 7px;
                color: {p.text_muted};
                font-size: 13pt;
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
        """)

    # ---------- 主题 ----------
    def _on_theme_changed(self, _palette) -> None:
        self._apply_style()
        self.scene.setBackgroundBrush(
            QBrush(QColor(theme_manager.palette.surface))
        )
        for node in self._nodes.values():
            node.update()
        for edge in self._edges:
            edge.update()
        self.scene.update()

    def _on_destroyed(self, *_args) -> None:
        try:
            theme_manager.changed.disconnect(self._on_theme_changed)
        except (TypeError, RuntimeError):
            pass

    # ---------- 加载 ----------
    def load(self) -> None:
        self.scene.clear()
        self._nodes.clear()
        self._edges.clear()
        self.status_label.setText("正在加载元数据…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self._build()
        except Exception as exc:  # noqa: BLE001
            log.exception("加载 ER 图失败")
            self.status_label.setText(f"加载失败：{exc}")
            QMessageBox.critical(self, "加载 ER 图失败", str(exc))
        finally:
            QApplication.restoreOverrideCursor()

    def _build(self) -> None:
        entries = self.client.tables(schema=self.schema)
        tables = [t for t, kind in entries if kind == "table"]
        if not tables:
            self.status_label.setText("该库没有表")
            return

        col_map = self.client.multi_columns(tables, schema=self.schema)
        for t in tables:
            node = ErNode(
                table=t, columns=col_map.get(t, []),
                schema=self.schema, db_type=self.client.cfg.db_type,
            )
            self.scene.addItem(node)
            self._nodes[t] = node

        for t in tables:
            try:
                fks = self.client.foreign_keys(t, schema=self.schema)
            except Exception:  # noqa: BLE001
                fks = []
            for fk in fks:
                ref = fk.get("referred_table") or ""
                if ref in self._nodes:
                    edge = ErEdge(self._nodes[t], self._nodes[ref])
                    self.scene.addItem(edge)
                    self._edges.append(edge)

        self._relayout()
        self.status_label.setText(
            f"{len(self._nodes)} 张表 · {len(self._edges)} 条外键"
        )

        self.filter_combo.blockSignals(True)
        current = self.filter_combo.currentText()
        self.filter_combo.clear()
        self.filter_combo.addItem("（全部表）")
        for t in sorted(self._nodes.keys()):
            self.filter_combo.addItem(t)
        self.filter_combo.setCurrentText(current or "（全部表）")
        self.filter_combo.blockSignals(False)
        self._loaded = True

    def _relayout(self) -> None:
        if not self._nodes:
            return
        referenced = {e.dst.table for e in self._edges}
        roots = sorted(t for t in self._nodes if t not in referenced)
        others = sorted(t for t in self._nodes if t in referenced)
        ordered = roots + others

        cols = min(4, max(1, int(len(ordered) ** 0.5 + 0.5)))
        x_gap = _NODE_W + 90
        y_gap = 50
        y_offsets = [0] * cols
        for idx, name in enumerate(ordered):
            col = idx % cols
            node = self._nodes[name]
            h = node.rect().height()
            node.setPos(col * x_gap, y_offsets[col])
            y_offsets[col] += h + y_gap

        for e in self._edges:
            e.update_path()

        self.scene.setSceneRect(
            self.scene.itemsBoundingRect().adjusted(-60, -60, 60, 60)
        )

    def _apply_filter(self, text: str) -> None:
        if not self._loaded:
            return
        text = (text or "").strip()
        for name, node in self._nodes.items():
            if not text or text == "（全部表）":
                node.setVisible(True)
            else:
                node.setVisible(text.lower() in name.lower())
        for e in self._edges:
            e.setVisible(e.src.isVisible() and e.dst.isVisible())

    def _on_node_dblclick(self, table: str) -> None:
        w = self.window()
        opener = getattr(w, "_open_table", None)
        if callable(opener):
            opener(
                ("table", self.client.cfg.name, self.schema, table, "table"),
                reuse=True,
            )