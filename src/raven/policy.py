"""Políticas de uso por fonte e classe de dado.

Regras:
- `pending` equivale a negado ("aprovações pendentes não autorizam uso restrito").
- Dado simulado nunca é evidência real: o pipeline o aceita apenas em modo de teste.
- Avaliações derivadas (LLM/cálculo) herdam a restrição mais severa das evidências de entrada:
  uma avaliação só compõe score se TODAS as entradas permitirem `scoring_input`. Isso impede
  usar dados restritos como entrada indireta do score.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, Field

from raven.domain import Evidence, Permission, Provenance, Use
from raven.settings import get_secrets, load_yaml


class SourcePolicy(BaseModel):
    source_id: str
    status: str = "pending"
    docs: list[str] = Field(default_factory=list)
    notes: str = ""
    retention_days: int = 30
    evidence_of_permission: str | None = None
    derived_metrics_amendment_accepted: bool = False
    data_classes: dict[str, dict[Use, Permission]] = Field(default_factory=dict)

    def permission(self, data_class: str, use: Use) -> Permission:
        rules = self.data_classes.get(data_class) or self.data_classes.get("any")
        if rules is None:
            return Permission.DENY
        return rules.get(use, Permission.DENY)

    def permitted_uses(self, data_class: str) -> list[Use]:
        return [u for u in Use if self.permission(data_class, u) is Permission.ALLOW]


class PolicyRegistry:
    def __init__(self, policies: dict[str, SourcePolicy]) -> None:
        self._policies = policies

    @classmethod
    def load(cls, path: Path | None = None) -> PolicyRegistry:
        p = path or get_secrets().config_dir / "source_policies.yaml"
        raw = load_yaml(p)
        policies = {
            sid: SourcePolicy.model_validate({"source_id": sid, **body})  # type: ignore[dict-item]
            for sid, body in raw.items()
        }
        return cls(policies)

    def get(self, source_id: str) -> SourcePolicy:
        # Fonte desconhecida: nenhum uso é permitido.
        return self._policies.get(source_id) or SourcePolicy(source_id=source_id)

    def all(self) -> list[SourcePolicy]:
        return list(self._policies.values())

    def uses_for(self, prov: Provenance) -> list[Use]:
        if prov.simulated:  # dados sintéticos seguem a política "simulated", nunca a da fonte real
            return self.get("simulated").permitted_uses("any")
        return self.get(prov.source_id).permitted_uses(prov.data_class)

    def allows(self, prov: Provenance, use: Use) -> bool:
        return use in self.uses_for(prov)

    def stamp(self, evidence: Evidence) -> Evidence:
        """Resolve e grava os usos permitidos na própria evidência."""
        return evidence.model_copy(update={"permitted_uses": self.uses_for(evidence.provenance)})


def inherited_uses(own: Iterable[Use], inputs: Iterable[Evidence]) -> list[Use]:
    """Usos de uma evidência derivada = interseção dos usos próprios com os de cada entrada."""
    allowed = set(own)
    for ev in inputs:
        allowed &= set(ev.permitted_uses)
    return [u for u in Use if u in allowed]
