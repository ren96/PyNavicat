"""查询页签：SQL 编辑器 + 结果区（多结果子页签 + 分页 + 取消 + 事务 + 流式加载
+ 只读拦截 + EXPLAIN 可视化）。

界面 v2（2024 美化重构）：
- 顶部工具栏图标化 + 分组（执行 / 事务 / 分页）
- 分页控件 segmented 化
- 底部状态栏 chip 化（连接信息胶囊）
- 更宽松的留白，更细腻的 hover 反馈
"""
from __future__ import annotations

import re
import threading

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QSpinBox, QSplitter, QTabWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from .. import config as _config
from ..config import DEFAULT_MAX_ROWS, DEFAULT_PAGE_SIZE
from ..engine import (
    DatabaseClient, QueryCancelled, dangerous_reason, split_statements,
)
from .explain_view import ExplainView, is_explain_result
from .result_view import ResultTable, TableContext
from .sql_editor import SqlEditor, validate_sql

_STREAM_BATCH = 500

_WRITE_RE = re.compile(
    r"\b(insert|update|delete|replace|merge|upsert"
    r"|drop|truncate|alter|create|rename|comment"
    r"|grant|revoke|call|exec|execute|lock|unlock"
    r"|copy|load\s+data|refresh\s+materialized"
    r"|set\s+(global|session|role)"
    r"|analyze|vacuum|optimize|reindex|repair)\b",
    re.I,
)
_SELECT_INTO_WRITE_RE = re.compile(
    r"\bselect\b[\s\S]*?\binto\s+(outfile|dumpfile)\b", re.I
)
_DO_BLOCK_RE = re.compile(r"^\s*do\b", re.I)
_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _strip_comments_and_strings(stmt: str) -> str:
    """把 SQL 里的注释与字符串字面量替换为空格，保留结构。"""
    out: list[str] = []
    n = len(stmt)
    i = 0
    quote: str | None = None
    dollar_tag: str | None = None

    while i < n:
        ch = stmt[i]

        if dollar_tag is not None:
            if stmt.startswith(dollar_tag, i):
                i += len(dollar_tag)
                dollar_tag = None
                out.append(" ")
                continue
            i += 1
            continue

        if quote is not None:
            if ch == quote:
                if i + 1 < n and stmt[i + 1] == quote:
                    i += 2
                    continue
                quote = None
                out.append(" ")
            elif ch == "\\" and i + 1 < n:
                i += 2
                continue
            i += 1
            continue

        if ch == "$":
            m = _DOLLAR_TAG_RE.match(stmt, i)
            if m is not None:
                dollar_tag = m.group(0)
                i = m.end()
                out.append(" ")
                continue

        if ch in ("'", '"', "`"):
            quote = ch
            out.append(" ")
            i += 1
            continue

        if ch == "-" and i + 1 < n and stmt[i + 1] == "-":
            j = stmt.find("\n", i)
            if j == -1:
                break
            out.append(" ")
            i = j
            continue
        if ch == "/" and i + 1 < n and stmt[i + 1] == "*":
            j = stmt.find("*/", i + 2)
            if j == -1:
                break
            out.append(" ")
            i = j + 2
            continue

        out.append(ch)
        i += 1

    return "".join(out)


def _has_write(sql: str) -> bool:
    """判断 SQL 文本是否包含写操作（多条语句中只要有一条就算）。"""
    for stmt in split_statements(sql):
        s = _strip_comments_and_strings(stmt)
        if _WRITE_RE.search(s) or _DO_BLOCK_RE.match(s):
            return True
        if _SELECT_INTO_WRITE_RE.search(s):
            return True
    return False


class _WorkerSignals(QObject):
    finished = Signal(list)
    failed = Signal(str)
    cancelled = Signal()
    batch = Signal(int, list, list)


