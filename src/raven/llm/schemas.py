"""Schemas versionados das saídas estruturadas do LLM.

Compatíveis com JSON Schema estrito da Responses API: sem valores padrão opcionais implícitos,
sem restrições numéricas (validação semântica é feita no código). Níveis ordinais usam enums.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel

Level = Literal["1", "2", "3", "4", "5", "insuficiente"]


class SubjectInterpretation(BaseModel):
    SCHEMA_VERSION: ClassVar[str] = "1"

    core_subject: str  # conceito investigado, independente do criador
    canonical_description_en: str  # descrição neutra (base para embeddings)
    entities: list[str]
    concepts: list[str]
    niche: str
    subniche: str
    approach_type: str  # ex.: explicativo, narrativo, lista, documentário
    narrative_hook: str
    culture_specific_elements: list[str]
    facts_to_verify: list[str]
    creator_specific_expression: list[str]  # elementos do criador original a NÃO replicar


class LanguageQueries(BaseModel):
    language: str
    queries: list[str]  # consultas nativas (não traduções literais)
    alternative_names: list[str]  # nomes locais, transliterações, sinônimos
    notes: str


class QueryPlan(BaseModel):
    SCHEMA_VERSION: ClassVar[str] = "1"

    per_language: list[LanguageQueries]


MatchLabel = Literal["mesmo_assunto", "assunto_relacionado", "mesmo_assunto_abordagem_distinta", "incorreto"]


class MatchJudgement(BaseModel):
    candidate_id: str
    label: MatchLabel
    rationale: str


class MatchValidation(BaseModel):
    SCHEMA_VERSION: ClassVar[str] = "1"

    judgements: list[MatchJudgement]


class CompetitionAssessment(BaseModel):
    SCHEMA_VERSION: ClassVar[str] = "1"

    market: str
    supply_gap_level: Level  # 5 = grande lacuna de oferta; "insuficiente" = dados não permitem dizer
    supply_situation: Literal[
        "baixa_concorrencia", "oferta_de_baixa_qualidade", "concorrencia_consolidada", "dados_insuficientes"
    ]
    dominant_formats: list[str]
    recency_note: str
    underexplored_angles: list[str]
    differentiation_options: list[str]
    rationale: str
    evidence_ids: list[str]


class CriterionJudgement(BaseModel):
    criterion: Literal[
        "origin_success",
        "economic_potential",
        "cultural_portability",
        "production_feasibility",
        "trend_timing",
    ]
    level: Level
    rationale: str
    evidence_ids: list[str]


class EditorialAssessment(BaseModel):
    SCHEMA_VERSION: ClassVar[str] = "1"

    market: str
    criteria: list[CriterionJudgement]
    rights_risk: Literal["baixo", "medio", "alto", "bloqueante"]
    rights_notes: str
    factual_risk: Literal["baixo", "medio", "alto"]
    needs_specialist: bool
    estimated_research_complexity: Literal["baixa", "media", "alta"]
    topic_lifespan: Literal["perene", "sazonal", "noticioso", "incerto"]


class Statement(BaseModel):
    text: str
    kind: Literal["fato", "inferencia", "recomendacao"]
    evidence_ids: list[str]  # obrigatório para "fato"


class DossierDraft(BaseModel):
    SCHEMA_VERSION: ClassVar[str] = "1"

    provisional_title: str
    summary: str
    why_now: list[Statement]
    demand_evidence: list[Statement]
    competition_evidence: list[Statement]
    information_gaps: list[str]
    original_approaches: list[str]
    title_and_hook_ideas: list[str]
    potential_audience: list[Statement]
    resources_needed: str
    editorial_risks: list[str]
    legal_risks: list[str]
    cultural_risks: list[str]
    final_opinion: str
    final_recommendation: Literal["investigar_mais", "aprovar_para_pauta", "arquivar"]
