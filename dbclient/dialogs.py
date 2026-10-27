"""连接配置对话框 + 新建数据库 + 新建表 + ALTER TABLE + 索引管理 + SQL 预览
+ 导入列映射向导 + 从本机导入数据（目标选择）+ 建表预览（导入自动建表）。

界面 v2（2024 美化重构）：
- 所有对话框统一 16/18 边距、8/10 间距
- 输入框最小高度 30、按钮最小高度 32
- 主按钮强调色实心，次按钮描边
- 分组框统一圆角 10px
- SSH 隧道收纳至「高级选项」，默认折叠
"""
from __future__ import annotations

import re
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .connection import DEFAULT_PORTS, ConnectionConfig
from .engine import DatabaseClient
from .sql_util import qualify_table, quote_ident
from .ssh_tunnel import available as ssh_tunnel_available
from .theme import theme_manager

# 所有受支持的类型 key（顺序影响「连接设置」对话框里的下拉框）
DB_TYPES = [
    # MySQL 协议族
    "mysql", "mariadb", "tidb", "oceanbase",
    # PostgreSQL 协议族
    "postgresql", "cockroachdb", "kingbase",
    # 企业级
    "mssql", "oracle", "db2", "firebird",
    # 嵌入式
    "sqlite", "duckdb",
    # 数仓 / 分析
    "clickhouse", "trino",
]

_DB_TYPE_LABELS: dict[str, str] = {
    "mysql": "MySQL",
    "mariadb": "MariaDB",
    "tidb": "TiDB",
    "oceanbase": "OceanBase",
    "postgresql": "PostgreSQL",
    "cockroachdb": "CockroachDB",
    "kingbase": "人大金仓 KingBase",
    "mssql": "SQL Server",
    "oracle": "Oracle",
    "db2": "IBM DB2",
    "firebird": "Firebird",
    "sqlite": "SQLite",
    "duckdb": "DuckDB",
    "clickhouse": "ClickHouse",
    "trino": "Trino",
}

_FILE_DB_TYPES = frozenset(("sqlite", "duckdb"))

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#@]*$")

PRESET_COLORS: tuple[tuple[str, str], ...] = (
    ("蓝", "#3b82f6"),
    ("绿", "#10b981"),
    ("橙", "#f59e0b"),
    ("红", "#ef4444"),
    ("紫", "#8b5cf6"),
    ("粉", "#ec4899"),
    ("青", "#06b6d4"),
    ("灰", "#64748b"),
)

# 表格内嵌控件的行高
_INLINE_ROW_HEIGHT = 30

# 统一尺寸
_INPUT_H = 30
_BTN_H = 32


