"""SQL 执行历史：内存 + JSON 落盘，可搜索、可清空。"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger(__name__)

HISTORY_FILE = DATA_DIR / "sql_history.json"
MAX_ENTRIES = 500


@dataclass
class HistoryEntry:
    sql: str
    connection: str = ""
    database: str = ""
    ts: float = 0.0
    elapsed: float = 0.0
    success: bool = True
    error: str = ""
    rowcount: int = 0

    def when(self) -> str:
        if not self.ts:
            return "-"
        return _dt.datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M:%S")

    def summary(self, limit: int = 90) -> str:
        text = self.sql.strip()
        line = text.splitlines()[0] if text else ""
        return line[:limit] + ("…" if len(line) > limit else "")


class SqlHistoryStore:
    def __init__(self, path: Path = HISTORY_FILE, max_entries: int = MAX_ENTRIES):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.RLock()
        self._entries: list[HistoryEntry] = []
        self.enabled = True
        self.load()

    def load(self) -> None:
        with self._lock:
            self._entries = []
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text("utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("读取 SQL 历史失败：%s", exc)
                return
            known = set(HistoryEntry.__dataclass_fields__)
            for d in raw:
                try:
                    self._entries.append(
                        HistoryEntry(**{k: v for k, v in d.items() if k in known})
                    )
                except Exception:  # noqa: BLE001
                    continue

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                data = [asdict(e) for e in self._entries[: self.max_entries]]
                self.path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001
                log.error("保存 SQL 历史失败：%s", exc)

    def add(self, entry: HistoryEntry) -> None:
        if not self.enabled:
            return
        if not entry.ts:
            entry.ts = time.time()
        with self._lock:
            # 相同 (sql, 连接, 库) 只保留最新一条，并从旧位置移除
            self._entries = [
                e for e in self._entries
                if not (e.sql == entry.sql
                        and e.connection == entry.connection
                        and e.database == entry.database)
            ]
            self._entries.insert(0, entry)
            if len(self._entries) > self.max_entries:
                del self._entries[self.max_entries:]
        self.save()

    def all(self) -> list[HistoryEntry]:
        with self._lock:
            return list(self._entries)

    def search(self, keyword: str) -> list[HistoryEntry]:
        kw = (keyword or "").strip().lower()
        with self._lock:
            if not kw:
                return list(self._entries)
            return [
                e for e in self._entries
                if kw in e.sql.lower()
                or kw in e.connection.lower()
                or kw in e.database.lower()
            ]

    def clear(self) -> None:
        with self._lock:
            self._entries = []
        self.save()

    def remove(self, entry: HistoryEntry) -> None:
        with self._lock:
            try:
                self._entries.remove(entry)
            except ValueError:
                return
        self.save()


history_store = SqlHistoryStore()