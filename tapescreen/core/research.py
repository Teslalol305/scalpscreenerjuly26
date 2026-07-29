"""ResearchDesk: a standing panel of quant roles that studies every outcome.

Use: desk = ResearchDesk(cfg, ledger, thoughts, db); desk.meeting(now) on a
timer; ledger consults desk.gate(rule, conf) before opening trades.
Depends on: signals.ledger (buckets/models), store.db (history + persistence),
core.thoughts (narration). The desk exists to push the system toward its goal -
issue signals only where a measurable edge exists - and it does so with four
roles, each deliberately bounded so the desk can never spiral or stall:

- performance analyst: per-strategy expectancy from the measured record
- feature scout: correlates CANDIDATE variables (data the system already sees
  but does not model) against outcomes; a candidate that proves predictive is
  PROMOTED into the models as a new tracked variable, and retired again if the
  evidence decays - the system grows its own feature set from evidence
- risk officer: puts strategies with a weak measured record on probation
  (their fires must clear a higher confidence bar to open trades)
- loop-breaker: dedups repeated conclusions, and guarantees exploration - a
  fixed fraction of probation-suppressed fires still opens, so a gated
  strategy keeps generating the very outcomes that could clear it. Gating can
  therefore never starve itself of data: no redundant loops, no dead ends.

Every action is narrated to the reasoning trace with the numbers behind it.
Deterministic by design: counters, not randomness.
"""

from __future__ import annotations

import json
import logging
import math
import time as _time
from typing import Any

from tapescreen.config import Config
from tapescreen.core.features import FeatureSnapshot
from tapescreen.core.signals.base import LONG, SignalEvent
from tapescreen.core.signals.models import FEATURES as FEATURES_BASE
from tapescreen.core.thoughts import CAT_DESK, ThoughtLog

log = logging.getLogger("tapescreen.research")

# Candidate variables: computable from data the system already ingests, but
# NOT part of the base model vector. Directional ones are side-signed so
# "edge toward the trade" is positive, like the base features.
CANDIDATES = [
    "hour_sin",       # time of day (UTC), cyclic
    "hour_cos",
    "dow",            # day of week, 0=Mon .. 1=Sun
    "funding_pctl",   # funding's 7-day percentile, centered (-1..1) - crowding regime
    "bb_squeeze",     # Bollinger-width compression (-1 wide .. +1 tight)
    "doi_norm",       # 5m OI change vs its session p95 (position flow regime)
    "imb_dur",        # how long the book has been pinned one-sided, minutes
    "vol_freshness",  # vol_z vs its 5m peak - is the burst fresh or fading
    "roc15_edge",     # 15m momentum toward the trade side
    "basis_edge",     # mark-mid premium toward the trade side, bps
    "streak",         # the firing rule's recent win streak (-1..1)
]


def _clamp(v: float, lo: float, hi: float) -> float:
    if not math.isfinite(v):
        return 0.0
    return lo if v < lo else hi if v > hi else v


