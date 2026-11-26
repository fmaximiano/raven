"""Ponta a ponta: investigação e benchmark, sem rede."""

from __future__ import annotations

import csv
import json

import pytest
import respx

from raven.benchmark.protocol import BenchmarkRunner, load_subjects
from raven.benchmark.report import render_report
from raven.connectors.dataforseo import DataForSEOConnector
from raven.connectors.registry import build_connectors
from raven.connectors.youtube_data import YouTubeDataConnector
from raven.domain import Subject, Use
from raven.llm.client import FakeLLM
from raven.llm.fake_handlers import heuristic_handler
from raven.pipeline.investigate import InvestigationRequest, Investigator
from raven.pipeline.scoring import ScoreStatus
from raven.policy import PolicyRegistry
from raven.settings import PROJECT_ROOT, Secrets
from tests.contract.test_connectors import D4, TRENDS_OK, YT, YT_SEARCH, YT_VIDEOS

SUBJECT = Subject(
    id="h01",
    label="Mecanismo de Anticítera",
    niche="history",
    origin_language="en",
    native_queries={"pt": ["mecanismo de Anticítera"], "de": ["Mechanismus von Antikythera"]},
)


async def test_simulated_investigation_writes_auditable_artifacts(cfg, policies, ledger, store):
    conns = build_connectors(cfg, Secrets(), ledger, simulate=True)
    req = InvestigationRequest(
        subject=SUBJECT,
        target_markets=["BR", "DE"],
        search_sources=["sim_search"],
        origin_sources=["sim_outliers"],
        trends_source="sim_search",
        allow_simulated=True,
    )
    res = await Investigator(cfg, policies, conns, FakeLLM(heuristic_handler), store, ledger).run(req)
    assert res.simulated and len(res.markets) == 2
    run_dir = store.root / res.run_id
    for f in (
        "manifest.json",
        "evidence.jsonl",
        "llm_calls.jsonl",
        "dossier.json",
        "dossier.md",
        "costs.json",
    ):
        assert (run_dir / f).exists(), f
    md = (run_dir / "dossier.md").read_text(encoding="utf-8")
    assert "EXECUÇÃO SIMULADA" in md and "## Referências" in md
    assert all(m.confidence and m.confidence.level == "nula" for m in res.markets)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["simulated"] is True and manifest["expires_at"]


def _real_connectors(ledger):
    return {
        "youtube_data": YouTubeDataConnector("K", ledger, min_interval_s=0),
        "dataforseo": DataForSEOConnector(
            "l", "p", ledger, location_codes={"BR": 2076, "DE": 2276}, min_interval_s=0
        ),
    }


def _mock_sources() -> None:
    respx.get(f"{YT}/search").respond(json=YT_SEARCH)
    respx.get(f"{YT}/videos").respond(json=YT_VIDEOS)
    respx.post(f"{D4}/keywords_data/google_trends/explore/live").respond(
        json={
            **TRENDS_OK,
            "tasks": [
                {
                    **TRENDS_OK["tasks"][0],
                    "result": [
                        {
                            "items": [
                                {
                                    "type": "google_trends_graph",
                                    "keywords": ["k"],
                                    "data": [
                                        {
                                            "date_from": f"2026-{i:02d}-01",
                                            "missing_data": False,
                                            "values": [10 + i],
                                        }
                                        for i in range(1, 13)
                                    ],
                                }
                            ]
                        }
                    ],
                }
            ],
        }
    )
    respx.post(f"{D4}/serp/youtube/organic/live/advanced").respond(
        json={
            "status_code": 20000,
            "cost": 0.002,
            "tasks": [{"id": "t", "status_code": 20000, "result": [{"items": []}]}],
        }
    )


@respx.mock
async def test_default_policies_keep_restricted_data_out_of_score(cfg, policies, ledger, store):
    _mock_sources()
    req = InvestigationRequest(subject=SUBJECT, target_markets=["BR"], origin_sources=[])
    res = await Investigator(
        cfg, policies, _real_connectors(ledger), FakeLLM(heuristic_handler), store, ledger
    ).run(req)
    br = res.markets[0]
    by = {a.criterion: a for a in br.assessments}
    # Demanda/momento (DataForSEO pendente) e lacuna (derivada de metadados do YouTube) ficam fora.
    assert by["destination_demand"].level is not None and not by["destination_demand"].scoring_permitted
    assert by["trend_timing"].scoring_permitted is False
    assert by["supply_gap"].scoring_permitted is False
    assert by["cultural_portability"].scoring_permitted is True
    assert br.score.status is ScoreStatus.INSUFFICIENT and br.score.point is None
    assert br.score.coverage == pytest.approx(0.30)
    # Evidência de métricas existe para exibição, mas sem llm_input.
    metrics = [e for e in res.evidence if e.id.startswith("M-")]
    assert metrics and all(Use.LLM_INPUT not in e.permitted_uses for e in metrics)


