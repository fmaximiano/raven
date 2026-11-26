from __future__ import annotations

from pathlib import Path

import pytest

from raven.policy import PolicyRegistry
from raven.settings import PROJECT_ROOT, AppConfig, load_config
from raven.storage.ledger import Ledger
from raven.storage.runstore import RunStore

CONFIG_DIR = PROJECT_ROOT / "config"


@pytest.fixture
def cfg() -> AppConfig:
    return load_config(CONFIG_DIR)


@pytest.fixture
def policies() -> PolicyRegistry:
    return PolicyRegistry.load(CONFIG_DIR / "source_policies.yaml")


@pytest.fixture
def ledger() -> Ledger:
    return Ledger(":memory:")


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path)