def compute_candidates(ev: SignalEvent, s: FeatureSnapshot,
                       streak: float) -> dict[str, float]:
    """One value per CANDIDATES entry, all finite and bounded."""
    sign = 1.0 if ev.side == LONG else -1.0
    frac = (ev.ts % 86400.0) / 86400.0  # UTC day fraction
    dow = (int(ev.ts // 86400.0) + 3) % 7  # epoch day 0 was a Thursday
    return {
        "hour_sin": math.sin(2 * math.pi * frac),
        "hour_cos": math.cos(2 * math.pi * frac),
        "dow": dow / 6.0,
        "funding_pctl": _clamp((s.funding_pctl_7d - 50.0) / 50.0, -1.0, 1.0),
        "bb_squeeze": _clamp((50.0 - s.bb_width_pctl) / 50.0, -1.0, 1.0),
        "doi_norm": _clamp(s.doi_5m / s.doi5_session_p95, -2.0, 2.0)
        if s.doi5_session_p95 > 0 else 0.0,
        "imb_dur": _clamp(max(s.imb_high_dur_s, s.imb_low_dur_s) / 60.0, 0.0, 5.0),
        "vol_freshness": _clamp(s.vol_z / s.vol_z_5m_peak, -2.0, 2.0)
        if s.vol_z_5m_peak > 0 else 0.0,
        "roc15_edge": _clamp(sign * math.tanh(s.roc_15m * 100.0), -1.0, 1.0),
        "basis_edge": _clamp(sign * s.basis_bps, -50.0, 50.0),
        "streak": _clamp(streak, -1.0, 1.0),
    }


def point_biserial(xs: list[float], wins: list[bool]) -> float:
    """Correlation between a numeric variable and the win/loss outcome.
    r = (mean_win - mean_loss) / std * sqrt(p * q); 0 when degenerate."""
    n = len(xs)
    if n < 2 or len(wins) != n:
        return 0.0
    n1 = sum(1 for w in wins if w)
    n0 = n - n1
    if n1 == 0 or n0 == 0:
        return 0.0
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / n
    if var <= 1e-18:
        return 0.0
    m1 = sum(x for x, w in zip(xs, wins, strict=True) if w) / n1
    m0 = sum(x for x, w in zip(xs, wins, strict=True) if not w) / n0
    return (m1 - m0) / math.sqrt(var) * math.sqrt((n1 / n) * (n0 / n))


class ResearchDesk:
    """Bounded, deterministic 'quant panel' driving evidence-based changes."""

    def __init__(self, cfg: Config, ledger: Any, thoughts: ThoughtLog,
                 db: Any = None) -> None:
        self.cfg = cfg
        self.rc = cfg.research
        self.ledger = ledger
        self.th = thoughts
        self.db = db
        self.active_extras: list[str] = []       # promoted variable names, ordered
        self.extra_scores: dict[str, dict] = {}  # name -> {r, n, ts} at promotion
        self.probation: dict[str, dict] = {}     # rule -> {since, conf_min, n_at_entry}
        self.retired: dict[str, float] = {}      # name -> retire ts (cooldown)
        self.suppressed: dict[str, int] = {}     # rule -> probation-suppressed fires
        self.explored: dict[str, int] = {}       # rule -> exploration trades opened
        self.exploration_every: dict[str, int] = {}
        self.meetings_total = 0
        self.meetings_since_change = 0
        self.candidate_scores: dict[str, dict] = {}  # last measured {r, n} per name
        self.findings: list[str] = []            # latest meeting summary for the UI
        self.next_focus = ""
        self.last_meeting_ts = 0.0
        self._resolved_seen = 0
        self._narrated: dict[str, str] = {}      # dedup: action key -> signature
        if db is not None:
            self._load_state()
        if self.active_extras:
            self.ledger.rebuild_models(self.active_extras)
            self.th.emit(_time.time(), CAT_DESK, "",
                         f"restored {len(self.active_extras)} promoted variable(s) "
                         f"from prior sessions: {', '.join(self.active_extras)}",
                         ["models rebuilt with the extended vector before going live"])
        self.ledger.research = self

    # ------------------------------------------------------------ persistence

    def _load_state(self) -> None:
        try:
            raw = self.db.research_get("desk")
            if not raw:
                return
            st = json.loads(raw)
            self.active_extras = [n for n in st.get("active_extras", []) if n in CANDIDATES]
            self.extra_scores = {n: v for n, v in st.get("extra_scores", {}).items()
                                 if n in self.active_extras}
            self.retired = {n: float(t) for n, t in st.get("retired", {}).items()
                            if n in CANDIDATES}
            self.probation = {r: v for r, v in st.get("probation", {}).items()}
        except Exception:
            log.exception("could not restore research state; starting fresh")

    def _save_state(self) -> None:
        if self.db is None:
            return
        self.db.research_set("desk", json.dumps({
            "active_extras": self.active_extras,
            "extra_scores": self.extra_scores,
            "retired": self.retired,
            "probation": self.probation,
        }))

    # ------------------------------------------------------------------ gate

    def gate(self, rule: str, conf: float) -> tuple[bool, bool, str]:
        """(allow, is_exploration, reason). Called by the ledger per fire."""
        p = self.probation.get(rule)
        if p is None or conf >= p["conf_min"]:
            return True, False, ""
        c = self.suppressed.get(rule, 0) + 1
        self.suppressed[rule] = c
        every = max(2, int(self.exploration_every.get(rule, self.rc.exploration_every)))
        if c % every == 0:
            self.explored[rule] = self.explored.get(rule, 0) + 1
            return True, True, (
                f"exploration trade: {rule} is on probation but 1 in {every} "
                "suppressed fires still opens - gated strategies must keep "
                "generating the outcomes that could clear them")
        return False, False, (
            f"probation gate: {rule} win probability {conf * 100:.0f}% is below the "
            f"{p['conf_min'] * 100:.0f}% bar its weak record requires "
            f"({c % every}/{every} until the next exploration trade)")

    # --------------------------------------------------------------- meeting

    def meeting(self, now: float | None = None) -> dict[str, Any]:
        """One desk cycle. Cheap when nothing new resolved; never raises."""
        now = now if now is not None else _time.time()
        try:
            return self._meeting(now)
        except Exception:
            log.exception("research meeting failed (skipped this cycle)")
            return self.snapshot()

    def _meeting(self, now: float) -> dict[str, Any]:
        led = self.ledger
        resolved_total = int(sum(b.n for b in led.rule_buckets.values()))
        new = resolved_total - self._resolved_seen
        self.meetings_total += 1
        self.last_meeting_ts = now
        if new <= 0 and self.meetings_total > 1:
            # nothing new to study: no narration, no state churn (anti-loop rule 1)
            return self.snapshot()
        self._resolved_seen = resolved_total

        findings: list[str] = []
        changed = False

        # ---- performance analyst: expectancy table from the measured record
        stats = []
        for rule, b in sorted(led.rule_buckets.items()):
            if b.n < self.rc.min_rule_n:
                continue
            post = led._posterior(b)
            avg_r = b.r_sum / b.r_n if b.r_n else None  # None: only legacy outcomes
            stats.append((rule, post, avg_r, int(b.n)))
            findings.append(
                f"{rule}: {b.wins:.0f}W-{b.n - b.wins:.0f}L, posterior {post * 100:.0f}%"
                + (f", avg {avg_r:+.2f}R" if avg_r is not None else ""))
        ranked_r = [s for s in stats if s[2] is not None]
        if len(ranked_r) >= 2:
            best = max(ranked_r, key=lambda s: s[2])
            worst = min(ranked_r, key=lambda s: s[2])
            if best[0] != worst[0]:
                findings.append(f"edge is concentrated in {best[0]} "
                                f"({best[2]:+.2f}R avg); {worst[0]} is the drag "
                                f"({worst[2]:+.2f}R avg)")

        # ---- risk officer: probation transitions off the posterior record
        for rule, b in led.rule_buckets.items():
            post = led._posterior(b)
            on = rule in self.probation
            if not on and b.n >= self.rc.probation_min_n and post < self.rc.probation_enter:
                self.probation[rule] = {"since": now,
                                        "conf_min": self.rc.probation_conf_min,
                                        "n_at_entry": b.n}
                changed = True
                self.th.emit(now, CAT_DESK, "",
                             f"risk officer: {rule} goes ON PROBATION", [
                    f"posterior win rate {post * 100:.0f}% over {b.n:.0f} trades is "
                    f"below the {self.rc.probation_enter * 100:.0f}% floor",
                    f"its fires now need >= {self.rc.probation_conf_min * 100:.0f}% "
                    "modeled win probability to open a trade",
                    "exploration keeps 1 in "
                    f"{self.rc.exploration_every} suppressed fires alive so the "
                    "strategy can still earn its way back",
                ])
            elif on and post >= self.rc.probation_exit:
                since_n = b.n - self.probation[rule].get("n_at_entry", 0)
                del self.probation[rule]
                self.exploration_every.pop(rule, None)
                changed = True
                self.th.emit(now, CAT_DESK, "",
                             f"risk officer: {rule} probation LIFTED", [
                    f"posterior recovered to {post * 100:.0f}% "
                    f"(+{since_n:.0f} trades while gated)",
                    "fires open normally again",
                ])

        # ---- feature scout: score candidates vs outcomes; promote/retire
        self._scout(now, findings)

        # ---- loop-breaker: stagnation watch + guaranteed data flow
        if changed:
            self.meetings_since_change = 0
        else:
            self.meetings_since_change += 1
        if self.meetings_since_change >= self.rc.stagnant_meetings and self.probation:
            for rule, p in self.probation.items():
                b = led.rule_buckets.get(rule)
                grown = (b.n - p.get("n_at_entry", 0)) if b else 0
                cur = self.exploration_every.get(rule, self.rc.exploration_every)
                if grown < 3 and cur > 2:
                    self.exploration_every[rule] = max(2, cur // 2)
                    self.meetings_since_change = 0
                    self.th.emit(now, CAT_DESK, "",
                                 f"loop-breaker: {rule} is data-starved under probation", [
                        f"only {grown:.0f} new outcomes since gating; exploration "
                        f"rate raised to 1 in {self.exploration_every[rule]} "
                        "so evidence keeps flowing",
                        "a gate that stops all data would be a dead end, not learning",
                    ])

        # ---- next focus: thinnest evidence gets the desk's attention
        if led.rule_buckets:
            thin = min(led.rule_buckets.items(), key=lambda kv: kv[1].n)
            self.next_focus = (f"gathering evidence on {thin[0]} "
                               f"(only {thin[1].n:.0f} resolved trades)")

        self.findings = findings[:10]
        if changed:
            self._save_state()
        # analyst summary: only when there was genuinely new material
        sig = f"{resolved_total}:{len(self.probation)}:{len(self.active_extras)}"
        if findings and self._narrated.get("analyst") != sig:
            self._narrated["analyst"] = sig
            self.th.emit(now, CAT_DESK, "",
                         f"desk meeting #{self.meetings_total}: "
                         f"{new} new outcome(s) studied", findings[:6])
        return self.snapshot()

    def _scout(self, now: float, findings: list[str]) -> None:
        if self.db is None or not self.cfg.learning.model_enabled:
            return
        try:
            rows = self.db.resolved_feature_history()
        except Exception:
            log.exception("scout could not load history")
            return
        samples: dict[str, list[float]] = {n: [] for n in CANDIDATES}
        wins: list[bool] = []
        for r in rows:
            raw = r.get("candidates")
            if not raw:
                continue
            try:
                cand = json.loads(raw)
            except (ValueError, TypeError):
                continue
            wins.append(r["status"] == "win")
            for n in CANDIDATES:
                samples[n].append(float(cand.get(n, 0.0)))
        n_hist = len(wins)
        if n_hist < self.rc.promote_min_n:
            return
        scored = {n: point_biserial(xs, wins) for n, xs in samples.items()}
        self.candidate_scores = {n: {"r": round(r, 3), "n": n_hist}
                                 for n, r in scored.items()}

        # retire first (frees a slot): active extra whose evidence has decayed
        for name in list(self.active_extras):
            r = scored.get(name, 0.0)
            if abs(r) < self.rc.demote_r and n_hist >= self.rc.promote_min_n * 2:
                self.active_extras.remove(name)
                self.extra_scores.pop(name, None)
                self.retired[name] = now
                self.ledger.rebuild_models(self.active_extras)
                self._save_state()
                self.meetings_since_change = 0
                self.th.emit(now, CAT_DESK, "",
                             f"feature scout: RETIRED tracked variable '{name}'", [
                    f"its win correlation decayed to {r:+.3f} over {n_hist} trades "
                    f"(floor {self.rc.demote_r})",
                    "models rebuilt without it - dead variables are noise, "
                    "and noise costs accuracy",
                ])
                break  # at most one structural change per role per meeting

        # promote: strongest candidate that clears the bar
        cooldown = self.rc.retire_cooldown_h * 3600.0
        ranked = sorted(((abs(r), n, r) for n, r in scored.items()), reverse=True)
        for _, name, r in ranked:
            if (name in self.active_extras
                    or now - self.retired.get(name, -1e18) < cooldown
                    or len(self.active_extras) >= self.rc.max_extras):
                continue
            if abs(r) < self.rc.promote_r:
                break  # ranked: nothing further can qualify
            self.active_extras.append(name)
            self.extra_scores[name] = {"r": round(r, 3), "n": n_hist, "ts": now}
            self.ledger.rebuild_models(self.active_extras)
            self._save_state()
            self.meetings_since_change = 0
            self.th.emit(now, CAT_DESK, "",
                         f"feature scout: PROMOTED '{name}' to tracked variable "
                         f"#{len(FEATURES_BASE) + len(self.active_extras)}", [
                f"win correlation {r:+.3f} across {n_hist} resolved trades "
                f"(bar: |r| >= {self.rc.promote_r}, n >= {self.rc.promote_min_n})",
                "the models now see this variable on every new signal and were "
                "refit on full history so past trades teach it too",
                f"tracked variables: {len(FEATURES_BASE)} base + "
                f"{len(self.active_extras)} discovered",
            ])
            findings.append(f"new variable tracked: {name} (r {r:+.3f})")
            break  # one promotion per meeting: deliberate, reviewable steps

    # -------------------------------------------------------------- snapshot

    def snapshot(self) -> dict[str, Any]:
        return {
            "meetings": self.meetings_total,
            "last_ts": self.last_meeting_ts,
            "findings": self.findings,
            "next_focus": self.next_focus,
            "active_extras": [
                {"name": n, **self.extra_scores.get(n, {}),
                 "live_r": self.candidate_scores.get(n, {}).get("r")}
                for n in self.active_extras
            ],
            "candidates": self.candidate_scores,
            "probation": {
                r: {"conf_min": p["conf_min"], "since": p["since"],
                    "suppressed": self.suppressed.get(r, 0),
                    "explored": self.explored.get(r, 0),
                    "every": self.exploration_every.get(r, self.rc.exploration_every)}
                for r, p in self.probation.items()
            },
            "base_features": len(FEATURES_BASE),
        }