class QueryWorker(QRunnable):
    def __init__(
        self,
        client: DatabaseClient,
        sql: str,
        max_rows: int,
        schema=None,
        page: int | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
        table_ctx: dict | None = None,
        tx_conn=None,
        streaming: bool = False,
    ):
        super().__init__()
        self.client = client
        self.sql = sql
        self.max_rows = max_rows
        self.schema = schema
        self.page = page
        self.page_size = page_size
        self.table_ctx = table_ctx
        self.tx_conn = tx_conn
        self.streaming = streaming
        self.cancel_event = threading.Event()
        self.signals = _WorkerSignals()

    def cancel(self) -> None:
        self.cancel_event.set()

    def _emit_batch(self, idx: int, cols: list, rows: list) -> None:
        self.signals.batch.emit(idx, cols, rows)

    def run(self) -> None:
        try:
            if self.page is not None:
                res = self.client.execute_paged(
                    self.sql, page=self.page, page_size=self.page_size,
                    schema=self.schema, table_ctx=self.table_ctx,
                    cancel_event=self.cancel_event,
                )
                self.signals.finished.emit([res])
            else:
                res = self.client.execute_many(
                    self.sql, max_rows=self.max_rows, schema=self.schema,
                    table_ctx=self.table_ctx, cancel_event=self.cancel_event,
                    tx_conn=self.tx_conn,
                    row_batch_size=_STREAM_BATCH if self.streaming else 0,
                    on_batch=self._emit_batch if self.streaming else None,
                )
                self.signals.finished.emit(res)
        except QueryCancelled:
            self.signals.cancelled.emit()
        except Exception as exc:  # noqa: BLE001
            if self.cancel_event.is_set():
                self.signals.cancelled.emit()
            else:
                self.signals.failed.emit(str(exc))


# ============================================================
# 小工具：分隔线 / 分组
# ============================================================
def _vsep() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.VLine)
    line.setProperty("role", "sep")
    line.setFixedWidth(1)
    return line


