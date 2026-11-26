"""Correspondência multilíngue por embeddings + validação de casos ambíguos por LLM."""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum


class MatchBand(StrEnum):
    STRONG = "forte"  # >= same_subject_threshold
    AMBIGUOUS = "ambiguo"  # [lower, same): validar com LLM
    WEAK = "fraco"  # < lower: descartado como não relacionado
    NOT_EVALUATED = "nao_avaliado"  # política da fonte não permite enviar ao provedor de embeddings


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def band(similarity: float, *, same: float, lower: float) -> MatchBand:
    if similarity >= same:
        return MatchBand.STRONG
    if similarity >= lower:
        return MatchBand.AMBIGUOUS
    return MatchBand.WEAK


def video_text(title: str, description: str | None, max_desc: int = 300) -> str:
    desc = (description or "").strip().replace("\n", " ")
    return f"{title}\n{desc[:max_desc]}" if desc else title
