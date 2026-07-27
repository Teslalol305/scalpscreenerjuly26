"""Trade-signal ledger: explicit entries/exits, win-loss tracking, learned confidence.

Use: led = SignalLedger(cfg, db); led.on_rule_fire(ev, snap) -> TradeSignal | None;
led.on_tick(tick) -> list[ExitEvent]. Screener only - it never places orders.
Depends on: config, store.db, signals.base. Confidence is the Beta-posterior win
probability of the signal's (rule, symbol) bucket, backing off to the rule bucket
below min_bucket_n samples; every resolution updates the buckets, and rules with
enough history get a bounded composite-weight multiplier (2 x P, clamped).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tapescreen.config import Config
from tapescreen.core.events import Tick
from tapescreen.core.features import FeatureSnapshot
from tapescreen.core.signals.base import LONG, SHORT, SignalEvent
from tapescreen.store.db import Db

log = logging.getLogger("tapescreen.ledger")

EXIT_TARGET = "TARGET"
EXIT_STOP = "STOP"
EXIT_TIME = "TIME"


@dataclass(slots=True)
class Bucket:
    wins: float = 0.0
    n: float = 0.0


@dataclass(slots=True)
class TradeSignal:
    id: int
    signal_id: int
    ts: float
    symbol: str
    side: str  # long | short
    rule: str
    tier: str
    confidence: float  # 0..1 posterior win probability at entry
    conf_n: int  # samples behind the confidence number
    entry: float
    stop: float
    target: float
    status: str = "open"  # open | win | loss
    exit_ts: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    r_result: float = 0.0
    last_price: float = 0.0
    extras: dict = field(default_factory=dict)

    @property
    def side_sign(self) -> float:
        return 1.0 if self.side == LONG else -1.0

    def live_r(self) -> float:
        """Unrealized result in R units at last_price (0 until a tick arrives)."""
        if self.last_price <= 0 or self.entry <= 0:
            return 0.0
        risk = abs(self.entry - self.stop)
        if risk <= 0:
            return 0.0
        return self.side_sign * (self.last_price - self.entry) / risk

    def to_dict(self) -> dict:
        return {
            "id": self.id, "ts": self.ts, "symbol": self.symbol, "side": self.side,
            "rule": self.rule, "tier": self.tier,
            "confidence": round(self.confidence * 100, 1), "conf_n": self.conf_n,
            "entry": self.entry, "stop": self.stop, "target": self.target,
            "status": self.status, "exit_ts": self.exit_ts, "exit_price": self.exit_price,
            "exit_reason": self.exit_reason, "r_result": round(self.r_result, 2),
            "live_r": round(self.live_r(), 2),
        }


class SignalLedger:
    """Opens a tracked trade signal per directional rule fire and resolves it."""

    def __init__(self, cfg: Config, db: Db | None) -> None:
        self.cfg = cfg
        self.lc = cfg.learning
        self.db = db
        self.haircut = cfg.stats.spread_haircut_bps / 1e4
        self.open: dict[str, list[TradeSignal]] = {}  # symbol -> open signals
        self.resolved_recent: list[dict] = []  # newest last, capped for the UI ticker
        self.rule_buckets: dict[str, Bucket] = {}
        self.pair_buckets: dict[tuple[str, str], Bucket] = {}  # (rule, symbol)
        self._next_id = 1
        if db is not None:
            self._next_id = db.next_trade_signal_id()
            self._bootstrap(db)

    # ------------------------------------------------------------- learning

    def _bootstrap(self, db: Db) -> None:
        """Seed win-rate buckets from everything already measured (trade signals
        resolved in prior sessions + the legacy outcomes table)."""
        try:
            rows = db.learning_buckets(self.cfg.stats.spread_haircut_bps)
        except Exception:
            log.exception("learning bootstrap failed; starting from priors")
            return
        for r in rows:
            wins, n = float(r["wins"] or 0), float(r["n"] or 0)
            rb = self.rule_buckets.setdefault(r["rule"], Bucket())
            rb.wins += wins
            rb.n += n
            pb = self.pair_buckets.setdefault((r["rule"], r["symbol"]), Bucket())
            pb.wins += wins
            pb.n += n
        total = sum(b.n for b in self.rule_buckets.values())
        log.info("learning bootstrapped from %d resolved outcomes across %d rules",
                 int(total), len(self.rule_buckets))

    def _posterior(self, b: Bucket) -> float:
        return (b.wins + self.lc.prior_wins) / (b.n + self.lc.prior_wins + self.lc.prior_losses)

    def confidence(self, rule: str, symbol: str) -> tuple[float, int]:
        """(posterior win probability, samples) for a prospective signal."""
        pair = self.pair_buckets.get((rule, symbol))
        if pair is not None and pair.n >= self.lc.min_bucket_n:
            return self._posterior(pair), int(pair.n)
        rb = self.rule_buckets.get(rule)
        if rb is not None and rb.n > 0:
            return self._posterior(rb), int(rb.n)
        return self._posterior(Bucket()), 0

    def rule_multiplier(self, rule: str) -> float:
        """Learned composite-weight multiplier: 2 x P clamped, 1.0 until weight_min_n."""
        if not self.lc.enabled:
            return 1.0
        rb = self.rule_buckets.get(rule)
        if rb is None or rb.n < self.lc.weight_min_n:
            return 1.0
        return min(self.lc.weight_mult_max, max(self.lc.weight_mult_min, 2.0 * self._posterior(rb)))

    def learning_snapshot(self) -> list[dict]:
        out = []
        for rule, b in sorted(self.rule_buckets.items()):
            out.append({
                "rule": rule, "n": int(b.n),
                "win_rate": round(b.wins / b.n * 100, 1) if b.n else None,
                "confidence": round(self._posterior(b) * 100, 1),
                "weight_mult": round(self.rule_multiplier(rule), 2),
            })
        return out

    # ------------------------------------------------------------- lifecycle

    def on_rule_fire(self, ev: SignalEvent, snap: FeatureSnapshot) -> TradeSignal | None:
        """Open a tracked trade signal for a directional rule fire (entry event)."""
        if ev.side not in (LONG, SHORT):
            return None
        entry = float(ev.snapshot.get("price") or snap.price)
        if entry <= 0:
            return None
        if self.lc.one_per_side and any(
            t.side == ev.side for t in self.open.get(ev.symbol, ())
        ):
            return None  # one active signal per (symbol, side): no board flooding

        atr = snap.atr_1m if snap.atr_1m > 0 else entry * 0.001
        inv = float(ev.snapshot.get("invalidation") or 0.0)
        dist = abs(entry - inv)
        wrong_side = (ev.side == LONG and inv >= entry) or (ev.side == SHORT and inv <= entry)
        if inv <= 0 or wrong_side or dist == 0:
            dist = atr
        dist = min(self.lc.stop_atr_max * atr, max(self.lc.stop_atr_min * atr, dist))
        sign = 1.0 if ev.side == LONG else -1.0
        stop = entry - sign * dist
        target = entry + sign * dist * self.lc.target_r

        conf, conf_n = self.confidence(ev.rule, ev.symbol)
        sig_id_val = int(ev.snapshot.get("_db_id", 0))
        ts = TradeSignal(
            id=self._next_id, signal_id=sig_id_val, ts=ev.ts, symbol=ev.symbol,
            side=ev.side, rule=ev.rule, tier=ev.tier, confidence=conf, conf_n=conf_n,
            entry=entry, stop=stop, target=target, last_price=entry,
        )
        self._next_id += 1
        self.open.setdefault(ev.symbol, []).append(ts)
        if self.db is not None:
            self.db.insert_trade_signal(ts.id, ts.signal_id, ts.ts, ts.symbol, ts.side,
                                        ts.rule, ts.tier, conf, entry, stop, target)
        return ts

    def on_tick(self, t: Tick) -> list[TradeSignal]:
        """Advance open signals on a trade print; returns any that just exited."""
        sigs = self.open.get(t.symbol)
        if not sigs:
            return []
        exited: list[TradeSignal] = []
        for ts in sigs:
            ts.last_price = t.price
            if t.ts_recv < ts.ts:
                continue
            if ts.side == LONG:
                hit_stop = t.price <= ts.stop
                hit_target = t.price >= ts.target
            else:
                hit_stop = t.price >= ts.stop
                hit_target = t.price <= ts.target
            if hit_stop:
                self._resolve(ts, t.ts_recv, ts.stop, EXIT_STOP)
            elif hit_target:
                self._resolve(ts, t.ts_recv, ts.target, EXIT_TARGET)
            elif t.ts_recv - ts.ts >= self.lc.max_hold_s:
                self._resolve(ts, t.ts_recv, t.price, EXIT_TIME)
            if ts.status != "open":
                exited.append(ts)
        if exited:
            self.open[t.symbol] = [s for s in sigs if s.status == "open"]
        return exited

    def _resolve(self, ts: TradeSignal, exit_ts: float, exit_price: float, reason: str) -> None:
        risk = abs(ts.entry - ts.stop)
        signed_ret = ts.side_sign * (exit_price / ts.entry - 1.0)
        ts.exit_ts = exit_ts
        ts.exit_price = exit_price
        ts.exit_reason = reason
        ts.r_result = ts.side_sign * (exit_price - ts.entry) / risk if risk > 0 else 0.0
        win = signed_ret > self.haircut  # same net-of-haircut test the stats use
        ts.status = "win" if win else "loss"

        rb = self.rule_buckets.setdefault(ts.rule, Bucket())
        rb.wins += 1.0 if win else 0.0
        rb.n += 1.0
        pb = self.pair_buckets.setdefault((ts.rule, ts.symbol), Bucket())
        pb.wins += 1.0 if win else 0.0
        pb.n += 1.0

        self.resolved_recent.append(ts.to_dict())
        if len(self.resolved_recent) > 60:
            del self.resolved_recent[0]
        if self.db is not None:
            self.db.close_trade_signal(ts.id, ts.status, exit_ts, exit_price, reason,
                                       ts.r_result)

    def active(self) -> list[dict]:
        out = []
        for sigs in self.open.values():
            out.extend(t.to_dict() for t in sigs)
        out.sort(key=lambda d: (-d["confidence"], -d["ts"]))
        return out
