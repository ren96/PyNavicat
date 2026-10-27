"""应用设置：外观 / 查询。写回 QSettings，调用方负责应用副作用。

界面 v2（2024 美化重构）：
- 选项卡左右留白更宽松
- 表单统一对齐、间距舒展
- 底部按钮主次分明
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QFrame,
    QLabel, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from .config import settings
from .theme import theme_manager


class SettingsDialog(QDialog):
    """打开前请确保主题等已从 settings 读取；本对话框只负责写回。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumSize(600, 480)
        s = settings()

        # ---------- 外观 ----------
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.addItem("深色", "dark")
        idx = self.theme_combo.findData(str(s.value("ui/theme", "light")))
        self.theme_combo.setCurrentIndex(max(0, idx))
        self.theme_combo.setMinimumHeight(30)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(8, 22)
        self.font_size_spin.setValue(int(s.value("ui/font_size", 11)))
        self.font_size_spin.setSuffix(" pt")
        self.font_size_spin.setMinimumHeight(30)

        appearance = QWidget()
        fa = QFormLayout(appearance)
        fa.setContentsMargins(24, 22, 24, 22)
        fa.setHorizontalSpacing(20)
        fa.setVerticalSpacing(14)
        fa.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        fa.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        fa.addRow("主题", self.theme_combo)
        fa.addRow("编辑器字号", self.font_size_spin)

        # ---------- 查询 ----------
        self.page_size_spin = QSpinBox()
        self.page_size_spin.setRange(10, 100_000)
        self.page_size_spin.setValue(int(s.value("query/page_size", 200)))
        self.page_size_spin.setMinimumHeight(30)

        self.max_rows_spin = QSpinBox()
        self.max_rows_spin.setRange(100, 10_000_000)
        self.max_rows_spin.setValue(int(s.value("query/max_rows", 5000)))
        self.max_rows_spin.setMinimumHeight(30)

        self.confirm_danger = QCheckBox(
            "执行 DROP / TRUNCATE / 无 WHERE 的 DELETE/UPDATE 前弹窗确认"
        )
        self.confirm_danger.setChecked(
            bool(s.value("query/confirm_danger", True, type=bool))
        )

        self.mask_sensitive = QCheckBox(
            "结果表中默认对敏感列（password/token 等）脱敏"
        )
        self.mask_sensitive.setChecked(
            bool(s.value("query/mask_sensitive", True, type=bool))
        )

        query = QWidget()
        fq = QFormLayout(query)
        fq.setContentsMargins(24, 22, 24, 22)
        fq.setHorizontalSpacing(20)
        fq.setVerticalSpacing(14)
        fq.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        fq.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        fq.addRow("默认分页大小", self.page_size_spin)
        fq.addRow("单次返回上限", self.max_rows_spin)
        fq.addRow("", self.confirm_danger)
        fq.addRow("", self.mask_sensitive)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(appearance, "外观")
        tabs.addTab(query, "查询")

        # ---------- 底部按钮 ----------
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("保存")
        ok_btn.setMinimumHeight(32)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        cancel_btn.setMinimumHeight(32)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        bottom = QWidget()
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(18, 10, 18, 14)
        bottom_layout.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(tabs, 1)
        layout.addWidget(bottom)

        self._apply_style()

    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QDialog {{
                background: {p.bg};
            }}
            QTabWidget::pane {{
                border: 1px solid {p.border};
                border-radius: 10px;
                background: {p.surface};
                top: -1px;
                padding: 4px;
            }}
            QLabel {{
                color: {p.text};
            }}
            QCheckBox {{
                spacing: 8px;
                padding: 4px 0;
            }}
        """)

    def _save(self) -> None:
        s = settings()
        s.setValue("ui/theme", self.theme_combo.currentData())
        s.setValue("ui/font_size", self.font_size_spin.value())
        s.setValue("query/page_size", self.page_size_spin.value())
        s.setValue("query/max_rows", self.max_rows_spin.value())
        s.setValue("query/confirm_danger", self.confirm_danger.isChecked())
        s.setValue("query/mask_sensitive", self.mask_sensitive.isChecked())
        s.sync()
        self.accept()