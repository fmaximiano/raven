from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from raven.context import current_run_id
from raven.llm.cache import LLMCache
from raven.llm.client import OpenAIResponsesClient, compute_usd
from raven.llm.schemas import QueryPlan
from raven.storage.ledger import BudgetExceeded


class FakeResponses:
    def __init__(self, parsed: Any, usage: Any) -> None:
        self.parsed, self.usage, self.calls = parsed, usage, []

    async def parse(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(output_parsed=self.parsed, usage=self.usage, status="completed")


def make_client(cfg, ledger, parsed=None, budgets=None):
    usage = SimpleNamespace(
        input_tokens=1000,
        output_tokens=500,
        input_tokens_details=SimpleNamespace(cached_tokens=200, cache_write_tokens=0),
    )
    responses = FakeResponses(parsed or QueryPlan(per_language=[]), usage)
    sdk = SimpleNamespace(responses=responses)
    client = OpenAIResponsesClient(
        "k", cfg.llm, ledger, budgets or cfg.budgets, LLMCache(":memory:"), sdk_client=sdk
    )
    return client, responses


def test_compute_usd_with_cache_and_write(cfg):
    # gpt-6.2-sol: 2.00 entrada, 0.10 cache, 10.00 saída, 2.50 escrita
    usd = compute_usd(cfg.llm, "gpt-6.2-sol", 1000, 200, 500, cache_write=100)
    assert usd == pytest.approx((700 * 2.0 + 200 * 0.10 + 100 * 2.5 + 500 * 10.0) / 1e6)


async def test_structured_routes_records_cost_and_caches(cfg, ledger):
    client, responses = make_client(cfg, ledger)
    token = current_run_id.set("run-x")
    try:
        r1 = await client.structured(
            task="query_generation",
            instructions="i",
            user_input="u",
            output_model=QueryPlan,
            prompt_version="v1",
        )
        r2 = await client.structured(
            task="query_generation",
            instructions="i",
            user_input="u",
            output_model=QueryPlan,
            prompt_version="v1",
        )
        r3 = await client.structured(
            task="query_generation",
            instructions="i",
            user_input="u",
            output_model=QueryPlan,
            prompt_version="v2",
        )
    finally:
        current_run_id.reset(token)
    assert r1.model == "gpt-6.2-sol" and not r1.cached and r1.usd > 0
    assert r2.cached and r2.usd == 0
    assert not r3.cached  # nova versão de prompt invalida o cache
    assert len(responses.calls) == 2
    call = responses.calls[0]
    assert call["text_format"] is QueryPlan and call["store"] is False and "tools" not in call
    assert "DADOS NÃO CONFIÁVEIS" in call["instructions"]
    assert ledger.usd_for_run("run-x") == pytest.approx(2 * r1.usd)


async def test_budget_blocks_before_calling(cfg, ledger):
    tiny = cfg.budgets.model_copy(update={"usd_per_run": 0.0001})
    client, responses = make_client(cfg, ledger, budgets=tiny)
    token = current_run_id.set("run-y")
    try:
        with pytest.raises(BudgetExceeded):
            await client.structured(
                task="deep_research",
                instructions="i",
                user_input="u" * 1000,
                output_model=QueryPlan,
                prompt_version="v1",
            )
    finally:
        current_run_id.reset(token)
    assert responses.calls == []


async def test_unknown_task_route_fails(cfg, ledger):
    client, _ = make_client(cfg, ledger)
    with pytest.raises(KeyError):
        await client.structured(
            task="inexistente", instructions="i", user_input="u", output_model=QueryPlan, prompt_version="v1"
        )
