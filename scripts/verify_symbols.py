"""Live symbol verification (Section 2 of the spec): run from a machine with API access.

Use: python scripts/verify_symbols.py [--config config.yaml] [--dry-run]
Calls POST /info {"type":"metaAndAssetCtxs"}, checks every watchlist symbol exists as
a tradable Hyperliquid perp, then rewrites symbol_map.yaml with status verified_live
plus szDecimals / maxLeverage / onlyIsolated / isDelisted. Depends on: stdlib + pyyaml.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tapescreen.config import load_config  # noqa: E402


def fetch_meta(info_url: str) -> tuple[list[dict], list[dict]]:
    req = urllib.request.Request(
        info_url,
        data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        meta, ctxs = json.load(resp)
    return meta["universe"], ctxs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify watchlist symbols against live Hyperliquid meta")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--dry-run", action="store_true", help="print results without rewriting the map")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    universe, _ctxs = fetch_meta(cfg.feed.info_url)
    by_name = {a["name"]: a for a in universe}

    rows: dict[str, dict] = {}
    problems: list[str] = []
    for ui, entry in cfg.symbols.items():
        asset = by_name.get(entry.venue_symbol)
        if asset is None:
            problems.append(f"{ui}: NOT FOUND on Hyperliquid (venue_symbol={entry.venue_symbol})")
            rows[ui] = {"venue": entry.venue, "venue_symbol": entry.venue_symbol, "status": "missing"}
            continue
        delisted = bool(asset.get("isDelisted", False))
        if delisted:
            problems.append(f"{ui}: listed but marked isDelisted=true")
        rows[ui] = {
            "venue": entry.venue,
            "venue_symbol": entry.venue_symbol,
            "status": "delisted" if delisted else "verified_live",
            "szDecimals": asset.get("szDecimals"),
            "maxLeverage": asset.get("maxLeverage"),
            "onlyIsolated": bool(asset.get("onlyIsolated", False)),
        }

    width = max(len(s) for s in rows)
    for ui, r in rows.items():
        extras = ", ".join(
            f"{k}={r[k]}" for k in ("szDecimals", "maxLeverage", "onlyIsolated") if k in r
        )
        print(f"{ui:<{width}}  {r['status']:<14}  {extras}")

    if not args.dry_run:
        import yaml

        map_path = Path(args.config).parent / cfg.raw["symbols_file"]
        map_path.write_text(
            "# Rewritten by scripts/verify_symbols.py from live metaAndAssetCtxs.\n"
            + yaml.safe_dump({"symbols": rows}, sort_keys=False),
        )
        print(f"\nwrote {map_path}")

    if problems:
        print("\nPROBLEMS:\n  " + "\n  ".join(problems), file=sys.stderr)
        return 1
    print(f"\nall {len(rows)} symbols verified live")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
