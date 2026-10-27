"""已保存查询片段：内存 + JSON 落盘，可搜索、可增删改。"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger(__name__)

SNIPPET_FILE = DATA_DIR / "sql_snippets.json"


@dataclass
class Snippet:
    name: str
    sql: str
    connection: str = ""
    database: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0

    def summary(self, limit: int = 90) -> str:
        text = (self.sql or "").strip()
        line = text.splitlines()[0] if text else ""
        return line[:limit] + ("…" if len(line) > limit else "")


class SnippetStore:
    def __init__(self, path: Path = SNIPPET_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._items: list[Snippet] = []
        self.load()

    # ---------- IO ----------
    def load(self) -> None:
        with self._lock:
            self._items = []
            if not self.path.exists():
                return
            try:
                raw = json.loads(self.path.read_text("utf-8"))
            except Exception as exc:  # noqa: BLE001
                log.warning("读取查询片段失败：%s", exc)
                return
            known = set(Snippet.__dataclass_fields__)
            for d in raw:
                try:
                    self._items.append(
                        Snippet(**{k: v for k, v in d.items() if k in known})
                    )
                except Exception:  # noqa: BLE001
                    continue

    def save(self) -> None:
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                data = [asdict(s) for s in self._items]
                self.path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001
                log.error("保存查询片段失败：%s", exc)

    # ---------- CRUD ----------
    def all(self) -> list[Snippet]:
        with self._lock:
            return list(self._items)

    def get(self, name: str) -> Snippet | None:
        with self._lock:
            return next((s for s in self._items if s.name == name), None)

    def search(self, keyword: str) -> list[Snippet]:
        kw = (keyword or "").strip().lower()
        with self._lock:
            if not kw:
                return list(self._items)
            return [
                s for s in self._items
                if kw in s.name.lower()
                or kw in s.sql.lower()
                or kw in s.connection.lower()
                or kw in s.database.lower()
                or any(kw in t.lower() for t in s.tags)
            ]

    def add(self, snippet: Snippet) -> None:
        now = time.time()
        if not snippet.created_at:
            snippet.created_at = now
        snippet.updated_at = now
        with self._lock:
            self._items = [s for s in self._items if s.name != snippet.name]
            self._items.append(snippet)
        self.save()

    def update(self, old_name: str, snippet: Snippet) -> None:
        snippet.updated_at = time.time()
        with self._lock:
            for i, s in enumerate(self._items):
                if s.name == old_name:
                    if not snippet.created_at:
                        snippet.created_at = s.created_at
                    self._items[i] = snippet
                    break
            else:
                self._items.append(snippet)
        self.save()

    def remove(self, name: str) -> None:
        with self._lock:
            self._items = [s for s in self._items if s.name != name]
        self.save()

    def clear(self) -> None:
        with self._lock:
            self._items = []
        self.save()


# 修复：降级到纯内存模式时也尝试 load()，避免磁盘上已有片段被忽略。
try:
    snippet_store = SnippetStore()
except Exception as _exc:  # noqa: BLE001
    log.warning("初始化片段存储失败，降级为内存模式：%s", _exc)
    snippet_store = SnippetStore.__new__(SnippetStore)
    snippet_store.path = SNIPPET_FILE
    snippet_store._lock = threading.RLock()
    snippet_store._items = []
    try:
        # load() 内部已有异常处理；即使磁盘只读也不会抛出
        snippet_store.load()
    except Exception:
        snippet_store._items = []