def color_dot_icon(hex_color: str, size: int = 14) -> QIcon:
    """生成实心圆点图标，用于连接的颜色标记。空字符串返回空图标。"""
    if not hex_color:
        return QIcon()
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QBrush(QColor(hex_color)))
    margin = max(1, size // 7)
    painter.drawEllipse(margin, margin, size - 2 * margin, size - 2 * margin)
    painter.end()
    return QIcon(pm)


def _set_error(widget, on: bool) -> None:
    widget.setProperty("error", "true" if on else "false")
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _compact_combo(parent: QWidget | None = None) -> QComboBox:
    """创建嵌入表格单元格的紧凑组合框。"""
    combo = QComboBox(parent)
    combo.setStyleSheet(
        "QComboBox { padding: 1px 6px; min-height: 0px; }"
        "QComboBox::drop-down { width: 18px; border: none; }"
    )
    return combo


def _vsep() -> QFrame:
    """竖直分隔线。"""
    line = QFrame()
    line.setFrameShape(QFrame.VLine)
    line.setProperty("role", "sep")
    line.setFixedWidth(1)
    return line


def _style_dialog(dlg: QDialog, extra: str = "") -> None:
    """为对话框应用统一样式。"""
    p = theme_manager.palette
    dlg.setStyleSheet(f"""
        QDialog {{
            background: {p.bg};
        }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
            min-height: {_INPUT_H - 8}px;
            padding: 4px 10px;
        }}
        QPushButton {{
            min-height: {_BTN_H - 10}px;
            padding: 6px 16px;
        }}
        QGroupBox {{
            border: 1px solid {p.border};
            border-radius: 10px;
            margin-top: 14px;
            padding-top: 12px;
            background: {p.surface};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 14px;
            padding: 0 6px;
            color: {p.text_muted};
            font-weight: 600;
            background: {p.surface};
        }}
        QPlainTextEdit {{
            background: {p.editor_bg};
            color: {p.text};
            border: 1px solid {p.border_strong};
            border-radius: 8px;
            padding: 8px 10px;
        }}
        QFrame[role="sep"] {{
            background: {p.border};
            border: none;
        }}
        QLabel[role="hint"] {{
            color: {p.text_muted};
        }}
        QLabel[role="error"] {{
            color: {p.danger};
        }}
        {extra}
    """)


# ==================== 连接测试后台 worker ====================
class _ConnectionTestWorker(QRunnable):
    """后台执行「测试连接」，避免 SSH + DB 握手期间卡住 UI。"""

    class _Signals(QObject):
        done = Signal(bool, str)

    def __init__(self, cfg: ConnectionConfig):
        super().__init__()
        self.cfg = cfg
        self.signals = _ConnectionTestWorker._Signals()
        self.setAutoDelete(True)

    def run(self) -> None:
        client = None
        try:
            client = DatabaseClient(self.cfg)
            client.test_connection()
        except Exception as exc:  # noqa: BLE001
            try:
                msg = ConnectionDialog._translate_connect_error(exc, self.cfg)
            except Exception:  # noqa: BLE001
                msg = str(exc) or exc.__class__.__name__
            self.signals.done.emit(False, msg)
        else:
            self.signals.done.emit(True, "")
        finally:
            if client is not None:
                try:
                    client.dispose()
                except Exception:  # noqa: BLE001
                    pass


# ==================== 连接配置对话框 ====================
class ConnectionDialog(QDialog):
    def __init__(
        self,
        parent=None,
        config: ConnectionConfig | None = None,
        *,
        preset_type: str | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("连接设置")
        self.setMinimumSize(640, 880)

        if config is None:
            config = ConnectionConfig()
            if preset_type:
                config.db_type = preset_type
                port = DEFAULT_PORTS.get(preset_type)
                if port:
                    config.port = port
                if preset_type in _FILE_DB_TYPES:
                    config.host = ""
                    config.user = ""

        self._config = config
        self._last_type = self._config.db_type
        self._test_worker: _ConnectionTestWorker | None = None

        # ---------- 数据库基础 ----------
        self.name_edit = QLineEdit(self._config.name)
        self.name_edit.setPlaceholderText("例如：本地 MySQL")

        self.type_combo = QComboBox()
        for key in DB_TYPES:
            self.type_combo.addItem(_DB_TYPE_LABELS.get(key, key), key)
        idx = self.type_combo.findData(self._config.db_type)
        if idx < 0:
            self.type_combo.addItem(
                _DB_TYPE_LABELS.get(self._config.db_type, self._config.db_type),
                self._config.db_type,
            )
            idx = self.type_combo.count() - 1
        self.type_combo.setCurrentIndex(idx)

        self.host_edit = QLineEdit(self._config.host)
        self.host_edit.setPlaceholderText("127.0.0.1")

        self.port_spin = QSpinBox()
        self.port_spin.setRange(0, 65535)
        self.port_spin.setValue(
            self._config.port or DEFAULT_PORTS.get(self._config.db_type, 3306)
        )
        self.port_spin.setMinimumWidth(140)

        self.user_edit = QLineEdit(self._config.user)
        self.pwd_edit = QLineEdit(self._config.password)
        self.pwd_edit.setEchoMode(QLineEdit.Password)
        self.pwd_edit.setPlaceholderText("密码（走系统钥匙串或本地加密存储）")
        self._pwd_toggle = QToolButton()
        self._pwd_toggle.setText("👁")
        self._pwd_toggle.setCheckable(True)
        self._pwd_toggle.setCursor(Qt.PointingHandCursor)
        self._pwd_toggle.setToolTip("显示 / 隐藏密码")
        self._pwd_toggle.setFixedSize(_INPUT_H, _INPUT_H)
        self._pwd_toggle.toggled.connect(self._toggle_pwd)
        pwd_row = QHBoxLayout()
        pwd_row.setContentsMargins(0, 0, 0, 0)
        pwd_row.setSpacing(4)
        pwd_row.addWidget(self.pwd_edit, 1)
        pwd_row.addWidget(self._pwd_toggle)

        self.db_edit = QLineEdit(self._config.database)
        self.db_edit.setPlaceholderText("默认数据库（可选）")
        self.charset_edit = QLineEdit(self._config.charset)

        self.path_edit = QLineEdit(self._config.sqlite_path)
        self.browse_btn = QPushButton("浏览…")
        self.browse_btn.setCursor(Qt.PointingHandCursor)
        self.browse_btn.clicked.connect(self._pick_sqlite)

        self._path_widget = QWidget()
        path_row = QHBoxLayout(self._path_widget)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(4)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_btn)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignTop)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.addRow("连接名 *", self.name_edit)
        form.addRow("类型", self.type_combo)

        self.host_label = QLabel("主机")
        form.addRow(self.host_label, self.host_edit)
        self.port_label = QLabel("端口")
        form.addRow(self.port_label, self.port_spin)
        self.user_label = QLabel("用户名")
        form.addRow(self.user_label, self.user_edit)
        self.pwd_label = QLabel("密码")
        form.addRow(self.pwd_label, pwd_row)
        self.db_label = QLabel("默认数据库")
        form.addRow(self.db_label, self.db_edit)
        self.charset_label = QLabel("字符集")
        form.addRow(self.charset_label, self.charset_edit)
        self.path_label = QLabel("SQLite 文件")
        form.addRow(self.path_label, self._path_widget)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(0, 3600)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setSpecialValueText("不限制")
        self.timeout_spin.setValue(int(self._config.query_timeout or 0))
        self.timeout_spin.setMinimumWidth(160)
        self.timeout_spin.setToolTip(
            "单条 SQL 的服务端/客户端超时；0 = 使用服务端默认。\n"
            "MySQL 走 read_timeout，PG 走 statement_timeout，SQL Server 走 timeout。"
        )

        self.group_edit = QLineEdit(self._config.group or "")
        self.group_edit.setPlaceholderText("可选，用于在对象树中分组显示")

        self.color_combo = QComboBox()
        self.color_combo.addItem(QIcon(), "（无）", "")
        for name, hexv in PRESET_COLORS:
            self.color_combo.addItem(color_dot_icon(hexv), name, hexv)
        idx = self.color_combo.findData(self._config.color or "")
        if idx >= 0:
            self.color_combo.setCurrentIndex(idx)
        elif self._config.color:
            self.color_combo.addItem(
                color_dot_icon(self._config.color),
                self._config.color,
                self._config.color,
            )
            self.color_combo.setCurrentIndex(self.color_combo.count() - 1)
        self.color_combo.setToolTip("在对象树中给该连接加上彩色圆点，便于区分环境")

        self.readonly_box = QCheckBox("只读连接（禁止执行写操作）")
        self.readonly_box.setChecked(bool(self._config.readonly))
        self.readonly_box.setToolTip(
            "启用后，本连接上的 INSERT / UPDATE / DELETE / DDL 会被拒绝；\n"
            "结果表也不可编辑。适合连接生产库时使用。"
        )

        form.addRow("查询超时", self.timeout_spin)
        form.addRow("分组", self.group_edit)
        form.addRow("标记颜色", self.color_combo)
        form.addRow("", self.readonly_box)

        # ---------- 高级选项（默认隐藏 SSH 隧道） ----------
        self.advanced_box = QCheckBox("显示高级选项（SSH 隧道）")
        self.advanced_box.setChecked(bool(self._config.ssh_host))
        form.addRow("", self.advanced_box)

        _expanding = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for w in (
            self.name_edit, self.type_combo, self.host_edit, self.port_spin,
            self.user_edit, self.pwd_edit, self.db_edit, self.charset_edit,
            self.timeout_spin, self.group_edit, self.color_combo,
            self._path_widget,
        ):
            w.setSizePolicy(_expanding)

        # ---------- SSH 隧道 ----------
        self.ssh_group = QGroupBox("SSH 隧道（可选）")
        self.ssh_group.setCheckable(True)
        self.ssh_group.setChecked(bool(self._config.ssh_host))
        self.ssh_group.setVisible(self.advanced_box.isChecked())

        self.ssh_host_edit = QLineEdit(self._config.ssh_host)
        self.ssh_host_edit.setPlaceholderText("跳板机地址，例如 bastion.example.com")

        self.ssh_port_spin = QSpinBox()
        self.ssh_port_spin.setRange(1, 65535)
        self.ssh_port_spin.setValue(int(self._config.ssh_port or 22))
        self.ssh_port_spin.setMinimumWidth(140)

        self.ssh_user_edit = QLineEdit(self._config.ssh_user)
        self.ssh_user_edit.setPlaceholderText("SSH 登录用户名")

        self.ssh_pwd_edit = QLineEdit(self._config.ssh_password)
        self.ssh_pwd_edit.setEchoMode(QLineEdit.Password)
        self.ssh_pwd_edit.setPlaceholderText("SSH 密码（与私钥 / agent 三选一）")
        self._ssh_pwd_toggle = QToolButton()
        self._ssh_pwd_toggle.setText("👁")
        self._ssh_pwd_toggle.setCheckable(True)
        self._ssh_pwd_toggle.setCursor(Qt.PointingHandCursor)
        self._ssh_pwd_toggle.setToolTip("显示 / 隐藏 SSH 密码")
        self._ssh_pwd_toggle.setFixedSize(_INPUT_H, _INPUT_H)
        self._ssh_pwd_toggle.toggled.connect(
            lambda c: self.ssh_pwd_edit.setEchoMode(
                QLineEdit.Normal if c else QLineEdit.Password
            )
        )
        ssh_pwd_row = QHBoxLayout()
        ssh_pwd_row.setContentsMargins(0, 0, 0, 0)
        ssh_pwd_row.setSpacing(4)
        ssh_pwd_row.addWidget(self.ssh_pwd_edit, 1)
        ssh_pwd_row.addWidget(self._ssh_pwd_toggle)

        self.ssh_key_edit = QLineEdit(self._config.ssh_key_file)
        self.ssh_key_edit.setPlaceholderText("私钥文件路径（优先于密码）")
        self.ssh_key_browse = QPushButton("浏览…")
        self.ssh_key_browse.setCursor(Qt.PointingHandCursor)
        self.ssh_key_browse.clicked.connect(self._pick_ssh_key)
        ssh_key_row = QHBoxLayout()
        ssh_key_row.setContentsMargins(0, 0, 0, 0)
        ssh_key_row.setSpacing(4)
        ssh_key_row.addWidget(self.ssh_key_edit, 1)
        ssh_key_row.addWidget(self.ssh_key_browse)

        ssh_form = QFormLayout(self.ssh_group)
        ssh_form.setContentsMargins(16, 22, 16, 14)
        ssh_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        ssh_form.setHorizontalSpacing(14)
        ssh_form.setVerticalSpacing(10)
        ssh_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        ssh_form.addRow("SSH 主机", self.ssh_host_edit)
        ssh_form.addRow("SSH 端口", self.ssh_port_spin)
        ssh_form.addRow("SSH 用户", self.ssh_user_edit)
        ssh_form.addRow("SSH 密码", ssh_pwd_row)
        ssh_form.addRow("私钥文件", ssh_key_row)

        self.ssh_agent_box = QCheckBox("使用 ssh-agent / Pageant 中的密钥")
        self.ssh_agent_box.setChecked(bool(self._config.ssh_use_agent))
        self.ssh_agent_box.setToolTip(
            "勾选后忽略下方的 SSH 密码与私钥，改用系统 ssh-agent：\n"
            "• Linux / macOS：先用 ssh-add 把私钥加入 agent\n"
            "• Windows：启动 Pageant 并加载密钥，或启用系统 ssh-agent 服务\n"
            "未勾选时按「私钥 > 密码」的顺序认证。"
        )
        self.ssh_agent_box.toggled.connect(self._on_ssh_agent_toggled)
        ssh_form.addRow("", self.ssh_agent_box)

        if not ssh_tunnel_available():
            warn = QLabel(
                "⚠ 未检测到 sshtunnel 库，启用后无法建立隧道。\n"
                "请执行：pip install sshtunnel"
            )
            warn.setProperty("role", "error")
            warn.setWordWrap(True)
            ssh_form.addRow(warn)

        for w in (
            self.ssh_host_edit, self.ssh_port_spin, self.ssh_user_edit,
            self.ssh_pwd_edit, self.ssh_key_edit,
        ):
            w.setSizePolicy(_expanding)

        # ---------- 底部按钮 ----------
        self.test_btn = QPushButton("测试连接")
        self.test_btn.setCursor(Qt.PointingHandCursor)
        self.test_btn.setMinimumHeight(_BTN_H)
        self.test_btn.clicked.connect(self._test)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("保存")
        ok_btn.setMinimumHeight(_BTN_H)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        cancel_btn.setMinimumHeight(_BTN_H)
        buttons.addButton(self.test_btn, QDialogButtonBox.ActionRole)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        tip = QLabel("提示：带 * 为必填项；密码与 SSH 凭据均不会明文落盘。")
        tip.setProperty("role", "hint")

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setProperty("role", "sep")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(self.ssh_group)
        layout.addWidget(sep)
        layout.addWidget(tip)
        layout.addWidget(buttons)

        self.type_combo.currentIndexChanged.connect(self._on_type_changed_idx)
        self.advanced_box.toggled.connect(self._on_advanced_toggled)
        self._on_type_changed(self._current_type_key())
        self._on_ssh_agent_toggled(self.ssh_agent_box.isChecked())

        _style_dialog(self)

    # ---------- 类型辅助 ----------
    def _current_type_key(self) -> str:
        data = self.type_combo.currentData()
        if data:
            return str(data)
        return self.type_combo.currentText()

    def _on_type_changed_idx(self, _idx: int) -> None:
        self._on_type_changed(self._current_type_key())

    # ---------- 显示 / 隐藏密码 ----------
    def _toggle_pwd(self, checked: bool) -> None:
        self.pwd_edit.setEchoMode(QLineEdit.Normal if checked else QLineEdit.Password)

    def _on_ssh_agent_toggled(self, checked: bool) -> None:
        for w in (
            self.ssh_pwd_edit, self._ssh_pwd_toggle,
            self.ssh_key_edit, self.ssh_key_browse,
        ):
            w.setEnabled(not checked)

    def _on_advanced_toggled(self, checked: bool) -> None:
        self.ssh_group.setVisible(checked)
        if not checked:
            self.ssh_group.setChecked(False)

    # ---------- 类型切换 ----------
    def _on_type_changed(self, db_type: str) -> None:
        is_file_db = db_type in _FILE_DB_TYPES

        for widget in (
            self.host_edit, self.port_spin, self.user_edit,
            self.pwd_edit, self._pwd_toggle, self.db_edit, self.charset_edit,
        ):
            widget.setEnabled(not is_file_db)
        for label in (
            self.host_label, self.port_label, self.user_label,
            self.pwd_label, self.db_label, self.charset_label,
        ):
            label.setVisible(not is_file_db)

        self.path_label.setText(
            "DuckDB 文件" if db_type == "duckdb" else "SQLite 文件"
        )
        self.path_label.setVisible(is_file_db)
        self._path_widget.setVisible(is_file_db)

        self.advanced_box.setVisible(not is_file_db)
        if is_file_db:
            self.advanced_box.setChecked(False)
            self.ssh_group.setChecked(False)

        if not is_file_db:
            default_port = DEFAULT_PORTS.get(db_type, 0)
            prev_default = DEFAULT_PORTS.get(self._last_type, 0)
            cur = self.port_spin.value()
            if default_port and cur in (0, prev_default):
                self.port_spin.setValue(default_port)
        self._last_type = db_type

    # ---------- 文件选择 ----------
    def _pick_sqlite(self) -> None:
        start_dir = ""
        if self.path_edit.text():
            start_dir = str(Path(self.path_edit.text()).parent)

        if self._current_type_key() == "duckdb":
            title = "选择 DuckDB 数据库文件"
            filt = "DuckDB (*.duckdb *.db);;所有文件 (*)"
        else:
            title = "选择 SQLite 数据库文件"
            filt = "SQLite (*.db *.sqlite *.sqlite3);;所有文件 (*)"

        path, _ = QFileDialog.getOpenFileName(self, title, start_dir, filt)
        if path:
            self.path_edit.setText(path)

    def _pick_ssh_key(self) -> None:
        start_dir = ""
        if self.ssh_key_edit.text():
            start_dir = str(Path(self.ssh_key_edit.text()).parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 SSH 私钥文件", start_dir,
            "私钥文件 (id_rsa id_ed25519 id_ecdsa *.pem *.ppk *.key);;所有文件 (*)",
        )
        if path:
            self.ssh_key_edit.setText(path)

    # ---------- 结果 ----------
    def result_config(self) -> ConnectionConfig:
        # 只有高级选项开启了，SSH 配置才生效
        advanced_on = self.advanced_box.isChecked()
        ssh_on = self.ssh_group.isChecked() and advanced_on

        return ConnectionConfig(
            name=self.name_edit.text().strip() or "未命名连接",
            db_type=self._current_type_key(),
            host=self.host_edit.text().strip(),
            port=self.port_spin.value(),
            user=self.user_edit.text().strip(),
            password=self.pwd_edit.text(),
            database=self.db_edit.text().strip(),
            sqlite_path=self.path_edit.text().strip(),
            charset=self.charset_edit.text().strip(),
            query_timeout=self.timeout_spin.value(),
            group=self.group_edit.text().strip(),
            color=str(self.color_combo.currentData() or ""),
            readonly=self.readonly_box.isChecked(),
            ssh_host=self.ssh_host_edit.text().strip() if ssh_on else "",
            ssh_port=int(self.ssh_port_spin.value()) if ssh_on else 22,
            ssh_user=self.ssh_user_edit.text().strip() if ssh_on else "",
            ssh_password=self.ssh_pwd_edit.text() if ssh_on else "",
            ssh_key_file=self.ssh_key_edit.text().strip() if ssh_on else "",
            ssh_use_agent=(
                self.ssh_agent_box.isChecked() if ssh_on else False
            ),
        )

    # ---------- 校验 ----------
    def _reset_field_styles(self) -> None:
        for w in (
            self.name_edit, self.host_edit, self.user_edit, self.path_edit,
            self.ssh_host_edit, self.ssh_user_edit, self.ssh_key_edit,
        ):
            _set_error(w, False)

    def _validate_ssh_fields(self) -> tuple[str, QWidget | None] | None:
        if not self.advanced_box.isChecked() or not self.ssh_group.isChecked():
            return None
        if not self.ssh_host_edit.text().strip():
            return "启用 SSH 隧道时 SSH 主机不能为空。", self.ssh_host_edit
        if not (0 < self.ssh_port_spin.value() <= 65535):
            return "SSH 端口必须在 1~65535 之间。", None
        if not self.ssh_user_edit.text().strip():
            return "启用 SSH 隧道时必须填写 SSH 用户名。", self.ssh_user_edit
        if not self.ssh_agent_box.isChecked():
            if not self.ssh_pwd_edit.text() and not self.ssh_key_edit.text().strip():
                return (
                    "启用 SSH 隧道时，SSH 密码、私钥文件或「使用 ssh-agent」"
                    "至少填写一项。",
                    self.ssh_key_edit,
                )
        return None

    def _on_accept(self) -> None:
        self._reset_field_styles()

        ssh_err = self._validate_ssh_fields()
        if ssh_err is not None:
            msg, widget = ssh_err
            if isinstance(widget, QLineEdit):
                _set_error(widget, True)
                widget.setFocus()
            QMessageBox.warning(self, "参数不合法", msg)
            return

        cfg = self.result_config()
        err = cfg.validate()
        if err:
            if "连接名" in err:
                _set_error(self.name_edit, True)
                self.name_edit.setFocus()
            elif "主机" in err:
                _set_error(self.host_edit, True)
                self.host_edit.setFocus()
            elif "用户名" in err:
                _set_error(self.user_edit, True)
                self.user_edit.setFocus()
            elif "端口" in err:
                self.port_spin.setFocus()
            elif "SQLite" in err or "DuckDB" in err:
                _set_error(self.path_edit, True)
                self.path_edit.setFocus()
            QMessageBox.warning(self, "参数不合法", err)
            return

        if cfg.db_type in _FILE_DB_TYPES and not Path(cfg.sqlite_path).exists():
            label = "DuckDB" if cfg.db_type == "duckdb" else "SQLite"
            if QMessageBox.question(
                self, "文件不存在",
                f"{label} 文件不存在：\n{cfg.sqlite_path}\n\n"
                "继续保存吗？（首次连接时会尝试创建）",
            ) != QMessageBox.Yes:
                return
        self.accept()

    # ---------- 测试连接（异步） ----------
    def _test(self) -> None:
        if self._test_worker is not None:
            return
        self._reset_field_styles()

        ssh_err = self._validate_ssh_fields()
        if ssh_err is not None:
            msg, widget = ssh_err
            if isinstance(widget, QLineEdit):
                _set_error(widget, True)
                widget.setFocus()
            QMessageBox.warning(self, "参数不合法", msg)
            return

        cfg = self.result_config()
        err = cfg.validate()
        if err:
            QMessageBox.warning(self, "参数不合法", err)
            return

        if cfg.ssh_host and not ssh_tunnel_available():
            QMessageBox.critical(
                self, "缺少依赖",
                "已启用 SSH 隧道，但未安装 sshtunnel 库。\n\n"
                "请执行：pip install sshtunnel\n"
                "（或关闭「SSH 隧道」后重试）",
            )
            return

        self.test_btn.setEnabled(False)
        self.test_btn.setText("测试中…")
        worker = _ConnectionTestWorker(cfg)
        worker.signals.done.connect(self._on_test_done)
        self._test_worker = worker
        QThreadPool.globalInstance().start(worker)

    def _on_test_done(self, ok: bool, message: str) -> None:
        try:
            self.test_btn.setEnabled(True)
            self.test_btn.setText("测试连接")
        except RuntimeError:
            return
        self._test_worker = None
        if not self.isVisible():
            return
        if ok:
            cfg = self.result_config()
            if cfg.ssh_host:
                via = (
                    "ssh-agent"
                    if cfg.ssh_use_agent
                    else (cfg.ssh_key_file or "密码")
                )
                QMessageBox.information(
                    self, "成功",
                    "连接成功！\n\n"
                    f"（已通过 SSH 隧道 {cfg.ssh_user}@{cfg.ssh_host}:"
                    f"{cfg.ssh_port}，认证：{via}）",
                )
            else:
                QMessageBox.information(self, "成功", "连接成功！")
        else:
            QMessageBox.critical(self, "连接失败", message)

    def done(self, code: int) -> None:
        if self._test_worker is not None:
            try:
                self.test_btn.setEnabled(True)
                self.test_btn.setText("测试连接")
            except RuntimeError:
                pass
        super().done(code)

    # ---------- 错误翻译 ----------
    @staticmethod
    def _format_ssh_error(detail: str, cfg: ConnectionConfig) -> str:
        lines = [
            "SSH 隧道建立失败。",
            "",
            f"SSH 主机：{cfg.ssh_host}:{cfg.ssh_port}",
            f"SSH 用户：{cfg.ssh_user or '(未填写)'}",
        ]
        if cfg.ssh_use_agent:
            lines.append("认证方式：ssh-agent / Pageant")
        elif cfg.ssh_key_file:
            lines.append(f"私钥文件：{cfg.ssh_key_file}")
        else:
            lines.append("认证方式：密码")
        lines += [
            "",
            f"原因：{detail}",
            "",
            "请检查：",
            "• SSH 主机地址 / 端口是否正确、是否可达",
            "• SSH 用户名 / 密码 / 私钥是否正确",
            "• 私钥是否已加入远端 ~/.ssh/authorized_keys",
            "• agent 模式下是否已 ssh-add / 已启动 Pageant",
            "• 依赖是否安装：pip install sshtunnel",
        ]
        return "\n".join(lines)

    @staticmethod
    def _translate_connect_error(exc: Exception, cfg: ConnectionConfig) -> str:
        text = str(exc)

        if text.startswith("SSH:"):
            return ConnectionDialog._format_ssh_error(text[4:].strip(), cfg)
        if text.startswith("SSH "):
            return ConnectionDialog._format_ssh_error(text[4:].strip(), cfg)

        _drv_hints = {
            "pyodbc": (
                "缺少 SQL Server 驱动 pyodbc。\n\n"
                "源码运行请执行：\n"
                "    pip install pyodbc\n"
                "并额外安装系统级的 Microsoft ODBC Driver for SQL Server\n"
                "（仅 pip 装 pyodbc 不足以连接 SQL Server）。\n\n"
                "如果你使用的是打包好的 exe，说明打包时未包含该驱动，\n"
                "请联系打包者重新构建。"
            ),
            "oracledb": (
                "缺少 Oracle 驱动 oracledb。\n\n"
                "源码运行：pip install oracledb\n"
                "（thin 模式为纯 Python，无需 Oracle 客户端；\n"
                " 若要连接 Oracle 11g 及更早版本，还需安装 Oracle Instant Client 并使用 thick 模式。）"
            ),
            "psycopg2": (
                "缺少 PostgreSQL 驱动 psycopg2。\n\n"
                "源码运行：pip install psycopg2-binary\n"
                "（-binary 版本自带 libpq，无需另外安装 PostgreSQL 客户端库。）"
            ),
            "pymysql": (
                "缺少 MySQL 驱动 pymysql。\n\n"
                "源码运行：pip install pymysql"
            ),
            "fdb": (
                "缺少 Firebird 驱动。\n\n"
                "源码运行请执行：\n"
                "    pip install sqlalchemy-firebird fdb"
            ),
            "ibm_db_sa": (
                "缺少 IBM DB2 驱动。\n\n"
                "源码运行请执行：\n"
                "    pip install ibm-db-sa\n"
                "并额外安装 IBM DB2 客户端（仅 pip 装不足以连接 DB2）。"
            ),
            "ibm_db": (
                "缺少 IBM DB2 驱动。\n\n"
                "源码运行请执行：\n"
                "    pip install ibm-db ibm-db-sa\n"
                "并额外安装 IBM DB2 客户端。"
            ),
            "clickhouse": (
                "缺少 ClickHouse 驱动。\n\n"
                "源码运行请执行：\n"
                "    pip install clickhouse-sqlalchemy"
            ),
            "trino": (
                "缺少 Trino 驱动。\n\n"
                "源码运行请执行：\n"
                "    pip install 'trino[sqlalchemy]'"
            ),
            "duckdb": (
                "缺少 DuckDB 驱动。\n\n"
                "源码运行请执行：\n"
                "    pip install duckdb-engine"
            ),
        }
        for mod, hint in _drv_hints.items():
            if f"No module named '{mod}'" in text:
                return hint
            if f"No module named {mod}" in text:
                return hint

        m = re.search(r"Unknown database '([^']+)'", text)
        if "1049" in text or m:
            db_name = m.group(1) if m else (cfg.database or "未知")
            return (
                f"数据库不存在：未知的数据库「{db_name}」。\n\n"
                f"请检查「默认数据库」字段是否正确填写。\n"
                f"如果不需要指定默认库，请将该字段留空。"
            )

        if "1045" in text or "Access denied" in text:
            return (
                f"认证失败：用户名或密码错误，或该用户不允许从当前主机登录。\n\n"
                f"请检查：\n"
                f"• 用户名：{cfg.user}\n"
                f"• 主机：{cfg.host}:{cfg.port}\n"
                f"• 密码是否正确\n"
                f"• 该用户是否允许从客户端 IP 连接"
            )

        if "2003" in text or "Can't connect to MySQL server" in text:
            return (
                f"无法连接到 MySQL 服务：{cfg.host}:{cfg.port}\n\n"
                f"请检查：\n"
                f"• 目标主机是否可达\n"
                f"• 端口是否正确、MySQL 服务是否已启动\n"
                f"• 防火墙是否放行"
            )

        if "1130" in text or "is not allowed to connect" in text:
            return (
                f"该 MySQL 用户「{cfg.user}」不允许从当前客户端主机登录。\n\n"
                f"需要在 MySQL 上为该用户授权来源 IP，例如：\n"
                f"  GRANT ALL ON *.* TO '{cfg.user}'@'%' IDENTIFIED BY '密码';\n"
                f"  FLUSH PRIVILEGES;"
            )

        if "does not exist" in text and "database" in text.lower():
            return (
                f"数据库不存在或无法访问「{cfg.database or '默认库'}」。\n\n"
                f"请检查「默认数据库」字段。"
            )

        if "password authentication failed" in text.lower():
            return (
                f"认证失败：用户名或密码错误。\n\n"
                f"请检查用户名「{cfg.user}」与对应密码。"
            )

        if "could not connect to server" in text.lower():
            return (
                f"无法连接到 PostgreSQL 服务：{cfg.host}:{cfg.port}\n\n"
                f"请检查：\n"
                f"• 目标主机是否可达\n"
                f"• PostgreSQL 服务是否已启动\n"
                f"• pg_hba.conf 是否允许该客户端连接"
            )

        if cfg.db_type in _FILE_DB_TYPES and (
            "unable to open database file" in text.lower()
            or "no such file" in text.lower()
        ):
            return (
                f"无法打开数据库文件：{cfg.sqlite_path}\n\n"
                f"请检查路径是否正确、目录是否可写。"
            )

        if "Login failed" in text:
            return (
                f"SQL Server 登录失败。\n\n"
                f"请检查用户名「{cfg.user}」与密码；\n"
                f"如使用 Windows 身份验证，用户名请留空。"
            )
        if "server was not found" in text.lower() or "could not open a connection" in text.lower():
            return (
                f"无法连接到 SQL Server：{cfg.host}:{cfg.port}\n\n"
                f"请检查主机可达性、端口（默认 1433）与 TCP/IP 协议是否启用。"
            )

        if "ORA-12541" in text:
            return "无法连接到 Oracle：监听器未启动或端口不正确。"
        if "ORA-01017" in text:
            return (
                f"Oracle 认证失败：用户名「{cfg.user}」或密码错误。\n"
                f"提示：Oracle 用户名默认是大写。"
            )
        if "ORA-12514" in text:
            return (
                f"Oracle 服务名「{cfg.database}」不存在。\n"
                f"请检查「默认数据库」字段填写的是 service_name。"
            )

        return text