@respx.mock
async def test_no_statistics_reach_the_llm(cfg, policies, ledger, store):
    _mock_sources()
    llm = FakeLLM(heuristic_handler)
    await Investigator(cfg, policies, _real_connectors(ledger), llm, store, ledger).run(
        InvestigationRequest(subject=SUBJECT, target_markets=["BR"], origin_sources=[])
    )
    sent = " ".join(u for _, u in llm.calls)
    assert "12345" not in sent and "view_count" not in sent


@respx.mock
async def test_confirmed_vendor_permission_enters_score(cfg, ledger, store, tmp_path):
    _mock_sources()
    raw = (PROJECT_ROOT / "config" / "source_policies.yaml").read_text(encoding="utf-8")
    p = tmp_path / "pol.yaml"
    p.write_text(
        raw.replace(
            "trends:         {display: allow, llm_input: pending, scoring_input: pending",
            "trends:         {display: allow, llm_input: allow, scoring_input: allow",
        ),
        encoding="utf-8",
    )
    pol = PolicyRegistry.load(p)
    res = await Investigator(
        cfg, pol, _real_connectors(ledger), FakeLLM(heuristic_handler), store, ledger
    ).run(InvestigationRequest(subject=SUBJECT, target_markets=["BR"], origin_sources=[]))
    br = res.markets[0]
    assert br.score.coverage == pytest.approx(0.60)
    assert br.score.status is ScoreStatus.SCORED


@respx.mock
async def test_source_failure_is_isolated(cfg, policies, ledger, store, monkeypatch):
    import raven.connectors.http as http_mod

    monkeypatch.setattr(http_mod, "wait_exponential_jitter", lambda **_: lambda _rs: 0)
    respx.get(f"{YT}/search").respond(json=YT_SEARCH)
    respx.get(f"{YT}/videos").respond(json=YT_VIDEOS)
    respx.post(f"{D4}/keywords_data/google_trends/explore/live").respond(500)
    respx.post(f"{D4}/serp/youtube/organic/live/advanced").respond(500)
    res = await Investigator(
        cfg, policies, _real_connectors(ledger), FakeLLM(heuristic_handler), store, ledger
    ).run(InvestigationRequest(subject=SUBJECT, target_markets=["BR", "DE"], origin_sources=[]))
    stages = {(i.stage, i.source) for i in res.issues}
    assert ("demanda", "dataforseo") in stages and ("busca", "dataforseo") in stages
    assert all(m.score is not None for m in res.markets)  # investigação continuou
    assert all("youtube_data" in m.searched_sources for m in res.markets)


async def test_simulated_benchmark_and_labels(cfg, ledger, store):
    subjects, h = load_subjects(PROJECT_ROOT / "config" / "benchmark_subjects.yaml")
    assert len(subjects) == 30
    assert all(len(s.native_queries) >= 4 for s in subjects)
    assert {s.niche for s in subjects} == {"sci_tech", "history"}
    conns = build_connectors(cfg, Secrets(), ledger, simulate=True)
    run_id = await BenchmarkRunner(cfg, conns, store, ledger).run(
        subjects[:3],
        languages=["en", "ja", "pt", "de"],
        providers=["sim_search", "sim_outliers", "youtube_data"],
        sample_hash=h,
        with_trends=True,
    )
    run_dir = store.root / run_id
    report = render_report(run_dir)
    assert "DADOS SIMULADOS" in report and "Pendente de rotulagem" in report
    assert "youtube_data" in report  # fornecedor ausente aparece como bloqueado, sem números inventados
    # Rotula tudo como relevante e confere métricas de qualidade.
    rows = list(csv.DictReader((run_dir / "labels.csv").open(encoding="utf-8")))
    assert rows
    for r in rows:
        r["relevance"], r["language_ok"] = "2", "s"
    with (run_dir / "labels.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    report2 = render_report(run_dir)
    assert "Qualidade (rótulos humanos)" in report2 and "itens rotulados" in report2
    assert "Cobertura de demanda" in report2
