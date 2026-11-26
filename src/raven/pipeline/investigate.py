"""Investigação de ponta a ponta (prova de conceito da Fase 0).

descoberta/origem → interpretação → consultas nativas → busca multilíngue → correspondência
→ demanda → concorrência → avaliação editorial → score/confiança → dossiê.

Falhas de uma fonte ou mercado são registradas em `issues` e não invalidam a investigação.
"""

from __future__ import annotations

import asyncio
import json
import logging
import statistics
from typing import Any

from pydantic import BaseModel, Field

from raven.connectors.base import (
    Capability,
    Connector,
    ConnectorError,
    TrendsRequest,
    VideoSearchRequest,
)
from raven.context import current_run_id
from raven.domain import Evidence, EvidenceKind, Market, Provenance, Subject, Use, VideoRef, stable_id
from raven.llm import prompts
from raven.llm.client import LLMClient, LLMError, LLMResult
from raven.llm.safety import external_block
from raven.llm.schemas import (
    CompetitionAssessment,
    DossierDraft,
    EditorialAssessment,
    MatchValidation,
    QueryPlan,
    SubjectInterpretation,
)
from raven.pipeline.demand import TrendSummary, best_summary, demand_level, summarize, timing_level
from raven.pipeline.evidence import EvidenceStore
from raven.pipeline.matching import MatchBand, band, cosine, video_text
from raven.pipeline.scoring import (
    Confidence,
    CriterionAssessment,
    Method,
    ScoreResult,
    compute_confidence,
    compute_score,
    level_from_llm,
)
from raven.policy import PolicyRegistry
from raven.settings import AppConfig
from raven.storage.ledger import BudgetExceeded, Ledger
from raven.storage.runstore import RunStore, expiry, new_run_id

log = logging.getLogger(__name__)

EDITORIAL_CRITERIA = {"economic_potential", "cultural_portability", "production_feasibility"}
RECOVERABLE = (ConnectorError, LLMError, BudgetExceeded)


class InvestigationRequest(BaseModel):
    subject: Subject
    origin_market: str | None = None
    target_markets: list[str]
    search_sources: list[str] = Field(default_factory=lambda: ["youtube_data", "dataforseo"])
    origin_sources: list[str] = Field(default_factory=lambda: ["outlierkit", "vidiq"])
    trends_source: str = "dataforseo"
    allow_simulated: bool = False


class Issue(BaseModel):
    stage: str
    market: str | None = None
    source: str | None = None
    error: str


class CandidateMatch(BaseModel):
    video_id: str
    title: str
    channel: str | None
    published_at: str | None
    sources: list[str]
    queries: list[str]
    similarity: float | None
    band: MatchBand
    label: str | None = None
    rationale: str | None = None
    evidence_id: str | None = None


class MarketResult(BaseModel):
    market: str
    language: str
    queries: list[str]
    searched_sources: list[str]
    candidates: list[CandidateMatch]
    trend_summaries: list[TrendSummary] = Field(default_factory=list)
    competition: CompetitionAssessment | None = None
    editorial: EditorialAssessment | None = None
    assessments: list[CriterionAssessment] = Field(default_factory=list)
    score: ScoreResult | None = None
    confidence: Confidence | None = None


class ModelCall(BaseModel):
    task: str
    model: str
    prompt_version: str
    cached: bool
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    usd: float
    latency_ms: float
    simulated: bool


class InvestigationResult(BaseModel):
    run_id: str
    subject: Subject
    interpretation: SubjectInterpretation | None = None
    origin_videos: list[str] = Field(default_factory=list)
    markets: list[MarketResult] = Field(default_factory=list)
    draft: DossierDraft | None = None
    draft_warnings: list[str] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    model_calls: list[ModelCall] = Field(default_factory=list)
    simulated: bool = False


