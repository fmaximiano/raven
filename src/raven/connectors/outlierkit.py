"""OutlierKit REST API v1 (https://outlierkit.com/api-docs).

Autenticação: `Authorization: Bearer <api_key>` (chave criada em /app/api).
Custos documentados (créditos): outliers/search 2 (cache), outliers/refresh 10 (ao vivo, plano
Pro+), channels/search 5, videos/:id 2. Envelope: {success, data, credits:{charged, remaining}}.

Limitações relevantes para o RAVEN: nenhum endpoint aceita filtro de idioma, país ou data; o
idioma só é controlável pelo texto da consulta. `contentLanguage` existe apenas no detalhe do
canal. A disponibilidade de API no plano Hobby é contraditória na documentação.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import httpx

from raven.connectors.base import (
    AuthError,
    BadResponse,
    CallCost,
    Capability,
    Connector,
    ConnectorError,
    ConnectorInfo,
    ConnectorStatus,
    HealthReport,
    NotConfigured,
    QuotaExceeded,
    RateLimited,
    UpstreamError,
    VideoSearchRequest,
    VideoSearchResult,
)
from raven.connectors.http import HttpTransport, MinIntervalLimiter, redact
from raven.context import current_run_id
from raven.domain import Provenance, VideoRef
from raven.storage.ledger import Ledger, start_of_utc_day

SOURCE_ID = "outlierkit"
DEFAULT_BASE_URL = "https://outlierkit.com/api/v1"
CREDITS = {"outliers.search": 2, "outliers.refresh": 10, "videos.get": 2, "channels.search": 5}


def _map_error(resp: httpx.Response) -> ConnectorError | None:
    if resp.is_success:
        return None
    try:
        body = resp.json()
        status = str(body.get("status", ""))
        message = redact(str((body.get("error") or {}).get("message", "")))
    except ValueError:
        status, message = "", redact(resp.text[:200])
    code = resp.status_code
    if code == 401:
        return AuthError(SOURCE_ID, f"401 {status}: {message}")
    if code == 403:
        return AuthError(SOURCE_ID, f"403 {status} (trial encerrado ou plano sem API?): {message}")
    if code == 402:
        return QuotaExceeded(SOURCE_ID, f"402 créditos insuficientes: {message}")
    if code == 429:
        return RateLimited(SOURCE_ID, f"429: {message}")
    if code >= 500:
        return UpstreamError(SOURCE_ID, f"{code} {status}: {message}")
    return ConnectorError(SOURCE_ID, f"{code} {status}: {message}")


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


class OutlierKitConnector(Connector):
    info = ConnectorInfo(
        source_id=SOURCE_ID,
        name="OutlierKit",
        connector_version="0.1.0",
        api_version="v1",
        status=ConnectorStatus.REAL,
        capabilities=[Capability.OUTLIER_SEARCH, Capability.VIDEO_SEARCH, Capability.VIDEO_DETAILS],
        supports_language_filter=False,
        supports_region_filter=False,
        data_classes=["outliers"],
        cost_unit="credits",
        docs=["https://outlierkit.com/api-docs", "https://outlierkit.com/terms"],
        limitations=[
            "Sem filtros de idioma, país ou data em nenhum endpoint",
            "Busca semântica sobre índice próprio (cache); atualização ao vivo custa 10 créditos",
            "Créditos compartilhados com o app; sem rollover",
            "Acesso à API no plano Hobby contraditório na documentação",
            "Plano gratuito sem acesso à API (observado em 26/11/2026)",
        ],
    )

    def __init__(
        self,
        api_key: str | None,
        ledger: Ledger,
        *,
        base_url: str = DEFAULT_BASE_URL,
        credits_per_day: int = 60,
        usd_per_credit: float = 0.198,  # Pro: US$ 99 / 500 créditos (estimativa contábil)
        min_interval_s: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._configured = bool(api_key)
        self.ledger = ledger
        self.credits_per_day = credits_per_day
        self.usd_per_credit = usd_per_credit
        self._http = HttpTransport(
            SOURCE_ID,
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key or ''}"},
            limiter=MinIntervalLimiter(min_interval_s, max_concurrency=1),
            error_mapper=_map_error,
            client=client,
        )

    def _ensure_credits(self, credits: int) -> None:
        used = self.ledger.units_since(SOURCE_ID, start_of_utc_day())
        if used + credits > self.credits_per_day:
            raise QuotaExceeded(SOURCE_ID, f"limite diário local de créditos ({used}/{self.credits_per_day})")

    async def _call(self, operation: str, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self._configured:
            raise NotConfigured(SOURCE_ID, "RAVEN_OUTLIERKIT_API_KEY ausente")
        expected = CREDITS.get(operation, 5)
        self._ensure_credits(expected)
        charged = 0.0
        ok = False
        try:
            resp = await self._http.request(method, path, **kwargs)
            body = resp.json()
            charged = float((body.get("credits") or {}).get("charged") or 0)
            if not body.get("success"):
                raise BadResponse(SOURCE_ID, f"success=false: {redact(str(body)[:200])}")
            data = body.get("data")
            if not isinstance(data, dict):
                raise BadResponse(SOURCE_ID, "campo data ausente")
            ok = True
            return {"data": data, "credits": body.get("credits") or {}, "requestId": body.get("requestId")}
        finally:
            self.ledger.record(
                source_id=SOURCE_ID,
                operation=operation,
                units=charged,
                unit="credits",
                usd=charged * self.usd_per_credit,
                run_id=current_run_id.get(),
                ok=ok,
            )

    async def check(self, live: bool = False) -> HealthReport:
        if not self._configured:
            return HealthReport(source_id=SOURCE_ID, configured=False, detail="chave ausente")
        if not live:
            return HealthReport(source_id=SOURCE_ID, configured=True)
        try:
            # Menor custo documentado com resposta útil: outliers/search com limit=1 (2 créditos).
            out = await self._call(
                "outliers.search", "POST", "/outliers/search", json={"query": "history", "limit": 1}
            )
        except ConnectorError as exc:
            return HealthReport(source_id=SOURCE_ID, configured=True, authenticated=False, detail=str(exc))
        return HealthReport(
            source_id=SOURCE_ID,
            configured=True,
            authenticated=True,
            detail="outliers/search respondeu (2 créditos)",
            observed={"credits_remaining": out["credits"].get("remaining")},
        )

    def _to_video(self, r: dict[str, Any], request_ref: str | None) -> VideoRef:
        ch = r.get("channel") or {}
        return VideoRef(
            platform_id=str(r.get("videoId") or r.get("id")),
            title=r.get("title") or "",
            channel_id=ch.get("channelId"),
            channel_title=ch.get("title"),
            description=r.get("description"),
            published_at=_parse_dt(r.get("publishedDate")),
            statistics={"view_count": r.get("views"), "channel_subscribers": ch.get("subscribers")},
            vendor_metrics={
                "outlier": r.get("outlierMetrics") or {},
                "similarity": r.get("similarity"),
                "video_length": r.get("videoLength"),
            },
            provenance=Provenance(
                source_id=SOURCE_ID, data_class="outliers", endpoint="outliers", request_ref=request_ref
            ),
        )

    async def search_outliers(self, req: VideoSearchRequest, *, live: bool = False) -> VideoSearchResult:
        body: dict[str, Any] = {"query": req.query[:8000], "limit": max(1, min(req.max_results, 100))}
        if live:
            body["maxPages"] = 3
            op, path = "outliers.refresh", "/outliers/refresh"
        else:
            body["sortBy"] = "views" if req.order == "viewCount" else "similarity"
            op, path = "outliers.search", "/outliers/search"
        t0 = time.perf_counter()
        out = await self._call(op, "POST", path, json=body)
        results = out["data"].get("results") or []
        charged = float(out["credits"].get("charged") or 0)
        ignored = [f for f, v in (("language", req.language), ("region", req.region)) if v]
        if req.published_after:
            ignored.append("published_after")
        return VideoSearchResult(
            request=req,
            videos=[self._to_video(r, out.get("requestId")) for r in results],
            cost=CallCost(units=charged, unit="credits", usd=charged * self.usd_per_credit),
            latency_ms=(time.perf_counter() - t0) * 1000,
            applied_filters={k: v for k, v in body.items() if k != "query"},
            ignored_filters=ignored,
            request_ref=out.get("requestId"),
        )

    async def search_videos(self, req: VideoSearchRequest) -> VideoSearchResult:
        return await self.search_outliers(req)

    async def video_details(self, video_ids: list[str]) -> list[VideoRef]:
        out: list[VideoRef] = []
        for vid in video_ids:
            res = await self._call("videos.get", "GET", f"/videos/{vid}")
            out.append(self._to_video(res["data"], res.get("requestId")))
        return out

    async def aclose(self) -> None:
        await self._http.aclose()
