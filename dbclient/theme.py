"""主题系统：浅色 / 深色主题调色板与样式表生成。

设计语言（v2）：克制、有呼吸感、明确的层级。
- 背景 4 级：bg / surface / surface_alt / surface_sunken
- 交互 4 态：hover / active / focus / disabled，都有视觉反馈
- 圆角统一：小 6 / 中 8 / 大 10 / 胶囊 999
- 间距节奏：4 的倍数，控件内边距 6/8/10/12
- 全部图标用纯色 + 边框，不用 SVG（避免部分 Qt 版本崩溃）

用法：
    from .theme import theme_manager, Theme
    theme_manager.apply(app, Theme.DARK)
    palette = theme_manager.palette

任何需要跟随主题重绘的自定义控件（如 SQL 编辑器）可连接：
    theme_manager.changed.connect(on_theme_changed)
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QObject, Signal


class Theme(str, Enum):
    LIGHT = "light"
    DARK = "dark"


@dataclass(frozen=True)
class Palette:
    """一次主题所需的所有颜色（十六进制字符串）。"""

    name: str
    is_dark: bool

    # 基础层级
    bg: str               # 窗口底色
    surface: str          # 卡片 / 内容表面
    surface_alt: str      # 次级表面（表头 / dock 标题）
    surface_sunken: str   # 凹陷表面（输入框暗底 / 轨道）
    surface_hover: str    # 悬停态
    surface_active: str   # 选中态
    border: str           # 常规分隔线
    border_strong: str    # 强调边框（输入框 / 可交互元素）

    # 文本
    text: str
    text_muted: str
    text_disabled: str

    # 强调 / 状态
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str      # 强调色淡背景
    accent_text: str      # 强调色背景上的文字
    danger: str
    danger_soft: str
    success: str
    warning: str

    # 编辑器
    editor_bg: str
    editor_gutter: str
    editor_gutter_text: str
    editor_line: str
    editor_selection: str
    editor_cursor: str

    # 语法高亮
    syn_kw: str
    syn_num: str
    syn_str: str
    syn_ident: str
    syn_cmt: str


# =========================================================
# 浅色 —— 冷灰蓝底 + 克制蓝强调（Tailwind slate + blue）
# 对比度经过 WCAG AA 校验：正文 >= 7:1，次要 >= 4.5:1
# =========================================================
LIGHT = Palette(
    name="Light",
    is_dark=False,

    bg="#f3f5f9",
    surface="#ffffff",
    surface_alt="#eef1f6",
    surface_sunken="#e6ebf2",
    surface_hover="#eef4fd",
    surface_active="#dbeafe",
    border="#e3e8ef",
    border_strong="#c7d0dd",

    text="#0f172a",
    text_muted="#54637a",
    text_disabled="#a4aebe",

    accent="#2563eb",
    accent_hover="#1d4ed8",
    accent_pressed="#1e40af",
    accent_soft="#dbeafe",
    accent_text="#1e40af",
    danger="#dc2626",
    danger_soft="#fee2e2",
    success="#059669",
    warning="#d97706",

    editor_bg="#ffffff",
    editor_gutter="#f3f5f9",
    editor_gutter_text="#a4aebe",
    editor_line="#f0f6ff",
    editor_selection="#cfe3ff",
    editor_cursor="#2563eb",

    syn_kw="#2563eb",
    syn_num="#c2410c",
    syn_str="#15803d",
    syn_ident="#7c3aed",
    syn_cmt="#94a3b8",
)


# =========================================================
# 深色 —— GitHub Dark 风格深海蓝灰
# =========================================================
DARK = Palette(
    name="Dark",
    is_dark=True,

    bg="#0b0f16",
    surface="#141a22",
    surface_alt="#1b222c",
    surface_sunken="#090d13",
    surface_hover="#232c38",
    surface_active="#2c3744",
    border="#222a36",
    border_strong="#3a4453",

    text="#e8eef7",
    text_muted="#8e99a9",
    text_disabled="#5a6473",

    accent="#58a6ff",
    accent_hover="#79b8ff",
    accent_pressed="#a0cfff",
    accent_soft="#1a2c44",
    accent_text="#c9e2ff",
    danger="#f87171",
    danger_soft="#3a1d1d",
    success="#4ade80",
    warning="#fbbf24",

    editor_bg="#0b0f16",
    editor_gutter="#0e131a",
    editor_gutter_text="#5a6473",
    editor_line="#141b25",
    editor_selection="#1e3a5f",
    editor_cursor="#58a6ff",

    syn_kw="#7dd3fc",
    syn_num="#fbbf24",
    syn_str="#86efac",
    syn_ident="#c4b5fd",
    syn_cmt="#6b7280",
)


# =========================================================
# 样式表
# =========================================================
def build_stylesheet(p: Palette) -> str:
    """根据调色板生成全局 QSS。

    注意：不要在这里使用 `image: url("data:image/svg+xml;utf8,...")`，
    某些 PySide6 版本（尤其 Python 3.13 + Qt 6.7+）在解析该语法时会触发
    原生访问冲突（0xC0000005）。复选框勾选状态改用纯色 + 边框表示，
    依赖 Qt 默认的 checkbox 渲染逻辑。
    """
    font_stack = (
        '"Microsoft YaHei UI", "Segoe UI", "PingFang SC", '
        '"Helvetica Neue", "Noto Sans CJK SC", sans-serif'
    )
    mono_stack = (
        '"JetBrains Mono", "Cascadia Mono", "Consolas", '
        '"Menlo", monospace'
    )

    return f"""
