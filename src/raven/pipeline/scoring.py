"""Score experimental e confiança — regras formais.

DEFINIÇÕES
- Cada critério c tem peso w_c (config, soma 1) e um sub-score s_c ∈ [0, 1] ou AUSENTE.
  Sub-scores vêm de níveis ordinais 1–5: s = (nível − 1) / 4.
- Um critério só é PRESENTE se sua avaliação tiver `scoring_permitted=True` (todas as evidências
  de entrada permitem `scoring_input` — ver policy.inherited_uses) e nível numérico.
- Cobertura K = Σ w_c dos critérios presentes.
- Intervalo: mínimo = Σ w_c·s_c (ausentes = 0); máximo = mínimo + (1 − K) (ausentes = 1).
  O intervalo mostra o quanto a nota pode mudar com os dados que faltam. Nenhum valor ausente
  é substituído por número arbitrário.
- Nota pontual = Σ w_c·s_c / K, exibida SOMENTE se K ≥ min_coverage_for_point_score.
- Filtro eliminatório: risco de direitos "bloqueante" => status BLOQUEADA, sem nota.
- Escala exibida: 0–100.

CONFIANÇA (independente da nota; 0–1 e nível baixa/média/alta)
  confiança = 0,40·K + 0,25·diversidade + 0,20·atualidade + 0,15·consistência
  - diversidade: fontes reais distintas com evidência usada: 0→0; 1→0,4; 2→0,75; ≥3→1.
  - atualidade: fração das evidências observadas com idade ≤ evidence_max_age_days.
  - consistência: 1 − 0,25 × nº de divergências registradas (mínimo 0).
  - Qualquer dado simulado => confiança 0 e marcação "SIMULADO".
  Níveis: < 0,4 baixa; < 0,7 média; ≥ 0,7 alta.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field

from raven.domain import Evidence, EvidenceKind
from raven.settings import ScoringConfig

CRITERIA = (
    "destination_demand",
    "supply_gap",
    "origin_success",
    "economic_potential",
    "cultural_portability",
    "trend_timing",
    "production_feasibility",
)

CRITERIA_LABELS = {
    "destination_demand": "Demanda no destino",
    "supply_gap": "Lacuna de oferta",
    "origin_success": "Evidência de sucesso na origem",
    "economic_potential": "Potencial econômico",
    "cultural_portability": "Portabilidade cultural",
    "trend_timing": "Momento da tendência",
    "production_feasibility": "Viabilidade de produção",
}


class Method(StrEnum):
    RULE = "regra"
    LLM = "llm"
    HUMAN = "humano"


class CriterionAssessment(BaseModel):
    criterion: str
    level: int | None = None  # 1–5; None = sem avaliação
    method: Method
    rationale: str
    evidence_ids: list[str] = Field(default_factory=list)
    scoring_permitted: bool = False
    exclusion_reason: str | None = None  # por que não entra no score

    @property
    def subscore(self) -> float | None:
        if self.level is None or not self.scoring_permitted:
            return None
        if not 1 <= self.level <= 5:
            raise ValueError(f"nível fora da escala 1–5: {self.level}")
        return (self.level - 1) / 4


class ScoreStatus(StrEnum):
    SCORED = "pontuada"
    INSUFFICIENT = "cobertura_insuficiente"
    BLOCKED = "bloqueada"


class ScoreResult(BaseModel):
    status: ScoreStatus
    point: float | None  # 0–100
    minimum: float  # 0–100
    maximum: float  # 0–100
    coverage: float  # 0–1
    present: list[str]
    missing: list[str]
    blocked_reason: str | None = None


class Confidence(BaseModel):
    value: float
    level: str
    reasons: list[str]
    simulated: bool = False


def compute_score(
    assessments: list[CriterionAssessment],
    cfg: ScoringConfig,
    *,
    rights_blocking: bool = False,
    rights_reason: str | None = None,
) -> ScoreResult:
    by_crit: dict[str, CriterionAssessment] = {}
    for a in assessments:
        if a.criterion not in cfg.weights:
            raise ValueError(f"critério desconhecido: {a.criterion}")
        prev = by_crit.get(a.criterion)
        # Avaliação humana prevalece; entre as demais, a que tem subscore permitido.
        if prev is None or a.method is Method.HUMAN or (prev.subscore is None and a.subscore is not None):
            by_crit[a.criterion] = a

    present = [c for c, a in by_crit.items() if a.subscore is not None]
    missing = [c for c in cfg.weights if c not in present]
    coverage = sum(cfg.weights[c] for c in present)
    weighted = sum(cfg.weights[c] * (by_crit[c].subscore or 0.0) for c in present)
    minimum = weighted
    maximum = weighted + (1 - coverage)

    if rights_blocking:
        return ScoreResult(
            status=ScoreStatus.BLOCKED,
            point=None,
            minimum=0.0,
            maximum=0.0,
            coverage=round(coverage, 4),
            present=present,
            missing=missing,
            blocked_reason=rights_reason or "risco de direitos bloqueante",
        )
    point = None
    status = ScoreStatus.INSUFFICIENT
    if coverage > 0 and coverage + 1e-9 >= cfg.min_coverage_for_point_score:
        point = round(100 * weighted / coverage, 1)
        status = ScoreStatus.SCORED
    return ScoreResult(
        status=status,
        point=point,
        minimum=round(100 * minimum, 1),
        maximum=round(100 * maximum, 1),
        coverage=round(coverage, 4),
        present=present,
        missing=missing,
    )


def _diversity(n_sources: int) -> float:
    return {0: 0.0, 1: 0.4, 2: 0.75}.get(n_sources, 1.0)


def compute_confidence(
    score: ScoreResult,
    used_evidence: list[Evidence],
    cfg: ScoringConfig,
    *,
    divergences: list[str] | None = None,
    now: datetime | None = None,
    simulated: bool = False,
) -> Confidence:
    divergences = divergences or []
    reasons: list[str] = []
    if simulated or any(e.provenance.simulated for e in used_evidence):
        return Confidence(
            value=0.0,
            level="nula",
            reasons=["Há dados SIMULADOS entre as evidências: resultado sem valor probatório."],
            simulated=True,
        )
    now = now or datetime.now(UTC)
    real_sources = {
        e.provenance.source_id for e in used_evidence if e.provenance.source_id not in ("internal",)
    }
    observed = [e for e in used_evidence if e.kind is EvidenceKind.OBSERVED]
    max_age = timedelta(days=cfg.evidence_max_age_days)
    fresh = [e for e in observed if now - e.provenance.collected_at <= max_age]
    freshness = len(fresh) / len(observed) if observed else 0.0
    consistency = max(0.0, 1 - 0.25 * len(divergences))
    diversity = _diversity(len(real_sources))
    value = 0.40 * score.coverage + 0.25 * diversity + 0.20 * freshness + 0.15 * consistency

    reasons.append(f"Cobertura dos critérios pontuáveis: {score.coverage:.0%}.")
    if score.missing:
        reasons.append(
            "Critérios sem evidência permitida: " + ", ".join(CRITERIA_LABELS[c] for c in score.missing) + "."
        )
    reasons.append(
        f"Fontes externas distintas: {len(real_sources)} ({', '.join(sorted(real_sources)) or 'nenhuma'})."
    )
    if observed:
        reasons.append(f"Evidências observadas dentro do prazo de atualidade: {len(fresh)}/{len(observed)}.")
    else:
        reasons.append("Nenhuma evidência observada (apenas estimativas/avaliações).")
    reasons.extend(f"Divergência: {d}" for d in divergences)

    level = "baixa" if value < 0.4 else "média" if value < 0.7 else "alta"
    return Confidence(value=round(value, 3), level=level, reasons=reasons)


def level_from_llm(level: str) -> int | None:
    return int(level) if level in {"1", "2", "3", "4", "5"} else None
