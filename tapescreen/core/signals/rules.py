"""The v1 rule set (spec section 5). All thresholds come from config.yaml.

Use: imported for side effects (registry); Composite instantiates per symbol.
Depends on: signals.base, core.features. Each rule documents its trigger inline.
"""

from __future__ import annotations

from tapescreen.core.features import FeatureSnapshot
from tapescreen.core.signals.base import (
    CONTEXT,
    LONG,
    SHORT,
    SignalEvent,
    SignalRule,
    register,
)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else 1.0 if x > 1.0 else x


@register
class MomentumIgnition(SignalRule):
    """Continuation: |ROC30s| >= k*sigma AND vol_z >= min AND CVD(1m) agrees AND tight spread."""

    name = "momentum_ignition"

    def evaluate(self, s: FeatureSnapshot) -> SignalEvent | None:
        if s.warming or s.roc30_sigma <= 0 or s.spread_bps > float(self.p["max_spread_bps"]):
            return None
        k = float(self.p["roc_sigma_mult"])
        vol_min = float(self.p["vol_z_min"])
        if abs(s.roc_30s) < k * s.roc30_sigma or s.vol_z < vol_min:
            return None
        side = LONG if s.roc_30s > 0 else SHORT
        if (s.cvd_slope_1m > 0) != (side == LONG):  # CVD slope must agree with direction
            return None
        strength = _clamp01(
            0.5 * (abs(s.roc_30s) / (2 * k * s.roc30_sigma)) + 0.5 * (s.vol_z / (2 * vol_min))
        )
        snap = self.base_snapshot(s, invalidation=s.vwap_session)
        return SignalEvent(s.ts, s.symbol, side, self.name, strength, snapshot=snap)


@register
class SweepReclaim(SignalRule):
    """Reversal: print beyond prior 15-min extreme by >=k*ATR, reclaimed within 45s
    while 60s aggressor imbalance flips through 0.5 in the reclaim direction."""

    name = "sweep_reclaim"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._sweep_side = ""  # LONG = swept below the low, awaiting reclaim up
        self._sweep_ts = 0.0
        self._level = 0.0  # the swept prior extreme
        self._extreme = 0.0  # worst print during the sweep
        self._imb_at_sweep = 0.5

    def evaluate(self, s: FeatureSnapshot) -> SignalEvent | None:
        if s.warming or s.atr_1m <= 0 or not s.prior_15m_low:
            return None
        pad = float(self.p["atr_mult"]) * s.atr_1m
        window = float(self.p["reclaim_window_s"])

        if self._sweep_side and s.ts - self._sweep_ts > window:
            self._sweep_side = ""  # expired un-reclaimed

        if not self._sweep_side:
            if s.price <= s.prior_15m_low - pad:
                self._sweep_side = LONG
                self._sweep_ts = s.ts
                self._level = s.prior_15m_low
                self._extreme = s.price
                self._imb_at_sweep = s.agg_imbalance_60s
            elif s.price >= s.prior_15m_high + pad:
                self._sweep_side = SHORT
                self._sweep_ts = s.ts
                self._level = s.prior_15m_high
                self._extreme = s.price
                self._imb_at_sweep = s.agg_imbalance_60s
            return None

        if self._sweep_side == LONG:
            self._extreme = min(self._extreme, s.price)
            reclaimed = s.price > self._level
            flipped = self._imb_at_sweep < 0.5 < s.agg_imbalance_60s
        else:
            self._extreme = max(self._extreme, s.price)
            reclaimed = s.price < self._level
            flipped = self._imb_at_sweep > 0.5 > s.agg_imbalance_60s
        if not (reclaimed and flipped):
            return None
        side = self._sweep_side
        depth = abs(self._level - self._extreme)
        strength = _clamp01(0.5 + 0.5 * (depth / (2 * pad)) if pad > 0 else 0.5)
        snap = self.base_snapshot(s, invalidation=self._extreme)
        self._sweep_side = ""
        return SignalEvent(s.ts, s.symbol, side, self.name, strength, snapshot=snap)


@register
class VwapExtensionFade(SignalRule):
    """Mean-revert (counter-trend): stretched >=k sigma from session VWAP while
    CVD(1m) slope decelerates against the move and vol_z is off its 5-min peak."""

    name = "vwap_fade"

    def evaluate(self, s: FeatureSnapshot) -> SignalEvent | None:
        if s.warming or s.vwap_session_sd <= 0:
            return None
        k = float(self.p["band_sigma"])
        d = s.vwap_dist_sigma
        if abs(d) < k:
            return None
        side = SHORT if d > 0 else LONG  # fade the extension
        if side == SHORT:
            decel = s.cvd_slope_1m < s.cvd_slope_1m_prev  # buying losing steam
        else:
            decel = s.cvd_slope_1m > s.cvd_slope_1m_prev
        vol_declining = s.vol_z_5m_peak > 0 and s.vol_z < s.vol_z_5m_peak
        if not (decel and vol_declining):
            return None
        strength = _clamp01(0.4 + 0.3 * (abs(d) - k) + 0.3 * (1 - s.vol_z / max(s.vol_z_5m_peak, 1e-9)))
        snap = self.base_snapshot(s, invalidation=s.price)  # ref: extension extreme
        snap["counter_trend"] = True
        return SignalEvent(s.ts, s.symbol, side, self.name, strength, snapshot=snap)


