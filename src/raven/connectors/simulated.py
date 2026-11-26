"""Conector SIMULADO para testes do pipeline sem credenciais.

Produz dados sintéticos determinísticos. Todo item leva `provenance.simulated=True` e ids com
prefixo `SIM-`. Nunca deve aparecer em relatórios de viabilidade como evidência.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from raven.connectors.base import (
    CallCost,
    Capability,
    Connector,
    ConnectorInfo,
    ConnectorStatus,
    HealthReport,
    TrendsRequest,
    VideoSearchRequest,
    VideoSearchResult,
)
from raven.domain import Provenance, TrendPoint, TrendSeries, VideoRef


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


class SimulatedConnector(Connector):
    def __init__(self, source_id: str = "simulated", *, coverage: float = 0.7) -> None:
        self.coverage = coverage
        self.info = ConnectorInfo(
            source_id=source_id,
            name=f"Simulado ({source_id})",
            connector_version="0.1.0",
            api_version="n/a",
            status=ConnectorStatus.SIMULATED,
            capabilities=[Capability.VIDEO_SEARCH, Capability.OUTLIER_SEARCH, Capability.SEARCH_TRENDS],
            supports_language_filter=True,
            supports_region_filter=True,
            data_classes=["any"],
            cost_unit="usd",
            limitations=["DADOS SINTÉTICOS — não constituem evidência"],
        )

    def _prov(self, data_class: str = "any") -> Provenance:
        return Provenance(source_id=self.source_id, data_class=data_class, simulated=True)

    async def check(self, live: bool = False) -> HealthReport:
        return HealthReport(source_id=self.source_id, configured=True, authenticated=True, detail="simulado")

    async def search_videos(self, req: VideoSearchRequest) -> VideoSearchResult:
        seed = _h(self.source_id, req.query, req.language or "", req.region or "")
        n = int(req.max_results * self.coverage * ((seed % 100) / 100 + 0.3))
        n = max(0, min(n, req.max_results))
        base = datetime(2026, 1, 1, tzinfo=UTC)
        videos = [
            VideoRef(
                platform_id=f"SIM-{_h(req.query, str(i)) % 10**8:08d}",
                title=f"[SIMULADO] {req.query} — variação {i + 1}",
                channel_id=f"SIMCH{(seed + i) % 37}",
                channel_title=f"Canal simulado {(seed + i) % 37}",
                published_at=base + timedelta(days=(seed + i * 13) % 300),
                language_hint=req.language,
                statistics={"view_count": (seed * (i + 1)) % 2_000_000},
                provenance=self._prov(),
            )
            for i in range(n)
        ]
        return VideoSearchResult(
            request=req,
            videos=videos,
            cost=CallCost(units=0, unit="usd", usd=0.0),
            latency_ms=1.0,
            applied_filters={"language": req.language, "region": req.region},
        )

    async def search_outliers(self, req: VideoSearchRequest) -> VideoSearchResult:
        return await self.search_videos(req)

    async def search_trends(self, req: TrendsRequest) -> list[TrendSeries]:
        out = []
        for kw in req.keywords:
            seed = _h(kw, str(req.location_code), req.language or "")
            pts = [
                TrendPoint(date=f"2026-{m:02d}-01", value=float((seed >> (m % 16)) % 100))
                for m in range(1, 13)
            ]
            out.append(
                TrendSeries(
                    keyword=kw,
                    location=str(req.location_code),
                    language=req.language,
                    points=pts,
                    provenance=self._prov("trends"),
                )
            )
        return out