class QueryTab(QWidget):
    def __init__(
        self,
        client: DatabaseClient,
        schema=None,
        sql: str = "",
        parent=None,
        *,
        paged: bool = False,
        table_ctx: dict | None = None,
    ):
        super().__init__(parent)
        self.client = client
        self.schema = schema
        self.max_rows = _config.max_rows()
        self.paged = paged
        self.table_ctx = table_ctx
        self.page = 1
        self.page_size = _config.page_size()
        self.base_sql: str | None = None
        self.table_key: tuple | None = None
        self._running = False
        self._worker: QueryWorker | None = None
        self._tx_conn = None
        self._pool = QThreadPool.globalInstance()
        self._pending_tables: dict[int, QWidget] = {}
        self._pending_close_tx = False

        # ---------- 编辑器 ----------
        self.editor = SqlEditor(self)
        if sql:
            self.editor.setPlainText(sql)

        # ---------- 工具栏按钮 ----------
        self.run_btn = QPushButton("▶  执行")
        self.run_btn.setObjectName("runPrimary")
        self.run_btn.setToolTip("执行 SQL（Ctrl+Enter / Ctrl+R）")
        self.run_btn.setCursor(Qt.PointingHandCursor)
        self.run_btn.setMinimumWidth(96)
        self.run_btn.clicked.connect(lambda: self.execute_sql())

        self.stop_btn = QPushButton("⏹  停止")
        self.stop_btn.setToolTip("取消正在执行的查询（Esc）")
        self.stop_btn.setCursor(Qt.PointingHandCursor)
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.cancel_running)

        self.format_btn = QPushButton("✨  格式化")
        self.format_btn.setToolTip("格式化 SQL（Ctrl+Shift+F）；选中时只格式化选中片段")
        self.format_btn.setCursor(Qt.PointingHandCursor)
        self.format_btn.clicked.connect(self.editor.format_selection)

        # ---------- 事务 ----------
        self.tx_check = QCheckBox("手动事务")
        self.tx_check.setToolTip("勾选后，SQL 在事务中执行，需手动提交或回滚")
        self.tx_check.toggled.connect(self._on_tx_toggled)

        self.commit_btn = QPushButton("提交")
        self.commit_btn.setCursor(Qt.PointingHandCursor)
        self.commit_btn.setEnabled(False)
        self.commit_btn.setMinimumWidth(56)
        self.commit_btn.clicked.connect(self._commit_tx)

        self.rollback_btn = QPushButton("回滚")
        self.rollback_btn.setCursor(Qt.PointingHandCursor)
        self.rollback_btn.setEnabled(False)
        self.rollback_btn.setMinimumWidth(56)
        self.rollback_btn.clicked.connect(self._rollback_tx)

        # ---------- 分页控件（segmented 化） ----------
        self.first_btn = self._make_page_btn("↩️", "首页")
        self.prev_btn = self._make_page_btn("⬆️", "上一页")
        self.next_btn = self._make_page_btn("⬇️", "下一页")
        self.last_btn = self._make_page_btn("↪️", "末页")

        self.page_spin = QSpinBox()
        self.page_spin.setRange(1, 1_000_000)
        self.page_spin.setValue(1)
        self.page_spin.setFixedWidth(84)
        self.page_spin.setToolTip("输入页码后按回车跳转")
        self.page_spin.setAlignment(Qt.AlignCenter)
        self.page_spin.setButtonSymbols(QSpinBox.NoButtons)

        self.page_label = QLabel("第 1 页")
        self.page_label.setProperty("role", "hint")

        self.first_btn.clicked.connect(lambda: self._goto_page(1))
        self.prev_btn.clicked.connect(lambda: self._goto_page(self.page - 1))
        self.next_btn.clicked.connect(lambda: self._goto_page(self.page + 1))
        self.last_btn.clicked.connect(
            lambda: self._goto_page(self.page_spin.maximum())
        )
        self.page_spin.editingFinished.connect(
            lambda: self._goto_page(self.page_spin.value())
        )

        # ---------- 状态 ----------
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("queryStatus")
        self.status_label.setProperty("role", "hint")

        target = client.cfg.name + (f" / {schema}" if schema else "")
        conn_text = f"{target}"
        self.conn_label = QLabel(conn_text)
        self.conn_label.setObjectName("connChip")
        if client.cfg.readonly:
            self.conn_label.setText(f"🔒 只读  ·  {conn_text}")
            self.conn_label.setProperty("readonly", "true")
            self.conn_label.setToolTip("当前连接为只读，禁止执行写操作")
        else:
            self.conn_label.setProperty("readonly", "false")
        self.conn_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        # ---------- 顶部工具条 ----------
        bar = QHBoxLayout()
        bar.setContentsMargins(12, 10, 12, 8)
        bar.setSpacing(6)

        bar.addWidget(self.run_btn)
        bar.addWidget(self.stop_btn)
        bar.addWidget(self.format_btn)

        bar.addSpacing(4)
        bar.addWidget(_vsep())
        bar.addSpacing(4)

        bar.addWidget(self.tx_check)
        bar.addWidget(self.commit_btn)
        bar.addWidget(self.rollback_btn)

        if paged:
            bar.addSpacing(4)
            bar.addWidget(_vsep())
            bar.addSpacing(4)

            # 分页控件装进一个胶囊容器
            pager = QWidget()
            pager.setObjectName("pagerCapsule")
            pager_layout = QHBoxLayout(pager)
            pager_layout.setContentsMargins(4, 2, 4, 2)
            pager_layout.setSpacing(2)
            pager_layout.addWidget(self.first_btn)
            pager_layout.addWidget(self.prev_btn)
            pager_layout.addWidget(self.page_spin)
            pager_layout.addWidget(self.next_btn)
            pager_layout.addWidget(self.last_btn)

            bar.addWidget(pager)
            bar.addSpacing(6)
            bar.addWidget(self.page_label)

        bar.addStretch(1)

        top = QWidget()
        top.setObjectName("queryTop")
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(6)
        top_layout.addLayout(bar)
        top_layout.addWidget(self.editor, 1)

        # ---------- 结果区 ----------
        self.result_tabs = QTabWidget()
        self.result_tabs.setObjectName("resultTabs")
        self.result_tabs.setDocumentMode(True)
        self.result_tabs.setTabsClosable(False)
        self.result_tabs.setStyleSheet(
            "QTabWidget::tab-bar { alignment: left; }"
        )

        splitter = QSplitter(Qt.Vertical)
        splitter.setObjectName("querySplitter")
        splitter.addWidget(top)
        splitter.addWidget(self.result_tabs)
        splitter.setSizes([420, 380])
        splitter.setHandleWidth(6)
        splitter.setChildrenCollapsible(False)

        # ---------- 底部状态栏 ----------
        bottom_bar = QHBoxLayout()
        bottom_bar.setContentsMargins(14, 4, 14, 8)
        bottom_bar.setSpacing(10)
        bottom_bar.addWidget(self.status_label)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self.conn_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(splitter, 1)
        layout.addLayout(bottom_bar)

        self._apply_local_style()

        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.execute_sql)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self.execute_sql)
        QShortcut(QKeySequence("Escape"), self, activated=self.cancel_running)

    # ---------- 局部样式 ----------
    def _apply_local_style(self) -> None:
        from ..theme import theme_manager
        p = theme_manager.palette
        # 主执行按钮：强调色实心
        self.run_btn.setStyleSheet(f"""
            QPushButton#runPrimary {{
                background: {p.accent};
                color: #ffffff;
                border: 1px solid {p.accent};
                font-weight: 600;
                padding: 7px 18px;
            }}
            QPushButton#runPrimary:hover {{
                background: {p.accent_hover};
                border-color: {p.accent_hover};
                color: #ffffff;
            }}
            QPushButton#runPrimary:pressed {{
                background: {p.accent_pressed};
                border-color: {p.accent_pressed};
            }}
            QPushButton#runPrimary:disabled {{
                background: {p.surface_alt};
                color: {p.text_disabled};
                border-color: {p.border};
            }}
        """)

        # 分页按钮：透明底，融入胶囊
        page_btn_style = f"""
            QToolButton {{
                background: transparent;
                border: 1px solid transparent;
                border-radius: 6px;
                color: {p.text_muted};
                font-size: 12pt;
                font-weight: 600;
                padding: 0;
            }}
            QToolButton:hover {{
                background: {p.surface_hover};
                color: {p.accent};
            }}
            QToolButton:pressed {{
                background: {p.surface_active};
            }}
            QToolButton:disabled {{
                color: {p.text_disabled};
            }}
        """
        for b in (self.first_btn, self.prev_btn, self.next_btn, self.last_btn):
            b.setStyleSheet(page_btn_style)

        # 状态栏 chip
        self.setStyleSheet(f"""
            QLabel#queryStatus {{
                color: {p.text_muted};
                font-size: 9pt;
                padding: 2px 0;
            }}
            QLabel#connChip {{
                background: {p.surface_alt};
                color: {p.text_muted};
                border: 1px solid {p.border};
                border-radius: 10px;
                padding: 3px 12px;
                font-size: 9pt;
            }}
            QLabel#connChip[readonly="true"] {{
                background: {p.danger_soft};
                color: {p.danger};
                border-color: {p.danger};
            }}
            QFrame[role="sep"] {{
                background: {p.border};
                border: none;
            }}
            QWidget#queryTop {{
                background: {p.surface};
                border-bottom: 1px solid {p.border};
            }}
        """)
        # 分页胶囊容器
        self.setStyleSheet(self.styleSheet() + f"""
            QWidget#pagerCapsule {{
                background: {p.surface_alt};
                border: 1px solid {p.border};
                border-radius: 14px;
            }}
        """)

        # 页码框融入胶囊（透明底、无边框）
        self.page_spin.setStyleSheet(
            f"QSpinBox {{"
            f"  padding: 0px 6px;"
            f"  min-height: 22px;"
            f"  background: transparent;"
            f"  border: none;"
            f"  color: {p.text};"
            f"  font-weight: 600;"
            f"}}"
        )

    def _make_page_btn(self, text: str, tooltip: str) -> QToolButton:
        btn = QToolButton()
        btn.setText(text)
        btn.setToolTip(tooltip)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setFixedSize(28, 28)
        return btn

    # ---------- 状态 ----------
    def is_running(self) -> bool:
        return bool(self._running)

    def has_pending_edits(self) -> bool:
        for i in range(self.result_tabs.count()):
            w = self.result_tabs.widget(i)
            if isinstance(w, ResultTable) and w.has_pending_edits():
                return True
        return False

    # ---------- 事务 ----------
    def _block_tx_check(self, checked: bool) -> None:
        self.tx_check.blockSignals(True)
        self.tx_check.setChecked(checked)
        self.tx_check.blockSignals(False)

    def _on_tx_toggled(self, checked: bool) -> None:
        if checked:
            if self.client.cfg.readonly:
                QMessageBox.information(
                    self, "只读连接",
                    "当前连接被标记为只读，无法开启写事务。",
                )
                self._block_tx_check(False)
                return
            try:
                self._tx_conn = self.client.open_transaction()
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "开启事务失败", str(exc))
                self._block_tx_check(False)
                return
            self.commit_btn.setEnabled(True)
            self.rollback_btn.setEnabled(True)
            self._show_status("已开启事务")
        else:
            if self._tx_conn is not None:
                answer = QMessageBox.question(
                    self, "结束事务",
                    "当前事务尚未提交，是否提交？\n选择「否」将回滚。",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                )
                if answer == QMessageBox.Cancel:
                    self._block_tx_check(True)
                    return
                try:
                    if answer == QMessageBox.Yes:
                        self.client.commit(self._tx_conn)
                    else:
                        self.client.rollback(self._tx_conn)
                except Exception as exc:  # noqa: BLE001
                    QMessageBox.critical(self, "事务结束失败", str(exc))
                finally:
                    self._close_tx_conn()
            self.commit_btn.setEnabled(False)
            self.rollback_btn.setEnabled(False)

    def _commit_tx(self) -> None:
        if self._tx_conn is None:
            return
        try:
            self.client.commit(self._tx_conn)
            self._show_status("事务已提交")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "提交失败", str(exc))
            return
        self._close_tx_conn()
        self.commit_btn.setEnabled(False)
        self.rollback_btn.setEnabled(False)
        if self.tx_check.isChecked():
            self._block_tx_check(False)

    def _rollback_tx(self) -> None:
        if self._tx_conn is None:
            return
        try:
            self.client.rollback(self._tx_conn)
            self._show_status("事务已回滚")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "回滚失败", str(exc))
        finally:
            self._close_tx_conn()
            self.commit_btn.setEnabled(False)
            self.rollback_btn.setEnabled(False)
            if self.tx_check.isChecked():
                self._block_tx_check(False)

    def _close_tx_conn(self) -> None:
        if self._tx_conn is not None:
            try:
                self._tx_conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._tx_conn = None

    def _cleanup_pending_tables(self) -> None:
        for _, w in list(self._pending_tables.items()):
            idx = self.result_tabs.indexOf(w)
            if idx >= 0:
                self.result_tabs.removeTab(idx)
                try:
                    w.deleteLater()
                except Exception:  # noqa: BLE001
                    pass
        self._pending_tables.clear()

    def _disconnect_worker_signals(self) -> None:
        w = self._worker
        if w is None:
            return
        for sig in (w.signals.finished, w.signals.failed,
                    w.signals.cancelled, w.signals.batch):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass

    def cleanup(self) -> None:
        if self._running:
            self.cancel_running()
            self._pending_close_tx = True
            self._cleanup_pending_tables()
            self._disconnect_worker_signals()
        else:
            self._close_tx_conn()
            self._cleanup_pending_tables()
            self._disconnect_worker_signals()

    # ---------- 执行 ----------
    def cancel_running(self) -> None:
        if self._worker is not None and self._running:
            self._worker.cancel()
            self.stop_btn.setEnabled(False)
            self._show_status("正在取消…")

    def execute_sql(self, sql: str | None = None) -> None:
        if not isinstance(sql, str):
            sql = None
        if self._running:
            QMessageBox.information(self, "提示", "当前查询还在执行中，请稍候。")
            return
        if self.has_pending_edits():
            if QMessageBox.question(
                self, "未保存的修改",
                "当前结果表有未保存的修改，重新执行会丢失。继续？",
            ) != QMessageBox.Yes:
                return
        if sql is None:
            sql = self.editor.sql_to_run()
        if not sql or not sql.strip():
            QMessageBox.information(self, "提示", "请先输入要执行的 SQL。")
            return

        if self.client.cfg.readonly and _has_write(sql):
            QMessageBox.warning(
                self, "只读连接",
                "当前连接已被标记为只读，禁止执行 INSERT / UPDATE / DELETE\n"
                "以及 DDL、权限、维护类等写操作。\n\n"
                "如需执行写操作，请在「连接设置」中取消只读开关。",
            )
            self._show_status("只读连接已拒绝写操作")
            return

        syntax_err = validate_sql(sql)
        if syntax_err:
            QMessageBox.warning(self, "SQL 输入不合法", syntax_err)
            self._show_status("SQL 输入不合法，已拒绝执行")
            return

        if _config.settings().value("query/confirm_danger", True, type=bool):
            reason = dangerous_reason(sql)
            if reason:
                from ..dialogs import SqlPreviewDialog
                dlg = SqlPreviewDialog(self, sql=sql, reason=reason)
                if dlg.exec() != QDialog.DialogCode.Accepted:
                    return
        self.max_rows = _config.max_rows()
        self.base_sql = sql
        self.page = 1
        self._run(page=None if not self.paged else 1)

    def _goto_page(self, page: int) -> None:
        if not self.paged or self._running:
            return
        try:
            page = int(page)
        except (TypeError, ValueError):
            return
        if page < 1:
            return
        if page > self.page_spin.maximum():
            page = self.page_spin.maximum()
        if not self.base_sql:
            return
        if self.has_pending_edits():
            if QMessageBox.question(
                self, "未保存的修改",
                "翻页会丢弃结果表中未保存的修改，继续？",
            ) != QMessageBox.Yes:
                return
        self._run(page=page)

    def _run(self, page: int | None) -> None:
        self._running = True
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self._set_nav_enabled(False)
        self._show_status("执行中…")
        self._pending_tables.clear()

        streaming = page is None
        worker = QueryWorker(
            self.client,
            self.base_sql or "",
            self.max_rows,
            self.schema,
            page=page,
            page_size=self.page_size,
            table_ctx=self.table_ctx,
            tx_conn=self._tx_conn,
            streaming=streaming,
        )
        worker.signals.finished.connect(self._on_finished)
        worker.signals.failed.connect(self._on_failed)
        worker.signals.cancelled.connect(self._on_cancelled)
        worker.signals.batch.connect(self._on_batch)
        self._worker = worker
        try:
            self._pool.start(worker)
        except Exception as exc:  # noqa: BLE001
            self._running = False
            self._worker = None
            self.run_btn.setEnabled(True)
            self.stop_btn.setEnabled(False)
            self._set_nav_enabled(True)
            self._show_status("启动失败")
            QMessageBox.critical(self, "启动查询失败", str(exc))

    def _clear_results(self) -> None:
        while self.result_tabs.count():
            w = self.result_tabs.widget(0)
            self.result_tabs.removeTab(0)
            if w is not None:
                w.deleteLater()

    def _build_ctx(self, ctx_dict: dict | None, columns: list) -> TableContext | None:
        if not ctx_dict or not columns:
            return None
        return TableContext(
            table=ctx_dict.get("table", ""),
            schema=ctx_dict.get("schema"),
            db_type=ctx_dict.get("db_type", "mysql"),
            primary_keys=tuple(ctx_dict.get("primary_keys") or ()),
            editable=bool(ctx_dict.get("editable")),
            column_types=dict(ctx_dict.get("column_types") or {}),
            auto_increment_columns=tuple(
                ctx_dict.get("auto_increment_columns") or ()
            ),
        )

    def _on_batch(self, stmt_idx: int, columns: list, rows: list) -> None:
        table = self._pending_tables.get(stmt_idx)
        if table is None:
            db_type = self.client.cfg.db_type
            if is_explain_result(columns, db_type):
                view = ExplainView(
                    self, columns=columns, rows=rows, db_type=db_type,
                )
                self._pending_tables[stmt_idx] = view
                self.result_tabs.addTab(view, f"执行计划 {stmt_idx}")
                return
            ctx = self._build_ctx(self.table_ctx, columns)
            table = ResultTable(self, self.client, self.table_ctx)
            table.set_result(columns, [], ctx)
            self.result_tabs.addTab(table, f"结果 {stmt_idx} · 接收中…")
            self._pending_tables[stmt_idx] = table

        if isinstance(table, ExplainView):
            table.append_rows(rows)
            return

        table.append_rows(rows)
        idx = self.result_tabs.indexOf(table)
        if idx >= 0:
            self.result_tabs.setTabText(
                idx, f"结果 {stmt_idx} · 已接收 {table.model.rowCount()} 行"
            )
        table.info.setText(f"接收中… {table.model.rowCount()} 行")

    def _on_finished(self, results: list) -> None:
        self._running = False
        self._worker = None
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_nav_enabled(True)

        pending = dict(self._pending_tables)
        self._pending_tables.clear()
        streaming_used = bool(pending)

        query_results = [r for r in results if r.columns]
        dml_results = [r for r in results if not r.columns]

        if not streaming_used:
            self._clear_results()
            if dml_results and not query_results:
                self._add_dml_summary_tab(dml_results)
            elif dml_results and query_results:
                self._add_query_tabs(query_results)
                self._add_dml_summary_tab(dml_results)
            else:
                self._add_query_tabs(query_results)
        else:
            rolled_back = any(getattr(r, "rolled_back", False) for r in results)
            for stmt_idx, table in sorted(pending.items()):
                idx = self.result_tabs.indexOf(table)
                if idx < 0:
                    continue
                if isinstance(table, ExplainView):
                    self.result_tabs.setTabText(idx, f"执行计划 {stmt_idx}")
                    continue
                pos = stmt_idx - 1
                if 0 <= pos < len(results):
                    res = results[pos]
                    title = f"结果 {stmt_idx} · {res.message}"
                    if res.truncated:
                        title += "（已截断）"
                else:
                    received = table.model.rowCount()
                    title = f"结果 {stmt_idx} · 已接收 {received} 行（语句未完成）"
                if rolled_back:
                    title += " ⚠ 事务已回滚，上方数据无效"
                self.result_tabs.setTabText(idx, title)
                table.info.setText(title)
            if dml_results:
                self._add_dml_summary_tab(dml_results)

        if results:
            self._show_status(results[0].message)

        if self._pending_close_tx:
            self._pending_close_tx = False
            self._close_tx_conn()

    def _add_query_tabs(self, results: list) -> None:
        for i, res in enumerate(results, 1):
            ctx_dict = res.table_ctx or self.table_ctx
            db_type = (ctx_dict or {}).get("db_type") or self.client.cfg.db_type
            if is_explain_result(res.columns, db_type):
                view = ExplainView(
                    self, columns=res.columns, rows=res.rows, db_type=db_type,
                )
                self.result_tabs.addTab(view, f"执行计划 {i}")
                continue
            ctx = self._build_ctx(ctx_dict, res.columns)
            table = ResultTable(self, self.client, ctx_dict)
            table.set_result(res.columns, res.rows, ctx)
            title = f"结果 {i} · {res.message}"
            if res.truncated:
                title += "（已截断）"
            self.result_tabs.addTab(table, title)

            if res.page:
                self.page = res.page.page
                if res.page.total is not None:
                    total_pages = max(
                        1,
                        (res.page.total + res.page.page_size - 1) // res.page.page_size,
                    )
                    self.page_spin.setRange(1, total_pages)
                    self.page_spin.setValue(res.page.page)
                    self.page_label.setText(f"第 {self.page} / {total_pages} 页")
                else:
                    self.page_spin.setRange(1, 1_000_000)
                    self.page_spin.setValue(res.page.page)
                    self.page_label.setText(f"第 {self.page} 页")

    def _add_dml_summary_tab(self, results: list) -> None:
        total_affected = sum(r.rowcount for r in results)
        failed = [r for r in results if not r.success]
        has_rollback = any(r.rolled_back for r in results)
        lines = []
        for r in results:
            mark = "✓" if r.success else "✗"
            lines.append(f"{mark}  {r.message}")
        summary = "\n".join(lines)
        table = ResultTable(self, self.client, None)
        table.set_result(
            ["序号", "状态", "影响行数", "耗时(ms)", "说明"],
            [
                (
                    i,
                    "成功" if r.success else "失败",
                    r.rowcount,
                    f"{r.elapsed * 1000:.1f}",
                    r.message,
                )
                for i, r in enumerate(results, 1)
            ],
            None,
        )
        title = f"DML 汇总 · {len(results)} 条 · 影响 {total_affected} 行"
        if failed:
            title += f" · {len(failed)} 条失败"
        if has_rollback:
            title += " · 已回滚"
        self.result_tabs.addTab(table, title)
        self.result_tabs.setCurrentIndex(self.result_tabs.count() - 1)
        table.view.setToolTip(summary)

    def _on_failed(self, message: str) -> None:
        self._running = False
        self._worker = None
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_nav_enabled(True)
        self._cleanup_pending_tables()
        self._show_status("执行失败")
        QMessageBox.critical(self, "SQL 执行失败", message)
        if self._pending_close_tx:
            self._pending_close_tx = False
            self._close_tx_conn()

    def _on_cancelled(self) -> None:
        self._running = False
        self._worker = None
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self._set_nav_enabled(True)
        self._cleanup_pending_tables()
        self._show_status("已取消")
        if self._pending_close_tx:
            self._pending_close_tx = False
            self._close_tx_conn()

    def _show_status(self, text: str) -> None:
        self.status_label.setText(text)
        for i in range(self.result_tabs.count()):
            w = self.result_tabs.widget(i)
            if isinstance(w, ResultTable):
                w.info.setText(text)

    def _set_nav_enabled(self, enabled: bool) -> None:
        for b in (
            self.first_btn, self.prev_btn, self.next_btn, self.last_btn,
            self.page_spin,
        ):
            b.setEnabled(enabled)