# ==================== 新建数据库 ====================
class CreateDatabaseDialog(QDialog):
    def __init__(self, parent=None, db_type: str = "mysql"):
        super().__init__(parent)
        self.setWindowTitle("新建数据库")
        self.setMinimumWidth(620)
        self._db_type = db_type

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：my_app_db")
        self.name_edit.setFocus()

        self.charset_edit = QLineEdit("utf8mb4")
        self.charset_edit.setPlaceholderText("utf8mb4")
        self.collation_edit = QLineEdit("utf8mb4_general_ci")
        self.collation_edit.setPlaceholderText("utf8mb4_general_ci")

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.addRow("数据库名 *", self.name_edit)

        if db_type in ("mysql", "mariadb", "tidb", "oceanbase"):
            form.addRow("字符集", self.charset_edit)
            form.addRow("排序规则", self.collation_edit)
        else:
            self.charset_edit.setParent(None)
            self.collation_edit.setParent(None)

        info = QLabel(
            f"将在 <b>{db_type}</b> 实例上执行 <code>CREATE DATABASE</code>。\n"
            "名称只允许字母、数字、下划线，且必须以字母或下划线开头。"
        )
        info.setWordWrap(True)
        info.setProperty("role", "hint")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("创建")
        ok_btn.setMinimumHeight(_BTN_H)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        cancel_btn.setMinimumHeight(_BTN_H)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setProperty("role", "sep")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(sep)
        layout.addWidget(info)
        layout.addWidget(buttons)

        _style_dialog(self)

        self._result_name = ""
        self._result_charset = ""
        self._result_collation = ""

    def _on_accept(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self._warn("数据库名不能为空。", self.name_edit)
            return
        if not _IDENT_RE.match(name):
            self._warn(
                "数据库名只允许字母、数字、下划线、$、#、@，\n"
                "且必须以字母或下划线开头。",
                self.name_edit,
            )
            return
        if len(name) > 64:
            self._warn("数据库名过长（不超过 64 字符）。", self.name_edit)
            return
        charset = self.charset_edit.text().strip()
        collation = self.collation_edit.text().strip()
        if charset and not _IDENT_RE.match(charset):
            self._warn("字符集包含非法字符。", self.charset_edit)
            return
        if collation and not _IDENT_RE.match(collation):
            self._warn("排序规则包含非法字符。", self.collation_edit)
            return
        self._result_name = name
        self._result_charset = charset
        self._result_collation = collation
        self.accept()

    @staticmethod
    def _warn(msg: str, widget: QLineEdit) -> None:
        _set_error(widget, True)
        widget.setFocus()
        QMessageBox.warning(widget.window(), "参数不合法", msg)

    def result_values(self) -> tuple[str, str, str]:
        return self._result_name, self._result_charset, self._result_collation


# ==================== SQL 预览 ====================
class SqlPreviewDialog(QDialog):
    def __init__(self, parent=None, sql: str = "", reason: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("SQL 执行预览")
        self.setMinimumSize(680, 420)

        title = QLabel("即将执行的 SQL：")
        title.setStyleSheet("font-weight: 600;")

        if reason:
            warn = QLabel(f"⚠ 检测到 <b>{reason}</b>，请确认后再执行。")
            warn.setProperty("role", "error")
            warn.setStyleSheet("padding: 4px 0;")
        else:
            warn = QLabel("")

        self.editor = QPlainTextEdit(sql)
        self.editor.setReadOnly(True)
        self.editor.setStyleSheet(
            "font-family: Consolas, 'JetBrains Mono', monospace; font-size: 12px;"
        )

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("执行")
        ok_btn.setMinimumHeight(_BTN_H)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        cancel_btn.setMinimumHeight(_BTN_H)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(title)
        if reason:
            layout.addWidget(warn)
        layout.addWidget(self.editor, 1)
        layout.addWidget(buttons)

        _style_dialog(self)


# ==================== 类型预设 ====================
_TYPE_OPTIONS: dict[str, list[str]] = {
    "mysql": [
        "INT", "BIGINT", "SMALLINT", "TINYINT", "MEDIUMINT",
        "VARCHAR", "CHAR", "TEXT", "MEDIUMTEXT", "LONGTEXT",
        "DECIMAL", "FLOAT", "DOUBLE",
        "DATE", "TIME", "DATETIME", "TIMESTAMP", "YEAR",
        "BOOLEAN", "JSON", "BLOB",
    ],
    "postgresql": [
        "INTEGER", "BIGINT", "SMALLINT", "SERIAL", "BIGSERIAL",
        "VARCHAR", "CHAR", "TEXT",
        "NUMERIC", "REAL", "DOUBLE PRECISION",
        "DATE", "TIME", "TIMESTAMP", "TIMESTAMPTZ",
        "BOOLEAN", "JSON", "JSONB", "UUID", "BYTEA",
    ],
    "sqlite": ["INTEGER", "TEXT", "REAL", "BLOB", "NUMERIC"],
    "mssql": [
        "INT", "BIGINT", "SMALLINT", "TINYINT",
        "VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "TEXT", "NTEXT",
        "DECIMAL", "FLOAT", "REAL", "MONEY",
        "DATE", "TIME", "DATETIME", "DATETIME2", "SMALLDATETIME",
        "BIT", "UNIQUEIDENTIFIER",
    ],
    "oracle": [
        "NUMBER", "FLOAT", "BINARY_FLOAT", "BINARY_DOUBLE",
        "VARCHAR2", "NVARCHAR2", "CHAR", "NCHAR",
        "CLOB", "NCLOB", "BLOB",
        "DATE", "TIMESTAMP",
    ],
    "db2": [
        "INTEGER", "BIGINT", "SMALLINT",
        "VARCHAR", "CHAR", "CLOB", "GRAPHIC", "VARGRAPHIC",
        "DECIMAL", "REAL", "DOUBLE",
        "DATE", "TIME", "TIMESTAMP",
        "BLOB",
    ],
    "firebird": [
        "INTEGER", "BIGINT", "SMALLINT",
        "VARCHAR", "CHAR", "BLOB SUB_TYPE TEXT",
        "NUMERIC", "DECIMAL", "FLOAT", "DOUBLE PRECISION",
        "DATE", "TIME", "TIMESTAMP",
        "BOOLEAN",
    ],
    "clickhouse": [
        "Int8", "Int16", "Int32", "Int64",
        "UInt8", "UInt16", "UInt32", "UInt64",
        "Float32", "Float64",
        "String", "FixedString",
        "Date", "DateTime",
        "Decimal", "UUID", "Array",
    ],
    "trino": [
        "bigint", "integer", "smallint", "tinyint",
        "varchar", "char", "varbinary",
        "double", "real", "decimal",
        "boolean",
        "date", "time", "timestamp",
        "json", "array", "map", "row", "uuid",
    ],
}

_TYPE_OPTIONS_ALIASES: dict[str, str] = {
    "mariadb": "mysql",
    "tidb": "mysql",
    "oceanbase": "mysql",
    "cockroachdb": "postgresql",
    "kingbase": "postgresql",
    "duckdb": "sqlite",
}


def _resolve_type_options_key(db_type: str) -> str:
    return _TYPE_OPTIONS_ALIASES.get(db_type, db_type)


# ==================== 新建表（两栏：左列表 + 右属性面板） ====================
class CreateTableDialog(QDialog):
    def __init__(self, parent=None, db_type: str = "mysql", schema: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("新建表")
        self.setMinimumSize(980, 660)
        self._db_type = db_type
        self._schema = schema
        self._fields: list[dict] = []
        self._current_idx = -1
        self._syncing = False

        # ---------- 顶部：表名 / 注释 ----------
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：users")
        self.name_edit.setFocus()

        self.comment_edit = QLineEdit()
        self.comment_edit.setPlaceholderText(
            "可选（MySQL / MariaDB / TiDB / OceanBase 生效）"
        )

        top_form = QFormLayout()
        top_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        top_form.setHorizontalSpacing(14)
        top_form.setVerticalSpacing(10)
        top_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        top_form.addRow("表名 *", self.name_edit)
        top_form.addRow("注释", self.comment_edit)

        # ---------- 左栏：字段列表 ----------
        self.field_list = QListWidget()
        self.field_list.setAlternatingRowColors(False)
        self.field_list.setSpacing(0)
        self.field_list.currentRowChanged.connect(self._on_field_selected)

        add_btn = QPushButton("＋  添加字段")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setMinimumHeight(_BTN_H)
        add_btn.clicked.connect(lambda: self._add_field())

        del_btn = QPushButton("－  删除")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setMinimumHeight(_BTN_H)
        del_btn.clicked.connect(self._remove_selected)

        up_btn = QToolButton()
        up_btn.setText("↑")
        up_btn.setFixedSize(_BTN_H, _BTN_H)
        up_btn.setToolTip("上移字段")
        up_btn.setCursor(Qt.PointingHandCursor)
        up_btn.clicked.connect(lambda: self._move_field(-1))

        down_btn = QToolButton()
        down_btn.setText("↓")
        down_btn.setFixedSize(_BTN_H, _BTN_H)
        down_btn.setToolTip("下移字段")
        down_btn.setCursor(Qt.PointingHandCursor)
        down_btn.clicked.connect(lambda: self._move_field(1))

        left_bar = QHBoxLayout()
        left_bar.setContentsMargins(0, 0, 0, 0)
        left_bar.setSpacing(6)
        left_bar.addWidget(add_btn)
        left_bar.addWidget(del_btn)
        left_bar.addStretch(1)
        left_bar.addWidget(up_btn)
        left_bar.addWidget(down_btn)

        left_label = QLabel("字段列表")
        left_label.setProperty("role", "hint")

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        left_layout.addWidget(left_label)
        left_layout.addWidget(self.field_list, 1)
        left_layout.addLayout(left_bar)

        # ---------- 右栏：属性面板 ----------
        self.prop_name = QLineEdit()
        self.prop_type = _compact_combo()
        self.prop_type.setEditable(True)
        self.prop_type.addItems(self._type_options())
        self.prop_length = QLineEdit()
        self.prop_length.setPlaceholderText("如 255、10,2")
        self.prop_nullable = QCheckBox("允许 NULL")
        self.prop_pk = QCheckBox("主键")
        self.prop_autoinc = QCheckBox("自增（AUTO_INCREMENT / SERIAL / IDENTITY）")
        self.prop_default = QLineEdit()
        self.prop_default.setPlaceholderText("如 0、'abc'、CURRENT_TIMESTAMP")

        self.prop_name.textChanged.connect(self._on_prop_changed)
        self.prop_type.currentTextChanged.connect(self._on_prop_changed)
        self.prop_length.textChanged.connect(self._on_prop_changed)
        self.prop_nullable.toggled.connect(self._on_prop_changed)
        self.prop_pk.toggled.connect(self._on_prop_changed)
        self.prop_autoinc.toggled.connect(self._on_prop_changed)
        self.prop_default.textChanged.connect(self._on_prop_changed)

        prop_form = QFormLayout()
        prop_form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        prop_form.setHorizontalSpacing(14)
        prop_form.setVerticalSpacing(10)
        prop_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        prop_form.addRow("名称 *", self.prop_name)
        prop_form.addRow("类型", self.prop_type)
        prop_form.addRow("长度/精度", self.prop_length)
        prop_form.addRow("", self.prop_nullable)
        prop_form.addRow("", self.prop_pk)
        prop_form.addRow("", self.prop_autoinc)
        prop_form.addRow("默认值", self.prop_default)

        prop_tip = QLabel(
            "类型可直接下拉选择或输入自定义类型；\n"
            "「长度/精度」仅当类型需要参数时填写，"
            "例如 VARCHAR 填 255、DECIMAL 填 10,2。"
        )
        prop_tip.setProperty("role", "hint")
        prop_tip.setWordWrap(True)

        right_label = QLabel("字段属性")
        right_label.setProperty("role", "hint")

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)
        right_layout.addWidget(right_label)
        right_layout.addLayout(prop_form)
        right_layout.addWidget(prop_tip)
        right_layout.addStretch(1)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([300, 600])
        splitter.setHandleWidth(6)

        # ---------- 底部：SQL 预览 ----------
        preview_label = QLabel("SQL 预览")
        preview_label.setProperty("role", "hint")

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(160)
        self.preview.setStyleSheet(
            "font-family: Consolas, 'JetBrains Mono', monospace; font-size: 12px;"
        )

        # ---------- 底部按钮 ----------
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("创建")
        ok_btn.setMinimumHeight(_BTN_H)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        cancel_btn.setMinimumHeight(_BTN_H)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        # ---------- 总布局 ----------
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addLayout(top_form)
        layout.addWidget(splitter, 1)
        layout.addWidget(preview_label)
        layout.addWidget(self.preview)
        layout.addWidget(buttons)

        self._add_field(name="id", ctype=self._default_pk_type(),
                        pk=True, autoinc=True)
        self._add_field()

        self.name_edit.textChanged.connect(self._refresh_preview)
        self.comment_edit.textChanged.connect(self._refresh_preview)
        self._refresh_preview()

        _style_dialog(self)

    # ---------- 类型 / 默认值 ----------
    def _default_pk_type(self) -> str:
        return {
            "mysql": "INT", "mariadb": "INT", "tidb": "INT", "oceanbase": "INT",
            "postgresql": "INTEGER", "cockroachdb": "INTEGER",
            "kingbase": "INTEGER",
            "sqlite": "INTEGER", "duckdb": "INTEGER",
            "mssql": "INT",
            "oracle": "NUMBER",
            "db2": "INTEGER",
            "firebird": "INTEGER",
            "clickhouse": "Int64",
            "trino": "bigint",
        }.get(self._db_type, "INT")

    def _type_options(self) -> list[str]:
        key = _resolve_type_options_key(self._db_type)
        return _TYPE_OPTIONS.get(key, _TYPE_OPTIONS["mysql"])

    # ---------- 字段 CRUD ----------
    def _add_field(self, name="", ctype=None, length="", nullable=True,
                   pk=False, autoinc=False, default="") -> None:
        field = {
            "name": name,
            "type": ctype or (self._type_options() or ["VARCHAR"])[0],
            "length": length,
            "nullable": nullable,
            "pk": pk,
            "autoinc": autoinc,
            "default": default,
        }
        self._fields.append(field)
        self._rebuild_list()
        self.field_list.setCurrentRow(len(self._fields) - 1)
        self._refresh_preview()

    def _remove_selected(self) -> None:
        idx = self.field_list.currentRow()
        if idx < 0 or idx >= len(self._fields):
            return
        del self._fields[idx]
        self._current_idx = -1
        self._rebuild_list()
        if self._fields:
            self.field_list.setCurrentRow(min(idx, len(self._fields) - 1))
        self._refresh_preview()

    def _move_field(self, direction: int) -> None:
        idx = self.field_list.currentRow()
        if idx < 0 or idx >= len(self._fields):
            return
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(self._fields):
            return
        self._fields[idx], self._fields[new_idx] = (
            self._fields[new_idx], self._fields[idx]
        )
        self._rebuild_list()
        self.field_list.setCurrentRow(new_idx)
        self._refresh_preview()

    # ---------- 列表渲染 ----------
    @staticmethod
    def _field_label(f: dict) -> str:
        text = f["name"] or "(未命名)"
        if f.get("type"):
            text += f"    {f['type']}"
            if f.get("length"):
                text += f"({f['length']})"
        badges: list[str] = []
        if f.get("pk"):
            badges.append("PK")
        if f.get("autoinc"):
            badges.append("AI")
        if not f.get("nullable", True):
            badges.append("NN")
        if badges:
            text += f"    [{' · '.join(badges)}]"
        return text

    def _rebuild_list(self) -> None:
        current = self.field_list.currentRow()
        self._syncing = True
        try:
            self.field_list.clear()
            for f in self._fields:
                self.field_list.addItem(self._field_label(f))
            if 0 <= current < len(self._fields):
                self.field_list.setCurrentRow(current)
        finally:
            self._syncing = False

    # ---------- 选中 → 表单 ----------
    def _on_field_selected(self, row: int) -> None:
        if self._syncing:
            return
        self._current_idx = row
        if row < 0 or row >= len(self._fields):
            return
        f = self._fields[row]
        self._syncing = True
        try:
            self.prop_name.setText(f["name"])
            self.prop_type.setCurrentText(f["type"])
            self.prop_length.setText(f["length"])
            self.prop_nullable.setChecked(f["nullable"])
            self.prop_pk.setChecked(f["pk"])
            self.prop_autoinc.setChecked(f["autoinc"])
            self.prop_default.setText(f["default"])
        finally:
            self._syncing = False

    # ---------- 表单 → 字段 ----------
    def _on_prop_changed(self, *_args) -> None:
        if self._syncing:
            return
        idx = self._current_idx
        if idx < 0 or idx >= len(self._fields):
            return
        f = self._fields[idx]
        f["name"] = self.prop_name.text().strip()
        f["type"] = self.prop_type.currentText().strip()
        f["length"] = self.prop_length.text().strip()
        f["nullable"] = self.prop_nullable.isChecked()
        f["pk"] = self.prop_pk.isChecked()
        f["autoinc"] = self.prop_autoinc.isChecked()
        f["default"] = self.prop_default.text().strip()

        self._syncing = True
        try:
            item = self.field_list.item(idx)
            if item is not None:
                item.setText(self._field_label(f))
        finally:
            self._syncing = False
        self._refresh_preview()

    # ---------- 收集 / SQL 生成 ----------
    def _collect_columns(self) -> list[dict]:
        return [dict(f) for f in self._fields if f["name"]]

    def result_sql(self) -> str:
        table = self.name_edit.text().strip()
        if not table:
            return ""
        db = self._db_type
        q = lambda n: quote_ident(n, db)  # noqa: E731
        qualified = qualify_table(table, self._schema, db)
        cols = self._collect_columns()
        if not cols:
            return ""
        lines: list[str] = []
        pk_cols: list[str] = []
        for c in cols:
            parts = [q(c["name"])]
            ctype = c["type"].upper()
            length = c["length"]
            autoinc = c["autoinc"]

            if autoinc:
                if db in ("postgresql", "cockroachdb", "kingbase"):
                    ctype = (
                        "BIGSERIAL"
                        if ctype in ("BIGINT", "BIGSERIAL")
                        else "SERIAL"
                    )
                elif db in ("sqlite", "duckdb"):
                    ctype = "INTEGER"

            if length:
                ctype = f"{ctype}({length})"
            parts.append(ctype)

            if autoinc and db == "mssql":
                parts.append("IDENTITY(1,1)")
            if autoinc and db == "oracle":
                parts.append("GENERATED BY DEFAULT AS IDENTITY")

            if not c["nullable"]:
                parts.append("NOT NULL")

            if c["default"] and not (autoinc and db == "oracle"):
                parts.append(f"DEFAULT {c['default']}")

            if autoinc and db in ("mysql", "mariadb", "tidb", "oceanbase"):
                parts.append("AUTO_INCREMENT")

            if c["pk"]:
                pk_cols.append(c["name"])
            lines.append("  " + " ".join(parts))

        if db in ("sqlite", "duckdb"):
            for i, c in enumerate(cols):
                if c["autoinc"]:
                    lines[i] += " PRIMARY KEY AUTOINCREMENT"
                    if c["name"] in pk_cols:
                        pk_cols.remove(c["name"])
                    break
        if pk_cols:
            lines.append("  PRIMARY KEY (" + ", ".join(q(c) for c in pk_cols) + ")")

        sql = f"CREATE TABLE {qualified} (\n" + ",\n".join(lines) + "\n)"
        if db in ("mysql", "mariadb", "tidb", "oceanbase") \
                and self.comment_edit.text().strip():
            cmt = self.comment_edit.text().strip().replace("'", "''")
            sql += f" COMMENT='{cmt}'"
        return sql + ";"

    def _refresh_preview(self, *_args) -> None:
        sql = self.result_sql()
        self.preview.setPlainText(sql or "-- 请填写表名和至少一列")

    # ---------- 校验 ----------
    def _on_accept(self) -> None:
        table = self.name_edit.text().strip()
        if not table:
            self._warn("表名不能为空。", self.name_edit)
            return
        if not _IDENT_RE.match(table):
            self._warn(
                "表名只允许字母、数字、下划线、$、#、@，\n"
                "且必须以字母或下划线开头。",
                self.name_edit,
            )
            return
        cols = self._collect_columns()
        if not cols:
            QMessageBox.warning(self, "参数不合法", "至少需要定义一列。")
            return
        seen: set[str] = set()
        for c in cols:
            name = c["name"]
            if name.lower() in seen:
                QMessageBox.warning(self, "参数不合法", f"列名重复：{name}")
                return
            seen.add(name.lower())
            if not _IDENT_RE.match(name):
                QMessageBox.warning(self, "参数不合法", f"列名不合法：{name}")
                return
        self.accept()

    @staticmethod
    def _warn(msg: str, widget: QLineEdit) -> None:
        _set_error(widget, True)
        widget.setFocus()
        QMessageBox.warning(widget.window(), "参数不合法", msg)


# ==================== ALTER TABLE ====================
class AlterTableDialog(QDialog):
    """编辑表结构，生成 ALTER TABLE 语句。"""

    def __init__(
        self,
        parent=None,
        db_type: str = "mysql",
        schema: str | None = None,
        table: str = "",
        current_columns: list[dict] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"修改表结构 - {table}")
        self.setMinimumSize(960, 620)
        self._db_type = db_type
        self._schema = schema
        self._table = table
        self._orig_columns = list(current_columns or [])
        self._orig_names = {c["name"] for c in self._orig_columns}

        info = QLabel(
            f"表：<b>{table}</b>　数据库类型：<b>{db_type}</b>"
            + (f"　模式：<b>{schema}</b>" if schema else "")
        )
        info.setProperty("role", "hint")

        self.col_table = QTableWidget(0, 6)
        self.col_table.setHorizontalHeaderLabels(
            ["原列名", "新列名", "类型", "允许 NULL", "默认值", "标记删除"]
        )
        header = self.col_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        for c in (1, 2, 3, 4, 5):
            header.setSectionResizeMode(c, QHeaderView.Stretch)
        self.col_table.verticalHeader().setDefaultSectionSize(_INLINE_ROW_HEIGHT)
        self.col_table.setAlternatingRowColors(True)
        self.col_table.setShowGrid(False)
        self.col_table.setFrameShape(QTableWidget.NoFrame)

        add_btn = QPushButton("＋  添加列")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setMinimumHeight(_BTN_H)
        add_btn.clicked.connect(lambda: self._add_row(is_new=True))

        tip = QLabel(
            "使用说明：<br>"
            "• 修改「新列名」→ 生成 RENAME COLUMN 语句<br>"
            "• 修改「类型」→ 生成 ALTER COLUMN 类型语句<br>"
            "• 勾选「标记删除」→ 生成 DROP COLUMN 语句<br>"
            "• 允许 NULL / 默认值 的变化同样会生成对应语句"
        )
        tip.setProperty("role", "hint")
        tip.setWordWrap(True)

        preview_label = QLabel("SQL 预览")
        preview_label.setProperty("role", "hint")

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(150)
        self.preview.setStyleSheet(
            "font-family: Consolas, 'JetBrains Mono', monospace; font-size: 12px;"
        )

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("执行")
        ok_btn.setMinimumHeight(_BTN_H)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        cancel_btn.setMinimumHeight(_BTN_H)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addWidget(info)
        layout.addWidget(self.col_table, 1)
        layout.addWidget(add_btn, alignment=Qt.AlignLeft)
        layout.addWidget(tip)
        layout.addWidget(preview_label)
        layout.addWidget(self.preview)
        layout.addWidget(buttons)

        for col in self._orig_columns:
            self._add_row(
                name=col.get("name", ""),
                ctype=str(col.get("type", "")),
                nullable=bool(col.get("nullable", True)),
                default=str(col.get("default") or ""),
            )
        self.col_table.itemChanged.connect(self._refresh_preview)

        _style_dialog(self)

    def _type_options(self) -> list[str]:
        key = _resolve_type_options_key(self._db_type)
        return _TYPE_OPTIONS.get(key, _TYPE_OPTIONS["mysql"])

    def _add_row(self, name="", ctype=None, nullable=True, default="", is_new=False):
        r = self.col_table.rowCount()
        self.col_table.insertRow(r)
        orig = QTableWidgetItem("" if is_new else name)
        orig.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self.col_table.setItem(r, 0, orig)
        self.col_table.setItem(r, 1, QTableWidgetItem("" if is_new else name))
        combo = _compact_combo()
        combo.setEditable(True)
        combo.addItems(self._type_options())
        if ctype:
            combo.setCurrentText(ctype)
        combo.currentTextChanged.connect(self._refresh_preview)
        self.col_table.setCellWidget(r, 2, combo)
        for col, checked in ((3, nullable), (5, False)):
            item = QTableWidgetItem()
            item.setFlags(
                Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable
            )
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
            self.col_table.setItem(r, col, item)
        self.col_table.setItem(r, 4, QTableWidgetItem(default))
        self._refresh_preview()

    def _is_checked(self, row: int, col: int) -> bool:
        it = self.col_table.item(row, col)
        return it is not None and it.checkState() == Qt.Checked

    def _collect(self) -> list[dict]:
        out = []
        for r in range(self.col_table.rowCount()):
            orig = self.col_table.item(r, 0)
            new = self.col_table.item(r, 1)
            combo = self.col_table.cellWidget(r, 2)
            default = self.col_table.item(r, 4)
            out.append({
                "orig": orig.text().strip() if orig else "",
                "new": new.text().strip() if new else "",
                "type": combo.currentText().strip() if combo else "",
                "nullable": self._is_checked(r, 3),
                "default": default.text().strip() if default else "",
                "drop": self._is_checked(r, 5),
            })
        return out

    def result_sql(self) -> str:
        db = self._db_type
        q = lambda n: quote_ident(n, db)  # noqa: E731
        qualified = qualify_table(self._table, self._schema, db)
        cols = self._collect()
        orig_map = {c["name"]: c for c in self._orig_columns}
        stmts: list[str] = []
        is_mysql_family = db in ("mysql", "mariadb", "tidb", "oceanbase")
        for c in cols:
            if not c["new"]:
                continue
            if c["drop"] and c["orig"]:
                stmts.append(f"ALTER TABLE {qualified} DROP COLUMN {q(c['orig'])};")
                continue
            if not c["orig"]:
                parts = [q(c["new"]), c["type"].upper() or "VARCHAR"]
                if not c["nullable"]:
                    parts.append("NOT NULL")
                if c["default"]:
                    parts.append(f"DEFAULT {c['default']}")
                stmts.append(
                    f"ALTER TABLE {qualified} ADD COLUMN " + " ".join(parts) + ";"
                )
            else:
                old = orig_map.get(c["orig"], {})
                old_type = str(old.get("type", "")).upper()
                old_nullable = bool(old.get("nullable", True))
                old_default = str(old.get("default") or "")

                type_changed = bool(c["type"]) and c["type"].upper() != old_type
                name_changed = c["new"] != c["orig"]
                nullable_changed = c["nullable"] != old_nullable
                default_changed = c["default"] != old_default

                if is_mysql_family:
                    if name_changed or type_changed or nullable_changed or default_changed:
                        col_type = (c["type"] or old_type or "VARCHAR(255)").upper()
                        line = (
                            f"ALTER TABLE {qualified} CHANGE COLUMN "
                            f"{q(c['orig'])} {q(c['new'])} {col_type}"
                        )
                        if not c["nullable"]:
                            line += " NOT NULL"
                        effective_default = c["default"] if default_changed else old_default
                        if effective_default:
                            line += f" DEFAULT {effective_default}"
                        stmts.append(line + ";")
                else:
                    if name_changed:
                        stmts.append(
                            f"ALTER TABLE {qualified} RENAME COLUMN "
                            f"{q(c['orig'])} TO {q(c['new'])};"
                        )
                    if type_changed:
                        stmts.append(
                            f"ALTER TABLE {qualified} ALTER COLUMN "
                            f"{q(c['new'])} TYPE {c['type'].upper()};"
                        )
                    if nullable_changed:
                        if c["nullable"]:
                            stmts.append(
                                f"ALTER TABLE {qualified} ALTER COLUMN "
                                f"{q(c['new'])} DROP NOT NULL;"
                            )
                        else:
                            stmts.append(
                                f"ALTER TABLE {qualified} ALTER COLUMN "
                                f"{q(c['new'])} SET NOT NULL;"
                            )
                    if default_changed:
                        if c["default"]:
                            stmts.append(
                                f"ALTER TABLE {qualified} ALTER COLUMN "
                                f"{q(c['new'])} SET DEFAULT {c['default']};"
                            )
                        else:
                            stmts.append(
                                f"ALTER TABLE {qualified} ALTER COLUMN "
                                f"{q(c['new'])} DROP DEFAULT;"
                            )
        return "\n".join(stmts)

    def _refresh_preview(self, *_a) -> None:
        sql = self.result_sql()
        self.preview.setPlainText(sql or "-- 无修改")

    def _on_accept(self) -> None:
        if not self.result_sql().strip():
            QMessageBox.information(self, "提示", "没有任何结构变化。")
            return
        self.accept()


# ==================== 索引管理 ====================
class IndexManagerDialog(QDialog):
    def __init__(
        self,
        parent=None,
        db_type: str = "mysql",
        schema: str | None = None,
        table: str = "",
        indexes: list[dict] | None = None,
        columns: list[str] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"索引管理 - {table}")
        self.setMinimumSize(820, 560)
        self._db_type = db_type
        self._schema = schema
        self._table = table
        self._columns = list(columns or [])
        self._indexes = list(indexes or [])

        # ---------- 现有索引表 ----------
        existing_label = QLabel("现有索引")
        existing_label.setProperty("role", "hint")

        self.table = QTableWidget(len(self._indexes), 4)
        self.table.setHorizontalHeaderLabels(["索引名", "列", "唯一", "主键"])
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setDefaultSectionSize(28)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableWidget.NoFrame)
        self._render_indexes()

        # ---------- 新增索引 ----------
        new_label = QLabel("新增索引")
        new_label.setProperty("role", "hint")

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("索引名（可选，留空自动生成）")
        self.cols_edit = QLineEdit()
        self.cols_edit.setPlaceholderText("列名（逗号分隔），例如：name,age")
        self.unique_box = QCheckBox("唯一索引")

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.addRow("索引名", self.name_edit)
        form.addRow("包含列", self.cols_edit)
        form.addRow("", self.unique_box)

        add_btn = QPushButton("＋  添加索引")
        add_btn.setCursor(Qt.PointingHandCursor)
        add_btn.setMinimumHeight(_BTN_H)
        add_btn.clicked.connect(self._add_index)

        del_btn = QPushButton("－  删除选中索引")
        del_btn.setCursor(Qt.PointingHandCursor)
        del_btn.setMinimumHeight(_BTN_H)
        del_btn.clicked.connect(self._del_selected)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(6)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(del_btn)
        btn_row.addStretch(1)

        tip = QLabel("提示：删除主键索引不受支持；只允许删除普通 / 唯一索引。")
        tip.setProperty("role", "hint")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("执行")
        ok_btn.setMinimumHeight(_BTN_H)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        cancel_btn.setMinimumHeight(_BTN_H)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addWidget(existing_label)
        layout.addWidget(self.table, 1)
        layout.addWidget(new_label)
        layout.addLayout(form)
        layout.addLayout(btn_row)
        layout.addWidget(tip)
        layout.addWidget(buttons)

        self._pending: list[str] = []

        _style_dialog(self)

    def _render_indexes(self) -> None:
        self.table.setRowCount(len(self._indexes))
        for r, idx in enumerate(self._indexes):
            name = idx.get("name") or "(未命名)"
            cols = ", ".join(idx.get("column_names") or [])
            unique = "是" if idx.get("unique") else "否"
            is_pk = "是" if idx.get("name") == "PRIMARY" else "否"
            self.table.setItem(r, 0, QTableWidgetItem(name))
            self.table.setItem(r, 1, QTableWidgetItem(cols))
            self.table.setItem(r, 2, QTableWidgetItem(unique))
            self.table.setItem(r, 3, QTableWidgetItem(is_pk))
            for c in range(4):
                item = self.table.item(r, c)
                if item is not None:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)

    def _add_index(self) -> None:
        cols = [c.strip() for c in self.cols_edit.text().split(",") if c.strip()]
        if not cols:
            QMessageBox.warning(self, "提示", "请填写至少一列。")
            return
        unknown = [c for c in cols if c not in self._columns]
        if unknown:
            QMessageBox.warning(
                self, "提示", f"列不存在：{', '.join(unknown)}"
            )
            return
        db = self._db_type
        q = lambda n: quote_ident(n, db)  # noqa: E731
        qualified = qualify_table(self._table, self._schema, db)
        name = self.name_edit.text().strip()

        if name and not _IDENT_RE.match(name):
            QMessageBox.warning(
                self, "提示",
                "索引名只允许字母、数字、下划线、$、#、@，\n"
                "且必须以字母或下划线开头。",
            )
            return

        unique = self.unique_box.isChecked()
        if not name:
            col_part = "_".join(cols)
            name = f"idx_{self._table}_{col_part}"
            limit = 64 if db in ("mysql", "mariadb", "tidb", "oceanbase") else 63
            if len(name) > limit:
                head = name[:8]
                tail = name[-(limit - 8):]
                name = (head + tail)[:limit]

        col_part = ", ".join(q(c) for c in cols)
        is_mysql_family = db in ("mysql", "mariadb", "tidb", "oceanbase")
        if is_mysql_family:
            kw = "UNIQUE INDEX" if unique else "INDEX"
            sql = f"CREATE {kw} {q(name)} ON {qualified} ({col_part});"
        else:
            sql = (
                f"CREATE {'UNIQUE ' if unique else ''}INDEX {q(name)} "
                f"ON {qualified} ({col_part});"
            )
        self._pending.append(sql)
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(name))
        self.table.setItem(r, 1, QTableWidgetItem(", ".join(cols)))
        self.table.setItem(r, 2, QTableWidgetItem("是" if unique else "否"))
        self.table.setItem(r, 3, QTableWidgetItem("否"))
        self.name_edit.clear()
        self.cols_edit.clear()
        self.unique_box.setChecked(False)

    def _del_selected(self) -> None:
        rows = sorted(
            {i.row() for i in self.table.selectedIndexes()}, reverse=True
        )
        if not rows:
            return
        db = self._db_type
        q = lambda n: quote_ident(n, db)  # noqa: E731
        qualified = qualify_table(self._table, self._schema, db)
        is_mysql_family = db in ("mysql", "mariadb", "tidb", "oceanbase")
        for r in rows:
            item = self.table.item(r, 0)
            if item is None:
                continue
            name = item.text()
            if name == "PRIMARY":
                QMessageBox.warning(self, "提示", "不支持删除主键索引。")
                continue
            if name.startswith("(未命名)"):
                continue
            if is_mysql_family:
                sql = f"DROP INDEX {q(name)} ON {qualified};"
            else:
                prefix = (
                    f"{quote_ident(self._schema, db)}." if self._schema else ""
                )
                sql = f"DROP INDEX {prefix}{q(name)};"
            self._pending.append(sql)
            self.table.removeRow(r)

    def result_sql(self) -> list[str]:
        return list(self._pending)


