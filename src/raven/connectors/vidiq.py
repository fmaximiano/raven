"""vidIQ via MCP remoto (https://mcp.vidiq.com/mcp).

Estado: SOMENTE INTERFACE. Fatos documentados (support.vidiq.com, artigo 15082430):
- Único acesso programático descrito é MCP; não há REST documentado.
- Autenticação por OAuth ou chave somente-leitura (Account Settings > MCP). O cabeçalho da chave
  NÃO é documentado: assume-se `Authorization: Bearer` e o teste autenticado confirma ou refuta.
- Pesquisa (keyword_research, outliers, trending_videos, stats) custa 5 créditos por chamada.
- Parâmetros das ferramentas, filtros de idioma/país e formato de resposta não são documentados.

Por isso, o mapeamento capacidade -> ferramenta fica em `config/raven.yaml` (sources.vidiq.tool_map),
preenchido após `raven sources tools vidiq` (tools/list não consome créditos).
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any

from raven.connectors.base import (
    AuthError,
    CallCost,
    Capability,
    CapabilityNotSupported,
    Connector,
    ConnectorError,
    ConnectorInfo,
    ConnectorStatus,
    HealthReport,
    NotConfigured,
    QuotaExceeded,
    VideoSearchRequest,
    VideoSearchResult,
)
from raven.context import current_run_id
from raven.domain import Provenance, VideoRef
from raven.storage.ledger import Ledger, start_of_utc_day

SOURCE_ID = "vidiq"
DEFAULT_MCP_URL = "https://mcp.vidiq.com/mcp"
RESEARCH_CREDITS = 5


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    """Lê o primeiro atributo existente (SDK MCP v2 usa snake_case; v1 usava camelCase)."""
    for n in names:
        value = getattr(obj, n, None)
        if value is not None:
            return value
    return default


def root_cause(exc: BaseException) -> str:
    """Desembrulha ExceptionGroup (anyio) até a causa real, para mensagens úteis."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"


def _walk_dicts(obj: Any) -> Iterator[dict[str, Any]]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v)


