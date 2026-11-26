"""Testes autenticados contra as APIs reais. Rodam só com `-m live` e credenciais no .env.

    uv run pytest -m live tests/live -v

Custo aproximado por execução completa: YouTube 3 unidades + 1 search.list; DataForSEO ~US$ 0,013;
OutlierKit 2 créditos; vidIQ 0 créditos (tools/list) + 5 se tool_map configurado.
"""

from __future__ import annotations

import pytest

from raven.connectors.base import TrendsRequest, VideoSearchRequest
from raven.connectors.registry import build_connectors
from raven.settings import Secrets

pytestmark = pytest.mark.live


@pytest.fixture
def conns(cfg, ledger):
    return build_connectors(cfg, Secrets(), ledger)


def _need(secret: object, name: str) -> None:
    if secret is None:
        pytest.skip(f"{name} ausente")


async def test_youtube_live(conns):
    _need(Secrets().youtube_api_key, "RAVEN_YOUTUBE_API_KEY")
    yt = conns["youtube_data"]
    assert (await yt.check(live=True)).authenticated
    res = await yt.search_videos(
        VideoSearchRequest(query="Mecanismo de Anticítera", language="pt", region="BR", max_results=5)
    )
    assert res.videos and all(v.platform_id and v.title for v in res.videos)


async def test_dataforseo_live(conns):
    _need(Secrets().dataforseo_login, "RAVEN_DATAFORSEO_LOGIN")
    d4 = conns["dataforseo"]
    health = await d4.check(live=True)
    assert health.authenticated, health.detail
    series = await d4.search_trends(
        TrendsRequest(keywords=["tardígrados"], location_code=2076, language="pt")
    )
    assert series and series[0].points
    res = await d4.search_videos(
        VideoSearchRequest(query="tardígrados", language="pt", region="BR", max_results=20)
    )
    assert res.cost.usd is not None


async def test_outlierkit_live(conns):
    _need(Secrets().outlierkit_api_key, "RAVEN_OUTLIERKIT_API_KEY")
    health = await conns["outlierkit"].check(live=True)
    assert health.authenticated, health.detail


async def test_vidiq_live(conns):
    _need(Secrets().vidiq_api_key, "RAVEN_VIDIQ_API_KEY")
    health = await conns["vidiq"].check(live=True)
    assert health.authenticated, health.detail
    assert health.observed.get("tools")


async def test_openai_live(cfg, ledger, tmp_path):
    key = Secrets().openai_api_key
    _need(key, "RAVEN_OPENAI_API_KEY")
    assert key is not None
    from raven.llm.cache import LLMCache
    from raven.llm.client import OpenAIResponsesClient
    from raven.llm.schemas import QueryPlan

    client = OpenAIResponsesClient(
        key.get_secret_value(), cfg.llm, ledger, cfg.budgets, LLMCache(tmp_path / "c.sqlite3")
    )
    r = await client.structured(
        task="classification",
        instructions="Gere 1 consulta de busca em alemão para o assunto.",
        user_input="Assunto: tardígrados",
        output_model=QueryPlan,
        prompt_version="live-test",
    )
    assert r.output.per_language and r.usd > 0
    vec = await client.embed(["tardigrades", "Bärtierchen"])
    assert len(vec) == 2 and len(vec[0]) == (cfg.llm.embedding_dimensions or len(vec[0]))
