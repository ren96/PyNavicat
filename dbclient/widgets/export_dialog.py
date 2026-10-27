"""导出向导：选列 / 行数上限 / 编码 / 批量。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)


class ExportDialog(QDialog):
    """返回 (selected_column_indices, limit, encoding, batch)。

    说明：导出的是「当前结果集」在客户端按列筛选和行数截断后的数据。
    WHERE 子句不对结果集生效（那是 SQL 层面的概念），因此本对话框不再暴露该字段，
    需要条件过滤时请使用结果表上方工具栏的过滤框，再选择「过滤结果」导出。
    """

    def __init__(
        self,
        parent=None,
        columns: list[str] | None = None,
        table: str = "",
        db_type: str = "mysql",
        kind: str = "csv",
    ):
        super().__init__(parent)
        self.setWindowTitle(f"导出 {kind.upper()}")
        self.setMinimumSize(540, 480)
        self._columns = list(columns or [])
        self._table = table
        self._db_type = db_type
        self._kind = kind

        # ---- 列勾选（每个 checkbox 关联列索引，避免重复列名歧义）----
        self._col_boxes: list[QCheckBox] = []
        self._col_indices: list[int] = []
        col_host = QVBoxLayout()
        col_host.setContentsMargins(8, 4, 8, 4)
        col_host.setSpacing(2)
        col_host.addWidget(QLabel("选择要导出的列："))
        for i, c in enumerate(self._columns):
            label = str(c)
            box = QCheckBox(label)
            box.setChecked(True)
            self._col_boxes.append(box)
            self._col_indices.append(i)
            col_host.addWidget(box)
        col_host.addStretch(1)
        col_widget = QWidget()
        col_widget.setLayout(col_host)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(col_widget)

        self.all_btn = QPushButton("全选")
        self.all_btn.clicked.connect(lambda: self._set_all(True))
        self.none_btn = QPushButton("全不选")
        self.none_btn.clicked.connect(lambda: self._set_all(False))
        col_bar = QHBoxLayout()
        col_bar.addWidget(self.all_btn)
        col_bar.addWidget(self.none_btn)
        col_bar.addStretch(1)

        # ---- 行数上限 ----
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(0, 100_000_000)
        self.limit_spin.setValue(0)
        self.limit_spin.setSpecialValueText("不限制")
        self.limit_spin.setToolTip("最多导出的行数（0 = 全部）")

        # ---- 编码（CSV）----
        self.encoding_combo = QComboBox()
        for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
            self.encoding_combo.addItem(enc, enc)

        # ---- 批量（SQL）----
        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 1000)
        self.batch_spin.setValue(1)
        self.batch_spin.setToolTip("每条 INSERT 包含的行数")

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight)
        form.addRow("行数上限", self.limit_spin)
        if kind == "csv":
            form.addRow("编码", self.encoding_combo)
        if kind == "sql":
            form.addRow("批量", self.batch_spin)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("下一步")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(col_bar)
        layout.addWidget(scroll, 1)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _set_all(self, checked: bool) -> None:
        for b in self._col_boxes:
            b.setChecked(checked)

    def _on_accept(self) -> None:
        if not self.result_column_indices():
            QMessageBox.warning(self, "提示", "请至少选择一列。")
            return
        self.accept()

    def result_columns(self) -> list[str]:
        """兼容旧调用：返回列名列表。"""
        return [self._columns[i] for i in self.result_column_indices()]

    def result_column_indices(self) -> list[int]:
        """返回被勾选的列在结果集中的原始索引。"""
        return [
            idx for idx, box in zip(self._col_indices, self._col_boxes)
            if box.isChecked()
        ]

    def result_limit(self) -> int:
        return int(self.limit_spin.value())

    def result_encoding(self) -> str:
        return str(self.encoding_combo.currentData() or "utf-8-sig")

    def result_batch(self) -> int:
        return int(self.batch_spin.value())