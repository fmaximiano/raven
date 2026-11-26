"""Execução de tarefas em segundo plano para a interface web (Fase 0/1).

Uma tarefa por vez, no próprio processo. Histórico em data/jobs.jsonl (um registro por mudança de
estado). Tarefas que estavam em execução quando o processo parou são marcadas como interrompidas
na inicialização. A fila durável no PostgreSQL substitui isto na Fase 2.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class JobStatus(StrEnum):
    RUNNING = "em_execucao"
    DONE = "concluida"
    FAILED = "falhou"
    INTERRUPTED = "interrompida"


class Job(BaseModel):
    id: str
    kind: str  # investigacao | benchmark
    params: dict[str, Any]
    status: JobStatus = JobStatus.RUNNING
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    run_id: str | None = None
    error: str | None = None


class Busy(RuntimeError):
    pass


class JobManager:
    def __init__(self, path: Path, keep: int = 50) -> None:
        self.path = path
        self.keep = keep
        self._current: Job | None = None  # definido de forma síncrona em start(): sem corrida
        self._tasks: set[asyncio.Task[None]] = set()
        self._jobs: dict[str, Job] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                job = Job.model_validate_json(line)
            except ValueError:
                continue
            self._jobs[job.id] = job
        for job in self._jobs.values():
            if job.status is JobStatus.RUNNING:
                job.status = JobStatus.INTERRUPTED
                job.error = "processo reiniciado durante a execução"
                self._save(job)

    def _save(self, job: Job) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(job.model_dump_json() + "\n")

    @property
    def current(self) -> Job | None:
        return self._current

    def recent(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)[: self.keep]

    def start(self, kind: str, params: dict[str, Any], work: Callable[[], Awaitable[str]]) -> Job:
        """Inicia `work` (que devolve o run_id) se nenhuma tarefa estiver em execução."""
        if self._current is not None:
            raise Busy("já existe uma tarefa em execução")
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, params=params)
        self._current = job
        self._jobs[job.id] = job
        self._save(job)

        async def _runner() -> None:
            try:
                job.run_id = await work()
                job.status = JobStatus.DONE
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"[:500]
                log.error(
                    json.dumps({"job": job.id, "error": job.error, "tb": traceback.format_exc()[-1500:]})
                )
            finally:
                job.finished_at = datetime.now(UTC)
                self._current = None
                self._save(job)

        task = asyncio.create_task(_runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def wait_idle(self) -> None:
        """Usado em testes: aguarda as tarefas em andamento."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)
