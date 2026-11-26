"""DataForSEO v3: Google Trends Explore (web/youtube) e SERP orgânica do YouTube.

Documentação:
- https://docs.dataforseo.com/v3/keywords_data/google_trends/explore/live/
- https://docs.dataforseo.com/v3/serp/youtube/organic/live/advanced/
Autenticação HTTP Basic (login/senha de app.dataforseo.com/api-access). Corpo = array de tarefas;
endpoints Live aceitam uma tarefa por chamada. O campo `cost` (USD) da resposta é registrado
como custo real.

Observações de conformidade (ver config/source_policies.yaml): uso de dados como entrada de
score depende de confirmação do fornecedor. Valores do Trends são índices relativos (0–100)
normalizados por requisição: não comparar níveis absolutos entre requisições distintas.
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
    TrendsRequest,
    UpstreamError,
    VideoSearchRequest,
    VideoSearchResult,
)
from raven.connectors.http import HttpTransport, MinIntervalLimiter
from raven.context import current_run_id
from raven.domain import Provenance, TrendPoint, TrendSeries, VideoRef
from raven.storage.ledger import Ledger, start_of_utc_day

SOURCE_ID = "dataforseo"
BASE_URL = "https://api.dataforseo.com/v3"
OK = 20000

TRENDS_PATH = "/keywords_data/google_trends/explore/live"
YT_SERP_PATH = "/serp/youtube/organic/live/advanced"
# Caracteres proibidos em keywords do Trends segundo a documentação (inclui hífen).
_TRENDS_FORBIDDEN = set('<>|"-+=~!:*()[]{}')


def sanitize_trends_keyword(kw: str) -> str:
    cleaned = "".join(" " if c in _TRENDS_FORBIDDEN else c for c in kw).replace(",", " ")
    return " ".join(cleaned.split())[:100]


def _status_error(code: int, message: str) -> ConnectorError:
    # Mapeamento por faixa; conferir com https://docs.dataforseo.com/v3/appendix/errors
    if 40100 <= code < 40200:
        return AuthError(SOURCE_ID, f"{code}: {message}")
    if 40200 <= code < 40300:
        return QuotaExceeded(SOURCE_ID, f"{code}: {message}")
    if code >= 50000:
        return UpstreamError(SOURCE_ID, f"{code}: {message}")
    return ConnectorError(SOURCE_ID, f"{code}: {message}")


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace(" +00:00", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


class DataForSEOConnector(Connector):
    info = ConnectorInfo(
        source_id=SOURCE_ID,
        name="DataForSEO",
        connector_version="0.1.0",
        api_version="v3",
        status=ConnectorStatus.REAL,
        capabilities=[Capability.SEARCH_TRENDS, Capability.VIDEO_SEARCH],
        supports_language_filter=True,
        supports_region_filter=True,
        data_classes=["trends", "youtube_serp"],
        cost_unit="usd",
        docs=[
            "https://docs.dataforseo.com/v3/keywords_data/google_trends/explore/live/",
            "https://docs.dataforseo.com/v3/serp/youtube/organic/live/advanced/",
            "https://dataforseo.com/pricing",
        ],
        limitations=[
            "Trends: até 5 keywords por tarefa; índices relativos à própria requisição",
            "Trends: hífen e outros símbolos proibidos nas keywords (sanitizados)",
            "SERP YouTube: cobrança por bloco de 20 resultados",
            "Depósito mínimo de US$ 50; crédito de teste de US$ 1",
            "Termos 7.1: dados de SERP não podem ser usados para competir com buscadores",
        ],
    )

    def __init__(
        self,
        login: str | None,
        password: str | None,
        ledger: Ledger,
        *,
        location_codes: dict[str, int],
        usd_per_day: float = 2.0,
        price_usd: dict[str, float] | None = None,
        min_interval_s: float = 0.3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._configured = bool(login and password)
        self.ledger = ledger
        self.location_codes = {k.upper(): v for k, v in location_codes.items()}
        self.usd_per_day = usd_per_day
        self.price = {"trends_explore_live": 0.011, "youtube_serp_live_per_20": 0.002}
        self.price.update(price_usd or {})
        self._http = HttpTransport(
            SOURCE_ID,
            base_url=BASE_URL,
            auth=(login or "", password or ""),
            timeout_s=90.0,  # Trends Live: "até 32 s em média"
            limiter=MinIntervalLimiter(min_interval_s, max_concurrency=2),
            client=client,
        )

    def _ensure_budget(self, expected_usd: float) -> None:
        spent = self.ledger.usd_since(start_of_utc_day(), SOURCE_ID)
        if spent + expected_usd > self.usd_per_day:
            raise QuotaExceeded(
                SOURCE_ID, f"limite diário local de US$ {self.usd_per_day:.2f} (gasto {spent:.4f})"
            )

    async def _post(
        self, operation: str, path: str, task: dict[str, Any], expected_usd: float
    ) -> dict[str, Any]:
        if not self._configured:
            raise NotConfigured(SOURCE_ID, "RAVEN_DATAFORSEO_LOGIN/PASSWORD ausentes")
        self._ensure_budget(expected_usd)
        usd: float | None = None
        ok = False
        try:
            resp = await self._http.request("POST", path, json=[task])
            body = resp.json()
            if body.get("status_code") != OK:
                raise _status_error(int(body.get("status_code", 0)), body.get("status_message", ""))
            usd = float(body.get("cost") or 0.0)
            tasks = body.get("tasks") or []
            if not tasks:
                raise BadResponse(SOURCE_ID, "resposta sem tasks")
            t = tasks[0]
            if t.get("status_code") != OK:
                raise _status_error(int(t.get("status_code", 0)), t.get("status_message", ""))
            ok = True
            return dict(t)
        finally:
            self.ledger.record(
                source_id=SOURCE_ID,
                operation=operation,
                units=usd if usd is not None else 0.0,
                unit="usd",
                usd=usd if usd is not None else 0.0,
                run_id=current_run_id.get(),
                ok=ok,
            )

    async def check(self, live: bool = False) -> HealthReport:
        if not self._configured:
            return HealthReport(source_id=SOURCE_ID, configured=False, detail="credenciais ausentes")
        if not live:
            return HealthReport(source_id=SOURCE_ID, configured=True)
        try:
            # Endpoint de conta: não cobra tarefa e confirma autenticação e saldo.
            resp = await self._http.request("GET", "/appendix/user_data")
            body = resp.json()
            if body.get("status_code") != OK:
                raise _status_error(int(body.get("status_code", 0)), body.get("status_message", ""))
            result = ((body.get("tasks") or [{}])[0].get("result") or [{}])[0]
            money = result.get("money") or {}
            return HealthReport(
                source_id=SOURCE_ID,
                configured=True,
                authenticated=True,
                detail="user_data respondeu",
                observed={"balance_usd": money.get("balance")},
            )
        except ConnectorError as exc:
            return HealthReport(source_id=SOURCE_ID, configured=True, authenticated=False, detail=str(exc))

    async def search_trends(self, req: TrendsRequest) -> list[TrendSeries]:
        keywords = [sanitize_trends_keyword(k) for k in req.keywords]
        keywords = [k for k in keywords if len(k) > 1]
        if not keywords:
            return []
        task: dict[str, Any] = {
            "keywords": keywords,
            "type": req.search_type,
            "time_range": req.time_range,
            "item_types": ["google_trends_graph"],
        }
        if req.location_code:
            task["location_code"] = req.location_code
        if req.language:
            task["language_code"] = req.language
        t = await self._post("trends.explore", TRENDS_PATH, task, self.price["trends_explore_live"])
        series: dict[str, list[TrendPoint]] = {k: [] for k in keywords}
        for result in t.get("result") or []:
            for item in result.get("items") or []:
                if item.get("type") != "google_trends_graph":
                    continue
                item_kws: list[str] = item.get("keywords") or keywords
                for point in item.get("data") or []:
                    values = point.get("values")
                    if not isinstance(values, list):
                        values = [values]
                    for i, kw in enumerate(item_kws):
                        v = values[i] if i < len(values) else None
                        missing = bool(point.get("missing_data"))
                        series.setdefault(kw, []).append(
                            TrendPoint(
                                date=point.get("date_from", ""),
                                value=None if (missing or v is None) else float(v),
                            )
                        )
        prov = Provenance(
            source_id=SOURCE_ID, data_class="trends", endpoint=TRENDS_PATH, request_ref=t.get("id")
        )
        return [
            TrendSeries(
                keyword=kw,
                location=str(req.location_code) if req.location_code else None,
                language=req.language,
                points=pts,
                provenance=prov,
            )
            for kw, pts in series.items()
        ]

    async def search_videos(self, req: VideoSearchRequest) -> VideoSearchResult:
        region = (req.region or "US").upper()
        location = self.location_codes.get(region)
        if location is None:
            raise ConnectorError(SOURCE_ID, f"sem location_code configurado para {region}")
        depth = max(20, min(((req.max_results + 19) // 20) * 20, 200))
        task = {
            "keyword": req.query.replace("%", "%25").replace("+", "%2B")[:700],
            "location_code": location,
            "language_code": req.language or "en",
            "block_depth": depth,
        }
        expected = self.price["youtube_serp_live_per_20"] * depth / 20
        t0 = time.perf_counter()
        t = await self._post("youtube_serp", YT_SERP_PATH, task, expected)
        videos: list[VideoRef] = []
        for result in t.get("result") or []:
            for item in result.get("items") or []:
                if item.get("type") != "youtube_video" or not item.get("video_id"):
                    continue
                videos.append(
                    VideoRef(
                        platform_id=item["video_id"],
                        title=item.get("title") or "",
                        channel_id=item.get("channel_id"),
                        channel_title=item.get("channel_name"),
                        description=(item.get("description") or "")[:1000],
                        published_at=_parse_dt(item.get("timestamp")),
                        url=item.get("url"),
                        statistics={"view_count": item.get("views_count")},
                        vendor_metrics={
                            "rank": item.get("rank_absolute"),
                            "is_shorts": item.get("is_shorts"),
                            "duration_s": item.get("duration_time_seconds"),
                            "publication_date_text": item.get("publication_date"),
                        },
                        provenance=Provenance(
                            source_id=SOURCE_ID,
                            data_class="youtube_serp",
                            endpoint=YT_SERP_PATH,
                            request_ref=t.get("id"),
                        ),
                    )
                )
        return VideoSearchResult(
            request=req,
            videos=videos[: req.max_results],
            cost=CallCost(units=float(t.get("cost") or 0.0), unit="usd", usd=float(t.get("cost") or 0.0)),
            latency_ms=(time.perf_counter() - t0) * 1000,
            applied_filters={"location_code": location, "language_code": task["language_code"]},
            ignored_filters=["published_after"] if req.published_after else [],
            request_ref=t.get("id"),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
