"""Testes de contrato: respostas no formato documentado de cada API (sem rede).

As fixtures reproduzem os campos documentados (consultados em 26/11/2026). Se o fornecedor
mudar o formato, o teste `live` correspondente deve falhar e estas fixtures, ser atualizadas.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from raven.connectors.base import (
    AuthError,
    CapabilityNotSupported,
    NotConfigured,
    QuotaExceeded,
    TrendsRequest,
    UpstreamError,
    VideoSearchRequest,
)
from raven.connectors.dataforseo import DataForSEOConnector, sanitize_trends_keyword
from raven.connectors.outlierkit import OutlierKitConnector
from raven.connectors.vidiq import VidIQConnector, extract_videos
from raven.connectors.youtube_data import YouTubeDataConnector

YT = "https://www.googleapis.com/youtube/v3"
D4 = "https://api.dataforseo.com/v3"
OK_URL = "https://outlierkit.com/api/v1"

YT_SEARCH = {
    "kind": "youtube#searchListResponse",
    "etag": "etag-1",
    "items": [
        {
            "id": {"kind": "youtube#video", "videoId": "vid00000001"},
            "snippet": {
                "title": "Mecanismo de Anticítera",
                "channelId": "UC1",
                "channelTitle": "Canal A",
                "publishedAt": "2025-01-02T03:04:05Z",
                "description": "d",
            },
        },
        {
            "id": {"kind": "youtube#video", "videoId": "vid00000002"},
            "snippet": {
                "title": "Outro",
                "channelId": "UC2",
                "channelTitle": "Canal B",
                "publishedAt": "2024-05-06T00:00:00Z",
            },
        },
    ],
}
YT_VIDEOS = {
    "items": [
        {
            "id": "vid00000001",
            "snippet": {
                "title": "Mecanismo de Anticítera",
                "channelId": "UC1",
                "channelTitle": "Canal A",
                "publishedAt": "2025-01-02T03:04:05Z",
                "defaultAudioLanguage": "pt-BR",
            },
            "statistics": {"viewCount": "12345", "likeCount": "10"},
            "contentDetails": {"duration": "PT10M"},
        },
    ]
}


def yt(ledger, **kw) -> YouTubeDataConnector:
    return YouTubeDataConnector("SECRET-KEY", ledger, min_interval_s=0, **kw)


# ---- YouTube Data API ----------------------------------------------------------------------


@respx.mock
async def test_youtube_search_merges_details_and_keeps_order(ledger):
    respx.get(f"{YT}/search").respond(json=YT_SEARCH)
    respx.get(f"{YT}/videos").respond(json=YT_VIDEOS)
    res = await yt(ledger).search_videos(
        VideoSearchRequest(query="anticitera", language="pt", region="br", max_results=10)
    )
    assert [v.platform_id for v in res.videos] == ["vid00000001", "vid00000002"]
    v1, v2 = res.videos
    assert v1.statistics["view_count"] == 12345 and v1.language_hint == "pt-BR"
    assert v1.provenance.data_class == "metadata" and v1.metrics_data_class == "statistics"
    assert v2.statistics == {}  # removido entre search e videos.list: só snippet
    params = respx.calls[0].request.url.params
    assert params["relevanceLanguage"] == "pt" and params["regionCode"] == "BR" and params["type"] == "video"
    assert res.applied_filters == {"relevanceLanguage": "pt", "regionCode": "BR"}


@respx.mock
async def test_youtube_quota_error_mapped_and_key_redacted(ledger):
    respx.get(f"{YT}/search").respond(
        403,
        json={
            "error": {"code": 403, "message": "quota key=SECRET-KEY", "errors": [{"reason": "quotaExceeded"}]}
        },
    )
    with pytest.raises(QuotaExceeded) as exc:
        await yt(ledger).search_videos(VideoSearchRequest(query="x"))
    assert "SECRET-KEY" not in str(exc.value)
    # chamada com erro também consome cota e é registrada
    from datetime import UTC, datetime, timedelta

    assert ledger.calls_since("youtube_data", datetime.now(UTC) - timedelta(hours=1), "search.list") == 1


@respx.mock
async def test_youtube_local_search_quota_blocks_before_call(ledger):
    route = respx.get(f"{YT}/search").respond(json={"items": []})
    conn = yt(ledger, search_calls_per_day=1)
    await conn.search_videos(VideoSearchRequest(query="a"))
    with pytest.raises(QuotaExceeded):
        await conn.search_videos(VideoSearchRequest(query="b"))
    assert route.call_count == 1


@respx.mock
async def test_youtube_invalid_key_is_auth_error(ledger):
    respx.get(f"{YT}/videos").respond(
        400, json={"error": {"errors": [{"reason": "keyInvalid"}], "message": "API key not valid"}}
    )
    r = await yt(ledger).check(live=True)
    assert r.authenticated is False and "keyInvalid" in r.detail


async def test_youtube_without_key(ledger):
    conn = YouTubeDataConnector(None, ledger)
    assert (await conn.check()).configured is False
    with pytest.raises(NotConfigured):
        await conn.search_videos(VideoSearchRequest(query="x"))


@respx.mock
async def test_youtube_retries_transient_errors(ledger, monkeypatch):
    import raven.connectors.http as http_mod

    monkeypatch.setattr(http_mod, "wait_exponential_jitter", lambda **_: lambda _rs: 0)
    route = respx.get(f"{YT}/videos").mock(
        side_effect=[
            httpx.Response(503, json={"error": {"message": "x"}}),
            httpx.Response(200, json=YT_VIDEOS),
        ]
    )
    vids = await yt(ledger).video_details(["vid00000001"])
    assert len(vids) == 1 and route.call_count == 2


# ---- DataForSEO -----------------------------------------------------------------------------


def d4(ledger, **kw) -> DataForSEOConnector:
    return DataForSEOConnector("login", "pass", ledger, location_codes={"BR": 2076}, min_interval_s=0, **kw)


TRENDS_OK = {
    "status_code": 20000,
    "status_message": "Ok.",
    "cost": 0.011,
    "tasks_error": 0,
    "tasks": [
        {
            "id": "task-1",
            "status_code": 20000,
            "status_message": "Ok.",
            "cost": 0.011,
            "result": [
                {
                    "keywords": ["anticitera", "computador grego"],
                    "items": [
                        {
                            "type": "google_trends_graph",
                            "keywords": ["anticitera", "computador grego"],
                            "data": [
                                {
                                    "date_from": "2026-01-01",
                                    "date_to": "2026-01-31",
                                    "timestamp": 1,
                                    "missing_data": False,
                                    "values": [54, 0],
                                },
                                {
                                    "date_from": "2026-02-01",
                                    "date_to": "2026-02-28",
                                    "timestamp": 2,
                                    "missing_data": True,
                                    "values": [None, None],
                                },
                                {
                                    "date_from": "2026-03-01",
                                    "date_to": "2026-03-31",
                                    "timestamp": 3,
                                    "missing_data": False,
                                    "values": [70, 12],
                                },
                            ],
                        }
                    ],
                }
            ],
        }
    ],
}


@respx.mock
async def test_dataforseo_trends_parsing_and_cost(ledger):
    route = respx.post(f"{D4}/keywords_data/google_trends/explore/live").respond(json=TRENDS_OK)
    out = await d4(ledger).search_trends(
        TrendsRequest(keywords=["anticitera", "computador-grego"], location_code=2076, language="pt")
    )
    body = route.calls[0].request.content.decode()
    assert '"type":"youtube"' in body.replace(" ", "") and "computador grego" in body  # hífen sanitizado
    assert [s.keyword for s in out] == ["anticitera", "computador grego"]
    assert [p.value for p in out[0].points] == [54.0, None, 70.0]
    assert out[1].points[0].value == 0.0
    assert out[0].provenance.data_class == "trends" and out[0].provenance.request_ref == "task-1"
    from datetime import UTC, datetime, timedelta

    assert ledger.usd_since(datetime.now(UTC) - timedelta(hours=1), "dataforseo") == pytest.approx(0.011)


@respx.mock
async def test_dataforseo_task_level_errors(ledger):
    respx.post(f"{D4}/keywords_data/google_trends/explore/live").respond(
        json={
            "status_code": 20000,
            "cost": 0,
            "tasks": [{"status_code": 40200, "status_message": "Payment Required."}],
        }
    )
    with pytest.raises(QuotaExceeded):
        await d4(ledger).search_trends(TrendsRequest(keywords=["x y"]))
    respx.post(f"{D4}/serp/youtube/organic/live/advanced").respond(
        json={"status_code": 40100, "status_message": "auth"}
    )
    with pytest.raises(AuthError):
        await d4(ledger).search_videos(VideoSearchRequest(query="x", region="BR", language="pt"))


@respx.mock
async def test_dataforseo_local_budget(ledger):
    route = respx.post(f"{D4}/keywords_data/google_trends/explore/live").respond(json=TRENDS_OK)
    conn = d4(ledger, usd_per_day=0.015)
    await conn.search_trends(TrendsRequest(keywords=["ab"]))
    with pytest.raises(QuotaExceeded):
        await conn.search_trends(TrendsRequest(keywords=["cd"]))
    assert route.call_count == 1


@respx.mock
async def test_dataforseo_youtube_serp(ledger):
    respx.post(f"{D4}/serp/youtube/organic/live/advanced").respond(
        json={
            "status_code": 20000,
            "cost": 0.002,
            "tasks": [
                {
                    "id": "t2",
                    "status_code": 20000,
                    "cost": 0.002,
                    "result": [
                        {
                            "items": [
                                {"type": "youtube_channel", "channel_id": "UCx"},
                                {
                                    "type": "youtube_video",
                                    "video_id": "abcdefghijk",
                                    "title": "Vídeo",
                                    "channel_name": "C",
                                    "channel_id": "UC1",
                                    "views_count": 999,
                                    "timestamp": "2025-03-04 05:06:07 +00:00",
                                    "is_shorts": False,
                                    "duration_time_seconds": 600,
                                    "url": "https://www.youtube.com/watch?v=abcdefghijk",
                                },
                            ]
                        }
                    ],
                }
            ],
        }
    )
    res = await d4(ledger).search_videos(
        VideoSearchRequest(query="a+b%", region="BR", language="pt", max_results=20)
    )
    assert [v.platform_id for v in res.videos] == ["abcdefghijk"]
    v = res.videos[0]
    assert v.statistics["view_count"] == 999 and v.published_at and v.published_at.year == 2025
    assert v.provenance.data_class == "youtube_serp"
    assert res.cost.usd == 0.002


def test_trends_keyword_sanitization():
    assert sanitize_trends_keyword('a-b "c" (d), e') == "a b c d e"
    assert len(sanitize_trends_keyword("x" * 300)) == 100


# ---- OutlierKit -----------------------------------------------------------------------------


@respx.mock
async def test_outlierkit_search_contract(ledger):
    route = respx.post(f"{OK_URL}/outliers/search").respond(
        json={
            "success": True,
            "requestId": "req-1",
            "credits": {"charged": 2, "remaining": 98},
            "timing": {"totalMs": 120},
            "data": {
                "searchText": "q",
                "totalFound": 1,
                "hasMore": False,
                "results": [
                    {
                        "id": "ok1",
                        "videoId": "vidOK000001",
                        "title": "Outlier",
                        "description": "d",
                        "views": 500000,
                        "publishedDate": "2025-06-01T00:00:00Z",
                        "similarity": 0.81,
                        "outlierMetrics": {"outlierMultiplier": 12.5, "channelOutlierRatio": 3.1},
                        "channel": {"channelId": "UCok", "title": "Canal OK", "subscribers": 40000},
                    }
                ],
            },
        }
    )
    conn = OutlierKitConnector("KEY", ledger, min_interval_s=0)
    res = await conn.search_outliers(VideoSearchRequest(query="q", language="ja", region="JP"))
    assert route.calls[0].request.headers["Authorization"] == "Bearer KEY"
    assert set(res.ignored_filters) == {"language", "region"}  # API não filtra idioma/país
    v = res.videos[0]
    assert v.vendor_metrics["outlier"]["outlierMultiplier"] == 12.5
    assert v.provenance.data_class == "outliers" and res.cost.units == 2


@respx.mock
async def test_outlierkit_errors(ledger):
    respx.post(f"{OK_URL}/outliers/search").respond(
        402,
        json={
            "success": False,
            "status": "INSUFFICIENT_CREDITS",
            "error": {"message": "no credits"},
            "credits": {"charged": 0},
        },
    )
    with pytest.raises(QuotaExceeded):
        await OutlierKitConnector("KEY", ledger, min_interval_s=0).search_outliers(
            VideoSearchRequest(query="q")
        )
    respx.post(f"{OK_URL}/outliers/search").respond(
        403,
        json={"success": False, "status": "FORBIDDEN", "error": {"message": "plan", "reason": "trial_ended"}},
    )
    with pytest.raises(AuthError):
        await OutlierKitConnector("KEY", ledger, min_interval_s=0).search_outliers(
            VideoSearchRequest(query="q")
        )


@respx.mock
async def test_outlierkit_5xx_is_retryable(ledger, monkeypatch):
    import raven.connectors.http as http_mod

    monkeypatch.setattr(http_mod, "wait_exponential_jitter", lambda **_: lambda _rs: 0)
    respx.post(f"{OK_URL}/outliers/search").respond(
        502, json={"success": False, "status": "UPSTREAM_ERROR", "error": {"message": "x"}}
    )
    with pytest.raises(UpstreamError):
        await OutlierKitConnector("KEY", ledger, min_interval_s=0).search_outliers(
            VideoSearchRequest(query="q")
        )
    assert respx.calls.call_count == 4  # max_attempts


# ---- vidIQ ----------------------------------------------------------------------------------


async def test_vidiq_requires_tool_map(ledger):
    with pytest.raises(CapabilityNotSupported):
        await VidIQConnector("KEY", ledger).search_outliers(VideoSearchRequest(query="q"))


def test_vidiq_tolerant_extraction():
    payload = {
        "data": {
            "videos": [
                {"videoId": "abcdefghijk", "title": "T", "outlierScore": 9},
                {"id": "short", "title": "x"},
            ],
            "other": {"video_id": "zzzzzzzzzzz", "videoTitle": "Z"},
        }
    }
    vids = extract_videos(payload)
    assert [v["_video_id"] for v in vids] == ["abcdefghijk", "zzzzzzzzzzz"]
