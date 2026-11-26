"""Carregamento de configuração: YAML versionado (parâmetros) + variáveis de ambiente (segredos)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from raven.domain import Market

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Secrets(BaseSettings):
    """Segredos lidos do ambiente/.env. Nunca registrados em log (SecretStr)."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="RAVEN_", extra="ignore")

    openai_api_key: SecretStr | None = None
    youtube_api_key: SecretStr | None = None
    dataforseo_login: SecretStr | None = None
    dataforseo_password: SecretStr | None = None
    outlierkit_api_key: SecretStr | None = None
    vidiq_api_key: SecretStr | None = None
    telegram_bot_token: SecretStr | None = None
    telegram_chat_id: str | None = None
    web_user: str = "raven"
    web_password: SecretStr | None = None  # sem senha, a interface web recusa acesso

    data_dir: Path = PROJECT_ROOT / "data"
    config_dir: Path = PROJECT_ROOT / "config"


class Niche(BaseModel):
    id: str
    label: str
    active: bool = True


class ScoringConfig(BaseModel):
    weights: dict[str, float]
    min_coverage_for_point_score: float = 0.6
    evidence_max_age_days: int = 30

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> ScoringConfig:
        total = sum(self.weights.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"pesos de scoring devem somar 1.0 (soma atual: {total})")
        return self


class LLMConfig(BaseModel):
    provider: Literal["openai"] = "openai"
    routes: dict[str, str]
    embedding_model: str
    embedding_dimensions: int | None = None
    prices_usd_per_mtok: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    max_output_tokens: int = 8000

    def model_for(self, task: str) -> str:
        try:
            return self.routes[task]
        except KeyError as exc:
            raise KeyError(f"tarefa de LLM sem rota configurada: {task}") from exc


class Budgets(BaseModel):
    usd_per_run: float
    usd_per_day: float
    usd_per_month: float


class MatchingConfig(BaseModel):
    same_subject_threshold: float = 0.80
    ambiguous_lower_bound: float = 0.62
    max_candidates_per_query: int = 25
    max_llm_validations_per_market: int = 15
    queries_per_language: int = 2


class BenchmarkConfig(BaseModel):
    sample_file: str = "config/benchmark_subjects.yaml"
    results_per_query: int = 20


class AppConfig(BaseModel):
    languages: list[str]
    markets: list[Market]
    niches: list[Niche] = Field(max_length=5)
    monitoring: dict[str, int] = Field(default_factory=dict)
    schedules: dict[str, str | int] = Field(default_factory=dict)
    scoring: ScoringConfig
    llm: LLMConfig
    budgets: Budgets
    matching: MatchingConfig = MatchingConfig()
    sources: dict[str, dict[str, Any]] = Field(default_factory=dict)
    benchmark: BenchmarkConfig = BenchmarkConfig()

    def source_settings(self, source_id: str) -> dict[str, Any]:
        return dict(self.sources.get(source_id, {}))

    def market(self, code: str) -> Market:
        for m in self.markets:
            if m.code == code.upper():
                return m
        raise KeyError(f"mercado não configurado: {code}")

    def markets_for_language(self, lang: str) -> list[Market]:
        return [m for m in self.markets if lang in m.languages]


def load_yaml(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} deve conter um mapeamento YAML")
    return data


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()


def load_config(config_dir: Path | None = None) -> AppConfig:
    cfg_dir = config_dir or get_secrets().config_dir
    return AppConfig.model_validate(load_yaml(cfg_dir / "raven.yaml"))
