"""Validation tracker: is any strategy's measured record ready for real money?

Use: report = validation_report(db, cfg, probation, now).
Depends on: store.db (resolved trade history). The bar, per strategy:
>= min_trades resolved R-trades, >= min_days distinct active days (regime
coverage), the one-sided 95% confidence interval of average R above zero
(net of the spread + fee haircut applied at resolve time), and not on
desk probation. A confidently NEGATIVE record is also a verdict: REJECTED.
Anything else is COLLECTING, with a projected ready date at the current
trade rate - so the dashboard answers "how much longer" every day.
"""

from __future__ import annotations

import math
from typing import Any

from tapescreen.config import Config

Z95 = 1.645  # one-sided 95%

ST_VALIDATED = "validated"
ST_REJECTED = "rejected"
ST_COLLECTING = "collecting"
ST_WAITING = "waiting"  # no resolved trades yet


def directional_rules(cfg: Config) -> list[str]:
    """Rules that open trades (context rules never trade)."""
    return [r for r in cfg.rules if r not in ("oi_compression", "funding_extremity")]


def _rule_report(row: dict[str, Any] | None, vc, probation: set[str],
                 rule: str, now: float) -> dict[str, Any]:
    if row is None or not row["n"]:
        return {"rule": rule, "status": ST_WAITING, "n": 0, "days": 0,
                "mean_r": None, "ci_lo": None, "ci_hi": None, "win_rate": None,
                "rate_per_day": 0.0, "eta_ts": None,
                "on_probation": rule in probation}
    n = int(row["n"])
    mean = row["r_sum"] / n
    var = max(0.0, row["r2_sum"] / n - mean * mean)
    se = math.sqrt(var / n) if n >= 2 else None
    ci_lo = mean - Z95 * se if se is not None else None
    ci_hi = mean + Z95 * se if se is not None else None
    days = int(row["days"])
    span_days = max(1.0, (row["last_ts"] - row["first_ts"]) / 86400.0)
    rate = row["recent_n"] / 7.0 if row["recent_n"] else n / span_days
    on_prob = rule in probation

    evidence_full = n >= vc.min_trades and days >= vc.min_days
    if evidence_full and ci_hi is not None and ci_hi < 0.0:
        status = ST_REJECTED  # confidently negative: a verdict, and a save
    elif evidence_full and ci_lo is not None and ci_lo > 0.0 and not on_prob:
        status = ST_VALIDATED
    else:
        status = ST_COLLECTING

    eta_ts = None
    if status == ST_COLLECTING and rate > 0:
        need_n_days = max(0.0, vc.min_trades - n) / rate
        need_cov_days = float(max(0, vc.min_days - days))
        eta_ts = now + max(need_n_days, need_cov_days) * 86400.0

    return {
        "rule": rule, "status": status, "n": n, "days": days,
        "mean_r": round(mean, 3),
        "ci_lo": round(ci_lo, 3) if ci_lo is not None else None,
        "ci_hi": round(ci_hi, 3) if ci_hi is not None else None,
        "win_rate": round(row["wins"] / n * 100, 1),
        "rate_per_day": round(rate, 2),
        "eta_ts": round(eta_ts, 0) if eta_ts else None,
        "on_probation": on_prob,
    }


def validation_report(db: Any, cfg: Config, probation: set[str],
                      now: float) -> dict[str, Any]:
    vc = cfg.validation
    by_rule = {r["rule"]: r for r in db.validation_stats(now - 7 * 86400.0)}
    rules = [_rule_report(by_rule.get(r), vc, probation, r, now)
             for r in directional_rules(cfg)]
    rules.sort(key=lambda r: (-r["n"], r["rule"]))
    validated = [r["rule"] for r in rules if r["status"] == ST_VALIDATED]
    return {
        "ts": now,
        "targets": {"min_trades": vc.min_trades, "min_days": vc.min_days},
        "rules": rules,
        "validated": validated,
        "ready": bool(validated),
    }
