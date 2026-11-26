"""Regras determinísticas sobre séries de interesse de busca (Google Trends via DataForSEO).

Limitação central: índices do Trends são relativos ao pico da própria requisição (0–100). Não
medem volume absoluto e não são comparáveis entre requisições distintas. Por isso as regras usam:
- presença: fração de períodos com interesse > 0 (o Google devolve 0 quando o volume é
  insuficiente) — proxy de demanda mínima sustentada;
- momento: média dos 3 períodos mais recentes ÷ média dos períodos anteriores.
Limiares são EXPERIMENTAIS e devem ser calibrados com resultados próprios.
"""

from __future__ import annotations

from pydantic import BaseModel

from raven.domain import TrendSeries

PRESENCE_THRESHOLDS = [(0.2, 1), (0.4, 2), (0.6, 3), (0.85, 4)]  # (< limiar, nível); senão 5
MOMENTUM_THRESHOLDS = [(0.5, 1), (0.85, 2), (1.15, 3), (1.5, 4)]
MIN_POINTS = 6


class TrendSummary(BaseModel):
    keyword: str
    points: int
    valid_points: int
    presence: float | None
    recent_mean: float | None
    previous_mean: float | None
    momentum: float | None


def summarize(series: TrendSeries) -> TrendSummary:
    values = [p.value for p in series.points]
    valid = [v for v in values if v is not None]
    if len(valid) < MIN_POINTS:
        return TrendSummary(
            keyword=series.keyword,
            points=len(values),
            valid_points=len(valid),
            presence=None,
            recent_mean=None,
            previous_mean=None,
            momentum=None,
        )
    presence = sum(1 for v in valid if v > 0) / len(valid)
    recent = valid[-3:]
    previous = valid[:-3]
    recent_mean = sum(recent) / len(recent)
    previous_mean = sum(previous) / len(previous)
    momentum = (recent_mean / previous_mean) if previous_mean > 0 else None
    return TrendSummary(
        keyword=series.keyword,
        points=len(values),
        valid_points=len(valid),
        presence=round(presence, 3),
        recent_mean=round(recent_mean, 2),
        previous_mean=round(previous_mean, 2),
        momentum=round(momentum, 3) if momentum is not None else None,
    )


def _bucket(value: float, thresholds: list[tuple[float, int]]) -> int:
    for limit, level in thresholds:
        if value < limit:
            return level
    return 5


def demand_level(s: TrendSummary) -> int | None:
    return None if s.presence is None else _bucket(s.presence, PRESENCE_THRESHOLDS)


def timing_level(s: TrendSummary) -> int | None:
    if s.momentum is None:
        return None
    return _bucket(s.momentum, MOMENTUM_THRESHOLDS)


def best_summary(summaries: list[TrendSummary]) -> TrendSummary | None:
    """Consulta com maior presença (as consultas nativas são alternativas para o mesmo assunto)."""
    usable = [s for s in summaries if s.presence is not None]
    return max(usable, key=lambda s: (s.presence or 0, s.recent_mean or 0)) if usable else None
