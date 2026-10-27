"""对象浏览器面板：搜索栏（支持正则）+ 状态圆点 + 彩色对象树。

设计参考 Navicat / DBeaver：
- 顶部工具条：导入/导出（下拉）、全部折叠、新建连接、刷新、全部展开
- 搜索栏：搜索框 + 正则切换（.*）+ 定位 + 选项 + 状态指示灯
- 树：连接（圆点）/ 数据库（圆点）/
      [表 视图 存储过程 函数 触发器 事件]

本模块只负责「面板 UI + 图标工厂」，具体的节点填充逻辑仍在 MainWindow：
MainWindow 通过 `panel.tree` 拿到内部 QTreeWidget，直接向其中添加/删除节点。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QToolButton,
    QTreeWidget, QVBoxLayout, QWidget,
)

from ..theme import theme_manager


# ============================================================
# 图标工厂：给每种对象类型画一个彩色矢量小图标
# ============================================================
_ICON_SIZE = 16


def make_object_icon(kind: str, size: int = _ICON_SIZE) -> QIcon:
    """按对象类型生成小图标（矢量绘制，不依赖外部资源）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)

    if kind == "table":
        p.setPen(QPen(QColor("#10b981"), 1.4))
        p.setBrush(QBrush(QColor("#d1fae5")))
        p.drawRoundedRect(2, 3, size - 4, size - 5, 2, 2)
        p.drawLine(2, 7, size - 2, 7)
        p.drawLine(2, 11, size - 2, 11)
        p.drawLine(size // 2, 3, size // 2, size - 2)

    elif kind == "view":
        p.setPen(QPen(QColor("#8b5cf6"), 1.4))
        p.setBrush(Qt.NoBrush)
        path = QPainterPath()
        path.moveTo(2, size / 2)
        path.quadTo(size / 2, 2, size - 2, size / 2)
        path.quadTo(size / 2, size - 2, 2, size / 2)
        p.drawPath(path)
        p.setBrush(QBrush(QColor("#8b5cf6")))
        p.setPen(Qt.NoPen)
        p.drawEllipse(size / 2 - 2, size / 2 - 2, 4, 4)

    elif kind == "procedure":
        p.setPen(QPen(QColor("#3b82f6"), 1.4))
        p.setBrush(QBrush(QColor("#dbeafe")))
        p.drawRoundedRect(3, 2, size - 6, size - 4, 2, 2)
        p.drawLine(6, 6, size - 6, 6)
        p.drawLine(6, 9, size - 6, 9)

    elif kind == "function":
        p.setPen(QPen(QColor("#06b6d4"), 1.8))
        p.drawLine(int(size * 0.68), 3, int(size * 0.42), 3)
        p.drawLine(int(size * 0.52), 3, int(size * 0.52), size - 2)
        p.drawLine(int(size * 0.36), int(size * 0.55),
                   int(size * 0.72), int(size * 0.55))

    elif kind == "trigger":
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor("#f59e0b")))
        path = QPainterPath()
        path.moveTo(size * 0.6, 1)
        path.lineTo(size * 0.25, size * 0.55)
        path.lineTo(size * 0.5, size * 0.55)
        path.lineTo(size * 0.4, size - 1)
        path.lineTo(size * 0.75, size * 0.45)
        path.lineTo(size * 0.5, size * 0.45)
        path.closeSubpath()
        p.drawPath(path)

    elif kind == "event":
        p.setPen(QPen(QColor("#ec4899"), 1.4))
        p.setBrush(QBrush(QColor("#fce7f3")))
        p.drawEllipse(2, 2, size - 4, size - 4)
        p.drawLine(size // 2, size // 2 - 3, size // 2, size // 2)
        p.drawLine(size // 2, size // 2, size // 2 + 3, size // 2 + 1)

    elif kind == "connection":
        p.setPen(QPen(QColor("#0891b2"), 1.4))
        p.setBrush(QBrush(QColor("#cffafe")))
        p.drawRoundedRect(3, 5, size - 6, size - 7, 2, 2)
        p.drawLine(int(size * 0.35), 5, int(size * 0.35), 2)
        p.drawLine(int(size * 0.65), 5, int(size * 0.65), 2)

    elif kind == "database":
        p.setPen(QPen(QColor("#3b82f6"), 1.4))
        p.setBrush(QBrush(QColor("#dbeafe")))
        p.drawEllipse(2, 1, size - 4, 4)
        p.drawRect(2, 3, size - 4, size - 6)
        p.drawEllipse(2, size - 5, size - 4, 4)

    elif kind == "folder":
        p.setPen(QPen(QColor("#94a3b8"), 1.2))
        p.setBrush(QBrush(QColor("#e2e8f0")))
        path = QPainterPath()
        path.moveTo(1, 4)
        path.lineTo(size * 0.4, 4)
        path.lineTo(size * 0.5, 6)
        path.lineTo(size - 1, 6)
        path.lineTo(size - 1, size - 2)
        path.lineTo(1, size - 2)
        path.closeSubpath()
        p.drawPath(path)

    elif kind == "column":
        p.setPen(QPen(QColor("#94a3b8"), 1.5))
        p.drawLine(4, size // 2, size - 3, size // 2)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor("#cbd5e1")))
        p.drawEllipse(1, size // 2 - 2, 4, 4)

    elif kind == "query":
        p.setPen(QPen(QColor("#2563eb"), 1.4))
        p.setBrush(QBrush(QColor("#dbeafe")))
        p.drawRoundedRect(2, 2, size - 7, size - 4, 1, 1)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(size - 7, size - 7, 6, 6)
        p.drawLine(size - 2, size - 2, size, size)

    p.end()
    return QIcon(pm)


def make_dot_icon(color: str, size: int = 10) -> QIcon:
    """实心圆点（状态指示灯）。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QBrush(QColor(color)))
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


# 状态圆点颜色（供 MainWindow 使用）
DOT_ONLINE = "#10b981"    # 绿
DOT_OFFLINE = "#94a3b8"   # 灰
DOT_WARN = "#f59e0b"      # 橙


# ============================================================
# 面板
# ============================================================
class ObjectBrowserPanel(QWidget):
    """对象浏览器面板。内部 QTreeWidget 通过 `panel.tree` 暴露。"""

    importRequested = Signal()      # 点击「导入数据」
    exportRequested = Signal()      # 点击「导出数据」
    refreshRequested = Signal()     # 点击「刷新」
    newConnectionRequested = Signal()
    filterChanged = Signal(str)     # 搜索文本或正则开关变化

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(240)
        self._build_ui()
        self._apply_style()
        theme_manager.changed.connect(self._on_theme_changed)
        self.destroyed.connect(self._on_destroyed)

    # ---------- 构建 ----------
    def _build_ui(self) -> None:
        # ---- 顶部工具条 ----
        self.toolbar = QWidget()
        tl = QHBoxLayout(self.toolbar)
        tl.setContentsMargins(6, 4, 6, 2)
        tl.setSpacing(2)

        self.import_btn = QToolButton()
        self.import_btn.setText("↑↓  导入/导出")
        self.import_btn.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.import_btn.setPopupMode(QToolButton.InstantPopup)
        self.import_btn.setCursor(Qt.PointingHandCursor)
        self.import_btn.setToolTip("导入/导出数据")
        import_menu = QMenu(self.import_btn)
        import_menu.addAction("从文件导入数据…", self.importRequested.emit)
        import_menu.addAction("导出查询结果…", self.exportRequested.emit)
        self.import_btn.setMenu(import_menu)

        self.collapse_all_btn = self._make_tool_btn("«", "全部折叠")
        self.new_btn = self._make_tool_btn("＋", "新建连接")
        self.refresh_btn = self._make_tool_btn("↻", "刷新")
        self.close_btn = self._make_tool_btn("✕", "关闭面板")

        tl.addWidget(self.import_btn)
        tl.addStretch(1)
        tl.addWidget(self.collapse_all_btn)
        tl.addWidget(self.new_btn)
        tl.addWidget(self.refresh_btn)
        tl.addWidget(self.close_btn)

        # ---- 搜索栏 ----
        self.search_bar = QWidget()
        sl = QHBoxLayout(self.search_bar)
        sl.setContentsMargins(6, 2, 6, 4)
        sl.setSpacing(4)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索…")
        self.search_edit.setClearButtonEnabled(True)

        self.regex_btn = QToolButton()
        self.regex_btn.setText(".*")
        self.regex_btn.setCheckable(True)
        self.regex_btn.setToolTip("正则表达式模式")
        self.regex_btn.setCursor(Qt.PointingHandCursor)
        self.regex_btn.setFixedSize(28, 26)

        self.locate_btn = self._make_tool_btn("◎", "定位到当前项")
        self.filter_btn = self._make_tool_btn("☰", "过滤选项")

        self.status_dot = QLabel()
        self.status_dot.setPixmap(make_dot_icon(DOT_ONLINE).pixmap(8, 8))
        self.status_dot.setFixedWidth(14)
        self.status_dot.setToolTip("连接状态")

        sl.addWidget(self.search_edit, 1)
        sl.addWidget(self.regex_btn)
        sl.addWidget(self.locate_btn)
        sl.addWidget(self.filter_btn)
        sl.addWidget(self.status_dot)

        # ---- 树 ----
        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["对象", ""])
        self.tree.setAnimated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIndentation(14)
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.tree.setEditTriggers(QTreeWidget.NoEditTriggers)

        header = self.tree.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionsMovable(False)
        self.tree.setColumnWidth(1, 18)
        header.setHidden(True)

        # ---- 总布局 ----
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.search_bar)
        layout.addWidget(self.tree, 1)

        # ---- 信号 ----
        self.new_btn.clicked.connect(self.newConnectionRequested.emit)
        self.refresh_btn.clicked.connect(self.refreshRequested.emit)
        self.collapse_all_btn.clicked.connect(self.tree.collapseAll)
        self.close_btn.clicked.connect(self._on_close_clicked)
        self.search_edit.textChanged.connect(self._on_search_changed)
        self.regex_btn.toggled.connect(self._on_regex_toggled)
        self.locate_btn.clicked.connect(self._locate_current)
        self.filter_btn.clicked.connect(self._show_filter_menu)

    def _make_tool_btn(self, text: str, tooltip: str) -> QToolButton:
        btn = QToolButton()
        btn.setText(text)
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(26, 26)
        return btn

    # ---------- 事件 ----------
    def _on_search_changed(self, text: str) -> None:
        self.filterChanged.emit(text)

    def _on_regex_toggled(self, _checked: bool) -> None:
        self.filterChanged.emit(self.search_edit.text())

    def _on_close_clicked(self) -> None:
        self.setVisible(False)

    def _locate_current(self) -> None:
        item = self.tree.currentItem()
        if item is not None:
            self.tree.scrollToItem(item)

    def _show_filter_menu(self) -> None:
        menu = QMenu(self)
        menu.addAction("全部展开", self.tree.expandAll)
        menu.addAction("全部折叠", self.tree.collapseAll)
        menu.addSeparator()
        menu.addAction("清空搜索", lambda: self.search_edit.clear())
        menu.exec(self.filter_btn.mapToGlobal(
            self.filter_btn.rect().bottomLeft()))

    # ---------- 对外 ----------
    def regex_enabled(self) -> bool:
        return self.regex_btn.isChecked()

    # ---------- 主题 ----------
    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QWidget {{
                background: {p.surface};
            }}
            QToolButton {{
                border: 1px solid transparent;
                border-radius: 5px;
                padding: 2px 4px;
                color: {p.text_muted};
                background: transparent;
            }}
            QToolButton:hover {{
                background: {p.surface_hover};
                color: {p.accent};
                border-color: {p.border};
            }}
            QToolButton:checked {{
                background: {p.accent_soft};
                color: {p.accent_text};
                border-color: {p.accent};
            }}
            QToolButton::menu-indicator {{
                image: none;
                width: 0;
            }}
            QLineEdit {{
                background: {p.surface_sunken};
                border: 1px solid {p.border};
                border-radius: 6px;
                padding: 4px 8px;
                color: {p.text};
            }}
            QLineEdit:focus {{
                border-color: {p.accent};
                background: {p.surface};
            }}
            QTreeWidget {{
                border: none;
                background: {p.surface};
                outline: 0;
                padding: 2px 0;
                color: {p.text};
            }}
            QTreeWidget::item {{
                padding: 5px 4px;
                border-radius: 4px;
                margin: 1px 3px;
            }}
            QTreeWidget::item:hover {{
                background: {p.surface_hover};
            }}
            QTreeWidget::item:selected {{
                background: {p.accent_soft};
                color: {p.accent_text};
            }}
            QTreeWidget::branch {{
                background: transparent;
            }}
        """)

    def _on_theme_changed(self, _palette) -> None:
        self._apply_style()

    def _on_destroyed(self, *_args) -> None:
        try:
            theme_manager.changed.disconnect(self._on_theme_changed)
        except (TypeError, RuntimeError):
            pass