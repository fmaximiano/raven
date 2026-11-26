from __future__ import annotations

import asyncio
import csv
import io
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from raven.settings import PROJECT_ROOT, Secrets
from raven.web.app import _csrf_token, create_app
from raven.web.jobs import Busy, JobManager, JobStatus

AUTH = ("raven", "s3nha-teste")


def _secrets(tmp_path: Path, password: str | None = AUTH[1]) -> Secrets:
    return Secrets(
        _env_file=None,
        data_dir=tmp_path,
        config_dir=PROJECT_ROOT / "config",
        web_password=SecretStr(password) if password else None,
    )


@pytest.fixture
def client(tmp_path: Path):
    with TestClient(create_app(_secrets(tmp_path))) as c:
        yield c


def _wait_job(client: TestClient, timeout: float = 20) -> None:
    jobs = client.app.state.jobs  # type: ignore[attr-defined]
    deadline = time.time() + timeout
    while jobs.current is not None and time.time() < deadline:
        time.sleep(0.05)
    assert jobs.current is None, "tarefa não terminou"


def _latest_run(client: TestClient) -> str:
    runs = sorted(p.name for p in client.app.state.ctx.store.root.iterdir())  # type: ignore[attr-defined]
    return runs[-1]


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["data_writable"] is True


def test_requires_password_configured(tmp_path):
    with TestClient(create_app(_secrets(tmp_path, password=None))) as c:
        assert c.get("/", auth=AUTH).status_code == 503


def test_wrong_credentials(client):
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("raven", "errada")).status_code == 401
    assert client.get("/", auth=AUTH).status_code == 200


def test_post_requires_csrf(client):
    r = client.post(
        "/tarefas/investigacao",
        auth=AUTH,
        data={"subject_id": "h01-antikythera", "targets": ["BR"], "simulate": "true"},
    )
    assert r.status_code == 403


def test_pages_render(client):
    for path in ("/", "/novo", "/fontes", "/custos?dias=7"):
        r = client.get(path, auth=AUTH)
        assert r.status_code == 200, path
    assert "Mecanismo de Anticítera" in client.get("/novo", auth=AUTH).text
    assert "Somente MCP" in client.get("/fontes", auth=AUTH).text


def test_real_run_without_openai_is_rejected(client):
    csrf = _csrf_token(client.app.state.ctx.secrets)  # type: ignore[attr-defined]
    r = client.post(
        "/tarefas/investigacao",
        auth=AUTH,
        data={"csrf": csrf, "subject_id": "h01-antikythera", "targets": ["BR"]},
    )
    assert r.status_code == 400 and "OPENAI" in r.text


