"""Protocolo de benchmark da Fase 0: mesmas consultas, mesmos critérios, todos os fornecedores.

Etapas:
1. `run`: para cada assunto × idioma × fornecedor, executa a consulta nativa fixa e grava
   cada chamada (resultados, custo, latência, filtros ignorados, erro) em results.jsonl.
   Opcionalmente consulta o Trends (YouTube) por assunto × mercado (cobertura de demanda).
2. `labels.csv`: lista única (assunto, idioma, vídeo) para rotulagem HUMANA:
   relevance = 2 mesmo assunto | 1 relacionado | 0 irrelevante; language_ok = s/n.
3. `report`: calcula métricas operacionais (sempre) e de qualidade (quando há rótulos).

Fornecedores ignorados por falta de credencial ficam registrados como bloqueados — não
recebem números inventados.
"""

from __future__ import annotations

import csv
import hashlib
import time
from pathlib import Path

from pydantic import BaseModel, Field

from raven.connectors.base import (
    Capability,
    Connector,
    ConnectorError,
    ConnectorStatus,
    NotConfigured,
    TrendsRequest,
    VideoSearchRequest,
)
from raven.context import current_run_id
from raven.domain import Subject
from raven.settings import AppConfig, load_yaml
from raven.storage.ledger import BudgetExceeded, Ledger
from raven.storage.runstore import RunStore, expiry, new_run_id

LABEL_COLUMNS = [
    "subject_id",
    "subject_label",
    "language",
    "video_id",
    "title",
    "channel",
    "url",
    "found_by",
    "relevance",
    "language_ok",
    "notes",
]


class BenchmarkCall(BaseModel):
    subject_id: str
    language: str
    market: str
    provider: str
    query: str
    ok: bool
    error: str | None = None
    blocked: bool = False  # sem credencial / capacidade
    n_results: int = 0
    video_ids: list[str] = Field(default_factory=list)
    titles: list[str] = Field(default_factory=list)
    channels: list[str | None] = Field(default_factory=list)
    language_hints: list[str | None] = Field(default_factory=list)
    cost_units: float = 0.0
    cost_unit: str = ""
    cost_usd: float | None = None
    latency_ms: float = 0.0
    ignored_filters: list[str] = Field(default_factory=list)
    simulated: bool = False


class TrendsCall(BaseModel):
    subject_id: str
    market: str
    language: str
    keywords: list[str]
    ok: bool
    error: str | None = None
    valid_series: int = 0
    presence: list[float | None] = Field(default_factory=list)
    simulated: bool = False


def load_subjects(path: Path) -> tuple[list[Subject], str]:
    raw = path.read_bytes()
    data = load_yaml(path)
    items = data.get("subjects") or []
    if not isinstance(items, list):
        raise ValueError(f"{path}: 'subjects' deve ser uma lista")
    subjects = [Subject.model_validate(s) for s in items]
    return subjects, hashlib.sha256(raw).hexdigest()[:12]


