"""SignalRule interface + registry, and the SignalEvent envelope.

Use: subclass SignalRule with @register; Composite instantiates one per symbol.
Depends on: core.features.FeatureSnapshot. Rules are pure functions of the
snapshot plus their own small per-symbol state; they never do I/O.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from tapescreen.config import Config
from tapescreen.core.features import FeatureSnapshot

LONG = "long"
SHORT = "short"
CONTEXT = ""  # context rules carry no side

TIER_INFO = "INFO"  # rule fired but composite confluence below WATCH; logged for stats
TIER_WATCH = "WATCH"
TIER_ALERT = "ALERT"


@dataclass(slots=True)
class SignalEvent:
    ts: float
    symbol: str
    side: str  # LONG | SHORT | "" (context)
    rule: str
    strength: float  # 0..1
    tier: str = ""  # assigned by the composite scorer
    score: float = 0.0  # composite score for (symbol, side) at fire time
    snapshot: dict[str, Any] = field(default_factory=dict)


class SignalRule(ABC):
    """One rule instance per (symbol, rule); may keep internal state across snapshots."""

    name: ClassVar[str] = ""
    is_context: ClassVar[bool] = False  # context rules modify scores, never alert

    def __init__(self, cfg: Config, symbol: str) -> None:
        self.cfg = cfg
        self.symbol = symbol
        self.p: dict[str, Any] = cfg.rules[self.name]

    @abstractmethod
    def evaluate(self, s: FeatureSnapshot) -> SignalEvent | None:
        """Return an event when the rule fires on this snapshot, else None."""

    def base_snapshot(self, s: FeatureSnapshot, invalidation: float = 0.0) -> dict[str, Any]:
        """Context-only capture written with every signal (not advice)."""
        return {
            "price": s.price,
            "vwap_dist_sigma": round(s.vwap_dist_sigma, 3),
            "vol_z": round(s.vol_z, 3),
            "cvd_1m": round(s.cvd_1m, 4),
            "cvd_5m": round(s.cvd_5m, 4),
            "book_imbalance": round(s.book_imbalance, 4),
            "spread_bps": round(s.spread_bps, 3),
            "doi_5m": round(s.doi_5m, 4),
            "funding": s.funding,
            "invalidation": invalidation,
        }


RULE_REGISTRY: dict[str, type[SignalRule]] = {}


def register(cls: type[SignalRule]) -> type[SignalRule]:
    if not cls.name:
        raise ValueError(f"rule class {cls.__name__} must set a name")
    if cls.name in RULE_REGISTRY:
        raise ValueError(f"duplicate rule name: {cls.name}")
    RULE_REGISTRY[cls.name] = cls
    return cls
