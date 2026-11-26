"""YouTube Data API v3 (chave de API; dados públicos = Non-Authorized Data).

Documentação: https://developers.google.com/youtube/v3/docs
Cota (determine_quota_cost): 100 chamadas search.list/dia em bucket próprio; demais endpoints
1 unidade por chamada, 10.000 unidades/dia por padrão; reinício à meia-noite do Pacífico.

Política: estatísticas ≤ 30 dias; sem métricas derivadas nem agregação entre canais sem a emenda
de derived metrics. Este conector apenas coleta; o uso é decidido pela `PolicyRegistry`.
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
from raven.storage.ledger import Ledger, start_of_pacific_day

SOURCE_ID = "youtube_data"
BASE_URL = "https://www.googleapis.com/youtube/v3"
HEALTH_VIDEO_ID = "jNQXAC9IVRw"  # vídeo público estável usado no teste de credencial (1 unidade)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _map_error(resp: httpx.Response) -> ConnectorError | None:
    if resp.is_success:
        return None
    try:
        err = resp.json().get("error", {})
        reasons = {e.get("reason") for e in err.get("errors", [])}
        message = err.get("message", "")
    except ValueError:
        reasons, message = set(), resp.text[:200]
    message = redact(message)
    if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
        return QuotaExceeded(SOURCE_ID, f"cota do YouTube esgotada: {message}")
    if reasons & {"rateLimitExceeded", "userRateLimitExceeded"} or resp.status_code == 429:
        return RateLimited(SOURCE_ID, message)
    key_problem = "API key not valid" in message or "API key expired" in message
    if (
        key_problem
        or reasons & {"keyInvalid", "forbidden", "accessNotConfigured"}
        or resp.status_code
        in (
            401,
            403,
        )
    ):
        return AuthError(SOURCE_ID, f"HTTP {resp.status_code} {sorted(reasons)}: {message}")
    if resp.status_code >= 500:
        return UpstreamError(SOURCE_ID, f"HTTP {resp.status_code}: {message}")
    return ConnectorError(SOURCE_ID, f"HTTP {resp.status_code} {sorted(reasons)}: {message}")


class YouTubeDataConnector(Connector):
    info = ConnectorInfo(
        source_id=SOURCE_ID,
        name="YouTube Data API v3",
        connector_version="0.1.0",
        api_version="v3",
        status=ConnectorStatus.REAL,
        capabilities=[Capability.VIDEO_SEARCH, Capability.VIDEO_DETAILS],
        supports_language_filter=True,  # relevanceLanguage (preferência, não filtro estrito)
        supports_region_filter=True,  # regionCode (vídeos visíveis no país, não público local)
        data_classes=["metadata", "statistics"],
        cost_unit="quota_units",
        docs=[
            "https://developers.google.com/youtube/v3/docs/search/list",
            "https://developers.google.com/youtube/v3/determine_quota_cost",
            "https://developers.google.com/youtube/terms/developer-policies",
        ],
        limitations=[
            "search.list limitado a 100 chamadas/dia na cota padrão",
            "relevanceLanguage é preferência: resultados em outros idiomas podem aparecer",
            "regionCode restringe disponibilidade, não identifica o público do vídeo",
            "search.list não retorna estatísticas; videos.list custa 1 unidade por até 50 ids",
            "Sem emenda de derived metrics: proibido calcular scores/razões com dados da API",
        ],
    )

    def __init__(
        self,
        api_key: str | None,
        ledger: Ledger,
        *,
        search_calls_per_day: int = 100,
        units_per_day: int = 10_000,
        min_interval_s: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._key = api_key
        self.ledger = ledger
        self.search_calls_per_day = search_calls_per_day
        self.units_per_day = units_per_day
        self._http = HttpTransport(
            SOURCE_ID,
            base_url=BASE_URL,
            limiter=MinIntervalLimiter(min_interval_s, max_concurrency=2),
            error_mapper=_map_error,
            client=client,
        )

    # ---- cota local -------------------------------------------------------------------

    def _ensure_quota(self, operation: str, units: float) -> None:
        since = start_of_pacific_day()
        if operation == "search.list":
            used = self.ledger.calls_since(SOURCE_ID, since, "search.list")
            if used + 1 > self.search_calls_per_day:
                raise QuotaExceeded(SOURCE_ID, f"limite local de search.list atingido ({used}/dia)")
            return
        used_units = self.ledger.units_since(SOURCE_ID, since) - self.ledger.units_since(
            SOURCE_ID, since, "search.list"
        )
        if used_units + units > self.units_per_day:
            raise QuotaExceeded(SOURCE_ID, f"limite local de unidades atingido ({used_units}/dia)")

    async def _get(self, operation: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self._key:
            raise NotConfigured(SOURCE_ID, "RAVEN_YOUTUBE_API_KEY ausente")
        self._ensure_quota(operation, 1)
        ok = False
        try:
            resp = await self._http.request("GET", path, params={**params, "key": self._key})
            data = resp.json()
            if not isinstance(data, dict):
                raise BadResponse(SOURCE_ID, "resposta não é objeto JSON")
            ok = True
            return data
        finally:
            # Chamadas com erro também consomem cota no YouTube.
            self.ledger.record(
                source_id=SOURCE_ID,
                operation=operation,
                units=1,
                unit="quota_units",
                usd=0.0,
                run_id=current_run_id.get(),
                ok=ok,
            )

    # ---- contrato -----------------------------------------------------------------------

    async def check(self, live: bool = False) -> HealthReport:
        if not self._key:
            return HealthReport(source_id=SOURCE_ID, configured=False, detail="chave ausente")
        if not live:
            return HealthReport(source_id=SOURCE_ID, configured=True)
        try:
            data = await self._get("videos.list", "/videos", {"part": "id", "id": HEALTH_VIDEO_ID})
        except ConnectorError as exc:
            return HealthReport(source_id=SOURCE_ID, configured=True, authenticated=False, detail=str(exc))
        return HealthReport(
            source_id=SOURCE_ID,
            configured=True,
            authenticated=True,
            detail="videos.list respondeu",
            observed={"items": len(data.get("items", []))},
        )

    async def search_videos(self, req: VideoSearchRequest) -> VideoSearchResult:
        params: dict[str, Any] = {
            "part": "snippet",
            "type": "video",
            "q": req.query,
            "maxResults": max(1, min(req.max_results, 50)),
            "order": req.order,
        }
        if req.language:
            params["relevanceLanguage"] = req.language
        if req.region:
            params["regionCode"] = req.region.upper()
        if req.published_after:
            params["publishedAfter"] = req.published_after.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        t0 = time.perf_counter()
        data = await self._get("search.list", "/search", params)
        ids: list[str] = []
        snippets: dict[str, dict[str, Any]] = {}
        for item in data.get("items", []):
            vid = (item.get("id") or {}).get("videoId")
            if vid:
                ids.append(vid)
                snippets[vid] = item.get("snippet", {})
        videos = await self.video_details(ids) if ids else []
        found = {v.platform_id for v in videos}
        # Vídeos que sumiram entre search e videos.list: mantém apenas snippet.
        for vid in ids:
            if vid not in found:
                videos.append(self._from_snippet(vid, snippets[vid], {}, {}))
        order = {vid: i for i, vid in enumerate(ids)}
        videos.sort(key=lambda v: order.get(v.platform_id, 1_000))
        return VideoSearchResult(
            request=req,
            videos=videos,
            cost=CallCost(units=1 + (len(ids) + 49) // 50, unit="quota_units", usd=0.0),
            latency_ms=(time.perf_counter() - t0) * 1000,
            applied_filters={k: params[k] for k in ("relevanceLanguage", "regionCode") if k in params},
            request_ref=data.get("etag"),
        )

    async def video_details(self, video_ids: list[str]) -> list[VideoRef]:
        out: list[VideoRef] = []
        for i in range(0, len(video_ids), 50):
            chunk = video_ids[i : i + 50]
            data = await self._get(
                "videos.list",
                "/videos",
                {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)},
            )
            for item in data.get("items", []):
                out.append(
                    self._from_snippet(
                        item["id"],
                        item.get("snippet", {}),
                        item.get("statistics", {}),
                        item.get("contentDetails", {}),
                    )
                )
        return out

    @staticmethod
    def _from_snippet(
        vid: str, sn: dict[str, Any], stats: dict[str, Any], details: dict[str, Any]
    ) -> VideoRef:
        def _int(v: Any) -> int | None:
            try:
                return int(v)
            except (TypeError, ValueError):
                return None

        return VideoRef(
            platform_id=vid,
            title=sn.get("title", ""),
            channel_id=sn.get("channelId"),
            channel_title=sn.get("channelTitle"),
            description=(sn.get("description") or "")[:1000],
            published_at=_parse_dt(sn.get("publishedAt")),
            language_hint=sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
            statistics={
                "view_count": _int(stats.get("viewCount")),
                "like_count": _int(stats.get("likeCount")),
                "comment_count": _int(stats.get("commentCount")),
            }
            if stats
            else {},
            vendor_metrics={"duration": details.get("duration")} if details else {},
            provenance=Provenance(
                source_id=SOURCE_ID,
                data_class="metadata",
                endpoint="videos.list" if stats else "search.list",
            ),
            metrics_data_class="statistics",
        )

    async def aclose(self) -> None:
        await self._http.aclose()