# ==================== 导入列映射向导 ====================
class ImportWizardDialog(QDialog):
    """读取文件 → 列映射 → 预览 → 输出 (columns, rows, batch_size, conflict)。

    界面 v2（2024 美化重构）：
    - 顶部信息 chip 化
    - 选项分两行布局，互不挤压
    - 表格无边框圆角，行高 30
    """
    def __init__(
        self,
        parent=None,
        path: str = "",
        table: str = "",
        table_columns: list[str] | None = None,
        db_type: str = "mysql",
    ):
        super().__init__(parent)
        self.setWindowTitle(f"导入数据 - {table}")
        self.setMinimumSize(980, 800)
        self._path = path
        self._table = table
        self._table_columns = list(table_columns or [])
        self._db_type = db_type

        self._header: list[str] = []
        self._rows: list[list] = []
        self._load_error = ""
        self._loading = False

        # ---------- 顶部信息 ----------
        self.info = QLabel()
        self.info.setObjectName("importInfo")
        self.info.setWordWrap(True)

        # ---------- 第一行选项：编码 / 分隔符 / 表头 ----------
        self.encoding_combo = QComboBox()
        for label, value in (
            ("UTF-8 (推荐)", "utf-8-sig"),
            ("UTF-8 (无 BOM)", "utf-8"),
            ("GBK", "gbk"),
            ("GB18030", "gb18030"),
        ):
            self.encoding_combo.addItem(label, value)
        self.encoding_combo.setMinimumHeight(_INPUT_H)

        self.delimiter_combo = QComboBox()
        for label, value in (
            ("逗号  ,", ","),
            ("制表符  \\t", "\t"),
            ("分号  ;", ";"),
        ):
            self.delimiter_combo.addItem(label, value)
        self.delimiter_combo.setMinimumHeight(_INPUT_H)

        self.header_box = QCheckBox("首行是表头")
        self.header_box.setChecked(True)

        is_csv = self._is_csv()
        self.encoding_combo.setEnabled(is_csv)
        self.delimiter_combo.setEnabled(is_csv)

        opt_row1 = QHBoxLayout()
        opt_row1.setContentsMargins(0, 0, 0, 0)
        opt_row1.setSpacing(8)
        opt_row1.addWidget(QLabel("编码"))
        opt_row1.addWidget(self.encoding_combo, 1)
        opt_row1.addSpacing(8)
        opt_row1.addWidget(QLabel("分隔符"))
        opt_row1.addWidget(self.delimiter_combo, 1)
        opt_row1.addSpacing(8)
        opt_row1.addWidget(self.header_box)

        # ---------- 第二行选项：冲突策略 / 批量 ----------
        self.conflict_combo = QComboBox()
        for label, value in (
            ("普通插入  INSERT", "insert"),
            ("忽略冲突  INSERT IGNORE", "ignore"),
            ("替换已有  REPLACE", "replace"),
            ("冲突则更新  UPSERT", "upsert"),
        ):
            self.conflict_combo.addItem(label, value)
        self.conflict_combo.setMinimumHeight(_INPUT_H)
        self.conflict_combo.setToolTip(
            "MySQL：INSERT IGNORE / REPLACE INTO / ON DUPLICATE KEY UPDATE\n"
            "PostgreSQL：ON CONFLICT DO NOTHING / DO UPDATE\n"
            "SQLite：INSERT OR IGNORE / INSERT OR REPLACE / DO UPDATE\n"
            "说明：REPLACE / UPSERT 在 PostgreSQL / SQLite 下需要主键，"
            "否则退化为 INSERT 或 DO NOTHING。"
        )

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 1000)
        self.batch_spin.setValue(1)
        self.batch_spin.setMinimumHeight(_INPUT_H)
        self.batch_spin.setMinimumWidth(110)
        self.batch_spin.setToolTip(
            "每条 INSERT 包含的行数。1 = 每行一条；批量插入在 MySQL/PG 下更快，\n"
            "但单条语句过长可能触发包体限制。"
        )

        opt_row2 = QHBoxLayout()
        opt_row2.setContentsMargins(0, 0, 0, 0)
        opt_row2.setSpacing(8)
        opt_row2.addWidget(QLabel("冲突策略"))
        opt_row2.addWidget(self.conflict_combo, 1)
        opt_row2.addSpacing(8)
        opt_row2.addWidget(QLabel("批量大小"))
        opt_row2.addWidget(self.batch_spin)

        # ---------- 第三行：跳过已存在 ----------
        self.skip_existing_box = QCheckBox(
            "跳过目标表中已存在的行（按主键 / 唯一键判重）"
        )
        self.skip_existing_box.setToolTip(
            "导入前先读取目标表的主键或唯一索引值，跳过重复的行。\n"
            "适用场景：目标表已有部分数据，只想补录新数据。\n\n"
            "注意：目标表必须有主键或唯一约束。\n"
            "没有约束时无法判重，会退化为按冲突策略执行（建议用 INSERT IGNORE / UPSERT）。"
        )

        opt_row3 = QHBoxLayout()
        opt_row3.setContentsMargins(0, 0, 0, 0)
        opt_row3.addWidget(self.skip_existing_box)
        opt_row3.addStretch(1)

        # ---------- 列映射表 ----------
        mapping_label = QLabel("列映射")
        mapping_label.setObjectName("sectionLabel")

        mapping_hint = QLabel("勾选「忽略」即不导入该列")
        mapping_hint.setProperty("role", "hint")

        mapping_head = QHBoxLayout()
        mapping_head.setContentsMargins(0, 0, 0, 0)
        mapping_head.addWidget(mapping_label)
        mapping_head.addSpacing(8)
        mapping_head.addWidget(mapping_hint)
        mapping_head.addStretch(1)

        self.map_table = QTableWidget(0, 4)
        self.map_table.setHorizontalHeaderLabels(
            ["文件列", "映射到", "示例值", "忽略"]
        )
        hdr = self.map_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.map_table.verticalHeader().setDefaultSectionSize(_INLINE_ROW_HEIGHT)
        self.map_table.setAlternatingRowColors(True)
        self.map_table.setShowGrid(False)
        self.map_table.setFrameShape(QTableWidget.NoFrame)

        # ---------- 数据预览 ----------
        preview_head = QHBoxLayout()
        preview_head.setContentsMargins(0, 0, 0, 0)
        preview_label = QLabel("数据预览")
        preview_label.setObjectName("sectionLabel")
        preview_hint = QLabel("前 20 行")
        preview_hint.setProperty("role", "hint")
        preview_head.addWidget(preview_label)
        preview_head.addSpacing(8)
        preview_head.addWidget(preview_hint)
        preview_head.addStretch(1)

        self.preview_table = QTableWidget(0, 0)
        self.preview_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.preview_table.setAlternatingRowColors(True)
        self.preview_table.setMaximumHeight(220)
        self.preview_table.setShowGrid(False)
        self.preview_table.setFrameShape(QTableWidget.NoFrame)
        self.preview_table.verticalHeader().setDefaultSectionSize(24)
        self.preview_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.Interactive
        )
        self.preview_table.horizontalHeader().setStretchLastSection(True)

        # ---------- 底部按钮 ----------
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("导入")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.setMinimumHeight(_BTN_H)
        ok_btn.setMinimumWidth(100)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        cancel_btn.setMinimumHeight(_BTN_H)
        cancel_btn.setMinimumWidth(80)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        # ---------- 总布局 ----------
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addWidget(self.info)
        layout.addLayout(opt_row1)
        layout.addLayout(opt_row2)
        layout.addLayout(opt_row3)
        layout.addSpacing(4)
        layout.addLayout(mapping_head)
        layout.addWidget(self.map_table, 1)
        layout.addSpacing(4)
        layout.addLayout(preview_head)
        layout.addWidget(self.preview_table)
        layout.addWidget(buttons)

        self.encoding_combo.currentIndexChanged.connect(self._reload_file)
        self.delimiter_combo.currentIndexChanged.connect(self._reload_file)
        self.header_box.toggled.connect(self._reload_file)

        _style_dialog(self, extra=f"""
            QLabel#importInfo {{
                background: {theme_manager.palette.surface_alt};
                border: 1px solid {theme_manager.palette.border};
                border-radius: 10px;
                padding: 10px 14px;
                color: {theme_manager.palette.text};
            }}
            QLabel#sectionLabel {{
                font-weight: 600;
                color: {theme_manager.palette.text};
            }}
            QPushButton#primaryBtn {{
                background: {theme_manager.palette.accent};
                color: #ffffff;
                border: 1px solid {theme_manager.palette.accent};
                font-weight: 600;
            }}
            QPushButton#primaryBtn:hover {{
                background: {theme_manager.palette.accent_hover};
                border-color: {theme_manager.palette.accent_hover};
            }}
            QPushButton#primaryBtn:pressed {{
                background: {theme_manager.palette.accent_pressed};
            }}
        """)

        self._read_file()

    def _is_csv(self) -> bool:
        return Path(self._path).suffix.lower() not in (".xlsx", ".xlsm", ".json")

    def _reload_file(self, *_args) -> None:
        if self._loading:
            return
        self._read_file()

    def _read_file(self) -> None:
        self._loading = True
        try:
            self._do_read()
        finally:
            self._loading = False

    def _do_read(self) -> None:
        self._header = []
        self._rows = []
        self._load_error = ""

        encoding = self.encoding_combo.currentData() or "utf-8-sig"
        delimiter = self.delimiter_combo.currentData() or ","
        has_header = self.header_box.isChecked()

        try:
            from . import io_tools
            self._header, self._rows = io_tools.read_any(
                self._path,
                encoding=encoding,
                delimiter=delimiter,
                has_header=has_header,
            )
        except Exception as exc:  # noqa: BLE001
            self._load_error = f"文件读取失败：{exc}"
            self.info.setText(
                f"<b style='color:red;'>读取失败：{exc}</b><br>"
                "请检查文件编码、分隔符、以及「首行是表头」选项。"
            )
            self.map_table.setRowCount(0)
            self.preview_table.setRowCount(0)
            self.preview_table.setColumnCount(0)
            return

        if not self._header:
            self._load_error = "文件没有可读取的内容（表头为空）。"
            self.info.setText(
                "<b style='color:red;'>文件没有可读取的内容</b><br>"
                "请检查文件是否为空，或分隔符是否与文件一致。"
            )
            self.map_table.setRowCount(0)
            self.preview_table.setRowCount(0)
            self.preview_table.setColumnCount(0)
            return

        self.info.setText(
            f"文件：<b>{self._path}</b>　"
            f"行数：<b>{len(self._rows)}</b>　"
            f"文件列：<b>{len(self._header)}</b>　"
            f"目标表列：<b>{len(self._table_columns)}</b>"
        )
        self._build_mapping()
        self._build_preview()

    def _build_mapping(self) -> None:
        lower_map = {c.lower(): c for c in self._table_columns}
        self.map_table.setRowCount(0)
        for i, col in enumerate(self._header):
            r = self.map_table.rowCount()
            self.map_table.insertRow(r)

            src = QTableWidgetItem(str(col))
            src.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.map_table.setItem(r, 0, src)

            combo = _compact_combo()
            combo.addItem("（忽略）", "")
            for tc in self._table_columns:
                combo.addItem(tc, tc)
            matched = lower_map.get(str(col).lower())
            if matched:
                combo.setCurrentIndex(combo.findData(matched))
            self.map_table.setCellWidget(r, 1, combo)

            sample = ""
            for row in self._rows[:3]:
                if i < len(row) and row[i] not in (None, ""):
                    sample = str(row[i])
                    break
            sample_item = QTableWidgetItem(sample[:60])
            sample_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            self.map_table.setItem(r, 2, sample_item)

            ignore = QTableWidgetItem()
            ignore.setFlags(
                Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable
            )
            ignore.setCheckState(Qt.Unchecked if matched else Qt.Checked)
            self.map_table.setItem(r, 3, ignore)

    def _build_preview(self) -> None:
        cols = list(self._header)
        self.preview_table.setColumnCount(len(cols))
        self.preview_table.setHorizontalHeaderLabels(cols)

        sample = self._rows[:20]
        self.preview_table.setRowCount(len(sample))
        for r, row in enumerate(sample):
            for c in range(len(cols)):
                v = row[c] if c < len(row) else None
                text = "" if v is None else str(v)
                if len(text) > 80:
                    text = text[:80] + "…"
                item = QTableWidgetItem(text)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.preview_table.setItem(r, c, item)

        self.preview_table.resizeColumnsToContents()
        for c in range(self.preview_table.columnCount()):
            if self.preview_table.columnWidth(c) > 220:
                self.preview_table.setColumnWidth(c, 220)

    def result(self) -> tuple[list[str], list[list], int, str]:
        """返回 (columns, rows, batch_size, conflict)。"""
        idx_map: list[tuple[int, str]] = []
        seen: set[str] = set()
        for r in range(self.map_table.rowCount()):
            ignore = self.map_table.item(r, 3)
            if ignore is not None and ignore.checkState() == Qt.Checked:
                continue
            combo = self.map_table.cellWidget(r, 1)
            target = combo.currentData() if combo else ""
            if not target:
                continue
            if target in seen:
                continue
            seen.add(target)
            idx_map.append((r, target))

        columns = [t for _, t in idx_map]
        rows_out = [
            [row[i] if i < len(row) else None for i, _ in idx_map]
            for row in self._rows
        ]
        try:
            batch = max(1, min(int(self.batch_spin.value()), 1000))
        except (TypeError, ValueError):
            batch = 1
        conflict = str(self.conflict_combo.currentData() or "insert")
        return columns, rows_out, batch, conflict

    def result_skip_existing(self) -> bool:
        return bool(self.skip_existing_box.isChecked())

    def _on_accept(self) -> None:
        if self._load_error:
            QMessageBox.warning(self, "无法导入", self._load_error)
            return
        columns, rows, _, _ = self.result()
        if not columns:
            QMessageBox.warning(self, "列映射为空", "请至少映射一列到目标表。")
            return
        if not rows:
            QMessageBox.information(self, "提示", "文件没有数据行。")
            return
        self.accept()


