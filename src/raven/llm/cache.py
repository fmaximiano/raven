"""Cache de respostas de LLM por (modelo, versão do prompt, versão do schema, entrada)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def cache_key(*, model: str, task: str, prompt_version: str, schema_version: str, payload: Any) -> str:
    raw = json.dumps(
        {"m": model, "t": task, "pv": prompt_version, "sv": schema_version, "p": payload},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


class LLMCache:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, created TEXT, model TEXT,"
            " task TEXT, output TEXT)"
        )
        self._conn.commit()

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT output FROM llm_cache WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def purge_older_than(self, days: int) -> int:
        """Saídas podem conter dados externos (ex.: títulos do YouTube): expiram como eles."""
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        with self._lock:
            cur = self._conn.execute("DELETE FROM llm_cache WHERE created < ?", (cutoff,))
            self._conn.commit()
        return cur.rowcount

    def put(self, key: str, *, model: str, task: str, output: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO llm_cache VALUES (?,?,?,?,?)",
                (key, datetime.now(UTC).isoformat(), model, task, json.dumps(output, ensure_ascii=False)),
            )
            self._conn.commit()
