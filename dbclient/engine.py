"""基于 SQLAlchemy 的数据库操作封装 + 元数据缓存 + 取消/事务/流式支持。"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine

from .connection import ConnectionConfig
from .sql_util import qualify_table, quote_ident

log = logging.getLogger(__name__)

_META_TTL = 300

# ============================================================
# 进程级全局锁：串行化所有元数据反射操作
# ============================================================
_META_GLOBAL_LOCK = threading.RLock()


def _resolve_dbapi_connection(conn: Connection) -> Any | None:
    """尽最大努力拿到 SQLAlchemy 底层的 DBAPI 连接对象。

    不同 SQLAlchemy 版本 / 连接类型属性名不同：
    - 2.0：`conn.connection.dbapi_connection`（`driver_connection` 是别名）
    - 1.4：`conn.connection.connection`

    任何一步失败都返回 None，调用方据此退化为"直接关闭/轮询"。
    """
    try:
        impl = conn.connection
    except Exception:  # noqa: BLE001
        return None
    for attr in ("dbapi_connection", "driver_connection", "connection"):
        raw = getattr(impl, attr, None)
        if raw is not None:
            return raw
    return None


def _try_driver_cancel(raw: Any) -> bool:
    """尝试调用驱动的 cancel()。返回是否真的调用成功。"""
    cancel = getattr(raw, "cancel", None)
    if not callable(cancel):
        return False
    try:
        cancel()
        return True
    except Exception:  # noqa: BLE001
        return False


def _try_hard_close(raw: Any) -> None:
    """尽最大努力强制关闭底层 socket，让阻塞中的 read 立即报错。"""
    close = getattr(raw, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


class QueryCancelled(Exception):
    """用户主动取消查询。"""


@dataclass
class PageInfo:
    page: int = 1
    page_size: int = 200
    total: int | None = None
    has_next: bool = False


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[tuple] = field(default_factory=list)
    rowcount: int = 0
    elapsed: float = 0.0
    message: str = ""
    truncated: bool = False
    page: PageInfo | None = None
    table_ctx: dict | None = None
    success: bool = True
    rolled_back: bool = False


# ============================================================
# SQL 语句拆分：基于切片，支持 PG dollar-quoted 字符串
# ============================================================
_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def split_statements(sql: str) -> list[str]:
    """按 SQL 语法规则拆分多条语句。"""
    stmts: list[str] = []
    n = len(sql)
    i = 0
    start = 0
    quote: str | None = None
    dollar_tag: str | None = None

    while i < n:
        ch = sql[i]

        if dollar_tag is not None:
            if sql.startswith(dollar_tag, i):
                i += len(dollar_tag)
                dollar_tag = None
                continue
            i += 1
            continue

        if quote:
            if ch == quote:
                if i + 1 < n and sql[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            elif ch == "\\" and i + 1 < n:
                i += 2
                continue
            i += 1
            continue

        if ch == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m is not None:
                dollar_tag = m.group(0)
                i = m.end()
                continue

        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue

        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue

        if ch == ";":
            s = sql[start:i].strip()
            if s:
                stmts.append(s)
            start = i + 1
        i += 1

    tail = sql[start:].strip()
    if tail:
        stmts.append(tail)
    return stmts


def statement_bounds(text: str, pos: int) -> tuple[int, int]:
    """返回 pos 位置所在 SQL 语句在 text 中的 (start, end) 区间。"""
    n = len(text)
    i = 0
    start = 0
    quote: str | None = None
    dollar_tag: str | None = None

    while i < n:
        ch = text[i]

        if dollar_tag is not None:
            if text.startswith(dollar_tag, i):
                i += len(dollar_tag)
                dollar_tag = None
                continue
            i += 1
            continue

        if quote:
            if ch == quote:
                if i + 1 < n and text[i + 1] == quote:
                    i += 2
                    continue
                quote = None
            elif ch == "\\" and i + 1 < n:
                i += 2
                continue
            i += 1
            continue

        if ch == "$":
            m = _DOLLAR_TAG_RE.match(text, i)
            if m is not None:
                dollar_tag = m.group(0)
                i = m.end()
                continue

        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            continue

        if ch == "-" and i + 1 < n and text[i + 1] == "-":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue

        if ch == ";":
            if start <= pos <= i:
                return start, i + 1
            start = i + 1
        i += 1
    return start, n


# ============================================================
# 危险语句检测
# ============================================================
_DANGEROUS_PATTERNS = (
    (re.compile(r"\bdrop\s+(table|database|schema|view|index|user|role)\b", re.I),
     "DROP 语句"),
    (re.compile(r"\btruncate\s+(table\s+)?\w", re.I), "TRUNCATE 语句"),
    (re.compile(r"\bcopy\s+\S+\s+from\b", re.I), "COPY FROM 导入"),
    (re.compile(r"\bgrant\b|\brevoke\b", re.I), "权限变更"),
)
_QUICK_HINT = re.compile(
    r"\b(drop|truncate|delete|update|copy|grant|revoke|into)\b", re.I
)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
_WHERE_RE = re.compile(r"\bwhere\b", re.I)
_DELETE_HEAD_RE = re.compile(r"\s*delete\b", re.I)
_UPDATE_HEAD_RE = re.compile(r"\s*update\b", re.I)
_SELECT_HEAD_RE = re.compile(r"^\s*(with\b|select\b)", re.I)
_SELECT_INTO_RE = re.compile(r"\binto\b", re.I)


def dangerous_reason(sql: str) -> str | None:
    """返回危险操作的说明；没有危险操作时返回 None。"""
    for stmt in split_statements(sql):
        if not _QUICK_HINT.search(stmt):
            continue
        s = _LINE_COMMENT_RE.sub(" ", stmt)
        s = _BLOCK_COMMENT_RE.sub(" ", s)
        for pat, label in _DANGEROUS_PATTERNS:
            if pat.search(s):
                return label

        m = _DELETE_HEAD_RE.match(s)
        if m and not _WHERE_RE.search(s[m.end():]):
            return "不带 WHERE 的 DELETE"
        m = _UPDATE_HEAD_RE.match(s)
        if m and not _WHERE_RE.search(s[m.end():]):
            return "不带 WHERE 的 UPDATE"

        if _SELECT_HEAD_RE.match(s) and _SELECT_INTO_RE.search(s):
            return "SELECT INTO（会写入文件或新表）"
    return None


class MetadataCache:
    """带 TTL 的 LRU 缓存。访问会刷新位置；容量超过 max_items 时淘汰最旧项。"""

    def __init__(self, ttl: float = _META_TTL, max_items: int = 2048):
        self._ttl = ttl
        self._max = max_items
        self._lock = threading.RLock()
        self._data: "OrderedDict[tuple, tuple[float, Any]]" = OrderedDict()

    def get(self, key: tuple) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            ts, value = item
            if time.monotonic() - ts > self._ttl:
                self._data.pop(key, None)
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key: tuple, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max:
                self._data.popitem(last=False)

    def invalidate(self, prefix: tuple | None = None) -> None:
        with self._lock:
            if prefix is None:
                self._data.clear()
                return
            n = len(prefix)
            for k in [k for k in self._data if k[:n] == prefix]:
                self._data.pop(k, None)


# 对象类型分组：面板会按此顺序渲染
OBJECT_GROUPS: tuple[str, ...] = (
    "table", "view", "procedure", "function", "trigger", "event",
)


class DatabaseClient:
    def __init__(self, cfg: ConnectionConfig):
        self.cfg = cfg
        self._lock = threading.RLock()
        self._meta_lock = _META_GLOBAL_LOCK
        self._ssh = None
        self._url_cfg = cfg

        if getattr(cfg, "ssh_host", ""):
            from .ssh_tunnel import SshTunnel
            tunnel = SshTunnel(cfg)
            # 隧道打开失败时由 SshTunnel 内部收尾（不会遗留半开的 server）。
            host, port = tunnel.open()
            self._ssh = tunnel
            self._url_cfg = replace(cfg, host=host, port=port)

        try:
            self.engine: Engine = self._make_engine()
        except Exception:
            # engine 创建失败 → 立刻释放已开的隧道，避免泄漏。
            if self._ssh is not None:
                try:
                    self._ssh.close()
                except Exception as exc:  # noqa: BLE001
                    log.warning("释放 SSH 隧道失败：%s", exc)
                self._ssh = None
            raise
        self.meta = MetadataCache()

    def _make_engine(self) -> Engine:
        return create_engine(
            self._url_cfg.to_url(),
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=5,
            max_overflow=5,
            pool_use_lifo=True,
            future=True,
            connect_args=self._url_cfg.connect_args(),
        )

    # ---------- 生命周期 ----------
    def dispose(self) -> None:
        with self._meta_lock:
            try:
                self.engine.dispose()
            except Exception as exc:  # noqa: BLE001
                log.warning("释放连接失败：%s", exc)
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception as exc:  # noqa: BLE001
                log.warning("关闭 SSH 隧道失败：%s", exc)
            self._ssh = None

    def test_connection(self) -> None:
        with self._meta_lock:
            with self.engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")

    def ensure_alive(self) -> None:
        with self._meta_lock:
            try:
                with self.engine.connect() as conn:
                    conn.exec_driver_sql("SELECT 1")
            except Exception as exc:  # noqa: BLE001
                log.warning("连接探测失败，重建连接池：%s", exc)
                try:
                    self.engine.dispose()
                except Exception:  # noqa: BLE001
                    pass
                with self._lock:
                    self.engine = self._make_engine()

    # ---------- Schema ----------
    def _normalize_schema(self, schema: str | None) -> str | None:
        if self.cfg.db_type == "sqlite" and schema in (None, "", "main", "temp"):
            return None
        return schema

    # ---------- 元数据（全部在进程级全局锁内） ----------
    def databases(self) -> list[str]:
        with self._meta_lock:
            key = ("databases",)
            cached = self.meta.get(key)
            if cached is not None:
                return cached
            insp = inspect(self.engine)
            try:
                result = sorted(insp.get_schema_names())
            except Exception as exc:  # noqa: BLE001
                log.warning("获取数据库列表失败：%s", exc)
                raise RuntimeError(f"读取数据库列表失败：{exc}") from exc
            self.meta.put(key, result)
            return result

    def tables(self, schema: str | None = None) -> list[tuple[str, str]]:
        schema = self._normalize_schema(schema)
        with self._meta_lock:
            key = ("tables", schema)
            cached = self.meta.get(key)
            if cached is not None:
                return cached
            insp = inspect(self.engine)
            try:
                table_names = insp.get_table_names(schema=schema)
            except Exception as exc:  # noqa: BLE001
                log.warning("获取表列表失败 [%s]：%s", schema, exc)
                raise RuntimeError(f"读取数据表列表失败：{exc}") from exc
            try:
                view_names = insp.get_view_names(schema=schema)
            except Exception as exc:  # noqa: BLE001
                log.debug("获取视图列表失败 [%s]：%s", schema, exc)
                view_names = []
            result = sorted(
                [(t, "table") for t in table_names] + [(v, "view") for v in view_names],
                key=lambda x: (x[1], x[0].lower()),
            )
            self.meta.put(key, result)
            return result

    # ---------- 所有对象（含例程 / 触发器 / 事件）----------
    def all_objects(self, schema: str | None = None) -> dict[str, list[str]]:
        """一次性返回该 schema 下的所有对象，按类型分组。

        返回：{"table": [...], "view": [...], "procedure": [...],
              "function": [...], "trigger": [...], "event": [...]}
        不支持的数据库类型 / 无权限时对应列表为空，不抛异常。
        """
        schema = self._normalize_schema(schema)
        with self._meta_lock:
            key = ("all_objects", schema)
            cached = self.meta.get(key)
            if cached is not None:
                return cached

            out: dict[str, list[str]] = {k: [] for k in OBJECT_GROUPS}

            # 表和视图（复用已有缓存）
            try:
                for name, kind in self.tables(schema=schema):
                    if kind in out:
                        out[kind].append(name)
            except Exception as exc:  # noqa: BLE001
                log.debug("读取表/视图失败：%s", exc)

            # 例程 / 触发器 / 事件
            db = self.cfg.db_type
            try:
                with self.engine.connect() as conn:
                    if db in ("mysql", "mariadb", "tidb", "oceanbase"):
                        self._load_mysql_objects(conn, schema, out)
                    elif db in ("postgresql", "cockroachdb", "kingbase"):
                        self._load_pg_objects(conn, schema, out)
                    elif db == "mssql":
                        self._load_mssql_objects(conn, schema, out)
                    elif db == "oracle":
                        self._load_oracle_objects(conn, schema, out)
            except Exception as exc:  # noqa: BLE001
                log.debug("读取例程/触发器/事件失败：%s", exc)

            for k in out:
                # 去重 + 小写优先排序
                seen: set[str] = set()
                uniq: list[str] = []
                for name in out[k]:
                    if name and name not in seen:
                        seen.add(name)
                        uniq.append(name)
                out[k] = sorted(uniq, key=str.lower)

            self.meta.put(key, out)
            return out

    @staticmethod
    def _load_mysql_objects(conn: Connection, schema: str | None,
                            out: dict[str, list[str]]) -> None:
        if not schema:
            return
        try:
            rows = conn.execute(
                text(
                    "SELECT ROUTINE_NAME, ROUTINE_TYPE "
                    "FROM information_schema.ROUTINES "
                    "WHERE ROUTINE_SCHEMA = :s"
                ),
                {"s": schema},
            ).fetchall()
            for name, kind in rows:
                k = str(kind).upper()
                if k == "PROCEDURE":
                    out["procedure"].append(str(name))
                elif k == "FUNCTION":
                    out["function"].append(str(name))
        except Exception as exc:  # noqa: BLE001
            log.debug("MySQL 读取例程失败：%s", exc)

        try:
            rows = conn.execute(
                text(
                    "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS "
                    "WHERE TRIGGER_SCHEMA = :s"
                ),
                {"s": schema},
            ).fetchall()
            out["trigger"].extend(str(r[0]) for r in rows)
        except Exception as exc:  # noqa: BLE001
            log.debug("MySQL 读取触发器失败：%s", exc)

        try:
            rows = conn.execute(
                text(
                    "SELECT EVENT_NAME FROM information_schema.EVENTS "
                    "WHERE EVENT_SCHEMA = :s"
                ),
                {"s": schema},
            ).fetchall()
            out["event"].extend(str(r[0]) for r in rows)
        except Exception as exc:  # noqa: BLE001
            log.debug("MySQL 读取事件失败：%s", exc)

    @staticmethod
    def _load_pg_objects(conn: Connection, schema: str | None,
                         out: dict[str, list[str]]) -> None:
        if not schema:
            return
        try:
            rows = conn.execute(
                text(
                    "SELECT routine_name, routine_type "
                    "FROM information_schema.routines "
                    "WHERE routine_schema = :s"
                ),
                {"s": schema},
            ).fetchall()
            for name, kind in rows:
                k = str(kind).upper()
                if k == "PROCEDURE":
                    out["procedure"].append(str(name))
                elif k == "FUNCTION":
                    out["function"].append(str(name))
        except Exception as exc:  # noqa: BLE001
            log.debug("PG 读取例程失败：%s", exc)

        try:
            rows = conn.execute(
                text(
                    "SELECT trigger_name FROM information_schema.triggers "
                    "WHERE trigger_schema = :s"
                ),
                {"s": schema},
            ).fetchall()
            out["trigger"].extend(str(r[0]) for r in rows)
        except Exception as exc:  # noqa: BLE001
            log.debug("PG 读取触发器失败：%s", exc)

    @staticmethod
    def _load_mssql_objects(conn: Connection, schema: str | None,
                            out: dict[str, list[str]]) -> None:
        if not schema:
            return
        try:
            rows = conn.execute(
                text(
                    "SELECT o.name, o.type "
                    "FROM sys.objects o "
                    "JOIN sys.schemas s ON o.schema_id = s.schema_id "
                    "WHERE s.name = :s "
                    "AND o.type IN ('P', 'FN', 'IF', 'TF', 'TR')"
                ),
                {"s": schema},
            ).fetchall()
            for name, kind in rows:
                k = str(kind).strip().upper()
                if k == "P":
                    out["procedure"].append(str(name))
                elif k in ("FN", "IF", "TF"):
                    out["function"].append(str(name))
                elif k == "TR":
                    out["trigger"].append(str(name))
        except Exception as exc:  # noqa: BLE001
            log.debug("MSSQL 读取对象失败：%s", exc)

    @staticmethod
    def _load_oracle_objects(conn: Connection, schema: str | None,
                             out: dict[str, list[str]]) -> None:
        owner = (schema or "").upper()
        if not owner:
            return
        try:
            rows = conn.execute(
                text(
                    "SELECT object_name, object_type FROM all_objects "
                    "WHERE owner = :o "
                    "AND object_type IN ('PROCEDURE', 'FUNCTION', 'TRIGGER')"
                ),
                {"o": owner},
            ).fetchall()
            for name, kind in rows:
                k = str(kind).upper()
                if k == "PROCEDURE":
                    out["procedure"].append(str(name))
                elif k == "FUNCTION":
                    out["function"].append(str(name))
                elif k == "TRIGGER":
                    out["trigger"].append(str(name))
        except Exception as exc:  # noqa: BLE001
            log.debug("Oracle 读取对象失败：%s", exc)

    def columns(self, table: str, schema: str | None = None) -> list[dict[str, Any]]:
        schema = self._normalize_schema(schema)
        with self._meta_lock:
            key = ("columns", schema, table)
            cached = self.meta.get(key)
            if cached is not None:
                return cached
            insp = inspect(self.engine)
            try:
                result = insp.get_columns(table, schema=schema)
            except Exception as exc:  # noqa: BLE001
                level = log.debug if schema is None else log.warning
                level("获取列信息失败 [%s.%s]：%s", schema, table, exc)
                result = []
            self.meta.put(key, result)
            return result

    def multi_columns(
        self, tables: list[str], schema: str | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        """批量获取多张表的列信息。"""
        if not tables:
            return {}
        schema = self._normalize_schema(schema)
        with self._meta_lock:
            insp = inspect(self.engine)
            get_multi = getattr(insp, "get_multi_columns", None)
            if callable(get_multi):
                try:
                    result = get_multi(schema=schema, filter_names=list(tables))
                    out: dict[str, list[dict[str, Any]]] = {}
                    for tbl, cols in dict(result).items():
                        out[tbl] = list(cols)
                        self.meta.put(("columns", schema, tbl), out[tbl])
                    for tbl in tables:
                        out.setdefault(tbl, [])
                    return out
                except (NotImplementedError, TypeError, AttributeError) as exc:
                    log.debug("批量列反射不可用，回退逐表调用：%s", exc)
                except Exception as exc:  # noqa: BLE001
                    log.debug("批量列反射失败，回退逐表调用：%s", exc)

            out = {}
            for tbl in tables:
                try:
                    out[tbl] = self.columns(tbl, schema=schema)
                except Exception:  # noqa: BLE001
                    out[tbl] = []
            return out

    def indexes(self, table: str, schema: str | None = None) -> list[dict[str, Any]]:
        schema = self._normalize_schema(schema)
        with self._meta_lock:
            key = ("indexes", schema, table)
            cached = self.meta.get(key)
            if cached is not None:
                return cached
            insp = inspect(self.engine)
            try:
                result = insp.get_indexes(table, schema=schema)
            except Exception as exc:  # noqa: BLE001
                level = log.debug if schema is None else log.warning
                level("获取索引失败 [%s.%s]：%s", schema, table, exc)
                result = []
            self.meta.put(key, result)
            return result

    def foreign_keys(self, table: str, schema: str | None = None) -> list[dict[str, Any]]:
        schema = self._normalize_schema(schema)
        with self._meta_lock:
            key = ("fks_out", schema, table)
            cached = self.meta.get(key)
            if cached is not None:
                return cached
            insp = inspect(self.engine)
            try:
                result = insp.get_foreign_keys(table, schema=schema)
            except Exception as exc:  # noqa: BLE001
                log.debug("获取外键失败 [%s.%s]：%s", schema, table, exc)
                result = []
            self.meta.put(key, result)
            return result

    def referencing_tables(self, table: str, schema: str | None = None) -> list[dict[str, Any]]:
        schema = self._normalize_schema(schema)
        with self._meta_lock:
            key = ("fks_in", schema, table)
            cached = self.meta.get(key)
            if cached is not None:
                return cached
            insp = inspect(self.engine)
            refs: list[dict[str, Any]] = []
            try:
                all_tables = insp.get_table_names(schema=schema)
            except Exception:  # noqa: BLE001
                all_tables = []
            target = table.lower()
            for t in all_tables:
                if t.lower() == target:
                    continue
                try:
                    fks = insp.get_foreign_keys(t, schema=schema)
                except Exception:  # noqa: BLE001
                    continue
                for fk in fks:
                    ref_tbl = (fk.get("referred_table") or "").lower()
                    ref_schema = fk.get("referred_schema")
                    if ref_tbl == target:
                        if ref_schema and schema and ref_schema != schema:
                            continue
                        refs.append({
                            "table": t,
                            "columns": fk.get("constrained_columns") or [],
                            "referred_columns": fk.get("referred_columns") or [],
                            "name": fk.get("name"),
                        })
            self.meta.put(key, refs)
            return refs

    def table_context(self, table: str, schema: str | None) -> dict:
        schema = self._normalize_schema(schema)
        with self._meta_lock:
            key = ("pk", schema, table)
            pk_cols = self.meta.get(key)
            if pk_cols is None:
                insp = inspect(self.engine)
                try:
                    pks = insp.get_pk_constraint(table, schema=schema)
                    pk_cols = tuple(pks.get("constrained_columns") or ())
                except Exception:  # noqa: BLE001
                    pk_cols = ()
                self.meta.put(key, pk_cols)

            col_types: dict[str, str] = {}
            auto_cols: list[str] = []
            try:
                for c in self.columns(table, schema=schema):
                    name = c.get("name")
                    if not name:
                        continue
                    col_types[str(name)] = str(c.get("type") or "")
                    autoinc = (
                        c.get("autoincrement") is True
                        or c.get("identity") is not None
                        or "auto_increment" in str(c.get("type") or "").lower()
                        or "serial" in str(c.get("type") or "").lower()
                    )
                    if autoinc and c.get("default") is None:
                        auto_cols.append(str(name))
            except Exception as exc:  # noqa: BLE001
                log.debug("收集列类型失败 [%s.%s]：%s", schema, table, exc)

            return {
                "table": table, "schema": schema, "db_type": self.cfg.db_type,
                "primary_keys": pk_cols,
                "editable": bool(pk_cols) and not self.cfg.readonly,
                "column_types": col_types,
                "auto_increment_columns": tuple(auto_cols),
            }

    def invalidate_metadata(self) -> None:
        self.meta.invalidate()

    # ---------- DDL 生成（带全局元数据锁） ----------
    def generate_create_table(
        self,
        table: str,
        schema: str | None = None,
        *,
        include_indexes: bool = True,
        include_fks: bool = True,
    ) -> str:
        from . import schema_ddl
        with self._meta_lock:
            return schema_ddl.generate_create_table(
                self.engine, table, schema=schema,
                db_type=self.cfg.db_type,
                include_indexes=include_indexes,
                include_fks=include_fks,
                lock=self._meta_lock,
            )

    # ---------- 事务（独立连接，不占全局锁） ----------
    def open_transaction(self) -> Connection:
        return self.engine.connect()

    def commit(self, conn: Connection) -> None:
        conn.commit()

    def rollback(self, conn: Connection) -> None:
        conn.rollback()

    # ---------- 维护库连接 ----------
    def _maintenance_engine(self, database: str) -> Engine:
        url = self._url_cfg.to_url().set(database=database)
        return create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=3600,
            pool_size=2,
            max_overflow=0,
            future=True,
            connect_args=self._url_cfg.connect_args(),
        )

    # ---------- 错误翻译 ----------
    @staticmethod
    def _translate_mysql_drop_error(exc: Exception, name: str) -> Exception:
        text = str(exc)
        m = re.search(r"Schema directory '([^']+)' does not exist", text)
        if "3679" in text or m:
            path = m.group(1) if m else f".\\{name}\\"
            return RuntimeError(
                f"MySQL 报错 3679：数据库「{name}」在数据字典里仍然存在，"
                f"但磁盘上的目录已经丢失。\n"
                f"缺失目录：{path}\n\n"
                "已尝试自动补建该目录但未能成功（通常是进程无权限写 MySQL 数据目录）。\n\n"
                "请手工修复：\n"
                "1. 找到 MySQL 数据目录（Windows 常见位置：\n"
                "   C:\\ProgramData\\MySQL\\MySQL Server 8.0\\Data）；\n"
                f"2. 在该目录下手工新建一个名为「{name}」的空文件夹；\n"
                "3. 回到本程序重试删除数据库。\n\n"
                "如果不知道数据目录在哪，可以在查询页执行：\n"
                "   SHOW VARIABLES LIKE 'datadir';"
            )
        if "1044" in text or "Access denied" in text:
            return RuntimeError(
                f"权限不足：当前 MySQL 用户没有删除数据库「{name}」的权限。\n"
                "请使用具备 DROP 权限的账号，或联系 DBA 授权。"
            )
        if "1008" in text or "Can't drop database" in text:
            return RuntimeError(
                f"数据库「{name}」不存在或已被删除。\n"
                "可以点「刷新」重新加载数据库列表。"
            )
        return exc

    def _try_repair_mysql_missing_dir(self, name: str) -> bool:
        try:
            with self.engine.connect() as conn:
                row = conn.exec_driver_sql(
                    "SHOW VARIABLES LIKE 'datadir'"
                ).fetchone()
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 MySQL datadir 失败：%s", exc)
            return False
        if not row or not row[1]:
            return False
        datadir = str(row[1]).strip()
        if not datadir:
            return False
        target = os.path.join(datadir, name)
        if os.path.isdir(target):
            return False
        try:
            os.makedirs(target, exist_ok=True)
            log.info("已为 MySQL 库 %s 补建数据目录：%s", name, target)
            return True
        except OSError as exc:
            log.warning("补建目录 %s 失败：%s", target, exc)
            return False

    def _drop_database_mysql(self, name: str, sql: str) -> None:
        def _exec_once() -> None:
            with self.engine.connect() as conn:
                conn.exec_driver_sql(sql)
                try:
                    conn.commit()
                except Exception:  # noqa: BLE001
                    pass

        try:
            _exec_once()
            return
        except Exception as exc:  # noqa: BLE001
            text = str(exc)
            is_3679 = (
                "3679" in text
                or "Schema directory" in text and "does not exist" in text
            )
            if not is_3679:
                raise self._translate_mysql_drop_error(exc, name) from exc
            if not self._try_repair_mysql_missing_dir(name):
                raise self._translate_mysql_drop_error(exc, name) from exc
            try:
                _exec_once()
            except Exception as exc2:  # noqa: BLE001
                raise self._translate_mysql_drop_error(exc2, name) from exc2

    # ---------- DDL ----------
    def create_database(self, name, charset=None, collation=None) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("数据库名不能为空")
        db = self.cfg.db_type
        if db in ("sqlite", "duckdb"):
            raise RuntimeError(
                f"{db} 是基于文件的数据库，没有 CREATE DATABASE 概念。\n"
                "请新建一个连接并指定文件路径即可。"
            )
        if db == "oracle":
            raise RuntimeError(
                "Oracle 创建数据库需要通过 DBCA 工具或手动执行 CREATE DATABASE，\n"
                "客户端暂不支持此操作。"
            )
        if db == "mssql":
            ident = "[" + name.replace("]", "]]") + "]"
        else:
            ident = quote_ident(name, db)
        sql = f"CREATE DATABASE {ident}"
        if db in ("mysql", "mariadb", "tidb", "oceanbase"):
            if charset:
                sql += f" DEFAULT CHARACTER SET {charset}"
            if collation:
                sql += f" COLLATE {collation}"
        log.info("创建数据库：%s", sql)
        with self._meta_lock:
            try:
                if db in ("postgresql", "cockroachdb", "kingbase"):
                    engine = self._maintenance_engine("postgres")
                    try:
                        with engine.connect() as conn:
                            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                            conn.exec_driver_sql(sql)
                    finally:
                        engine.dispose()
                elif db == "mssql":
                    engine = self._maintenance_engine("master")
                    try:
                        with engine.connect() as conn:
                            conn.exec_driver_sql(sql)
                            conn.commit()
                    finally:
                        engine.dispose()
                else:
                    with self.engine.connect() as conn:
                        conn.exec_driver_sql(sql)
                        try:
                            conn.commit()
                        except Exception:  # noqa: BLE001
                            pass
            finally:
                self.invalidate_metadata()

    def drop_database(self, name: str) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("数据库名不能为空")
        db = self.cfg.db_type
        if db in ("sqlite", "duckdb"):
            raise RuntimeError(
                f"{db} 是基于文件的数据库，没有 DROP DATABASE 概念。\n"
                "直接删除数据文件即可。"
            )
        if db == "oracle":
            raise RuntimeError("Oracle 客户端暂不支持删除数据库。")
        if db == "mssql":
            ident = "[" + name.replace("]", "]]") + "]"
        else:
            ident = quote_ident(name, db)
        sql = f"DROP DATABASE {ident}"
        log.info("删除数据库：%s", sql)
        with self._meta_lock:
            try:
                if db in ("postgresql", "cockroachdb", "kingbase"):
                    self._drop_database_postgres(name, sql)
                elif db == "mssql":
                    self._drop_database_mssql(ident, sql)
                elif db in ("mysql", "mariadb", "tidb", "oceanbase"):
                    self._drop_database_mysql(name, sql)
                else:
                    with self.engine.connect() as conn:
                        conn.exec_driver_sql(sql)
                        try:
                            conn.commit()
                        except Exception:  # noqa: BLE001
                            pass
            finally:
                try:
                    self.engine.dispose()
                except Exception:  # noqa: BLE001
                    pass
                with self._lock:
                    self.engine = self._make_engine()
                self.invalidate_metadata()

    def _drop_database_postgres(self, name: str, sql: str) -> None:
        engine = self._maintenance_engine("postgres")
        try:
            with engine.connect() as conn:
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                conn.exec_driver_sql(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                conn.exec_driver_sql(sql)
        finally:
            engine.dispose()

    def _drop_database_mssql(self, ident: str, sql: str) -> None:
        engine = self._maintenance_engine("master")
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql(
                    f"ALTER DATABASE {ident} SET SINGLE_USER "
                    "WITH ROLLBACK IMMEDIATE"
                )
                conn.commit()
                try:
                    conn.exec_driver_sql(sql)
                    conn.commit()
                except Exception:
                    try:
                        conn.exec_driver_sql(
                            f"ALTER DATABASE {ident} SET MULTI_USER"
                        )
                        conn.commit()
                    except Exception:  # noqa: BLE001
                        pass
                    raise
        finally:
            engine.dispose()

    def drop_object(self, name, schema=None, kind="table", *,
                    cascade: bool = False) -> None:
        name = (name or "").strip()
        if not name:
            raise ValueError("对象名不能为空")
        db = self.cfg.db_type
        keyword = {
            "view": "VIEW",
            "procedure": "PROCEDURE",
            "function": "FUNCTION",
            "trigger": "TRIGGER",
            "event": "EVENT",
        }.get(kind, "TABLE")
        qualified = qualify_table(name, schema, db)
        sql = f"DROP {keyword} {qualified}"
        if cascade and db in ("postgresql", "cockroachdb", "kingbase"):
            sql += " CASCADE"
        log.info("删除对象：%s", sql)
        with self._meta_lock:
            with self.engine.connect() as conn:
                if schema and db in (
                    "mysql", "mariadb", "tidb", "oceanbase",
                    "postgresql", "cockroachdb", "kingbase", "mssql", "duckdb"
                ):
                    self._use_schema(conn, schema)
                conn.exec_driver_sql(sql)
                try:
                    conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            self.invalidate_metadata()

    # ---------- 执行（用户主动查询：独立 Connection，不占全局锁） ----------
    def execute_many(
        self,
        sql: str,
        max_rows: int = 5000,
        schema: str | None = None,
        table_ctx: dict | None = None,
        progress: Callable[[int, int], None] | None = None,
        cancel_event: threading.Event | None = None,
        tx_conn: Connection | None = None,
        *,
        row_batch_size: int = 0,
        on_batch: Callable[[int, list, list], None] | None = None,
        cancel_hard_grace: float = 1.5,
    ) -> list[QueryResult]:
        statements = split_statements(sql)
        if not statements:
            return [QueryResult(message="没有可执行的语句")]

        streaming = callable(on_batch) and row_batch_size > 0
        own_conn = tx_conn is None
        conn = tx_conn if tx_conn is not None else self.engine.connect()

        stop_watch = threading.Event()
        watcher: threading.Thread | None = None
        if cancel_event is not None:
            watcher = threading.Thread(
                target=self._watch_cancel,
                args=(conn, cancel_event, stop_watch),
                kwargs={"hard_grace": cancel_hard_grace},
                daemon=True,
            )
            watcher.start()

        results: list[QueryResult] = []
        had_failure = False
        try:
            if schema:
                self._use_schema(conn, schema)
            total = len(statements)
            for idx, stmt in enumerate(statements, 1):
                if cancel_event is not None and cancel_event.is_set():
                    raise QueryCancelled("查询已取消")
                if progress:
                    progress(idx, total)
                start = time.perf_counter()
                try:
                    r = conn.exec_driver_sql(stmt)
                    if r.returns_rows:
                        cols = list(r.keys())
                        if streaming:
                            emitted = 0
                            truncated = False
                            while True:
                                if cancel_event is not None and cancel_event.is_set():
                                    raise QueryCancelled("查询已取消")
                                batch = r.fetchmany(row_batch_size)
                                if not batch:
                                    break
                                if emitted + len(batch) > max_rows:
                                    batch = batch[: max_rows - emitted]
                                    truncated = True
                                emitted += len(batch)
                                rows_batch = [tuple(x) for x in batch]
                                try:
                                    on_batch(idx, cols, rows_batch)
                                except Exception as exc:  # noqa: BLE001
                                    log.warning("on_batch 回调异常：%s", exc)
                                if truncated:
                                    break
                            results.append(QueryResult(
                                columns=cols, rows=[], rowcount=emitted,
                                elapsed=time.perf_counter() - start,
                                message=f"[{idx}] 返回 {emitted} 行",
                                truncated=truncated,
                                table_ctx=table_ctx if total == 1 else None,
                                success=True,
                            ))
                        else:
                            fetched = r.fetchmany(max_rows + 1)
                            truncated = len(fetched) > max_rows
                            rows = [tuple(x) for x in fetched[:max_rows]]
                            results.append(QueryResult(
                                columns=cols, rows=rows, rowcount=len(rows),
                                elapsed=time.perf_counter() - start,
                                message=f"[{idx}] 返回 {len(rows)} 行",
                                truncated=truncated,
                                table_ctx=table_ctx if total == 1 else None,
                                success=True,
                            ))
                    else:
                        rc = r.rowcount or 0
                        results.append(QueryResult(
                            rowcount=rc,
                            elapsed=time.perf_counter() - start,
                            message=f"[{idx}] 影响 {rc} 行",
                            success=True,
                        ))
                except Exception as exc:  # noqa: BLE001
                    if cancel_event is not None and cancel_event.is_set():
                        raise QueryCancelled("查询已取消") from exc
                    results.append(QueryResult(
                        message=f"[{idx}] 失败：{exc}",
                        elapsed=time.perf_counter() - start,
                        success=False,
                    ))
                    had_failure = True
                    break

            if own_conn:
                try:
                    if had_failure:
                        conn.rollback()
                        for r in results:
                            r.rolled_back = True
                            r.message += "（事务已回滚）"
                    else:
                        conn.commit()
                except Exception:  # noqa: BLE001
                    pass
            return results
        finally:
            stop_watch.set()
            if watcher is not None:
                # 给 watcher 时间走完 grace 阶段（hard_grace + 缓冲）。
                watcher.join(timeout=cancel_hard_grace + 0.5)
            if own_conn:
                # 取消路径下连接状态可能已脏（socket 被强关 / 服务端已中止），
                # 直接 invalidate，避免坏连接被归还到池里。
                if cancel_event is not None and cancel_event.is_set():
                    try:
                        conn.invalidate()
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    conn.close()
                except Exception:  # noqa: BLE001
                    pass

    def _watch_cancel(
        self,
        conn: Connection,
        cancel_event: threading.Event,
        stop_event: threading.Event,
        *,
        hard_grace: float = 1.5,
    ) -> None:
        """监控取消事件。

        阶段 1（等待）：每 100ms 检查一次；stop_event 设置 → 直接退出。
        阶段 2（触发）：cancel_event 设置 → 尝试调用驱动层的 cancel()。
            这是"优雅取消"：让服务端停止发送结果，PyMySQL / psycopg2
            都会立刻让阻塞的 read 抛异常返回。
        阶段 3（兜底）：等待 hard_grace 秒仍未结束 → 硬关底层连接。
            应对"驱动没有 cancel()"或"socket 卡在 SSL 层"的场景。
            代价是该连接被标记为不可复用（主线程 finally 里会 invalidate）。
        """
        while not stop_event.wait(0.1):
            if not cancel_event.is_set():
                continue

            raw = _resolve_dbapi_connection(conn)
            cancelled = False
            if raw is not None:
                cancelled = _try_driver_cancel(raw)

            # 优雅取消后给的宽限期短一些；没有 cancel() 支持的驱动，
            # 说明大概率还要靠硬关，给稍长一点时间观察是否真的卡死。
            grace = hard_grace if cancelled else hard_grace * 0.5
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                if stop_event.wait(0.05):
                    return  # 主线程已收尾

            # 兜底：硬关底层连接
            if raw is not None:
                _try_hard_close(raw)

            # 再等一小段，给主线程捕捉异常的机会；无论如何退出。
            stop_event.wait(hard_grace)
            return

    def execute(self, sql: str, max_rows: int = 5000,
                schema: str | None = None) -> QueryResult:
        results = self.execute_many(sql, max_rows, schema)
        return results[0] if results else QueryResult()

    def execute_paged(
        self,
        base_sql: str,
        page: int = 1,
        page_size: int = 200,
        schema: str | None = None,
        count_total: bool = True,
        table_ctx: dict | None = None,
        cancel_event: threading.Event | None = None,
    ) -> QueryResult:
        if cancel_event is not None and cancel_event.is_set():
            raise QueryCancelled("查询已取消")
        page = max(1, int(page))
        page_size = max(1, int(page_size))
        offset = (page - 1) * page_size
        db = self.cfg.db_type
        stripped = base_sql.strip().rstrip(";")
        if db in ("mysql", "mariadb", "tidb", "oceanbase",
                  "postgresql", "cockroachdb", "kingbase", "sqlite", "duckdb"):
            paged = f"SELECT * FROM (\n{stripped}\n) AS _paged LIMIT {page_size} OFFSET {offset}"
        elif db == "mssql":
            if re.search(r"\border\s+by\b", stripped, re.I):
                paged = f"{stripped} OFFSET {offset} ROWS FETCH NEXT {page_size} ROWS ONLY"
            else:
                paged = (
                    f"SELECT * FROM (\n{stripped}\n) AS _paged "
                    f"ORDER BY (SELECT NULL) OFFSET {offset} ROWS FETCH NEXT {page_size} ROWS ONLY"
                )
        elif db == "oracle":
            paged = f"SELECT * FROM (\n{stripped}\n) _paged OFFSET {offset} ROWS FETCH NEXT {page_size} ROWS ONLY"
        else:
            paged = stripped

        start = time.perf_counter()
        total: int | None = None
        columns: list[str] = []
        rows: list[tuple] = []

        conn = self.engine.connect()
        stop_watch = threading.Event()
        watcher: threading.Thread | None = None
        if cancel_event is not None:
            watcher = threading.Thread(
                target=self._watch_cancel,
                args=(conn, cancel_event, stop_watch),
                kwargs={"hard_grace": 1.5},
                daemon=True,
            )
            watcher.start()
        try:
            if schema:
                self._use_schema(conn, schema)
            try:
                result = conn.exec_driver_sql(paged)
                columns = list(result.keys())
                rows = [tuple(r) for r in result.fetchall()]
                if count_total:
                    try:
                        cnt = conn.exec_driver_sql(
                            f"SELECT COUNT(*) FROM (\n{stripped}\n) AS _cnt"
                        ).scalar()
                        total = int(cnt) if cnt is not None else None
                    except Exception:
                        try:
                            count_src = re.sub(
                                r"\border\s+by\b[\s\S]*$", "", stripped, flags=re.I
                            ).strip() or stripped
                            cnt = conn.exec_driver_sql(
                                f"SELECT COUNT(*) FROM (\n{count_src}\n) AS _cnt"
                            ).scalar()
                            total = int(cnt) if cnt is not None else None
                        except Exception:  # noqa: BLE001
                            total = None
            except Exception as exc:
                if cancel_event is not None and cancel_event.is_set():
                    raise QueryCancelled("查询已取消") from exc
                raise
        finally:
            stop_watch.set()
            if watcher is not None:
                watcher.join(timeout=2.0)
            if cancel_event is not None and cancel_event.is_set():
                try:
                    conn.invalidate()
                except Exception:  # noqa: BLE001
                    pass
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

        info = PageInfo(
            page=page, page_size=page_size, total=total,
            has_next=((total is not None and page * page_size < total)
                      or len(rows) == page_size),
        )
        return QueryResult(
            columns=columns, rows=rows, rowcount=len(rows),
            elapsed=time.perf_counter() - start,
            message=f"第 {page} 页，共 {total if total is not None else '?'} 行",
            page=info, table_ctx=table_ctx, success=True,
        )

    def _use_schema(self, conn: Connection, schema: str) -> None:
        """切换当前会话的 schema；对 schema 名做标识符转义，避免注入与语法错误。"""
        try:
            # 处理多级 schema（如 DuckDB 的 catalog.schema）
            if "." in schema:
                parts = schema.split(".")
                q = ".".join(quote_ident(p, self.cfg.db_type) for p in parts)
            else:
                q = quote_ident(schema, self.cfg.db_type)

            if self.cfg.db_type in ("mysql", "mariadb", "tidb", "oceanbase"):
                conn.exec_driver_sql(f"USE {q}")
            elif self.cfg.db_type in ("postgresql", "cockroachdb", "kingbase"):
                conn.exec_driver_sql(f"SET search_path TO {q}")
            elif self.cfg.db_type == "mssql":
                conn.exec_driver_sql(f"USE {q}")
            elif self.cfg.db_type == "duckdb":
                conn.exec_driver_sql(f"USE {q}")
        except Exception as exc:  # noqa: BLE001
            log.debug("切换 schema 失败 [%s]：%s", schema, exc)