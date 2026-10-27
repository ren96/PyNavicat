"""连接配置模型与本地持久化。密码优先走系统钥匙串（keyring），
钥匙串不可用时退回本地 Fernet 加密（见 crypto.py）。"""
from __future__ import annotations

import getpass
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

from sqlalchemy import URL

from .config import CONFIG_FILE
from .crypto import LocalCipher

try:
    import keyring
    _HAS_KEYRING = True
except ImportError:  # pragma: no cover
    keyring = None
    _HAS_KEYRING = False

log = logging.getLogger(__name__)

SERVICE_NAME = "pynavicat"

# 本地加密器（懒加载单例）——基于配置文件所在目录
# 说明：LocalCipher 构造时会执行 PBKDF2（20 万次迭代），
# 放在模块导入阶段会导致启动被拖慢；改为首次真正需要时再构建。
# 若 keyring 可用，密码读写全程不碰本地加密器，PBKDF2 完全跳过。
_CIPHER: LocalCipher | None = None
_CIPHER_LOCK = threading.RLock()

# keyring 探测的超时（秒）。某些环境下后端初始化会卡住，
# 阻塞主线程导致启动看起来死掉；超时后一律视为不可用，退回本地加密。
_KEYRING_PROBE_TIMEOUT = 2.0


def _cipher() -> LocalCipher:
    """懒加载本地加密器。线程安全。"""
    global _CIPHER
    if _CIPHER is None:
        with _CIPHER_LOCK:
            if _CIPHER is None:
                _CIPHER = LocalCipher(CONFIG_FILE.parent)
    return _CIPHER


# 各数据库默认端口；0 表示不适用（如基于文件的嵌入式数据库）
DEFAULT_PORTS = {
    # ---- MySQL 协议族 ----
    "mysql": 3306,
    "mariadb": 3306,
    "tidb": 4000,          # TiDB 默认端口
    "oceanbase": 2881,     # OceanBase MySQL 模式
    # ---- PostgreSQL 协议族 ----
    "postgresql": 5432,
    "cockroachdb": 26257,  # CockroachDB 默认端口
    "kingbase": 54321,     # 人大金仓 KingBase
    # ---- 嵌入式（基于文件）----
    "sqlite": 0,
    "duckdb": 0,
    # ---- 企业级 ----
    "mssql": 1433,
    "oracle": 1521,
    "db2": 50000,
    "firebird": 3050,
    # ---- 数仓 / 分析 ----
    "clickhouse": 8123,    # HTTP 端口（native 为 9000）
    "trino": 8080,
}

DRIVERS = {
    # ---- MySQL 协议族（复用 PyMySQL）----
    "mysql": "mysql+pymysql",
    "mariadb": "mysql+pymysql",
    "tidb": "mysql+pymysql",
    "oceanbase": "mysql+pymysql",
    # ---- PostgreSQL 协议族（复用 psycopg2）----
    "postgresql": "postgresql+psycopg2",
    "cockroachdb": "postgresql+psycopg2",
    "kingbase": "postgresql+psycopg2",
    # ---- 嵌入式 ----
    "sqlite": "sqlite",
    "duckdb": "duckdb",
    # ---- 企业级 ----
    "mssql": "mssql+pyodbc",
    "oracle": "oracle+oracledb",
    "db2": "ibm_db_sa+db2",
    "firebird": "firebird+fdb",
    # ---- 数仓 / 分析 ----
    "clickhouse": "clickhouse+http",
    "trino": "trino",
}

# 基于文件的嵌入式数据库集合（不需要 host / user / password / 端口）
_FILE_DB_TYPES = frozenset(("sqlite", "duckdb"))

_KEYRING_OK: bool | None = None


def _keyring_available() -> bool:
    global _KEYRING_OK
    if _KEYRING_OK is not None:
        return _KEYRING_OK
    if not _HAS_KEYRING:
        _KEYRING_OK = False
        return False

    # 探测放到后台线程里做，主线程最多等 _KEYRING_PROBE_TIMEOUT 秒。
    # 超时视为不可用，改用本地加密 —— 宁可牺牲一次钥匙串，也不让启动卡死。
    result: list[bool] = []

    def _probe() -> None:
        try:
            probe = f"__probe__{os.getpid()}"
            keyring.set_password(SERVICE_NAME, probe, "x")
            keyring.delete_password(SERVICE_NAME, probe)
            result.append(True)
        except Exception as exc:  # noqa: BLE001
            log.warning("keyring 不可用，将改用本地加密：%s", exc)
            result.append(False)

    t = threading.Thread(target=_probe, name="keyring-probe", daemon=True)
    t.start()
    t.join(timeout=_KEYRING_PROBE_TIMEOUT)

    if not result:
        log.warning(
            "keyring 探测超时（>%.1fs），改用本地加密", _KEYRING_PROBE_TIMEOUT
        )
        _KEYRING_OK = False
    else:
        _KEYRING_OK = result[0]
    return _KEYRING_OK


