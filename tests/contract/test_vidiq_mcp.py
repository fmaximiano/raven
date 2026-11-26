"""Conector do vidIQ contra um servidor MCP REAL (SDK v2) rodando localmente via HTTP.

Não reproduz o catálogo do vidIQ (não documentado); valida o caminho completo do cliente:
autenticação por cabeçalho, initialize, tools/list com paginação, tools/call e leitura do
resultado com os nomes de campos do SDK v2. Foi um campo da v1 (`inputSchema`) que derrubou a
página de fontes em produção.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from raven.connectors.base import VideoSearchRequest
from raven.connectors.vidiq import VidIQConnector

GOOD_KEY = "chave-valida"


def _server_app():  # type: ignore[no-untyped-def]
    mcp = MCPServer("vidiq-falso")

    @mcp.tool()
    def outliers(query: str, limit: int = 10) -> dict:
        """Vídeos com desempenho excepcional."""
        return {
            "videos": [
                {"videoId": "abcdefghijk", "title": f"{query} explicado", "views": 1000, "outlierScore": 7.5},
                {"videoId": "bcdefghijkl", "title": f"{query} em 10 minutos", "views": 500},
            ][:limit]
        }

    @mcp.tool()
    def credit_balance() -> dict:
        """Saldo de créditos."""
        return {"credits": 150}

    app = mcp.streamable_http_app()

    async def auth(request, call_next):  # type: ignore[no-untyped-def]
        if request.headers.get("authorization") != f"Bearer {GOOD_KEY}":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=auth)
    return app


@pytest.fixture(scope="module")
def mcp_url() -> Iterator[str]:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_server_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "servidor MCP local não subiu"
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


async def test_check_lists_tools_with_v2_fields(mcp_url, ledger):
    conn = VidIQConnector(GOOD_KEY, ledger, mcp_url=mcp_url)
    report = await conn.check(live=True)
    assert report.authenticated is True, report.detail
    assert set(report.observed["tools"]) == {"outliers", "credit_balance"}
    tools = await conn.list_tools()
    by_name = {t["name"]: t for t in tools}
    assert by_name["outliers"]["input_schema"]["properties"]["query"]["type"] == "string"
    assert by_name["outliers"]["description"] == "Vídeos com desempenho excepcional."


async def test_wrong_key_is_reported_clearly(mcp_url, ledger):
    report = await VidIQConnector("errada", ledger, mcp_url=mcp_url).check(live=True)
    assert report.authenticated is False
    assert "TaskGroup" not in report.detail  # a causa real, não o invólucro do anyio
    assert "401" in report.detail or "nauthorized" in report.detail


async def test_search_via_tool_map(mcp_url, ledger):
    conn = VidIQConnector(
        GOOD_KEY,
        ledger,
        mcp_url=mcp_url,
        tool_map={"outlier_search": {"tool": "outliers", "args": {"query": "{query}", "limit": "{limit}"}}},
    )
    res = await conn.search_outliers(VideoSearchRequest(query="Hashima", language="ja", max_results=5))
    assert [v.platform_id for v in res.videos] == ["abcdefghijk", "bcdefghijkl"]
    assert res.videos[0].vendor_metrics == {"outlierScore": 7.5}
    assert res.ignored_filters == ["language"]  # a ferramenta não recebeu o filtro de idioma
    assert res.cost.units == 5
