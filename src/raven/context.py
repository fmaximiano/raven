"""Contexto da execução corrente (id da execução para contabilizar custos)."""

from __future__ import annotations

from contextvars import ContextVar

current_run_id: ContextVar[str | None] = ContextVar("current_run_id", default=None)