def _keyring_key(conn_name: str) -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "user"
    return f"{user}@{conn_name}"


# ========== 密码读写：keyring 优先 → 本地加密 ==========
def store_password(conn_name: str, password: str) -> str:
    """写入密码。成功走钥匙串时返回空串；否则返回可落盘的密文。"""
    if not password:
        delete_password(conn_name)
        return ""

    if _keyring_available():
        try:
            keyring.set_password(SERVICE_NAME, _keyring_key(conn_name), password)
            return ""
        except Exception as exc:  # noqa: BLE001
            log.warning("写入钥匙串失败，改用本地加密：%s", exc)
            # 清理可能残留的旧密码，避免下次读到过时值
            try:
                keyring.delete_password(SERVICE_NAME, _keyring_key(conn_name))
            except Exception:  # noqa: BLE001
                pass

    return _cipher().encrypt(password)


def load_password(conn_name: str, fallback: str = "") -> str:
    """读取密码。fallback 是 JSON 里的密文（可能为空，也可能为旧 base64）。"""
    if _keyring_available():
        try:
            pwd = keyring.get_password(SERVICE_NAME, _keyring_key(conn_name))
            if pwd is not None:
                return pwd
        except Exception as exc:  # noqa: BLE001
            log.warning("读取钥匙串失败，尝试本地密文：%s", exc)

    return _cipher().decrypt(fallback)


def delete_password(conn_name: str) -> None:
    if not _keyring_available():
        return
    try:
        keyring.delete_password(SERVICE_NAME, _keyring_key(conn_name))
    except Exception:  # noqa: BLE001
        pass


def store_ssh_password(conn_name: str, password: str) -> str:
    """SSH 密码单独存一份，避免和数据库密码互相覆盖。"""
    if not password:
        return ""
    if _keyring_available():
        try:
            keyring.set_password(
                SERVICE_NAME, f"ssh:{_keyring_key(conn_name)}", password
            )
            return ""
        except Exception as exc:  # noqa: BLE001
            log.warning("写入 SSH 钥匙串失败，改用本地加密：%s", exc)
            try:
                keyring.delete_password(
                    SERVICE_NAME, f"ssh:{_keyring_key(conn_name)}"
                )
            except Exception:  # noqa: BLE001
                pass
    return _cipher().encrypt(password)


def load_ssh_password(conn_name: str, fallback: str = "") -> str:
    if _keyring_available():
        try:
            pwd = keyring.get_password(
                SERVICE_NAME, f"ssh:{_keyring_key(conn_name)}"
            )
            if pwd is not None:
                return pwd
        except Exception:  # noqa: BLE001
            pass
    return _cipher().decrypt(fallback)


def delete_ssh_password(conn_name: str) -> None:
    if not _keyring_available():
        return
    try:
        keyring.delete_password(SERVICE_NAME, f"ssh:{_keyring_key(conn_name)}")
    except Exception:  # noqa: BLE001
        pass