# ==================== 从本机导入数据（目标选择） ====================
class ImportDataDialog(QDialog):
    """从本机文件导入数据：选择源文件 + 目标表。

    界面 v2（2024 美化重构）：
    - 表单字段舒展，行间距 12px
    - 提示区卡片化
    - 「下一步」主按钮强调色
    """
    def __init__(
        self,
        parent=None,
        *,
        connections: list[str] | None = None,
        client_getter=None,
        preset_conn: str = "",
        preset_db: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle("从本机导入数据")
        self.setMinimumWidth(700)

        self._connections = list(connections or [])
        self._client_getter = client_getter
        self._preset_db = preset_db or ""

        # ---------- 源文件 ----------
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("选择要导入的文件（CSV / JSON / Excel）")
        self.browse_btn = QPushButton("浏览…")
        self.browse_btn.setCursor(Qt.PointingHandCursor)
        self.browse_btn.setMinimumHeight(_INPUT_H)
        self.browse_btn.clicked.connect(self._pick_file)

        path_row = QHBoxLayout()
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(6)
        path_row.addWidget(self.path_edit, 1)
        path_row.addWidget(self.browse_btn)

        # ---------- 目标连接 ----------
        self.conn_combo = QComboBox()
        self.conn_combo.addItems(self._connections)
        self.conn_combo.setMinimumHeight(_INPUT_H)
        if preset_conn and preset_conn in self._connections:
            self.conn_combo.setCurrentText(preset_conn)
        self.conn_combo.currentIndexChanged.connect(self._on_conn_changed)

        # ---------- 目标数据库 ----------
        self.db_combo = QComboBox()
        self.db_combo.setEditable(False)
        self.db_combo.setEnabled(False)
        self.db_combo.setMinimumHeight(_INPUT_H)
        self.db_combo.currentIndexChanged.connect(self._on_db_index_changed)

        # ---------- 目标表 ----------
        self.table_combo = QComboBox()
        self.table_combo.setEditable(True)
        self.table_combo.setInsertPolicy(QComboBox.NoInsert)
        self.table_combo.setEnabled(False)
        self.table_combo.setMinimumHeight(_INPUT_H)
        self.table_combo.lineEdit().setPlaceholderText("请先选择数据库")
        self.table_combo.lineEdit().setToolTip(
            "可从下拉列表选择已有表；也可直接输入新表名，导入前若表不存在，\n"
            "程序会根据文件列自动推断类型并提示建表。"
        )

        self.reload_tables_btn = QToolButton()
        self.reload_tables_btn.setText("↻")
        self.reload_tables_btn.setFixedSize(_INPUT_H, _INPUT_H)
        self.reload_tables_btn.setToolTip("重新加载目标表列表")
        self.reload_tables_btn.setCursor(Qt.PointingHandCursor)
        self.reload_tables_btn.clicked.connect(self._load_tables)

        table_row = QHBoxLayout()
        table_row.setContentsMargins(0, 0, 0, 0)
        table_row.setSpacing(6)
        table_row.addWidget(self.table_combo, 1)
        table_row.addWidget(self.reload_tables_btn)

        # ---------- 状态 ----------
        self.status_label = QLabel("")
        self.status_label.setProperty("role", "hint")
        self.status_label.setWordWrap(True)

        # ---------- 表单 ----------
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(12)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        form.addRow("源文件 *", path_row)
        form.addRow("目标连接 *", self.conn_combo)
        form.addRow("目标数据库 *", self.db_combo)
        form.addRow("目标表 *", table_row)

        # ---------- 提示卡片 ----------
        tip = QLabel(
            "支持格式：CSV（.csv）、JSON（.json）、Excel（.xlsx / .xlsm）。<br>"
            "点击「下一步」进入列映射向导，可指定文件列与表列的对应关系，"
            "并设置批量大小与冲突策略。<br>"
            "提示：目标数据库只能从下拉列表选择；目标表可手动输入新表名，"
            "导入时会自动建表。"
        )
        tip.setWordWrap(True)
        tip.setObjectName("tipCard")

        # ---------- 底部按钮 ----------
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("下一步")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.setMinimumHeight(_BTN_H)
        ok_btn.setMinimumWidth(100)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        cancel_btn.setMinimumHeight(_BTN_H)
        cancel_btn.setMinimumWidth(80)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(form)
        layout.addWidget(self.status_label)
        layout.addWidget(tip)
        layout.addSpacing(4)
        layout.addWidget(buttons)

        _style_dialog(self, extra=f"""
            QLabel#tipCard {{
                background: {theme_manager.palette.surface_alt};
                border: 1px solid {theme_manager.palette.border};
                border-radius: 10px;
                padding: 12px 14px;
                color: {theme_manager.palette.text_muted};
                line-height: 1.5;
            }}
            QPushButton#primaryBtn {{
                background: {theme_manager.palette.accent};
                color: #ffffff;
                border: 1px solid {theme_manager.palette.accent};
                font-weight: 600;
            }}
            QPushButton#primaryBtn:hover {{
                background: {theme_manager.palette.accent_hover};
                border-color: {theme_manager.palette.accent_hover};
            }}
            QPushButton#primaryBtn:pressed {{
                background: {theme_manager.palette.accent_pressed};
            }}
        """)

        if self.conn_combo.currentText():
            self._load_databases()

    def _pick_file(self) -> None:
        start = ""
        if self.path_edit.text():
            start = str(Path(self.path_edit.text()).parent)
        path, _ = QFileDialog.getOpenFileName(
            self, "选择要导入的文件", start,
            "所有支持 (*.csv *.json *.xlsx *.xlsm);;"
            "CSV (*.csv);;JSON (*.json);;Excel (*.xlsx *.xlsm);;"
            "所有文件 (*)",
        )
        if path:
            self.path_edit.setText(path)

    def _on_conn_changed(self, *_args) -> None:
        self._load_databases()

    def _on_db_index_changed(self, *_args) -> None:
        self._load_tables()

    def _load_databases(self) -> None:
        conn_name = self.conn_combo.currentText().strip()

        self.db_combo.blockSignals(True)
        self.db_combo.clear()
        self.db_combo.blockSignals(False)

        self.table_combo.blockSignals(True)
        self.table_combo.clear()
        self.table_combo.blockSignals(False)
        self.table_combo.setEnabled(False)
        self.table_combo.lineEdit().setPlaceholderText("请先选择数据库")

        if not conn_name or self._client_getter is None:
            self.db_combo.setEnabled(False)
            self.status_label.setText("")
            return

        self.db_combo.setEnabled(True)
        self.status_label.setText(f"正在加载「{conn_name}」的数据库列表…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            client = self._client_getter(conn_name)
            databases = list(client.databases()) if client is not None else []
        except Exception as exc:  # noqa: BLE001
            self.status_label.setText(f"加载数据库列表失败：{exc}")
            databases = []
        finally:
            QApplication.restoreOverrideCursor()

        self.db_combo.blockSignals(True)
        self.db_combo.addItems(databases)
        self.db_combo.blockSignals(False)

        if not databases:
            self.db_combo.setEnabled(False)
            self.status_label.setText(
                f"「{conn_name}」下未发现任何数据库，请检查连接与权限。"
            )
            return

        self.db_combo.blockSignals(True)
        if self._preset_db and self._preset_db in databases:
            self.db_combo.setCurrentText(self._preset_db)
            self._preset_db = ""
        else:
            self.db_combo.setCurrentIndex(0)
        self.db_combo.blockSignals(False)

        self._load_tables()

    def _load_tables(self) -> None:
        conn_name = self.conn_combo.currentText().strip()
        db_name = self.db_combo.currentText().strip()

        self.table_combo.blockSignals(True)
        self.table_combo.clear()
        self.table_combo.blockSignals(False)

        if not conn_name or not db_name or self._client_getter is None:
            self.table_combo.setEnabled(False)
            self.table_combo.lineEdit().setPlaceholderText("请先选择数据库")
            self.status_label.setText("")
            return

        self.table_combo.setEnabled(True)
        self.table_combo.lineEdit().setPlaceholderText("加载中…")
        self.status_label.setText(f"正在加载「{db_name}」的表列表…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            client = self._client_getter(conn_name)
            entries = (
                list(client.tables(schema=db_name)) if client is not None else []
            )
            tables = [t for t, kind in entries if kind == "table"]
        except Exception as exc:  # noqa: BLE001
            self.table_combo.lineEdit().setPlaceholderText(
                "加载失败，可手动输入新表名"
            )
            self.status_label.setText(f"加载表列表失败：{exc}")
            return
        finally:
            QApplication.restoreOverrideCursor()

        if not tables:
            self.table_combo.lineEdit().setPlaceholderText(
                "（该库无表，可直接输入新表名）"
            )
            self.status_label.setText(
                f"「{db_name}」下未发现用户表，可直接输入新表名，导入时自动创建。"
            )
            return

        self.table_combo.blockSignals(True)
        self.table_combo.addItems(tables)
        self.table_combo.blockSignals(False)
        self.table_combo.lineEdit().setPlaceholderText(
            "可从下拉选择，或输入新表名自动建表"
        )
        self.table_combo.setCurrentIndex(-1)
        self.table_combo.setEditText("")
        self.status_label.setText(
            f"「{db_name}」共 {len(tables)} 张表。可直接输入新表名，不存在时自动创建。"
        )

    def _on_accept(self) -> None:
        path = self.path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "参数不合法", "请选择要导入的文件。")
            return
        if not Path(path).is_file():
            QMessageBox.warning(self, "文件不存在", f"文件不存在：\n{path}")
            return
        if not self.conn_combo.currentText().strip():
            QMessageBox.warning(self, "参数不合法", "请选择目标连接。")
            return
        if not self.db_combo.currentText().strip():
            QMessageBox.warning(
                self, "参数不合法",
                "请选择目标数据库（从下拉列表中选择）。",
            )
            return
        if not self.table_combo.currentText().strip():
            QMessageBox.warning(self, "参数不合法", "请选择或输入目标表。")
            return
        self.accept()

    def result_values(self) -> tuple[str, str, str, str]:
        return (
            self.path_edit.text().strip(),
            self.conn_combo.currentText().strip(),
            self.db_combo.currentText().strip(),
            self.table_combo.currentText().strip(),
        )


# ==================== 建表预览（导入自动建表） ====================
class CreateTablePreviewDialog(QDialog):
    """导入时目标表不存在：展示推断出的 CREATE TABLE，允许用户修改。

    界面 v2（2024 美化重构）：
    - 顶部说明卡片化
    - 类型推断表无边框圆角
    - SQL 编辑器强调色边框
    - 「建表并继续导入」主按钮强调色
    """
    def __init__(
        self,
        parent=None,
        table: str = "",
        columns: list[str] | None = None,
        rows: list[list] | None = None,
        schema: str | None = None,
        db_type: str = "mysql",
    ):
        super().__init__(parent)
        self.setWindowTitle(f"目标表不存在 - 需要先创建「{table}」")
        self.setMinimumSize(880, 680)

        self._table = table
        self._schema = schema
        self._db_type = db_type
        self._columns = list(columns or [])
        self._rows = [list(r) for r in (rows or [])]

        from . import io_tools
        inferred = io_tools.infer_columns(self._columns, self._rows, db_type)
        self._inferred = inferred

        # ---------- 顶部说明卡片 ----------
        info = QLabel(
            f"目标表 <b>{table}</b> 在数据库中不存在。<br>"
            "将根据导入文件的列名与数据自动推断类型，生成下面的 "
            "<code>CREATE TABLE</code>。请确认或修改后点击「建表并继续导入」。"
        )
        info.setWordWrap(True)
        info.setObjectName("infoCard")

        # ---------- 类型推断表 ----------
        type_label = QLabel("推断的列")
        type_label.setObjectName("sectionLabel")
        type_hint = QLabel("含前 5 行样例值")
        type_hint.setProperty("role", "hint")
        type_head = QHBoxLayout()
        type_head.setContentsMargins(0, 0, 0, 0)
        type_head.addWidget(type_label)
        type_head.addSpacing(8)
        type_head.addWidget(type_hint)
        type_head.addStretch(1)

        type_table = QTableWidget(len(inferred), 3)
        type_table.setHorizontalHeaderLabels(["列名", "推断类型", "样例值"])
        hdr = type_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.Stretch)
        type_table.verticalHeader().setDefaultSectionSize(26)
        type_table.setEditTriggers(QTableWidget.NoEditTriggers)
        type_table.setMaximumHeight(240)
        type_table.setAlternatingRowColors(True)
        type_table.setShowGrid(False)
        type_table.setFrameShape(QTableWidget.NoFrame)

        for r, (name, ctype) in enumerate(inferred):
            type_table.setItem(r, 0, QTableWidgetItem(name))
            type_table.setItem(r, 1, QTableWidgetItem(ctype))
            try:
                idx = self._columns.index(name)
            except ValueError:
                idx = r
            sample = ""
            for row in self._rows[:5]:
                if idx < len(row) and row[idx] not in (None, ""):
                    sample = str(row[idx])
                    break
            type_table.setItem(r, 2, QTableWidgetItem(sample[:80]))

        # ---------- SQL 编辑器 ----------
        sql_label = QLabel("建表 SQL")
        sql_label.setObjectName("sectionLabel")
        sql_hint = QLabel("可编辑；如需主键 / 索引请自行添加")
        sql_hint.setProperty("role", "hint")
        sql_head = QHBoxLayout()
        sql_head.setContentsMargins(0, 0, 0, 0)
        sql_head.addWidget(sql_label)
        sql_head.addSpacing(8)
        sql_head.addWidget(sql_hint)
        sql_head.addStretch(1)

        self.editor = QPlainTextEdit()
        self.editor.setPlainText(io_tools.build_create_table(
            table, inferred, schema=schema, db_type=db_type,
        ))
        self.editor.setStyleSheet(
            "font-family: Consolas, 'JetBrains Mono', monospace; font-size: 12px;"
        )

        # ---------- 底部按钮 ----------
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_btn = buttons.button(QDialogButtonBox.Ok)
        ok_btn.setText("建表并继续导入")
        ok_btn.setObjectName("primaryBtn")
        ok_btn.setMinimumHeight(_BTN_H)
        ok_btn.setMinimumWidth(160)
        cancel_btn = buttons.button(QDialogButtonBox.Cancel)
        cancel_btn.setText("取消")
        cancel_btn.setMinimumHeight(_BTN_H)
        cancel_btn.setMinimumWidth(80)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        # ---------- 总布局 ----------
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        layout.addWidget(info)
        layout.addSpacing(2)
        layout.addLayout(type_head)
        layout.addWidget(type_table)
        layout.addSpacing(2)
        layout.addLayout(sql_head)
        layout.addWidget(self.editor, 1)
        layout.addWidget(buttons)

        _style_dialog(self, extra=f"""
            QLabel#infoCard {{
                background: {theme_manager.palette.accent_soft};
                border: 1px solid {theme_manager.palette.accent};
                border-radius: 10px;
                padding: 12px 14px;
                color: {theme_manager.palette.text};
                line-height: 1.5;
            }}
            QLabel#sectionLabel {{
                font-weight: 600;
                color: {theme_manager.palette.text};
            }}
            QPushButton#primaryBtn {{
                background: {theme_manager.palette.accent};
                color: #ffffff;
                border: 1px solid {theme_manager.palette.accent};
                font-weight: 600;
            }}
            QPushButton#primaryBtn:hover {{
                background: {theme_manager.palette.accent_hover};
                border-color: {theme_manager.palette.accent_hover};
            }}
            QPushButton#primaryBtn:pressed {{
                background: {theme_manager.palette.accent_pressed};
            }}
        """)

    def _on_accept(self) -> None:
        if not self.editor.toPlainText().strip():
            QMessageBox.warning(self, "提示", "建表 SQL 不能为空。")
            return
        self.accept()

    def result_sql(self) -> str:
        return self.editor.toPlainText().strip()