"""表结构对比：比较两个库（或两个连接）的表和列差异，生成迁移 SQL。

方向：源(A) → 目标(B)，即生成把 A 改成 B 的 SQL。
生成的迁移 SQL 应在 **A** 库上执行。

界面 v2（2024 美化重构）：
- 顶部 A/B 选择卡片化
- 差异说明 info 卡片
- 表格无边框圆角
- SQL 预览强调边框
- 底部按钮主次分明
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPlainTextEdit,
    QPushButton, QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from ..sql_util import qualify_table, quote_ident
from ..theme import theme_manager

log = logging.getLogger(__name__)

_INPUT_H = 30
_BTN_H = 32


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool = True
    default: str | None = None


@dataclass
class TableInfo:
    name: str
    columns: dict[str, ColumnInfo] = field(default_factory=dict)


@dataclass
class SchemaSnapshot:
    name: str
    tables: dict[str, TableInfo] = field(default_factory=dict)


def _snapshot(client, schema: str | None, label: str) -> SchemaSnapshot:
    snap = SchemaSnapshot(name=label)
    entries = client.tables(schema=schema)
    names = [t for t, kind in entries if kind == "table"]
    col_map = client.multi_columns(names, schema=schema)
    for t in names:
        tbl = TableInfo(name=t)
        for c in col_map.get(t, []):
            name = c.get("name")
            if not name:
                continue
            tbl.columns[str(name)] = ColumnInfo(
                name=str(name),
                type=str(c.get("type") or ""),
                nullable=bool(c.get("nullable", True)),
                default=c.get("default"),
            )
        snap.tables[t] = tbl
    return snap


@dataclass
class DiffResult:
    only_in_a: list[str] = field(default_factory=list)
    only_in_b: list[str] = field(default_factory=list)
    common: list[str] = field(default_factory=list)
    column_diffs: dict[str, dict] = field(default_factory=dict)


def _diff(a: SchemaSnapshot, b: SchemaSnapshot) -> DiffResult:
    r = DiffResult()
    ta, tb = set(a.tables), set(b.tables)
    r.only_in_a = sorted(ta - tb)
    r.only_in_b = sorted(tb - ta)
    r.common = sorted(ta & tb)

    for t in r.common:
        ca = a.tables[t].columns
        cb = b.tables[t].columns
        only_a = [ca[n] for n in ca if n not in cb]
        only_b = [cb[n] for n in cb if n not in ca]
        changed: list[tuple[str, ColumnInfo, ColumnInfo]] = []
        for n in ca:
            if n in cb:
                x, y = ca[n], cb[n]
                if (x.type.lower() != y.type.lower()
                        or x.nullable != y.nullable
                        or (x.default or "") != (y.default or "")):
                    changed.append((n, x, y))
        if only_a or only_b or changed:
            r.column_diffs[t] = {
                "only_a": only_a, "only_b": only_b, "changed": changed,
            }
    return r


class SchemaDiffDialog(QDialog):
    """选源/目标 → 展示差异 → 复制迁移 SQL。"""

    def __init__(
        self,
        parent=None,
        *,
        connections: list[str],
        client_getter,
        db_lister,
        preset_a: tuple[str, str] | None = None,
        preset_b: tuple[str, str] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("表结构对比")
        self.setMinimumSize(1080, 720)

        self._client_getter = client_getter
        self._db_lister = db_lister
        self._snap_a: SchemaSnapshot | None = None
        self._snap_b: SchemaSnapshot | None = None
        self._diff: DiffResult | None = None

        # ---------- A / B 选择控件 ----------
        self.conn_a = QComboBox()
        self.conn_a.addItems(connections)
        self.conn_a.setMinimumHeight(_INPUT_H)
        self.db_a = QComboBox()
        self.db_a.setMinimumHeight(_INPUT_H)

        self.conn_b = QComboBox()
        self.conn_b.addItems(connections)
        self.conn_b.setMinimumHeight(_INPUT_H)
        self.db_b = QComboBox()
        self.db_b.setMinimumHeight(_INPUT_H)

        if preset_a:
            self.conn_a.setCurrentText(preset_a[0])
        if preset_b:
            self.conn_b.setCurrentText(preset_b[0])

        # ---------- A 卡片 ----------
        form_a = QFormLayout()
        form_a.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form_a.setHorizontalSpacing(12)
        form_a.setVerticalSpacing(10)
        form_a.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form_a.addRow("连接", self.conn_a)
        form_a.addRow("库 / Schema", self.db_a)

        box_a = QWidget()
        box_a.setObjectName("cardA")
        box_a.setLayout(form_a)

        # ---------- B 卡片 ----------
        form_b = QFormLayout()
        form_b.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form_b.setHorizontalSpacing(12)
        form_b.setVerticalSpacing(10)
        form_b.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form_b.addRow("连接", self.conn_b)
        form_b.addRow("库 / Schema", self.db_b)

        box_b = QWidget()
        box_b.setObjectName("cardB")
        box_b.setLayout(form_b)

        # ---------- 对比按钮 ----------
        self.run_btn = QPushButton("开始对比")
        self.run_btn.setObjectName("primaryBtn")
        self.run_btn.setCursor(Qt.PointingHandCursor)
        self.run_btn.setMinimumHeight(_BTN_H)
        self.run_btn.setMinimumWidth(120)
        self.run_btn.clicked.connect(self._run)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_a = QLabel("<b>源（A）</b>")
        header_b = QLabel("<b>目标（B）</b>")
        header_row.addWidget(header_a, 1)
        header_row.addSpacing(20)
        header_row.addWidget(header_b, 1)

        body_row = QHBoxLayout()
        body_row.setContentsMargins(0, 0, 0, 0)
        body_row.setSpacing(16)
        body_row.addWidget(box_a, 1)
        body_row.addWidget(box_b, 1)
        body_row.addWidget(self.run_btn, alignment=Qt.AlignBottom)

        # ---------- 说明卡片 ----------
        tip = QLabel(
            "生成 SQL 的方向为「把 A 改成 B」，<b>脚本在 A 库上执行</b>：<br>"
            "• A 有 B 无 → <code>DROP TABLE</code>　"
            "• B 有 A 无 → <code>CREATE TABLE</code>　"
            "• 共有表的列差异 → <code>ALTER TABLE …</code>"
        )
        tip.setObjectName("infoCard")
        tip.setWordWrap(True)

        # ---------- 表清单 ----------
        self.table_list = QTableWidget(0, 3)
        self.table_list.setHorizontalHeaderLabels(["表", "状态", "细节"])
        hdr = self.table_list.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table_list.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table_list.setSelectionBehavior(QTableWidget.SelectRows)
        self.table_list.verticalHeader().setVisible(False)
        self.table_list.setAlternatingRowColors(True)
        self.table_list.setShowGrid(False)
        self.table_list.setFrameShape(QTableWidget.NoFrame)

        # ---------- 列详细 ----------
        self.col_table = QTableWidget(0, 5)
        self.col_table.setHorizontalHeaderLabels(
            ["表", "列", "A 定义", "B 定义", "变更"]
        )
        self.col_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Stretch
        )
        self.col_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.col_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.col_table.verticalHeader().setVisible(False)
        self.col_table.setAlternatingRowColors(True)
        self.col_table.setShowGrid(False)
        self.col_table.setFrameShape(QTableWidget.NoFrame)

        # ---------- SQL 预览 ----------
        self.sql_preview = QPlainTextEdit()
        self.sql_preview.setReadOnly(True)
        self.sql_preview.setStyleSheet(
            "font-family: Consolas, 'JetBrains Mono', monospace; font-size: 12px;"
        )

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self.table_list, "表清单")
        tabs.addTab(self.col_table, "列 / 详细")
        tabs.addTab(self.sql_preview, "迁移 SQL")

        # ---------- 底部按钮 ----------
        self.copy_btn = QPushButton("复制全部 SQL")
        self.copy_btn.setObjectName("primaryBtn")
        self.copy_btn.setCursor(Qt.PointingHandCursor)
        self.copy_btn.setMinimumHeight(_BTN_H)
        self.copy_btn.setMinimumWidth(140)
        self.copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(
                self.sql_preview.toPlainText()
            )
        )

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_btn = buttons.button(QDialogButtonBox.Close)
        close_btn.setText("关闭")
        close_btn.setMinimumHeight(_BTN_H)
        close_btn.setMinimumWidth(88)
        buttons.rejected.connect(self.reject)
        buttons.addButton(self.copy_btn, QDialogButtonBox.ActionRole)

        # ---------- 总布局 ----------
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(header_row)
        layout.addLayout(body_row)
        layout.addWidget(tip)
        layout.addWidget(tabs, 1)
        layout.addWidget(buttons)

        self._apply_style()

        # ---------- 信号 ----------
        self.conn_a.currentTextChanged.connect(
            lambda _: self._reload_dbs(self.conn_a, self.db_a))
        self.conn_b.currentTextChanged.connect(
            lambda _: self._reload_dbs(self.conn_b, self.db_b))
        self._reload_dbs(self.conn_a, self.db_a)
        self._reload_dbs(self.conn_b, self.db_b)

        if preset_a:
            self.db_a.setCurrentText(preset_a[1])
        if preset_b:
            self.db_b.setCurrentText(preset_b[1])

    # ---------- 样式 ----------
    def _apply_style(self) -> None:
        p = theme_manager.palette
        self.setStyleSheet(f"""
            QDialog {{
                background: {p.bg};
            }}
            QWidget#cardA {{
                background: {p.surface};
                border: 1px solid {p.border};
                border-radius: 12px;
                padding: 4px;
            }}
            QWidget#cardB {{
                background: {p.surface};
                border: 1px solid {p.border};
                border-radius: 12px;
                padding: 4px;
            }}
            QLabel#infoCard {{
                background: {p.surface_alt};
                border: 1px solid {p.border};
                border-radius: 10px;
                padding: 12px 14px;
                color: {p.text_muted};
                line-height: 1.5;
            }}
            QPushButton#primaryBtn {{
                background: {p.accent};
                color: #ffffff;
                border: 1px solid {p.accent};
                border-radius: 8px;
                padding: 6px 18px;
                font-weight: 600;
            }}
            QPushButton#primaryBtn:hover {{
                background: {p.accent_hover};
                border-color: {p.accent_hover};
            }}
            QPushButton#primaryBtn:pressed {{
                background: {p.accent_pressed};
            }}
            QTabWidget::pane {{
                border: 1px solid {p.border};
                border-radius: 10px;
                background: {p.surface};
                top: -1px;
                padding: 4px;
            }}
            QPlainTextEdit {{
                background: {p.editor_bg};
                color: {p.text};
                border: 1px solid {p.border_strong};
                border-radius: 8px;
                padding: 10px 12px;
                selection-background-color: {p.editor_selection};
                selection-color: {p.text};
            }}
            QPlainTextEdit:focus {{
                border-color: {p.accent};
            }}
        """)

    # ---------- 内部 ----------
    def _reload_dbs(self, conn_combo: QComboBox, db_combo: QComboBox) -> None:
        name = conn_combo.currentText().strip()
        db_combo.clear()
        if not name:
            return
        try:
            dbs = list(self._db_lister(name))
        except Exception as exc:  # noqa: BLE001
            log.debug("加载库列表失败：%s", exc)
            return
        db_combo.addItems(dbs)

    def _run(self) -> None:
        conn_a = self.conn_a.currentText().strip()
        db_a = self.db_a.currentText().strip()
        conn_b = self.conn_b.currentText().strip()
        db_b = self.db_b.currentText().strip()
        if not conn_a or not db_a or not conn_b or not db_b:
            QMessageBox.warning(self, "参数不合法", "请选择源和目标。")
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            client_a = self._client_getter(conn_a)
            client_b = self._client_getter(conn_b)
            if client_a is None or client_b is None:
                raise RuntimeError("连接尚未打开")
            self._snap_a = _snapshot(
                client_a, db_a or None, f"{conn_a} / {db_a}")
            self._snap_b = _snapshot(
                client_b, db_b or None, f"{conn_b} / {db_b}")
            self._diff = _diff(self._snap_a, self._snap_b)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "对比失败", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        self._render()

    def _render(self) -> None:
        assert self._diff is not None
        d = self._diff

        self.table_list.setRowCount(0)
        for name in d.only_in_a:
            r = self.table_list.rowCount()
            self.table_list.insertRow(r)
            self.table_list.setItem(r, 0, QTableWidgetItem(name))
            self.table_list.setItem(r, 1, QTableWidgetItem("仅 A 有"))
            self.table_list.setItem(r, 2, QTableWidgetItem("将执行 DROP TABLE"))
        for name in d.only_in_b:
            r = self.table_list.rowCount()
            self.table_list.insertRow(r)
            self.table_list.setItem(r, 0, QTableWidgetItem(name))
            self.table_list.setItem(r, 1, QTableWidgetItem("仅 B 有"))
            self.table_list.setItem(
                r, 2, QTableWidgetItem("将执行 CREATE TABLE"))
        for name in d.common:
            detail = d.column_diffs.get(name)
            if not detail:
                continue
            bits = []
            if detail["only_a"]:
                bits.append(f"A 独有 {len(detail['only_a'])} 列")
            if detail["only_b"]:
                bits.append(f"B 独有 {len(detail['only_b'])} 列")
            if detail["changed"]:
                bits.append(f"{len(detail['changed'])} 列定义变动")
            r = self.table_list.rowCount()
            self.table_list.insertRow(r)
            self.table_list.setItem(r, 0, QTableWidgetItem(name))
            self.table_list.setItem(r, 1, QTableWidgetItem("列差异"))
            self.table_list.setItem(r, 2, QTableWidgetItem("；".join(bits)))

        self.col_table.setRowCount(0)
        for name in d.common:
            detail = d.column_diffs.get(name)
            if not detail:
                continue
            for c in detail["only_a"]:
                self._add_col_row(name, c.name, self._fmt(c), "（无）", "删除列")
            for c in detail["only_b"]:
                self._add_col_row(name, c.name, "（无）", self._fmt(c), "新增列")
            for col_name, ca, cb in detail["changed"]:
                self._add_col_row(name, col_name, self._fmt(ca),
                                  self._fmt(cb), "修改列")

        self.sql_preview.setPlainText(self._build_sql())

    def _add_col_row(self, table: str, col: str,
                     a: str, b: str, act: str) -> None:
        r = self.col_table.rowCount()
        self.col_table.insertRow(r)
        self.col_table.setItem(r, 0, QTableWidgetItem(table))
        self.col_table.setItem(r, 1, QTableWidgetItem(col))
        self.col_table.setItem(r, 2, QTableWidgetItem(a))
        self.col_table.setItem(r, 3, QTableWidgetItem(b))
        self.col_table.setItem(r, 4, QTableWidgetItem(act))

    @staticmethod
    def _fmt(c: ColumnInfo) -> str:
        bits = [c.type or "?"]
        if not c.nullable:
            bits.append("NOT NULL")
        if c.default is not None:
            bits.append(f"DEFAULT {c.default}")
        return " ".join(bits)

    def _build_sql(self) -> str:
        assert self._diff is not None
        if self._snap_a is None or self._snap_b is None:
            return ""
        d = self._diff

        # 方向是"把 A 改成 B"，脚本在 A 上执行。
        # db_type 与 schema_target 均取自 A。
        db_type = "mysql"
        try:
            c = self._client_getter(self.conn_a.currentText().strip())
            if c is not None:
                db_type = c.cfg.db_type
        except Exception:  # noqa: BLE001
            pass

        q = lambda n: quote_ident(n, db_type)  # noqa: E731

        schema_target = self.db_a.currentText().strip() or None

        lines: list[str] = [
            f"-- 源（将被修改）：{self._snap_a.name}",
            f"-- 参考目标：{self._snap_b.name}",
            "-- 脚本在源（A）库上执行，使 A 的结构与 B 一致。",
            "",
        ]

        for name in d.only_in_a:
            lines.append(
                f"DROP TABLE {qualify_table(name, schema_target, db_type)};"
            )
        if d.only_in_a:
            lines.append("")

        for name in d.only_in_b:
            tbl = self._snap_b.tables[name]
            col_defs: list[str] = []
            for c in tbl.columns.values():
                col_defs.append(
                    f"  {q(c.name)} {c.type}"
                    + ("" if c.nullable else " NOT NULL")
                    + (f" DEFAULT {c.default}"
                       if c.default is not None else "")
                )
            qualified = qualify_table(name, schema_target, db_type)
            lines.append(f"CREATE TABLE {qualified} (")
            lines.append(",\n".join(col_defs))
            lines.append(");")
            lines.append("")

        for name in d.common:
            detail = d.column_diffs.get(name)
            if not detail:
                continue
            qualified = qualify_table(name, schema_target, db_type)
            for c in detail["only_a"]:
                lines.append(
                    f"ALTER TABLE {qualified} DROP COLUMN {q(c.name)};"
                )
            for c in detail["only_b"]:
                parts = [q(c.name), c.type]
                if not c.nullable:
                    parts.append("NOT NULL")
                if c.default is not None:
                    parts.append(f"DEFAULT {c.default}")
                lines.append(
                    f"ALTER TABLE {qualified} ADD COLUMN "
                    + " ".join(parts) + ";"
                )
            for col_name, _ca, cb in detail["changed"]:
                if db_type == "mysql":
                    parts = [q(col_name), cb.type]
                    if not cb.nullable:
                        parts.append("NOT NULL")
                    if cb.default is not None:
                        parts.append(f"DEFAULT {cb.default}")
                    lines.append(
                        f"ALTER TABLE {qualified} MODIFY COLUMN "
                        + " ".join(parts) + ";"
                    )
                elif db_type == "postgresql":
                    lines.append(
                        f"ALTER TABLE {qualified} ALTER COLUMN "
                        f"{q(col_name)} TYPE {cb.type};"
                    )
                    if cb.nullable:
                        lines.append(
                            f"ALTER TABLE {qualified} ALTER COLUMN "
                            f"{q(col_name)} DROP NOT NULL;"
                        )
                    else:
                        lines.append(
                            f"ALTER TABLE {qualified} ALTER COLUMN "
                            f"{q(col_name)} SET NOT NULL;"
                        )
                    if cb.default is not None:
                        lines.append(
                            f"ALTER TABLE {qualified} ALTER COLUMN "
                            f"{q(col_name)} SET DEFAULT {cb.default};"
                        )
                    else:
                        lines.append(
                            f"ALTER TABLE {qualified} ALTER COLUMN "
                            f"{q(col_name)} DROP DEFAULT;"
                        )
                else:
                    lines.append(
                        f"-- 表 {name} 列 {col_name}："
                        f"{self._fmt(_ca)} → {self._fmt(cb)}（请手工处理）"
                    )
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"