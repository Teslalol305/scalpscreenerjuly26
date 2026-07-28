"""Canonical event types every venue adapter normalizes into.

Use: construct in feeds/normalize, consume in core.state / store; slots-only,
floats parsed once here, no further allocation on the hot path.
Depends on: stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass

BUY = "buy"
SELL = "sell"


@dataclass(slots=True)
class Tick:
    """One aggressor trade print."""

    ts_exch: float  # exchange timestamp, seconds since epoch
    ts_recv: float  # local wall-clock receive time, seconds since epoch
    ts_mono: float  # local monotonic receive time (pipeline latency measurement)
    symbol: str  # ui symbol
    price: float
    size: float  # base-asset size
    side: str  # BUY | SELL = aggressor direction
    trade_id: int
    venue: str


@dataclass(slots=True)
class BookTop:
    """Top-N mirror of the order book (full snapshot per update)."""

    ts: float  # exchange snapshot time, seconds
    ts_recv: float
    ts_mono: float
    symbol: str
    bids: list[tuple[float, float]]  # (price, size), best first, up to N levels
    asks: list[tuple[float, float]]
    spread_bps: float  # 1e4 * (ask - bid) / mid; 0 if one side empty
    imbalance: float  # bid_qty / (bid_qty + ask_qty) over mirrored levels; 0.5 if empty


@dataclass(slots=True)
class Bbo:
    """Best bid/offer change (higher cadence than the throttled l2Book feed)."""

    ts: float
    ts_recv: float
    ts_mono: float
    symbol: str
    bid_px: float  # 0.0 when that side is empty
    bid_sz: float
    ask_px: float
    ask_sz: float


@dataclass(slots=True)
class PerpCtx:
    """Perp context: funding / open interest / mark & oracle prices."""

    ts: float  # receive time; venue sends no timestamp on this channel
    ts_recv: float
    ts_mono: float
    symbol: str
    mark: float
    oracle: float
    mid: float  # 0.0 if venue sent null
    funding_rate: float  # current hourly funding rate (signed)
    open_interest: float  # base-asset units
    premium: float


@dataclass(slots=True)
class Mids:
    """Venue-published mid prices (allMids): independent reference for auditing."""

    ts_recv: float
    mids: dict[str, float]  # ui symbol -> mid


@dataclass(slots=True)
class FeedStatus:
    """Feed lifecycle + health, surfaced in the UI status bar."""

    ts: float
    venue: str
    connected: bool
    detail: str


Event = Tick | BookTop | Bbo | PerpCtx | Mids | FeedStatus