class BenchmarkRunner:
    def __init__(
        self, cfg: AppConfig, connectors: dict[str, Connector], store: RunStore, ledger: Ledger
    ) -> None:
        self.cfg = cfg
        self.connectors = connectors
        self.store = store
        self.ledger = ledger

    def _market_for(self, lang: str) -> str:
        markets = self.cfg.markets_for_language(lang)
        if not markets:
            raise ValueError(f"nenhum mercado configurado para o idioma {lang}")
        return markets[0].code

    async def run(
        self,
        subjects: list[Subject],
        *,
        languages: list[str],
        providers: list[str],
        sample_hash: str,
        queries_per_language: int = 1,
        results_per_query: int = 20,
        with_trends: bool = False,
    ) -> str:
        run_id = new_run_id("bench")
        token = current_run_id.set(run_id)
        simulated = any(
            self.connectors[p].info.status is ConnectorStatus.SIMULATED
            for p in providers
            if p in self.connectors
        )
        try:
            self.store.write_json(
                run_id,
                "manifest.json",
                {
                    "run_id": run_id,
                    "kind": "benchmark",
                    "simulated": simulated,
                    "sample_hash": sample_hash,
                    "subjects": [s.id for s in subjects],
                    "languages": languages,
                    "providers": providers,
                    "provider_status": {
                        p: (self.connectors[p].info.status.value if p in self.connectors else "ausente")
                        for p in providers
                    },
                    "queries_per_language": queries_per_language,
                    "results_per_query": results_per_query,
                    "expires_at": expiry([30]),
                },
            )
            blocked: set[str] = set()
            for subj in subjects:
                for lang in languages:
                    queries = subj.native_queries.get(lang, [])[:queries_per_language]
                    market = self._market_for(lang)
                    for provider in providers:
                        for q in queries:
                            call = await self._search(
                                provider, subj, lang, market, q, results_per_query, blocked
                            )
                            self.store.append_jsonl(run_id, "results.jsonl", call)
                if with_trends:
                    await self._trends(run_id, subj, languages)
            self._write_labels(run_id, subjects)
            self.store.write_json(run_id, "costs.json", self.ledger.run_summary(run_id))
        finally:
            current_run_id.reset(token)
        return run_id

    async def _search(
        self, provider: str, subj: Subject, lang: str, market: str, query: str, k: int, blocked: set[str]
    ) -> BenchmarkCall:
        base = {
            "subject_id": subj.id,
            "language": lang,
            "market": market,
            "provider": provider,
            "query": query,
        }
        conn = self.connectors.get(provider)
        if conn is None or not (
            conn.supports(Capability.VIDEO_SEARCH) or conn.supports(Capability.OUTLIER_SEARCH)
        ):
            return BenchmarkCall(
                **base, ok=False, blocked=True, error="fornecedor ausente ou sem capacidade de busca"
            )
        if provider in blocked:
            return BenchmarkCall(
                **base, ok=False, blocked=True, error="bloqueado em chamada anterior (credencial/cota)"
            )
        req = VideoSearchRequest(query=query, language=lang, region=market, max_results=k)
        t0 = time.perf_counter()
        try:
            res = await (
                conn.search_videos(req)
                if conn.supports(Capability.VIDEO_SEARCH)
                else conn.search_outliers(req)
            )
        except (NotConfigured, BudgetExceeded) as exc:
            blocked.add(provider)
            return BenchmarkCall(**base, ok=False, blocked=True, error=str(exc))
        except ConnectorError as exc:
            if type(exc).__name__ in ("AuthError", "QuotaExceeded"):
                blocked.add(provider)
            return BenchmarkCall(
                **base, ok=False, error=str(exc), latency_ms=(time.perf_counter() - t0) * 1000
            )
        return BenchmarkCall(
            **base,
            ok=True,
            n_results=len(res.videos),
            video_ids=[v.platform_id for v in res.videos],
            titles=[v.title for v in res.videos],
            channels=[v.channel_title for v in res.videos],
            language_hints=[v.language_hint for v in res.videos],
            cost_units=res.cost.units,
            cost_unit=res.cost.unit,
            cost_usd=res.cost.usd,
            latency_ms=res.latency_ms,
            ignored_filters=res.ignored_filters,
            simulated=any(v.provenance.simulated for v in res.videos)
            or conn.info.status is ConnectorStatus.SIMULATED,
        )

    async def _trends(self, run_id: str, subj: Subject, languages: list[str]) -> None:
        conn = next((c for c in self.connectors.values() if c.supports(Capability.SEARCH_TRENDS)), None)
        if conn is None:
            return
        from raven.pipeline.demand import summarize

        for lang in languages:
            m = self.cfg.market(self._market_for(lang))
            kws = subj.native_queries.get(lang, [])[:5]
            if not kws:
                continue
            try:
                series = await conn.search_trends(
                    TrendsRequest(
                        keywords=kws,
                        location_code=m.dataforseo_location_code,
                        language=lang,
                        search_type="youtube",
                    )
                )
                summaries = [summarize(s) for s in series]
                call = TrendsCall(
                    subject_id=subj.id,
                    market=m.code,
                    language=lang,
                    keywords=kws,
                    ok=True,
                    valid_series=sum(1 for s in summaries if s.presence is not None),
                    presence=[s.presence for s in summaries],
                    simulated=any(s.provenance.simulated for s in series),
                )
            except (ConnectorError, BudgetExceeded) as exc:
                call = TrendsCall(
                    subject_id=subj.id, market=m.code, language=lang, keywords=kws, ok=False, error=str(exc)
                )
            self.store.append_jsonl(run_id, "trends.jsonl", call)

    def _write_labels(self, run_id: str, subjects: list[Subject]) -> None:
        labels = {s.id: s.label for s in subjects}
        rows: dict[tuple[str, str, str], dict[str, str]] = {}
        path = self.store.dir(run_id) / "results.jsonl"
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            call = BenchmarkCall.model_validate_json(line)
            for vid, title, ch in zip(call.video_ids, call.titles, call.channels, strict=True):
                key = (call.subject_id, call.language, vid)
                row = rows.setdefault(
                    key,
                    {
                        "subject_id": call.subject_id,
                        "subject_label": labels.get(call.subject_id, ""),
                        "language": call.language,
                        "video_id": vid,
                        "title": title,
                        "channel": ch or "",
                        "url": "" if vid.startswith("SIM-") else f"https://www.youtube.com/watch?v={vid}",
                        "found_by": "",
                        "relevance": "",
                        "language_ok": "",
                        "notes": "",
                    },
                )
                found = set(filter(None, row["found_by"].split("|")))
                found.add(call.provider)
                row["found_by"] = "|".join(sorted(found))
        with (self.store.dir(run_id) / "labels.csv").open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS)
            w.writeheader()
            for row in sorted(rows.values(), key=lambda r: (r["subject_id"], r["language"], r["video_id"])):
                w.writerow(row)
