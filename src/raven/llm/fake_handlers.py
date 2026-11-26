"""Respostas SINTÉTICAS de LLM, válidas pelo schema, para testes e modo --simulate.

Não fazem análise real: servem para exercitar o pipeline sem custo e sem credenciais.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel

from raven.llm.schemas import (
    CompetitionAssessment,
    CriterionJudgement,
    DossierDraft,
    EditorialAssessment,
    LanguageQueries,
    MatchJudgement,
    MatchValidation,
    QueryPlan,
    Statement,
    SubjectInterpretation,
)

_TAG = "[SIMULADO]"


def _first_label(user_input: str) -> str:
    m = re.search(r'"label":\s*"([^"]+)"', user_input) or re.search(
        r'"core_subject":\s*"([^"]+)"', user_input
    )
    return m.group(1) if m else "assunto"


def heuristic_handler(task: str, user_input: str, model: type[BaseModel]) -> BaseModel:
    label = _first_label(user_input)
    if model is SubjectInterpretation:
        return SubjectInterpretation(
            core_subject=label,
            canonical_description_en=label,
            entities=[],
            concepts=[label],
            niche="indefinido",
            subniche="indefinido",
            approach_type="explicativo",
            narrative_hook=f"{_TAG} gancho",
            culture_specific_elements=[],
            facts_to_verify=[f"{_TAG} verificar fatos centrais"],
            creator_specific_expression=[],
        )
    if model is QueryPlan:
        langs = re.search(r"Idiomas: ([a-z, ]+)", user_input)
        codes = [c.strip() for c in (langs.group(1) if langs else "en").split(",")]
        return QueryPlan(
            per_language=[
                LanguageQueries(language=c, queries=[f"{label} {c}"], alternative_names=[], notes=_TAG)
                for c in codes
            ]
        )
    if model is MatchValidation:
        ids = re.findall(r'"candidate_id":\s*"([^"]+)"', user_input)
        return MatchValidation(
            judgements=[
                MatchJudgement(
                    candidate_id=i,
                    label="mesmo_assunto" if n % 2 == 0 else "assunto_relacionado",
                    rationale=_TAG,
                )
                for n, i in enumerate(ids)
            ]
        )
    if model is CompetitionAssessment:
        market = re.search(r'"market":\s*"([A-Z]{2})"', user_input)
        ids = re.findall(r'"evidence_id":\s*"(V-[^"]+)"', user_input)
        return CompetitionAssessment(
            market=market.group(1) if market else "??",
            supply_gap_level="3",
            supply_situation="dados_insuficientes" if not ids else "baixa_concorrencia",
            dominant_formats=[],
            recency_note=_TAG,
            underexplored_angles=[f"{_TAG} ângulo"],
            differentiation_options=[],
            rationale=f"{_TAG} {len(ids)} equivalentes considerados",
            evidence_ids=ids[:3],
        )
    if model is EditorialAssessment:
        market = re.search(r'"code":\s*"([A-Z]{2})"', user_input)
        return EditorialAssessment(
            market=market.group(1) if market else "??",
            criteria=[
                CriterionJudgement(criterion=c, level="3", rationale=_TAG, evidence_ids=[])
                for c in ("economic_potential", "cultural_portability", "production_feasibility")
            ],
            rights_risk="baixo",
            rights_notes=_TAG,
            factual_risk="medio",
            needs_specialist=False,
            estimated_research_complexity="media",
            topic_lifespan="perene",
        )
    if model is DossierDraft:
        ids = re.findall(r'"evidence_id":\s*"([^"]+)"', user_input)
        return DossierDraft(
            provisional_title=f"{_TAG} {label}",
            summary=f"{_TAG} Resumo sintético para teste do pipeline.",
            why_now=[Statement(text=f"{_TAG} motivo", kind="inferencia", evidence_ids=[])],
            demand_evidence=[Statement(text=f"{_TAG} demanda", kind="fato", evidence_ids=ids[:1])],
            competition_evidence=[],
            information_gaps=[f"{_TAG} todas as lacunas"],
            original_approaches=[],
            title_and_hook_ideas=[],
            potential_audience=[],
            resources_needed=_TAG,
            editorial_risks=[],
            legal_risks=[],
            cultural_risks=[],
            final_opinion=_TAG,
            final_recommendation="investigar_mais",
        )
    raise ValueError(f"sem resposta simulada para {model.__name__} ({task}): {json.dumps(user_input[:80])}")
