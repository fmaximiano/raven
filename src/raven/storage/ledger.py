"""Livro-razão de consumo: chamadas externas, cotas e custos (fontes e LLM).

SQLite local na Fase 0 (sem servidor). Na Fase 1 a tabela migra para o PostgreSQL (UsageCosts).
Serve a dois propósitos: (1) bloquear chamadas acima de cotas/orçamentos antes de gastá-las;
(2) relatar custo real observado por execução, fonte e operação.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    run_id TEXT,
    source_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    units REAL NOT NULL DEFAULT 0,
    unit TEXT NOT NULL,
    usd REAL,
    ok INTEGER NOT NULL DEFAULT 1,
    simulated INTEGER NOT NULL DEFAULT 0,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS usage_src_ts ON usage(source_id, ts);
CREATE INDEX IF NOT EXISTS usage_run ON usage(run_id);
"""

PACIFIC = ZoneInfo("America/Los_Angeles")  # cota do YouTube reinicia à meia-noite do Pacífico


class BudgetExceeded(RuntimeError):
    """Limite de gasto/cota atingido: a chamada não foi feita."""


class Ledger:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)

    def record(
        self,
        *,
        source_id: str,
        operation: str,
        units: float,
        unit: str,
        usd: float | None = None,
        run_id: str | None = None,
        ok: bool = True,
        simulated: bool = False,
        detail: str | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage(ts, run_id, source_id, operation, units, unit, usd, ok,"
                " simulated, detail) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    datetime.now(UTC).isoformat(),
                    run_id,
                    source_id,
                    operation,
                    units,
                    unit,
                    usd,
                    int(ok),
                    int(simulated),
                    detail,
                ),
            )
            self._conn.commit()

    def _sum(self, column: str, where: str, params: tuple[object, ...]) -> float:
        with self._lock:
            row = self._conn.execute(
                f"SELECT COALESCE(SUM({column}), 0) FROM usage WHERE {where}", params
            ).fetchone()
        return float(row[0])

    def units_since(self, source_id: str, since: datetime, operation: str | None = None) -> float:
        where = "source_id = ? AND ts >= ? AND simulated = 0"
        params: tuple[object, ...] = (source_id, since.astimezone(UTC).isoformat())
        if operation:
            where += " AND operation = ?"
            params += (operation,)
        return self._sum("units", where, params)

    def calls_since(self, source_id: str, since: datetime, operation: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM usage WHERE source_id=? AND operation=? AND ts>=? AND simulated=0",
                (source_id, operation, since.astimezone(UTC).isoformat()),
            ).fetchone()
        return int(row[0])

    def usd_since(self, since: datetime, source_id: str | None = None) -> float:
        where = "ts >= ? AND simulated = 0"
        params: tuple[object, ...] = (since.astimezone(UTC).isoformat(),)
        if source_id:
            where += " AND source_id = ?"
            params += (source_id,)
        return self._sum("usd", where, params)

    def usd_for_run(self, run_id: str) -> float:
        return self._sum("usd", "run_id = ? AND simulated = 0", (run_id,))

    def run_summary(self, run_id: str) -> list[dict[str, object]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source_id, operation, unit, COUNT(*), SUM(units), SUM(COALESCE(usd,0)),"
                " SUM(1-ok), MAX(simulated) FROM usage WHERE run_id = ?"
                " GROUP BY source_id, operation, unit ORDER BY source_id, operation",
                (run_id,),
            ).fetchall()
        keys = ("source_id", "operation", "unit", "calls", "units", "usd", "errors", "simulated")
        return [dict(zip(keys, r, strict=True)) for r in rows]


def start_of_pacific_day(now: datetime | None = None) -> datetime:
    n = (now or datetime.now(UTC)).astimezone(PACIFIC)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def start_of_utc_day(now: datetime | None = None) -> datetime:
    n = now or datetime.now(UTC)
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def start_of_utc_month(now: datetime | None = None) -> datetime:
    return start_of_utc_day(now).replace(day=1)


def rolling(days: int, now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=days)
