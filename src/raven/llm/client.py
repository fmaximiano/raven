"""Cliente de LLM: OpenAI Responses API com roteamento por tarefa, cache, orçamento e custos.

- Saída estruturada via `responses.parse(text_format=<modelo Pydantic>)` (JSON Schema estrito).
- Limites de gasto verificados ANTES da chamada (por execução, dia e mês) com estimativa
  conservadora; custo real registrado a partir de `usage`.
- Nenhuma ferramenta é habilitada em chamadas que recebem conteúdo externo.
"""

from __future__ import annotations

import abc
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from raven.context import current_run_id
from raven.llm.cache import LLMCache, cache_key
from raven.llm.safety import SAFETY_PREAMBLE
from raven.settings import Budgets, LLMConfig
from raven.storage.ledger import BudgetExceeded, Ledger, start_of_utc_day, start_of_utc_month

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
SOURCE_ID = "openai"


class LLMError(RuntimeError):
    pass


@dataclass
class LLMResult(Generic[T]):
    output: T
    model: str
    task: str
    prompt_version: str
    cached: bool
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0
    latency_ms: float = 0.0
    simulated: bool = False


class LLMClient(abc.ABC):
    simulated: bool = False

    @abc.abstractmethod
    async def structured(
        self,
        *,
        task: str,
        instructions: str,
        user_input: str,
        output_model: type[T],
        prompt_version: str,
    ) -> LLMResult[T]: ...

    @abc.abstractmethod
    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


def _price(cfg: LLMConfig, model: str) -> tuple[float, ...]:
    p = cfg.prices_usd_per_mtok.get(model)
    if p is None:
        raise LLMError(f"preço não configurado para {model}: configure llm.prices_usd_per_mtok")
    return p


def compute_usd(
    cfg: LLMConfig, model: str, input_tokens: int, cached: int, output: int, cache_write: int = 0
) -> float:
    """Preços: (entrada, entrada em cache, saída[, escrita em cache]) em USD por 1M tokens."""
    p = _price(cfg, model)
    pin, pcached, pout = p[0], p[1], p[2]
    pwrite = p[3] if len(p) > 3 else pin
    plain = max(input_tokens - cached - cache_write, 0)
    total = plain * pin + cached * pcached + cache_write * pwrite + output * pout
    return total / 1_000_000


class BudgetGuard:
    def __init__(self, ledger: Ledger, budgets: Budgets) -> None:
        self.ledger = ledger
        self.budgets = budgets

    def check(self, expected_usd: float) -> None:
        run_id = current_run_id.get()
        if run_id:
            spent = self.ledger.usd_for_run(run_id)
            if spent + expected_usd > self.budgets.usd_per_run:
                raise BudgetExceeded(
                    f"limite por execução (US$ {self.budgets.usd_per_run}) atingido: {spent:.4f}"
                )
        day = self.ledger.usd_since(start_of_utc_day())
        if day + expected_usd > self.budgets.usd_per_day:
            raise BudgetExceeded(f"limite diário (US$ {self.budgets.usd_per_day}) atingido: {day:.4f}")
        month = self.ledger.usd_since(start_of_utc_month())
        if month + expected_usd > self.budgets.usd_per_month:
            raise BudgetExceeded(f"limite mensal (US$ {self.budgets.usd_per_month}) atingido: {month:.4f}")


