"""Registro de evidências de uma execução, com aplicação de política na entrada e na saída."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any

from raven.domain import Evidence, EvidenceKind, Provenance, Use, VideoRef, stable_id
from raven.policy import PolicyRegistry, inherited_uses


class EvidenceStore:
    def __init__(self, policies: PolicyRegistry, *, allow_simulated: bool = False) -> None:
        self.policies = policies
        self.allow_simulated = allow_simulated
        self._items: dict[str, Evidence] = {}

    def __contains__(self, eid: str) -> bool:
        return eid in self._items

    def __iter__(self) -> Iterator[Evidence]:
        return iter(self._items.values())

    def get(self, eid: str) -> Evidence:
        return self._items[eid]

    def all(self) -> list[Evidence]:
        return list(self._items.values())

    def add(self, ev: Evidence, *, derived_from: list[Evidence] | None = None) -> Evidence:
        """Registra evidência resolvendo usos permitidos; derivadas herdam restrições das entradas."""
        stamped = self.policies.stamp(ev)
        if derived_from is not None:
            uses = inherited_uses(stamped.permitted_uses, derived_from)
            simulated = stamped.provenance.simulated or any(e.provenance.simulated for e in derived_from)
            stamped = stamped.model_copy(
                update={
                    "permitted_uses": uses,
                    "provenance": stamped.provenance.model_copy(update={"simulated": simulated}),
                    "data": {**stamped.data, "derived_from": [e.id for e in derived_from]},
                }
            )
        self._items[stamped.id] = stamped
        return stamped

    def allows(self, ev: Evidence, use: Use) -> bool:
        if ev.provenance.simulated:
            return self.allow_simulated and use in ev.permitted_uses
        return use in ev.permitted_uses

    def llm_view(self, evidence: Iterable[Evidence]) -> list[dict[str, Any]]:
        """Apenas evidências cujo uso `llm_input` é permitido, sem URLs nem campos extras."""
        return [
            {
                "evidence_id": e.id,
                "kind": e.kind.value,
                "claim": e.claim,
                "market": e.market,
                "data": _public_data(e),
            }
            for e in evidence
            if self.allows(e, Use.LLM_INPUT)
        ]

    # ---- fábricas ------------------------------------------------------------------------

    def add_video(
        self, video: VideoRef, *, market: str | None, language: str | None, extra: dict[str, Any]
    ) -> Evidence:
        """Cria evidência de metadados e, separada, de métricas (políticas distintas)."""
        meta = self.add(
            Evidence(
                id=f"V-{stable_id(video.provenance.source_id, video.platform_id)}",
                kind=EvidenceKind.OBSERVED,
                claim=(
                    f'Vídeo encontrado ({video.provenance.source_id}): "{video.title}" — '
                    f"{video.channel_title or 'canal não informado'}"
                    + (f", publicado em {video.published_at:%Y-%m-%d}" if video.published_at else "")
                ),
                market=market,
                language=language,
                url=video.youtube_url if not video.platform_id.startswith("SIM-") else None,
                data={
                    "video_id": video.platform_id,
                    "title": video.title,
                    "channel": video.channel_title,
                    "published_at": video.published_at.isoformat() if video.published_at else None,
                    "language_hint": video.language_hint,
                    **extra,
                },
                provenance=video.provenance,
            )
        )
        metrics = {
            k: v for k, v in {**video.statistics, **video.vendor_metrics}.items() if v not in (None, {}, "")
        }
        if metrics:
            self.add(
                Evidence(
                    id=f"M-{stable_id(video.provenance.source_id, video.platform_id)}",
                    kind=EvidenceKind.OBSERVED,
                    claim=f'Métricas informadas pela fonte para "{video.title}"',
                    market=market,
                    language=language,
                    url=meta.url,
                    data={"video_id": video.platform_id, "metrics": metrics},
                    provenance=video.metrics_provenance(),
                )
            )
        return meta

    def add_assessment(
        self,
        *,
        prefix: str,
        key: str,
        claim: str,
        market: str | None,
        data: dict[str, Any],
        inputs: list[Evidence],
        data_class: str = "llm_assessment",
        kind: EvidenceKind = EvidenceKind.ESTIMATED,
    ) -> Evidence:
        return self.add(
            Evidence(
                id=f"{prefix}-{stable_id(prefix, key)}",
                kind=kind,
                claim=claim,
                market=market,
                data=data,
                provenance=Provenance(source_id="internal", data_class=data_class),
            ),
            derived_from=inputs,
        )


def _public_data(e: Evidence) -> dict[str, Any]:
    return {k: v for k, v in e.data.items() if k not in ("derived_from",)}