@dataclass
class ConnectionConfig:
    name: str = "新建连接"
    db_type: str = "mysql"
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = ""
    sqlite_path: str = ""
    charset: str = "utf8mb4"
    extra_query: dict[str, str] = field(default_factory=dict)

    # ---- 查询超时（秒；0 = 服务端默认）----
    query_timeout: int = 0
    # ---- 连接分组（空串 = 未分组）----
    group: str = ""

    # ---- 视觉标记与只读保护 ----
    color: str = ""              # 空 = 不标记；否则 #RRGGBB
    readonly: bool = False       # 只读连接：禁止所有写操作

    # ---- SSH 隧道（预留，客户端需要 sshtunnel 库）----
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_key_file: str = ""
    ssh_use_agent: bool = False  # True = 用系统 ssh-agent / Pageant

    # ---------- URL ----------
    def to_url(self) -> URL:
        # 基于文件的嵌入式数据库：SQLite / DuckDB
        if self.db_type in _FILE_DB_TYPES:
            raw = (self.sqlite_path or "").strip()
            if raw and raw != ":memory:":
                p = Path(raw)
                if not p.is_absolute():
                    # 相对路径统一按数据目录解析，避免依赖进程工作目录
                    p = (CONFIG_FILE.parent / p).resolve()
                raw = str(p)
            return URL.create(self.db_type, database=raw)

        driver = DRIVERS.get(self.db_type, self.db_type)
        query = dict(self.extra_query or {})
        if self.db_type == "mysql" and self.charset:
            query.setdefault("charset", self.charset)
        if self.query_timeout and self.db_type == "postgresql":
            query.setdefault(
                "options", f"-c statement_timeout={self.query_timeout * 1000}"
            )

        return URL.create(
            driver,
            username=self.user or None,
            password=self.password or None,
            host=self.host or None,
            port=int(self.port) if self.port else None,
            database=self.database or None,
            query=query,
        )

    # ---------- SQLAlchemy connect_args ----------
    def connect_args(self) -> dict[str, Any]:
        """把 query_timeout 翻译成各驱动的连接参数。"""
        t = int(self.query_timeout or 0)
        if t <= 0:
            return {}
        if self.db_type == "mysql":
            # PyMySQL：读写超时单位是秒
            return {
                "read_timeout": t,
                "write_timeout": t,
                "connect_timeout": min(t, 30),
            }
        if self.db_type == "mssql":
            return {"timeout": t}
        if self.db_type == "oracle":
            return {"call_timeout": t * 1000}
        return {}

    # ---------- 序列化 ----------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["password"] = store_password(self.name, self.password)
        d["ssh_password"] = store_ssh_password(self.name, self.ssh_password)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ConnectionConfig":
        d = dict(d)
        name = d.get("name", "新建连接")
        d["password"] = load_password(name, d.get("password", ""))
        d["ssh_password"] = load_ssh_password(name, d.get("ssh_password", ""))
        # 兼容旧配置：color / readonly / ssh_use_agent 缺失时用默认值
        d.setdefault("color", "")
        d.setdefault("readonly", False)
        d.setdefault("ssh_use_agent", False)
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    # ---------- 校验 ----------
    _NAME_BAD = re.compile(r"[\\/:*?\"<>|\r\n\t]")

    def validate(self) -> str | None:
        """返回 None 表示合法，否则返回错误信息。"""
        if not self.name.strip():
            return "连接名不能为空。"
        if self._NAME_BAD.search(self.name):
            return '连接名不能包含 \\ / : * ? " < > | 等字符。'

        # 基于文件的嵌入式数据库：只校验路径
        if self.db_type in _FILE_DB_TYPES:
            if not self.sqlite_path.strip():
                label = "DuckDB" if self.db_type == "duckdb" else "SQLite"
                return f"{label} 需要指定数据库文件路径。"
            return None

        # ---- SSH 隧道（可选）----
        if self.ssh_host.strip():
            if not (0 < int(self.ssh_port or 0) <= 65535):
                return "SSH 端口必须在 1~65535 之间。"
            if not self.ssh_user.strip():
                return "SSH 用户名不能为空。"
            if not self.ssh_use_agent:
                if not self.ssh_password and not self.ssh_key_file.strip():
                    return "SSH 密码、私钥文件、或勾选「使用 ssh-agent」至少填写一项。"

        if not self.host.strip():
            return "主机地址不能为空。"
        if not (0 <= int(self.port) <= 65535):
            return "端口必须在 0~65535 之间。"
        if not self.user.strip() and self.db_type in (
            "mysql", "mariadb", "tidb", "oceanbase",
            "postgresql", "cockroachdb", "kingbase",
            "mssql",
        ):
            return "用户名不能为空。"
        if self.query_timeout < 0:
            return "查询超时必须大于等于 0。"
        return None

    def display_name(self) -> str:
        """用于状态栏/页签显示。"""
        if self.db_type in _FILE_DB_TYPES:
            base = f"{self.name}  ·  {self.db_type}://{Path(self.sqlite_path).name}"
        else:
            host = self.host or "?"
            base = f"{self.name}  ·  {self.db_type}://{host}:{self.port}"
        if self.ssh_host:
            base += f"  [SSH {self.ssh_user}@{self.ssh_host}]"
        if self.readonly:
            base += "  [只读]"
        return base


class ConnectionStore:
    """连接列表的读写，落盘为 JSON。"""

    def __init__(self, path: Path = CONFIG_FILE):
        self.path = path
        self.connections: list[ConnectionConfig] = []
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.connections = []
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            self.connections = [ConnectionConfig.from_dict(d) for d in raw]
        except Exception as exc:  # noqa: BLE001
            log.warning("读取连接配置失败：%s", exc)
            self.connections = []

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            data = [c.to_dict() for c in self.connections]
            self.path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:  # noqa: BLE001
            log.error("保存连接配置失败：%s", exc)

    def get(self, name: str) -> ConnectionConfig | None:
        return next((c for c in self.connections if c.name == name), None)

    def add(self, cfg: ConnectionConfig) -> None:
        self.connections.append(cfg)
        self.save()

    def update(self, old_name: str, cfg: ConnectionConfig) -> None:
        if old_name != cfg.name:
            delete_password(old_name)
            delete_ssh_password(old_name)
        for i, c in enumerate(self.connections):
            if c.name == old_name:
                self.connections[i] = cfg
                break
        else:
            self.connections.append(cfg)
        self.save()

    def remove(self, name: str) -> None:
        delete_password(name)
        delete_ssh_password(name)
        self.connections = [c for c in self.connections if c.name != name]
        self.save()

    # ---------- 分组视图 ----------
    def groups(self) -> dict[str, list[ConnectionConfig]]:
        """按 group 字段分组，空 group 归入「未分组」。"""
        out: dict[str, list[ConnectionConfig]] = {}
        for c in self.connections:
            key = (c.group or "").strip() or "未分组"
            out.setdefault(key, []).append(c)
        return out