"""CLI do RAVEN. Uso: `uv run raven --help`."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Annotated

import typer

from raven.connectors.registry import PLANNED_ONLY, build_connectors, close_all
from raven.services import (
    BenchmarkParams,
    Ctx,
    InvestigationParams,
    MissingCredential,
    check_sources,
    costs_summary,
    purge,
    refresh_benchmark_report,
    run_benchmark,
    run_investigation,
)

app = typer.Typer(help="RAVEN — Content Radar (Fase 0: viabilidade)", no_args_is_help=True)
sources_app = typer.Typer(help="Fontes de dados", no_args_is_help=True)
bench_app = typer.Typer(help="Benchmark de fontes", no_args_is_help=True)
data_app = typer.Typer(help="Dados e retenção", no_args_is_help=True)
app.add_typer(sources_app, name="sources")
app.add_typer(bench_app, name="benchmark")
app.add_typer(data_app, name="data")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        from raven.connectors.http import redact

        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        if record.exc_info:
            entry["exc"] = redact(self.formatException(record.exc_info))[-4000:]
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: int = logging.WARNING) -> None:
    h = logging.StreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[h], force=True)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _fail(msg: str) -> typer.Exit:
    typer.secho(msg, fg="red")
    return typer.Exit(2)


@app.callback()
def _main() -> None:
    setup_logging()


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
    for r in asyncio.run(check_sources(Ctx(), live)):
        auth = {True: "autenticado", False: "FALHOU", None: "não testado"}[r.authenticated]
        color = (
            "green"
            if r.authenticated
            else "red"
            if r.authenticated is False or not r.configured
            else "yellow"
        )
        typer.secho(
            f"{r.source_id:14} credencial={'sim' if r.configured else 'não':3}  {auth:12} {r.detail} {r.observed or ''}",
            fg=color,
        )


@sources_app.command("tools")
def sources_tools(source: str = "vidiq") -> None:
    """Lista ferramentas MCP do fornecedor (tools/list não consome créditos)."""
    ctx = Ctx()
    conn = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger).get(source)
    if conn is None or not hasattr(conn, "list_tools"):
        raise _fail(f"{source} não expõe MCP")
    tools = asyncio.run(conn.list_tools())
    out = ctx.store.dir("mcp-tools") / f"{source}.json"
    out.write_text(json.dumps(tools, ensure_ascii=False, indent=2), encoding="utf-8")
    for t in tools:
        typer.echo(f"- {t['name']}: {(t['description'] or '')[:100]}")
    typer.echo(f"\nSchemas completos em {out}")


# ---- benchmark ------------------------------------------------------------------------------


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


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
    ctx = Ctx()
    params = BenchmarkParams(
        providers=_csv(providers),
        languages=_csv(languages),
        subject_ids=_csv(subjects),
        limit=limit,
        queries_per_language=queries_per_language,
        results_per_query=results_per_query,
        trends=trends,
        simulate=simulate,
    )
    try:
        run_id = asyncio.run(run_benchmark(ctx, params))
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    run_dir = ctx.store.root / run_id
    typer.secho(f"\nExecução {run_id}", bold=True)
    typer.echo(f"Relatório: {run_dir / 'report.md'}\nRotulagem: {run_dir / 'labels.csv'}")


@bench_app.command("report")
def benchmark_report(run_id: str) -> None:
    """Recalcula o relatório (após preencher labels.csv)."""
    typer.echo(refresh_benchmark_report(Ctx(), run_id))


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
    ctx = Ctx()
    params = InvestigationParams(
        subject_id=subject_id,
        label=label,
        description=description,
        niche=niche,
        origin_language=origin_language,
        origin_market=origin_market,
        targets=_csv(targets),
        seeds=_csv(seeds),
        simulate=simulate,
    )
    try:
        result = asyncio.run(run_investigation(ctx, params))
    except (ValueError, MissingCredential) as exc:
        raise _fail(str(exc)) from exc
    typer.secho(f"Execução {result.run_id}{' (SIMULADA)' if result.simulated else ''}", bold=True)
    for m in result.markets:
        sc = m.score
        shown = "—" if not sc else sc.point if sc.point is not None else f"[{sc.minimum}–{sc.maximum}]"
        typer.echo(
            f"  {m.market}: score {shown} · cobertura {sc.coverage if sc else 0:.0%} · confiança {m.confidence.level if m.confidence else '—'} · equivalentes {sum(1 for c in m.candidates if c.evidence_id)}"
        )
    if result.issues:
        typer.secho(f"  {len(result.issues)} falha(s)/bloqueio(s) registrados", fg="yellow")
    typer.echo(f"Dossiê: {ctx.store.root / result.run_id / 'dossier.md'}")


# ---- dados ----------------------------------------------------------------------------------


@data_app.command("purge")
def data_purge(dry_run: bool = False) -> None:
    """Remove execuções e cache cuja retenção expirou (política por fonte)."""
    removed, cache_removed = purge(Ctx(), dry_run)
    typer.echo(("Expiradas (simulação): " if dry_run else "Removidas: ") + (", ".join(removed) or "nenhuma"))
    if not dry_run:
        typer.echo(f"Cache de LLM: {cache_removed} entrada(s) expirada(s) removida(s)")


@app.command()
def costs(days: int = 30) -> None:
    """Gasto real registrado no ledger (exclui simulados)."""
    rows = costs_summary(Ctx(), days)
    for sid, usd, units in rows:
        typer.echo(f"{sid:14} US$ {usd:.4f}   unidades {units:.1f}")
    typer.echo(f"{'total':14} US$ {sum(r[1] for r in rows):.4f}")


@app.command()
def web(
    host: str = "0.0.0.0",
    port: Annotated[int | None, typer.Option(help="Padrão: variável PORT ou 8000")] = None,
) -> None:
    """Sobe a interface web (use RAVEN_WEB_PASSWORD para habilitar o acesso)."""
    import uvicorn

    setup_logging(logging.INFO)
    uvicorn.run(
        "raven.web.app:create_app",
        factory=True,
        host=host,
        port=port or int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
    )


if __name__ == "__main__":  # pragma: no cover
    app()