class OpenAIResponsesClient(LLMClient):
    def __init__(
        self,
        api_key: str,
        cfg: LLMConfig,
        ledger: Ledger,
        budgets: Budgets,
        cache: LLMCache,
        *,
        sdk_client: Any | None = None,
    ) -> None:
        from openai import AsyncOpenAI

        self.cfg = cfg
        self.ledger = ledger
        self.guard = BudgetGuard(ledger, budgets)
        self.cache = cache
        self._sdk = sdk_client or AsyncOpenAI(api_key=api_key, max_retries=3, timeout=180)

    async def structured(
        self,
        *,
        task: str,
        instructions: str,
        user_input: str,
        output_model: type[T],
        prompt_version: str,
    ) -> LLMResult[T]:
        model = self.cfg.model_for(task)
        schema_version = str(getattr(output_model, "SCHEMA_VERSION", "1"))
        system = f"{SAFETY_PREAMBLE}\n\n{instructions}"
        key = cache_key(
            model=model,
            task=task,
            prompt_version=prompt_version,
            schema_version=schema_version,
            payload={"system": system, "input": user_input},
        )
        hit = self.cache.get(key)
        if hit is not None:
            return LLMResult(
                output=output_model.model_validate(hit),
                model=model,
                task=task,
                prompt_version=prompt_version,
                cached=True,
            )
        est_in = len(system + user_input) // 3 + 200
        self.guard.check(compute_usd(self.cfg, model, est_in, 0, self.cfg.max_output_tokens))
        t0 = time.perf_counter()
        ok = False
        usd = 0.0
        in_tok = cached_tok = out_tok = 0
        try:
            resp = await self._sdk.responses.parse(
                model=model,
                instructions=system,
                input=user_input,
                text_format=output_model,
                max_output_tokens=self.cfg.max_output_tokens,
                store=False,
            )
            usage = getattr(resp, "usage", None)
            in_tok = int(getattr(usage, "input_tokens", 0) or 0)
            out_tok = int(getattr(usage, "output_tokens", 0) or 0)
            details = getattr(usage, "input_tokens_details", None)
            cached_tok = int(getattr(details, "cached_tokens", 0) or 0)
            write_tok = int(getattr(details, "cache_write_tokens", 0) or 0)
            usd = compute_usd(self.cfg, model, in_tok, cached_tok, out_tok, write_tok)
            if getattr(resp, "status", "completed") == "incomplete":
                raise LLMError(f"resposta incompleta: {getattr(resp, 'incomplete_details', None)}")
            parsed = getattr(resp, "output_parsed", None)
            if parsed is None:
                raise LLMError("sem saída estruturada (recusa ou falha de schema)")
            ok = True
        finally:
            self.ledger.record(
                source_id=SOURCE_ID,
                operation=f"{task}:{model}",
                units=in_tok + out_tok,
                unit="tokens",
                usd=usd,
                run_id=current_run_id.get(),
                ok=ok,
                detail=f"in={in_tok} cached={cached_tok} out={out_tok} pv={prompt_version}",
            )
        self.cache.put(key, model=model, task=task, output=parsed.model_dump(mode="json"))
        return LLMResult(
            output=parsed,
            model=model,
            task=task,
            prompt_version=prompt_version,
            cached=False,
            input_tokens=in_tok,
            cached_tokens=cached_tok,
            output_tokens=out_tok,
            usd=usd,
            latency_ms=(time.perf_counter() - t0) * 1000,
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model = self.cfg.embedding_model
        est = sum(len(t) for t in texts) // 3
        self.guard.check(compute_usd(self.cfg, model, est, 0, 0))
        kwargs: dict[str, Any] = {"model": model, "input": list(texts)}
        if self.cfg.embedding_dimensions:
            kwargs["dimensions"] = self.cfg.embedding_dimensions
        resp = await self._sdk.embeddings.create(**kwargs)
        tokens = int(getattr(resp.usage, "total_tokens", 0) or 0)
        self.ledger.record(
            source_id=SOURCE_ID,
            operation=f"embeddings:{model}",
            units=tokens,
            unit="tokens",
            usd=compute_usd(self.cfg, model, tokens, 0, 0),
            run_id=current_run_id.get(),
        )
        return [list(d.embedding) for d in resp.data]


Handler = Callable[[str, str, type[BaseModel]], BaseModel]


class FakeLLM(LLMClient):
    """LLM determinístico para testes: `handler(task, user_input, output_model)` gera a saída.

    Resultados são marcados como simulados e nunca devem compor relatórios reais.
    """

    simulated = True

    def __init__(self, handler: Handler, embedder: Callable[[str], list[float]] | None = None) -> None:
        self.handler = handler
        self.embedder = embedder or _hash_embedding
        self.calls: list[tuple[str, str]] = []

    async def structured(
        self,
        *,
        task: str,
        instructions: str,
        user_input: str,
        output_model: type[T],
        prompt_version: str,
    ) -> LLMResult[T]:
        self.calls.append((task, user_input))
        out = self.handler(task, user_input, output_model)
        if not isinstance(out, output_model):
            raise LLMError(f"handler retornou {type(out).__name__}, esperado {output_model.__name__}")
        return LLMResult(
            output=out, model="fake", task=task, prompt_version=prompt_version, cached=False, simulated=True
        )

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self.embedder(t) for t in texts]


def _hash_embedding(text: str, dims: int = 64) -> list[float]:
    """Embedding de brinquedo baseado em trigramas (somente testes)."""
    import hashlib
    import math

    vec = [0.0] * dims
    t = f"  {text.lower()}  "
    for i in range(len(t) - 2):
        h = int(hashlib.md5(t[i : i + 3].encode()).hexdigest()[:8], 16)
        vec[h % dims] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