class Investigator:
    def __init__(
        self,
        cfg: AppConfig,
        policies: PolicyRegistry,
        connectors: dict[str, Connector],
        llm: LLMClient,
        store: RunStore,
        ledger: Ledger,
    ) -> None:
        self.cfg = cfg
        self.policies = policies
        self.connectors = connectors
        self.llm = llm
        self.store = store
        self.ledger = ledger

    # ---- utilitários ----------------------------------------------------------------------

    async def _llm(self, res_list: list[ModelCall], **kwargs: Any) -> LLMResult[Any]:
        res = await self.llm.structured(**kwargs)
        res_list.append(
            ModelCall(
                task=res.task,
                model=res.model,
                prompt_version=res.prompt_version,
                cached=res.cached,
                input_tokens=res.input_tokens,
                cached_tokens=res.cached_tokens,
                output_tokens=res.output_tokens,
                usd=res.usd,
                latency_ms=res.latency_ms,
                simulated=res.simulated,
            )
        )
        return res

    def _sources(self, ids: list[str], cap: Capability) -> list[Connector]:
        return [self.connectors[i] for i in ids if i in self.connectors and self.connectors[i].supports(cap)]

    # ---- execução -------------------------------------------------------------------------

    async def run(self, req: InvestigationRequest) -> InvestigationResult:
        run_id = new_run_id("inv")
        token = current_run_id.set(run_id)
        ev = EvidenceStore(self.policies, allow_simulated=req.allow_simulated)
        result = InvestigationResult(run_id=run_id, subject=req.subject)
        try:
            await self._run(req, result, ev)
        finally:
            current_run_id.reset(token)
            result.evidence = ev.all()
            result.simulated = self.llm.simulated or any(e.provenance.simulated for e in result.evidence)
            self._persist(req, result)
        return result

    async def _run(self, req: InvestigationRequest, result: InvestigationResult, ev: EvidenceStore) -> None:
        subj = req.subject
        markets = [
            self.cfg.market(code)
            for code in req.target_markets
            if code.upper() != (req.origin_market or "").upper()
        ]
        subject_ev = ev.add(
            Evidence(
                id=f"S-{subj.id}",
                kind=EvidenceKind.HYPOTHESIS,
                claim=f"Assunto proposto pelo operador: {subj.label}",
                data={"label": subj.label, "description": subj.description, "niche": subj.niche},
                provenance=Provenance(source_id="internal", data_class="operator_input"),
            )
        )

        # 1. Origem: vídeos-semente e sinais de outlier (fontes especializadas).
        origin_ev, origin_assessment = await self._origin(req, result, ev)

        # 2. Interpretação semântica.
        interp_inputs = [subject_ev, *[e for e in origin_ev if ev.allows(e, Use.LLM_INPUT)]]
        try:
            r = await self._llm(
                result.model_calls,
                task="extraction",
                instructions=prompts.INTERPRET.instructions,
                user_input=external_block("assunto_e_sementes", ev.llm_view(interp_inputs)),
                output_model=SubjectInterpretation,
                prompt_version=prompts.INTERPRET.version,
            )
            result.interpretation = r.output
        except RECOVERABLE as exc:
            result.issues.append(Issue(stage="interpretacao", error=str(exc)))
            return  # sem interpretação não há busca semântica confiável
        interp_ev = ev.add_assessment(
            prefix="I",
            key=subj.id,
            claim=f"Interpretação do assunto: {result.interpretation.core_subject}",
            market=None,
            data=result.interpretation.model_dump(),
            inputs=interp_inputs,
        )

        # 3. Consultas nativas por idioma.
        queries = await self._queries(subj, markets, result, interp_ev)

        # 4–8. Mercados em paralelo, com falhas isoladas.
        market_results = await asyncio.gather(
            *(
                self._market(
                    req, m, queries.get(m.languages[0], []), result, ev, interp_ev, origin_assessment
                )
                for m in markets
            )
        )
        result.markets = list(market_results)

        # 9. Dossiê.
        await self._dossier(result, ev)

    async def _origin(
        self, req: InvestigationRequest, result: InvestigationResult, ev: EvidenceStore
    ) -> tuple[list[Evidence], CriterionAssessment | None]:
        subj = req.subject
        out: list[Evidence] = []
        yt = self.connectors.get("youtube_data")
        if subj.seed_video_ids and yt and yt.supports(Capability.VIDEO_DETAILS):
            try:
                for v in await yt.video_details(subj.seed_video_ids):
                    out.append(
                        ev.add_video(
                            v,
                            market=req.origin_market,
                            language=subj.origin_language,
                            extra={"role": "semente"},
                        )
                    )
            except RECOVERABLE as exc:
                result.issues.append(Issue(stage="origem", source="youtube_data", error=str(exc)))
        query = (subj.native_queries.get(subj.origin_language or "", []) or [subj.label])[0]
        metrics_ev: list[Evidence] = []
        multipliers: list[float] = []
        for conn in self._sources(req.origin_sources, Capability.OUTLIER_SEARCH):
            try:
                res = await conn.search_outliers(
                    VideoSearchRequest(query=query, language=subj.origin_language, max_results=10)
                )
            except RECOVERABLE as exc:
                result.issues.append(Issue(stage="origem", source=conn.source_id, error=str(exc)))
                continue
            for v in res.videos[:5]:
                result.origin_videos.append(v.platform_id)
                out.append(
                    ev.add_video(
                        v,
                        market=req.origin_market,
                        language=subj.origin_language,
                        extra={"role": "origem", "query": query},
                    )
                )
                mid = out[-1].id.replace("V-", "M-", 1)
                if mid in ev:
                    metrics_ev.append(ev.get(mid))
                mult = (v.vendor_metrics.get("outlier") or {}).get("outlierMultiplier")
                if isinstance(mult, int | float):
                    multipliers.append(float(mult))
        if multipliers:
            med = statistics.median(multipliers)
            level = 5 if med >= 10 else 4 if med >= 5 else 3 if med >= 2 else 2 if med >= 1 else 1
            a = ev.add_assessment(
                prefix="R",
                key=f"origin|{subj.id}",
                claim=f"Mediana do multiplicador de outlier informado pelas fontes na origem: {med:.1f}x (nível {level})",
                market=req.origin_market,
                data={
                    "criterion": "origin_success",
                    "level": level,
                    "median_multiplier": med,
                    "n": len(multipliers),
                },
                inputs=metrics_ev,
                data_class="rule_assessment",
            )
            permitted = ev.allows(a, Use.SCORING_INPUT)
            return out, CriterionAssessment(
                criterion="origin_success",
                level=level,
                method=Method.RULE,
                rationale=a.claim,
                evidence_ids=[a.id],
                scoring_permitted=permitted,
                exclusion_reason=None if permitted else "política das fontes de origem não permite pontuação",
            )
        return out, None

    async def _queries(
        self, subj: Subject, markets: list[Market], result: InvestigationResult, interp_ev: Evidence
    ) -> dict[str, list[str]]:
        n = self.cfg.matching.queries_per_language
        langs = sorted({m.languages[0] for m in markets})
        out = {lang: subj.native_queries[lang][:n] for lang in langs if subj.native_queries.get(lang)}
        missing = [lang for lang in langs if lang not in out]
        if missing and result.interpretation:
            try:
                r = await self._llm(
                    result.model_calls,
                    task="query_generation",
                    instructions=prompts.QUERIES.instructions,
                    user_input=(
                        f"Idiomas: {', '.join(missing)}\n"
                        + external_block("interpretacao", result.interpretation.model_dump())
                    ),
                    output_model=QueryPlan,
                    prompt_version=prompts.QUERIES.version,
                )
                for lq in r.output.per_language:
                    if lq.language in missing:
                        out[lq.language] = [q for q in lq.queries if q.strip()][:n]
            except RECOVERABLE as exc:
                result.issues.append(Issue(stage="consultas", error=str(exc)))
        return out

    async def _market(
        self,
        req: InvestigationRequest,
        market: Market,
        queries: list[str],
        result: InvestigationResult,
        ev: EvidenceStore,
        interp_ev: Evidence,
        origin_assessment: CriterionAssessment | None,
    ) -> MarketResult:
        lang = market.languages[0]
        mr = MarketResult(
            market=market.code, language=lang, queries=queries, searched_sources=[], candidates=[]
        )
        if not queries:
            result.issues.append(
                Issue(stage="busca", market=market.code, error="sem consultas para o idioma")
            )
        # 4. Busca multilíngue.
        found: dict[str, tuple[VideoRef, set[str], set[str]]] = {}
        for conn in self._sources(req.search_sources, Capability.VIDEO_SEARCH):
            searched = False
            for q in queries:
                try:
                    res = await conn.search_videos(
                        VideoSearchRequest(
                            query=q,
                            language=lang,
                            region=market.code,
                            max_results=self.cfg.matching.max_candidates_per_query,
                        )
                    )
                    searched = True
                except RECOVERABLE as exc:
                    result.issues.append(
                        Issue(stage="busca", market=market.code, source=conn.source_id, error=str(exc))
                    )
                    break
                for v in res.videos:
                    entry = found.setdefault(v.platform_id, (v, set(), set()))
                    entry[1].add(conn.source_id)
                    entry[2].add(q)
            if searched:
                mr.searched_sources.append(conn.source_id)

        # 5. Correspondência semântica.
        await self._match(mr, found, result, ev, market)

        # 6. Demanda (Trends YouTube) — regra determinística.
        await self._demand(req, mr, market, queries, result, ev)

        # 7. Concorrência (LLM sobre evidências permitidas).
        await self._competition(mr, market, result, ev, interp_ev)

        # 8. Avaliação editorial (somente entradas internas: não herda restrições de dados de terceiros).
        await self._editorial(mr, market, result, ev, interp_ev)

        # Score e confiança.
        assessments = list(mr.assessments)
        if origin_assessment:
            assessments.append(origin_assessment)
        rights_block = bool(mr.editorial and mr.editorial.rights_risk == "bloqueante")
        mr.assessments = assessments
        mr.score = compute_score(
            assessments,
            self.cfg.scoring,
            rights_blocking=rights_block,
            rights_reason=mr.editorial.rights_notes if rights_block and mr.editorial else None,
        )
        used_ids = {eid for a in assessments if a.subscore is not None for eid in a.evidence_ids}
        used: list[Evidence] = []
        for eid in used_ids:
            if eid in ev:
                e = ev.get(eid)
                used.append(e)
                used.extend(ev.get(d) for d in e.data.get("derived_from", []) if d in ev)
        mr.confidence = compute_confidence(mr.score, used, self.cfg.scoring, simulated=self.llm.simulated)
        return mr

    async def _match(
        self,
        mr: MarketResult,
        found: dict[str, tuple[VideoRef, set[str], set[str]]],
        result: InvestigationResult,
        ev: EvidenceStore,
        market: Market,
    ) -> None:
        mc = self.cfg.matching
        assert result.interpretation is not None
        embeddable: list[tuple[VideoRef, set[str], set[str]]] = []
        for v, srcs, qs in found.values():
            prov_ok = self.policies.allows(v.provenance, Use.LLM_INPUT) or (
                v.provenance.simulated and ev.allow_simulated
            )
            if prov_ok:
                embeddable.append((v, srcs, qs))
            else:
                mr.candidates.append(
                    CandidateMatch(
                        video_id=v.platform_id,
                        title=v.title,
                        channel=v.channel_title,
                        published_at=v.published_at.isoformat() if v.published_at else None,
                        sources=sorted(srcs),
                        queries=sorted(qs),
                        similarity=None,
                        band=MatchBand.NOT_EVALUATED,
                        rationale="política da fonte não permite envio ao provedor de IA",
                    )
                )
        if not embeddable:
            return
        try:
            vecs = await self.llm.embed(
                [result.interpretation.canonical_description_en]
                + [video_text(v.title, v.description) for v, _, _ in embeddable]
            )
        except RECOVERABLE as exc:
            result.issues.append(
                Issue(stage="correspondencia", market=market.code, source="openai", error=str(exc))
            )
            return
        ref = vecs[0]
        scored = []
        for (v, srcs, qs), vec in zip(embeddable, vecs[1:], strict=True):
            sim = cosine(ref, vec)
            scored.append(
                CandidateMatch(
                    video_id=v.platform_id,
                    title=v.title,
                    channel=v.channel_title,
                    published_at=v.published_at.isoformat() if v.published_at else None,
                    sources=sorted(srcs),
                    queries=sorted(qs),
                    similarity=round(sim, 4),
                    band=band(sim, same=mc.same_subject_threshold, lower=mc.ambiguous_lower_bound),
                )
            )
        scored.sort(key=lambda c: c.similarity or 0, reverse=True)
        ambiguous = [c for c in scored if c.band is MatchBand.AMBIGUOUS][: mc.max_llm_validations_per_market]
        if ambiguous:
            try:
                r = await self._llm(
                    result.model_calls,
                    task="match_validation",
                    instructions=prompts.MATCH.instructions,
                    user_input=(
                        external_block(
                            "assunto",
                            {
                                "core_subject": result.interpretation.core_subject,
                                "description": result.interpretation.canonical_description_en,
                            },
                        )
                        + "\n"
                        + external_block(
                            "candidatos",
                            [
                                {"candidate_id": c.video_id, "title": c.title, "channel": c.channel}
                                for c in ambiguous
                            ],
                        )
                    ),
                    output_model=MatchValidation,
                    prompt_version=prompts.MATCH.version,
                )
                labels = {j.candidate_id: j for j in r.output.judgements}
                for c in ambiguous:
                    j = labels.get(c.video_id)
                    if j:
                        c.label, c.rationale = j.label, j.rationale
            except RECOVERABLE as exc:
                result.issues.append(
                    Issue(stage="validacao_correspondencia", market=market.code, error=str(exc))
                )
        for c in scored:
            if c.band is MatchBand.STRONG:
                c.label = c.label or "mesmo_assunto"
                c.rationale = c.rationale or "similaridade de embedding acima do limiar forte"
            if c.label and c.label != "incorreto":
                v = found[c.video_id][0]
                e = ev.add_video(
                    v,
                    market=market.code,
                    language=mr.language,
                    extra={"match_label": c.label, "similarity": c.similarity, "found_by": c.sources},
                )
                c.evidence_id = e.id
        mr.candidates.extend(scored)

    async def _demand(
        self,
        req: InvestigationRequest,
        mr: MarketResult,
        market: Market,
        queries: list[str],
        result: InvestigationResult,
        ev: EvidenceStore,
    ) -> None:
        conn = self.connectors.get(req.trends_source)
        if not conn or not conn.supports(Capability.SEARCH_TRENDS) or not queries:
            return
        try:
            series = await conn.search_trends(
                TrendsRequest(
                    keywords=queries[:5],
                    location_code=market.dataforseo_location_code,
                    language=mr.language,
                    search_type="youtube",
                )
            )
        except RECOVERABLE as exc:
            result.issues.append(
                Issue(stage="demanda", market=market.code, source=conn.source_id, error=str(exc))
            )
            return
        trend_ev: dict[str, Evidence] = {}
        for s in series:
            summ = summarize(s)
            mr.trend_summaries.append(summ)
            trend_ev[s.keyword] = ev.add(
                Evidence(
                    id=f"T-{market.code}-{stable_id(s.keyword, str(s.provenance.request_ref))}",
                    kind=EvidenceKind.OBSERVED,
                    claim=(
                        f'Interesse de busca no YouTube por "{s.keyword}" em {market.name} (12 meses): '
                        + (
                            f"{summ.presence:.0%} dos períodos com interesse mensurável; média recente {summ.recent_mean}/100 (índice relativo)"
                            if summ.presence is not None
                            else "dados insuficientes"
                        )
                    ),
                    market=market.code,
                    language=mr.language,
                    data={
                        "keyword": s.keyword,
                        "summary": summ.model_dump(),
                        "points": [p.model_dump() for p in s.points],
                    },
                    provenance=s.provenance,
                )
            )
        best = best_summary(mr.trend_summaries)
        if not best:
            return
        inputs = [trend_ev[best.keyword]]
        for crit, level, label in (
            ("destination_demand", demand_level(best), "presença de interesse"),
            ("trend_timing", timing_level(best), "momento (recente ÷ anterior)"),
        ):
            if level is None:
                continue
            a = ev.add_assessment(
                prefix="R",
                key=f"{crit}|{market.code}|{best.keyword}",
                claim=f'{label} para "{best.keyword}": nível {level}',
                market=market.code,
                data={"criterion": crit, "level": level, "summary": best.model_dump()},
                inputs=inputs,
                data_class="rule_assessment",
            )
            permitted = ev.allows(a, Use.SCORING_INPUT)
            mr.assessments.append(
                CriterionAssessment(
                    criterion=crit,
                    level=level,
                    method=Method.RULE,
                    rationale=a.claim,
                    evidence_ids=[a.id],
                    scoring_permitted=permitted,
                    exclusion_reason=None
                    if permitted
                    else f"política de {conn.source_id} não permite pontuação",
                )
            )

    async def _competition(
        self,
        mr: MarketResult,
        market: Market,
        result: InvestigationResult,
        ev: EvidenceStore,
        interp_ev: Evidence,
    ) -> None:
        matched = [ev.get(c.evidence_id) for c in mr.candidates if c.evidence_id]
        view = ev.llm_view(matched)
        inputs = [e for e in matched if ev.allows(e, Use.LLM_INPUT)] + [interp_ev]
        not_eval = sum(1 for c in mr.candidates if c.band is MatchBand.NOT_EVALUATED)
        context = {
            "market": market.code,
            "language": mr.language,
            "queries": mr.queries,
            "searched_sources": mr.searched_sources,
            "candidates_total": len(mr.candidates),
            "candidates_not_evaluable_by_policy": not_eval,
            "equivalents": view,
        }
        try:
            r = await self._llm(
                result.model_calls,
                task="competition_analysis",
                instructions=prompts.COMPETITION.instructions,
                user_input=external_block(
                    "interpretacao", result.interpretation.model_dump() if result.interpretation else {}
                )
                + "\n"
                + external_block("mercado_destino", context),
                output_model=CompetitionAssessment,
                prompt_version=prompts.COMPETITION.version,
            )
        except RECOVERABLE as exc:
            result.issues.append(Issue(stage="concorrencia", market=market.code, error=str(exc)))
            return
        comp = r.output
        mr.competition = comp
        a = ev.add_assessment(
            prefix="A",
            key=f"competition|{market.code}|{result.run_id}",
            claim=f"Avaliação de oferta em {market.name}: {comp.supply_situation} (lacuna {comp.supply_gap_level})",
            market=market.code,
            data=comp.model_dump(),
            inputs=inputs,
        )
        permitted = ev.allows(a, Use.SCORING_INPUT)
        mr.assessments.append(
            CriterionAssessment(
                criterion="supply_gap",
                level=level_from_llm(comp.supply_gap_level),
                method=Method.LLM,
                rationale=comp.rationale,
                evidence_ids=[a.id],
                scoring_permitted=permitted,
                exclusion_reason=None
                if permitted
                else "avaliação derivada de dados cuja política não permite pontuação",
            )
        )

    async def _editorial(
        self,
        mr: MarketResult,
        market: Market,
        result: InvestigationResult,
        ev: EvidenceStore,
        interp_ev: Evidence,
    ) -> None:
        try:
            r = await self._llm(
                result.model_calls,
                task="synthesis",
                instructions=prompts.EDITORIAL.instructions,
                user_input=external_block("interpretacao", ev.llm_view([interp_ev]))
                + "\n"
                + external_block(
                    "mercado", {"code": market.code, "name": market.name, "language": mr.language}
                ),
                output_model=EditorialAssessment,
                prompt_version=prompts.EDITORIAL.version,
            )
        except RECOVERABLE as exc:
            result.issues.append(Issue(stage="editorial", market=market.code, error=str(exc)))
            return
        ed = r.output
        mr.editorial = ed
        a = ev.add_assessment(
            prefix="A",
            key=f"editorial|{market.code}|{result.run_id}",
            claim=f"Avaliação editorial qualitativa para {market.name}",
            market=market.code,
            data=ed.model_dump(),
            inputs=[interp_ev],
        )
        permitted = ev.allows(a, Use.SCORING_INPUT)
        for cj in ed.criteria:
            if cj.criterion not in EDITORIAL_CRITERIA:
                continue
            mr.assessments.append(
                CriterionAssessment(
                    criterion=cj.criterion,
                    level=level_from_llm(cj.level),
                    method=Method.LLM,
                    rationale=cj.rationale,
                    evidence_ids=[a.id],
                    scoring_permitted=permitted,
                    exclusion_reason=None if permitted else "entradas com restrição de pontuação",
                )
            )

    async def _dossier(self, result: InvestigationResult, ev: EvidenceStore) -> None:
        from raven.pipeline.dossier import sanitize_draft

        if not result.interpretation:
            return
        markets_view = [
            {
                "market": m.market,
                "score": m.score.model_dump() if m.score else None,
                "confidence": m.confidence.model_dump() if m.confidence else None,
                "assessments": [a.model_dump() for a in m.assessments],
                "competition": m.competition.model_dump() if m.competition else None,
                "rights_risk": m.editorial.rights_risk if m.editorial else None,
                "equivalents_found": sum(1 for c in m.candidates if c.evidence_id),
            }
            for m in result.markets
        ]
        payload = {
            "subject": result.subject.label,
            "interpretation": result.interpretation.model_dump(),
            "markets": markets_view,
            "issues": [i.model_dump() for i in result.issues],
        }
        try:
            r = await self._llm(
                result.model_calls,
                task="synthesis",
                instructions=prompts.DOSSIER.instructions,
                user_input=external_block("analise", payload, max_chars_per_field=3000)
                + "\n"
                + external_block("evidencias", ev.llm_view(ev.all())),
                output_model=DossierDraft,
                prompt_version=prompts.DOSSIER.version,
            )
        except RECOVERABLE as exc:
            result.issues.append(Issue(stage="dossie", error=str(exc)))
            return
        result.draft, result.draft_warnings = sanitize_draft(r.output, {e.id for e in ev.all()})

    # ---- persistência ---------------------------------------------------------------------

    def _persist(self, req: InvestigationRequest, result: InvestigationResult) -> None:
        from raven.pipeline.dossier import render_markdown

        sources = sorted({e.provenance.source_id for e in result.evidence} - {"internal"})
        retention = [self.policies.get(s).retention_days for s in sources]
        manifest = {
            "run_id": result.run_id,
            "kind": "investigation",
            "simulated": result.simulated,
            "request": req.model_dump(mode="json"),
            "sources": sources,
            "expires_at": expiry(retention),
            "prompt_versions": {
                p.name: p.version
                for p in (
                    prompts.INTERPRET,
                    prompts.QUERIES,
                    prompts.MATCH,
                    prompts.COMPETITION,
                    prompts.EDITORIAL,
                    prompts.DOSSIER,
                )
            },
            "llm_routes": self.cfg.llm.routes,
            "issues": len(result.issues),
        }
        rid = result.run_id
        self.store.write_json(rid, "manifest.json", manifest)
        for e in result.evidence:
            self.store.append_jsonl(rid, "evidence.jsonl", e)
        for c in result.model_calls:
            self.store.append_jsonl(rid, "llm_calls.jsonl", c)
        for i in result.issues:
            self.store.append_jsonl(rid, "issues.jsonl", i)
        self.store.write_json(rid, "costs.json", self.ledger.run_summary(rid))
        self.store.write_json(rid, "dossier.json", json.loads(result.model_dump_json(exclude={"evidence"})))
        self.store.write_text(rid, "dossier.md", render_markdown(result, self.ledger.run_summary(rid)))
