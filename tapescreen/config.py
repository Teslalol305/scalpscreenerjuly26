"""Loads and validates config.yaml + symbol_map.yaml into typed objects.

Use: cfg = load_config("config.yaml"); cfg.symbols, cfg.rules["momentum_ignition"], ...
Depends on: pyyaml only. Fails fast with ConfigError naming the offending key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised when configuration is missing, malformed, or out of range."""


@dataclass(slots=True)
class SymbolEntry:
    ui_symbol: str
    venue: str
    venue_symbol: str
    status: str


@dataclass(slots=True)
class FeedConfig:
    ws_url: str
    info_url: str
    ping_interval_s: float
    watchdog_timeout_s: float
    backoff_initial_s: float
    backoff_max_s: float
    warmup_candles: bool
    warmup_candle_bars: int
    warmup_funding_history: bool


@dataclass(slots=True)
class RecorderConfig:
    enabled: bool
    path: str
    rotate_mb: int


@dataclass(slots=True)
class StateConfig:
    bars_1s_cap: int
    bars_1m_cap: int
    tick_window_s: float
    baseline_window_s: float


@dataclass(slots=True)
class FeatureConfig:
    large_print_pctl: float
    burst_k: int
    burst_window_s: float
    wall_mult: float
    book_top_levels: int


@dataclass(slots=True)
class CompositeConfig:
    watch_score: float
    alert_score: float
    rule_cooldown_s: float
    alert_cooldown_s: float


@dataclass(slots=True)
class StatsConfig:
    spread_haircut_bps: float
    taker_fee_bps: float
    horizons_s: list[int]
    mfe_mae_window_s: int


@dataclass(slots=True)
class ValidationConfig:
    min_trades: int  # resolved R-trades per strategy before a verdict
    min_days: int    # distinct active days (regime coverage) before a verdict


@dataclass(slots=True)
class LearningConfig:
    enabled: bool
    prior_wins: float
    prior_losses: float
    min_bucket_n: int
    weight_mult_min: float
    weight_mult_max: float
    weight_min_n: int
    stop_atr_min: float
    stop_atr_max: float
    one_per_side: bool
    entry_levels: int
    entry_step_r: float
    entry_window_s: float
    tp1_r: float
    tp1_fraction: float
    trail_start_r: float
    trail_dist_r: float
    max_hold_s: float
    model_enabled: bool
    model_lr: float
    model_l2: float
    model_min_n: int
    refit_epochs: int


@dataclass(slots=True)
class ResearchConfig:
    enabled: bool
    interval_s: float
    min_rule_n: int            # analyst reports a rule once it has this many outcomes
    probation_min_n: int       # risk officer acts only with this much evidence
    probation_enter: float     # posterior below this -> probation
    probation_exit: float      # posterior at/above this -> cleared
    probation_conf_min: float  # confidence bar a gated rule's fires must clear
    exploration_every: int     # 1 in N suppressed fires still opens (anti-starvation)
    promote_r: float           # |point-biserial| to promote a candidate variable
    promote_min_n: int         # resolved trades needed before scouting acts
    demote_r: float            # |r| below this (with 2x evidence) retires a variable
    max_extras: int            # cap on promoted variables (bounded model growth)
    stagnant_meetings: int     # meetings without change before the loop-breaker acts
    retire_cooldown_h: float   # hours before a retired variable can re-qualify


@dataclass(slots=True)
class AuditConfig:
    selftest_on_boot: bool
    interval_s: float
    price_divergence_pct: float
    mids_fresh_s: float
    quarantine_clear_checks: int


@dataclass(slots=True)
class Config:
    port: int
    sound_default: bool
    push_interval_ms: int
    symbols: dict[str, SymbolEntry]
    feed: FeedConfig
    binance_enabled: bool
    recorder: RecorderConfig
    symbol_stale_s: float
    state: StateConfig
    features: FeatureConfig
    rules: dict[str, dict[str, Any]]
    composite: CompositeConfig
    stats: StatsConfig
    validation: ValidationConfig
    learning: LearningConfig
    research: ResearchConfig
    audit: AuditConfig
    db_path: str
    log_path: str
    feed_debug: bool
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


def _get(d: dict[str, Any], path: str) -> Any:
    node: Any = d
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ConfigError(f"config missing required key: {path!r}")
        node = node[part]
    return node


def _num(d: dict[str, Any], path: str, lo: float | None = None) -> float:
    v = _get(d, path)
    if isinstance(v, bool) or not isinstance(v, int | float):
        raise ConfigError(f"config key {path!r} must be a number, got {type(v).__name__}")
    if lo is not None and v < lo:
        raise ConfigError(f"config key {path!r} must be >= {lo}, got {v}")
    return float(v)


