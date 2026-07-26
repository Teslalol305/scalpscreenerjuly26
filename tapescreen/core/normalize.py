"""Hyperliquid WS messages -> canonical events (Tick / BookTop / Bbo / PerpCtx).

Use: n = Normalizer(coin_to_symbol, top_levels=10); events = n.normalize(msg, ts_recv, ts_mono).
Depends on: core.events. Pure and deterministic: same (msg, timestamps) -> same events.
"""

from __future__ import annotations

from typing import Any

from tapescreen.core.events import BUY, SELL, Bbo, BookTop, Event, PerpCtx, Tick

VENUE = "hyperliquid"


def _f(v: Any) -> float:
    """Parse Hyperliquid decimal-string (or number) fields; None -> 0.0."""
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class Normalizer:
    """Stateless translator from Hyperliquid envelope messages to canonical events.

    Unknown channels and unknown coins are counted (``dropped``) but never raise:
    a malformed message must not take the pipeline down.
    """

    def __init__(self, coin_to_symbol: dict[str, str], top_levels: int = 10) -> None:
        self.coin_to_symbol = coin_to_symbol
        self.top_levels = top_levels
        self.dropped = 0

    def normalize(self, msg: dict[str, Any], ts_recv: float, ts_mono: float) -> list[Event]:
        channel = msg.get("channel")
        data = msg.get("data")
        if channel == "trades":
            return self._trades(data, ts_recv, ts_mono)
        if channel == "l2Book":
            return self._l2book(data, ts_recv, ts_mono)
        if channel == "bbo":
            return self._bbo(data, ts_recv, ts_mono)
        if channel == "activeAssetCtx":
            return self._ctx(data, ts_recv, ts_mono)
        if channel in ("subscriptionResponse", "pong", "allMids", "error"):
            return []  # handled (or intentionally ignored) upstream in the feed
        self.dropped += 1
        return []

    def _sym(self, coin: Any) -> str | None:
        sym = self.coin_to_symbol.get(coin)
        if sym is None:
            self.dropped += 1
        return sym

    def _trades(self, data: Any, ts_recv: float, ts_mono: float) -> list[Event]:
        if not isinstance(data, list):
            self.dropped += 1
            return []
        out: list[Event] = []
        for t in data:
            sym = self._sym(t.get("coin"))
            if sym is None:
                continue
            side = t.get("side")
            out.append(
                Tick(
                    ts_exch=_f(t.get("time")) / 1000.0,
                    ts_recv=ts_recv,
                    ts_mono=ts_mono,
                    symbol=sym,
                    price=_f(t.get("px")),
                    size=_f(t.get("sz")),
                    side=BUY if side == "B" else SELL,
                    trade_id=int(t.get("tid") or 0),
                    venue=VENUE,
                )
            )
        return out

    def _l2book(self, data: Any, ts_recv: float, ts_mono: float) -> list[Event]:
        if not isinstance(data, dict):
            self.dropped += 1
            return []
        sym = self._sym(data.get("coin"))
        if sym is None:
            return []
        levels = data.get("levels") or [[], []]
        n = self.top_levels
        bids = [(_f(lv.get("px")), _f(lv.get("sz"))) for lv in levels[0][:n]]
        asks = [(_f(lv.get("px")), _f(lv.get("sz"))) for lv in levels[1][:n]]
        spread_bps = 0.0
        if bids and asks:
            bb, ba = bids[0][0], asks[0][0]
            mid = (bb + ba) / 2.0
            if mid > 0:
                spread_bps = (ba - bb) / mid * 1e4
        bid_qty = sum(sz for _, sz in bids)
        ask_qty = sum(sz for _, sz in asks)
        total = bid_qty + ask_qty
        imbalance = bid_qty / total if total > 0 else 0.5
        return [
            BookTop(
                ts=_f(data.get("time")) / 1000.0,
                ts_recv=ts_recv,
                ts_mono=ts_mono,
                symbol=sym,
                bids=bids,
                asks=asks,
                spread_bps=spread_bps,
                imbalance=imbalance,
            )
        ]

    def _bbo(self, data: Any, ts_recv: float, ts_mono: float) -> list[Event]:
        if not isinstance(data, dict):
            self.dropped += 1
            return []
        sym = self._sym(data.get("coin"))
        if sym is None:
            return []
        bbo = data.get("bbo") or [None, None]
        bid, ask = (bbo + [None, None])[:2]
        return [
            Bbo(
                ts=_f(data.get("time")) / 1000.0,
                ts_recv=ts_recv,
                ts_mono=ts_mono,
                symbol=sym,
                bid_px=_f(bid.get("px")) if isinstance(bid, dict) else 0.0,
                bid_sz=_f(bid.get("sz")) if isinstance(bid, dict) else 0.0,
                ask_px=_f(ask.get("px")) if isinstance(ask, dict) else 0.0,
                ask_sz=_f(ask.get("sz")) if isinstance(ask, dict) else 0.0,
            )
        ]

    def _ctx(self, data: Any, ts_recv: float, ts_mono: float) -> list[Event]:
        if not isinstance(data, dict):
            self.dropped += 1
            return []
        sym = self._sym(data.get("coin"))
        if sym is None:
            return []
        ctx = data.get("ctx") or {}
        return [
            PerpCtx(
                ts=ts_recv,
                ts_recv=ts_recv,
                ts_mono=ts_mono,
                symbol=sym,
                mark=_f(ctx.get("markPx")),
                oracle=_f(ctx.get("oraclePx")),
                mid=_f(ctx.get("midPx")),
                funding_rate=_f(ctx.get("funding")),
                open_interest=_f(ctx.get("openInterest")),
                premium=_f(ctx.get("premium")),
            )
        ]
