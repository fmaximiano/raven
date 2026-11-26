"""Montagem dos conectores a partir de configuração + segredos."""

from __future__ import annotations

from dataclasses import dataclass

from raven.connectors.base import Connector
from raven.connectors.dataforseo import DataForSEOConnector
from raven.connectors.outlierkit import OutlierKitConnector
from raven.connectors.simulated import SimulatedConnector
from raven.connectors.vidiq import VidIQConnector
from raven.connectors.youtube_data import YouTubeDataConnector
from raven.settings import AppConfig, Secrets
from raven.storage.ledger import Ledger


@dataclass(frozen=True)
class PlannedSource:
    """Fonte prevista no plano, ainda sem conector executável."""

    source_id: str
    name: str
    reason: str


PLANNED_ONLY = [
    PlannedSource(
        "google_trends_api",
        "Google Trends API (alpha)",
        "Acesso por solicitação, não concedido. DataForSEO Trends cobre o caso enquanto pendente.",
    ),
    PlannedSource(
        "youtube_analytics",
        "YouTube Analytics API",
        "Fase 4: apenas canais próprios via OAuth, após existirem vídeos publicados.",
    ),
]


def _secret(value: object) -> str | None:
    get = getattr(value, "get_secret_value", None)
    return get() if callable(get) else None


def build_connectors(
    cfg: AppConfig, secrets: Secrets, ledger: Ledger, *, simulate: bool = False
) -> dict[str, Connector]:
    """Conectores ativos. Com `simulate=True`, substitui todos por simulados (testes)."""
    if simulate:
        return {
            "sim_search": SimulatedConnector("sim_search"),
            "sim_outliers": SimulatedConnector("sim_outliers", coverage=0.5),
        }
    out: dict[str, Connector] = {}
    yt = cfg.source_settings("youtube_data")
    if yt.get("enabled", True):
        out["youtube_data"] = YouTubeDataConnector(
            _secret(secrets.youtube_api_key),
            ledger,
            search_calls_per_day=int(yt.get("search_calls_per_day", 100)),
            units_per_day=int(yt.get("units_per_day", 10_000)),
            min_interval_s=float(yt.get("min_interval_s", 0.1)),
        )
    d4 = cfg.source_settings("dataforseo")
    if d4.get("enabled", True):
        out["dataforseo"] = DataForSEOConnector(
            _secret(secrets.dataforseo_login),
            _secret(secrets.dataforseo_password),
            ledger,
            location_codes={
                m.code: m.dataforseo_location_code for m in cfg.markets if m.dataforseo_location_code
            },
            usd_per_day=float(d4.get("usd_per_day", 2.0)),
            price_usd=d4.get("price_usd"),
            min_interval_s=float(d4.get("min_interval_s", 0.3)),
        )
    ok = cfg.source_settings("outlierkit")
    if ok.get("enabled", True):
        out["outlierkit"] = OutlierKitConnector(
            _secret(secrets.outlierkit_api_key),
            ledger,
            base_url=str(ok.get("base_url", "https://outlierkit.com/api/v1")),
            credits_per_day=int(ok.get("credits_per_day", 60)),
            min_interval_s=float(ok.get("min_interval_s", 0.5)),
        )
    vq = cfg.source_settings("vidiq")
    if vq.get("enabled", True):
        out["vidiq"] = VidIQConnector(
            _secret(secrets.vidiq_api_key),
            ledger,
            mcp_url=str(vq.get("mcp_url", "https://mcp.vidiq.com/mcp")),
            tool_map=vq.get("tool_map") or {},
            credits_per_day=int(vq.get("credits_per_day", 150)),
        )
    return out


async def close_all(connectors: dict[str, Connector]) -> None:
    for c in connectors.values():
        await c.aclose()
