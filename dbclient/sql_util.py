"""SQL 字面量与标识符引用工具。"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any


def quote_ident(name: str | None, db_type: str = "mysql") -> str:
    if name is None:
        return ""
    if db_type == "mysql":
        return "`" + name.replace("`", "``") + "`"
    if db_type == "mssql":
        # SQL Server 官方分隔符为方括号，避免依赖 QUOTED_IDENTIFIER 会话设置。
        return "[" + name.replace("]", "]]") + "]"
    return '"' + name.replace('"', '""') + '"'


def qualify_table(table: str, schema: str | None, db_type: str = "mysql") -> str:
    """统一的 schema 限定名生成入口。

    除 SQLite 外，其它数据库在给定了 schema 的情况下统一加上前缀。
    SQLite 只有附加库（非 main/temp）才需要 schema 前缀。
    对于 DuckDB / PG 等具备多级结构的数据库，schema 可能形如 "cache.main"，
    需要将其拆分后分别 quote，生成 "cache"."main"."table"。
    """
    q = quote_ident(table, db_type)
    if not schema:
        return q
    if db_type == "sqlite":
        if schema.lower() in ("main", "temp"):
            return q
        return f"{quote_ident(schema, db_type)}.{q}"

    # 处理多级 schema（如 DuckDB 的 catalog.schema）
    if "." in schema:
        parts = schema.split(".")
        quoted_schema = ".".join(quote_ident(p, db_type) for p in parts)
        return f"{quoted_schema}.{q}"

    return f"{quote_ident(schema, db_type)}.{q}"


def sql_literal(value: Any, db_type: str = "mysql") -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        if db_type in ("mysql", "sqlite"):
            return "1" if value else "0"
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, datetime):
        return "'" + value.isoformat(sep=" ", timespec="seconds") + "'"
    if isinstance(value, (date, time)):
        return "'" + value.isoformat() + "'"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "X'" + bytes(value).hex() + "'"
    return "'" + str(value).replace("'", "''") + "'"