# TapeScreen

Real-time crypto scalp screener over **Hyperliquid perpetuals**: one dashboard that
ingests trades / order book / funding+OI for 13 symbols and surfaces scalp-signal
candidates (seconds-to-15-minutes horizon) with **measured forward outcomes**.

> Screener and alerting tool only. It places no orders, holds no keys, and promises
> no profitability — every signal is logged to SQLite with forward returns and
> MFE/MAE so signal quality is *measured*, never assumed.

## Quick start (macOS / any machine with API access)

```bash
python3.11 -m pip install -e ".[dev]"

# 1) verify the watchlist against live Hyperliquid meta (rewrites symbol_map.yaml)
python scripts/verify_symbols.py

# 2) run the screener + dashboard
python -m tapescreen
# -> http://localhost:5560
```

Useful modes:

```bash
python -m tapescreen --record 60              # capture 60s of raw feed, then exit
python -m tapescreen --replay recordings/hl-....ndjson --speed 10   # replay a capture
python -m tapescreen --no-ui                  # headless (feed + signals + DB only)
```

Everything is configured in `config.yaml` (port, every rule threshold/weight,
cooldowns, tiers, spread-cost haircut, recorder, staleness). The config is
validated at startup with errors that name the offending key.

## What you see

- **Main grid** — one row per symbol: price (tick-flash), Δ% 1m/5m/15m, volume
  z-score, CVD 5m sparkline, top-10 book-imbalance bar, spread (bps), funding badge
  (bold when at a 7-day extreme, hover for percentile), ΔOI 5m, and heat-colored
  Long/Short composite scores. Sorted by max(L, S). Rows gray out when stale
  (no tick in 30s); `warm` badge = baselines still warming, z-scores not yet trusty.
- **Detail drawer** (click a row) — 1s/1m candles with session VWAP ±1σ/±2σ lines,
  CVD subplot, and signal markers (lightweight-charts, vendored, no CDN).
- **Signal feed** — reverse-chronological events with tier / rule / side / snapshot
  values; filter by symbol, tier, rule. Tier `INFO` = rule fired but composite
  confluence below WATCH (logged for stats; spec tiers WATCH ≥ 60, ALERT ≥ 80).
- **Alerts** — optional sound + browser notification for **ALERT tier only** (🔔 toggle).
- **Stats tab** — per-rule / per-symbol counts, hit-rate at +30s/+1m/+3m/+5m *net of
  a configurable spread-cost haircut*, average MFE/MAE. This is the tuning loop.
- **Status bar** — feed connection, msg/s, ingest latency (exchange→receive,
  includes clock skew), tick→UI pipeline latency p50/p95 (monotonic clock),
  dropped messages, uptime.

## Architecture

Single Python 3.11 asyncio process. FastAPI serves the static frontend and a
`/ws` bridge (250ms batched grid diffs + immediate signal events). SQLite (WAL)
via a dedicated writer thread. No Redis/Kafka/Docker.

```
Hyperliquid WS ──> feeds/hyperliquid.py ──> asyncio.Queue ──> core/engine.py
   (trades, l2Book,      │ reconnect+resubscribe,                 │ per symbol:
    bbo, activeAssetCtx) │ 30s ping, 10s watchdog                 │ state -> features -> rules -> composite
                         └─> feeds/recorder.py (raw ndjson)       ├─> store/db.py (signals, outcomes, funding)
 feeds/replayer.py  ──────────────────────────^                   ├─> store/outcomes.py (fwd ret, MFE/MAE)
   (same pipeline, deterministic)                                 └─> server/app.py -> browser
```

- The pipeline clock is `ts_recv` (recorded), so replaying a recording reproduces
  the exact feature/signal stream — the replayer is the dev harness.
- `bbo` is subscribed in addition to the spec's three feeds because the June-2026
  l2Book throttle (20 levels / 2s) is too slow for spread gating; bbo restores
  real-time top-of-book.
- Hot path is allocation-light: slots dataclasses, ring buffers with hard caps
  (documented in `core/state.py`), O(1) incremental calculators, no pandas.

## Verification & tests

```bash
ruff check .        # clean
python -m pytest    # unit + integration suite
```

- Feature calculators are unit-tested against hand-computed fixtures.
- A mock Hyperliquid WS server (`tests/mock_hl_server.py`) speaks the verified wire
  protocol; kill-and-recover and watchdog tests prove auto-reconnect+resubscribe.
- Replay determinism: identical event and feature streams across replays.
- End-to-end test asserts tick→UI p95 ≤ 250ms against the mock feed.
- Each of the 7 rules is triggered by a crafted synthetic tick script; cooldowns
  (90s per symbol+rule, 60s per symbol+side for alerts) are asserted.

After a ~30-minute live session, `GET /stats` should show every non-recent signal
with all horizon outcomes filled — that's the M5 acceptance check.

## Symbols

13 Hyperliquid perps: BTC, ETH, SOL, HYPE, LINK, AAVE, NEAR, TAO, WLD, ZEC, AERO,
PUMP, LIT. Note `LIT` is **Lighter** (lighter.xyz, TGE Dec 2025) — not Litentry.
`symbol_map.yaml` records verification status; `scripts/verify_symbols.py`
re-verifies against live `metaAndAssetCtxs` (szDecimals, maxLeverage, isDelisted)
and the app re-checks at startup, graying out anything unavailable.

## Non-goals (v1)

No order execution or exchange keys, no backtesting engine, no tick-history DB
beyond raw ndjson recordings, no auth/multi-user, no mobile layout, no ML.
Binance USDⓈ-M futures cross-venue module is config-stubbed (`feeds.binance.enabled:
false`) and deferred to v1.1 — the feed layer is adapter-shaped so it's a new
module, not a rewrite.
