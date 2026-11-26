"""Cliente HTTP comum: timeouts, retry com backoff exponencial, intervalo mínimo e redação."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from raven.connectors.base import AuthError, ConnectorError, QuotaExceeded, RateLimited, UpstreamError

log = logging.getLogger(__name__)

_SECRET_PARAMS = re.compile(r"(key|token|password|secret|authorization)=([^&\s]+)", re.I)


def redact(text: str) -> str:
    return _SECRET_PARAMS.sub(r"\1=***", text)


class MinIntervalLimiter:
    """Garante intervalo mínimo entre chamadas e concorrência máxima por fonte."""

    def __init__(self, min_interval_s: float = 0.0, max_concurrency: int = 2) -> None:
        self.min_interval_s = min_interval_s
        self._sem = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self) -> None:
        await self._sem.acquire()
        async with self._lock:
            wait = self._last + self.min_interval_s - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = time.monotonic()

    async def __aexit__(self, *exc: object) -> None:
        self._sem.release()


ErrorMapper = Callable[[httpx.Response], ConnectorError | None]


class HttpTransport:
    def __init__(
        self,
        source_id: str,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        auth: httpx.Auth | tuple[str, str] | None = None,
        timeout_s: float = 30.0,
        max_attempts: int = 4,
        limiter: MinIntervalLimiter | None = None,
        error_mapper: ErrorMapper | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.source_id = source_id
        self.max_attempts = max_attempts
        self.limiter = limiter or MinIntervalLimiter()
        self.error_mapper = error_mapper
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers=dict(headers or {}),
            auth=auth,
            timeout=httpx.Timeout(timeout_s),
        )

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        def _retryable(exc: BaseException) -> bool:
            return isinstance(exc, ConnectorError) and exc.retryable

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.max_attempts),
            wait=wait_exponential_jitter(initial=1, max=30),
            retry=retry_if_exception(_retryable),
            reraise=True,
        ):
            with attempt:
                async with self.limiter:
                    try:
                        resp = await self._client.request(method, url, **kwargs)
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        raise UpstreamError(self.source_id, f"falha de transporte: {exc!r}") from exc
                self._raise_for(resp)
                return resp
        raise AssertionError("inalcançável")  # pragma: no cover

    def _raise_for(self, resp: httpx.Response) -> None:
        if self.error_mapper:
            err = self.error_mapper(resp)
            if err is not None:
                raise err
        if resp.is_success:
            return
        snippet = redact(resp.text[:300])
        code = resp.status_code
        if code in (401, 403):
            raise AuthError(self.source_id, f"HTTP {code}: {snippet}")
        if code == 402:
            raise QuotaExceeded(self.source_id, f"HTTP 402 (créditos insuficientes): {snippet}")
        if code == 429:
            raise RateLimited(self.source_id, f"HTTP 429: {snippet}")
        if code >= 500:
            raise UpstreamError(self.source_id, f"HTTP {code}: {snippet}")
        raise ConnectorError(self.source_id, f"HTTP {code}: {snippet}")

    async def aclose(self) -> None:
        await self._client.aclose()