def _int(d: dict[str, Any], path: str, lo: int | None = None) -> int:
    v = _num(d, path, lo)
    if v != int(v):
        raise ConfigError(f"config key {path!r} must be an integer, got {v}")
    return int(v)


def _bool(d: dict[str, Any], path: str) -> bool:
    v = _get(d, path)
    if not isinstance(v, bool):
        raise ConfigError(f"config key {path!r} must be a boolean, got {type(v).__name__}")
    return v


def _str(d: dict[str, Any], path: str) -> str:
    v = _get(d, path)
    if not isinstance(v, str) or not v:
        raise ConfigError(f"config key {path!r} must be a non-empty string")
    return v


REQUIRED_RULES = (
    "momentum_ignition",
    "sweep_reclaim",
    "vwap_fade",
    "squeeze_release",
    "book_imbalance",
    "oi_compression",
    "funding_extremity",
)


def load_symbol_map(path: str | Path) -> dict[str, SymbolEntry]:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"symbol map file not found: {p}")
    doc = yaml.safe_load(p.read_text())
    if not isinstance(doc, dict) or not isinstance(doc.get("symbols"), dict):
        raise ConfigError(f"{p}: expected top-level 'symbols' mapping")
    out: dict[str, SymbolEntry] = {}
    for ui, entry in doc["symbols"].items():
        if not isinstance(entry, dict):
            raise ConfigError(f"{p}: symbol {ui!r} entry must be a mapping")
        for k in ("venue", "venue_symbol", "status"):
            if k not in entry:
                raise ConfigError(f"{p}: symbol {ui!r} missing key {k!r}")
        out[str(ui)] = SymbolEntry(
            ui_symbol=str(ui),
            venue=str(entry["venue"]),
            venue_symbol=str(entry["venue_symbol"]),
            status=str(entry["status"]),
        )
    if not out:
        raise ConfigError(f"{p}: symbol map is empty")
    return out


