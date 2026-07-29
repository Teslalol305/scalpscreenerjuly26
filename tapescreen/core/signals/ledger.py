"""Trade-signal ledger: laddered entries, BE/trailing management, learned confidence.

Use: led = SignalLedger(cfg, db); led.on_rule_fire(ev, snap); led.on_tick(tick).
Screener only - it never places orders; it simulates the trade plan it displays
so every signal produces a measurable outcome the system can learn from.

Trade plan (mirrors a scale-in style): tranche 1 at the signal print plus adds
each entry_step_r below (long); R is quoted on the tranche-1-to-stop distance.
At +tp1_r the plan banks tp1_fraction and moves the stop to break-even; at
+trail_start_r a trailing stop (trail_dist_r behind the best price) takes over;
TIME closes anything left at max_hold_s. Exits: STOP | BE | TRAIL | TIME.

Learning: per-rule Beta win-rate buckets (all history, incl. legacy outcomes)
drive composite weight multipliers; a per-rule online logistic regression over
the entry context (core.signals.models) predicts each signal's win probability,
refits from the full stored trade history at boot, and updates on every
resolution - the system sharpens continuously as outcomes accumulate.
"""

from __future__ import annotations

import json
import logging
import time as _time
from dataclasses import dataclass, field

from tapescreen.config import Config
from tapescreen.core.events import Tick
from tapescreen.core.features import FeatureSnapshot
from tapescreen.core.signals.base import LONG, SHORT, SignalEvent
from tapescreen.core.signals.models import OnlineLogistic
from tapescreen.core.thoughts import CAT_LEARN, CAT_SYSTEM, CAT_TRADE, ThoughtLog, px
from tapescreen.store.db import Db

log = logging.getLogger("tapescreen.ledger")

EXIT_WHY = {
    "STOP": "initial stop hit",
    "BE": "break-even stop",
    "TRAIL": "trailing stop",
    "TIME": "max-hold time limit",
}

EXIT_STOP = "STOP"
EXIT_BE = "BE"
EXIT_TRAIL = "TRAIL"
EXIT_TIME = "TIME"

ST_INIT = "INIT"
ST_BE = "BE"
ST_TRAIL = "TRAIL"


@dataclass(slots=True)
class Bucket:
    wins: float = 0.0
    n: float = 0.0
    r_sum: float = 0.0
    r_n: float = 0.0


@dataclass(slots=True)
class TradeSignal:
    id: int
    signal_id: int
    ts: float
    symbol: str
    side: str  # long | short
    rule: str
    tier: str
    confidence: float  # 0..1 win probability at entry
    conf_n: int
    conf_src: str  # "model" | "bucket"
    levels: list[dict]  # [{px, filled_ts|None}, ...] ladder, tranche 1 pre-filled
    avg_entry: float
    risk_unit: float  # price distance defining 1R (tranche 1 -> initial stop)
    stop: float  # current stop (moves to BE, then trails)
    stop_initial: float
    state: str = ST_INIT
    best_px: float = 0.0
    trail_note_stop: float = 0.0  # last trail level narrated (rate-limits thoughts)
    tp1_done: bool = False
    tp1_fraction: float = 1.0 / 3.0
    realized: float = 0.0  # R banked by the TP1 partial
    features: list[float] = field(default_factory=list)
    status: str = "open"  # open | win | loss
    exit_ts: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    total_r: float = 0.0
    last_price: float = 0.0

    @property
    def side_sign(self) -> float:
        return 1.0 if self.side == LONG else -1.0

    @property
    def filled(self) -> int:
        return sum(1 for lv in self.levels if lv["filled_ts"] is not None)

    def leg_r(self, px: float) -> float:
        """R of the running leg at price px, measured off the average entry."""
        if self.risk_unit <= 0:
            return 0.0
        return self.side_sign * (px - self.avg_entry) / self.risk_unit

    def live_r(self) -> float:
        """Unrealized total R right now (banked partial + open leg)."""
        if self.last_price <= 0:
            return self.realized
        frac = 1.0 - (self.tp1_fraction if self.tp1_done else 0.0)
        return self.realized + frac * self.leg_r(self.last_price)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "ts": self.ts, "symbol": self.symbol, "side": self.side,
            "rule": self.rule, "tier": self.tier,
            "confidence": round(self.confidence * 100, 1), "conf_n": self.conf_n,
            "conf_src": self.conf_src,
            "levels": [{"px": lv["px"], "filled": lv["filled_ts"] is not None}
                       for lv in self.levels],
            "avg_entry": self.avg_entry, "filled": self.filled,
            "stop": self.stop, "state": self.state, "risk_unit": self.risk_unit,
            "tp1_done": self.tp1_done, "realized": round(self.realized, 2),
            "status": self.status, "exit_ts": self.exit_ts,
            "exit_price": self.exit_price, "exit_reason": self.exit_reason,
            "r_result": round(self.total_r, 2), "live_r": round(self.live_r(), 2),
            # R-space positions for the UI trade gauge
            "leg_r": round(self.leg_r(self.last_price), 2) if self.last_price > 0 else 0.0,
            "stop_r": round(self.side_sign * (self.stop - self.avg_entry) / self.risk_unit, 2)
            if self.risk_unit > 0 else -1.0,
        }