/* ============================================================
   基础
   ============================================================ */
QWidget {{
    font-family: {font_stack};
    font-size: 9.5pt;
    color: {p.text};
}}
QMainWindow, QDialog {{
    background: {p.bg};
}}
QToolTip {{
    background: {p.surface};
    color: {p.text};
    border: 1px solid {p.border_strong};
    padding: 6px 10px;
    border-radius: 6px;
}}
QFrame[role="sep"] {{
    border: none;
    border-top: 1px solid {p.border};
    max-height: 1px;
}}

/* ============================================================
   菜单栏 / 菜单 / 工具栏
   ============================================================ */
QMenuBar {{
    background: {p.surface};
    border-bottom: 1px solid {p.border};
    padding: 2px 8px;
    spacing: 2px;
}}
QMenuBar::item {{
    padding: 6px 12px;
    background: transparent;
    border-radius: 6px;
}}
QMenuBar::item:selected {{
    background: {p.surface_hover};
    color: {p.accent};
}}
QMenuBar::item:pressed {{
    background: {p.accent_soft};
    color: {p.accent_text};
}}

QMenu {{
    background: {p.surface};
    border: 1px solid {p.border_strong};
    border-radius: 8px;
    padding: 6px;
}}
QMenu::item {{
    padding: 7px 28px 7px 20px;
    border-radius: 6px;
    margin: 1px 2px;
    min-width: 140px;
}}
QMenu::item:selected {{
    background: {p.accent_soft};
    color: {p.accent_text};
}}
QMenu::item:disabled {{
    color: {p.text_disabled};
}}
QMenu::separator {{
    height: 1px;
    background: {p.border};
    margin: 5px 10px;
}}
QMenu::icon {{
    padding-left: 6px;
}}

QToolBar {{
    background: {p.surface};
    border: none;
    border-bottom: 1px solid {p.border};
    padding: 6px 10px;
    spacing: 4px;
}}
QToolBar::separator {{
    width: 1px;
    background: {p.border};
    margin: 6px 8px;
}}
QToolBar QToolButton {{
    padding: 6px 12px;
    border-radius: 7px;
    background: transparent;
    color: {p.text};
    border: 1px solid transparent;
    font-weight: 500;
}}
QToolBar QToolButton:hover {{
    background: {p.surface_hover};
    color: {p.accent};
    border-color: {p.border};
}}
QToolBar QToolButton:pressed {{
    background: {p.surface_active};
}}
QToolBar QToolButton:checked {{
    background: {p.accent_soft};
    color: {p.accent_text};
    border-color: {p.accent};
}}
QToolBar QToolButton:disabled {{
    color: {p.text_disabled};
}}

/* ============================================================
   状态栏
   ============================================================ */
QStatusBar {{
    background: {p.surface};
    border-top: 1px solid {p.border};
    color: {p.text_muted};
    padding: 4px 12px;
}}
QStatusBar::item {{ border: none; }}

/* ============================================================
   Dock 面板
   ============================================================ */
QDockWidget {{
    color: {p.text};
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
}}
QDockWidget::title {{
    background: {p.surface_alt};
    border-bottom: 1px solid {p.border};
    padding: 9px 14px;
    text-align: left;
    font-weight: 600;
    color: {p.text_muted};
    letter-spacing: 0.3px;
}}
QDockWidget::close-button, QDockWidget::float-button {{
    background: transparent;
    border: none;
    padding: 3px;
    border-radius: 5px;
}}
QDockWidget::close-button:hover, QDockWidget::float-button:hover {{
    background: {p.surface_hover};
}}

/* ============================================================
   树 / 表格 / 列表
   ============================================================ */