def load_config(path: str | Path = "config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config file not found: {p}")
    try:
        doc = yaml.safe_load(p.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{p}: invalid YAML: {e}") from e
    if not isinstance(doc, dict):
        raise ConfigError(f"{p}: top level must be a mapping")

    symbols = load_symbol_map(p.parent / _str(doc, "symbols_file"))

    rules: dict[str, dict[str, Any]] = {}
    for name in REQUIRED_RULES:
        r = _get(doc, f"rules.{name}")
        if not isinstance(r, dict):
            raise ConfigError(f"config key 'rules.{name}' must be a mapping")
        rules[name] = r

    horizons = _get(doc, "stats.horizons_s")
    supported_horizons = {30, 60, 180, 300}  # the outcomes schema's ret_* columns
    if not isinstance(horizons, list) or not all(isinstance(h, int) and h > 0 for h in horizons):
        raise ConfigError("config key 'stats.horizons_s' must be a list of positive integers")
    bad = [h for h in horizons if h not in supported_horizons]
    if bad:
        raise ConfigError(
            f"config key 'stats.horizons_s' contains unsupported horizons {bad}; "
            f"supported: {sorted(supported_horizons)}"
        )

    return Config(
        port=_int(doc, "ui.port", 1),
        sound_default=_bool(doc, "ui.sound_default"),
        push_interval_ms=_int(doc, "ui.push_interval_ms", 50),
        symbols=symbols,
        feed=FeedConfig(
            ws_url=_str(doc, "feeds.hyperliquid.ws_url"),
            info_url=_str(doc, "feeds.hyperliquid.info_url"),
            ping_interval_s=_num(doc, "feeds.hyperliquid.ping_interval_s", 1),
            watchdog_timeout_s=_num(doc, "feeds.hyperliquid.watchdog_timeout_s", 1),
            backoff_initial_s=_num(doc, "feeds.hyperliquid.backoff_initial_s", 0.1),
            backoff_max_s=_num(doc, "feeds.hyperliquid.backoff_max_s", 1),
            warmup_candles=_bool(doc, "feeds.hyperliquid.warmup.candles"),
            warmup_candle_bars=_int(doc, "feeds.hyperliquid.warmup.candle_bars", 1),
            warmup_funding_history=_bool(doc, "feeds.hyperliquid.warmup.funding_history"),
        ),
        binance_enabled=_bool(doc, "feeds.binance.enabled"),
        recorder=RecorderConfig(
            enabled=_bool(doc, "recorder.enabled"),
            path=_str(doc, "recorder.path"),
            rotate_mb=_int(doc, "recorder.rotate_mb", 1),
        ),
        symbol_stale_s=_num(doc, "staleness.symbol_stale_s", 1),
        state=StateConfig(
            bars_1s_cap=_int(doc, "state.bars_1s_cap", 60),
            bars_1m_cap=_int(doc, "state.bars_1m_cap", 30),
            tick_window_s=_num(doc, "state.tick_window_s", 60),
            baseline_window_s=_num(doc, "state.baseline_window_s", 60),
        ),
        features=FeatureConfig(
            large_print_pctl=_num(doc, "features.large_print_pctl", 50),
            burst_k=_int(doc, "features.burst_k", 2),
            burst_window_s=_num(doc, "features.burst_window_s", 0.1),
            wall_mult=_num(doc, "features.wall_mult", 1),
            book_top_levels=_int(doc, "features.book_top_levels", 1),
        ),
        rules=rules,
        composite=CompositeConfig(
            watch_score=_num(doc, "composite.watch_score", 0),
            alert_score=_num(doc, "composite.alert_score", 0),
            rule_cooldown_s=_num(doc, "composite.rule_cooldown_s", 0),
            alert_cooldown_s=_num(doc, "composite.alert_cooldown_s", 0),
        ),
        stats=StatsConfig(
            spread_haircut_bps=_num(doc, "stats.spread_haircut_bps", 0),
            taker_fee_bps=_num(doc, "stats.taker_fee_bps", 0),
            horizons_s=list(horizons),
            mfe_mae_window_s=_int(doc, "stats.mfe_mae_window_s", 30),
        ),
        validation=ValidationConfig(
            min_trades=_int(doc, "validation.min_trades", 10),
            min_days=_int(doc, "validation.min_days", 1),
        ),
        learning=LearningConfig(
            enabled=_bool(doc, "learning.enabled"),
            prior_wins=_num(doc, "learning.prior_wins", 0),
            prior_losses=_num(doc, "learning.prior_losses", 0),
            min_bucket_n=_int(doc, "learning.min_bucket_n", 1),
            weight_mult_min=_num(doc, "learning.weight_mult_min", 0.1),
            weight_mult_max=_num(doc, "learning.weight_mult_max", 0.1),
            weight_min_n=_int(doc, "learning.weight_min_n", 1),
            stop_atr_min=_num(doc, "learning.stop_atr_min", 0.05),
            stop_atr_max=_num(doc, "learning.stop_atr_max", 0.05),
            one_per_side=_bool(doc, "learning.one_per_side"),
            entry_levels=_int(doc, "learning.entry_levels", 1),
            entry_step_r=_num(doc, "learning.entry_step_r", 0.05),
            entry_window_s=_num(doc, "learning.entry_window_s", 10),
            tp1_r=_num(doc, "learning.tp1_r", 0.1),
            tp1_fraction=_num(doc, "learning.tp1_fraction", 0),
            trail_start_r=_num(doc, "learning.trail_start_r", 0.1),
            trail_dist_r=_num(doc, "learning.trail_dist_r", 0.05),
            max_hold_s=_num(doc, "learning.max_hold_s", 60),
            model_enabled=_bool(doc, "learning.model_enabled"),
            model_lr=_num(doc, "learning.model_lr", 0.0001),
            model_l2=_num(doc, "learning.model_l2", 0),
            model_min_n=_int(doc, "learning.model_min_n", 1),
            refit_epochs=_int(doc, "learning.refit_epochs", 0),
        ),
        research=ResearchConfig(
            enabled=_bool(doc, "research.enabled"),
            interval_s=_num(doc, "research.interval_s", 10),
            min_rule_n=_int(doc, "research.min_rule_n", 1),
            probation_min_n=_int(doc, "research.probation_min_n", 5),
            probation_enter=_num(doc, "research.probation_enter", 0.05),
            probation_exit=_num(doc, "research.probation_exit", 0.05),
            probation_conf_min=_num(doc, "research.probation_conf_min", 0.05),
            exploration_every=_int(doc, "research.exploration_every", 2),
            promote_r=_num(doc, "research.promote_r", 0.01),
            promote_min_n=_int(doc, "research.promote_min_n", 10),
            demote_r=_num(doc, "research.demote_r", 0.0),
            max_extras=_int(doc, "research.max_extras", 0),
            stagnant_meetings=_int(doc, "research.stagnant_meetings", 1),
            retire_cooldown_h=_num(doc, "research.retire_cooldown_h", 0),
        ),
        audit=AuditConfig(
            selftest_on_boot=_bool(doc, "audit.selftest_on_boot"),
            interval_s=_num(doc, "audit.interval_s", 5),
            price_divergence_pct=_num(doc, "audit.price_divergence_pct", 0.01),
            mids_fresh_s=_num(doc, "audit.mids_fresh_s", 1),
            quarantine_clear_checks=_int(doc, "audit.quarantine_clear_checks", 1),
        ),
        db_path=_str(doc, "db.path"),
        log_path=_str(doc, "logging.path"),
        feed_debug=_bool(doc, "logging.feed_debug"),
        raw=doc,
    )
