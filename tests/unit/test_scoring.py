from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from raven.domain import Evidence, EvidenceKind, Provenance, Use
from raven.pipeline.scoring import (
    CriterionAssessment,
    Method,
    ScoreStatus,
    compute_confidence,
    compute_score,
)
from raven.settings import ScoringConfig


def a(
    criterion: str, level: int | None, permitted: bool = True, method: Method = Method.LLM
) -> CriterionAssessment:
    return CriterionAssessment(
        criterion=criterion, level=level, method=method, rationale="x", scoring_permitted=permitted
    )


def test_weights_must_sum_to_one(cfg):
    with pytest.raises(ValidationError):
        ScoringConfig(weights={"a": 0.5, "b": 0.4})


def test_full_coverage_point_score(cfg):
    res = compute_score([a(c, 5) for c in cfg.scoring.weights], cfg.scoring)
    assert res.status is ScoreStatus.SCORED
    assert res.point == 100.0
    assert res.minimum == res.maximum == 100.0
    assert res.coverage == 1.0


def test_missing_values_are_not_imputed(cfg):
    # Só critérios editoriais (0,30 de peso) presentes, todos no nível 5.
    res = compute_score(
        [a("economic_potential", 5), a("cultural_portability", 5), a("production_feasibility", 5)],
        cfg.scoring,
    )
    assert res.status is ScoreStatus.INSUFFICIENT
    assert res.point is None
    assert res.coverage == pytest.approx(0.30)
    assert res.minimum == pytest.approx(30.0)  # ausentes = 0
    assert res.maximum == pytest.approx(100.0)  # ausentes = 1
    assert set(res.missing) == {"destination_demand", "supply_gap", "origin_success", "trend_timing"}


def test_non_permitted_assessment_does_not_count(cfg):
    res = compute_score([a("destination_demand", 5, permitted=False)], cfg.scoring)
    assert "destination_demand" in res.missing
    assert res.coverage == 0


def test_threshold_boundary(cfg):
    # demanda .25 + lacuna .25 + econ .15 = .65 >= .60
    res = compute_score(
        [a("destination_demand", 3), a("supply_gap", 3), a("economic_potential", 3)], cfg.scoring
    )
    assert res.status is ScoreStatus.SCORED
    assert res.point == 50.0


def test_rights_blocking_is_eliminatory(cfg):
    res = compute_score(
        [a(c, 5) for c in cfg.scoring.weights],
        cfg.scoring,
        rights_blocking=True,
        rights_reason="uso de imagens sem licença",
    )
    assert res.status is ScoreStatus.BLOCKED
    assert res.point is None and res.maximum == 0
    assert "licença" in (res.blocked_reason or "")


def test_human_assessment_prevails(cfg):
    res = compute_score(
        [a("supply_gap", 1), a("supply_gap", 5, method=Method.HUMAN)]
        + [a(c, 5) for c in ("destination_demand", "economic_potential")],
        cfg.scoring,
    )
    assert res.point == 100.0


def test_level_out_of_range_raises(cfg):
    with pytest.raises(ValueError):
        compute_score([a("supply_gap", 7)], cfg.scoring)


def _ev(
    source: str, *, simulated: bool = False, age_days: int = 0, kind: EvidenceKind = EvidenceKind.OBSERVED
) -> Evidence:
    return Evidence(
        id=f"E-{source}-{age_days}",
        kind=kind,
        claim="c",
        provenance=Provenance(
            source_id=source,
            data_class="x",
            simulated=simulated,
            collected_at=datetime.now(UTC) - timedelta(days=age_days),
        ),
        permitted_uses=[Use.SCORING_INPUT],
    )


def test_confidence_simulated_is_null(cfg):
    res = compute_score([a(c, 5) for c in cfg.scoring.weights], cfg.scoring)
    conf = compute_confidence(res, [_ev("youtube_data", simulated=True)], cfg.scoring)
    assert conf.level == "nula" and conf.value == 0 and conf.simulated


def test_confidence_grows_with_diversity_and_freshness(cfg):
    res = compute_score([a(c, 5) for c in cfg.scoring.weights], cfg.scoring)
    one_old = compute_confidence(res, [_ev("dataforseo", age_days=90)], cfg.scoring)
    many_fresh = compute_confidence(
        res, [_ev("dataforseo"), _ev("youtube_data"), _ev("outlierkit")], cfg.scoring
    )
    assert many_fresh.value > one_old.value
    assert many_fresh.level == "alta"


def test_confidence_penalizes_divergence(cfg):
    res = compute_score([a(c, 5) for c in cfg.scoring.weights], cfg.scoring)
    base = compute_confidence(res, [_ev("dataforseo")], cfg.scoring)
    div = compute_confidence(
        res, [_ev("dataforseo")], cfg.scoring, divergences=["fontes discordam", "datas conflitantes"]
    )
    assert div.value < base.value
