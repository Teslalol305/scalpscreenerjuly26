"""Normalizer unit tests with hand-computed expected values."""

from __future__ import annotations

from tapescreen.core.events import BUY, SELL, Bbo, BookTop, PerpCtx, Tick
from tapescreen.core.normalize import Normalizer

MAP = {"BTC": "BTC", "ETH": "ETH"}


def test_trades_batch_fanout_and_sides() -> None:
    n = Normalizer(MAP)
    msg = {
        "channel": "trades",
        "data": [
            {"coin": "BTC", "side": "B", "px": "50000.5", "sz": "0.25",
             "time": 1700000000123, "hash": "0xab", "tid": 42, "users": ["0x1", "0x2"]},
            {"coin": "BTC", "side": "A", "px": "50000.0", "sz": "1.5",
             "time": 1700000000456, "hash": "0xcd", "tid": 43, "users": ["0x1", "0x2"]},
        ],
    }
    evs = n.normalize(msg, ts_recv=1000.0, ts_mono=5.0)
    assert len(evs) == 2
    t0, t1 = evs
    assert isinstance(t0, Tick) and isinstance(t1, Tick)
    assert t0.side == BUY and t1.side == SELL
    assert t0.price == 50000.5 and t0.size == 0.25
    assert t0.ts_exch == 1700000000.123
    assert t0.ts_recv == 1000.0 and t0.ts_mono == 5.0
    assert t0.trade_id == 42 and t0.venue == "hyperliquid"


def test_l2book_spread_and_imbalance_hand_computed() -> None:
    n = Normalizer(MAP, top_levels=10)
    msg = {
        "channel": "l2Book",
        "data": {
            "coin": "ETH",
            "time": 1700000001000,
            "levels": [
                [{"px": "2000.0", "sz": "3.0", "n": 2}, {"px": "1999.0", "sz": "1.0", "n": 1}],
                [{"px": "2001.0", "sz": "1.0", "n": 1}],
            ],
        },
    }
    (book,) = n.normalize(msg, ts_recv=1.0, ts_mono=2.0)
    assert isinstance(book, BookTop)
    # mid = (2000+2001)/2 = 2000.5 ; spread = 1.0/2000.5*1e4 = 4.99875... bps
    assert abs(book.spread_bps - (1.0 / 2000.5 * 1e4)) < 1e-9
    # imbalance = (3+1)/(3+1+1) = 0.8
    assert abs(book.imbalance - 0.8) < 1e-12
    assert book.bids[0] == (2000.0, 3.0) and book.asks[0] == (2001.0, 1.0)
    assert book.ts == 1700000001.0


def test_l2book_truncates_to_top_levels() -> None:
    n = Normalizer(MAP, top_levels=2)
    levels = [
        [{"px": str(100 - i), "sz": "1", "n": 1} for i in range(5)],
        [{"px": str(101 + i), "sz": "1", "n": 1} for i in range(5)],
    ]
    msg = {"channel": "l2Book", "data": {"coin": "BTC", "time": 0, "levels": levels}}
    (book,) = n.normalize(msg, 0.0, 0.0)
    assert len(book.bids) == 2 and len(book.asks) == 2


def test_bbo_with_null_side() -> None:
    n = Normalizer(MAP)
    msg = {
        "channel": "bbo",
        "data": {"coin": "BTC", "time": 1700000002500,
                 "bbo": [None, {"px": "50001.0", "sz": "0.5", "n": 3}]},
    }
    (bbo,) = n.normalize(msg, 1.0, 1.0)
    assert isinstance(bbo, Bbo)
    assert bbo.bid_px == 0.0 and bbo.bid_sz == 0.0
    assert bbo.ask_px == 50001.0 and bbo.ask_sz == 0.5


def test_active_asset_ctx_fields() -> None:
    n = Normalizer(MAP)
    msg = {
        "channel": "activeAssetCtx",
        "data": {"coin": "ETH", "ctx": {
            "funding": "-0.0000125", "openInterest": "12345.6", "prevDayPx": "1990.0",
            "dayNtlVlm": "1e6", "premium": "0.0002", "oraclePx": "2000.1",
            "markPx": "2000.2", "midPx": None, "impactPxs": None, "dayBaseVlm": "500.0"}},
    }
    (ctx,) = n.normalize(msg, 7.0, 8.0)
    assert isinstance(ctx, PerpCtx)
    assert ctx.funding_rate == -0.0000125
    assert ctx.open_interest == 12345.6
    assert ctx.mark == 2000.2 and ctx.oracle == 2000.1
    assert ctx.mid == 0.0  # null midPx -> 0.0
    assert ctx.ts == 7.0


def test_unknown_coin_and_channel_counted_not_raised() -> None:
    n = Normalizer(MAP)
    assert n.normalize({"channel": "trades", "data": [{"coin": "DOGE", "px": "1"}]}, 0, 0) == []
    assert n.normalize({"channel": "mystery", "data": {}}, 0, 0) == []
    assert n.dropped == 2
    # infra channels are silently ignored, not counted as drops
    assert n.normalize({"channel": "pong"}, 0, 0) == []
    assert n.normalize({"channel": "subscriptionResponse", "data": {}}, 0, 0) == []
    assert n.dropped == 2
