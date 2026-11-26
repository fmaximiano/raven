"""Modelos de domínio compartilhados por conectores, pipeline e benchmark.

Princípios:
- Todo dado externo carrega procedência (fonte, data de coleta, classe de dado, simulado ou não).
- Evidência distingue observado, estimado e hipótese.
- Nada aqui chama rede ou LLM.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


def stable_id(*parts: str) -> str:
    """Identificador determinístico curto (para deduplicação e referências em dossiês)."""
    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return digest[:12]


class Use(StrEnum):
    """Usos de um dado externo que dependem de permissão da fonte."""

    DISPLAY = "display"  # exibir ao operador
    LLM_INPUT = "llm_input"  # enviar a um provedor de LLM
    SCORING_INPUT = "scoring_input"  # compor score/priorização
    DERIVED_METRIC = "derived_metric"  # calcular métricas novas (razões, médias, outlier score)
    AGGREGATE = "aggregate"  # agregar entre canais/mercados


class Permission(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    PENDING = "pending"  # pendente de confirmação: tratado como negado


class Provenance(BaseModel):
    """Origem rastreável de um dado externo."""

    model_config = ConfigDict(frozen=True)

    source_id: str  # ex.: "youtube_data", "dataforseo", "simulated"
    data_class: str  # ex.: "metadata", "statistics", "trends"
    collected_at: datetime = Field(default_factory=utcnow)
    endpoint: str | None = None
    request_ref: str | None = None  # id da requisição/execução no fornecedor
    simulated: bool = False  # True => NÃO é evidência real


class VideoRef(BaseModel):
    """Vídeo retornado por uma fonte. Estatísticas são opcionais e sujeitas à política da fonte."""

    platform_id: str  # id do vídeo no YouTube
    title: str
    channel_id: str | None = None
    channel_title: str | None = None
    description: str | None = None
    published_at: datetime | None = None
    language_hint: str | None = None  # idioma declarado pela fonte, se houver
    region_hint: str | None = None
    url: str | None = None
    statistics: dict[str, float | int | None] = Field(default_factory=dict)
    vendor_metrics: dict[str, Any] = Field(default_factory=dict)  # ex.: outlierMultiplier
    provenance: Provenance  # data_class aplica-se aos metadados (título, canal, data)
    # Classe de dados de `statistics`/`vendor_metrics` (políticas podem diferir dos metadados).
    metrics_data_class: str | None = None

    def metrics_provenance(self) -> Provenance:
        return self.provenance.model_copy(
            update={"data_class": self.metrics_data_class or self.provenance.data_class}
        )

    @property
    def youtube_url(self) -> str:
        return self.url or f"https://www.youtube.com/watch?v={self.platform_id}"


class TrendPoint(BaseModel):
    date: str  # ISO (data inicial do período)
    value: float | None  # índice relativo 0–100 (Google Trends) ou None se ausente


class TrendSeries(BaseModel):
    keyword: str
    location: str | None
    language: str | None
    points: list[TrendPoint]
    provenance: Provenance


class KeywordVolume(BaseModel):
    keyword: str
    location: str | None
    language: str | None
    monthly_volume: int | None
    competition: float | str | None = None
    provenance: Provenance


class EvidenceKind(StrEnum):
    OBSERVED = "observado"  # dado coletado de fonte
    ESTIMATED = "estimado"  # derivado/estimado a partir de dados (cálculo ou modelo)
    HYPOTHESIS = "hipotese"  # não verificado


class Evidence(BaseModel):
    """Unidade citável num dossiê. LLMs só podem citar evidências por `id`."""

    id: str
    kind: EvidenceKind
    claim: str  # o que a evidência sustenta, em linguagem simples
    market: str | None = None  # ex.: "BR"
    language: str | None = None  # ex.: "pt"
    url: str | None = None  # sempre vinda do conector, nunca do LLM
    data: dict[str, Any] = Field(default_factory=dict)
    provenance: Provenance
    permitted_uses: list[Use] = Field(default_factory=list)  # resolvido pela política da fonte

    def allows(self, use: Use) -> bool:
        return use in self.permitted_uses and not self.provenance.simulated


class Market(BaseModel):
    code: str  # ISO 3166-1 alpha-2
    name: str
    languages: list[str]
    dataforseo_location_code: int | None = None


class Subject(BaseModel):
    """Assunto investigado (conceito, não a expressão de um criador)."""

    id: str
    label: str  # rótulo de trabalho em PT-BR
    niche: str
    origin_language: str | None = None
    description: str | None = None
    native_queries: dict[str, list[str]] = Field(default_factory=dict)  # idioma -> consultas
    seed_video_ids: list[str] = Field(default_factory=list)