def extract_features(ev: SignalEvent, s: FeatureSnapshot) -> list[float]:
    """Entry-context vector, side-signed so 'edge toward the trade' is positive.
    Order MUST match models.FEATURES."""
    sign = 1.0 if ev.side == LONG else -1.0
    price = s.price if s.price > 0 else 1.0
    return [
        s.vol_z,
        s.tps_z,
        sign * s.vwap_dist_sigma,
        sign * (s.book_imbalance - 0.5) * 2.0,
        sign * (s.agg_imbalance_60s - 0.5) * 2.0,
        s.spread_bps,
        -sign * s.funding * 1e4,
        sign * (s.roc_5m / s.roc5m_sigma if s.roc5m_sigma > 1e-12 else 0.0),
        s.atr_1m / price * 1e4,
        sign * (s.rsi_14 - 50.0) / 50.0,
        ev.strength,
        min(1.0, ev.score / 100.0),
    ]


class SignalLedger:
    """Opens a laddered, managed trade plan per directional rule fire."""

    def __init__(self, cfg: Config, db: Db | None,
                 thoughts: ThoughtLog | None = None) -> None:
        self.cfg = cfg
        self.lc = cfg.learning
        self.db = db
        self.th = thoughts
        self.haircut = cfg.stats.spread_haircut_bps / 1e4
        self.open: dict[str, list[TradeSignal]] = {}
        self.resolved_recent: list[dict] = []
        self.rule_buckets: dict[str, Bucket] = {}
        self.pair_buckets: dict[tuple[str, str], Bucket] = {}
        self.models: dict[str, OnlineLogistic] = {}
        self._next_id = 1
        if db is not None:
            self._next_id = db.next_trade_signal_id()
            self._bootstrap(db)
            if self.lc.model_enabled:
                self._refit(db)

    # ------------------------------------------------------------- learning

    def _bootstrap(self, db: Db) -> None:
        try:
            rows = db.learning_buckets(self.cfg.stats.spread_haircut_bps)
        except Exception:
            log.exception("learning bootstrap failed; starting from priors")
            return
        for r in rows:
            wins, n = float(r["wins"] or 0), float(r["n"] or 0)
            r_sum, r_n = float(r.get("r_sum") or 0), float(r.get("r_n") or 0)
            for b in (self.rule_buckets.setdefault(r["rule"], Bucket()),
                      self.pair_buckets.setdefault((r["rule"], r["symbol"]), Bucket())):
                b.wins += wins
                b.n += n
                b.r_sum += r_sum
                b.r_n += r_n
        total = sum(b.n for b in self.rule_buckets.values())
        log.info("learning bootstrapped from %d resolved outcomes across %d rules",
                 int(total), len(self.rule_buckets))
        if self.th is not None and self.rule_buckets:
            lines = []
            for rule, b in sorted(self.rule_buckets.items()):
                wr = b.wins / b.n * 100 if b.n else 0.0
                lines.append(f"{rule}: {b.wins:.0f}W-{b.n - b.wins:.0f}L "
                             f"({wr:.1f}% win rate) -> score weight x{self.rule_multiplier(rule):.2f}")
            self.th.emit(_time.time(), CAT_SYSTEM, "",
                         f"learning memory loaded: {int(total)} resolved outcomes "
                         f"across {len(self.rule_buckets)} strategies", lines)

    def _refit(self, db: Db) -> None:
        """Rebuild the per-rule models from the full stored trade history.

        Models are refit from scratch each boot (persisted state is for
        inspection only), so history is never double-counted."""
        try:
            rows = db.resolved_feature_history()
        except Exception:
            log.exception("model refit failed; starting fresh")
            return
        samples: dict[str, list[tuple[list[float], bool]]] = {}
        for r in rows:
            try:
                x = [float(v) for v in json.loads(r["features"])]
            except (ValueError, TypeError):
                continue
            samples.setdefault(r["rule"], []).append((x, r["status"] == "win"))
        for rule, data in samples.items():
            m = self._model(rule)
            for _ in range(max(1, self.lc.refit_epochs)):
                for x, won in data:
                    m.update(x, won)
            m.n = len(data)  # n reflects distinct samples, not epoch passes
        if samples:
            log.info("models refit: %s",
                     {r: len(d) for r, d in samples.items()})
            if self.th is not None:
                self.th.emit(_time.time(), CAT_SYSTEM, "",
                             "ML models rebuilt from the full stored trade history",
                             [f"{r}: trained on {len(d)} resolved trades"
                              for r, d in sorted(samples.items())]
                             + ["refit from scratch each boot so no outcome is ever double-counted"])

    def _model(self, rule: str) -> OnlineLogistic:
        m = self.models.get(rule)
        if m is None:
            m = OnlineLogistic(self.lc.model_lr, self.lc.model_l2)
            self.models[rule] = m
        return m

    def _posterior(self, b: Bucket) -> float:
        return (b.wins + self.lc.prior_wins) / (b.n + self.lc.prior_wins + self.lc.prior_losses)

    def confidence(self, rule: str, symbol: str,
                   features: list[float] | None = None) -> tuple[float, int, str]:
        """(win probability, samples, source) - model when trained, else buckets."""
        if (self.lc.model_enabled and features is not None):
            m = self.models.get(rule)
            if m is not None and m.n >= self.lc.model_min_n:
                return m.predict(features), m.n, "model"
        pair = self.pair_buckets.get((rule, symbol))
        if pair is not None and pair.n >= self.lc.min_bucket_n:
            return self._posterior(pair), int(pair.n), "bucket"
        rb = self.rule_buckets.get(rule)
        if rb is not None and rb.n > 0:
            return self._posterior(rb), int(rb.n), "bucket"
        return self._posterior(Bucket()), 0, "bucket"

    def _conf_explain(self, rule: str, symbol: str, features: list[float],
                      conf: float, n: int, src: str) -> str:
        """One detail line saying where a confidence number actually came from."""
        pc = f"win prob {conf * 100:.0f}%"
        if src == "model":
            m = self.models.get(rule)
            top = [f"{name} {c:+.2f}" for name, c in m.contributions(features)[:3]
                   if abs(c) > 1e-9] if m else []
            factors = f"; strongest factors: {', '.join(top)}" if top else ""
            return (f"{pc} from the ML model for {rule} "
                    f"(trained on {n} resolved trades){factors}")
        pair = self.pair_buckets.get((rule, symbol))
        if pair is not None and pair.n >= self.lc.min_bucket_n:
            return (f"{pc} from measured history: {rule} on {symbol} won "
                    f"{pair.wins:.0f} of {pair.n:.0f} "
                    f"(Beta({self.lc.prior_wins:g},{self.lc.prior_losses:g}) prior)")
        rb = self.rule_buckets.get(rule)
        if rb is not None and rb.n > 0:
            return (f"{pc} from measured history: {rule} won {rb.wins:.0f} of "
                    f"{rb.n:.0f} across all symbols "
                    f"(Beta({self.lc.prior_wins:g},{self.lc.prior_losses:g}) prior)")
        return f"{pc}: no track record yet - starting from the neutral prior"

    def rule_multiplier(self, rule: str) -> float:
        if not self.lc.enabled:
            return 1.0
        rb = self.rule_buckets.get(rule)
        if rb is None or rb.n < self.lc.weight_min_n:
            return 1.0
        return min(self.lc.weight_mult_max, max(self.lc.weight_mult_min, 2.0 * self._posterior(rb)))

    def learning_snapshot(self) -> list[dict]:
        out = []
        for rule, b in sorted(self.rule_buckets.items()):
            m = self.models.get(rule)
            out.append({
                "rule": rule, "n": int(b.n),
                "win_rate": round(b.wins / b.n * 100, 1) if b.n else None,
                "avg_r": round(b.r_sum / b.r_n, 2) if b.r_n else None,
                "confidence": round(self._posterior(b) * 100, 1),
                "weight_mult": round(self.rule_multiplier(rule), 2),
                "model_n": m.n if m else 0,
            })
        return out

    # ------------------------------------------------------------- lifecycle

    def on_rule_fire(self, ev: SignalEvent, snap: FeatureSnapshot) -> TradeSignal | None:
        if ev.side not in (LONG, SHORT):
            return None
        entry = float(ev.snapshot.get("price") or snap.price)
        if entry <= 0:
            return None
        if self.lc.one_per_side and any(
            t.side == ev.side for t in self.open.get(ev.symbol, ())
        ):
            if self.th is not None:
                self.th.emit(ev.ts, CAT_TRADE, ev.symbol,
                             f"{ev.rule} fired {ev.side} but a {ev.side} trade is already open - not stacking",
                             ["one position per side keeps risk defined; the open trade's plan stands"])
            return None

        atr = snap.atr_1m if snap.atr_1m > 0 else entry * 0.001
        inv = float(ev.snapshot.get("invalidation") or 0.0)
        dist = abs(entry - inv)
        wrong_side = (ev.side == LONG and inv >= entry) or (ev.side == SHORT and inv <= entry)
        if inv <= 0 or wrong_side or dist == 0:
            dist = atr
            stop_basis = "no usable invalidation level -> 1 ATR(1m) fallback"
        else:
            stop_basis = f"rule invalidation level {px(inv)}"
        raw_dist = dist
        dist = min(self.lc.stop_atr_max * atr, max(self.lc.stop_atr_min * atr, dist))
        if dist > raw_dist:
            stop_basis += (f", widened to the {self.lc.stop_atr_min:g}x ATR floor "
                           "(too tight = noise stops you out)")
        elif dist < raw_dist:
            stop_basis += (f", tightened to the {self.lc.stop_atr_max:g}x ATR cap "
                           "(caps the damage when the level is far)")
        sign = 1.0 if ev.side == LONG else -1.0
        stop = entry - sign * dist

        levels = [{"px": entry, "filled_ts": ev.ts}]
        for i in range(1, max(1, self.lc.entry_levels)):
            levels.append({"px": entry - sign * dist * self.lc.entry_step_r * i,
                           "filled_ts": None})

        features = extract_features(ev, snap)
        conf, conf_n, conf_src = self.confidence(ev.rule, ev.symbol, features)
        ts = TradeSignal(
            id=self._next_id, signal_id=int(ev.snapshot.get("_db_id", 0)), ts=ev.ts,
            symbol=ev.symbol, side=ev.side, rule=ev.rule, tier=ev.tier,
            confidence=conf, conf_n=conf_n, conf_src=conf_src,
            levels=levels, avg_entry=entry, risk_unit=dist,
            stop=stop, stop_initial=stop, best_px=entry,
            tp1_fraction=self.lc.tp1_fraction,
            features=features, last_price=entry,
        )
        self._next_id += 1
        self.open.setdefault(ev.symbol, []).append(ts)
        if self.th is not None:
            step = self.lc.entry_step_r
            rungs = " · ".join(
                f"E{i + 1} {px(lv['px'])}" + (" (filled at signal)" if i == 0
                                              else f" ({-step * i:g}R)")
                for i, lv in enumerate(levels))
            self.th.emit(ev.ts, CAT_TRADE, ev.symbol,
                         f"opening {ev.side} · {ev.rule} · win prob {conf * 100:.0f}%", [
                f"stop {px(stop)}: {stop_basis}; 1R = {px(dist)} "
                f"({dist / entry * 1e4:.0f}bp of price)",
                f"scale-in ladder: {rungs} - "
                f"{self.lc.entry_window_s / 60:.0f} min for adds to fill",
                self._conf_explain(ev.rule, ev.symbol, features, conf, conf_n, conf_src),
                f"plan: bank {self.lc.tp1_fraction:.0%} at +{self.lc.tp1_r:g}R and move "
                f"stop to break-even · trail {self.lc.trail_dist_r:g}R behind best from "
                f"+{self.lc.trail_start_r:g}R · hard exit at "
                f"{self.lc.max_hold_s / 3600:g}h",
            ])
        if self.db is not None:
            self.db.insert_trade_signal(
                ts.id, ts.signal_id, ts.ts, ts.symbol, ts.side, ts.rule, ts.tier,
                conf, entry, stop, 0.0,
                entries_json=json.dumps(ts.levels), features_json=json.dumps(features),
            )
        return ts

    def on_tick(self, t: Tick) -> list[TradeSignal]:
        sigs = self.open.get(t.symbol)
        if not sigs:
            return []
        exited: list[TradeSignal] = []
        for ts in sigs:
            ts.last_price = t.price
            if t.ts_recv < ts.ts:
                continue
            changed = self._fill_ladder(ts, t)
            if self._check_exit(ts, t):
                exited.append(ts)
                continue
            if self._manage(ts, t):
                changed = True
            if changed and self.db is not None:
                self.db.update_trade_signal(ts.id, ts.avg_entry, json.dumps(ts.levels),
                                            ts.filled, ts.state, ts.stop)
        if exited:
            self.open[t.symbol] = [s for s in sigs if s.status == "open"]
        return exited

    def _fill_ladder(self, ts: TradeSignal, t: Tick) -> bool:
        """Adds fill when price trades through their level inside the entry window."""
        if t.ts_recv - ts.ts > self.lc.entry_window_s:
            return False
        changed = False
        hit_rungs: list[int] = []
        for i, lv in enumerate(ts.levels):
            if lv["filled_ts"] is not None:
                continue
            hit = t.price <= lv["px"] if ts.side == LONG else t.price >= lv["px"]
            if hit:
                lv["filled_ts"] = t.ts_recv
                hit_rungs.append(i)
                changed = True
        if changed:
            prev_avg = ts.avg_entry
            filled = [lv["px"] for lv in ts.levels if lv["filled_ts"] is not None]
            ts.avg_entry = sum(filled) / len(filled)
            if self.th is not None:
                rungs = ", ".join(f"E{i + 1} at {px(ts.levels[i]['px'])}" for i in hit_rungs)
                self.th.emit(t.ts_recv, CAT_TRADE, ts.symbol,
                             f"{ts.side} add filled: {rungs} ({ts.filled}/{len(ts.levels)} tranches in)",
                             [f"average entry improves {px(prev_avg)} -> {px(ts.avg_entry)} - "
                              "scaling in lowers the cost of an imperfect first entry"])
        return changed

    def _check_exit(self, ts: TradeSignal, t: Tick) -> bool:
        hit_stop = t.price <= ts.stop if ts.side == LONG else t.price >= ts.stop
        if hit_stop:
            reason = {ST_INIT: EXIT_STOP, ST_BE: EXIT_BE, ST_TRAIL: EXIT_TRAIL}[ts.state]
            self._resolve(ts, t.ts_recv, ts.stop, reason)
            return True
        if t.ts_recv - ts.ts >= self.lc.max_hold_s:
            self._resolve(ts, t.ts_recv, t.price, EXIT_TIME)
            return True
        return False

    def _manage(self, ts: TradeSignal, t: Tick) -> bool:
        """Break-even + trailing management on the open leg. Returns True on change."""
        changed = False
        sign = ts.side_sign
        better = (t.price > ts.best_px) if ts.side == LONG else (t.price < ts.best_px)
        if better:
            ts.best_px = t.price
        r_now = ts.leg_r(t.price)
        if not ts.tp1_done and r_now >= self.lc.tp1_r:
            ts.tp1_done = True
            ts.realized = ts.tp1_fraction * self.lc.tp1_r
            old_stop = ts.stop
            ts.state = ST_BE
            ts.stop = ts.avg_entry  # worst case from here: banked partial, flat leg
            changed = True
            if self.th is not None:
                self.th.emit(t.ts_recv, CAT_TRADE, ts.symbol,
                             f"{ts.side} hit +{self.lc.tp1_r:g}R - banking "
                             f"{ts.tp1_fraction:.0%}, stop to break-even", [
                    f"price {px(t.price)} = +{r_now:.2f}R off avg entry {px(ts.avg_entry)}",
                    f"+{ts.realized:.2f}R secured; stop {px(old_stop)} -> "
                    f"{px(ts.stop)} (entry) - worst case from here is a net win",
                ])
        if ts.state != ST_TRAIL and ts.leg_r(ts.best_px) >= self.lc.trail_start_r:
            ts.state = ST_TRAIL
            ts.trail_note_stop = ts.stop
            changed = True
            if self.th is not None:
                self.th.emit(t.ts_recv, CAT_TRADE, ts.symbol,
                             f"{ts.side} trailing stop engaged at "
                             f"+{ts.leg_r(ts.best_px):.2f}R", [
                    f"best price {px(ts.best_px)} cleared the "
                    f"+{self.lc.trail_start_r:g}R trigger",
                    f"stop now follows {self.lc.trail_dist_r:g}R behind the best "
                    "price - lets the winner run while protecting gains",
                ])
        if ts.state == ST_TRAIL:
            trail = ts.best_px - sign * self.lc.trail_dist_r * ts.risk_unit
            tighter = (trail > ts.stop) if ts.side == LONG else (trail < ts.stop)
            if tighter:
                ts.stop = trail
                changed = True
                moved = abs(ts.stop - ts.trail_note_stop)
                if self.th is not None and moved >= 0.5 * ts.risk_unit:
                    ts.trail_note_stop = ts.stop
                    locked = ts.leg_r(ts.stop)
                    self.th.emit(t.ts_recv, CAT_TRADE, ts.symbol,
                                 f"{ts.side} trail ratchets: stop -> {px(ts.stop)}",
                                 [f"best price {px(ts.best_px)}; exit here would lock "
                                  f"{locked:+.2f}R on the open leg"])
        return changed

    def _resolve(self, ts: TradeSignal, exit_ts: float, exit_price: float, reason: str) -> None:
        frac = 1.0 - (ts.tp1_fraction if ts.tp1_done else 0.0)
        leg = ts.leg_r(exit_price)
        ts.total_r = ts.realized + frac * leg
        ts.exit_ts = exit_ts
        ts.exit_price = exit_price
        ts.exit_reason = reason
        # win test net of the spread haircut, converted into R-space
        haircut_r = self.haircut / (ts.risk_unit / ts.avg_entry) if ts.risk_unit > 0 else 0.0
        won = ts.total_r > haircut_r
        ts.status = "win" if won else "loss"

        if self.th is not None:
            banked = f" + banked {ts.realized:.2f}R" if ts.tp1_done else ""
            hold_min = max(0.0, exit_ts - ts.ts) / 60.0
            self.th.emit(exit_ts, CAT_TRADE, ts.symbol,
                         f"closed {ts.side} · {EXIT_WHY.get(reason, reason)} · "
                         f"{ts.total_r:+.2f}R {'WIN' if won else 'LOSS'}", [
                f"exit {px(exit_price)} after {hold_min:.0f} min: open leg "
                f"{leg:+.2f}R on {frac:.0%} of the plan{banked} = {ts.total_r:+.2f}R total",
                f"win test: {ts.total_r:+.2f}R must beat the {haircut_r:.2f}R spread "
                f"haircut -> {'WIN' if won else 'LOSS'}",
            ])

        rb = self.rule_buckets.setdefault(ts.rule, Bucket())
        wr_before = rb.wins / rb.n * 100 if rb.n else None
        mult_before = self.rule_multiplier(ts.rule)
        for b in (rb, self.pair_buckets.setdefault((ts.rule, ts.symbol), Bucket())):
            b.wins += 1.0 if won else 0.0
            b.n += 1.0
            b.r_sum += ts.total_r
            b.r_n += 1.0

        learn_lines = [
            f"{ts.rule} record: " + (f"{wr_before:.1f}%" if wr_before is not None else "no data")
            + f" -> {rb.wins:.0f}W-{rb.n - rb.wins:.0f}L ({rb.wins / rb.n * 100:.1f}% win rate), "
            f"avg {rb.r_sum / max(rb.r_n, 1):+.2f}R per trade",
        ]
        mult_after = self.rule_multiplier(ts.rule)
        if abs(mult_after - mult_before) >= 0.005:
            learn_lines.append(
                f"score weight x{mult_before:.2f} -> x{mult_after:.2f} - "
                + ("this strategy earns a louder voice" if mult_after > mult_before
                   else "this strategy's voice gets quieter"))
        else:
            learn_lines.append(f"score weight holds at x{mult_after:.2f}")

        if self.lc.model_enabled and ts.features:
            m = self._model(ts.rule)
            p_before = m.update(ts.features, won)
            p_after = m.predict(ts.features)
            learn_lines.append(
                f"ML model took the lesson (n={m.n}): the same setup would now score "
                f"{p_after * 100:.0f}% (was {p_before * 100:.0f}% pre-update); "
                f"this trade was entered at {ts.confidence * 100:.0f}%")
            if self.db is not None:
                self.db.save_model(ts.rule, json.dumps(m.to_state()), m.n, exit_ts)

        if self.th is not None:
            self.th.emit(exit_ts, CAT_LEARN, ts.symbol,
                         f"learned from the {ts.rule} {'win' if won else 'loss'}",
                         learn_lines)

        self.resolved_recent.append(ts.to_dict())
        if len(self.resolved_recent) > 60:
            del self.resolved_recent[0]
        if self.db is not None:
            self.db.close_trade_signal(ts.id, ts.status, exit_ts, exit_price, reason,
                                       ts.total_r, ts.total_r, ts.state)

    def active(self) -> list[dict]:
        out = []
        for sigs in self.open.values():
            out.extend(t.to_dict() for t in sigs)
        out.sort(key=lambda d: (-d["confidence"], -d["ts"]))
        return out
