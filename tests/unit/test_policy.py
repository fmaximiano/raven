from raven.domain import Evidence, EvidenceKind, Permission, Provenance, Use
from raven.pipeline.evidence import EvidenceStore
from raven.policy import inherited_uses


def prov(source: str, data_class: str, simulated: bool = False) -> Provenance:
    return Provenance(source_id=source, data_class=data_class, simulated=simulated)


def test_youtube_defaults_follow_documented_policy(policies):
    yt = policies.get("youtube_data")
    assert yt.retention_days == 30
    assert policies.allows(prov("youtube_data", "metadata"), Use.LLM_INPUT)
    assert not policies.allows(prov("youtube_data", "metadata"), Use.SCORING_INPUT)
    assert not policies.allows(prov("youtube_data", "statistics"), Use.LLM_INPUT)
    assert not policies.allows(prov("youtube_data", "statistics"), Use.DERIVED_METRIC)
    assert not policies.allows(prov("youtube_data", "statistics"), Use.AGGREGATE)


def test_pending_is_treated_as_denied(policies):
    d4 = policies.get("dataforseo")
    assert d4.permission("trends", Use.SCORING_INPUT) is Permission.PENDING
    assert not policies.allows(prov("dataforseo", "trends"), Use.SCORING_INPUT)
    assert policies.allows(prov("dataforseo", "trends"), Use.DISPLAY)


def test_unknown_source_allows_nothing(policies):
    assert policies.uses_for(prov("fonte_desconhecida", "x")) == []


def test_simulated_uses_simulated_policy(policies):
    assert Use.SCORING_INPUT in policies.uses_for(prov("youtube_data", "statistics", simulated=True))


def test_inherited_uses_is_intersection():
    ev_yt = Evidence(
        id="V-1",
        kind=EvidenceKind.OBSERVED,
        claim="c",
        provenance=prov("youtube_data", "metadata"),
        permitted_uses=[Use.DISPLAY, Use.LLM_INPUT],
    )
    assert inherited_uses(list(Use), [ev_yt]) == [Use.DISPLAY, Use.LLM_INPUT]


def test_llm_assessment_cannot_launder_restricted_inputs(policies):
    """Avaliação do LLM sobre metadados do YouTube não pode compor score."""
    store = EvidenceStore(policies)
    v = store.add(
        Evidence(id="V-1", kind=EvidenceKind.OBSERVED, claim="c", provenance=prov("youtube_data", "metadata"))
    )
    internal = store.add(
        Evidence(
            id="S-1", kind=EvidenceKind.HYPOTHESIS, claim="s", provenance=prov("internal", "operator_input")
        )
    )
    derived = store.add_assessment(
        prefix="A", key="k", claim="gap", market="BR", data={}, inputs=[v, internal]
    )
    assert not store.allows(derived, Use.SCORING_INPUT)
    assert store.allows(derived, Use.LLM_INPUT)
    clean = store.add_assessment(
        prefix="A", key="k2", claim="portab", market="BR", data={}, inputs=[internal]
    )
    assert store.allows(clean, Use.SCORING_INPUT)


def test_llm_view_excludes_disallowed_evidence(policies):
    store = EvidenceStore(policies)
    meta = store.add(
        Evidence(
            id="V-1",
            kind=EvidenceKind.OBSERVED,
            claim="título",
            url="https://x",
            provenance=prov("youtube_data", "metadata"),
        )
    )
    stats = store.add(
        Evidence(
            id="M-1", kind=EvidenceKind.OBSERVED, claim="views", provenance=prov("youtube_data", "statistics")
        )
    )
    vendor = store.add(
        Evidence(
            id="V-2", kind=EvidenceKind.OBSERVED, claim="outlier", provenance=prov("outlierkit", "outliers")
        )
    )
    view = store.llm_view([meta, stats, vendor])
    assert [v["evidence_id"] for v in view] == ["V-1"]
    assert "url" not in view[0]