def test_simulated_investigation_flow_and_xss(client):
    csrf = _csrf_token(client.app.state.ctx.secrets)  # type: ignore[attr-defined]
    r = client.post(
        "/tarefas/investigacao",
        auth=AUTH,
        data={
            "csrf": csrf,
            "label": "<script>alert(1)</script> Tardígrados",
            "targets": ["BR", "DE"],
            "simulate": "true",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    _wait_job(client)
    run_id = _latest_run(client)
    page = client.get(f"/execucoes/{run_id}", auth=AUTH)
    assert page.status_code == 200
    assert "EXECUÇÃO SIMULADA" in page.text
    assert "<script>alert" not in page.text and "&lt;script&gt;" in page.text
    dl = client.get(f"/execucoes/{run_id}/arquivos/dossier.md", auth=AUTH)
    assert dl.status_code == 200 and dl.text.startswith(">")
    assert client.get(f"/execucoes/{run_id}/arquivos/..%2Fledger.sqlite3", auth=AUTH).status_code == 404
    assert client.get("/execucoes/..%2F..%2Fetc/arquivos/passwd", auth=AUTH).status_code == 404
    index = client.get("/", auth=AUTH).text
    assert run_id in index and "concluida" in index


def test_simulated_benchmark_and_label_upload(client):
    csrf = _csrf_token(client.app.state.ctx.secrets)  # type: ignore[attr-defined]
    r = client.post(
        "/tarefas/benchmark",
        auth=AUTH,
        data={"csrf": csrf, "languages": ["en", "ja"], "limit": "2", "simulate": "true", "trends": "true"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    _wait_job(client)
    run_id = _latest_run(client)
    labels = client.get(f"/execucoes/{run_id}/arquivos/labels.csv", auth=AUTH).text
    rows = list(csv.DictReader(io.StringIO(labels)))
    assert rows
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(rows[0]), delimiter=";")  # formato do Excel PT-BR
    w.writeheader()
    for row in rows:
        row["relevance"], row["language_ok"] = "2", "s"
        w.writerow(row)
    up = client.post(
        f"/execucoes/{run_id}/rotulos",
        auth=AUTH,
        data={"csrf": csrf},
        files={"file": ("labels.csv", out.getvalue().encode("utf-8-sig"), "text/csv")},
        follow_redirects=False,
    )
    assert up.status_code == 303
    page = client.get(f"/execucoes/{run_id}", auth=AUTH).text
    assert "itens rotulados" in page


def test_label_upload_validation(client):
    csrf = _csrf_token(client.app.state.ctx.secrets)  # type: ignore[attr-defined]
    client.post(
        "/tarefas/benchmark",
        auth=AUTH,
        data={"csrf": csrf, "languages": ["en"], "limit": "1", "simulate": "true"},
    )
    _wait_job(client)
    run_id = _latest_run(client)
    bad = client.post(
        f"/execucoes/{run_id}/rotulos",
        auth=AUTH,
        data={"csrf": csrf},
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert bad.status_code == 400 and "Colunas ausentes" in bad.text


async def test_job_manager_single_job_and_interrupted_on_restart(tmp_path):
    path = tmp_path / "jobs.jsonl"
    jm = JobManager(path)
    gate = asyncio.Event()

    async def work() -> str:
        await gate.wait()
        return "inv-x"

    job = jm.start("investigacao", {}, work)
    with pytest.raises(Busy):
        jm.start("benchmark", {}, work)
    # Simula reinício do processo com a tarefa ainda em execução.
    restarted = JobManager(path)
    assert restarted.recent()[0].status is JobStatus.INTERRUPTED
    gate.set()
    await jm.wait_idle()
    assert job.status is JobStatus.DONE and job.run_id == "inv-x" and jm.current is None


async def test_job_failure_is_recorded(tmp_path):
    jm = JobManager(tmp_path / "jobs.jsonl")

    async def boom() -> str:
        raise RuntimeError("falha controlada")

    job = jm.start("investigacao", {}, boom)
    await jm.wait_idle()
    assert job.status is JobStatus.FAILED and "falha controlada" in (job.error or "")


async def test_dossier_markdown_keeps_paragraphs(cfg, policies, ledger, store):
    """Rótulos e listas do dossiê não podem ser emendados na renderização."""
    from raven.connectors.registry import build_connectors
    from raven.llm.client import FakeLLM
    from raven.llm.fake_handlers import heuristic_handler
    from raven.pipeline.investigate import InvestigationRequest, Investigator
    from raven.web.app import MD
    from tests.e2e.test_pipeline import SUBJECT

    conns = build_connectors(cfg, Secrets(_env_file=None), ledger, simulate=True)
    req = InvestigationRequest(
        subject=SUBJECT,
        target_markets=["BR"],
        search_sources=["sim_search"],
        origin_sources=["sim_outliers"],
        trends_source="sim_search",
        allow_simulated=True,
    )
    res = await Investigator(cfg, policies, conns, FakeLLM(heuristic_handler), store, ledger).run(req)
    html = MD.render((store.root / res.run_id / "dossier.md").read_text(encoding="utf-8"))
    assert "<p><strong>Confiança:</strong>" in html
    assert "<p><strong>Elementos do criador original a não replicar:</strong></p>" in html


def test_vidiq_catalog_download(client, monkeypatch):
    from raven.connectors.vidiq import VidIQConnector

    async def fake_tools(self):
        return [{"name": "outliers", "description": "d", "input_schema": {"type": "object"}}]

    monkeypatch.setattr(VidIQConnector, "list_tools", fake_tools)
    csrf = _csrf_token(client.app.state.ctx.secrets)  # type: ignore[attr-defined]
    assert client.post("/fontes/vidiq/catalogo", auth=AUTH, data={}).status_code == 403
    r = client.post("/fontes/vidiq/catalogo", auth=AUTH, data={"csrf": csrf})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    assert r.json()["tools"][0]["name"] == "outliers"


def test_vidiq_catalog_without_key_reports_error(client):
    csrf = _csrf_token(client.app.state.ctx.secrets)  # type: ignore[attr-defined]
    r = client.post("/fontes/vidiq/catalogo", auth=AUTH, data={"csrf": csrf})
    assert r.status_code == 502 and "RAVEN_VIDIQ_API_KEY" in r.text


def test_unexpected_source_error_does_not_break_sources_page(client, monkeypatch):
    from raven.connectors.youtube_data import YouTubeDataConnector

    async def broken(self, live=False):
        raise AttributeError("'Tool' object has no attribute 'inputSchema'")

    monkeypatch.setattr(YouTubeDataConnector, "check", broken)
    csrf = _csrf_token(client.app.state.ctx.secrets)  # type: ignore[attr-defined]
    r = client.post("/fontes/testar", auth=AUTH, data={"csrf": csrf})
    assert r.status_code == 200
    assert "erro inesperado" in r.text and "inputSchema" in r.text


def test_unhandled_error_shows_friendly_page(tmp_path, monkeypatch):
    import raven.web.app as web_app

    def boom(*a, **k):
        raise RuntimeError("falha interna de teste")

    monkeypatch.setattr(web_app, "costs_summary", boom)
    with TestClient(create_app(_secrets(tmp_path)), raise_server_exceptions=False) as c:
        r = c.get("/custos", auth=AUTH)
    assert r.status_code == 500 and "Referência" in r.text and "falha interna" not in r.text