@register
class SqueezeRelease(SignalRule):
    """Breakout: BB width pctl <= max for >= N minutes, then a 1m bar with
    range >= k*ATR14 and vol_z >= min - direction of the expansion bar."""

    name = "squeeze_release"

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self._run_before_bar = 0  # squeeze run as of BEFORE the latest 1m close

    def evaluate(self, s: FeatureSnapshot) -> SignalEvent | None:
        fire: SignalEvent | None = None
        if s.new_1m_bar and not s.warming:
            long_enough = self._run_before_bar >= int(self.p["squeeze_min_minutes"])
            expanded = s.atr_1m > 0 and s.last_1m_range >= float(self.p["range_atr_mult"]) * s.atr_1m
            loud = s.vol_z >= float(self.p["vol_z_min"])
            if long_enough and expanded and loud and s.last_1m_dir:
                side = LONG if s.last_1m_dir > 0 else SHORT
                strength = _clamp01(
                    0.5 * (s.last_1m_range / (2 * float(self.p["range_atr_mult"]) * s.atr_1m))
                    + 0.5 * (self._run_before_bar / (3 * int(self.p["squeeze_min_minutes"])) + 0.33)
                )
                base = s.price - s.last_1m_range if side == LONG else s.price + s.last_1m_range
                snap = self.base_snapshot(s, invalidation=base)
                snap["squeeze_run_min"] = self._run_before_bar
                fire = SignalEvent(s.ts, s.symbol, side, self.name, strength, snapshot=snap)
        if s.new_1m_bar:
            self._run_before_bar = s.squeeze_run_min
        return fire


@register
class BookImbalancePersistence(SignalRule):
    """Continuation: top-10 imbalance pinned >= hi (or <= lo) for >= N seconds while
    price sits within a few bps of the rolling 15-min high (low)."""

    name = "book_imbalance"

    def evaluate(self, s: FeatureSnapshot) -> SignalEvent | None:
        if s.warming or s.price <= 0:
            return None
        sustain = float(self.p["sustain_s"])
        prox = float(self.p["range_proximity_bps"])
        hi = max(s.prior_15m_high, s.price)
        lo = min(s.prior_15m_low, s.price) if s.prior_15m_low else s.price
        if s.imb_high_dur_s >= sustain and (hi - s.price) / s.price * 1e4 <= prox:
            side, dur, level = LONG, s.imb_high_dur_s, lo
        elif s.imb_low_dur_s >= sustain and (s.price - lo) / s.price * 1e4 <= prox:
            side, dur, level = SHORT, s.imb_low_dur_s, hi
        else:
            return None
        imb = s.book_imbalance if side == LONG else 1.0 - s.book_imbalance
        strength = _clamp01(0.4 + 0.4 * (imb - 0.65) / 0.35 + 0.2 * min(1.0, dur / (3 * sustain)))
        snap = self.base_snapshot(s, invalidation=level)
        snap["imb_duration_s"] = round(dur, 1)
        return SignalEvent(s.ts, s.symbol, side, self.name, strength, snapshot=snap)


@register
class OICompression(SignalRule):
    """Context flag (no side, never alerts): OI building while price goes nowhere."""

    name = "oi_compression"
    is_context = True

    def evaluate(self, s: FeatureSnapshot) -> SignalEvent | None:
        if s.warming or s.doi5_session_p95 <= 0 or s.roc5m_sigma <= 0:
            return None
        if abs(s.doi_5m) < s.doi5_session_p95:
            return None
        if abs(s.roc_5m) > float(self.p["roc_sigma_max"]) * s.roc5m_sigma:
            return None
        return SignalEvent(s.ts, s.symbol, CONTEXT, self.name, 1.0,
                           snapshot=self.base_snapshot(s))


@register
class FundingExtremity(SignalRule):
    """Context flag / bias badge: |funding| at or beyond its trailing 7-day p95.
    Positive funding -> crowded longs -> short bias, and vice versa."""

    name = "funding_extremity"
    is_context = True

    def evaluate(self, s: FeatureSnapshot) -> SignalEvent | None:
        if s.funding_abs_p95_7d <= 0 or abs(s.funding) < s.funding_abs_p95_7d:
            return None
        ev = SignalEvent(s.ts, s.symbol, CONTEXT, self.name, 1.0,
                         snapshot=self.base_snapshot(s))
        ev.snapshot["bias"] = SHORT if s.funding > 0 else LONG
        return ev
