"""Contrato comum dos conectores de fontes externas.

Cada conector declara identidade, versão, estado de verificação, capacidades, idiomas/regiões,
classes de dados e custo. Capacidades não suportadas levantam `CapabilityNotSupported`: o
pipeline registra a lacuna em vez de falhar a investigação inteira.
"""

from __future__ import annotations

import abc
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel, Field

from raven.domain import KeywordVolume, TrendSeries, VideoRef


class ConnectorStatus(StrEnum):
    REAL = "real"  # implementação contra API documentada; requer credencial para executar
    SIMULATED = "simulado"  # dados sintéticos: NÃO produz evidência
    INTERFACE_ONLY = "somente_interface"  # contrato pronto; integração dependente de validação


class Capability(StrEnum):
    VIDEO_SEARCH = "video_search"
    VIDEO_DETAILS = "video_details"
    CHANNEL_DETAILS = "channel_details"
    CHANNEL_UPLOADS = "channel_uploads"
    OUTLIER_SEARCH = "outlier_search"
    CHANNEL_DISCOVERY = "channel_discovery"
    SEARCH_TRENDS = "search_trends"
    KEYWORD_VOLUME = "keyword_volume"


class ConnectorInfo(BaseModel):
    source_id: str
    name: str
    connector_version: str
    api_version: str
    status: ConnectorStatus
    capabilities: list[Capability]
    supports_language_filter: bool
    supports_region_filter: bool
    data_classes: list[str]
    cost_unit: str  # "quota_units", "credits", "usd"
    docs: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class CallCost(BaseModel):
    units: float = 0.0
    unit: str = "usd"
    usd: float | None = None  # custo monetário quando conhecido/estimável


class VideoSearchRequest(BaseModel):
    query: str
    language: str | None = None  # ISO 639-1
    region: str | None = None  # ISO 3166-1 alpha-2
    max_results: int = 20
    published_after: datetime | None = None
    order: str = "relevance"


class VideoSearchResult(BaseModel):
    request: VideoSearchRequest
    videos: list[VideoRef]
    cost: CallCost
    latency_ms: float
    applied_filters: dict[str, Any] = Field(default_factory=dict)
    ignored_filters: list[str] = Field(default_factory=list)  # filtros pedidos e não suportados
    request_ref: str | None = None


class TrendsRequest(BaseModel):
    keywords: list[str] = Field(min_length=1, max_length=5)
    location_code: int | None = None
    language: str | None = None
    search_type: str = "youtube"  # web | youtube | news ...
    time_range: str = "past_12_months"


class HealthReport(BaseModel):
    source_id: str
    configured: bool  # credenciais presentes
    authenticated: bool | None = None  # None = não testado
    detail: str = ""
    observed: dict[str, Any] = Field(default_factory=dict)


# ---- erros --------------------------------------------------------------------------------


class ConnectorError(Exception):
    retryable: ClassVar[bool] = False

    def __init__(self, source_id: str, message: str) -> None:
        super().__init__(f"[{source_id}] {message}")
        self.source_id = source_id


class NotConfigured(ConnectorError):
    pass


class AuthError(ConnectorError):
    pass


class CapabilityNotSupported(ConnectorError):
    pass


class QuotaExceeded(ConnectorError):
    """Cota do fornecedor ou limite local esgotado. Não repetir automaticamente."""


class RateLimited(ConnectorError):
    retryable = True


class UpstreamError(ConnectorError):
    retryable = True


class BadResponse(ConnectorError):
    """Resposta fora do contrato esperado (falha de contrato)."""


# ---- contrato -----------------------------------------------------------------------------


class Connector(abc.ABC):
    info: ConnectorInfo

    @property
    def source_id(self) -> str:
        return self.info.source_id

    def supports(self, cap: Capability) -> bool:
        return cap in self.info.capabilities

    def _unsupported(self, cap: Capability) -> CapabilityNotSupported:
        return CapabilityNotSupported(self.source_id, f"capacidade não suportada: {cap}")

    @abc.abstractmethod
    async def check(self, live: bool = False) -> HealthReport:
        """Verifica credenciais; com `live=True`, faz a chamada mais barata possível."""

    async def search_videos(self, req: VideoSearchRequest) -> VideoSearchResult:
        raise self._unsupported(Capability.VIDEO_SEARCH)

    async def video_details(self, video_ids: list[str]) -> list[VideoRef]:
        raise self._unsupported(Capability.VIDEO_DETAILS)

    async def search_outliers(self, req: VideoSearchRequest) -> VideoSearchResult:
        raise self._unsupported(Capability.OUTLIER_SEARCH)

    async def search_trends(self, req: TrendsRequest) -> list[TrendSeries]:
        raise self._unsupported(Capability.SEARCH_TRENDS)

    async def keyword_volume(
        self, keywords: list[str], location_code: int | None, language: str | None
    ) -> list[KeywordVolume]:
        raise self._unsupported(Capability.KEYWORD_VOLUME)

    async def aclose(self) -> None:  # noqa: B027 - opcional
        pass