QTreeWidget, QTreeView, QTableView, QListView {{
    background: {p.surface};
    alternate-background-color: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 8px;
    outline: 0;
    selection-background-color: {p.accent_soft};
    selection-color: {p.accent_text};
    gridline-color: {p.border};
    padding: 2px;
}}
QTreeWidget::item, QTreeView::item {{
    padding: 5px 6px;
    border-radius: 5px;
    margin: 1px 2px;
    min-height: 20px;
}}
QTreeWidget::item:hover, QTreeView::item:hover {{
    background: {p.surface_hover};
}}
QTreeWidget::item:selected, QTreeView::item:selected {{
    background: {p.accent_soft};
    color: {p.accent_text};
}}
QTreeWidget::branch {{
    background: transparent;
}}
QTableView::item {{
    padding: 3px 8px;
    border: none;
}}
QTableView::item:hover {{
    background: {p.surface_hover};
}}
QTableView::item:selected {{
    background: {p.accent_soft};
    color: {p.accent_text};
}}

QHeaderView {{
    background: transparent;
    border: none;
}}
QHeaderView::section {{
    background: {p.surface_alt};
    padding: 8px 12px;
    border: none;
    border-right: 1px solid {p.border};
    border-bottom: 1px solid {p.border};
    font-weight: 600;
    color: {p.text_muted};
    letter-spacing: 0.2px;
}}
QHeaderView::section:last {{
    border-right: none;
}}
QHeaderView::section:hover {{
    background: {p.surface_hover};
    color: {p.accent};
}}

QTableCornerButton::section {{
    background: {p.surface_alt};
    border: none;
    border-right: 1px solid {p.border};
    border-bottom: 1px solid {p.border};
}}

/* ============================================================
   Tab
   ============================================================ */
QTabWidget::pane {{
    border: 1px solid {p.border};
    border-radius: 10px;
    background: {p.surface};
    top: -1px;
    padding: 6px;
}}
QTabBar {{
    qproperty-drawBase: 0;
    background: transparent;
}}
QTabBar::tab {{
    background: transparent;
    color: {p.text_muted};
    padding: 8px 18px;
    margin-right: 2px;
    margin-bottom: 0;
    border: 1px solid transparent;
    border-top-left-radius: 9px;
    border-top-right-radius: 9px;
    min-width: 60px;
    font-weight: 500;
}}
QTabBar::tab:hover {{
    background: {p.surface_hover};
    color: {p.text};
}}
QTabBar::tab:selected {{
    background: {p.surface};
    color: {p.accent};
    border-color: {p.border};
    border-bottom-color: {p.surface};
    font-weight: 600;
}}
QTabBar::close-button {{
    subcontrol-position: right;
    border-radius: 4px;
    padding: 2px;
    background: transparent;
}}
QTabBar::close-button:hover {{
    background: {p.danger_soft};
}}

/* ============================================================
   按钮
   ============================================================ */
QPushButton {{
    padding: 7px 16px;
    border-radius: 7px;
    background: {p.surface};
    border: 1px solid {p.border_strong};
    color: {p.text};
    min-height: 18px;
    font-weight: 500;
}}
QPushButton:hover {{
    background: {p.surface_hover};
    border-color: {p.accent};
    color: {p.accent};
}}
QPushButton:pressed {{
    background: {p.surface_active};
    border-color: {p.accent_pressed};
}}
QPushButton:disabled {{
    color: {p.text_disabled};
    background: {p.surface_alt};
    border-color: {p.border};
}}
QPushButton:default {{
    background: {p.accent};
    color: #ffffff;
    border-color: {p.accent};
}}
QPushButton:default:hover {{
    background: {p.accent_hover};
    border-color: {p.accent_hover};
    color: #ffffff;
}}
QPushButton:default:pressed {{
    background: {p.accent_pressed};
    border-color: {p.accent_pressed};
}}
QPushButton[flat="true"] {{
    background: transparent;
    border: 1px solid transparent;
}}
QPushButton[flat="true"]:hover {{
    background: {p.surface_hover};
    border-color: {p.border};
}}

QToolButton {{
    padding: 5px 9px;
    border-radius: 6px;
    background: transparent;
    border: 1px solid transparent;
    color: {p.text};
}}
QToolButton:hover {{
    background: {p.surface_hover};
    color: {p.accent};
}}
QToolButton:pressed {{
    background: {p.surface_active};
}}

/* ============================================================
   输入控件
   ============================================================ */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {p.surface};
    border: 1px solid {p.border_strong};
    border-radius: 7px;
    padding: 6px 10px;
    selection-background-color: {p.editor_selection};
    selection-color: {p.text};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover,
QPlainTextEdit:hover, QTextEdit:hover {{
    border-color: {p.accent};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {p.accent};
    background: {p.surface};
}}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
QComboBox:disabled, QPlainTextEdit:disabled, QTextEdit:disabled {{
    background: {p.surface_alt};
    color: {p.text_disabled};
    border-color: {p.border};
}}
QLineEdit[error="true"], QComboBox[error="true"], QSpinBox[error="true"] {{
    border-color: {p.danger};
}}
QLineEdit[error="true"]:focus, QComboBox[error="true"]:focus {{
    border-color: {p.danger};
}}

