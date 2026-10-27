"""主窗口：对象树 + 查询页签 + 片段面板 + 全局搜索 + 主题切换 + 对象过滤
+ 连接颜色标记 + 页签恢复 / 撤销关闭 + 会话 / 变量面板 + 从本机导入数据
+ 命令面板（Ctrl+P / Ctrl+Shift+P）+ ER 图 / 表结构对比 / 多窗口。

启动流程（避免打包后启动卡顿）：
1. 主窗口立即 show()，QTimer 延迟 200ms 才做首启弹窗 / 页签恢复；
2. 恢复页签前先后台「预热连接」（SSH 隧道握手 / 连接池建立都在后台线程）；
3. 每个页签创建后让出一次事件循环，UI 保持可交互；
4. 补全词（库名 / 表名）首次异步加载，不阻塞页签创建。
"""
from __future__ import annotations

import logging
import re

from PySide6.QtCore import (
    QObject, QRunnable, QThreadPool, QTimer, Qt, QUrl, Signal,
)
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QDialog, QDockWidget, QFileDialog, QHBoxLayout, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPushButton, QSizePolicy, QStatusBar,
    QTabWidget, QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import config as _config
from . import io_tools, schema_ddl
from .about_dialog import AboutDialog
from .config import (
    APP_TITLE, APP_VERSION, DATA_DIR, LOG_FILE, DEFAULT_WINDOW_SIZE,
    app_icon, settings,
)
from .connection import ConnectionStore
from .connection_type_dialog import DatabaseTypeDialog
from .dialogs import (
    ConnectionDialog, CreateDatabaseDialog, CreateTableDialog,
    AlterTableDialog, IndexManagerDialog, ImportWizardDialog,
    ImportDataDialog, CreateTablePreviewDialog,
    color_dot_icon,
)
from .engine import DatabaseClient
from .settings_dialog import SettingsDialog
from .sql_util import qualify_table, quote_ident, sql_literal
from .theme import Theme, theme_manager
from .widgets.command_palette import CommandPalette, PaletteItem
from .widgets.object_browser import (
    DOT_OFFLINE, DOT_ONLINE, ObjectBrowserPanel,
    make_dot_icon, make_object_icon,
)
from .widgets.query_tab import QueryTab
from .widgets.result_view import ResultTable
from .widgets.session_panel import SessionPanel
from .widgets.snippets_panel import SnippetsPanel
from .widgets.sql_editor import KEYWORDS

log = logging.getLogger(__name__)

PLACEHOLDER = "加载中…"

# 数据库下的对象分组（顺序即渲染顺序）
_SECTION_DEFS: tuple[tuple[str, str, str], ...] = (
    ("表", "table", "table"),
    ("视图", "view", "view"),
    ("存储过程", "procedure", "procedure"),
    ("函数", "function", "function"),
    ("触发器", "trigger", "trigger"),
    ("事件", "event", "event"),
)

_MAX_UNDO_TABS = 10
_MAX_COMPLETION_CACHE = 64

_COLUMN_LOADER_POOL: QThreadPool | None = None
_SEARCH_POOL: QThreadPool | None = None


def _column_loader_pool() -> QThreadPool:
    global _COLUMN_LOADER_POOL
    if _COLUMN_LOADER_POOL is None:
        _COLUMN_LOADER_POOL = QThreadPool()
        _COLUMN_LOADER_POOL.setMaxThreadCount(1)
    return _COLUMN_LOADER_POOL


def _search_pool() -> QThreadPool:
    global _SEARCH_POOL
    if _SEARCH_POOL is None:
        _SEARCH_POOL = QThreadPool()
        _SEARCH_POOL.setMaxThreadCount(1)
    return _SEARCH_POOL


class _ClientWarmup(QRunnable):
    """后台预建 DatabaseClient。

    `DatabaseClient.__init__` 在配置了 SSH 时会同步完成隧道握手，可能
    阻塞数十秒。启动期在主线程直接调用会让窗口看起来卡死，所以拆到
    后台线程，完成后回主线程注册到 `clients` 字典。
    """

    class _Signals(QObject):
        done = Signal(str, object)   # conn_name, DatabaseClient
        failed = Signal(str, str)    # conn_name, message

    def __init__(self, store: ConnectionStore, conn_name: str):
        super().__init__()
        self.store = store
        self.conn_name = conn_name
        self.signals = _ClientWarmup._Signals()
        self.setAutoDelete(True)

    def run(self) -> None:
        try:
            cfg = self.store.get(self.conn_name)
            if cfg is None:
                self.signals.failed.emit(self.conn_name, "连接不存在")
                return
            client = DatabaseClient(cfg)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(self.conn_name, str(exc))
            return
        self.signals.done.emit(self.conn_name, client)


class _CompletionWordsLoader(QRunnable):
    """后台加载某连接/库的补全词（库名 + 表名）。

    列表可能在几百张表上下，反射会真正打数据库；放在页签创建路径上会
    让新页签打开时卡顿。改为后台加载，主线程先用关键字顶上。
    """

    class _Signals(QObject):
        done = Signal(list)

    def __init__(self, client: DatabaseClient, db: str | None):
        super().__init__()
        self.client = client
        self.db = db
        self.signals = _CompletionWordsLoader._Signals()
        self.setAutoDelete(True)

    def run(self) -> None:
        words: list[str] = []
        try:
            words.extend(list(self.client.databases()))
            if self.db:
                for tbl, _kind in self.client.tables(schema=self.db):
                    words.append(tbl)
        except Exception as exc:  # noqa: BLE001
            log.debug("补全词后台加载失败：%s", exc)
        self.signals.done.emit(words)


class _ColumnCompletionLoader(QRunnable):
    """后台加载某库的全部列名，返回 {表名: [列名]} 映射。"""

    class _Signals(QObject):
        done = Signal(dict)

    def __init__(self, client: DatabaseClient, db: str):
        super().__init__()
        self.client = client
        self.db = db
        self.signals = _ColumnCompletionLoader._Signals()

    def run(self) -> None:
        mapping: dict[str, list[str]] = {}
        try:
            tables = self.client.tables(schema=self.db)
        except Exception:  # noqa: BLE001
            self.signals.done.emit(mapping)
            return
        if len(tables) > 200:
            self.signals.done.emit(mapping)
            return
        for tbl, _kind in tables:
            try:
                mapping[tbl] = [
                    c["name"] for c in self.client.columns(tbl, schema=self.db)
                ]
            except Exception:  # noqa: BLE001
                continue
        self.signals.done.emit(mapping)


class _SearchWorker(QRunnable):
    """在当前连接的所有库中搜索表名 / 列名。"""

    MAX_DBS = 64
    MAX_TABLES_PER_DB = 400
    MAX_HITS = 2000

    class _Signals(QObject):
        done = Signal(list)
        failed = Signal(str)

    def __init__(self, client: DatabaseClient, keyword: str):
        super().__init__()
        self.client = client
        self.keyword = keyword.lower()
        self.signals = _SearchWorker._Signals()

    def run(self) -> None:
        hits: list[tuple[str, str, str, str]] = []
        try:
            dbs = list(self.client.databases())[: self.MAX_DBS]
            for db in dbs:
                if len(hits) >= self.MAX_HITS:
                    break
                try:
                    tables = self.client.tables(schema=db)
                except Exception:  # noqa: BLE001
                    continue
                table_names = [t for t, kind in tables if kind == "table"]
                if not table_names:
                    continue

                if len(table_names) > self.MAX_TABLES_PER_DB:
                    for t in table_names:
                        if self.keyword in t.lower():
                            hits.append((db, t, "table", t))
                            if len(hits) >= self.MAX_HITS:
                                break
                    continue

                for t in table_names:
                    if self.keyword in t.lower():
                        hits.append((db, t, "table", t))
                        if len(hits) >= self.MAX_HITS:
                            break

                col_map = self.client.multi_columns(table_names, schema=db)
                for t, cols in col_map.items():
                    if len(hits) >= self.MAX_HITS:
                        break
                    for c in cols:
                        name = c.get("name", "")
                        if name and self.keyword in name.lower():
                            hits.append((db, t, "column", name))
                            if len(hits) >= self.MAX_HITS:
                                break
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(str(exc))
            return
        self.signals.done.emit(hits)


