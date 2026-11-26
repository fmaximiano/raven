"""Operações de alto nível compartilhadas pela CLI e pela interface web."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from raven.benchmark.protocol import BenchmarkRunner, load_subjects
from raven.benchmark.report import render_report
from raven.connectors.base import Connector, HealthReport
from raven.connectors.http import redact
from raven.connectors.registry import build_connectors, close_all
from raven.connectors.vidiq import root_cause
from raven.domain import Subject
from raven.llm.cache import LLMCache
from raven.llm.client import FakeLLM, LLMClient, OpenAIResponsesClient
from raven.llm.fake_handlers import heuristic_handler
from raven.pipeline.investigate import InvestigationRequest, InvestigationResult, Investigator
from raven.policy import PolicyRegistry
from raven.settings import AppConfig, Secrets, get_secrets, load_config
from raven.storage.ledger import Ledger
from raven.storage.runstore import RunStore

log = logging.getLogger(__name__)

SOURCE_IDS = ("openai", "youtube_data", "dataforseo", "outlierkit", "vidiq")


class MissingCredential(RuntimeError):
    pass


class Ctx:
    def __init__(self, secrets: Secrets | None = None) -> None:
        self.secrets = secrets or get_secrets()
        self.cfg: AppConfig = load_config(self.secrets.config_dir)
        self.policies = PolicyRegistry.load(self.secrets.config_dir / "source_policies.yaml")
        self.ledger = Ledger(self.secrets.data_dir / "ledger.sqlite3")
        self.store = RunStore(self.secrets.data_dir)

    @property
    def sample_path(self) -> Path:
        # sample_file é relativo à raiz do projeto (pai de config/).
        return self.secrets.config_dir.parent / self.cfg.benchmark.sample_file

    def sample(self) -> tuple[list[Subject], str]:
        return load_subjects(self.sample_path)

    def llm(self, simulate: bool) -> LLMClient:
        if simulate:
            return FakeLLM(heuristic_handler)
        key = self.secrets.openai_api_key
        if key is None:
            raise MissingCredential(
                "RAVEN_OPENAI_API_KEY ausente: use o modo simulado para testar sem custo."
            )
        return OpenAIResponsesClient(
            key.get_secret_value(),
            self.cfg.llm,
            self.ledger,
            self.cfg.budgets,
            LLMCache(self.secrets.data_dir / "llm_cache.sqlite3"),
        )


@dataclass
class InvestigationParams:
    subject_id: str | None = None
    label: str | None = None
    description: str | None = None
    niche: str = "sci_tech"
    origin_language: str = "en"
    origin_market: str | None = None
    targets: tuple[str, ...] = ("BR", "MX", "DE", "JP", "FR")
    seeds: tuple[str, ...] = ()
    simulate: bool = False


def resolve_subject(ctx: Ctx, p: InvestigationParams) -> Subject:
    if p.subject_id:
        subject = next((s for s in ctx.sample()[0] if s.id == p.subject_id), None)
        if subject is None:
            raise ValueError(f"assunto {p.subject_id} não encontrado na amostra")
    elif p.label and p.label.strip():
        label = p.label.strip()
        slug = "".join(c if c.isalnum() else "-" for c in label.lower()).strip("-")[:40]
        subject = Subject(
            id=slug or "assunto",
            label=label,
            description=p.description,
            niche=p.niche,
            origin_language=p.origin_language,
        )
    else:
        raise ValueError("informe um assunto da amostra ou um assunto livre")
    if p.seeds:
        subject = subject.model_copy(update={"seed_video_ids": list(p.seeds)})
    return subject


async def run_investigation(ctx: Ctx, p: InvestigationParams) -> InvestigationResult:
    subject = resolve_subject(ctx, p)
    llm = ctx.llm(p.simulate)  # falha cedo se faltar credencial
    conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger, simulate=p.simulate)
    req = InvestigationRequest(
        subject=subject,
        origin_market=p.origin_market,
        target_markets=list(p.targets),
        search_sources=["sim_search"] if p.simulate else ["youtube_data", "dataforseo"],
        origin_sources=["sim_outliers"] if p.simulate else ["outlierkit", "vidiq"],
        trends_source="sim_search" if p.simulate else "dataforseo",
        allow_simulated=p.simulate,
    )
    try:
        return await Investigator(ctx.cfg, ctx.policies, conns, llm, ctx.store, ctx.ledger).run(req)
    finally:
        await close_all(conns)


@dataclass
class BenchmarkParams:
    providers: tuple[str, ...] = ("youtube_data", "dataforseo", "outlierkit", "vidiq")
    languages: tuple[str, ...] = ("en", "es", "pt", "de", "ja", "fr")
    subject_ids: tuple[str, ...] = ()
    limit: int | None = None
    queries_per_language: int = 1
    results_per_query: int | None = None
    trends: bool = False
    simulate: bool = False


async def run_benchmark(ctx: Ctx, p: BenchmarkParams) -> str:
    subjects, sample_hash = ctx.sample()
    if p.subject_ids:
        wanted = set(p.subject_ids)
        subjects = [s for s in subjects if s.id in wanted]
    if p.limit:
        subjects = subjects[: p.limit]
    if not subjects:
        raise ValueError("nenhum assunto selecionado")
    conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger, simulate=p.simulate)
    providers = list(conns) if p.simulate else list(p.providers)
    try:
        run_id = await BenchmarkRunner(ctx.cfg, conns, ctx.store, ctx.ledger).run(
            subjects,
            languages=list(p.languages),
            providers=providers,
            sample_hash=sample_hash,
            queries_per_language=p.queries_per_language,
            results_per_query=p.results_per_query or ctx.cfg.benchmark.results_per_query,
            with_trends=p.trends,
        )
    finally:
        await close_all(conns)
    refresh_benchmark_report(ctx, run_id)
    return run_id


def refresh_benchmark_report(ctx: Ctx, run_id: str) -> Path:
    run_dir = ctx.store.root / run_id
    out = run_dir / "report.md"
    out.write_text(render_report(run_dir), encoding="utf-8")
    return out


async def _safe_check(conn: Connector, live: bool, timeout_s: float) -> HealthReport:
    """Nenhuma falha de uma fonte pode derrubar a verificação das demais."""
    try:
        return await asyncio.wait_for(conn.check(live=live), timeout=timeout_s)
    except TimeoutError:
        return HealthReport(
            source_id=conn.source_id,
            configured=True,
            authenticated=False,
            detail=f"sem resposta em {timeout_s:.0f} s",
        )
    except Exception as exc:
        log.exception("falha inesperada ao verificar %s", conn.source_id)
        return HealthReport(
            source_id=conn.source_id,
            configured=True,
            authenticated=False,
            detail=f"erro inesperado ({redact(root_cause(exc))[:300]})",
        )


async def check_sources(ctx: Ctx, live: bool, timeout_s: float = 60.0) -> list[HealthReport]:
    conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger)
    try:
        reports = [await _safe_check(c, live, timeout_s) for c in conns.values()]
    finally:
        await close_all(conns)
    reports.append(
        HealthReport(
            source_id="openai",
            configured=ctx.secrets.openai_api_key is not None,
            detail="credencial presente" if ctx.secrets.openai_api_key else "chave ausente",
        )
    )
    return reports


def purge(ctx: Ctx, dry_run: bool = False) -> tuple[list[str], int]:
    removed = ctx.store.purge_expired(dry_run=dry_run)
    external = [p.retention_days for p in ctx.policies.all() if p.source_id not in ("internal", "simulated")]
    cache_removed = 0
    if external and not dry_run:
        cache_removed = LLMCache(ctx.secrets.data_dir / "llm_cache.sqlite3").purge_older_than(min(external))
    return removed, cache_removed


def costs_summary(ctx: Ctx, days: int = 30) -> list[tuple[str, float, float]]:
    since = datetime.now(UTC) - timedelta(days=days)
    return [(sid, ctx.ledger.usd_since(since, sid), ctx.ledger.units_since(sid, since)) for sid in SOURCE_IDS]