def extract_videos(payload: Any) -> list[dict[str, Any]]:
    """Normalização tolerante: procura objetos com id de vídeo e título em qualquer nível."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in _walk_dicts(payload):
        vid = d.get("videoId") or d.get("video_id") or d.get("youtubeId")
        if not vid and isinstance(d.get("id"), str) and len(d["id"]) == 11:
            vid = d["id"]
        title = d.get("title") or d.get("videoTitle")
        if isinstance(vid, str) and isinstance(title, str) and vid not in seen:
            seen.add(vid)
            found.append({**d, "_video_id": vid, "_title": title})
    return found


class VidIQConnector(Connector):
    info = ConnectorInfo(
        source_id=SOURCE_ID,
        name="vidIQ (MCP)",
        connector_version="0.1.0",
        api_version="mcp",
        status=ConnectorStatus.INTERFACE_ONLY,
        capabilities=[Capability.OUTLIER_SEARCH, Capability.VIDEO_SEARCH],
        supports_language_filter=False,  # desconhecido até inspeção de tools/list
        supports_region_filter=False,
        data_classes=["outliers", "keywords"],
        cost_unit="credits",
        docs=["https://vidiq.com/mcp/", "https://support.vidiq.com/en/articles/15082430-vidiq-mcp"],
        limitations=[
            "Somente MCP; uso headless com chave não documentado",
            "Nomes/parâmetros das ferramentas exigem descoberta via tools/list",
            "5 créditos por chamada de pesquisa; créditos compartilhados com o app",
            "Plano Free: 150 créditos/mês (~30 pesquisas); MAX: 6.000",
        ],
    )

    def __init__(
        self,
        api_key: str | None,
        ledger: Ledger,
        *,
        mcp_url: str = DEFAULT_MCP_URL,
        tool_map: dict[str, dict[str, Any]] | None = None,
        credits_per_day: int = 150,
        usd_per_credit: float = 49 / 6000,  # MAX mensal (estimativa contábil)
    ) -> None:
        self._key = api_key
        self.ledger = ledger
        self.mcp_url = mcp_url
        # Ex.: {"outlier_search": {"tool": "outliers", "args": {"query": "{query}", "limit": "{limit}"}}}
        self.tool_map = tool_map or {}
        self.credits_per_day = credits_per_day
        self.usd_per_credit = usd_per_credit

    async def _session_call(
        self, method: str, name: str | None = None, args: dict[str, Any] | None = None
    ) -> Any:
        if not self._key:
            raise NotConfigured(SOURCE_ID, "RAVEN_VIDIQ_API_KEY ausente")
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client
        from mcp.shared._httpx_utils import create_mcp_http_client

        client = create_mcp_http_client(headers={"Authorization": f"Bearer {self._key}"})
        http_errors: list[int] = []

        async def _record_status(response: Any) -> None:
            if response.status_code >= 400:
                http_errors.append(response.status_code)

        client.event_hooks["response"].append(_record_status)  # o SDK descarta o status HTTP
        try:
            async with (
                client,
                streamable_http_client(self.mcp_url, http_client=client) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                tools = await self._all_tools(session)  # também habilita cabeçalhos x-mcp-header
                if method == "list_tools":
                    return tools
                return await session.call_tool(name or "", args or {})
        except ConnectorError:
            raise
        except Exception as exc:
            text = root_cause(exc)
            if http_errors:
                text = f"HTTP {http_errors[-1]} do servidor MCP ({text})"
            if any(code in (401, 403) for code in http_errors) or any(
                k in text for k in ("nauthorized", "orbidden")
            ):
                raise AuthError(
                    SOURCE_ID, f"{text[:300]} — chave recusada ou formato de cabeçalho diferente"
                ) from exc
            raise ConnectorError(SOURCE_ID, f"falha MCP: {text[:300]}") from exc

    @staticmethod
    async def _all_tools(session: Any, max_pages: int = 20) -> list[Any]:
        from mcp_types import PaginatedRequestParams

        tools: list[Any] = []
        cursor: str | None = None
        for _ in range(max_pages):
            params = PaginatedRequestParams(cursor=cursor) if cursor else None
            page = await session.list_tools(params=params)
            tools.extend(page.tools)
            cursor = _field(page, "next_cursor", "nextCursor")
            if not cursor:
                break
        return tools

    async def list_tools(self) -> list[dict[str, Any]]:
        tools = await self._session_call("list_tools")
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": _field(t, "input_schema", "inputSchema"),
                "output_schema": _field(t, "output_schema", "outputSchema"),
            }
            for t in tools
        ]

    async def check(self, live: bool = False) -> HealthReport:
        if not self._key:
            return HealthReport(source_id=SOURCE_ID, configured=False, detail="chave ausente")
        if not live:
            return HealthReport(
                source_id=SOURCE_ID, configured=True, detail="tool_map vazio" if not self.tool_map else ""
            )
        try:
            tools = await self.list_tools()
        except ConnectorError as exc:
            return HealthReport(source_id=SOURCE_ID, configured=True, authenticated=False, detail=str(exc))
        return HealthReport(
            source_id=SOURCE_ID,
            configured=True,
            authenticated=True,
            detail="tools/list respondeu (sem custo)",
            observed={"tools": [t["name"] for t in tools]},
        )

    async def search_outliers(self, req: VideoSearchRequest) -> VideoSearchResult:
        spec = self.tool_map.get(Capability.OUTLIER_SEARCH.value)
        if not spec:
            raise CapabilityNotSupported(
                SOURCE_ID, "tool_map.outlier_search não configurado (rode `raven sources tools vidiq`)"
            )
        used = self.ledger.units_since(SOURCE_ID, start_of_utc_day())
        if used + RESEARCH_CREDITS > self.credits_per_day:
            raise QuotaExceeded(SOURCE_ID, f"limite diário local de créditos ({used})")
        values = {
            "query": req.query,
            "limit": req.max_results,
            "language": req.language or "",
            "region": req.region or "",
        }
        args: dict[str, Any] = {}
        for k, template in (spec.get("args") or {}).items():
            if isinstance(template, str) and template.startswith("{") and template.endswith("}"):
                v = values.get(template[1:-1])
                if v not in ("", None):
                    args[k] = v
            else:
                args[k] = template
        t0 = time.perf_counter()
        ok = False
        try:
            res = await self._session_call("call_tool", spec["tool"], args)
            ok = not _field(res, "is_error", "isError", default=False)
        finally:
            self.ledger.record(
                source_id=SOURCE_ID,
                operation=f"mcp.{spec['tool']}",
                units=RESEARCH_CREDITS,  # custo documentado; saldo real conferir via ferramenta de créditos
                unit="credits",
                usd=RESEARCH_CREDITS * self.usd_per_credit,
                run_id=current_run_id.get(),
                ok=ok,
            )
        if not ok:
            raise ConnectorError(SOURCE_ID, f"ferramenta retornou erro: {str(res)[:300]}")
        payload: Any = _field(res, "structured_content", "structuredContent")
        if payload is None:
            texts = [getattr(c, "text", "") for c in getattr(res, "content", [])]
            try:
                payload = json.loads("".join(texts))
            except ValueError:
                payload = {}
        videos = [
            VideoRef(
                platform_id=d["_video_id"],
                title=d["_title"],
                channel_title=d.get("channelTitle") or d.get("channel_name"),
                statistics={"view_count": d.get("views") or d.get("viewCount")},
                vendor_metrics={k: v for k, v in d.items() if "outlier" in k.lower() or "score" in k.lower()},
                provenance=Provenance(
                    source_id=SOURCE_ID, data_class="outliers", endpoint=f"mcp:{spec['tool']}"
                ),
            )
            for d in extract_videos(payload)
        ][: req.max_results]
        applied = set(args)
        ignored = [f for f in ("language", "region") if values[f] and f not in applied]
        return VideoSearchResult(
            request=req,
            videos=videos,
            cost=CallCost(units=RESEARCH_CREDITS, unit="credits", usd=RESEARCH_CREDITS * self.usd_per_credit),
            latency_ms=(time.perf_counter() - t0) * 1000,
            applied_filters=args,
            ignored_filters=ignored,
        )

    async def search_videos(self, req: VideoSearchRequest) -> VideoSearchResult:
        return await self.search_outliers(req)
