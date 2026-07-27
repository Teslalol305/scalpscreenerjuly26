"""Composite scoring, tiers, and cooldowns over the per-symbol rule set.

Use: comp = Composite(cfg, symbol); events, flags = comp.evaluate(snapshot).
Depends on: signals.base, signals.rules (registry). Scoring: per side,
score = min(100, sum(weight_r * strength_r * decay_r) * ctx_mult) where a fired
rule's strength decays linearly to 0 over rule_cooldown_s. Context flags multiply
the score (oi_compression both sides; funding_extremity boosts its bias side,
dampens the other) but never produce WATCH/ALERT events themselves.
"""

from __future__ import annotations

from dataclasses import dataclass

import tapescreen.core.signals.rules  # noqa: F401  (populates the registry)
from tapescreen.config import Config
from tapescreen.core.features import FeatureSnapshot
from tapescreen.core.signals.base import (
    CONTEXT,
    LONG,
    RULE_REGISTRY,
    SHORT,
    TIER_ALERT,
    TIER_INFO,
    TIER_WATCH,
    SignalEvent,
)


@dataclass(slots=True)
class Fired:
    ts: float
    strength: float
    weight: float
    side: str


class Composite:
    """Per-symbol: runs rules, applies cooldowns, maintains decayed side scores.

    ``weight_mult(rule_name)`` lets the learning layer scale rule weights by
    measured win probability (bounded upstream); defaults to 1.0 for all rules.
    """

    def __init__(self, cfg: Config, symbol: str, weight_mult=None) -> None:
        self.cfg = cfg
        self.symbol = symbol
        self.weight_mult = weight_mult or (lambda _rule: 1.0)
        self.rules = [cls(cfg, symbol) for cls in RULE_REGISTRY.values()]
        self.last_fire: dict[str, float] = {}  # rule name -> ts (per-symbol+rule cooldown)
        self.last_alert: dict[str, float] = {LONG: -1e18, SHORT: -1e18}
        self.active: dict[str, Fired] = {}  # rule name -> latest scoring contribution
        self.ctx_active: dict[str, tuple[float, str]] = {}  # context rule -> (ts, bias)
        self.scores: dict[str, float] = {LONG: 0.0, SHORT: 0.0}

    def evaluate(self, s: FeatureSnapshot) -> tuple[list[SignalEvent], dict[str, object]]:
        """Run all rules on a snapshot; returns (loggable events, row flags)."""
        cd = self.cfg.composite
        events: list[SignalEvent] = []
        for rule in self.rules:
            ev = rule.evaluate(s)
            if ev is None:
                continue
            if rule.is_context:
                self.ctx_active[rule.name] = (s.ts, str(ev.snapshot.get("bias", "")))
                continue
            last = self.last_fire.get(rule.name, -1e18)
            if s.ts - last < cd.rule_cooldown_s:
                continue  # per (symbol, rule) cooldown
            self.last_fire[rule.name] = s.ts
            weight = float(self.cfg.rules[rule.name].get("weight", 0))
            self.active[rule.name] = Fired(s.ts, ev.strength, weight, ev.side)
            events.append(ev)

        self._rescore(s.ts)

        for ev in events:  # every directional fire is logged; tier grades confluence
            ev.score = self.scores[ev.side]
            if ev.score >= cd.alert_score:
                if s.ts - self.last_alert[ev.side] >= cd.alert_cooldown_s:
                    ev.tier = TIER_ALERT
                    self.last_alert[ev.side] = s.ts  # per (symbol, side) alert cooldown
                else:
                    ev.tier = TIER_WATCH
            elif ev.score >= cd.watch_score:
                ev.tier = TIER_WATCH
            else:
                ev.tier = TIER_INFO
        return events, self.flags(s.ts)

    def _rescore(self, now: float) -> None:
        cd = self.cfg.composite.rule_cooldown_s
        raw = {LONG: 0.0, SHORT: 0.0}
        for name in list(self.active):
            f = self.active[name]
            decay = 1.0 - (now - f.ts) / cd
            if decay <= 0.0:
                del self.active[name]
                continue
            if f.side in raw:
                raw[f.side] += f.weight * self.weight_mult(name) * f.strength * decay

        oi_mult = float(self.cfg.rules["oi_compression"].get("weight_mult", 1.0))
        f_al = float(self.cfg.rules["funding_extremity"].get("weight_mult_aligned", 1.0))
        f_ag = float(self.cfg.rules["funding_extremity"].get("weight_mult_against", 1.0))
        oi_on = self._ctx_fresh("oi_compression", now)
        bias = self._ctx_bias(now)
        for side in (LONG, SHORT):
            mult = oi_mult if oi_on else 1.0
            if bias:
                mult *= f_al if side == bias else f_ag
            raw[side] = min(100.0, raw[side] * mult)
        self.scores = raw

    def _ctx_fresh(self, name: str, now: float, ttl: float = 120.0) -> bool:
        hit = self.ctx_active.get(name)
        return bool(hit) and now - hit[0] <= ttl

    def _ctx_bias(self, now: float, ttl: float = 120.0) -> str:
        hit = self.ctx_active.get("funding_extremity")
        if hit and now - hit[0] <= ttl and hit[1] in (LONG, SHORT):
            return hit[1]
        return ""

    def flags(self, now: float) -> dict[str, object]:
        return {
            "oi_compression": self._ctx_fresh("oi_compression", now),
            "funding_bias": self._ctx_bias(now),
            "score_long": round(self.scores[LONG], 1),
            "score_short": round(self.scores[SHORT], 1),
        }


__all__ = ["Composite", "CONTEXT", "LONG", "SHORT", "SignalEvent"]
