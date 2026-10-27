"""从 SQLAlchemy inspector 元数据反向生成 CREATE TABLE 语句。"""
from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from .sql_util import qualify_table, quote_ident


def _type_of(col: dict[str, Any]) -> str:
    # 修复 #14：str(t) 可能返回空串（自定义类型/未知类型），
    # 空类型会生成非法 DDL（"col_name  NOT NULL"），MySQL 直接报语法错。
    t = col.get("type")
    s = str(t) if t is not None else ""
    return s.strip() or "VARCHAR(255)"


def generate_create_table(
    engine: Engine,
    table: str,
    schema: str | None = None,
    db_type: str = "mysql",
    *,
    include_indexes: bool = True,
    include_fks: bool = True,
    lock: threading.RLock | None = None,
) -> str:
    """生成 CREATE TABLE 语句。

    lock：如果调用方已经持有进程级元数据全局锁（推荐），
    传入该锁以避免嵌套反射引发 PyMySQL SSL socket 竞态。
    """
    q = lambda name: quote_ident(name, db_type)  # noqa: E731

    def _do() -> str:
        insp = inspect(engine)
        cols = insp.get_columns(table, schema=schema)
        if not cols:
            raise RuntimeError(f"未获取到表 {table} 的列信息")

        try:
            pks = insp.get_pk_constraint(table, schema=schema)
            pk_cols: list[str] = list(pks.get("constrained_columns") or [])
        except Exception:  # noqa: BLE001
            pk_cols = []
        try:
            uniques = (
                insp.get_unique_constraints(table, schema=schema)
                if include_indexes else []
            )
        except Exception:  # noqa: BLE001
            uniques = []
        try:
            indexes = (
                insp.get_indexes(table, schema=schema)
                if include_indexes else []
            )
        except Exception:  # noqa: BLE001
            indexes = []
        try:
            fks = (
                insp.get_foreign_keys(table, schema=schema)
                if include_fks else []
            )
        except Exception:  # noqa: BLE001
            fks = []

        lines: list[str] = []
        for c in cols:
            parts = [q(c["name"]), _type_of(c)]
            if not c.get("nullable", True):
                parts.append("NOT NULL")
            default = c.get("default")
            if default:
                parts.append(f"DEFAULT {default}")
            if c.get("autoincrement") and db_type == "mysql":
                parts.append("AUTO_INCREMENT")
            lines.append("  " + " ".join(parts))

        if pk_cols:
            lines.append(
                "  PRIMARY KEY (" + ", ".join(q(k) for k in pk_cols) + ")"
            )

        for u in uniques:
            cols_u = ", ".join(q(c) for c in u["column_names"])
            name = u.get("name") or f"uq_{table}_{'_'.join(u['column_names'])}"
            lines.append(f"  CONSTRAINT {q(name)} UNIQUE ({cols_u})")

        for fk in fks:
            local = ", ".join(q(c) for c in fk["constrained_columns"])
            ref_schema = fk.get("referred_schema") or schema
            ref_table = (
                f"{q(ref_schema)}.{q(fk['referred_table'])}"
                if ref_schema and ref_schema != schema
                else q(fk["referred_table"])
            )
            remote = ", ".join(q(c) for c in fk["referred_columns"])
            name = (
                fk.get("name")
                or f"fk_{table}_{'_'.join(fk['constrained_columns'])}"
            )
            lines.append(
                f"  CONSTRAINT {q(name)} FOREIGN KEY ({local}) "
                f"REFERENCES {ref_table} ({remote})"
            )

        qualified = qualify_table(table, schema, db_type)
        ddl = f"CREATE TABLE {qualified} (\n" + ",\n".join(lines) + "\n);"

        for idx in indexes:
            if idx.get("unique") or not idx.get("column_names"):
                continue
            iname = (
                idx.get("name")
                or f"ix_{table}_{'_'.join(idx['column_names'])}"
            )
            icols = ", ".join(q(c) for c in idx["column_names"] if c)
            ddl += f"\nCREATE INDEX {q(iname)} ON {qualified} ({icols});"

        return ddl

    if lock is not None:
        with lock:
            return _do()
    return _do()