QComboBox::drop-down {{
    border: none;
    width: 24px;
    subcontrol-origin: padding;
    subcontrol-position: center right;
}}
QComboBox::down-arrow {{
    width: 8px;
    height: 8px;
    margin-right: 8px;
}}
QComboBox QAbstractItemView {{
    background: {p.surface};
    border: 1px solid {p.border_strong};
    border-radius: 8px;
    padding: 5px;
    selection-background-color: {p.accent_soft};
    selection-color: {p.accent_text};
    outline: 0;
}}

QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    width: 20px;
    border: none;
    background: transparent;
    border-radius: 5px;
    margin: 1px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover,
QDoubleSpinBox::up-button:hover, QDoubleSpinBox::down-button:hover {{
    background: {p.surface_hover};
}}

/* ============================================================
   复选框 —— 只用纯色 + 边框表示状态（不引入 SVG，避免 Qt 崩溃）
   ============================================================ */
QCheckBox {{
    spacing: 7px;
    padding: 2px;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1.5px solid {p.border_strong};
    border-radius: 5px;
    background: {p.surface};
}}
QCheckBox::indicator:hover {{
    border-color: {p.accent};
}}
QCheckBox::indicator:checked {{
    background: {p.accent};
    border-color: {p.accent};
}}
QCheckBox::indicator:checked:hover {{
    background: {p.accent_hover};
    border-color: {p.accent_hover};
}}
QCheckBox::indicator:checked:disabled {{
    background: {p.text_disabled};
    border-color: {p.text_disabled};
}}
QCheckBox::indicator:disabled {{
    background: {p.surface_alt};
    border-color: {p.border};
}}

/* ============================================================
   滚动条 —— 更克制、无箭头
   ============================================================ */
QScrollBar:vertical {{
    background: transparent;
    width: 11px;
    margin: 2px 2px 2px 0;
    border: none;
}}
QScrollBar::handle:vertical {{
    background: {p.border_strong};
    min-height: 32px;
    border-radius: 5px;
    margin: 1px;
}}
QScrollBar::handle:vertical:hover {{
    background: {p.accent};
}}
QScrollBar::handle:vertical:pressed {{
    background: {p.accent_pressed};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QScrollBar:horizontal {{
    background: transparent;
    height: 11px;
    margin: 0 2px 2px 2px;
    border: none;
}}
QScrollBar::handle:horizontal {{
    background: {p.border_strong};
    min-width: 32px;
    border-radius: 5px;
    margin: 1px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {p.accent};
}}
QScrollBar::handle:horizontal:pressed {{
    background: {p.accent_pressed};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: none; }}

/* ============================================================
   Splitter
   ============================================================ */
QSplitter::handle {{
    background: {p.border};
}}
QSplitter::handle:horizontal {{ width: 2px; }}
QSplitter::handle:vertical {{ height: 2px; }}
QSplitter::handle:hover {{
    background: {p.accent};
}}

/* ============================================================
   分组框 / 进度条
   ============================================================ */
QGroupBox {{
    border: 1px solid {p.border};
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 10px;
    background: {p.surface};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 6px;
    color: {p.text_muted};
    font-weight: 600;
    background: {p.surface};
}}

QProgressBar {{
    border: 1px solid {p.border};
    border-radius: 6px;
    text-align: center;
    background: {p.surface_sunken};
    color: {p.text};
    height: 8px;
}}
QProgressBar::chunk {{
    background: {p.accent};
    border-radius: 5px;
}}

/* ============================================================
   标签角色
   ============================================================ */
QLabel[role="hint"] {{ color: {p.text_muted}; }}
QLabel[role="error"] {{ color: {p.danger}; }}
QLabel[role="title"] {{ font-weight: 600; }}
QLabel[role="mono"] {{
    font-family: {mono_stack};
    font-size: 9pt;
}}
"""


# =========================================================
# 管理器
# =========================================================
class ThemeManager(QObject):
    """全局主题管理器（单例）。"""

    changed = Signal(object)  # Palette

    def __init__(self) -> None:
        super().__init__()
        self._theme = Theme.LIGHT
        self._palette = LIGHT

    @property
    def theme(self) -> Theme:
        return self._theme

    @property
    def palette(self) -> Palette:
        return self._palette

    def toggle(self, app) -> None:
        self.apply(app, Theme.DARK if self._theme == Theme.LIGHT else Theme.LIGHT)

    def apply(self, app, theme: Theme) -> None:
        self._theme = theme
        self._palette = DARK if theme == Theme.DARK else LIGHT
        app.setStyleSheet(build_stylesheet(self._palette))
        self.changed.emit(self._palette)


theme_manager = ThemeManager()