class MainWindow(QMainWindow):
    def __init__(self, *, restore_session: bool = True):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(*DEFAULT_WINDOW_SIZE)
        icon = app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.store = ConnectionStore()
        self.clients: dict[str, DatabaseClient] = {}
        self._completion_cache: dict[tuple, list[str]] = {}
        self._closed_tabs: list[dict] = []
        self._extra_windows: list["MainWindow"] = []
        self._er_windows: list = []
        self._pending_close_tabs: list[QueryTab] = []
        self._pending_dispose: dict[str, DatabaseClient] = {}

        # ---- 启动恢复状态（分批恢复 / 后台预热）----
        self._restore_queue: list[dict] = []
        self._restore_restored = 0
        self._restore_skipped = 0
        self._warmup_pending: set[str] = set()
        self._warmup_ok = 0
        self._warmup_failed = 0

        self._build_ui()
        self._build_actions()
        self._restore_state()
        self._reload_tree()
        self.statusBar().showMessage(
            f"{APP_TITLE} v{APP_VERSION} · {len(self.store.connections)} 个连接"
            "　（展开连接节点或右键「打开连接」时才会建立数据库连接）"
        )
        # 延迟到窗口绘制完再处理首启弹窗 / 页签恢复，
        # 避免打包后启动时被同步 I/O（keyring / SSH / 元数据反射）拖住。
        if restore_session:
            QTimer.singleShot(200, self._post_startup)
        else:
            QTimer.singleShot(200, self._post_startup_no_restore)

    # ================= UI =================
    def _build_ui(self) -> None:
        # ---- 对象浏览器面板（含工具栏 + 搜索栏 + 树）----
        self.object_browser = ObjectBrowserPanel(self)
        self.tree = self.object_browser.tree
        self.tree_filter = self.object_browser.search_edit

        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.tree.itemExpanded.connect(self._on_expanded)
        self.tree.itemClicked.connect(self._on_item_clicked)

        # ---- Dock：必须先创建，close_btn 才能连到它的 hide ----
        self.tree_dock = QDockWidget("对象浏览器", self)
        self.tree_dock.setObjectName("tree_dock")
        self.tree_dock.setWidget(self.object_browser)
        self.tree_dock.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.tree_dock)

        # 面板信号 → 主窗口槽（此时 tree_dock 已存在）
        self.object_browser.filterChanged.connect(self._apply_tree_filter)
        self.object_browser.importRequested.connect(self.import_data_from_file)
        self.object_browser.exportRequested.connect(self._quick_export)
        self.object_browser.refreshRequested.connect(self._reload_tree)
        self.object_browser.newConnectionRequested.connect(self.new_connection)
        # 面板内部已把 close_btn 连到自身的 setVisible(False)；
        # 这里再补一个「隐藏整个 Dock」的动作，两个都会触发。
        self.object_browser.close_btn.clicked.connect(self.tree_dock.hide)

        # ---- 查询片段面板 ----
        self.snippets_panel = SnippetsPanel(self)
        self.snippets_panel.insertRequested.connect(self._insert_snippet)
        self.snippets_panel.set_sql_provider(self._current_sql_provider)

        self.snippets_dock = QDockWidget("查询片段", self)
        self.snippets_dock.setObjectName("snippets_dock")
        self.snippets_dock.setWidget(self.snippets_panel)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.snippets_dock)
        self.tabifyDockWidget(self.tree_dock, self.snippets_dock)
        self.tree_dock.raise_()

        # ---- 全局搜索 ----
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("🔍 搜索表名 / 列名（在当前连接内）")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.returnPressed.connect(self._do_search)

        self.search_btn = QPushButton("搜索")
        self.search_btn.clicked.connect(self._do_search)

        self.search_result = QTreeWidget()
        self.search_result.setHeaderLabels(["类型", "库", "表", "名称"])
        self.search_result.setColumnWidth(0, 60)
        self.search_result.setColumnWidth(1, 140)
        self.search_result.setColumnWidth(2, 160)
        self.search_result.itemDoubleClicked.connect(self._on_search_double_clicked)

        search_panel = QWidget()
        sp_layout = QVBoxLayout(search_panel)
        sp_layout.setContentsMargins(8, 8, 8, 8)
        sp_layout.setSpacing(6)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(self.search_edit, 1)
        row.addWidget(self.search_btn)
        sp_layout.addLayout(row)
        sp_layout.addWidget(self.search_result, 1)

        self.search_dock = QDockWidget("全局搜索", self)
        self.search_dock.setObjectName("search_dock")
        self.search_dock.setWidget(search_panel)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.search_dock)
        self.search_dock.hide()

        # ---- 会话面板 ----
        self.session_panel = SessionPanel(self)
        self.session_panel.set_providers(
            connections=lambda: [c.name for c in self.store.connections],
            db_type=lambda name: (
                self.store.get(name).db_type
                if self.store.get(name) is not None else "mysql"
            ),
            client_getter=lambda name: self.clients.get(name),
        )
        self.session_dock = QDockWidget("会话 / 变量", self)
        self.session_dock.setObjectName("session_dock")
        self.session_dock.setWidget(self.session_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self.session_dock)
        self.session_dock.hide()

        # ---- 中央页签 ----
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.setElideMode(Qt.ElideRight)
        self.tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.tabs.setMinimumWidth(0)
        self.tabs.tabCloseRequested.connect(self._close_tab)
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar())

        self.command_palette = CommandPalette(self)

    def _build_actions(self) -> None:
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        self.act_new_conn = QAction("＋  新建连接", self)
        self.act_new_conn.triggered.connect(self.new_connection)
        self.act_new_db = QAction("＋  新建数据库", self)
        self.act_new_db.triggered.connect(self.create_database)
        self.act_new_table = QAction("＋  新建表", self)
        self.act_new_table.triggered.connect(self.new_table)
        self.act_new_query = QAction("＋  新建查询", self)
        self.act_new_query.setShortcut(QKeySequence("Ctrl+T"))
        self.act_new_query.triggered.connect(lambda: self.new_query_tab())
        self.act_run = QAction("▶  执行", self)
        self.act_run.setShortcut(QKeySequence("Ctrl+R"))
        self.act_run.triggered.connect(self.run_current)
        self.act_refresh = QAction("↻  刷新", self)
        self.act_refresh.setShortcut(QKeySequence("F5"))
        self.act_refresh.triggered.connect(self._reload_tree)

        self.act_import_data = QAction("📥  导入数据", self)
        self.act_import_data.setShortcut(QKeySequence("Ctrl+I"))
        self.act_import_data.setToolTip(
            "从本机 CSV / JSON / Excel 文件导入数据到指定表（Ctrl+I）"
        )
        self.act_import_data.triggered.connect(self.import_data_from_file)

        for act in (
            self.act_new_conn, self.act_new_db, self.act_new_table,
            self.act_new_query, self.act_import_data,
        ):
            toolbar.addAction(act)
        toolbar.addSeparator()
        for act in (self.act_run, self.act_refresh):
            toolbar.addAction(act)

        menubar = self.menuBar()

        view_menu = menubar.addMenu("视图(&V)")
        toggle_tree = self.tree_dock.toggleViewAction()
        toggle_tree.setText("对象浏览器")
        view_menu.addAction(toggle_tree)
        toggle_snip = self.snippets_dock.toggleViewAction()
        toggle_snip.setText("查询片段")
        view_menu.addAction(toggle_snip)
        toggle_search = self.search_dock.toggleViewAction()
        toggle_search.setText("全局搜索")
        view_menu.addAction(toggle_search)
        toggle_session = self.session_dock.toggleViewAction()
        toggle_session.setText("会话 / 变量")
        view_menu.addAction(toggle_session)
        view_menu.addSeparator()

        self.act_goto_palette = QAction("跳转到对象…", self)
        self.act_goto_palette.setShortcut(QKeySequence("Ctrl+P"))
        self.act_goto_palette.setToolTip(
            "搜索并跳转到表 / 视图 / 页签 / 连接 / 数据库（Ctrl+P）"
        )
        self.act_goto_palette.triggered.connect(self.show_goto_palette)
        view_menu.addAction(self.act_goto_palette)

        self.act_command_palette = QAction("命令面板…", self)
        self.act_command_palette.setShortcut(QKeySequence("Ctrl+Shift+P"))
        self.act_command_palette.setToolTip(
            "搜索并执行应用命令（Ctrl+Shift+P）"
        )
        self.act_command_palette.triggered.connect(self.show_command_palette)
        view_menu.addAction(self.act_command_palette)
        view_menu.addSeparator()

        theme_menu = view_menu.addMenu("主题")
        self._theme_group = QActionGroup(self)
        self._theme_group.setExclusive(True)
        act_light = QAction("浅色", self, checkable=True)
        act_dark = QAction("深色", self, checkable=True)
        current = theme_manager.theme
        act_light.setChecked(current == Theme.LIGHT)
        act_dark.setChecked(current == Theme.DARK)
        act_light.triggered.connect(lambda: self._switch_theme(Theme.LIGHT))
        act_dark.triggered.connect(lambda: self._switch_theme(Theme.DARK))
        self._theme_group.addAction(act_light)
        self._theme_group.addAction(act_dark)
        theme_menu.addAction(act_light)
        theme_menu.addAction(act_dark)

        file_menu = menubar.addMenu("文件(&F)")
        file_menu.addAction(self.act_new_conn)
        file_menu.addAction(self.act_new_db)
        file_menu.addAction(self.act_new_table)
        file_menu.addSeparator()
        file_menu.addAction(self.act_import_data)
        file_menu.addSeparator()

        new_window_act = QAction("新建窗口", self)
        new_window_act.setShortcut(QKeySequence("Ctrl+Shift+N"))
        new_window_act.setToolTip("打开一个新的主窗口（可并排对比）")
        new_window_act.triggered.connect(self.new_window)
        file_menu.addAction(new_window_act)
        file_menu.addSeparator()

        settings_act = QAction("设置…", self)
        settings_act.setShortcut(QKeySequence("Ctrl+,"))
        settings_act.triggered.connect(self.open_settings)
        file_menu.addAction(settings_act)
        file_menu.addSeparator()
        quit_act = QAction("退出", self)
        quit_act.setShortcut(QKeySequence.Quit)
        quit_act.triggered.connect(self.close)
        file_menu.addAction(quit_act)

        query_menu = menubar.addMenu("查询(&Q)")
        query_menu.addAction(self.act_new_query)
        query_menu.addAction(self.act_run)
        query_menu.addSeparator()
        self.act_undo_close = QAction("撤销关闭页签", self)
        self.act_undo_close.setShortcut(QKeySequence("Ctrl+Shift+T"))
        self.act_undo_close.triggered.connect(self._undo_close_tab)
        query_menu.addAction(self.act_undo_close)
        self.act_restore_session = QAction("恢复上次的页签…", self)
        self.act_restore_session.triggered.connect(
            lambda: self._restore_open_tabs(force=True)
        )
        query_menu.addAction(self.act_restore_session)
        query_menu.addSeparator()
        save_snip = QAction("把当前 SQL 保存为片段…", self)
        save_snip.triggered.connect(lambda: self.snippets_panel._add_from_current())
        query_menu.addAction(save_snip)

        tools_menu = menubar.addMenu("工具(&T)")

        er_act = QAction("查看 ER 图…", self)
        er_act.setShortcut(QKeySequence("Ctrl+Shift+E"))
        er_act.setToolTip("把当前库的表与外键关系画成图形")
        er_act.triggered.connect(self.show_er_diagram)
        tools_menu.addAction(er_act)

        diff_act = QAction("表结构对比…", self)
        diff_act.setShortcut(QKeySequence("Ctrl+Shift+M"))
        diff_act.setToolTip("比较两个库的表/列差异，生成迁移 SQL")
        diff_act.triggered.connect(self.show_schema_diff)
        tools_menu.addAction(diff_act)

        tools_menu.addSeparator()
        tools_menu.addAction(self.act_import_data)

        help_menu = menubar.addMenu("帮助(&H)")
        about_act = QAction("关于 PyNavicat", self)
        about_act.triggered.connect(self.show_about)
        help_menu.addAction(about_act)
        help_menu.addSeparator()
        open_data_act = QAction("打开数据目录", self)
        open_data_act.triggered.connect(lambda: self._open_local_path(DATA_DIR))
        help_menu.addAction(open_data_act)
        open_log_act = QAction("打开日志目录", self)
        open_log_act.triggered.connect(lambda: self._open_local_path(LOG_FILE.parent))
        help_menu.addAction(open_log_act)
        help_menu.addSeparator()
        about_qt_act = QAction("关于 Qt", self)
        about_qt_act.triggered.connect(lambda: QMessageBox.aboutQt(self, "关于 Qt"))
        help_menu.addAction(about_qt_act)

    def _quick_export(self) -> None:
        """从对象浏览器工具条触发的快捷导出：直接转发到当前结果表的导出。"""
        w = self.tabs.currentWidget()
        if not isinstance(w, QueryTab):
            QMessageBox.information(self, "提示", "请先打开一个查询页签。")
            return
        rt = w.result_tabs.currentWidget()
        if not isinstance(rt, ResultTable):
            QMessageBox.information(self, "提示", "当前页签没有结果表可导出。")
            return
        rt._show_export_menu()

    def _switch_theme(self, theme: Theme) -> None:
        app = QApplication.instance()
        if app is None:
            return
        theme_manager.apply(app, theme)
        settings().setValue("ui/theme", theme.value)
        label = "深色" if theme == Theme.DARK else "浅色"
        self.statusBar().showMessage(f"已切换到{label}主题")

    def show_about(self) -> None:
        dlg = AboutDialog(self)
        dlg.exec()

    @staticmethod
    def _open_local_path(path) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def open_settings(self) -> None:
        old_theme = theme_manager.theme.value
        old_font = int(settings().value("ui/font_size", 11))
        dlg = SettingsDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        s = settings()
        new_theme = str(s.value("ui/theme", "light"))
        if new_theme != old_theme:
            theme = Theme.DARK if new_theme == "dark" else Theme.LIGHT
            app = QApplication.instance()
            if app is not None:
                theme_manager.apply(app, theme)

        new_font = int(s.value("ui/font_size", 11))
        if new_font != old_font:
            self._apply_editor_font(new_font)

        mask = bool(s.value("query/mask_sensitive", True, type=bool))
        self._apply_mask_setting(mask)

        self.statusBar().showMessage("设置已保存")

    def _apply_editor_font(self, size: int) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, QueryTab):
                try:
                    w.editor.set_font_size(size)
                except Exception:  # noqa: BLE001
                    pass

    def _apply_mask_setting(self, enabled: bool) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not isinstance(w, QueryTab):
                continue
            for j in range(w.result_tabs.count()):
                rt = w.result_tabs.widget(j)
                if isinstance(rt, ResultTable):
                    rt.mask_box.setChecked(enabled)
                    rt.model.set_mask_enabled(enabled)

    def _restore_state(self) -> None:
        s = settings()
        try:
            geo = s.value("window/geometry")
            if geo:
                self.restoreGeometry(geo)
            st = s.value("window/state")
            if st:
                self.restoreState(st)
        except Exception as exc:  # noqa: BLE001
            log.warning("恢复窗口状态失败：%s", exc)

    def _save_state(self) -> None:
        s = settings()
        try:
            s.setValue("window/geometry", self.saveGeometry())
            s.setValue("window/state", self.saveState())
        except Exception as exc:  # noqa: BLE001
            log.warning("保存窗口状态失败：%s", exc)

    # ================= 启动后处理 =================
    def _post_startup(self) -> None:
        if not self.store.connections:
            # 首启弹窗单独延迟，让主窗口先完整绘制出来，
            # 避免用户看到"只有一片空白 / 卡住"。
            self.statusBar().showMessage("尚未配置任何连接，稍后将打开「新建连接」…")
            QTimer.singleShot(300, self._open_first_run_dialog)
            return

        self._restore_open_tabs()

    def _open_first_run_dialog(self) -> None:
        if self.store.connections:
            return
        self.new_connection()
        if not self.store.connections:
            self.statusBar().showMessage(
                "还没有数据库连接；点击工具栏「＋ 新建连接」开始使用"
            )

    def _post_startup_no_restore(self) -> None:
        """新窗口启动：不恢复上次页签，避免和已有窗口的内容重复。"""
        if not self.store.connections:
            self.statusBar().showMessage(
                "还没有数据库连接；点击工具栏「＋ 新建连接」开始使用"
            )

    # ================= 页签持久化 =================
    def _snapshot_tab(self, tab: QueryTab, title: str) -> dict | None:
        try:
            sql = tab.editor.toPlainText()
            table_key = getattr(tab, "table_key", None)
            if table_key is not None:
                try:
                    table_key = list(table_key)
                except TypeError:
                    table_key = None
            return {
                "conn": tab.client.cfg.name,
                "db": tab.schema,
                "sql": sql,
                "title": title,
                "paged": bool(tab.paged),
                "table_key": table_key,
            }
        except Exception as exc:  # noqa: BLE001
            log.debug("快照页签失败：%s", exc)
            return None

    def _save_open_tabs(self) -> None:
        snapshots: list[dict] = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not isinstance(w, QueryTab):
                continue
            snap = self._snapshot_tab(w, self.tabs.tabText(i))
            if snap is not None:
                snapshots.append(snap)
        _config.save_open_tabs(snapshots)

    def _restore_open_tabs(self, force: bool = False) -> None:
        data = _config.load_open_tabs()
        if not data:
            if force:
                self.statusBar().showMessage("没有可恢复的页签")
            return
        if not force and self.tabs.count() > 0:
            return

        if force:
            dirty: list[str] = []
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if isinstance(w, QueryTab) and w.has_pending_edits():
                    dirty.append(self.tabs.tabText(i))
            if dirty:
                preview = "\n  • ".join(dirty[:8])
                more = f"\n  • ...等 {len(dirty)} 个页签" if len(dirty) > 8 else ""
                if QMessageBox.question(
                    self, "确认恢复",
                    f"以下页签有未保存的修改：\n\n  • {preview}{more}\n\n"
                    "恢复会丢弃它们，继续？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                ) != QMessageBox.Yes:
                    return
            for i in range(self.tabs.count() - 1, -1, -1):
                w = self.tabs.widget(i)
                if isinstance(w, QueryTab):
                    w.cleanup()
                self.tabs.removeTab(i)
                if w is not None:
                    w.deleteLater()

        self._restore_queue = list(data)
        self._restore_restored = 0
        self._restore_skipped = 0

        # 先找出需要预热的连接（未建立 client 且配置存在），
        # 放到后台线程建立，避免 SSH 隧道握手 / 连接池初始化阻塞主线程。
        needed: set[str] = set()
        for entry in self._restore_queue:
            conn = entry.get("conn")
            if not conn:
                continue
            if self.store.get(conn) is None:
                continue
            if conn in self.clients:
                continue
            needed.add(conn)

        if not needed:
            self.statusBar().showMessage(
                f"正在恢复 {len(self._restore_queue)} 个页签…"
            )
            QTimer.singleShot(0, self._restore_next)
            return

        self._warmup_pending = set(needed)
        self._warmup_ok = 0
        self._warmup_failed = 0
        self.statusBar().showMessage(
            f"正在预热 {len(needed)} 个连接…"
        )
        for conn_name in needed:
            self._start_warmup(conn_name)

    def _start_warmup(self, conn_name: str) -> None:
        worker = _ClientWarmup(self.store, conn_name)
        worker.signals.done.connect(self._on_warmup_done)
        worker.signals.failed.connect(self._on_warmup_failed)
        QThreadPool.globalInstance().start(worker)

    def _on_warmup_done(self, conn_name: str, client) -> None:
        if client is not None and conn_name not in self.clients:
            self.clients[conn_name] = client
        self._warmup_ok += 1
        self._warmup_pending.discard(conn_name)
        self._maybe_continue_restore()

    def _on_warmup_failed(self, conn_name: str, message: str) -> None:
        log.warning("预热连接 %s 失败：%s", conn_name, message)
        self._warmup_failed += 1
        self._warmup_pending.discard(conn_name)
        self._maybe_continue_restore()

    def _maybe_continue_restore(self) -> None:
        if self._warmup_pending:
            self.statusBar().showMessage(
                f"正在预热连接… 剩余 {len(self._warmup_pending)}"
            )
            return
        if self._warmup_failed:
            log.info(
                "预热完成：成功 %d，失败 %d；开始恢复页签",
                self._warmup_ok, self._warmup_failed,
            )
        self.statusBar().showMessage(
            f"正在恢复 {len(self._restore_queue)} 个页签…"
        )
        QTimer.singleShot(0, self._restore_next)

    def _restore_next(self) -> None:
        if not self._restore_queue:
            if self._restore_restored or self._restore_skipped:
                msg = f"已恢复 {self._restore_restored} 个页签"
                if self._restore_skipped:
                    msg += f"（{self._restore_skipped} 个因连接缺失被跳过）"
                self.statusBar().showMessage(msg)
            return

        entry = self._restore_queue.pop(0)
        try:
            if self._create_tab_from_snapshot(entry):
                self._restore_restored += 1
            else:
                self._restore_skipped += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("恢复页签失败：%s", exc)
            self._restore_skipped += 1

        if self._restore_queue:
            self.statusBar().showMessage(
                f"正在恢复页签… 剩余 {len(self._restore_queue)}"
            )
        # 让出一次事件循环，UI 可以处理点击 / 重绘 / 关闭
        QTimer.singleShot(0, self._restore_next)

    def _create_tab_from_snapshot(self, entry: dict) -> bool:
        conn_name = entry.get("conn")
        if not conn_name or self.store.get(conn_name) is None:
            return False
        db = entry.get("db") or None
        sql = entry.get("sql") or ""
        title = entry.get("title") or conn_name
        paged = bool(entry.get("paged"))
        table_key = entry.get("table_key")
        if isinstance(table_key, list):
            table_key = tuple(table_key)

        try:
            client = self._client(conn_name)
        except Exception as exc:  # noqa: BLE001
            log.warning("恢复页签时建立连接失败：%s", exc)
            return False

        table_ctx = None
        if paged and table_key and len(table_key) >= 4:
            try:
                table_ctx = client.table_context(table_key[3], schema=db)
            except Exception:
                table_ctx = None

        tab = QueryTab(
            client, schema=db, sql=sql, parent=self,
            paged=paged, table_ctx=table_ctx,
        )
        tab.table_key = table_key
        try:
            tab.editor.set_completion_words(self._completion_words(conn_name, db))
        except Exception:  # noqa: BLE001
            pass
        idx = self.tabs.addTab(tab, title)
        self.tabs.setCurrentIndex(idx)

        if db:
            try:
                loader = _ColumnCompletionLoader(client, db)
                loader.signals.done.connect(
                    lambda mapping, t=tab: self._on_columns_loaded(t, mapping)
                )
                _column_loader_pool().start(loader)
            except Exception as exc:  # noqa: BLE001
                log.debug("列名补全后台加载失败：%s", exc)
        return True

    # ================= 撤销关闭 =================
    def _push_closed_tab(self, tab: QueryTab, title: str) -> None:
        snap = self._snapshot_tab(tab, title)
        if snap is None:
            return
        self._closed_tabs.append(snap)
        if len(self._closed_tabs) > _MAX_UNDO_TABS:
            self._closed_tabs.pop(0)

    def _undo_close_tab(self) -> None:
        if not self._closed_tabs:
            self.statusBar().showMessage("没有可恢复的已关闭页签")
            return
        entry = self._closed_tabs[-1]
        if not self._create_tab_from_snapshot(entry):
            QMessageBox.information(
                self, "提示",
                f"原连接「{entry.get('conn')}」不存在，无法恢复该页签。\n"
                "该页签已保留在撤销栈中，可稍后重试。",
            )
            return
        self._closed_tabs.pop()
        self.statusBar().showMessage("已恢复页签")

    # ================= 片段 =================
    def _current_sql_provider(self) -> tuple[str, str, str]:
        w = self.tabs.currentWidget()
        if not isinstance(w, QueryTab):
            return "", "", ""
        try:
            sql = w.editor.sql_to_run()
        except Exception:  # noqa: BLE001
            sql = ""
        return sql, w.client.cfg.name, w.schema or ""

    def _insert_snippet(self, sql: str) -> None:
        w = self.tabs.currentWidget()
        if not isinstance(w, QueryTab):
            return
        cursor = w.editor.textCursor()
        cursor.insertText(sql)
        w.editor.setTextCursor(cursor)
        self.statusBar().showMessage("片段已插入")

    # ================= 多窗口 =================
    def new_window(self) -> None:
        w = MainWindow(restore_session=False)
        w.resize(self.size().width(), self.size().height())
        w.show()
        self._extra_windows.append(w)

    # ================= ER 图 =================
    def _current_db(self) -> str | None:
        item = self.tree.currentItem()
        while item is not None:
            payload = item.data(0, Qt.UserRole) or ()
            if payload and payload[0] in ("db", "table"):
                return payload[2]
            item = item.parent()
        return None

    def show_er_diagram(self) -> None:
        conn_name = self._current_connection()
        if not conn_name:
            QMessageBox.information(self, "提示", "请先选择一个连接")
            return
        db = self._current_db()
        if not db:
            QMessageBox.information(
                self, "提示", "请先在对象树中展开并选中一个数据库"
            )
            return
        try:
            client = self._client(conn_name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "连接失败", str(exc))
            return

        from .widgets.er_diagram import ErDiagramWidget
        w = ErDiagramWidget(client, schema=db, parent=None)
        w.setWindowTitle(f"ER 图 - {conn_name} / {db}")
        w.resize(1100, 760)
        w.setWindowIcon(self.windowIcon())
        w.show()
        w.load()
        self._er_windows.append(w)

    # ================= 表结构对比 =================
    def show_schema_diff(self) -> None:
        if not self.store.connections:
            QMessageBox.information(self, "提示", "请先新建一个数据库连接")
            return

        preset_a: tuple[str, str] | None = None
        conn_name = self._current_connection()
        if conn_name:
            db = self._current_db()
            if db:
                preset_a = (conn_name, db)

        def _db_lister(name: str) -> list[str]:
            try:
                client = self._client(name)
                return list(client.databases())
            except Exception as exc:  # noqa: BLE001
                log.warning("加载库列表失败：%s", exc)
                return []

        from .widgets.schema_diff_dialog import SchemaDiffDialog
        dlg = SchemaDiffDialog(
            self,
            connections=[c.name for c in self.store.connections],
            client_getter=self._client,
            db_lister=_db_lister,
            preset_a=preset_a,
            preset_b=None,
        )
        dlg.exec()

    # ================= 命令面板 =================
    def show_goto_palette(self) -> None:
        items = self._collect_palette_objects()
        if not items:
            self.statusBar().showMessage(
                "没有可跳转的对象；请先展开对象树或打开查询页签"
            )
        self.command_palette.open_with(
            items,
            placeholder="搜索表 / 视图 / 已打开页签 / 连接 / 数据库…",
            hint=f"共 {len(items)} 项 · Enter 跳转 · Esc 关闭",
        )

    def show_command_palette(self) -> None:
        items = self._collect_palette_commands()
        self.command_palette.open_with(
            items,
            placeholder="输入命令，例如：新建 / 刷新 / 主题 / 设置 / 导入…",
            initial=">",
            hint=f"共 {len(items)} 条命令 · Enter 执行 · Esc 关闭",
        )

    def _focus_tab_widget(self, widget) -> None:
        """切换到指定页签（基于 widget 引用，避免索引过期）。"""
        try:
            idx = self.tabs.indexOf(widget)
        except RuntimeError:
            return
        if idx >= 0:
            self.tabs.setCurrentIndex(idx)

    @staticmethod
    def _walk_tree(item: QTreeWidgetItem):
        try:
            yield item
            for i in range(item.childCount()):
                yield from MainWindow._walk_tree(item.child(i))
        except RuntimeError:
            return

    def _collect_palette_objects(self) -> list[PaletteItem]:
        out: list[PaletteItem] = []

        # ---- 已打开的查询页签 ----
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not isinstance(w, QueryTab):
                continue
            title = self.tabs.tabText(i)
            bits = ["页签", w.client.cfg.name]
            if w.schema:
                bits.append(w.schema)
            out.append(PaletteItem(
                label=title,
                detail=" · ".join(bits),
                kind="tab",
                payload=("tab", i),
                action=lambda widget=w: self._focus_tab_widget(widget),
            ))

        # ---- 对象树里已加载的节点 ----
        try:
            top_count = self.tree.topLevelItemCount()
        except RuntimeError:
            top_count = 0
        for i in range(top_count):
            try:
                top = self.tree.topLevelItem(i)
            except RuntimeError:
                break
            if top is None:
                continue
            for node in self._walk_tree(top):
                payload = node.data(0, Qt.UserRole) or ()
                if not payload:
                    continue
                kind = payload[0]
                if kind == "table":
                    conn_name = payload[1]
                    db = payload[2]
                    tbl = payload[3]
                    obj_kind = payload[4] if len(payload) > 4 else "table"
                    if obj_kind not in ("table", "view"):
                        continue
                    label = f"{db}.{tbl}" if db else tbl
                    out.append(PaletteItem(
                        label=label,
                        detail=f"{conn_name} · "
                               f"{'视图' if obj_kind == 'view' else '表'}",
                        kind=obj_kind,
                        payload=payload,
                        action=lambda p=payload: self._open_table(p, reuse=True),
                    ))
                elif kind == "conn":
                    name = payload[1]
                    out.append(PaletteItem(
                        label=f"连接 · {name}",
                        detail="连接",
                        kind="conn",
                        payload=payload,
                        action=lambda n=name: self.new_query_tab(n),
                    ))
                elif kind == "db":
                    conn_name = payload[1]
                    db = payload[2]
                    out.append(PaletteItem(
                        label=f"{conn_name} / {db}",
                        detail="数据库",
                        kind="db",
                        payload=payload,
                        action=lambda c=conn_name, d=db: self.new_query_tab(c, d),
                    ))
        return out

    def _collect_palette_commands(self) -> list[PaletteItem]:
        cmds: list[PaletteItem] = []

        def add(
            label: str,
            action,
            shortcut: str = "",
            detail: str = "",
        ) -> None:
            cmds.append(PaletteItem(
                label=label, detail=detail, shortcut=shortcut,
                action=action, kind="command",
            ))

        add("新建连接", self.new_connection)
        add("新建数据库", self.create_database, detail="当前连接")
        add("新建表", self.new_table, detail="当前库")
        add("新建查询页签", lambda: self.new_query_tab(), "Ctrl+T")
        add("新建窗口", self.new_window, "Ctrl+Shift+N")
        add("从本机导入数据…", self.import_data_from_file, "Ctrl+I")
        add("设置…", self.open_settings, "Ctrl+,")
        add("刷新对象树", self._reload_tree, "F5")

        add("执行当前 SQL", self.run_current, "Ctrl+R")
        add("撤销关闭页签", self._undo_close_tab, "Ctrl+Shift+T")
        add("恢复上次的页签…", lambda: self._restore_open_tabs(force=True))
        add("把当前 SQL 保存为片段…",
            lambda: self.snippets_panel._add_from_current())

        add("查看 ER 图…", self.show_er_diagram, "Ctrl+Shift+E",
            detail="当前库")
        add("表结构对比…", self.show_schema_diff, "Ctrl+Shift+M")

        add("主题：浅色", lambda: self._switch_theme(Theme.LIGHT))
        add("主题：深色", lambda: self._switch_theme(Theme.DARK))
        add("显示 / 隐藏 对象浏览器",
            lambda: self.tree_dock.setVisible(not self.tree_dock.isVisible()))
        add("显示 / 隐藏 查询片段",
            lambda: self.snippets_dock.setVisible(not self.snippets_dock.isVisible()))
        add("显示 / 隐藏 全局搜索",
            lambda: self.search_dock.setVisible(not self.search_dock.isVisible()))
        add("显示 / 隐藏 会话 / 变量",
            lambda: self.session_dock.setVisible(not self.session_dock.isVisible()))

        add("打开数据目录", lambda: self._open_local_path(DATA_DIR))
        add("打开日志目录", lambda: self._open_local_path(LOG_FILE.parent))
        add("关于 PyNavicat", self.show_about)
        add("关于 Qt", lambda: QMessageBox.aboutQt(self, "关于 Qt"))

        # ---- 已打开页签的快捷切换 ----
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, QueryTab):
                add(
                    f"切换页签 → {self.tabs.tabText(i)}",
                    lambda widget=w: self._focus_tab_widget(widget),
                )

        # ---- 断开已有连接 ----
        for cfg in self.store.connections:
            add(
                f"断开连接 → {cfg.name}",
                lambda n=cfg.name: self.disconnect(n),
            )

        return cmds

    # ================= 连接管理 =================
    def _client(self, name: str) -> DatabaseClient:
        if name not in self.clients:
            cfg = self.store.get(name)
            if cfg is None:
                raise KeyError(f"连接 {name} 不存在")
            self.clients[name] = DatabaseClient(cfg)
        return self.clients[name]

    def new_connection(self) -> None:
        # 第一步：卡片式选择数据库类型
        type_dlg = DatabaseTypeDialog(self)
        if type_dlg.exec() != QDialog.Accepted:
            return
        db_type = type_dlg.selected_type()
        if not db_type:
            return

        # 第二步：填写连接参数（沿用原对话框，按类型预填默认值）
        dlg = ConnectionDialog(self, preset_type=db_type)
        if dlg.exec() != QDialog.Accepted:
            return
        cfg = dlg.result_config()
        if self.store.get(cfg.name):
            QMessageBox.warning(self, "重名", f"连接「{cfg.name}」已存在，请换一个名字。")
            return
        self.store.add(cfg)
        self._invalidate_cache()
        self._reload_tree()

    def edit_connection(self, name: str) -> None:
        cfg = self.store.get(name)
        if cfg is None:
            return
        dlg = ConnectionDialog(self, cfg)
        if dlg.exec() != QDialog.Accepted:
            return
        new_cfg = dlg.result_config()
        if new_cfg.name != name and self.store.get(new_cfg.name):
            QMessageBox.warning(self, "重名", f"连接「{new_cfg.name}」已存在。")
            return
        self.disconnect(name)
        self.store.update(name, new_cfg)
        self._close_tabs_for_connection(name)
        self._invalidate_cache()
        self._reload_tree()

    def delete_connection(self, name: str) -> None:
        if QMessageBox.question(
            self, "确认删除", f"删除连接「{name}」？此操作不可撤销。"
        ) != QMessageBox.Yes:
            return
        self.disconnect(name)
        self.store.remove(name)
        self._close_tabs_for_connection(name)
        self._invalidate_cache()
        self._reload_tree()

    def disconnect(self, name: str) -> None:
        """断开连接：先关闭所有引用该连接的页签，再释放客户端资源。"""
        self._close_tabs_for_connection(name)

        client = self.clients.pop(name, None)
        if client is not None:
            has_pending = any(
                isinstance(w, QueryTab) and w.client is client
                for w in self._pending_close_tabs
            )
            if has_pending:
                self._pending_dispose[name] = client
            else:
                try:
                    client.dispose()
                except Exception as exc:  # noqa: BLE001
                    log.warning("释放连接失败 [%s]：%s", name, exc)

        self._invalidate_cache()
        # 更新连接的状态圆点（灰）
        self._refresh_conn_dot(name)
        self.statusBar().showMessage(f"已断开连接：{name}")

    def _refresh_conn_dot(self, name: str) -> None:
        """更新某个连接节点的状态圆点颜色。"""
        online = name in self.clients
        color = DOT_ONLINE if online else DOT_OFFLINE
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for node in self._walk_tree(top):
                payload = node.data(0, Qt.UserRole) or ()
                if payload and payload[0] == "conn" and payload[1] == name:
                    node.setIcon(1, make_dot_icon(color))
                    return

    # ---------- DDL ----------
    def create_database(self, conn_name: str | None = None) -> None:
        conn_name = conn_name or self._current_connection()
        if not conn_name:
            QMessageBox.information(self, "提示", "请先新建一个数据库连接")
            return
        cfg = self.store.get(conn_name)
        if cfg is not None and cfg.readonly:
            QMessageBox.warning(self, "只读连接", "当前连接被标记为只读，无法创建数据库。")
            return
        try:
            client = self._client(conn_name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "连接失败", str(exc)); return
        db_type = cfg.db_type if cfg else "mysql"
        dlg = CreateDatabaseDialog(self, db_type=db_type)
        if dlg.exec() != QDialog.Accepted:
            return
        name, charset, collation = dlg.result_values()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            client.create_database(name, charset=charset, collation=collation)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "创建数据库失败", str(exc)); return
        finally:
            QApplication.restoreOverrideCursor()
        QMessageBox.information(self, "成功", f"数据库「{name}」已创建")
        self._invalidate_cache(); self._reload_tree()
        self.statusBar().showMessage(f"已创建数据库：{name}")

    def drop_database(self, conn_name: str, db: str) -> None:
        cfg = self.store.get(conn_name)
        if cfg is not None and cfg.readonly:
            QMessageBox.warning(self, "只读连接", "当前连接被标记为只读，无法删除数据库。")
            return
        db_type = cfg.db_type if cfg else "mysql"
        extra = ""
        if db_type in ("postgresql", "cockroachdb", "kingbase"):
            extra = "\n• 该库上的其它连接会被强制断开。"
        elif db_type == "mssql":
            extra = "\n• 数据库会被置为 SINGLE_USER 并断开所有占用连接。"
        elif db_type in ("mysql", "mariadb", "tidb", "oceanbase"):
            extra = "\n• 该库上的其它连接会被强制断开。"
        if QMessageBox.warning(
            self, "确认删除数据库",
            f"确定要删除数据库「{db}」吗？{extra}\n\n"
            "库内所有表和数据都将被永久删除，此操作不可撤销！",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            client = self._client(conn_name)
            client.drop_database(db)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "删除数据库失败", str(exc)); return
        finally:
            QApplication.restoreOverrideCursor()

        closed = self._close_tabs_for_db(conn_name, db)

        QMessageBox.information(self, "成功", f"数据库「{db}」已删除")
        self._invalidate_cache(); self._reload_tree()
        msg = f"已删除数据库：{db}"
        if closed:
            msg += f"（同时关闭 {closed} 个相关页签）"
        self.statusBar().showMessage(msg)

    def new_table(self, conn_name: str | None = None, db: str | None = None) -> None:
        conn_name = conn_name or self._current_connection()
        if not conn_name:
            QMessageBox.information(self, "提示", "请先选择一个数据库连接"); return
        cfg = self.store.get(conn_name)
        if cfg is not None and cfg.readonly:
            QMessageBox.warning(self, "只读连接", "当前连接被标记为只读，无法新建表。")
            return
        if not db:
            item = self.tree.currentItem()
            while item is not None:
                payload = item.data(0, Qt.UserRole) or ()
                if payload and payload[0] == "db":
                    db = payload[2]; break
                item = item.parent()
            if not db:
                QMessageBox.information(self, "提示", "请先展开并选中一个数据库"); return
        try:
            client = self._client(conn_name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "连接失败", str(exc)); return
        db_type = cfg.db_type if cfg else "mysql"
        dlg = CreateTableDialog(self, db_type=db_type, schema=db)
        if dlg.exec() != QDialog.Accepted:
            return
        sql = dlg.result_sql()
        if not sql:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            results = client.execute_many(sql, schema=db)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "创建表失败", str(exc)); return
        finally:
            QApplication.restoreOverrideCursor()
        failed = [r for r in results if not r.success]
        if failed:
            QMessageBox.critical(self, "创建表失败", failed[0].message); return
        QMessageBox.information(self, "成功", "表已创建")
        client.invalidate_metadata(); self._invalidate_cache()
        self._refresh_by_conn_db(conn_name, db)
        self.statusBar().showMessage(f"已创建表：{db}")

    def alter_table(self, payload) -> None:
        _, conn_name, db, table = payload[:4]
        cfg = self.store.get(conn_name)
        if cfg is not None and cfg.readonly:
            QMessageBox.warning(self, "只读连接", "当前连接被标记为只读，无法修改表结构。")
            return
        try:
            client = self._client(conn_name)
            cols = client.columns(table, schema=db)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "读取表结构失败", str(exc)); return
        if not cols:
            QMessageBox.information(self, "提示", "未获取到列信息"); return
        db_type = cfg.db_type if cfg else "mysql"
        dlg = AlterTableDialog(
            self, db_type=db_type, schema=db, table=table, current_columns=cols,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        sql = dlg.result_sql().strip()
        if not sql:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            results = client.execute_many(sql, schema=db)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "修改表失败", str(exc)); return
        finally:
            QApplication.restoreOverrideCursor()
        failed = [r for r in results if not r.success]
        if failed:
            QMessageBox.critical(self, "修改表失败", failed[0].message); return
        QMessageBox.information(self, "成功", "表结构已更新")
        client.invalidate_metadata(); self._invalidate_cache()
        self._refresh_by_conn_db(conn_name, db)
        self._reload_open_table_tabs(conn_name, db, table)

    def manage_indexes(self, payload) -> None:
        _, conn_name, db, table = payload[:4]
        cfg = self.store.get(conn_name)
        if cfg is not None and cfg.readonly:
            QMessageBox.warning(self, "只读连接", "当前连接被标记为只读，无法管理索引。")
            return
        try:
            client = self._client(conn_name)
            idxs = client.indexes(table, schema=db)
            cols = [c["name"] for c in client.columns(table, schema=db)]
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "读取索引失败", str(exc)); return
        db_type = cfg.db_type if cfg else "mysql"
        dlg = IndexManagerDialog(
            self, db_type=db_type, schema=db, table=table,
            indexes=idxs, columns=cols,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        stmts = dlg.result_sql()
        if not stmts:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            results = client.execute_many("\n".join(stmts), schema=db)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "执行失败", str(exc)); return
        finally:
            QApplication.restoreOverrideCursor()
        failed = [r for r in results if not r.success]
        if failed:
            QMessageBox.critical(self, "索引操作失败", failed[0].message); return
        QMessageBox.information(self, "成功", "索引已更新")
        client.invalidate_metadata(); self._invalidate_cache()
        self._refresh_by_conn_db(conn_name, db)
        self._reload_open_table_tabs(conn_name, db, table)

    def drop_table(self, conn_name: str, db: str | None, table: str,
                   kind: str = "table") -> None:
        cfg = self.store.get(conn_name)
        if cfg is not None and cfg.readonly:
            QMessageBox.warning(self, "只读连接", "当前连接被标记为只读，无法删除对象。")
            return
        label = {
            "view": "视图", "procedure": "存储过程",
            "function": "函数", "trigger": "触发器", "event": "事件",
        }.get(kind, "表")
        cascade = False
        already_confirmed = False
        if kind == "table":
            refs: list[dict] = []
            out_fks: list[dict] = []
            try:
                client = self._client(conn_name)
                refs = client.referencing_tables(table, schema=db)
                out_fks = client.foreign_keys(table, schema=db)
            except Exception:  # noqa: BLE001
                refs, out_fks = [], []
            if refs:
                can_cascade = cfg is not None and cfg.db_type in (
                    "postgresql", "cockroachdb", "kingbase",
                )
                ref_lines = "\n".join(
                    f"  • {r['table']}（{', '.join(r['columns'])} → "
                    f"{', '.join(r['referred_columns'])}）"
                    for r in refs
                )
                box = QMessageBox(self)
                box.setIcon(QMessageBox.Warning)
                box.setWindowTitle(f"确认删除{label}")
                box.setText(
                    f"表「{table}」被下列 {len(refs)} 张表通过外键引用：\n\n{ref_lines}\n\n"
                    "直接删除会因外键约束失败。"
                )
                if can_cascade:
                    box.setInformativeText(
                        "选择「级联删除」将生成 DROP TABLE ... CASCADE，"
                        "同时删除依赖该表的视图/外键。"
                    )
                    cascade_btn = box.addButton("级联删除", QMessageBox.DestructiveRole)
                else:
                    cascade_btn = None
                    box.setInformativeText(
                        "建议先删除引用本表的子表，或临时禁用外键检查。"
                    )
                del_btn = box.addButton("仍然删除", QMessageBox.AcceptRole)
                cancel_btn = box.addButton("取消", QMessageBox.RejectRole)
                box.setDefaultButton(cancel_btn)
                box.exec()
                clicked = box.clickedButton()
                if clicked is cancel_btn:
                    return
                if cascade_btn is not None and clicked is cascade_btn:
                    cascade = True
                elif clicked is del_btn:
                    pass
                else:
                    return
                already_confirmed = True
            elif out_fks:
                fk_lines = "\n".join(
                    f"  • {', '.join(fk['constrained_columns'])} → "
                    f"{fk['referred_table']}({', '.join(fk['referred_columns'])})"
                    for fk in out_fks
                )
                if QMessageBox.question(
                    self, f"确认删除{label}",
                    f"表「{table}」自身引用了下列外键：\n\n{fk_lines}\n\n"
                    "删除本表不会影响被引用的表，但本表的外键会被一并移除。"
                    "继续吗？",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                ) != QMessageBox.Yes:
                    return
                already_confirmed = True
        if not already_confirmed:
            if QMessageBox.warning(
                self, f"确认删除{label}",
                f"确定要删除{label}「{table}」吗？\n\n此操作不可撤销！",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            client = self._client(conn_name)
            client.drop_object(table, schema=db, kind=kind, cascade=cascade)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, f"删除{label}失败", str(exc)); return
        finally:
            QApplication.restoreOverrideCursor()

        closed = self._close_tabs_for_table(conn_name, db, table)

        QMessageBox.information(self, "成功", f"{label}「{table}」已删除")
        self._invalidate_cache()
        self._refresh_by_conn_db(conn_name, db)
        msg = f"已删除{label}：{table}"
        if closed:
            msg += f"（同时关闭 {closed} 个相关页签）"
        self.statusBar().showMessage(msg)

    def _invalidate_cache(self) -> None:
        self._completion_cache.clear()

    # ================= 表相关页签清理 / 刷新 =================
    def _close_tabs_for_table(self, conn_name: str, db: str | None,
                              table: str) -> int:
        key = self._table_key(conn_name, db, table)

        targets: list[int] = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, QueryTab) and getattr(w, "table_key", None) == key:
                targets.append(i)
        if not targets:
            return 0

        dirty: list[str] = []
        for i in targets:
            w = self.tabs.widget(i)
            if isinstance(w, QueryTab) and w.has_pending_edits():
                dirty.append(self.tabs.tabText(i))
        if dirty:
            preview = "\n  • ".join(dirty[:5])
            more = f"\n  • ...等 {len(dirty)} 个页签" if len(dirty) > 5 else ""
            if QMessageBox.question(
                self, "确认关闭",
                f"以下页签有未保存的修改：\n\n  • {preview}{more}\n\n"
                "关闭后修改将丢失，继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return 0

        closed = 0
        for i in reversed(targets):
            w = self.tabs.widget(i)
            if not isinstance(w, QueryTab):
                continue
            try:
                w.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self.tabs.removeTab(i)
            w.deleteLater()
            closed += 1
        return closed

    def _close_tabs_for_db(self, conn_name: str, db: str) -> int:
        targets: list[int] = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not isinstance(w, QueryTab):
                continue
            try:
                same_conn = w.client.cfg.name == conn_name
            except Exception:  # noqa: BLE001
                same_conn = False
            if not same_conn:
                continue
            if (w.schema or "") != (db or ""):
                continue
            targets.append(i)
        if not targets:
            return 0

        dirty: list[str] = []
        for i in targets:
            w = self.tabs.widget(i)
            if isinstance(w, QueryTab) and w.has_pending_edits():
                dirty.append(self.tabs.tabText(i))
        if dirty:
            preview = "\n  • ".join(dirty[:5])
            more = f"\n  • ...等 {len(dirty)} 个页签" if len(dirty) > 5 else ""
            if QMessageBox.question(
                self, "确认关闭",
                f"数据库「{db}」相关的以下页签有未保存的修改：\n\n"
                f"  • {preview}{more}\n\n"
                "关闭后修改将丢失，继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return 0

        closed = 0
        for i in reversed(targets):
            w = self.tabs.widget(i)
            if not isinstance(w, QueryTab):
                continue
            try:
                w.cleanup()
            except Exception:  # noqa: BLE001
                pass
            self.tabs.removeTab(i)
            w.deleteLater()
            closed += 1
        return closed

    def _reload_open_table_tabs(self, conn_name: str, db: str | None,
                                table: str) -> None:
        """让指向该表的已打开页签重新执行一次，拿到最新列定义与数据。"""
        key = self._table_key(conn_name, db, table)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not isinstance(w, QueryTab):
                continue
            if getattr(w, "table_key", None) != key:
                continue
            if w.is_running():
                continue
            try:
                w.table_ctx = self._client(conn_name).table_context(
                    table, schema=db
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("刷新表上下文失败 [%s.%s]：%s", db, table, exc)
            w.execute_sql()

    # ================= 对象树 =================
    def _collect_expanded_paths(self) -> set[tuple]:
        paths: set[tuple] = set()

        def walk(item: QTreeWidgetItem) -> None:
            try:
                payload = item.data(0, Qt.UserRole) or ()
                if item.isExpanded() and payload:
                    paths.add(tuple(payload))
            except RuntimeError:
                return
            for i in range(item.childCount()):
                walk(item.child(i))

        try:
            for i in range(self.tree.topLevelItemCount()):
                walk(self.tree.topLevelItem(i))
        except RuntimeError:
            pass
        return paths

    def _restore_expanded_paths(self, expanded: set[tuple]) -> None:
        if not expanded:
            return

        to_expand: list[QTreeWidgetItem] = []

        def walk(item: QTreeWidgetItem) -> None:
            try:
                payload = item.data(0, Qt.UserRole) or ()
                if tuple(payload) in expanded and not item.isExpanded():
                    to_expand.append(item)
            except RuntimeError:
                return
            for i in range(item.childCount()):
                walk(item.child(i))

        try:
            for i in range(self.tree.topLevelItemCount()):
                walk(self.tree.topLevelItem(i))
        except RuntimeError:
            pass

        for item in to_expand:
            try:
                item.setExpanded(True)
            except RuntimeError:
                continue

    def _reload_tree(self) -> None:
        filter_text = (
            self.tree_filter.text() if getattr(self, "tree_filter", None) else ""
        )
        expanded_paths = self._collect_expanded_paths()

        self.tree.clear()
        grouped = self.store.groups()
        group_nodes: list[QTreeWidgetItem] = []
        for group_name, cfgs in grouped.items():
            parent: QTreeWidgetItem | None = None
            if group_name != "未分组":
                parent = QTreeWidgetItem([group_name, ""])
                parent.setIcon(0, make_object_icon("folder"))
                parent.setData(0, Qt.UserRole, ("group", group_name))
                self.tree.addTopLevelItem(parent)
                group_nodes.append(parent)
            for cfg in cfgs:
                online = cfg.name in self.clients
                item = QTreeWidgetItem([cfg.name, ""])
                if cfg.color:
                    item.setIcon(0, color_dot_icon(cfg.color))
                else:
                    item.setIcon(0, make_object_icon("connection"))
                item.setData(0, Qt.UserRole, ("conn", cfg.name))

                dot_color = DOT_ONLINE if online else DOT_OFFLINE
                item.setIcon(1, make_dot_icon(dot_color))

                tip = cfg.display_name()
                if cfg.readonly:
                    tip += "\n只读连接，禁止执行写操作"
                if cfg.ssh_host:
                    if cfg.ssh_use_agent:
                        via = "ssh-agent"
                    elif cfg.ssh_key_file:
                        via = f"私钥 {cfg.ssh_key_file}"
                    else:
                        via = "密码"
                    tip += (
                        f"\n通过 SSH 隧道：{cfg.ssh_user}@{cfg.ssh_host}:"
                        f"{cfg.ssh_port}（认证：{via}）"
                    )
                item.setToolTip(0, tip)

                item.addChild(QTreeWidgetItem([PLACEHOLDER, ""]))
                if parent is None:
                    self.tree.addTopLevelItem(item)
                else:
                    parent.addChild(item)
        for node in group_nodes:
            node.setExpanded(True)

        self._restore_expanded_paths(expanded_paths)

        if filter_text and getattr(self, "tree_filter", None) is not None:
            self.tree_filter.blockSignals(True)
            self.tree_filter.setText(filter_text)
            self.tree_filter.blockSignals(False)
            self._apply_tree_filter(filter_text)

        self.statusBar().showMessage(f"共 {len(self.store.connections)} 个连接")
        if getattr(self, "session_panel", None) is not None:
            self.session_panel.refresh_connections()

    def _apply_tree_filter(self, text: str) -> None:
        """按搜索文本过滤树节点；支持正则（面板 .* 按钮切换）。"""
        text = (text or "").strip()
        if not text:
            self._set_subtree_visible(self.tree.invisibleRootItem(), True)
            return

        regex_mode = False
        if getattr(self, "object_browser", None) is not None:
            regex_mode = self.object_browser.regex_enabled()

        if regex_mode:
            try:
                pattern = re.compile(text, re.IGNORECASE)
            except re.error:
                return

            def matcher(s: str) -> bool:
                return bool(pattern.search(s))
        else:
            needle = text.lower()

            def matcher(s: str) -> bool:
                return needle in s.lower()

        for i in range(self.tree.topLevelItemCount()):
            self._filter_node(self.tree.topLevelItem(i), matcher)

    def _filter_node(self, item: QTreeWidgetItem, matcher) -> bool:
        own = matcher(item.text(0))
        any_child = False
        for j in range(item.childCount()):
            if self._filter_node(item.child(j), matcher):
                any_child = True
        visible = own or any_child
        item.setHidden(not visible)
        if any_child:
            item.setExpanded(True)
        return visible

    def _set_subtree_visible(self, item: QTreeWidgetItem, visible: bool) -> None:
        for j in range(item.childCount()):
            child = item.child(j)
            child.setHidden(not visible)
            self._set_subtree_visible(child, visible)

    @staticmethod
    def _is_placeholder(item: QTreeWidgetItem) -> bool:
        return item.childCount() == 1 and item.child(0).text(0) == PLACEHOLDER

    @staticmethod
    def _is_error_node(item: QTreeWidgetItem) -> bool:
        return item.childCount() == 1 and item.child(0).text(0).startswith("⚠")

    def _report_load_error(self, item: QTreeWidgetItem, exc: Exception) -> None:
        log.warning("加载对象树节点失败：%s", exc)
        message = str(exc) or exc.__class__.__name__
        err = QTreeWidgetItem([f"⚠ {message}", ""])
        err.setForeground(0, Qt.red)
        item.addChild(err)
        self.statusBar().showMessage(f"打开失败：{message}")
        QMessageBox.warning(
            self, "打开数据库失败",
            f"无法加载「{item.text(0)}」：\n\n{message}",
        )

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        payload = item.data(0, Qt.UserRole)
        if not payload or not (
            self._is_placeholder(item) or self._is_error_node(item)
        ):
            return
        item.takeChildren()
        kind = payload[0]
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if kind == "conn":
                self._fill_databases(item, payload[1])
            elif kind == "db":
                self._fill_tables(item, payload[1], payload[2])
            elif kind == "table":
                self._fill_columns(item, payload[1], payload[2], payload[3])
        except Exception as exc:  # noqa: BLE001
            self._report_load_error(item, exc)
        finally:
            QApplication.restoreOverrideCursor()

    def _fill_databases(self, item: QTreeWidgetItem, conn_name: str) -> None:
        client = self._client(conn_name)
        try:
            dbs = client.databases()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"连接「{conn_name}」失败：{exc}") from exc
        if not dbs:
            item.addChild(QTreeWidgetItem(["（无可见数据库）", ""]))
            item.setIcon(1, make_dot_icon(DOT_ONLINE))
            return
        for db in dbs:
            node = QTreeWidgetItem([db, ""])
            node.setIcon(0, make_object_icon("database"))
            node.setIcon(1, make_dot_icon(DOT_ONLINE))
            node.setData(0, Qt.UserRole, ("db", conn_name, db))
            node.addChild(QTreeWidgetItem([PLACEHOLDER, ""]))
            item.addChild(node)
        # 连接项也变绿
        item.setIcon(1, make_dot_icon(DOT_ONLINE))

    def _fill_tables(self, item: QTreeWidgetItem, conn_name: str, db: str) -> None:
        client = self._client(conn_name)
        try:
            groups = client.all_objects(schema=db)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"打开数据库「{db}」失败：{exc}") from exc

        for label, kind, icon_key in _SECTION_DEFS:
            names = groups.get(kind, [])
            title = f"{label} ({len(names)})" if names else label
            folder = QTreeWidgetItem([title, ""])
            folder.setIcon(0, make_object_icon(icon_key))
            folder.setData(0, Qt.UserRole, ("folder", conn_name, db, kind))
            if names:
                for name in names:
                    node = QTreeWidgetItem([name, ""])
                    node.setIcon(0, make_object_icon(icon_key))
                    node.setData(
                        0, Qt.UserRole,
                        ("table", conn_name, db, name, kind),
                    )
                    node.addChild(QTreeWidgetItem([PLACEHOLDER, ""]))
                    folder.addChild(node)
            else:
                empty = QTreeWidgetItem(["（无）", ""])
                empty.setFlags(Qt.ItemIsEnabled)
                folder.addChild(empty)
            item.addChild(folder)

    def _fill_columns(self, item, conn_name, db, table) -> None:
        client = self._client(conn_name)
        cols = client.columns(table, schema=db)
        if not cols:
            item.addChild(QTreeWidgetItem(["（无列信息）", ""]))
            return
        for col in cols:
            name = col.get("name", "?")
            ctype = str(col.get("type", ""))
            nullable = "" if col.get("nullable", True) else " NOT NULL"
            node = QTreeWidgetItem([f"{name}   {ctype}{nullable}", ""])
            node.setIcon(0, make_object_icon("column"))
            node.setData(0, Qt.UserRole, ("column",))
            item.addChild(node)

    def _refresh_node(self, item: QTreeWidgetItem) -> None:
        payload = item.data(0, Qt.UserRole)
        if not payload:
            return
        kind = payload[0]

        # 分组：刷新父数据库节点
        if kind == "folder":
            parent = item.parent()
            if parent is not None:
                self._refresh_node(parent)
            return

        if len(payload) > 1:
            client = self.clients.get(payload[1])
            if client:
                client.invalidate_metadata()
        self._invalidate_cache()
        if kind not in ("conn", "db", "table"):
            return
        was_expanded = item.isExpanded()
        item.takeChildren()
        if not was_expanded:
            item.addChild(QTreeWidgetItem([PLACEHOLDER, ""]))
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if kind == "conn":
                self._fill_databases(item, payload[1])
            elif kind == "db":
                self._fill_tables(item, payload[1], payload[2])
            elif kind == "table":
                self._fill_columns(item, payload[1], payload[2], payload[3])
        except Exception as exc:  # noqa: BLE001
            self._report_load_error(item, exc)
        finally:
            QApplication.restoreOverrideCursor()
        if was_expanded:
            item.setExpanded(True)

    def _refresh_by_conn_db(self, conn_name: str, db: str | None) -> None:
        def _walk(item: QTreeWidgetItem) -> bool:
            payload = item.data(0, Qt.UserRole) or ()
            if len(payload) >= 2 and payload[0] == "conn" and payload[1] == conn_name:
                if db is None:
                    self._refresh_node(item)
                    return True
                for j in range(item.childCount()):
                    child = item.child(j)
                    cp = child.data(0, Qt.UserRole) or ()
                    if len(cp) >= 3 and cp[0] == "db" and cp[2] == db:
                        self._refresh_node(child)
                        return True
                self._refresh_node(item)
                return True
            for j in range(item.childCount()):
                if _walk(item.child(j)):
                    return True
            return False
        for i in range(self.tree.topLevelItemCount()):
            if _walk(self.tree.topLevelItem(i)):
                return

    # ================= 全局搜索 =================
    def _do_search(self) -> None:
        keyword = self.search_edit.text().strip()
        if not keyword:
            return
        conn = self._current_connection()
        if not conn:
            QMessageBox.information(self, "提示", "请先选择一个连接")
            return
        try:
            client = self._client(conn)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "连接失败", str(exc)); return
        self.search_result.clear()
        self.statusBar().showMessage(f"正在搜索「{keyword}」…")
        self.search_btn.setEnabled(False)
        worker = _SearchWorker(client, keyword)
        worker.signals.done.connect(self._on_search_done)
        worker.signals.failed.connect(self._on_search_failed)
        _search_pool().start(worker)

    def _on_search_done(self, hits: list) -> None:
        self.search_btn.setEnabled(True)
        self.search_result.clear()
        for db, tbl, kind, name in hits:
            type_label = "表" if kind == "table" else "列"
            item = QTreeWidgetItem([type_label, db, tbl, name])
            item.setData(0, Qt.UserRole, ("hit", kind, db, tbl, name))
            self.search_result.addTopLevelItem(item)
        self.statusBar().showMessage(f"搜索完成：{len(hits)} 条命中")

    def _on_search_failed(self, message: str) -> None:
        self.search_btn.setEnabled(True)
        QMessageBox.critical(self, "搜索失败", message)

    def _on_search_double_clicked(self, item: QTreeWidgetItem) -> None:
        payload = item.data(0, Qt.UserRole) or ()
        if len(payload) < 5:
            return
        _, _kind, db, tbl, _name = payload[:5]
        conn = self._current_connection()
        if not conn:
            return
        self._open_table(("table", conn, db, tbl, "table"), reuse=True)

    # ================= 补全 =================
    def _completion_words(self, conn_name: str, db: str | None) -> list[str]:
        key = (conn_name, db)
        cached = self._completion_cache.get(key)
        if cached is not None:
            return cached

        # 首次调用：立即返回关键字，库名 / 表名放到后台线程加载。
        # 关键在于不去碰 client.databases()/tables()，否则新页签打开时会
        # 触发同步元数据反射（SSH + 上百张表的反射可能要几秒）。
        client = self.clients.get(conn_name)
        if client is not None:
            try:
                loader = _CompletionWordsLoader(client, db)
                loader.signals.done.connect(
                    lambda words, k=key: self._on_completion_words_loaded(k, words)
                )
                _column_loader_pool().start(loader)
            except Exception as exc:  # noqa: BLE001
                log.debug("补全词后台加载启动失败：%s", exc)
        return list(KEYWORDS)

    def _on_completion_words_loaded(self, key: tuple, words: list[str]) -> None:
        if not words:
            return
        if key in self._completion_cache:
            return
        if len(self._completion_cache) >= _MAX_COMPLETION_CACHE:
            self._completion_cache.pop(next(iter(self._completion_cache)))
        merged = list(KEYWORDS) + list(words)
        self._completion_cache[key] = merged

        # 把新词推送到同连接/库的已打开页签
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not isinstance(w, QueryTab):
                continue
            try:
                if (w.client.cfg.name, w.schema) == key:
                    w.editor.add_completion_words(words)
            except RuntimeError:
                continue

    def _on_columns_loaded(self, tab: QueryTab, mapping: dict) -> None:
        if not mapping:
            return
        flat = [c for cols in mapping.values() for c in cols]
        try:
            tab.editor.add_completion_words(flat)
            tab.editor.set_table_columns(mapping)
        except RuntimeError:
            return

    # ================= 交互 =================
    def _on_context_menu(self, pos) -> None:
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        if item is None:
            menu.addAction("新建连接", self.new_connection)
            menu.addAction("从本机导入数据…", self.import_data_from_file)
            menu.addAction("刷新", self._reload_tree)
        else:
            payload = item.data(0, Qt.UserRole) or ()
            kind = payload[0] if payload else ""
            cfg = self.store.get(payload[1]) if len(payload) > 1 else None
            readonly_conn = cfg is not None and cfg.readonly
            if kind == "group":
                menu.addAction("刷新", self._reload_tree)
            elif kind == "conn":
                menu.addAction("打开连接", lambda: item.setExpanded(True))
                menu.addAction("新建查询", lambda: self.new_query_tab(payload[1]))
                if not readonly_conn:
                    menu.addAction("创建数据库", lambda: self.create_database(payload[1]))
                    menu.addAction("从本机导入数据…", self.import_data_from_file)
                menu.addSeparator()
                menu.addAction("编辑连接", lambda: self.edit_connection(payload[1]))
                menu.addAction("断开连接", lambda: self.disconnect(payload[1]))
                menu.addAction("删除连接", lambda: self.delete_connection(payload[1]))
                menu.addSeparator()
                menu.addAction("刷新", lambda: self._refresh_node(item))
            elif kind == "db":
                menu.addAction("新建查询", lambda: self.new_query_tab(payload[1], payload[2]))
                if not readonly_conn:
                    menu.addAction("新建表", lambda: self.new_table(payload[1], payload[2]))
                    menu.addAction("从本机导入数据…", self.import_data_from_file)
                    menu.addSeparator()
                    menu.addAction("删除数据库", lambda: self.drop_database(payload[1], payload[2]))
                menu.addSeparator()
                menu.addAction("查看 ER 图…", self.show_er_diagram)
                menu.addSeparator()
                menu.addAction("刷新", lambda: self._refresh_node(item))
            elif kind == "folder":
                menu.addAction("刷新", lambda: self._refresh_node(item))
                menu.addAction(
                    "全部展开", lambda: self._expand_folder(item),
                )
            elif kind == "table":
                obj_kind = payload[4] if len(payload) > 4 else "table"
                obj_label = {
                    "view": "视图", "procedure": "存储过程",
                    "function": "函数", "trigger": "触发器", "event": "事件",
                }.get(obj_kind, "表")

                if obj_kind in ("table", "view"):
                    menu.addAction("查询数据", lambda: self._open_table(payload, reuse=False))
                    menu.addAction("生成 DDL", lambda: self._generate_ddl(payload))
                    if obj_kind == "table" and not readonly_conn:
                        menu.addAction("导入数据…", lambda: self._import_data(payload))
                        menu.addAction("修改表结构…", lambda: self.alter_table(payload))
                        menu.addAction("管理索引…", lambda: self.manage_indexes(payload))
                        menu.addSeparator()
                        menu.addAction("解释执行计划", lambda: self._explain(payload))

                menu.addSeparator()
                menu.addAction("复制名称", lambda: self._copy_obj_name(payload[3]))
                if obj_kind in ("table", "view") and not readonly_conn:
                    menu.addAction(
                        f"删除{obj_label}",
                        lambda: self.drop_table(
                            payload[1], payload[2], payload[3], obj_kind,
                        ),
                    )
                menu.addSeparator()
                menu.addAction("刷新", lambda: self._refresh_node(item))
            else:
                menu.addAction("刷新", lambda: self._refresh_node(item))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    @staticmethod
    def _copy_obj_name(name: str) -> None:
        QApplication.clipboard().setText(name)

    def _expand_folder(self, item: QTreeWidgetItem) -> None:
        item.setExpanded(True)
        for i in range(item.childCount()):
            self._expand_folder(item.child(i))

    def _on_item_clicked(self, item, _column) -> None:
        if item is None:
            return
        payload = item.data(0, Qt.UserRole)
        if not payload:
            return
        if payload[0] == "table":
            obj_kind = payload[4] if len(payload) > 4 else "table"
            if obj_kind in ("table", "view"):
                self._open_table(payload, reuse=True)

    # ================= 打开表 =================
    @staticmethod
    def _table_key(conn_name: str, db: str | None, table: str) -> tuple:
        return ("__table__", conn_name, db, table)

    def _open_table(self, payload, *, reuse: bool = True) -> None:
        _, conn_name, db, table = payload[:4]
        key = self._table_key(conn_name, db, table)
        if reuse:
            for i in range(self.tabs.count()):
                w = self.tabs.widget(i)
                if (isinstance(w, QueryTab)
                        and getattr(w, "table_key", None) == key):
                    self.tabs.setCurrentIndex(i)
                    if not w.is_running() and w.result_tabs.count() == 0:
                        try:
                            w.execute_sql()
                        except Exception:  # noqa: BLE001
                            log.exception("打开表时自动执行失败")
                    return
        cfg = self.store.get(conn_name)
        db_type = cfg.db_type if cfg else "mysql"
        qualified = qualify_table(table, db, db_type)
        sql = f"SELECT * FROM {qualified}"
        client = self._client(conn_name)
        table_ctx = client.table_context(table, schema=db)
        self.new_query_tab(
            conn_name, db, sql=sql, title=table,
            paged=True, table_ctx=table_ctx, table_key=key,
        )

    def _generate_ddl(self, payload) -> None:
        _, conn_name, db, table = payload[:4]
        try:
            client = self._client(conn_name)
            ddl = client.generate_create_table(table, schema=db)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "生成 DDL 失败", str(exc)); return
        self.new_query_tab(
            conn_name, db, sql=ddl, title=f"DDL:{table}",
            paged=False, auto_run=False,
        )

    def _explain(self, payload) -> None:
        _, conn_name, db, table = payload[:4]
        cfg = self.store.get(conn_name)
        db_type = cfg.db_type if cfg else "mysql"
        qualified = qualify_table(table, db, db_type)
        base = f"SELECT * FROM {qualified}"
        if db_type in ("postgresql", "cockroachdb", "kingbase"):
            sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)\n{base};"
        elif db_type in ("sqlite", "duckdb"):
            sql = f"EXPLAIN QUERY PLAN\n{base};"
        elif db_type == "oracle":
            sql = (
                f"EXPLAIN PLAN FOR\n{base};\n"
                "SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY);"
            )
        else:
            sql = f"EXPLAIN\n{base};"
        self.new_query_tab(conn_name, db, sql=sql, title=f"EXPLAIN:{table}", paged=False)

    # ================= 导入：跳过已存在的行 =================
    def _filter_existing_rows(
        self,
        client: DatabaseClient,
        table: str,
        schema: str | None,
        db_type: str,
        columns: list[str],
        rows: list[list],
        pk_columns: list[str],
        max_scan: int = 2_000_000,
    ) -> tuple[list[list], int, str]:
        if not rows:
            return list(rows), 0, ""

        key_cols = [c for c in (pk_columns or []) if c in columns]

        if not key_cols:
            try:
                for idx in client.indexes(table, schema=schema):
                    if idx.get("unique") and idx.get("column_names"):
                        cand = [c for c in idx["column_names"] if c in columns]
                        if cand:
                            key_cols = cand
                            break
            except Exception as exc:  # noqa: BLE001
                log.debug("读取唯一索引失败：%s", exc)

        if not key_cols:
            return list(rows), 0, ""

        key_indices = [columns.index(c) for c in key_cols]
        qualified = qualify_table(table, schema, db_type)
        collist = ", ".join(quote_ident(c, db_type) for c in key_cols)

        try:
            results = client.execute_many(
                f"SELECT {collist} FROM {qualified}",
                max_rows=max_scan,
                schema=schema,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("读取目标表已有数据失败，跳过去重：%s", exc)
            return list(rows), 0, ""

        if not results or not results[0].success:
            return list(rows), 0, ""

        existing: set[tuple] = set()
        for r in results[0].rows:
            try:
                existing.add(
                    tuple(sql_literal(r[i], db_type) for i in range(len(key_cols)))
                )
            except Exception:  # noqa: BLE001
                continue

        filtered: list[list] = []
        skipped = 0
        for r in rows:
            try:
                k = tuple(
                    sql_literal(r[i], db_type) if i < len(r) else "NULL"
                    for i in key_indices
                )
            except Exception:  # noqa: BLE001
                filtered.append(r)
                continue
            if k in existing:
                skipped += 1
                continue
            existing.add(k)
            filtered.append(r)

        return filtered, skipped, ", ".join(key_cols)

    # ================= 导入：右键表 =================
    def _import_data(self, payload) -> None:
        _, conn_name, db, table = payload[:4]
        cfg = self.store.get(conn_name)
        if cfg is not None and cfg.readonly:
            QMessageBox.warning(self, "只读连接", "当前连接被标记为只读，无法导入数据。")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要导入的文件", "",
            "所有支持 (*.csv *.json *.xlsx *.xlsm);;"
            "CSV (*.csv);;JSON (*.json);;Excel (*.xlsx *.xlsm)",
        )
        if not path:
            return

        try:
            client = self._client(conn_name)
            table_cols = [c["name"] for c in client.columns(table, schema=db)]
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "读取表结构失败", str(exc)); return
        if not table_cols:
            QMessageBox.warning(self, "提示", f"目标表「{table}」没有列信息。")
            return

        db_type = cfg.db_type if cfg else "mysql"
        pk_columns: list[str] = []
        try:
            ctx = client.table_context(table, schema=db)
            pk_columns = list(ctx.get("primary_keys", ()))
        except Exception:  # noqa: BLE001
            pk_columns = []

        dlg = ImportWizardDialog(
            self, path=path, table=table,
            table_columns=table_cols, db_type=db_type,
        )
        if dlg.exec() != QDialog.Accepted:
            return
        columns, data_rows, batch, conflict = dlg.result()
        if not columns or not data_rows:
            return

        skip_existing = dlg.result_skip_existing()
        skipped_count = 0
        key_desc = ""
        if skip_existing:
            data_rows, skipped_count, key_desc = self._filter_existing_rows(
                client, table, db, db_type, columns, data_rows, pk_columns,
            )
            if not key_desc:
                QMessageBox.information(
                    self, "无法判重",
                    "目标表没有主键或唯一约束，无法按行判重。\n"
                    "已改为按冲突策略执行 —— 建议将冲突策略改为\n"
                    "「INSERT IGNORE」或「冲突则更新（UPSERT）」。",
                )
            if not data_rows:
                QMessageBox.information(
                    self, "没有需要导入的行",
                    f"目标表「{table}」已经包含文件中的所有数据，无需导入。",
                )
                return

        stmts = io_tools.build_inserts(
            table, columns, data_rows, schema=db, db_type=db_type,
            batch=batch, conflict=conflict, pk_columns=pk_columns,
        )

        conflict_label = {
            "insert": "普通插入", "ignore": "忽略冲突",
            "replace": "替换已有", "upsert": "冲突则更新",
        }.get(conflict, conflict)

        confirm_lines = [
            f"将向 {table} 导入 {len(data_rows)} 行",
            f"列：{', '.join(columns)}",
            f"冲突策略：{conflict_label}",
        ]
        if skip_existing and skipped_count:
            confirm_lines.append(
                f"已按 {key_desc} 跳过目标表中已存在的 {skipped_count} 行"
            )
        confirm_lines.append(f"语句数：{len(stmts)}")
        confirm_lines.append("")
        confirm_lines.append("继续？")

        if QMessageBox.question(
            self, "确认导入", "\n".join(confirm_lines),
        ) != QMessageBox.Yes:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            results = client.execute_many("\n".join(stmts), schema=db)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导入失败", str(exc)); return
        finally:
            QApplication.restoreOverrideCursor()
        failed = [r for r in results if not r.success]
        if failed:
            QMessageBox.critical(self, "导入中断", failed[0].message)
        else:
            msg = f"已导入 {len(data_rows)} 行"
            if skip_existing and skipped_count:
                msg += f"\n（按 {key_desc} 跳过了 {skipped_count} 行）"
            QMessageBox.information(self, "完成", msg)
            client.invalidate_metadata()

    # ================= 导入：从本机（可自动建表） =================
    def import_data_from_file(self) -> None:
        if not self.store.connections:
            QMessageBox.information(self, "提示", "请先新建一个数据库连接。")
            return

        preset_conn = self._current_connection() or ""
        preset_db = ""
        item = self.tree.currentItem()
        while item is not None:
            payload = item.data(0, Qt.UserRole) or ()
            if payload and payload[0] == "db":
                preset_db = payload[2]
                break
            item = item.parent()

        dlg = ImportDataDialog(
            self,
            connections=[c.name for c in self.store.connections],
            client_getter=self._client,
            preset_conn=preset_conn,
            preset_db=preset_db,
        )
        if dlg.exec() != QDialog.Accepted:
            return

        path, conn_name, db_name, table_name = dlg.result_values()

        cfg = self.store.get(conn_name)
        if cfg is not None and cfg.readonly:
            QMessageBox.warning(self, "只读连接", "当前连接被标记为只读，无法导入数据。")
            return

        try:
            client = self._client(conn_name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "连接失败", str(exc))
            return

        db_type = cfg.db_type if cfg else "mysql"

        try:
            entries = list(client.tables(schema=db_name))
            existing_tables = {t for t, kind in entries if kind == "table"}
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "检查目标表失败", str(exc))
            return

        table_exists = table_name in existing_tables
        pk_columns: list[str] = []

        if table_exists:
            try:
                table_cols = [
                    c["name"] for c in client.columns(table_name, schema=db_name)
                ]
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "读取表结构失败", str(exc))
                return
            if not table_cols:
                QMessageBox.warning(
                    self, "提示", f"目标表「{table_name}」没有列信息。"
                )
                return
            try:
                ctx = client.table_context(table_name, schema=db_name)
                pk_columns = list(ctx.get("primary_keys", ()))
            except Exception:  # noqa: BLE001
                pk_columns = []
        else:
            table_cols = []
            try:
                header, _ = io_tools.read_any(path)
                table_cols = list(header)
            except Exception as exc:  # noqa: BLE001
                log.debug("预读文件失败（可忽略）：%s", exc)
                table_cols = []

        wizard = ImportWizardDialog(
            self, path=path, table=table_name,
            table_columns=table_cols, db_type=db_type,
        )
        if wizard.exec() != QDialog.Accepted:
            return

        columns, data_rows, batch, conflict = wizard.result()
        if not columns or not data_rows:
            return

        skip_existing = wizard.result_skip_existing()
        skipped_count = 0
        key_desc = ""
        if skip_existing and table_exists:
            data_rows, skipped_count, key_desc = self._filter_existing_rows(
                client, table_name, db_name, db_type, columns, data_rows, pk_columns,
            )
            if not key_desc:
                QMessageBox.information(
                    self, "无法判重",
                    "目标表没有主键或唯一约束，无法按行判重。\n"
                    "已改为按冲突策略执行 —— 建议将冲突策略改为\n"
                    "「INSERT IGNORE」或「冲突则更新（UPSERT）」。",
                )
            if not data_rows:
                QMessageBox.information(
                    self, "没有需要导入的行",
                    f"目标表「{table_name}」已经包含文件中的所有数据，无需导入。",
                )
                return

        if not table_exists:
            create_dlg = CreateTablePreviewDialog(
                self,
                table=table_name,
                columns=columns,
                rows=data_rows,
                schema=db_name,
                db_type=db_type,
            )
            if create_dlg.exec() != QDialog.Accepted:
                return
            ddl = create_dlg.result_sql()
            if not ddl:
                QMessageBox.warning(self, "提示", "建表 SQL 为空，已取消导入。")
                return

            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                results = client.execute_many(ddl, schema=db_name)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.critical(self, "建表失败", str(exc))
                return
            finally:
                QApplication.restoreOverrideCursor()

            failed = [r for r in results if not r.success]
            if failed:
                QMessageBox.critical(self, "建表失败", failed[0].message)
                return

            try:
                client.invalidate_metadata()
            except Exception:  # noqa: BLE001
                pass
            self.statusBar().showMessage(f"已创建表：{table_name}")
            try:
                self._refresh_by_conn_db(conn_name, db_name)
            except Exception:  # noqa: BLE001
                pass

        stmts = io_tools.build_inserts(
            table_name, columns, data_rows,
            schema=db_name, db_type=db_type,
            batch=batch, conflict=conflict, pk_columns=pk_columns,
        )

        conflict_label = {
            "insert": "普通插入", "ignore": "忽略冲突",
            "replace": "替换已有", "upsert": "冲突则更新",
        }.get(conflict, conflict)

        target_desc = f"目标：{conn_name} / {db_name} / {table_name}"
        if not table_exists:
            target_desc += "（已创建）"

        confirm_lines = [
            target_desc,
            f"文件：{path}",
            f"行数：{len(data_rows)}",
            f"列：{', '.join(columns)}",
            f"冲突策略：{conflict_label}",
        ]
        if skip_existing and skipped_count:
            confirm_lines.append(
                f"已按 {key_desc} 跳过目标表中已存在的 {skipped_count} 行"
            )
        confirm_lines.append(f"语句数：{len(stmts)}")
        confirm_lines.append("")
        confirm_lines.append("继续？")

        if QMessageBox.question(
            self, "确认导入", "\n".join(confirm_lines),
        ) != QMessageBox.Yes:
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            results = client.execute_many("\n".join(stmts), schema=db_name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "导入失败", str(exc))
            return
        finally:
            QApplication.restoreOverrideCursor()

        failed = [r for r in results if not r.success]
        if failed:
            QMessageBox.critical(self, "导入中断", failed[0].message)
            self.statusBar().showMessage("导入中断")
        else:
            msg = f"已导入 {len(data_rows)} 行"
            if skip_existing and skipped_count:
                msg += f"\n（按 {key_desc} 跳过了 {skipped_count} 行）"
            QMessageBox.information(self, "完成", msg)
            self.statusBar().showMessage(
                f"导入完成：{len(data_rows)} 行 → {table_name}"
            )
            try:
                client.invalidate_metadata()
            except Exception:  # noqa: BLE001
                pass

    # ================= 页签 =================
    def _current_connection(self) -> str | None:
        item = self.tree.currentItem()
        while item is not None:
            payload = item.data(0, Qt.UserRole) or ()
            if payload and payload[0] in ("conn", "db", "table"):
                return payload[1]
            item = item.parent()
        if self.store.connections:
            return self.store.connections[0].name
        return None

    def new_query_tab(
        self, conn_name: str | None = None, db: str | None = None,
        sql: str = "", title: str | None = None,
        *, paged: bool = False, table_ctx: dict | None = None,
        table_key: tuple | None = None, auto_run: bool = True,
    ) -> QueryTab | None:
        conn_name = conn_name or self._current_connection()
        if not conn_name:
            QMessageBox.information(self, "提示", "请先新建一个数据库连接")
            return None
        if self.store.get(conn_name) is None:
            QMessageBox.warning(self, "提示", f"连接「{conn_name}」不存在")
            return None
        try:
            client = self._client(conn_name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "连接失败", str(exc)); return None
        tab = QueryTab(
            client, schema=db, sql=sql, parent=self,
            paged=paged, table_ctx=table_ctx,
        )
        tab.table_key = table_key
        tab.editor.set_completion_words(self._completion_words(conn_name, db))
        if db:
            try:
                loader = _ColumnCompletionLoader(client, db)
                loader.signals.done.connect(
                    lambda mapping, t=tab: self._on_columns_loaded(t, mapping)
                )
                _column_loader_pool().start(loader)
            except Exception as exc:  # noqa: BLE001
                log.debug("列名补全后台加载失败：%s", exc)
        index = self.tabs.addTab(tab, title or conn_name)
        self.tabs.setCurrentIndex(index)
        # 连接已打开，更新圆点
        self._refresh_conn_dot(conn_name)
        if sql and auto_run:
            try:
                tab.execute_sql(sql)
            except Exception:  # noqa: BLE001
                log.exception("新建页签后自动执行失败")
        return tab

    def run_current(self) -> None:
        widget = self.tabs.currentWidget()
        if isinstance(widget, QueryTab):
            widget.execute_sql()

    def _close_tab(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if isinstance(widget, QueryTab):
            if widget.has_pending_edits():
                if QMessageBox.question(
                    self, "确认关闭", "该页签有未提交的编辑，确定关闭？"
                ) != QMessageBox.Yes:
                    return
            self._push_closed_tab(widget, self.tabs.tabText(index))
            widget.cleanup()
        self.tabs.removeTab(index)
        if widget is not None:
            widget.deleteLater()
        if self._pending_close_tabs or self._pending_dispose:
            QTimer.singleShot(0, self._flush_pending_close)

    # ================= 关闭确认 =================
    def _collect_dirty_tabs(self, conn_name: str | None = None) -> list[str]:
        titles: list[str] = []
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if not isinstance(w, QueryTab):
                continue
            if conn_name and w.client.cfg.name != conn_name:
                continue
            if w.has_pending_edits():
                titles.append(self.tabs.tabText(i))
        return titles

    def _close_tabs_for_connection(self, conn_name: str) -> None:
        dirty = self._collect_dirty_tabs(conn_name)
        if dirty:
            preview = "\n  • ".join(dirty[:5])
            more = f"\n  • ...等 {len(dirty)} 个页签" if len(dirty) > 5 else ""
            if QMessageBox.question(
                self, "确认关闭",
                f"连接「{conn_name}」的以下页签有未保存的修改：\n\n"
                f"  • {preview}{more}\n\n"
                "关闭后修改将丢失，继续吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
        for i in range(self.tabs.count() - 1, -1, -1):
            w = self.tabs.widget(i)
            if not (isinstance(w, QueryTab) and w.client.cfg.name == conn_name):
                continue
            if w.is_running():
                try:
                    w.cleanup()
                except Exception:  # noqa: BLE001
                    pass
                self._pending_close_tabs.append(w)
                continue
            w.cleanup()
            self.tabs.removeTab(i)
            w.deleteLater()

    def _flush_pending_close(self) -> None:
        remaining: list[QueryTab] = []
        for w in self._pending_close_tabs:
            try:
                if w.is_running():
                    remaining.append(w)
                    continue
            except RuntimeError:
                continue
            idx = self.tabs.indexOf(w)
            if idx >= 0:
                self.tabs.removeTab(idx)
            try:
                w.deleteLater()
            except Exception:  # noqa: BLE001
                pass
        self._pending_close_tabs = remaining

        if self._pending_dispose:
            still_running_conns: set[str] = set()
            for w in remaining:
                try:
                    still_running_conns.add(w.client.cfg.name)
                except RuntimeError:
                    continue
            for name, client in list(self._pending_dispose.items()):
                if name in still_running_conns:
                    continue
                try:
                    client.dispose()
                except Exception as exc:  # noqa: BLE001
                    log.warning("延迟释放连接失败 [%s]：%s", name, exc)
                del self._pending_dispose[name]

    def closeEvent(self, event) -> None:
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, QueryTab) and w.is_running():
                try:
                    w.cancel_running()
                except Exception:  # noqa: BLE001
                    pass

        dirty = self._collect_dirty_tabs()
        if dirty:
            preview = "\n  • ".join(dirty[:8])
            more = f"\n  • ...等 {len(dirty)} 个页签" if len(dirty) > 8 else ""
            if QMessageBox.question(
                self, "确认退出",
                f"以下页签有未保存的修改：\n\n  • {preview}{more}\n\n"
                "退出后修改将丢失，仍要退出吗？",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                event.ignore()
                return
        self._save_open_tabs()
        self._save_state()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, QueryTab):
                w.cleanup()
        for client in self.clients.values():
            client.dispose()
        self.clients.clear()
        for client in self._pending_dispose.values():
            try:
                client.dispose()
            except Exception:  # noqa: BLE001
                pass
        self._pending_dispose.clear()
        super().closeEvent(event)