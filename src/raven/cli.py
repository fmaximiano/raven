"""CLI do RAVEN (Fase 0). Uso: `uv run raven --help`."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Annotated

import typer

from raven.connectors.registry import PLANNED_ONLY, build_connectors, close_all
from raven.domain import Subject
from raven.llm.cache import LLMCache
from raven.llm.client import FakeLLM, LLMClient, OpenAIResponsesClient
from raven.llm.fake_handlers import heuristic_handler
from raven.policy import PolicyRegistry
from raven.settings import PROJECT_ROOT, AppConfig, get_secrets, load_config
from raven.storage.ledger import Ledger
from raven.storage.runstore import RunStore

app = typer.Typer(help="RAVEN — Content Radar (Fase 0: viabilidade)", no_args_is_help=True)
sources_app = typer.Typer(help="Fontes de dados", no_args_is_help=True)
bench_app = typer.Typer(help="Benchmark de fontes", no_args_is_help=True)
data_app = typer.Typer(help="Dados e retenção", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(bench_app, name="benchmark")
app.add_typer(data_app, name="data")


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from raven.connectors.http import redact

        return json.dumps(
            {
                "ts": datetime.now(UTC).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "msg": redact(record.getMessage()),
            },
            ensure_ascii=False,
        )


def _setup_logging() -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(_JsonFormatter())
    logging.basicConfig(level=logging.WARNING, handlers=[h])
    logging.getLogger("httpx").setLevel(logging.WARNING)


class Ctx:
    def __init__(self) -> None:
        self.secrets = get_secrets()
        self.cfg: AppConfig = load_config()
        self.policies = PolicyRegistry.load()
        self.ledger = Ledger(self.secrets.data_dir / "ledger.sqlite3")
        self.store = RunStore(self.secrets.data_dir)

    def llm(self, simulate: bool) -> LLMClient:
        key = self.secrets.openai_api_key
        if simulate:
            return FakeLLM(heuristic_handler)
        if key is None:
            typer.secho(
                "RAVEN_OPENAI_API_KEY ausente: use --simulate para testar o pipeline sem custo.", fg="red"
            )
            raise typer.Exit(2)
        return OpenAIResponsesClient(
            key.get_secret_value(),
            self.cfg.llm,
            self.ledger,
            self.cfg.budgets,
            LLMCache(self.secrets.data_dir / "llm_cache.sqlite3"),
        )


@app.callback()
def _main() -> None:
    _setup_logging()


# ---- sources --------------------------------------------------------------------------------


@sources_app.command("list")
def sources_list() -> None:
    """Catálogo de fontes: estado, capacidades, credencial e política."""
    ctx = Ctx()
    conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger)
    for sid, c in conns.items():
        pol = ctx.policies.get(sid)
        typer.secho(f"\n{c.info.name} [{sid}] — {c.info.status.value}", bold=True)
        typer.echo(f"  capacidades: {', '.join(c.info.capabilities)}")
        typer.echo(
            f"  filtro idioma/região: {c.info.supports_language_filter}/{c.info.supports_region_filter}; custo em {c.info.cost_unit}"
        )
        typer.echo(f"  política: {pol.status}; retenção {pol.retention_days} dias")
        for dc, rules in pol.data_classes.items():
            typer.echo(f"    {dc}: " + ", ".join(f"{u}={p}" for u, p in rules.items()))
        for lim in c.info.limitations:
            typer.echo(f"  - {lim}")
    for p in PLANNED_ONLY:
        typer.secho(f"\n{p.name} [{p.source_id}] — não implementado", bold=True)
        typer.echo(f"  {p.reason}")
    asyncio.run(close_all(conns))


@sources_app.command("check")
def sources_check(
    live: Annotated[bool, typer.Option(help="Faz a chamada mais barata de cada fonte")] = False,
) -> None:
    """Verifica credenciais (e autenticação real com --live)."""
    ctx = Ctx()
    conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger)

    async def _run() -> None:
        try:
            for sid, c in conns.items():
                r = await c.check(live=live)
                auth = {True: "autenticado", False: "FALHOU", None: "não testado"}[r.authenticated]
                color = (
                    "green"
                    if r.authenticated
                    else "red"
                    if r.authenticated is False or not r.configured
                    else "yellow"
                )
                typer.secho(
                    f"{sid:14} credencial={'sim' if r.configured else 'não':3}  {auth:12} {r.detail} {r.observed or ''}",
                    fg=color,
                )
        finally:
            await close_all(conns)

    asyncio.run(_run())
    openai_key = ctx.secrets.openai_api_key is not None
    typer.secho(
        f"{'openai':14} credencial={'sim' if openai_key else 'não':3}", fg="green" if openai_key else "red"
    )


@sources_app.command("tools")
def sources_tools(source: str = "vidiq") -> None:
    """Lista ferramentas MCP do fornecedor (tools/list não consome créditos)."""
    ctx = Ctx()
    conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger)
    conn = conns.get(source)
    if conn is None or not hasattr(conn, "list_tools"):
        typer.secho(f"{source} não expõe MCP", fg="red")
        raise typer.Exit(2)

    async def _run() -> None:
        tools = await conn.list_tools()
        out = ctx.store.dir("mcp-tools") / f"{source}.json"
        out.write_text(json.dumps(tools, ensure_ascii=False, indent=2), encoding="utf-8")
        for t in tools:
            typer.echo(f"- {t['name']}: {(t['description'] or '')[:100]}")
        typer.echo(f"\nSchemas completos em {out}")

    asyncio.run(_run())


# ---- benchmark ------------------------------------------------------------------------------


@bench_app.command("run")
def benchmark_run(
    providers: Annotated[
        str, typer.Option(help="Separados por vírgula")
    ] = "youtube_data,dataforseo,outlierkit,vidiq",
    languages: Annotated[str, typer.Option(help="Idiomas (4–6)")] = "en,es,pt,de,ja,fr",
    subjects: Annotated[str | None, typer.Option(help="Ids separados por vírgula (padrão: todos)")] = None,
    limit: Annotated[int | None, typer.Option(help="Usar só os N primeiros assuntos")] = None,
    queries_per_language: int = 1,
    results_per_query: int | None = None,
    trends: Annotated[bool, typer.Option(help="Consultar também Trends (YouTube) por mercado")] = False,
    simulate: Annotated[bool, typer.Option(help="Usar conectores simulados")] = False,
) -> None:
    """Executa o protocolo de benchmark (mesmas consultas em todos os fornecedores)."""
    from raven.benchmark.protocol import BenchmarkRunner, load_subjects
    from raven.benchmark.report import render_report

    ctx = Ctx()
    subs, sample_hash = load_subjects(PROJECT_ROOT / ctx.cfg.benchmark.sample_file)
    if subjects:
        wanted = set(subjects.split(","))
        subs = [s for s in subs if s.id in wanted]
    if limit:
        subs = subs[:limit]
    conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger, simulate=simulate)
    provs = list(conns) if simulate else providers.split(",")
    langs = languages.split(",")
    typer.echo(
        f"{len(subs)} assuntos × {len(langs)} idiomas × {len(provs)} fornecedores × {queries_per_language} consulta(s)"
    )

    async def _run() -> str:
        try:
            return await BenchmarkRunner(ctx.cfg, conns, ctx.store, ctx.ledger).run(
                subs,
                languages=langs,
                providers=provs,
                sample_hash=sample_hash,
                queries_per_language=queries_per_language,
                results_per_query=results_per_query or ctx.cfg.benchmark.results_per_query,
                with_trends=trends,
            )
        finally:
            await close_all(conns)

    run_id = asyncio.run(_run())
    run_dir = ctx.store.root / run_id
    (run_dir / "report.md").write_text(render_report(run_dir), encoding="utf-8")
    typer.secho(f"\nExecução {run_id}", bold=True)
    typer.echo(f"Relatório: {run_dir / 'report.md'}\nRotulagem: {run_dir / 'labels.csv'}")


@bench_app.command("report")
def benchmark_report(run_id: str) -> None:
    """Recalcula o relatório (após preencher labels.csv)."""
    from raven.benchmark.report import render_report

    ctx = Ctx()
    run_dir = ctx.store.root / run_id
    (run_dir / "report.md").write_text(render_report(run_dir), encoding="utf-8")
    typer.echo(run_dir / "report.md")


# ---- investigação ---------------------------------------------------------------------------


@app.command()
def investigate(
    subject_id: Annotated[str | None, typer.Option(help="Assunto da amostra de benchmark")] = None,
    label: Annotated[str | None, typer.Option(help="Assunto livre (PT-BR)")] = None,
    description: str | None = None,
    niche: str = "sci_tech",
    origin_language: str = "en",
    origin_market: str | None = None,
    targets: Annotated[str, typer.Option(help="Mercados de destino")] = "BR,MX,DE,JP,FR",
    seeds: Annotated[str | None, typer.Option(help="Ids de vídeos-semente no YouTube")] = None,
    simulate: Annotated[bool, typer.Option(help="Conectores e LLM simulados (sem custo)")] = False,
) -> None:
    """Prova de ponta a ponta: descoberta → interpretação → busca multilíngue → evidências → dossiê."""
    from raven.benchmark.protocol import load_subjects
    from raven.pipeline.investigate import InvestigationRequest, InvestigationResult, Investigator

    ctx = Ctx()
    if subject_id:
        subs, _ = load_subjects(PROJECT_ROOT / ctx.cfg.benchmark.sample_file)
        subject = next((s for s in subs if s.id == subject_id), None)
        if subject is None:
            typer.secho(f"assunto {subject_id} não encontrado", fg="red")
            raise typer.Exit(2)
    elif label:
        subject = Subject(
            id=label.lower().replace(" ", "-")[:40],
            label=label,
            description=description,
            niche=niche,
            origin_language=origin_language,
        )
    else:
        typer.secho("informe --subject-id ou --label", fg="red")
        raise typer.Exit(2)
    if seeds:
        subject = subject.model_copy(update={"seed_video_ids": seeds.split(",")})
    conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger, simulate=simulate)
    req = InvestigationRequest(
        subject=subject,
        origin_market=origin_market,
        target_markets=targets.split(","),
        search_sources=["sim_search"] if simulate else ["youtube_data", "dataforseo"],
        origin_sources=["sim_outliers"] if simulate else ["outlierkit", "vidiq"],
        trends_source="sim_search" if simulate else "dataforseo",
        allow_simulated=simulate,
    )

    async def _run() -> InvestigationResult:
        try:
            return await Investigator(
                ctx.cfg, ctx.policies, conns, ctx.llm(simulate), ctx.store, ctx.ledger
            ).run(req)
        finally:
            await close_all(conns)

    result = asyncio.run(_run())
    run_dir = ctx.store.root / result.run_id
    typer.secho(f"Execução {result.run_id}{' (SIMULADA)' if result.simulated else ''}", bold=True)
    for m in result.markets:
        sc = m.score
        shown = "—" if not sc else sc.point if sc.point is not None else f"[{sc.minimum}–{sc.maximum}]"
        typer.echo(
            f"  {m.market}: score {shown} · cobertura {sc.coverage if sc else 0:.0%} · confiança {m.confidence.level if m.confidence else '—'} · equivalentes {sum(1 for c in m.candidates if c.evidence_id)}"
        )
    if result.issues:
        typer.secho(f"  {len(result.issues)} falha(s)/bloqueio(s) registrados", fg="yellow")
    typer.echo(f"Dossiê: {run_dir / 'dossier.md'}")


# ---- dados ----------------------------------------------------------------------------------


@data_app.command("purge")
def data_purge(dry_run: bool = False) -> None:
    """Remove execuções cuja retenção expirou (política por fonte)."""
    ctx = Ctx()
    removed = ctx.store.purge_expired(dry_run=dry_run)
    typer.echo(("Expiradas (simulação): " if dry_run else "Removidas: ") + (", ".join(removed) or "nenhuma"))
    external = [p.retention_days for p in ctx.policies.all() if p.source_id not in ("internal", "simulated")]
    if external and not dry_run:
        n = LLMCache(ctx.secrets.data_dir / "llm_cache.sqlite3").purge_older_than(min(external))
        typer.echo(f"Cache de LLM: {n} entrada(s) com mais de {min(external)} dias removida(s)")


@app.command()
def costs(days: int = 30) -> None:
    """Gasto real registrado no ledger (exclui simulados)."""
    ctx = Ctx()
    since = datetime.now(UTC) - timedelta(days=days)
    for sid in ("openai", "youtube_data", "dataforseo", "outlierkit", "vidiq"):
        typer.echo(
            f"{sid:14} US$ {ctx.ledger.usd_since(since, sid):.4f}   unidades {ctx.ledger.units_since(sid, since):.1f}"
        )
    typer.echo(f"{'total':14} US$ {ctx.ledger.usd_since(since):.4f}")


if __name__ == "__main__":  # pragma: no cover
    app()
