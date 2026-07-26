"""Shared fixtures: config loaded from the repo's real config.yaml (validated)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tapescreen.config import Config, SymbolEntry, load_config

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def cfg() -> Config:
    return load_config(REPO / "config.yaml")


@pytest.fixture()
def two_symbols() -> dict[str, SymbolEntry]:
    return {
        "BTC": SymbolEntry("BTC", "hyperliquid", "BTC", "verified_research"),
        "ETH": SymbolEntry("ETH", "hyperliquid", "ETH", "verified_research"),
    }
