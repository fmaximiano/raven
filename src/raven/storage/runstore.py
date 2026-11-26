"""Artefatos auditáveis de cada execução (Fase 0: arquivos; Fase 1: PostgreSQL).

data/runs/<run_id>/
  manifest.json    parâmetros, versões, fontes, status, simulado?, expiração
  evidence.jsonl   evidências com procedência e usos permitidos
  llm_calls.jsonl  execuções de modelo (tarefa, modelo, versão de prompt, tokens, custo, cache)
  issues.jsonl     falhas isoladas por etapa/mercado/fonte
  dossier.json / dossier.md
  costs.json       consumo agregado do ledger

A expiração respeita o menor `retention_days` entre as fontes externas presentes.
"""

from __future__ import annotations

import json
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root / "runs"
        self.root.mkdir(parents=True, exist_ok=True)

    def dir(self, run_id: str) -> Path:
        d = self.root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def write_json(self, run_id: str, name: str, obj: Any) -> Path:
        p = self.dir(run_id) / name
        data = obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return p

    def append_jsonl(self, run_id: str, name: str, obj: Any) -> None:
        data = obj.model_dump(mode="json") if isinstance(obj, BaseModel) else obj
        with (self.dir(run_id) / name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")

    def write_text(self, run_id: str, name: str, text: str) -> Path:
        p = self.dir(run_id) / name
        p.write_text(text, encoding="utf-8")
        return p

    def read_json(self, run_id: str, name: str) -> Any:
        return json.loads((self.root / run_id / name).read_text(encoding="utf-8"))

    def purge_expired(self, now: datetime | None = None, dry_run: bool = False) -> list[str]:
        now = now or datetime.now(UTC)
        removed: list[str] = []
        for d in sorted(self.root.iterdir()):
            manifest = d / "manifest.json"
            if not manifest.exists():
                continue
            expires = json.loads(manifest.read_text(encoding="utf-8")).get("expires_at")
            if expires and datetime.fromisoformat(expires) <= now:
                removed.append(d.name)
                if not dry_run:
                    shutil.rmtree(d)
        return removed


def expiry(retention_days: list[int], now: datetime | None = None) -> str | None:
    if not retention_days:
        return None
    return ((now or datetime.now(UTC)) + timedelta(days=min(retention_days))).isoformat()
