"""Interface web mínima do RAVEN (administrativa, operador único).

Segurança:
- HTTP Basic (RAVEN_WEB_USER / RAVEN_WEB_PASSWORD). Sem senha configurada, tudo exceto /health
  responde 503 — nunca fica aberto por omissão.
- POST exige token CSRF (HMAC da senha) além da autenticação.
- Markdown renderizado com HTML bruto desabilitado (títulos externos não injetam script) e links
  validados pelo markdown-it; Jinja2 com autoescape.
- run_id validado por regex e downloads limitados a uma lista fixa de arquivos.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt

from raven.benchmark.protocol import LABEL_COLUMNS
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
from raven.settings import Secrets, get_secrets
from raven.web.jobs import Busy, JobManager

RUN_ID = re.compile(r"^(inv|bench)-\d{8}T\d{6}Z-[0-9a-f]{6}$")
DOWNLOADS = {
    "dossier.md": "text/markdown",
    "dossier.json": "application/json",
    "evidence.jsonl": "application/x-ndjson",
    "llm_calls.jsonl": "application/x-ndjson",
    "issues.jsonl": "application/x-ndjson",
    "costs.json": "application/json",
    "manifest.json": "application/json",
    "report.md": "text/markdown",
    "results.jsonl": "application/x-ndjson",
    "trends.jsonl": "application/x-ndjson",
    "labels.csv": "text/csv",
}
MAX_UPLOAD = 5 * 1024 * 1024
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
MD = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table")
security = HTTPBasic(auto_error=False)


def _csrf_token(s: Secrets) -> str:
    key = (s.web_password.get_secret_value() if s.web_password else "").encode()
    return hmac.new(key, b"raven-csrf-v1", hashlib.sha256).hexdigest()


def require_auth(request: Request, creds: Annotated[HTTPBasicCredentials | None, Depends(security)]) -> str:
    sec: Secrets = request.app.state.secrets
    if sec.web_password is None:
        raise HTTPException(503, "Defina RAVEN_WEB_PASSWORD para habilitar a interface.")
    ok = creds is not None and (
        secrets.compare_digest(creds.username.encode(), sec.web_user.encode())
        & secrets.compare_digest(creds.password.encode(), sec.web_password.get_secret_value().encode())
    )
    if not ok:
        raise HTTPException(
            401, "Autenticação necessária", headers={"WWW-Authenticate": 'Basic realm="RAVEN"'}
        )
    return sec.web_user


def require_csrf(request: Request, csrf: Annotated[str, Form()] = "") -> None:
    if not secrets.compare_digest(csrf, _csrf_token(request.app.state.secrets)):
        raise HTTPException(403, "Token CSRF inválido. Recarregue a página.")


Auth = Annotated[str, Depends(require_auth)]
Csrf = Depends(require_csrf)


def create_app(secrets_override: Secrets | None = None) -> FastAPI:
    sec = secrets_override or get_secrets()
    try:
        sec.data_dir.mkdir(parents=True, exist_ok=True)
        probe = sec.data_dir / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"Diretório de dados {sec.data_dir} não é gravável ({exc}). "
            "Verifique o volume e as permissões (o entrypoint da imagem ajusta isso quando roda como root)."
        ) from exc
    ctx = Ctx(sec)
    jobs = JobManager(sec.data_dir / "jobs.jsonl")
    app = FastAPI(title="RAVEN", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    app.state.jobs = jobs
    app.state.secrets = sec

    def render(request: Request, name: str, **kw: Any) -> HTMLResponse:
        return TEMPLATES.TemplateResponse(
            request,
            name,
            {
                "csrf": _csrf_token(sec),
                "job": jobs.current,
                "now": datetime.now(UTC),
                "openai_ok": sec.openai_api_key is not None,
                **kw,
            },
        )

    def run_dir(run_id: str) -> Path:
        if not RUN_ID.match(run_id):
            raise HTTPException(404)
        d = ctx.store.root / run_id
        if not (d / "manifest.json").exists():
            raise HTTPException(404, "Execução não encontrada (pode ter expirado).")
        return d

    def list_runs() -> list[dict[str, Any]]:
        out = []
        for d in sorted(ctx.store.root.glob("*-*"), reverse=True):
            mf = d / "manifest.json"
            if not RUN_ID.match(d.name) or not mf.exists():
                continue
            m = json.loads(mf.read_text(encoding="utf-8"))
            req = m.get("request") or {}
            subject = (req.get("subject") or {}).get("label") or f"{len(m.get('subjects', []))} assuntos"
            out.append(
                {
                    "id": d.name,
                    "kind": m.get("kind"),
                    "simulated": m.get("simulated"),
                    "subject": subject,
                    "sources": m.get("sources") or m.get("providers") or [],
                    "issues": m.get("issues"),
                    "expires_at": (m.get("expires_at") or "")[:10],
                    "when": datetime.strptime(d.name.split("-")[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC),
                }
            )
        return out

    # ---- rotas ---------------------------------------------------------------------------

    @app.get("/health")
    def health() -> JSONResponse:
        data_ok = True
        try:
            probe = sec.data_dir / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            data_ok = False
        return JSONResponse(
            {"status": "ok" if data_ok else "degraded", "data_writable": data_ok},
            status_code=200 if data_ok else 503,
        )

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, _: Auth) -> HTMLResponse:
        return render(request, "index.html", runs=list_runs(), jobs=jobs.recent()[:10])

    @app.get("/novo", response_class=HTMLResponse)
    def new(request: Request, _: Auth) -> HTMLResponse:
        subjects, _h = ctx.sample()
        return render(
            request,
            "new.html",
            subjects=subjects,
            markets=ctx.cfg.markets,
            languages=ctx.cfg.languages,
            providers=["youtube_data", "dataforseo", "outlierkit", "vidiq"],
        )

    @app.post("/tarefas/investigacao", dependencies=[Csrf])
    async def start_investigation(
        _: Auth,
        subject_id: Annotated[str, Form()] = "",
        label: Annotated[str, Form()] = "",
        description: Annotated[str, Form()] = "",
        niche: Annotated[str, Form()] = "sci_tech",
        origin_language: Annotated[str, Form()] = "en",
        targets: Annotated[list[str] | None, Form()] = None,
        simulate: Annotated[bool, Form()] = False,
    ) -> RedirectResponse:
        params = InvestigationParams(
            subject_id=subject_id or None,
            label=label or None,
            description=description or None,
            niche=niche,
            origin_language=origin_language,
            targets=tuple(targets or ()),
            simulate=simulate,
        )
        if not params.targets:
            raise HTTPException(400, "Escolha ao menos um mercado de destino.")
        try:
            ctx.llm(simulate)  # valida credencial antes de enfileirar
            from raven.services import resolve_subject

            resolve_subject(ctx, params)
        except (ValueError, MissingCredential) as exc:
            raise HTTPException(400, str(exc)) from exc

        async def work() -> str:
            return (await run_investigation(ctx, params)).run_id

        try:
            jobs.start("investigacao", {k: v for k, v in params.__dict__.items() if v}, work)
        except Busy as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse("/", status_code=303)

    @app.post("/tarefas/benchmark", dependencies=[Csrf])
    async def start_benchmark(
        _: Auth,
        providers: Annotated[list[str] | None, Form()] = None,
        languages: Annotated[list[str] | None, Form()] = None,
        limit: Annotated[int, Form()] = 5,
        queries_per_language: Annotated[int, Form()] = 1,
        trends: Annotated[bool, Form()] = False,
        simulate: Annotated[bool, Form()] = False,
    ) -> RedirectResponse:
        params = BenchmarkParams(
            providers=tuple(providers or ()),
            languages=tuple(languages or ()),
            limit=max(1, min(limit, 30)),
            queries_per_language=max(1, min(queries_per_language, 2)),
            trends=trends,
            simulate=simulate,
        )
        if not params.languages or (not params.providers and not simulate):
            raise HTTPException(400, "Escolha idiomas e fornecedores.")

        async def work() -> str:
            return await run_benchmark(ctx, params)

        try:
            jobs.start("benchmark", {k: v for k, v in params.__dict__.items() if v}, work)
        except Busy as exc:
            raise HTTPException(409, str(exc)) from exc
        return RedirectResponse("/", status_code=303)

    @app.get("/execucoes/{run_id}", response_class=HTMLResponse)
    def show_run(request: Request, run_id: str, _: Auth) -> HTMLResponse:
        d = run_dir(run_id)
        manifest = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        doc = d / ("report.md" if manifest.get("kind") == "benchmark" else "dossier.md")
        html = (
            MD.render(doc.read_text(encoding="utf-8"))
            if doc.exists()
            else "<p>Documento ainda não gerado.</p>"
        )
        files = [n for n in DOWNLOADS if (d / n).exists()]
        return render(request, "run.html", run_id=run_id, manifest=manifest, body=html, files=files)

    @app.get("/execucoes/{run_id}/arquivos/{name}")
    def download(run_id: str, name: str, _: Auth) -> FileResponse:
        d = run_dir(run_id)
        if name not in DOWNLOADS or not (d / name).exists():
            raise HTTPException(404)
        return FileResponse(d / name, media_type=DOWNLOADS[name], filename=f"{run_id}-{name}")

    @app.post("/execucoes/{run_id}/rotulos", dependencies=[Csrf])
    async def upload_labels(run_id: str, _: Auth, file: Annotated[UploadFile, File()]) -> RedirectResponse:
        d = run_dir(run_id)
        raw = await file.read(MAX_UPLOAD + 1)
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(413, "Arquivo maior que 5 MB.")
        try:
            text = raw.decode("utf-8-sig")
            sample = text[:4096]
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
            rows = list(csv.DictReader(io.StringIO(text), dialect=dialect))
        except (UnicodeDecodeError, csv.Error) as exc:
            raise HTTPException(400, f"CSV inválido: {exc}") from exc
        missing = set(LABEL_COLUMNS) - set(rows[0].keys() if rows else ())
        if missing:
            raise HTTPException(400, f"Colunas ausentes: {', '.join(sorted(missing))}")
        bad = [r["relevance"] for r in rows if (r.get("relevance") or "").strip() not in {"", "0", "1", "2"}]
        if bad:
            raise HTTPException(400, f"Valores inválidos em relevance (use 2, 1 ou 0): {bad[:5]}")
        with (d / "labels.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        refresh_benchmark_report(ctx, run_id)
        return RedirectResponse(f"/execucoes/{run_id}", status_code=303)

    @app.get("/fontes", response_class=HTMLResponse)
    async def sources(request: Request, _: Auth) -> HTMLResponse:
        conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger)
        infos = [(c.info, ctx.policies.get(sid)) for sid, c in conns.items()]
        await close_all(conns)
        reports = await check_sources(ctx, live=False)
        return render(request, "sources.html", infos=infos, reports=reports, planned=PLANNED_ONLY, live=False)

    @app.post("/fontes/testar", dependencies=[Csrf], response_class=HTMLResponse)
    async def sources_live(request: Request, _: Auth) -> HTMLResponse:
        conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger)
        infos = [(c.info, ctx.policies.get(sid)) for sid, c in conns.items()]
        await close_all(conns)
        reports = await check_sources(ctx, live=True)
        return render(request, "sources.html", infos=infos, reports=reports, planned=PLANNED_ONLY, live=True)

    @app.post("/fontes/vidiq/catalogo", dependencies=[Csrf])
    async def vidiq_catalog(_: Auth) -> Response:
        """Catálogo de ferramentas MCP do vidIQ (tools/list não consome créditos)."""
        from raven.connectors.base import ConnectorError
        from raven.connectors.vidiq import VidIQConnector

        conns = build_connectors(ctx.cfg, ctx.secrets, ctx.ledger)
        conn = conns.get("vidiq")
        try:
            if not isinstance(conn, VidIQConnector):
                raise HTTPException(400, "Conector do vidIQ desativado na configuração.")
            tools = await conn.list_tools()
        except ConnectorError as exc:
            raise HTTPException(502, f"Falha ao consultar o vidIQ: {exc}") from exc
        finally:
            await close_all(conns)
        body = json.dumps(
            {"fetched_at": datetime.now(UTC).isoformat(), "tools": tools}, ensure_ascii=False, indent=2
        )
        return Response(
            body,
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="vidiq-mcp-tools.json"'},
        )

    @app.get("/custos", response_class=HTMLResponse)
    def costs(request: Request, _: Auth, dias: int = 30) -> HTMLResponse:
        rows = costs_summary(ctx, max(1, min(dias, 365)))
        return render(request, "costs.html", rows=rows, days=dias, budgets=ctx.cfg.budgets)

    @app.post("/dados/expurgar", dependencies=[Csrf])
    def data_purge(_: Auth) -> RedirectResponse:
        purge(ctx)
        return RedirectResponse("/", status_code=303)

    return app
